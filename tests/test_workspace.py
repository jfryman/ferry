import subprocess
from pathlib import Path

import pytest

from ferry.workspace import CheckoutError, Workspace, fetch_commit


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    """A bare repo with main and refs/pull/1/head, like GitHub's."""
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    git(src, "config", "user.email", "t@t")
    git(src, "config", "user.name", "t")
    (src / "ferry.yaml").write_text("inventories: [{name: lab, path: inv}]\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "main")
    main_sha = git(src, "rev-parse", "HEAD")
    git(src, "checkout", "-qb", "feature")
    (src / "site.yml").write_text("- hosts: all\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "pr")
    pr_sha = git(src, "rev-parse", "HEAD")
    bare = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(bare))
    git(bare, "update-ref", "refs/pull/1/head", pr_sha)
    return f"file://{bare}", main_sha, pr_sha


def test_main_and_pr_checkouts(remote, tmp_path):
    url, main_sha, pr_sha = remote
    ws = Workspace(tmp_path / "work", url)
    main_dir, sha = ws.main("main")
    assert sha == main_sha and (main_dir / "ferry.yaml").exists() and not (main_dir / "site.yml").exists()
    pr_dir = ws.pull_request(1, pr_sha)
    assert git(pr_dir, "rev-parse", "HEAD") == pr_sha and (pr_dir / "site.yml").exists()
    ws.discard(pr_dir)
    assert not pr_dir.exists()


def test_pr_that_moved_on_is_refused(remote, tmp_path):
    url, main_sha, _ = remote
    with pytest.raises(CheckoutError, match="moved on"):
        Workspace(tmp_path / "work", url).pull_request(1, main_sha)


def test_token_never_lands_in_git_config(remote, tmp_path):
    url, _, pr_sha = remote
    dest = tmp_path / "co"
    fetch_commit(url, "refs/pull/1/head", pr_sha, dest, token="ghs_SECRET")
    for f in (dest / ".git").rglob("*"):
        if f.is_file():
            assert b"ghs_SECRET" not in f.read_bytes(), f


def test_git_errors_hide_the_token(tmp_path):
    with pytest.raises(CheckoutError) as e:
        fetch_commit(f"file://{tmp_path}/missing.git", "refs/heads/main", None, tmp_path / "co", token="ghs_SECRET")
    assert "ghs_SECRET" not in str(e.value)


def test_collections_without_requirements(tmp_path):
    ws = Workspace(tmp_path / "work", "file:///nowhere")
    assert ws.collections(tmp_path) == {}


def test_collections_are_installed_once_per_requirements_hash(tmp_path):
    calls = []
    galaxy = tmp_path / "fake-galaxy"
    galaxy.write_text(f"#!/bin/sh\necho \"$@\" >> {tmp_path}/galaxy.log\n")
    galaxy.chmod(0o755)
    co = tmp_path / "co"
    co.mkdir()
    (co / "requirements.yml").write_text("collections: [community.general]\n")
    ws = Workspace(tmp_path / "work", "file:///nowhere")
    env1 = ws.collections(co, galaxy=str(galaxy))
    env2 = ws.collections(co, galaxy=str(galaxy))
    assert env1 == env2 and env1["ANSIBLE_COLLECTIONS_PATH"].startswith(str(tmp_path / "work" / "collections"))
    assert len((tmp_path / "galaxy.log").read_text().splitlines()) == 1
