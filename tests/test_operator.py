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
