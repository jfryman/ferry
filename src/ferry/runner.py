"""Run ansible-playbook and collect ferry_events results.

The Runner protocol is the seam for tests (FakeRunner) and for a future remote executor.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .results import RunResult, parse_file

PLUGIN_DIR = Path(__file__).parent / "ansible" / "callback_plugins"
PLAYBOOK_DIR = Path(__file__).parent / "ansible" / "playbooks"


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


class AnsibleRunner:
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

    def command(self, req: RunRequest) -> list[str]:
        cmd = [self.bin, "-i", req.inventory, req.playbook]
        if req.limit:
            cmd += ["--limit", req.limit]
        if req.check:
            cmd.append("--check")
        if req.diff:
            cmd.append("--diff")
        if req.extra_vars_file:
            cmd += ["-e", f"@{req.extra_vars_file}"]
        return cmd

    def run(self, req: RunRequest) -> RunResult:
        with tempfile.TemporaryDirectory(prefix="ferry-") as tmp:
            events = Path(tmp) / "events.jsonl"
            env = dict(os.environ)
            enabled = [c for c in env.get("ANSIBLE_CALLBACKS_ENABLED", "").split(",") if c]
            env.update(
                {
                    "FERRY_EVENTS_FILE": str(events),
                    "ANSIBLE_CALLBACK_PLUGINS": os.pathsep.join(
                        p for p in [str(PLUGIN_DIR), env.get("ANSIBLE_CALLBACK_PLUGINS", "")] if p
                    ),
                    "ANSIBLE_CALLBACKS_ENABLED": ",".join(enabled + ["ferry_events"]),
                    "ANSIBLE_NOCOLOR": "1",
                    "ANSIBLE_RETRY_FILES_ENABLED": "0",
                }
            )
            env.update(self.env)
            env.update(req.extra_env)
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
                for line in proc.stdout:
                    shown = self.echo(line) if self.echo else line
                    print(shown or "", end="", flush=True)  # the human log stays in the CI job output
                    tail.append(line)
                    if len(tail) > 40:
                        tail.pop(0)
                rc = proc.wait()
            finally:
                if timer:
                    timer.cancel()
            if timed_out.is_set():
                tail.append(f"\nferry: killed after {self.timeout:.0f}s (job timeout)\n")
                rc = rc if rc else -signal.SIGKILL
            return parse_file(events, self.busy_marker, rc=rc, log_tail="".join(tail))
