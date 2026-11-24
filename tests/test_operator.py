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
