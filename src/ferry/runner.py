"""Run ansible-playbook and collect ferry_events results.

The Runner protocol is the seam for tests (FakeRunner) and for the executor (executor.py), which
runs ansible-playbook as a separate, unprivileged user that can't read the service's secrets.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol

from .results import RunResult, parse_file

PLUGIN_DIR = Path(__file__).parent / "ansible" / "callback_plugins"
PLAYBOOK_DIR = Path(__file__).parent / "ansible" / "playbooks"

# Set by job_credentials: the per-job credentials directory. For the runner only, never ansible's.
CREDS_DIR_ENV = "FERRY_CREDS_DIR"
# The service's own secrets, if given as environment variables: never inherited by ansible.
SERVICE_SECRET_ENV = ("FERRY_WEBHOOK_SECRET", "FERRY_GITLAB_TOKEN", "FERRY_GITHUB_TOKEN")


@dataclass
class RunRequest:
    playbook: str
    inventory: str
    limit: str | None = None
    check: bool = True
    diff: bool = True
    extra_vars_file: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)


class Runner(Protocol):
    def run(self, req: RunRequest) -> RunResult: ...


def ferry_ansible_env(base: dict[str, str], events_file: str) -> dict[str, str]:
    """The settings ferry needs in every run: its events callback, no colour, no retry files."""
    enabled = [c for c in base.get("ANSIBLE_CALLBACKS_ENABLED", "").split(",") if c]
    return {
        "FERRY_EVENTS_FILE": events_file,
        "ANSIBLE_CALLBACK_PLUGINS": os.pathsep.join(
            p for p in [str(PLUGIN_DIR), base.get("ANSIBLE_CALLBACK_PLUGINS", "")] if p),
        "ANSIBLE_CALLBACKS_ENABLED": ",".join(enabled + ["ferry_events"]),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
    }


class AnsibleRunner:
    """Runs ansible-playbook as a child of this process: same user, same files. Fine for the CI
    driver and for a service without secrets worth separating; the service otherwise uses
    executor.ExecutorRunner."""

    def __init__(self, repo_dir: str | Path, busy_marker: str, ansible_playbook: str = "ansible-playbook",
                 env: dict[str, str] | None = None, timeout: float | None = None,
                 echo: "Callable[[str], str | None] | None" = None):
        self.repo_dir = Path(repo_dir)
        self.busy_marker = busy_marker
        self.bin = ansible_playbook
        # Per-job environment (the service's SSH certificate, OpenBao token, vault password file).
        self.env = dict(env or {})
        # Kill the run after this many seconds (None: no limit). In the service a hung host would
        # otherwise block the single worker, and a run past the certificate's TTL fails halfway.
        self.timeout = timeout
        # Applied to each output line before it's echoed (the service passes its redactor: there the
        # echo goes to the service log, not a CI job log).
        self.echo = echo

    def args(self, req: RunRequest) -> list[str]:
        cmd = ["-i", req.inventory, req.playbook]
        if req.limit:
            cmd += ["--limit", req.limit]
        if req.check:
            cmd.append("--check")
        if req.diff:
            cmd.append("--diff")
        if req.extra_vars_file:
            cmd += ["-e", f"@{req.extra_vars_file}"]
        return cmd

    def command(self, req: RunRequest) -> list[str]:
        return [self.bin, *self.args(req)]

    def job_env(self, req: RunRequest) -> dict[str, str]:
        """The job's own variables: credentials and request extras, without the runner-only ones."""
        env = {**self.env, **req.extra_env}
        env.pop(CREDS_DIR_ENV, None)
        return env

    def _collect(self, lines: Iterable[str], tail: list[str]) -> None:
        for line in lines:
            shown = self.echo(line) if self.echo else line
            print(shown or "", end="", flush=True)  # the human log stays in the CI job output
            tail.append(line)
            if len(tail) > 40:
                tail.pop(0)

    def run(self, req: RunRequest) -> RunResult:
        with tempfile.TemporaryDirectory(prefix="ferry-") as tmp:
            events = Path(tmp) / "events.jsonl"
            env = {k: v for k, v in os.environ.items() if k not in SERVICE_SECRET_ENV}
            env.update(ferry_ansible_env(env, str(events)))
            env.update(self.job_env(req))
            proc = subprocess.Popen(
                self.command(req),
                cwd=self.repo_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,  # its own process group, so a timeout kills ssh children too
            )
            timed_out = threading.Event()

            def kill():
                timed_out.set()
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

            timer = threading.Timer(self.timeout, kill) if self.timeout else None
            if timer:
                timer.daemon = True
                timer.start()
            tail: list[str] = []
            assert proc.stdout is not None
            try:
                self._collect(proc.stdout, tail)
                rc = proc.wait()
            finally:
                if timer:
                    timer.cancel()
            if timed_out.is_set():
                tail.append(f"\nferry: killed after {self.timeout:.0f}s (job timeout)\n")
                rc = rc if rc else -signal.SIGKILL
            return parse_file(events, self.busy_marker, rc=rc, log_tail="".join(tail))
