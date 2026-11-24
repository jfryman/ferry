"""Run the playbooks as a separate, unprivileged user that can't read the service's secrets.

A plan runs the change's own playbooks, and a playbook can run code on the controller: lookups,
`delegate_to: localhost`, `check_mode: false` tasks, plugins shipped in the repo. If that code runs as
the service, it can read everything the service can: the GitHub App key or GitLab token, the webhook
secret, the workload identity token for OpenBao, the lock file.

So the service doesn't run ansible itself. `ferry executor` is a second process, as a different user
and (in Kubernetes or Nomad) in a second container that mounts none of the service's secrets. It
listens on a Unix socket in a directory the two share. Per job the service sends:

  - the ansible-playbook arguments and the working directory (a checkout in the shared work dir,
    which the executor can read but not write);
  - the job's environment (only what ferry sets for the job, never the service's own environment);
  - the job's credential files' contents: the SSH key and certificate, the vault password.

The executor writes those files into a private per-job directory, runs ansible-playbook, streams the
output back, returns the events file and removes the directory. The job's SSH certificate and vault
password are readable by the playbook: that's inherent, the playbook needs them. Everything else
isn't there to read.

After each job, if the executor is its container's PID 1, it kills every other process in the
container: nothing a playbook started (a listener in place of the socket, a background reader)
outlives its job and sees the next job's credentials.

Protocol: one JSON line from the service, then JSON lines back: {"out": line} ... and a final
{"rc": int, "timed_out": bool, "events": base64}, or {"error": text}.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Iterator

from .results import RunResult, parse_file
from .runner import CREDS_DIR_ENV, AnsibleRunner, RunRequest, ferry_ansible_env

log = logging.getLogger("ferry.executor")

JOB = "@@FERRY_JOB_DIR@@"  # in args/env from the service: the executor's per-job directory
MAX_REQUEST = 4 * 1024 * 1024
MAX_FILE = 256 * 1024
CONNECT_TRIES = 20


class ExecutorError(RuntimeError):
    pass


# ---- the service's side ------------------------------------------------------------------------


class ExecutorRunner(AnsibleRunner):
    """An AnsibleRunner whose ansible-playbook runs in the executor (see the module docstring)."""

    def __init__(self, socket_path: str, repo_dir, busy_marker: str, **kw):
        super().__init__(repo_dir, busy_marker, **kw)
        self.socket_path = socket_path

    def request(self, req: RunRequest) -> dict:
        env = self.job_env(req)
        files: dict[str, str] = {}
        creds = self.env.get(CREDS_DIR_ENV)
        if creds:
            for f in sorted(Path(creds).iterdir()):
                if f.is_file() and not f.is_symlink():
                    files[f"creds/{f.name}"] = base64.b64encode(f.read_bytes()).decode()
            env = {k: v.replace(creds, f"{JOB}/creds") if isinstance(v, str) else v for k, v in env.items()}
        args = self.args(req)
        if req.extra_vars_file:
            files["extra-vars.json"] = base64.b64encode(Path(req.extra_vars_file).read_bytes()).decode()
            args = [f"@{JOB}/extra-vars.json" if a == f"@{req.extra_vars_file}" else a for a in args]
        return {"v": 1, "args": args, "cwd": str(self.repo_dir), "env": env, "files": files,
                "timeout": self.timeout}

    def run(self, req: RunRequest) -> RunResult:
        msg = self.request(req)
        tail: list[str] = []
        final: dict = {}

        def lines(conn) -> Iterator[str]:
            for raw in conn.makefile("r", encoding="utf-8", newline="\n"):
                m = json.loads(raw)
                if "out" in m:
                    yield m["out"]
                else:
                    final.update(m)
                    return

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(None)
            for attempt in range(CONNECT_TRIES):  # the executor's container may still be starting
                try:
                    conn.connect(self.socket_path)
                    break
                except (FileNotFoundError, ConnectionRefusedError) as e:
                    if attempt == CONNECT_TRIES - 1:
                        raise ExecutorError(f"can't reach the executor at {self.socket_path}: {e}") from e
                    time.sleep(0.5)
                except OSError as e:
                    raise ExecutorError(f"can't reach the executor at {self.socket_path}: {e}") from e
            conn.sendall(json.dumps(msg).encode() + b"\n")
            self._collect(lines(conn), tail)
        if "error" in final or "rc" not in final:
            raise ExecutorError(f"executor: {final.get('error', 'connection closed mid-job')}")
        if final.get("timed_out"):
            tail.append(f"\nferry: killed after {self.timeout:.0f}s (job timeout)\n")
        with tempfile.TemporaryDirectory(prefix="ferry-") as tmp:
            events = Path(tmp) / "events.jsonl"
            events.write_bytes(base64.b64decode(final.get("events") or ""))
            return parse_file(events, self.busy_marker, rc=int(final["rc"]), log_tail="".join(tail))


# ---- the executor ------------------------------------------------------------------------------


def _subst(value: str, job: Path) -> str:
    return value.replace(JOB, str(job))


def _job_file(job: Path, name: str) -> Path:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or len(p.parts) not in (1, 2):
        raise ExecutorError(f"bad file name {name!r}")
    return job / p


class Executor:
    def __init__(self, socket_path: str, roots: list[str], ansible_playbook: str = "ansible-playbook",
                 reap_all: bool | None = None):
        self.socket_path = socket_path
        self.roots = [Path(r).resolve() for r in roots]
        self.bin = ansible_playbook
        # Kill everything else after each job: only safe (and only complete) as the container's PID 1.
        self.reap_all = os.getpid() == 1 if reap_all is None else reap_all
        self._sock: socket.socket | None = None
        self._ino = None

    # Startup ---------------------------------------------------------------------------------------

    @staticmethod
    def check_separation(must_not_read: list[str]) -> list[str]:
        """Problems that mean the separation isn't there: running as root, or a service secret readable."""
        problems = []
        if os.geteuid() == 0:
            problems.append("running as root")
        for p in must_not_read:
            if p and os.access(p, os.R_OK):
                problems.append(f"can read {p}")
        return problems

    def bind(self) -> None:
        if self._sock:
            self._sock.close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.socket_path)
        # The directory decides who can connect (only the service's container shares it). A client
        # that connects can only run ansible as this same unprivileged user.
        os.chmod(self.socket_path, 0o666)
        s.listen(4)
        self._sock, self._ino = s, os.stat(self.socket_path).st_ino

    # Jobs ------------------------------------------------------------------------------------------

    def serve_forever(self, stop: threading.Event | None = None) -> None:
        self.bind()
        log.info("executor listening on %s (reap_all=%s)", self.socket_path, self.reap_all)
        assert self._sock is not None
        self._sock.settimeout(1.0)
        while not (stop and stop.is_set()):
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            with conn:
                conn.settimeout(None)
                self.handle(conn)
            self.after_job()

    def after_job(self) -> None:
        if self.reap_all:
            try:
                os.kill(-1, signal.SIGKILL)  # as PID 1: every other process in this container
            except ProcessLookupError:
                pass
            while True:
                try:
                    if os.waitpid(-1, os.WNOHANG) == (0, 0):
                        break
                except ChildProcessError:
                    break
        try:
            same = os.stat(self.socket_path).st_ino == self._ino
        except FileNotFoundError:
            same = False
        if not same:
            log.warning("the socket at %s was replaced during a job; binding it again", self.socket_path)
            self.bind()
            assert self._sock is not None
            self._sock.settimeout(1.0)

    def _read_request(self, conn: socket.socket) -> dict:
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                raise ExecutorError("no request")
            buf += chunk
            if len(buf) > MAX_REQUEST:
                raise ExecutorError("request too large")
        return json.loads(buf)

    def _check(self, msg: dict) -> tuple[list[str], Path, dict[str, str], dict[str, bytes], float | None]:
        args, cwd, env, files = msg.get("args"), msg.get("cwd"), msg.get("env") or {}, msg.get("files") or {}
        if msg.get("v") != 1 or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ExecutorError("bad request")
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ExecutorError("bad env")
        where = Path(str(cwd)).resolve()
        if not where.is_dir() or not any(where == r or r in where.parents for r in self.roots):
            raise ExecutorError(f"cwd {cwd!r} is not a directory under {[str(r) for r in self.roots]}")
        blobs = {}
        for name, b64 in files.items():
            data = base64.b64decode(b64)
            if len(data) > MAX_FILE:
                raise ExecutorError(f"file {name!r} too large")
            blobs[name] = data
        timeout = msg.get("timeout")
        return args, where, env, blobs, float(timeout) if timeout else None

    def handle(self, conn: socket.socket) -> None:
        def send(obj) -> bool:
            try:
                conn.sendall(json.dumps(obj).encode() + b"\n")
                return True
            except OSError:
                return False

        try:
            args, cwd, req_env, files, timeout = self._check(self._read_request(conn))
        except (ExecutorError, ValueError) as e:
            send({"error": str(e)})
            return
        job = Path(tempfile.mkdtemp(prefix="ferry-job-"))
        try:
            os.chmod(job, 0o700)
            for name, data in files.items():
                dest = _job_file(job, name)
                dest.parent.mkdir(mode=0o700, exist_ok=True)
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
            events = job / "events.jsonl"
            env = dict(os.environ)  # the executor's own environment holds no secrets
            env.update(ferry_ansible_env(env, str(events)))
            env.update({k: _subst(v, job) for k, v in req_env.items()})
            rc, timed_out = self._run([self.bin, *[_subst(a, job) for a in args]], cwd, env, timeout, send)
            data = events.read_bytes() if events.exists() else b""
            send({"rc": rc, "timed_out": timed_out, "events": base64.b64encode(data).decode()})
        except ExecutorError as e:
            send({"error": str(e)})
        except Exception as e:  # report, keep serving
            log.exception("job failed")
            send({"error": f"{type(e).__name__}: {e}"})
        finally:
            shutil.rmtree(job, ignore_errors=True)

    def _run(self, cmd, cwd, env, timeout, send) -> tuple[int, bool]:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, start_new_session=True)
        timed_out = threading.Event()

        def kill():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        def on_timeout():
            timed_out.set()
            kill()

        timer = threading.Timer(timeout, on_timeout) if timeout else None
        if timer:
            timer.daemon = True
            timer.start()
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if not send({"out": line}):  # the service went away: don't run on unobserved
                    kill()
                    break
            rc = proc.wait()
        finally:
            if timer:
                timer.cancel()
            kill()  # anything the run left in its process group
        if timed_out.is_set():
            rc = rc if rc else -signal.SIGKILL
        return rc, timed_out.is_set()
