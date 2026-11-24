"""ferry executor: playbooks run in another process (another user/container in production) that
gets only the job's own credentials, never the service's environment or files.

The executor runs here as a subprocess with a clean environment, the way it runs in its own
container. The lab (deploy/local/lab.sh evil) repeats the hostile-playbook check with real ansible,
separate users and separate containers.
"""

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from ferry.executor import JOB, Executor, ExecutorError, ExecutorRunner
from ferry.runner import RunRequest

SRC = str(Path(__file__).resolve().parents[1] / "src")
SERVICE_SECRETS = {"FERRY_WEBHOOK_SECRET": "whsec-service-only", "FERRY_GITLAB_TOKEN": "glpat-service-only",
                   "FERRY_GITHUB_APP_KEY_FILE": "/secrets/app.pem"}


def fake_playbook(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return str(path)


# A hostile playbook: dumps its environment, reads every file the job points it at, and tries the
# service's secret file. It also leaves a process behind and replaces the executor's socket.
EVIL = """
    import json, os, subprocess, sys
    args = sys.argv[1:]
    out = {"env": dict(os.environ), "argv": args, "files": {}}
    for k, v in os.environ.items():
        if os.path.isfile(v):
            out["files"][k] = open(v).read()
    try:
        out["service_secret"] = open(os.environ["LEAK_TRY"]).read()
    except Exception as e:
        out["service_secret"] = "unreadable: " + type(e).__name__
    if "--check" in args:
        with open(os.environ["FERRY_EVENTS_FILE"], "w") as fh:
            fh.write("")
    print("EVIL " + json.dumps(out), flush=True)
    if os.environ.get("LINGER"):
        subprocess.Popen(["sleep", "300"], start_new_session=True)
        sock = os.environ["SOCK"]
        os.unlink(sock)
        open(sock, "w").write("squatter")
"""


@pytest.fixture
def executor(tmp_path):
    """`ferry executor` in a subprocess with only a minimal environment, like its own container."""
    work = tmp_path / "work"
    (work / "pr-1").mkdir(parents=True)
    run = tmp_path / "run"
    run.mkdir()
    sock = str(run / "executor.sock")
    pb = fake_playbook(tmp_path / "ansible-playbook", EVIL)
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": SRC, "HOME": str(tmp_path), "FERRY_EXECUTOR_SOCKET": sock,
           "FERRY_WORKDIR": str(work), "FERRY_EXECUTOR_ANSIBLE_PLAYBOOK": pb, "SOCK": sock}
    proc = subprocess.Popen([sys.executable, "-m", "ferry.cli", "executor"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    yield sock, work / "pr-1", proc
    proc.terminate()
    proc.wait(timeout=10)


def job_creds(tmp_path) -> dict:
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "id_ed25519").write_text("PRIVATE KEY\n")
    (creds / "id_ed25519-cert.pub").write_text("CERT\n")
    (creds / "vault-password").write_text("vault-pass\n")
    return {"FERRY_CREDS_DIR": str(creds), "ANSIBLE_PRIVATE_KEY_FILE": str(creds / "id_ed25519"),
            "ANSIBLE_VAULT_PASSWORD_FILE": str(creds / "vault-password"),
            "ANSIBLE_SSH_CONTROL_PATH_DIR": str(creds / "cp"), "ANSIBLE_REMOTE_USER": "root"}


def evil_output(capsys) -> dict:
    [line] = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("EVIL ")]
    return json.loads(line[5:])


def test_request_carries_only_the_job(tmp_path, monkeypatch):
    for k, v in SERVICE_SECRETS.items():
        monkeypatch.setenv(k, v)
    creds = job_creds(tmp_path)
    vars_file = tmp_path / "vars.json"
    vars_file.write_text("{}")
    r = ExecutorRunner("/nonexistent.sock", tmp_path, "BUSY", env=creds)
    msg = r.request(RunRequest("site.yml", "inv", extra_vars_file=str(vars_file)))
    assert not set(SERVICE_SECRETS) & set(msg["env"]) and "FERRY_CREDS_DIR" not in msg["env"]
    assert msg["env"]["ANSIBLE_PRIVATE_KEY_FILE"] == f"{JOB}/creds/id_ed25519"
    assert msg["env"]["ANSIBLE_SSH_CONTROL_PATH_DIR"] == f"{JOB}/creds/cp"
    assert sorted(msg["files"]) == ["creds/id_ed25519", "creds/id_ed25519-cert.pub", "creds/vault-password",
                                    "extra-vars.json"]
    assert f"@{JOB}/extra-vars.json" in msg["args"] and str(tmp_path) not in json.dumps(msg["env"])


def test_hostile_playbook_gets_only_the_jobs_credentials(executor, tmp_path, monkeypatch, capsys):
    sock, checkout, _ = executor
    for k, v in SERVICE_SECRETS.items():
        monkeypatch.setenv(k, v)
    creds = job_creds(tmp_path)
    r = ExecutorRunner(sock, checkout, "BUSY", env={**creds, "LEAK_TRY": "/nonexistent/app.pem"})  # the lab tries the real path, as another user
    result = r.run(RunRequest("site.yml", "inv"))
    assert result.rc == 0
    out = evil_output(capsys)
    # Nothing of the service's environment reached the playbook.
    for k, v in SERVICE_SECRETS.items():
        assert k not in out["env"] and v not in json.dumps(out)
    assert not {"VAULT_TOKEN", "FERRY_CREDS_DIR"} & set(out["env"])
    # The job's own credentials did (inherent), from the executor's per-job dir, not the service's.
    key = out["env"]["ANSIBLE_PRIVATE_KEY_FILE"]
    assert out["files"]["ANSIBLE_PRIVATE_KEY_FILE"] == "PRIVATE KEY\n" and not key.startswith(str(tmp_path / "creds"))
    assert out["files"]["ANSIBLE_VAULT_PASSWORD_FILE"] == "vault-pass\n"
    assert out["service_secret"].startswith("unreadable")
    assert not os.path.exists(os.path.dirname(key))  # the job dir is gone


def test_leftovers_and_a_replaced_socket_dont_survive_the_job(tmp_path):
    """As PID 1 the executor kills everything else after a job; here (not PID 1) at least the
    replaced socket is bound again, so the next job doesn't talk to a squatter."""
    work = tmp_path / "work"
    work.mkdir()
    sock = str(tmp_path / "x.sock")
    ex = Executor(sock, [str(work)], reap_all=False)
    ex.bind()
    os.unlink(sock)
    Path(sock).write_text("squatter")
    ex.after_job()
    s = socket.socket(socket.AF_UNIX)
    s.connect(sock)  # ours again
    s.close()


@pytest.mark.parametrize("msg, why", [
    ({"v": 1, "args": ["x"], "cwd": "/"}, "not a directory under"),
    ({"v": 1, "args": "rm -rf", "cwd": "WORK"}, "bad request"),
    ({"v": 1, "args": [], "cwd": "WORK", "env": {"A": 1}}, "bad env"),
])
def test_bad_requests_are_refused(tmp_path, msg, why):
    work = tmp_path / "work"
    work.mkdir()
    ex = Executor(str(tmp_path / "s"), [str(work)], reap_all=False)
    msg = {**msg, "cwd": str(work) if msg["cwd"] == "WORK" else msg["cwd"]}
    with pytest.raises(ExecutorError, match=why):
        ex._check(msg)


@pytest.mark.parametrize("name", ["/etc/passwd", "../x", "creds/../../x", "a/b/c"])
def test_file_names_stay_in_the_job_dir(tmp_path, name):
    from ferry.executor import _job_file
    with pytest.raises(ExecutorError):
        _job_file(tmp_path, name)


def test_separation_check(tmp_path):
    readable = tmp_path / "token"
    readable.write_text("t")
    problems = Executor.check_separation([str(readable), str(tmp_path / "absent"), ""])
    assert problems == [f"can read {readable}"] or problems[-1] == f"can read {readable}"


def test_executor_refuses_to_start_when_it_can_read_a_secret(tmp_path):
    readable = tmp_path / "token"
    readable.write_text("t")
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": SRC, "FERRY_EXECUTOR_SOCKET": str(tmp_path / "s"),
           "FERRY_EXECUTOR_MUST_NOT_READ": f"/nonexistent:{readable}"}
    proc = subprocess.run([sys.executable, "-m", "ferry.cli", "executor"], env=env, capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode != 0 and f"can read {readable}" in proc.stderr


def test_timeout_is_enforced_by_the_executor(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    pb = fake_playbook(tmp_path / "slow", """
        import subprocess, time
        subprocess.Popen(["sleep", "60"])
        print("started", flush=True)
        time.sleep(60)
    """)
    sock = str(tmp_path / "s")
    ex = Executor(sock, [str(work)], ansible_playbook=pb, reap_all=False)
    import threading
    stop = threading.Event()
    t = threading.Thread(target=ex.serve_forever, args=(stop,), daemon=True)
    t.start()
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    try:
        start = time.monotonic()
        result = ExecutorRunner(sock, work, "BUSY", timeout=1).run(RunRequest("site.yml", "inv"))
        assert time.monotonic() - start < 20
        assert not result.clean and "job timeout" in result.log_tail
    finally:
        stop.set()
        t.join(timeout=5)


def test_unreachable_executor_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr("ferry.executor.CONNECT_TRIES", 1)
    with pytest.raises(ExecutorError, match="can't reach the executor"):
        ExecutorRunner(str(tmp_path / "none.sock"), tmp_path, "BUSY").run(RunRequest("site.yml", "inv"))


def test_serve_uses_the_executor_when_configured(monkeypatch, tmp_path):
    from ferry import cli
    monkeypatch.setenv("FERRY_EXECUTOR_SOCKET", "/run/ferry/executor.sock")
    factory = cli._runner_factory()["runner_factory"]
    runner = factory(tmp_path, "BUSY", {})
    assert isinstance(runner, ExecutorRunner) and runner.socket_path == "/run/ferry/executor.sock"
    monkeypatch.delenv("FERRY_EXECUTOR_SOCKET")
    assert cli._runner_factory() == {}
