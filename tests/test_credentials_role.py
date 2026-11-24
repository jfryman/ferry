"""OpenBao.sign_ssh_key role/principal selection (#69)."""
from types import SimpleNamespace

from ferry.credentials import OpenBao


def bao(calls):
    b = OpenBao.__new__(OpenBao)
    b.s = SimpleNamespace(ssh_mount='ssh', ssh_role='service-role', ssh_ttl='20m', ssh_principal='ferry')
    b._call = lambda method, path, token=None, data=None: calls.append((path, data)) or {'data': {'signed_key': 'cert'}}
    return b


def test_service_role_by_default():
    calls = []
    assert bao(calls).sign_ssh_key('t', 'pub') == 'cert'
    assert calls == [('ssh/sign/service-role', {'public_key': 'pub', 'cert_type': 'user', 'ttl': '20m',
                                                 'valid_principals': 'ferry'})]


def test_cli_role_and_principal():
    calls = []
    bao(calls).sign_ssh_key('t', 'pub', ttl='15m', role='cli-role', principal='ferry-operator')
    assert calls == [('ssh/sign/cli-role', {'public_key': 'pub', 'cert_type': 'user', 'ttl': '15m',
                                             'valid_principals': 'ferry-operator'})]
