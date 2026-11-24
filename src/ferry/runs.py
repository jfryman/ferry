"""Service-owned operator leases. SQLite holds metadata only, never credentials or identity tokens."""
import base64
import json
import logging
import re
import sqlite3
import struct
import threading
import unicodedata
import uuid
from contextlib import closing
from pathlib import Path

from . import config
from .run_identity import IdentityVerifier, IdentityError
from .workspace import Workspace


def certificate_details(cert):
    """Read the signed OpenSSH cert's serial and validity (not a configured TTL guess)."""
    blob = base64.b64decode(cert.split()[1], validate=True)
    pos = 0
    def string():
        nonlocal pos
        n = struct.unpack_from('>I', blob, pos)[0]
        pos += 4
        value = blob[pos:pos+n]
        if len(value) != n:
            raise ValueError('truncated certificate')
        pos += n
        return value
    if string() != b'ssh-ed25519-cert-v01@openssh.com':
        raise ValueError('not an ed25519 certificate')
    string()  # nonce
    string()  # public key
    serial, kind = struct.unpack_from('>QI', blob, pos)
    pos += 12
    string()  # key id (role-controlled, never overridden)
    string()  # principals
    start, end = struct.unpack_from('>QQ', blob, pos)
    if kind != 1:
        raise ValueError('not a user certificate')
    return str(serial), end


class RunError(ValueError):
    def __init__(self, status, text):
        self.status, self.text = status, text


class Runs:
    def __init__(self, service, verifier=None):
        self.svc = service
        s = service.settings
        self.verifier = verifier or IdentityVerifier(s.run_oidc_issuer, s.run_oidc_audience, clock=service.clock)
        self.mutex = threading.RLock()
        self.config_mutex = threading.Lock()
        self.inflight = set()  # signing owns its lock until the bounded upstream calls return
        # API intentionally requires persistent service locks, not a second lock store.
        self.path = service.locks.path
        with closing(self.db()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)')

    def db(self):
        return sqlite3.connect(self.path, timeout=30, isolation_level=None)

    def save(self, run):
        with closing(self.db()) as db:
            db.execute('INSERT OR REPLACE INTO runs VALUES (?, ?)', (run['run_id'], json.dumps(run)))

    def expire(self):
        with self.mutex, closing(self.db()) as db:
            for (data,) in db.execute('SELECT data FROM runs').fetchall():
                run = json.loads(data)
                if run['run_id'] not in self.inflight and run['lease_expires_at'] <= self.svc.clock():
                    self.complete(run, 'error')

    def complete(self, run, result):
        # Record before releasing the lock; a failed audit write fails closed.
        now = self.svc.clock()
        self.svc.state.record_job({**run, 'action': 'apply' if run['mode'] == 'apply' else 'plan',
            'trigger': 'cli', 'user': run['subject'], 'result': result,
            'seconds': max(0, now-run['started'])})
        self.svc.metrics.job_done('apply' if run['mode'] == 'apply' else 'plan', result,
                                 max(0, now-run['started']), now, trigger='cli')
        self.svc.locks.release(run['inventory'], run['holder'])
        with closing(self.db()) as db:
            db.execute('DELETE FROM runs WHERE id = ?', (run['run_id'],))

    def request(self, path, headers, body):
        try:
            if len(body) > self.svc.settings.max_body_bytes:
                raise RunError(413, 'body too large')
            auth = headers.get('Authorization', '')
            if not auth.startswith('Bearer '):
                raise RunError(401, 'identity required')
            claims = self.verifier.verify(auth[7:])
            subjects = self.svc.settings.run_allowed_subjects
            groups = self.svc.settings.run_allowed_groups
            # Empty entries never match: unset env lists split to ('',), and a groups claim may hold "".
            subjects = {x for x in subjects if x}
            groups = {x for x in groups if x}
            if claims['sub'] not in subjects and not {g for g in claims.get('groups', []) if g} & groups:
                raise RunError(403, 'subject not allowed')
            try:
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError()
            except ValueError:
                raise RunError(400, 'bad json')
            self.svc.recover_drift()
            self.expire()
            if path == '/api/runs':
                return 201, self.start(claims, data)
            with self.mutex:
                m = re.fullmatch(r'/api/runs/([a-f0-9]{32})/(lease|finish)', path)
                if not m:
                    raise RunError(404, 'unknown run')
                with closing(self.db()) as db:
                    row = db.execute('SELECT data FROM runs WHERE id = ?', (m[1],)).fetchone()
                if not row:
                    raise RunError(404, 'unknown run')
                run = json.loads(row[0])
                if run['subject'] != claims['sub']:
                    raise RunError(403, 'different subject')
                if run.get('state', 'active') != 'active':
                    raise RunError(409, 'run pending')
                if m[2] == 'lease':
                    run['lease_expires_at'] = min(self.svc.clock() + self.svc.settings.run_lease_seconds,
                                                  run['certificate_expires_at'])
                    self.save(run)
                    return 200, {'lease_expires_at': run['lease_expires_at']}
                if not isinstance(data.get('ok'), bool):
                    raise RunError(400, 'ok must be boolean')
                # Client-supplied results are assertions, not evidence. Keep bounded numeric totals
                # only; no task output, arbitrary field names or free-text messages in the audit.
                counts = data.get('counts', {})
                if not isinstance(counts, dict) or any(k not in ('hosts', 'changed_tasks', 'failed_tasks') or
                        not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 10**9
                        for k, v in counts.items()):
                    raise RunError(400, 'invalid counts')
                run['counts'] = counts
                self.complete(run, 'ok' if data['ok'] else 'failed')
                return 200, {'ok': True}
        except IdentityError:
            return 401, {'error': 'invalid identity token'}
        except RunError as e:
            return e.status, {'error': e.text}
        except Exception as e:
            logging.getLogger("ferry.runs").warning("run unavailable (%s)", type(e).__name__)
            # Never echo upstream errors or bodies containing credentials.
            return 503, {'error': 'run unavailable'}

    def start(self, claims, data):
        svc, now = self.svc, self.svc.clock()
        if data.get('mode') not in ('check', 'apply') or not re.fullmatch('[a-f0-9]{40}', str(data.get('sha', ''))):
            raise RunError(400, 'invalid mode or sha')
        playbook = data.get('playbook')
        if not isinstance(playbook, str) or not 0 < len(playbook) <= 512 or playbook.startswith('/') or '\\' in playbook or any(p in ('', '.', '..') for p in playbook.split('/')) or any(ord(c) < 32 or ord(c) == 127 for c in playbook):
            raise RunError(400, 'invalid repo-relative playbook')
        limit = data.get('limit')
        if limit is not None and (not isinstance(limit, str) or len(limit) > 1024 or any(ord(c)<32 for c in limit)):
            raise RunError(400, 'invalid limit')
        # Validate before locking or calling OpenBao. Only throwaway ed25519 public keys accepted.
        try:
            from cryptography.hazmat.primitives.serialization import load_ssh_public_key
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            pub = data['public_key']
            if not isinstance(pub, str) or len(pub) > 1024 or not isinstance(load_ssh_public_key(pub.encode()), Ed25519PublicKey):
                raise ValueError()
        except Exception:
            raise RunError(400, 'invalid public key')
        off = svc._disabled('apply')
        if off:
            raise RunError(403, 'CLI runs disabled while applies are disabled: check/apply is client-chosen')
        if svc.bao is None:
            raise RunError(503, 'credentials not configured')
        # Separate main checkout from the webhook worker: it may be reading its own main directory.
        ws = Workspace(svc.workspace.root / 'operator-config', svc.workspace.remote,
                       svc.workspace._token, git_user=svc.workspace.git_user,
                       git_timeout=svc.workspace.git_timeout)
        # Slow config fetches serialize only other starts, never existing lease renewals.
        with self.config_mutex:
            main, head = ws.main(svc.settings.default_branch)
            cfg = config.load(main / 'ferry.yaml')
        # An apply runs only the reviewed default-branch head (the CLI analogue of refusing a stale
        # PR plan). Check mode may name any published SHA. The SHA is still a client assertion.
        if data['mode'] == 'apply' and data['sha'] != head:
            raise RunError(409, f"apply requires the {svc.settings.default_branch} head ({head[:12]}): "
                                "merge and pull, then retry")
        try:
            inv = cfg.inventory(data.get('inventory'))
        except KeyError:
            raise RunError(400, 'unknown inventory')
        allowed = cfg.apply_for(inv.name).run_allowed
        identities = {claims['sub'], *(g for g in claims.get('groups', []) if g)}
        if not identities.intersection(allowed):
            raise RunError(403, 'inventory not allowed')
        if svc._disabled('apply'):
            raise RunError(403, 'CLI runs disabled while applies are disabled: check/apply is client-chosen')
        now = svc.clock()
        rid = uuid.uuid4().hex
        name = claims.get(svc.settings.run_name_claim)
        if not isinstance(name, str):
            name = claims['sub']
        name = ''.join(c for c in name if not unicodedata.category(c).startswith('C'))
        name = ' '.join(name.split())[:128] or claims['sub']
        holder = f"run:{rid} ({name}; sub={claims['sub']})"
        run = dict(run_id=rid, holder=holder, subject=claims['sub'], display_name=name, inventory=inv.name,
                   limit=limit, mode=data['mode'], sha=data['sha'], playbook=playbook, state='pending', started=now,
                   certificate_serial=None, certificate_expires_at=now + svc.settings.run_lease_seconds,
                   lease_expires_at=now + svc.settings.run_lease_seconds)
        # Same table/transaction as PR locks. Persist the initial lease atomically with acquire:
        # a service crash between separate inserts must not leave an immortal operator holder.
        with self.mutex, closing(self.db()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                held = db.execute('SELECT holder FROM locks WHERE key = ?', (inv.name,)).fetchone()
                if held:
                    raise RunError(409, 'inventory locked')
                db.execute('INSERT INTO locks (key, holder, sha, user, since) VALUES (?, ?, ?, ?, ?)',
                           (inv.name, holder, data['sha'], claims['sub'], now))
                db.execute('INSERT INTO runs VALUES (?, ?)', (rid, json.dumps(run)))
                db.execute('COMMIT')
                self.inflight.add(rid)
            except BaseException:
                db.execute('ROLLBACK')
                raise
        token = None
        try:
            token = svc.bao.login()
            if svc._disabled('apply'):
                raise RunError(403, 'CLI runs disabled while applies are disabled: check/apply is client-chosen')
            cert = svc.bao.sign_ssh_key(token, pub)
            serial, expires = certificate_details(cert)
            if expires <= svc.clock():
                raise ValueError('certificate expired')
            password = svc.bao.read_field(token, svc.bao.s.vault_password_path, svc.bao.s.vault_password_field) \
                       if svc.bao.s.vault_password_path else ''
            svc.bao.revoke(token)
            token = None
            with self.mutex:
                # Expiry skips in-flight signing so no certificate is minted after releasing its
                # lock. A slow start still fails, rather than resurrecting an elapsed initial lease.
                if svc.clock() >= run['lease_expires_at'] or svc.clock() >= expires:
                    raise RunError(503, 'initial lease expired')
                with closing(self.db()) as db:
                    persisted = db.execute('SELECT data FROM runs WHERE id = ?', (rid,)).fetchone()
                    held = db.execute('SELECT holder FROM locks WHERE key = ?', (inv.name,)).fetchone()
                if not persisted or json.loads(persisted[0]).get('state') != 'pending' or not held or held[0] != holder:
                    raise RunError(409, 'run or lock lost during start')
                if svc._disabled('apply'):
                    raise RunError(403, 'CLI runs disabled while applies are disabled: check/apply is client-chosen')
                run.update(state='active', certificate_serial=serial, certificate_expires_at=expires,
                           lease_expires_at=min(svc.clock() + svc.settings.run_lease_seconds, expires))
                self.save(run)
                # Fail closed if the issuance audit cannot be persisted. No credentials in this row.
                svc.state.record_job({**run, 'action': 'apply' if run['mode'] == 'apply' else 'plan',
                    'trigger': 'cli', 'user': run['subject'], 'result': 'issued', 'seconds': 0})
                self.inflight.discard(rid)
            return dict(run_id=rid, certificate=cert, vault_password=password,
                        lease_expires_at=run['lease_expires_at'], inventory_path=inv.path,
                        ssh_user=svc.settings.ssh_user)
        except BaseException:
            with self.mutex:
                svc.locks.release(inv.name, holder)
                with closing(self.db()) as db:
                    db.execute('DELETE FROM runs WHERE id = ?', (rid,))
                self.inflight.discard(rid)
            raise
        finally:
            if token:
                svc.bao.revoke(token)
