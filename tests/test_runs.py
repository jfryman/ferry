"""Operator API security gates: real RSA identities, SQLite persistence, no network Bao."""
import base64
import hashlib
import hmac
import http.client
import json
import sqlite3
import struct
import threading
from contextlib import closing
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from ferry import config
from ferry.engine import Engine
from ferry.locks import new_lock
from ferry.run_identity import IdentityVerifier
from ferry.runs import Runs, duration_seconds
from ferry.service import make_handler
from ferry.state import SQLiteState
from ferry.workspace import Workspace
from test_service import NOW, World

ISSUER = 'https://identity.example/v1/identity/oidc/provider/ferry'
AUDIENCE = 'ferry-operators'
SUBJECT = 'operator-example'
OTHER = 'other-operator-example'


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


@pytest.fixture(scope='module')
def signer():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_numbers()
    jwks = {'keys': [{'kid': 'test-key', 'kty': 'RSA', 'alg': 'RS256', 'use': 'sig',
                      'n': b64(public.n.to_bytes((public.n.bit_length()+7)//8, 'big')),
                      'e': b64(public.e.to_bytes((public.e.bit_length()+7)//8, 'big'))}]}

    def token(overrides=None, *, alg='RS256', bad_signature=False):
        claims = dict(iss=ISSUER, aud=AUDIENCE, sub=SUBJECT, exp=NOW+3600, iat=NOW-1)
        claims.update(overrides or {})
        message = (b64(json.dumps({'alg': alg, 'kid': 'test-key'}).encode()) + '.' +
                   b64(json.dumps(claims).encode())).encode()
        if alg == 'none':
            signature = b''
        elif alg == 'HS256':
            # The classic RSA-public-key-as-HMAC-secret algorithm confusion attempt.
            pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                               serialization.PublicFormat.SubjectPublicKeyInfo)
            signature = hmac.new(pem, message, hashlib.sha256).digest()
        else:
            signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if bad_signature else key
            signature = signing_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        return message.decode() + '.' + b64(signature)

    return SimpleNamespace(token=token, jwks=jwks)


def certificate(expires, serial=42):
    """Minimal binary OpenSSH certificate fields consumed by certificate_details, not a mock parser."""
    def string(value):
        return struct.pack('>I', len(value)) + value
    blob = (string(b'ssh-ed25519-cert-v01@openssh.com') + string(b'nonce') + string(b'k'*32) +
            struct.pack('>QI', serial, 1) + string(b'test-role') + string(string(b'root')) +
            struct.pack('>QQ', int(NOW-1), int(expires)))
    return 'ssh-ed25519-cert-v01@openssh.com ' + base64.b64encode(blob).decode()


@pytest.fixture
def api(repo, tmp_path, monkeypatch, signer):
    world = World(repo, tmp_path/'operator-world')
    world.now = NOW
    world.svc.clock = lambda: world.now
    settings = world.svc.settings
    settings.run_oidc_issuer = ISSUER
    settings.run_oidc_audience = AUDIENCE
    settings.run_allowed_subjects = (SUBJECT, OTHER)
    settings.run_allowed_groups = ('operators-example',)
    settings.run_lease_seconds = 60
    world.svc.state = SQLiteState(world.locks.path)
    world.svc.runs = Runs(world.svc, IdentityVerifier(ISSUER, AUDIENCE,
                         fetch=lambda: signer.jwks, clock=world.svc.clock))
    # Config is independently sourced from the configured default branch, not the requested SHA.
    trusted = tmp_path/'trusted-main'
    trusted.mkdir()
    (trusted/'ferry.yaml').write_text('''inventories:
  - name: homelab
    path: inventories/homelab
    apply:
      run_allowed: [operator-example, operators-example]
  - name: restricted
    path: inventories/restricted
    apply:
      run_allowed: [restricted-operator-example]
''')
    world.main = Mock(return_value=(trusted, world.sha))
    monkeypatch.setattr(Workspace, 'main', world.main)
    world.bao = Mock()
    world.bao.s = SimpleNamespace(vault_password_path='secret/example', vault_password_field='password')
    world.bao.login.return_value = 'test-only-bao-token'
    world.bao.sign_ssh_key.return_value = certificate(NOW+180)
    world.bao.read_field.return_value = 'test-only-vault-password'
    world.svc.bao = world.bao
    pub = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    world.payload = dict(playbook='site.yml', mode='apply', inventory='homelab', sha=world.sha, public_key=pub, limit='example-host')
    world.token = signer.token

    def request(path='/api/runs', data=None, token=None, body=None):
        return world.svc.runs.request(path, {'Authorization': 'Bearer '+(token or signer.token())},
                                      body if body is not None else json.dumps(world.payload if data is None else data).encode())
    world.request = request
    return world


def assert_not_minted(api):
    assert api.bao.mock_calls == []
    assert api.locks.count() == 0
    with sqlite3.connect(api.locks.path) as db:
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0
    # A refusal for a verified identity is audited, but nothing is issued.
    assert all(j['result'] == 'refused' for j in api.svc.state.jobs())


@pytest.mark.parametrize('attack', ['signature', 'issuer', 'audience', 'expired', 'none', 'HS256'])
def test_identity_failures_never_mint(api, attack):
    overrides = {'issuer': {'iss': 'https://attacker.example'}, 'audience': {'aud': 'other-service'},
                 'expired': {'exp': NOW}}.get(attack)
    token = api.token(overrides, alg=attack if attack in ('none', 'HS256') else 'RS256',
                      bad_signature=attack == 'signature')
    assert api.request(token=token) == (401, {'error': 'invalid identity token'})
    assert_not_minted(api)


def test_missing_identity_never_mints(api):
    assert api.svc.runs.request('/api/runs', {}, json.dumps(api.payload).encode()) == (
        401, {'error': 'identity required'})
    assert_not_minted(api)


@pytest.mark.parametrize('body', [b'{', b'[]', b'null'])
def test_bad_json_never_mints(api, body):
    assert api.request(body=body) == (400, {'error': 'bad json'})
    assert_not_minted(api)


def test_unavailable_config_never_mints(api):
    api.main.side_effect = RuntimeError('private upstream error')
    assert api.request() == (503, {'error': 'run unavailable'})
    assert_not_minted(api)


def test_unconfigured_credentials_never_mint(api):
    api.svc.bao = None
    assert api.request() == (503, {'error': 'credentials not configured'})
    assert_not_minted(api)


def test_unauthorized_subject_never_mints(api):
    assert api.request(token=api.token({'sub': 'unauthorized-example'}))[0] == 403
    assert_not_minted(api)


@pytest.mark.parametrize('mode', ['check', 'apply'])
def test_per_inventory_rule_never_mints(api, mode):
    assert api.request(data={**api.payload, 'inventory': 'restricted', 'mode': mode}) == (
        403, {'error': 'inventory not allowed'})
    assert_not_minted(api)
    api.main.assert_called_once_with('main')


@pytest.mark.parametrize('change', [dict(mode='bogus'), dict(sha='not-a-sha'), dict(public_key='bad'),
                                    dict(limit='host\nsecret'), dict(inventory='unknown')])
def test_invalid_start_input_never_mints(api, change):
    assert api.request(data={**api.payload, **change})[0] == 400
    assert_not_minted(api)


@pytest.mark.parametrize('mode', ['check', 'apply'])
def test_command_disabled_never_mints(api, mode):
    api.svc.settings.allow_commands = ()
    assert api.request(data={**api.payload, 'mode': mode}) == (403, {'error': 'CLI runs disabled while applies are disabled: check/apply is client-chosen'})
    assert_not_minted(api)


def test_disable_apply_switch_never_mints(api):
    switches = api.tmp/'switches'
    switches.mkdir()
    (switches/'disable-apply').write_text('maintenance')
    api.svc.settings.switch_dir = str(switches)
    assert api.request()[0] == 403
    assert_not_minted(api)


def test_pr_lock_blocks_run_without_credentials(api):
    assert api.locks.try_acquire(new_lock('homelab', 1, api.sha, 'example-user')) is None
    assert api.request()[0] == 409
    assert api.bao.mock_calls == []
    assert api.locks.get('homelab').holder == 1
    with sqlite3.connect(api.locks.path) as db:
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0


def test_allowed_group_can_start_only_authorized_inventory(api):
    identity = api.token({'sub': 'group-member-example', 'groups': ['operators-example']})
    assert api.request(token=identity, data={**api.payload, 'inventory': 'restricted'})[0] == 403
    assert_not_minted(api)
    assert api.request(token=identity)[0] == 201
    assert api.locks.get('homelab').holder.endswith('(group-member-example; sub=group-member-example)')


def test_run_lock_blocks_pr_without_vcs_lookup(api):
    status, run = api.request()
    assert status == 201, run
    holder = api.locks.get('homelab').holder
    vcs = Mock()
    engine = Engine(config.Config(), api.tmp, Mock(), vcs=vcs, locks=api.locks)
    reason = engine._lock_conflict('homelab', 1, api.sha, 'example-user')
    # #57: the comment names the run, never the operator's name or identity subject.
    assert f"run:{run['run_id']}" in reason and 'operator run' in reason
    assert SUBJECT not in reason and 'sub=' not in reason
    assert vcs.mock_calls == []
    assert api.locks.get('homelab').holder == holder


@pytest.mark.parametrize('action', ['lease', 'finish'])
def test_other_subject_cannot_touch_run(api, action):
    status, run = api.request()
    assert status == 201
    api.bao.reset_mock()
    with sqlite3.connect(api.locks.path) as db:
        before = db.execute('SELECT data FROM runs').fetchone()[0]
    assert api.request(f"/api/runs/{run['run_id']}/{action}", data={'ok': True},
                       token=api.token({'sub': OTHER})) == (403, {'error': 'different subject'})
    with sqlite3.connect(api.locks.path) as db:
        assert db.execute('SELECT data FROM runs').fetchone()[0] == before
    assert api.locks.count() == 1
    assert api.bao.mock_calls == []
    assert api.svc.state.jobs()[0]['result'] == 'issued'


@pytest.mark.parametrize('action', ['lease', 'finish'])
@pytest.mark.parametrize('rid', ['0'*32, 'invalid'])
def test_unknown_run_never_mints(api, action, rid):
    assert api.request(f'/api/runs/{rid}/{action}', data={'ok': True}) == (404, {'error': 'unknown run'})
    assert_not_minted(api)


def test_lease_expiry_releases_lock_and_audits(api):
    status, run = api.request()
    assert status == 201
    api.now = run['lease_expires_at']
    api.svc.runs.expire()
    assert api.locks.get('homelab') is None
    assert api.request(f"/api/runs/{run['run_id']}/lease", data={})[0] == 404
    job, issued = SQLiteState(api.locks.path).jobs()
    assert issued['result'] == 'issued'
    assert job['run_id'] == run['run_id']
    assert job['subject'] == SUBJECT and job['user'] == SUBJECT
    assert job['result'] == 'error' and job['trigger'] == 'cli'
    assert job['certificate_serial'] == '42'
    assert job['inventory'] == 'homelab' and job['mode'] == 'apply'


def test_renewal_cannot_extend_past_actual_certificate_expiry(api):
    api.bao.sign_ssh_key.return_value = certificate(NOW+75)
    status, run = api.request()
    assert status == 201
    assert run['lease_expires_at'] == NOW+60
    api.now = NOW+50
    assert api.request(f"/api/runs/{run['run_id']}/lease", data={}) == (
        200, {'lease_expires_at': NOW+75})
    api.now = NOW+75
    api.svc.runs.expire()
    assert api.locks.count() == 0
    assert api.svc.state.jobs()[0]['result'] == 'error'


@pytest.mark.parametrize('ok,result', [(True, 'ok'), (False, 'failed')])
def test_finish_audits_metadata_not_credentials(api, ok, result):
    token = api.token()
    status, run = api.request(token=token)
    assert status == 201
    api.bao.revoke.assert_called_once_with('test-only-bao-token')
    assert api.request(f"/api/runs/{run['run_id']}/finish", data={'ok': ok, 'output': 'private-output'}) == (
        200, {'ok': True})
    assert api.locks.count() == 0
    job, issued = SQLiteState(api.locks.path).jobs()
    assert issued['result'] == 'issued'
    assert job['result'] == result and job['sha'] == api.sha
    assert job['limit'] == 'example-host' and job['trigger'] == 'cli'
    with closing(sqlite3.connect(api.locks.path)) as db:
        dump = '\n'.join(db.iterdump())
    for secret in (token, run['certificate'], 'test-only-bao-token', 'test-only-vault-password', 'private-output'):
        assert secret not in dump


def test_oversized_http_body_refused_before_identity_or_mint(api):
    api.svc.settings.max_body_bytes = 128
    api.svc.runs.verifier.fetch = Mock(side_effect=AssertionError('must not verify oversized request'))
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(api.svc))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        # Send headers only: rejection must not wait for the oversized body to arrive.
        connection.putrequest('POST', '/api/runs')
        connection.putheader('Content-Length', '129')
        connection.putheader('Authorization', 'Bearer '+api.token())
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 413
        response.read()
        api.svc.runs.verifier.fetch.assert_not_called()
        assert_not_minted(api)
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_blocked_start_does_not_starve_existing_lease(api):
    status, run = api.request()
    assert status == 201
    entered, release = threading.Event(), threading.Event()
    original = api.main.return_value
    def slow_config(*args):
        entered.set()
        assert release.wait(5)
        return original
    api.main.side_effect = slow_config
    result = []
    thread = threading.Thread(target=lambda: result.append(api.request()))
    thread.start()
    assert entered.wait(3)
    try:
        api.now += 1
        assert api.request(f"/api/runs/{run['run_id']}/lease", data={}) == (
            200, {'lease_expires_at': NOW+61})
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert result[0][0] == 409


def test_inflight_signing_keeps_lock_but_cannot_resurrect_elapsed_lease(api):
    entered, release = threading.Event(), threading.Event()
    def slow_sign(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return certificate(NOW+180)
    api.bao.sign_ssh_key.side_effect = slow_sign
    result = []
    thread = threading.Thread(target=lambda: result.append(api.request()))
    thread.start()
    assert entered.wait(3)
    try:
        api.now += 61
        api.svc.runs.expire()
        assert api.locks.count() == 1  # no release while upstream signing is in progress
    finally:
        release.set()
        thread.join(timeout=5)
    assert result == [(503, {'error': 'initial lease expired'})]
    assert api.locks.count() == 0
    with sqlite3.connect(api.locks.path) as db:
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0


def test_switch_flipped_during_config_fetch_never_mints(api):
    original = api.main.return_value
    def flip_switch(*args):
        api.svc.settings.allow_commands = ('plan',)
        return original
    api.main.side_effect = flip_switch
    assert api.request() == (403, {'error': 'CLI runs disabled while applies are disabled: check/apply is client-chosen'})
    assert_not_minted(api)


@pytest.mark.parametrize('via_file', [False, True])
def test_apply_off_refuses_check_without_minting(api, via_file):
    if via_file:
        switches = api.tmp/'freeze-switches'
        switches.mkdir(parents=True, exist_ok=True)
        api.svc.settings.switch_dir = str(switches)
        (switches/'disable-apply').write_text('freeze')
    else:
        api.svc.settings.allow_commands = ('plan',)
    status, body = api.request(data={**api.payload, 'mode': 'check'})
    assert status == 403 and 'client-chosen' in body['error']
    assert_not_minted(api)


@pytest.mark.parametrize('claim,value,expected', [
    ('name', 'Demo Operator', 'Demo Operator'),
    ('preferred_name', '  Demo\nOperator\x1b\u202e ', 'DemoOperator'),
    ('name', 'x'*200, 'x'*128),
    ('name', None, SUBJECT),
])
def test_display_name_is_bounded_sanitized_and_never_authorizes(api, claim, value, expected):
    api.svc.settings.run_name_claim = claim
    status, run = api.request(token=api.token({claim: value}))
    assert status == 201
    with sqlite3.connect(api.locks.path) as db:
        holder = db.execute('SELECT holder FROM locks').fetchone()[0]
    assert expected in holder and SUBJECT in holder
    assert api.request(f"/api/runs/{run['run_id']}/finish", data={'ok': True})[0] == 200
    job = api.svc.state.jobs()[0]
    assert job['display_name'] == expected and job['subject'] == SUBJECT
    assert api.request(token=api.token({'sub': 'not-authorized', claim: SUBJECT}))[0] == 403


@pytest.mark.parametrize('field', ['iat', 'nbf'])
@pytest.mark.parametrize('offset,accepted', [(30, True), (31, False)])
def test_identity_fixed_activation_leeway(api, field, offset, accepted):
    status, _ = api.request(token=api.token({field: NOW+offset}))
    assert status == (201 if accepted else 401)
    if not accepted:
        assert_not_minted(api)


def test_identity_expiration_has_no_leeway(api):
    assert api.request(token=api.token({'exp': NOW-1, 'iat': NOW+30}))[0] == 401
    assert_not_minted(api)


@pytest.mark.parametrize('action', ['finish', 'lease'])
def test_pending_run_cannot_be_finished_or_renewed(api, action):
    entered, release = threading.Event(), threading.Event()
    def sign(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return certificate(NOW+180)
    api.bao.sign_ssh_key.side_effect = sign
    results = []
    thread = threading.Thread(target=lambda: results.append(api.request()))
    thread.start()
    assert entered.wait(3)
    try:
        with sqlite3.connect(api.locks.path) as db:
            run = json.loads(db.execute('SELECT data FROM runs').fetchone()[0])
        assert run['state'] == 'pending'
        assert api.request()[0] == 409
        assert api.request(f"/api/runs/{run['run_id']}/{action}", data={'ok': True}) == (409, {'error': 'run pending'})
        assert api.locks.get('homelab').holder == run['holder']
    finally:
        release.set()
        thread.join(5)
    assert results[0][0] == 201
    assert api.locks.get('homelab').holder == run['holder']
    assert api.svc.state.jobs()[0]['result'] == 'issued'


@pytest.mark.parametrize('lost', ['run', 'lock'])
def test_lost_pending_state_fails_closed(api, lost):
    def sign(*args, **kwargs):
        with sqlite3.connect(api.locks.path) as db:
            db.execute('DELETE FROM '+('runs' if lost == 'run' else 'locks'))
        return certificate(NOW+180)
    api.bao.sign_ssh_key.side_effect = sign
    assert api.request() == (409, {'error': 'run or lock lost during start'})
    assert api.locks.count() == 0
    assert all(j['result'] == 'refused' for j in api.svc.state.jobs())


@pytest.mark.parametrize('playbook', ['/etc/passwd', '../site.yml', 'a/../site.yml', 'a\\site.yml', '', 'x'*513, 'site\n.yml'])
def test_invalid_playbook_never_mints(api, playbook):
    assert api.request(data={**api.payload, 'playbook': playbook})[0] == 400
    assert_not_minted(api)


@pytest.mark.parametrize('field', ['iat', 'nbf', 'exp'])
@pytest.mark.parametrize('value', [True, False, float('nan'), float('inf'), -float('inf'), '123'])
def test_numeric_dates_are_strict(api, field, value):
    assert api.request(token=api.token({field: value}))[0] == 401
    assert_not_minted(api)


def test_drift_holder_refuses_operator(api):
    from ferry.locks import Lock
    api.locks.try_acquire(Lock('homelab', 'drift', api.sha, 'schedule', NOW))
    assert api.request()[0] == 409
    assert api.bao.mock_calls == []


@pytest.mark.parametrize('during', ['login', 'sign'])
def test_switch_flip_at_credential_boundary_fails_closed(api, during):
    def flip(*args, **kwargs):
        api.svc.settings.allow_commands = ('plan',)
        return 'test-only-bao-token' if during == 'login' else certificate(NOW+180)
    getattr(api.bao, 'login' if during == 'login' else 'sign_ssh_key').side_effect = flip
    assert api.request()[0] == 403
    assert api.locks.count() == 0
    if during == 'login':
        api.bao.sign_ssh_key.assert_not_called()
    assert all(j['result'] == 'refused' for j in api.svc.state.jobs())
    with sqlite3.connect(api.locks.path) as db:
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0


def test_apply_requires_default_branch_head(api):
    payload = {**api.payload, 'sha': 'c'*40}
    status, body = api.request(data=payload)
    assert status == 409 and 'head' in body['error']
    assert_not_minted(api)


def test_check_may_name_any_published_sha(api):
    status, body = api.request(data={**api.payload, 'mode': 'check', 'sha': 'c'*40})
    assert status == 201 and body['certificate']


@pytest.mark.parametrize('where', ['global', 'inventory'])
def test_empty_group_never_matches(api, where):
    api.svc.settings.run_allowed_subjects = ('',)
    api.svc.settings.run_allowed_groups = ('',) if where == 'global' else ('operators-example',)
    token = api.token({'sub': 'stranger-example', 'groups': ['']})
    status, _ = api.request(token=token)
    assert status == 403
    assert_not_minted(api)


def test_cli_certificate_requests_short_ttl(api):
    assert api.request()[0] == 201
    assert api.bao.sign_ssh_key.call_args.kwargs['ttl'] == '15m'


def test_cli_certificate_longer_than_ttl_is_refused(api):
    api.svc.settings.run_cert_ttl = '2m'
    api.bao.sign_ssh_key.return_value = certificate(NOW + 600)
    status, body = api.request()
    assert status == 503 and 'certificate' not in body
    assert api.locks.count() == 0


@pytest.mark.parametrize('value,seconds', [('15m', 900), ('900', 900), ('90s', 90), ('1h', 3600)])
def test_duration_seconds(value, seconds):
    assert duration_seconds(value) == seconds


@pytest.mark.parametrize('value', ['', '0m', '-5m', '15d', 'soon'])
def test_duration_seconds_rejects(value):
    with pytest.raises(ValueError):
        duration_seconds(value)
def audit_lines(caplog, event):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(f'cli run {event}:')]


def test_every_run_is_audited_in_jobs_and_log(api, caplog):
    caplog.set_level('INFO', logger='ferry.runs')
    status, run = api.request(data={**api.payload, 'reason': 'restore\nservice\x1b[2J after outage'})
    assert status == 201
    issued = api.svc.state.jobs()[0]
    assert issued['result'] == 'issued' and issued['subject'] == SUBJECT and issued['inventory'] == 'homelab'
    assert issued['sha'] == api.sha and issued['started'] and issued['trigger'] == 'cli'
    assert issued['reason'] == 'restore service[2J after outage'
    [line] = audit_lines(caplog, 'issued')
    for want in (run['run_id'], f'subject="{SUBJECT}"', 'inventory="homelab"', f'sha="{api.sha}"',
                 'mode="apply"', 'serial="42"', 'reason="restore service[2J after outage"'):
        assert want in line
    assert '\n' not in line and '\x1b' not in line and 'test-only' not in line
    assert api.request(f"/api/runs/{run['run_id']}/finish", data={'ok': True, 'counts': {'hosts': 1}})[0] == 200
    done = api.svc.state.jobs()[0]
    assert done['result'] == 'ok' and done['subject'] == SUBJECT and done['reason'] == issued['reason']
    [line] = audit_lines(caplog, 'ok')
    assert f'sha="{api.sha}"' in line and 'inventory="homelab"' in line


def test_reason_is_optional_and_bounded(api):
    assert api.request(data={**api.payload, 'reason': 'x' * 1025})[0] == 400
    assert api.request(data={**api.payload, 'reason': 7})[0] == 400
    assert api.request()[0] == 201
    assert api.svc.state.jobs()[0]['reason'] is None


def test_refused_identity_is_audited(api, caplog):
    caplog.set_level('INFO', logger='ferry.runs')
    token = api.token({'sub': 'stranger-example', 'groups': ['operators-example']})
    assert api.request(token=token, data={**api.payload, 'inventory': 'restricted'})[0] == 403
    [job] = api.svc.state.jobs()
    assert job['result'] == 'refused' and job['subject'] == 'stranger-example'
    assert job['inventory'] == 'restricted' and job['sha'] == api.sha
    [line] = audit_lines(caplog, 'refused')
    assert 'subject="stranger-example"' in line and 'inventory not allowed' in line
    assert api.bao.mock_calls == []


def test_unverified_identity_leaves_no_job(api, caplog):
    caplog.set_level('INFO', logger='ferry.runs')
    assert api.request(token='not.a.token')[0] == 401
    assert api.svc.state.jobs() == []
    assert any('invalid identity token' in r.getMessage() for r in caplog.records)
