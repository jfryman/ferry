"""The operator refuses unpublished/dirty code before asking for any credential."""
import subprocess
from pathlib import Path

import pytest
from ferry.operator import OperatorError, verified_sha


def git(path, *args):
    return subprocess.run(['git', *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def repository(tmp_path):
    remote = tmp_path / 'remote.git'
    git(tmp_path, 'init', '--bare', str(remote))
    local = tmp_path / 'local'
    git(tmp_path, 'clone', str(remote), str(local))
    git(local, 'config', 'user.email', 'operator@example.com')
    git(local, 'config', 'user.name', 'Operator')
    (local / 'site.yml').write_text('[]\n')
    git(local, 'add', '.')
    git(local, 'commit', '-m', 'initial')
    git(local, 'push', 'origin', 'HEAD')
    return local


def test_clean_remote_sha(tmp_path):
    repo = repository(tmp_path)
    assert verified_sha(repo) == git(repo, 'rev-parse', 'HEAD')


def test_untracked_and_dirty_refused(tmp_path):
    repo = repository(tmp_path)
    (repo / 'untracked').write_text('not reviewed')
    with pytest.raises(OperatorError, match='dirty'):
        verified_sha(repo)
    (repo / 'untracked').unlink()
    (repo / 'site.yml').write_text('changed')
    with pytest.raises(OperatorError, match='dirty'):
        verified_sha(repo)


def test_unpublished_commit_refused(tmp_path):
    repo = repository(tmp_path)
    (repo / 'site.yml').write_text('new')
    git(repo, 'commit', '-am', 'unpublished')
    with pytest.raises(OperatorError, match='not on origin'):
        verified_sha(repo)


def test_deleted_remote_branch_not_trusted(tmp_path):
    repo = repository(tmp_path)
    git(repo, 'checkout', '-b', 'temporary')
    (repo / 'site.yml').write_text('new')
    git(repo, 'commit', '-am', 'temporary')
    git(repo, 'push', 'origin', 'temporary')
    git(repo, 'push', 'origin', '--delete', 'temporary')
    with pytest.raises(OperatorError, match='not on origin'):
        verified_sha(repo)


@pytest.mark.parametrize('exit_code', [0, 7])
@pytest.mark.parametrize('output_kind', ['normal', 'short-multiline', 'overflow'])
def test_operator_redacts_both_streams_and_preserves_exit(tmp_path, monkeypatch, capsys, exit_code, output_kind):
    import json
    import time
    from types import SimpleNamespace
    import ferry.operator as operator
    repo = repository(tmp_path)
    (repo/'ferry.yaml').write_text('inventories:\n  - name: demo\n    path: inventory\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'config')
    git(repo, 'push', 'origin', 'HEAD')
    binary = tmp_path/'bin'
    binary.mkdir()
    executable = binary/'ansible-playbook'
    password = 'abc' if output_kind == 'short-multiline' else 'vault-output-123'
    cert = 'line1\nline2' if output_kind == 'short-multiline' else 'certificate-output-456'
    body = 'import sys\nprint('+repr(password)+')\nprint('+repr(cert)+', file=sys.stderr)\n'
    if output_kind == 'overflow':
        body += 'print("x" * (2*1024*1024))\n'
    executable.write_text('#!/usr/bin/env python3\n'+body+'sys.exit('+str(exit_code)+')\n')
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary)+':'+__import__('os').environ['PATH'])
    monkeypatch.setattr(operator, 'ferry_ansible_env', lambda *args: {})
    requests = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args):
            return json.dumps(dict(run_id='a'*32, certificate=cert,
                vault_password=password, lease_expires_at=time.time()+300,
                inventory_path='inventory')).encode()
    class Opener:
        def open(self, request, **kw):
            requests.append(json.loads(request.data))
            return Response()
    monkeypatch.setattr(operator.urllib.request, 'build_opener', lambda *args: Opener())
    args = SimpleNamespace(repo=repo, service='http://localhost:18080', insecure_http=False,
        playbook='site.yml', token_command='printf identity-test', inventory='demo', limit=None, apply=False)
    assert operator.run(args) == exit_code
    out = capsys.readouterr()
    assert password not in out.out and cert not in out.out and not out.err
    if output_kind == 'overflow':
        assert 'output truncated' in out.out and len(out.out) < 512
    else:
        assert out.out.count('[REDACTED]') == 2
    assert requests[0]['playbook'] == 'site.yml'
    assert requests[-1]['ok'] == (exit_code == 0)


def run_operator(tmp_path, monkeypatch, body, extra_bin=None, opener=None, **overrides):
    """Run operator.run against a fake service and a fake ansible-playbook (a Python body)."""
    import json
    import time
    from types import SimpleNamespace
    import ferry.operator as operator
    repo = repository(tmp_path)
    (repo/'ferry.yaml').write_text('inventories:\n  - name: demo\n    path: inventory\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'config')
    git(repo, 'push', 'origin', 'HEAD')
    binary = tmp_path/'bin'
    binary.mkdir()
    executable = binary/'ansible-playbook'
    executable.write_text('#!/usr/bin/env python3\nimport os, sys\n' + body)
    executable.chmod(0o755)
    for name, script in (extra_bin or {}).items():
        (binary/name).write_text(script)
        (binary/name).chmod(0o755)
    monkeypatch.setenv('PATH', str(binary)+':'+__import__('os').environ['PATH'])
    monkeypatch.setattr(operator, 'ferry_ansible_env', lambda *args: {})
    requests = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args):
            return json.dumps(dict(run_id='a'*32, certificate='cert-test-only', vault_password='',
                lease_expires_at=time.time()+300, inventory_path='inventory')).encode()
    class Opener:
        def open(self, request, **kw):
            requests.append(json.loads(request.data))
            return Response()
    monkeypatch.setattr(operator.urllib.request, 'build_opener', lambda *args: opener or Opener())
    args = dict(repo=repo, service='http://localhost:18080', insecure_http=False, no_host_key_checking=False,
                playbook='site.yml', token_command='printf identity-test', inventory='demo', limit=None,
                apply=False, reason=None)
    args.update(overrides)
    return operator.run(SimpleNamespace(**args)), requests


@pytest.mark.parametrize('insecure_http,no_hkc,expected', [
    (False, False, 'True'), (True, False, 'True'), (False, True, 'False'), (True, True, 'False')])
def test_host_key_checking_is_its_own_flag(tmp_path, monkeypatch, capsys, insecure_http, no_hkc, expected):
    rc, _ = run_operator(tmp_path, monkeypatch, 'print("HKC=" + os.environ["ANSIBLE_HOST_KEY_CHECKING"])\n',
                         insecure_http=insecure_http, no_host_key_checking=no_hkc)
    assert rc == 0
    assert f'HKC={expected}' in capsys.readouterr().out


@pytest.mark.parametrize('reason', [None, 'restore DNS after failed upgrade'])
def test_reason_is_sent_to_the_service(tmp_path, monkeypatch, capsys, reason):
    rc, requests = run_operator(tmp_path, monkeypatch, 'sys.exit(0)\n', reason=reason)
    assert rc == 0
    assert requests[0]['reason'] == reason


def test_ssh_keygen_failure_is_a_clean_error(tmp_path, monkeypatch):
    import ferry.operator as operator
    keygen = '#!/bin/sh\necho "No user exists for uid 4242" >&2\nexit 255\n'
    with pytest.raises(operator.OperatorError) as e:
        run_operator(tmp_path, monkeypatch, 'sys.exit(0)\n', extra_bin={'ssh-keygen': keygen})
    assert str(e.value) == "couldn't generate a key (ssh-keygen exit 255): No user exists for uid 4242"


def http_error(code, ctype, body):
    import io
    import urllib.error
    from email.message import Message
    headers = Message()
    if ctype:
        headers['Content-Type'] = ctype
    return urllib.error.HTTPError('https://ferry.example.com/api/runs', code, 'x', headers, io.BytesIO(body))


@pytest.mark.parametrize('ctype,body,expected', [
    ('application/json', b'{"error": "apply requires the main head (abc): merge and pull, then retry"}',
     'apply requires the main head (abc): merge and pull, then retry'),
    ('application/json', b'{"error": "inventory\\nlocked\\u001b[2J"}', 'inventory locked [2J'),
    ('application/json', b'{"error": "' + b'x' * 1000 + b'"}', 'x' * 300),
    ('text/html', b'<html>proxy error</html>', None),
    ('application/json', b'not json', None),
    ('application/json', b'{"error": 7}', None),
    (None, b'{"error": "no content type"}', None),
])
def test_refusal_reason(ctype, body, expected):
    from ferry.operator import refusal_reason
    assert refusal_reason(http_error(409, ctype, body)) == expected


def test_refusal_reason_reaches_the_operator(tmp_path, monkeypatch):
    import ferry.operator as operator
    class Refusing:
        def open(self, request, **kw):
            raise http_error(409, 'application/json', b'{"error": "inventory locked"}')
    with pytest.raises(operator.OperatorError) as e:
        run_operator(tmp_path, monkeypatch, 'sys.exit(0)\n', opener=Refusing())
    assert str(e.value) == 'service refused request (HTTP 409): inventory locked'
