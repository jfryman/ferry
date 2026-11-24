"""Local operator driver: identity to service; playbooks get only this run's credentials."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .runner import ferry_ansible_env
from .results import parse_file
from .sanitize import Redactor
from . import config


class OperatorError(RuntimeError):
    pass


def refusal_reason(err):
    """The service's short JSON `error` text (#67); None for anything else (a proxy's page)."""
    try:
        if 'application/json' not in (err.headers.get('Content-Type') or ''):
            return None
        text = json.loads(err.read(4096)).get('error')
    except Exception:
        return None
    if not isinstance(text, str):
        return None
    text = ' '.join(''.join(c if c.isprintable() else ' ' for c in text).split())[:300]
    return text or None


def git(repo, *args):
    p = subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise OperatorError('git verification failed')
    return p.stdout.strip()


def verified_sha(repo):
    if git(repo, 'status', '--porcelain', '--untracked-files=all'):
        raise OperatorError('refusing dirty tree (including untracked files)')
    sha = git(repo, 'rev-parse', 'HEAD')
    # Fetch freshly: stale remote-tracking branches are not evidence of a published commit.
    git(repo, 'fetch', '--prune', 'origin')
    if not git(repo, 'branch', '-r', '--contains', sha, '--list', 'origin/*'):
        raise OperatorError('HEAD is not on origin')
    return sha


def run(args):
    repo = Path(args.repo).resolve()
    url = args.service.rstrip('/')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
        raise OperatorError('invalid service URL')
    if parsed.scheme != 'https' and parsed.hostname not in ('localhost', '127.0.0.1', '::1') and not args.insecure_http:
        raise OperatorError('HTTPS required (local lab: --insecure-http)')
    sha = verified_sha(repo)
    playbook = (repo / args.playbook).resolve()
    if not playbook.is_relative_to(repo) or not playbook.is_file():
        raise OperatorError('playbook must be inside the clean repository')
    command = shlex.split(args.token_command)
    if not command:
        raise OperatorError('empty token command')

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None  # Never forward bearer credentials to a redirected URL.
    opener = urllib.request.build_opener(NoRedirect())

    def request(path, data):
        # No shell, no token in argv, no credential subprocess output on errors.
        try:
            p = subprocess.run(command, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            raise OperatorError('identity token command failed') from None
        if p.returncode or not p.stdout.strip():
            raise OperatorError('identity token command failed')
        token = p.stdout.strip()
        req = urllib.request.Request(url + path, data=json.dumps(data).encode(), method='POST',
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        try:
            with opener.open(req, timeout=10) as resp:
                return json.loads(resp.read(1024 * 1024))
        except urllib.error.HTTPError as e:
            # Service error text is intentionally small and credential-free, but do not trust a proxy's.
            reason = refusal_reason(e)
            raise OperatorError(f'service refused request (HTTP {e.code})' + (f': {reason}' if reason else '')) from None
        except Exception:
            raise OperatorError('service request failed') from None

    stop = threading.Event()
    lease_failed = threading.Event()
    proc = None
    run_id = None
    renewer = None
    ok = False
    counts, changed = {}, []
    old_handlers = {}
    reader = None

    def interrupted(signum, frame):
        raise KeyboardInterrupt()

    def terminate():
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            except ProcessLookupError:
                pass

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, interrupted)
        with tempfile.TemporaryDirectory(prefix='ferry-run-') as temp:
            tmp = Path(temp)
            os.chmod(tmp, 0o700)
            key = tmp / 'id_ed25519'
            try:
                subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)],
                               check=True, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as e:
                # (#68) e.g. "No user exists for uid ...": ssh needs a passwd entry for this uid too.
                detail = (getattr(e, 'stderr', None) or str(e)).strip().splitlines()[-1:] or ['']
                code = getattr(e, 'returncode', None)
                raise OperatorError(f"couldn't generate a key (ssh-keygen"
                                    + (f" exit {code}" if code is not None else "") + f"): {detail[0][:200]}") from None
            try:
                result = request('/api/runs', dict(inventory=args.inventory, limit=args.limit,
                    mode='apply' if args.apply else 'check', sha=sha, reason=getattr(args, 'reason', None), playbook=playbook.relative_to(repo).as_posix(), public_key=key.with_suffix('.pub').read_text()))
                run_id = result['run_id']
                print(f'ferry run: {run_id} ({"apply" if args.apply else "check"})', flush=True)
                (tmp / 'id_ed25519-cert.pub').write_text(result['certificate'])
                vault = tmp / 'vault-password'
                vault.write_text(result['vault_password'])
                os.chmod(vault, 0o600)
                expires = [result['lease_expires_at']]
                def lease():
                    while not stop.wait(max(0.1, min(30, (expires[0]-time.time()) / 3))):
                        try:
                            answer = request(f'/api/runs/{run_id}/lease', {})
                            expires[0] = answer['lease_expires_at']
                            if expires[0] <= time.time():
                                raise OperatorError('lease expired')
                        except Exception:
                            lease_failed.set()
                            terminate()
                            return
                renewer = threading.Thread(target=lease, daemon=True)
                renewer.start()
                # No inherited SSH agent, OpenBao token, vault configuration, Ansible plugins or service env.
                env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL') if k in os.environ}
                env.update(HOME=str(tmp), ANSIBLE_PRIVATE_KEY_FILE=str(key),
                    ANSIBLE_SSH_CONTROL_PATH_DIR=str(tmp / 'cp'),
                    ANSIBLE_SSH_ARGS='-o IdentitiesOnly=yes -o ControlMaster=auto -o ControlPersist=30s',
                    ANSIBLE_HOST_KEY_CHECKING='False' if getattr(args, 'no_host_key_checking', False) else 'True')
                if result.get('ssh_user'):
                    env['ANSIBLE_REMOTE_USER'] = result['ssh_user']
                if result['vault_password']:
                    env['ANSIBLE_VAULT_PASSWORD_FILE'] = str(vault)
                known = Path.home() / '.ssh' / 'known_hosts'
                if known.is_file():
                    (tmp / '.ssh').mkdir(mode=0o700)
                    (tmp / '.ssh' / 'known_hosts').write_bytes(known.read_bytes())
                events = tmp / 'events.jsonl'
                env.update(ferry_ansible_env(env, str(events)))
                inventory = result['inventory_path']
                if not (repo / inventory).resolve().is_relative_to(repo):
                    raise OperatorError('inventory outside repository')
                cmd = ['ansible-playbook', '-i', inventory, str(playbook)]
                if not args.apply:
                    cmd += ['--check', '--diff']
                if args.limit:
                    cmd += ['--limit', args.limit]
                cfg = config.load(repo / 'ferry.yaml')
                red = Redactor(cfg.redact.patterns, cfg.redact.env_vars, cfg.redact.sensitive_paths,
                               environ=env, values=[result['vault_password'], result['certificate'], key.read_text()])
                # Merge streams at the OS pipe, drain continuously, retain at most 1 MiB.
                # Whole-output redaction is intentional: arbitrary multiline patterns/PEM blocks
                # cannot be safely emitted incrementally. On overflow discard ALL captured text,
                # not a raw/truncated prefix which could contain an incomplete secret.
                output = bytearray()
                truncated = threading.Event()
                proc = subprocess.Popen(cmd, cwd=repo, env=env, start_new_session=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                def drain():
                    while True:
                        chunk = proc.stdout.read(8192)
                        if not chunk:
                            break
                        if not truncated.is_set():
                            if len(output) + len(chunk) > 1024 * 1024:
                                output.clear()
                                truncated.set()
                            else:
                                output.extend(chunk)
                reader = threading.Thread(target=drain, daemon=True)
                reader.start()
                # Lease failure before launch must not leave a live playbook.
                if lease_failed.is_set():
                    terminate()
                while proc.poll() is None:
                    # Renewal transport may stall: stop at the already granted lease deadline,
                    # not only once a failed HTTP request eventually returns.
                    if time.time() >= expires[0]:
                        lease_failed.set()
                        terminate()
                        break
                    try:
                        proc.wait(timeout=0.1)
                    except subprocess.TimeoutExpired:
                        pass
                rc = proc.wait()
                reader.join(timeout=3)
                if reader.is_alive():
                    print('ferry run: output truncated (descendant kept pipe open)', flush=True)
                elif truncated.is_set():
                    print('ferry run: output truncated (1 MiB limit; captured output withheld)', flush=True)
                else:
                    sys.stdout.write(red.text(output.decode('utf-8', errors='replace')))
                    sys.stdout.flush()
                ok = rc == 0 and not lease_failed.is_set()
                if events.exists():
                    summary = parse_file(events, 'FERRY_HOST_BUSY', rc)
                    counts = summary.counts()
                    changed = [name for name, h in summary.hosts.items() if h.state == 'changed']
                return rc if rc > 0 else (0 if ok else 1)
            finally:
                terminate()
                if reader is not None:
                    reader.join(timeout=3)
                    if not reader.is_alive() and proc.stdout:
                        proc.stdout.close()
                stop.set()
                if renewer:
                    renewer.join(timeout=25)
                if run_id:
                    try:
                        request(f'/api/runs/{run_id}/finish', dict(ok=ok, counts=counts, changed_hosts=changed))
                    except Exception:
                        print('ferry run: finish unavailable; service lease will expire', flush=True)
    except KeyboardInterrupt:
        return 130
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
