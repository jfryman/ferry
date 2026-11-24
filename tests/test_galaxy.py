"""#26: ansible-galaxy runs on the PR's requirements.yml, so it gets the executor's side of the split:
no service environment, a hard timeout, an optional pinned server, a pruned cache."""

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from ferry import galaxy
from ferry.workspace import Workspace, WorkspaceTimeout, fetch_commit

SRC = str(Path(__file__).resolve().parents[1] / "src")
SECRETS = {"FERRY_WEBHOOK_SECRET": "whsec-service-only", "FERRY_GITLAB_TOKEN": "glpat-service-only",
           "FERRY_GITHUB_APP_KEY_FILE": "/secrets/app.pem", "VAULT_TOKEN": "hvs.serviceonlytoken1234567890"}


def fake_galaxy(tmp_path: Path, body: str = "") -> Path:
    """Records its argv and environment, then runs `body` (shell)."""
    g = tmp_path / "fake-galaxy"
    g.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys, time
        with open({str(tmp_path / 'galaxy.log')!r}, "a") as fh:
            fh.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")
        """) + textwrap.dedent(body))
    g.chmod(0o755)
    return g


def calls(tmp_path):
    log = tmp_path / "galaxy.log"
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def checkout(tmp_path):
    co = tmp_path / "co"
    co.mkdir()
    (co / "requirements.yml").write_text("collections:\n  - name: community.general\n    version: '9.0.0'\n")
    return co


def test_galaxy_gets_none_of_the_services_environment(tmp_path, checkout, monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    g = fake_galaxy(tmp_path)
    galaxy.install(checkout / "requirements.yml", tmp_path / "cache", galaxy=str(g))
    [call] = calls(tmp_path)
    assert not any(k.startswith("FERRY_") for k in call["env"])
    assert not set(SECRETS.values()) & set(call["env"].values())
    assert call["env"]["HOME"] != os.environ.get("HOME")


def test_workspace_collections_use_the_same_minimal_env(tmp_path, checkout, monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    g = fake_galaxy(tmp_path)
    env = Workspace(tmp_path / "work", "file:///nowhere").collections(checkout, galaxy=str(g))
    assert env["ANSIBLE_COLLECTIONS_PATH"].startswith(str(tmp_path / "work" / "collections"))
    assert not any(k.startswith("FERRY_") for k in calls(tmp_path)[0]["env"])


def test_a_galaxy_that_never_answers_is_killed_at_the_timeout(tmp_path, checkout):
    g = fake_galaxy(tmp_path, """
        import subprocess
        subprocess.Popen(["sleep", "300"])  # a child too: the whole group goes
        time.sleep(300)
        """)
    t = time.monotonic()
    with pytest.raises(galaxy.GalaxyError, match="timed out after 1s"):
        galaxy.install(checkout / "requirements.yml", tmp_path / "cache", galaxy=str(g), timeout=1)
    assert time.monotonic() - t < 15
    assert not (tmp_path / "cache").exists() or not any((tmp_path / "cache").glob("*/.installed"))


def test_a_failed_install_is_not_cached(tmp_path, checkout):
    g = fake_galaxy(tmp_path, "sys.exit(3)\n")
    for _ in range(2):
        with pytest.raises(galaxy.GalaxyError):
            galaxy.install(checkout / "requirements.yml", tmp_path / "cache", galaxy=str(g))
    assert len(calls(tmp_path)) == 2


def test_installed_once_per_requirements_digest(tmp_path, checkout):
    g = fake_galaxy(tmp_path)
    a = galaxy.install(checkout / "requirements.yml", tmp_path / "cache", galaxy=str(g))
    b = galaxy.install(checkout / "requirements.yml", tmp_path / "cache", galaxy=str(g))
    assert a == b and len(calls(tmp_path)) == 1


def test_a_pinned_server_is_passed_and_other_sources_are_refused(tmp_path, checkout):
    g = fake_galaxy(tmp_path)
    galaxy.install(checkout / "requirements.yml", tmp_path / "cache", galaxy=str(g),
                   server="https://galaxy.example.org")
    [call] = calls(tmp_path)
    assert call["argv"][call["argv"].index("--server") + 1] == "https://galaxy.example.org"
    for bad in ["collections:\n  - name: https://evil.example/x.tar.gz\n",
                "collections:\n  - name: git+https://evil.example/x.git\n",
                "collections:\n  - name: ./local.tar.gz\n",
                "collections:\n  - name: a.b\n    source: https://evil.example\n",
                "collections:\n  - name: a.b\n    type: git\n",
                "collections: [a.b]\nroles: [{src: 'https://evil.example/r.tgz'}]\n",
                "collections: not-a-list\n",
                ": not yaml : [\n"]:
        (checkout / "requirements.yml").write_text(bad)
        with pytest.raises(galaxy.GalaxyError, match="pinned|requirements"):
            galaxy.install(checkout / "requirements.yml", tmp_path / "cache", galaxy=str(g),
                           server="https://galaxy.example.org")
    assert len(calls(tmp_path)) == 1


def test_old_cache_entries_are_pruned_and_the_one_in_use_kept(tmp_path, checkout):
    g = fake_galaxy(tmp_path)
    cache = tmp_path / "cache"
    for d in (cache / "old", cache / "fresh"):
        d.mkdir(parents=True)
        (d / ".installed").touch()
    long_ago = time.time() - 30 * 86400
    os.utime(cache / "old", (long_ago, long_ago))
    used = galaxy.install(checkout / "requirements.yml", cache, galaxy=str(g), max_age_days=14)
    os.utime(used, (long_ago, long_ago))  # in use by this job: kept even though it looks old
    galaxy.prune(cache, 14, keep={used})
    assert not (cache / "old").exists() and (cache / "fresh").exists() and used.exists()
    # Using an entry again refreshes it.
    galaxy.install(checkout / "requirements.yml", cache, galaxy=str(g))
    assert used.stat().st_mtime > time.time() - 60


def test_git_fetch_has_a_timeout(tmp_path, monkeypatch):
    import ferry.workspace as ws_mod
    seen = {}
    real = subprocess.Popen

    def slow_run(cmd, **kw):
        seen["timeout"] = kw.get("timeout")
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
    monkeypatch.setattr(ws_mod.subprocess, "run", slow_run)
    with pytest.raises(WorkspaceTimeout, match="timed out"):
        fetch_commit("file:///nowhere", "refs/heads/main", None, tmp_path / "co", timeout=7)
    assert seen["timeout"] == 7


# ---- in the executor ---------------------------------------------------------------------------------

@pytest.fixture
def executor(tmp_path):
    work = tmp_path / "work"
    (work / "pr-1").mkdir(parents=True)
    (work / "pr-1" / "requirements.yml").write_text("collections: [community.general]\n")
    run = tmp_path / "run"
    run.mkdir()
    sock = str(run / "executor.sock")
    g = fake_galaxy(tmp_path)
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": SRC, "HOME": str(tmp_path), "SOCK": sock,
           "WORK": str(work), "GALAXY": str(g), "COLL": str(tmp_path / "executor-collections")}
    code = ("import os; from ferry.executor import Executor; "
            "Executor(os.environ['SOCK'], [os.environ['WORK']], galaxy=os.environ['GALAXY'], "
            "collections_root=os.environ['COLL']).serve_forever()")
    proc = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    yield sock, work / "pr-1"
    proc.terminate()
    proc.wait(timeout=10)


def test_the_executor_installs_collections_into_its_own_dir(executor, tmp_path, monkeypatch):
    from ferry.executor import install_collections
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)  # the service's env: must not reach galaxy
    sock, co = executor
    path = install_collections(sock, co, timeout=30)
    assert path.startswith(str(tmp_path / "executor-collections"))
    [call] = calls(tmp_path)
    assert not any(k.startswith("FERRY_") for k in call["env"])
    assert not set(SECRETS.values()) & set(call["env"].values())


def test_the_executor_reports_a_galaxy_failure(executor, tmp_path):
    from ferry.executor import ExecutorError, install_collections
    sock, co = executor
    (co / "requirements.yml").write_text("collections:\n  - name: git+https://evil.example/x.git\n")
    with pytest.raises(ExecutorError, match="pinned"):
        install_collections(sock, co, timeout=30, server="https://galaxy.example.org")


def test_the_executor_refuses_a_checkout_outside_its_roots(executor, tmp_path):
    from ferry.executor import ExecutorError, install_collections
    sock, _ = executor
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "requirements.yml").write_text("collections: [a.b]\n")
    with pytest.raises(ExecutorError, match="not a directory under"):
        install_collections(sock, elsewhere, timeout=30)
