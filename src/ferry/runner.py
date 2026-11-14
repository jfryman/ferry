"""Run ansible-playbook and collect ferry_events results.

The Runner protocol is the seam for tests (FakeRunner) and for a future remote executor.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

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
    def __init__(self, repo_dir: str | Path, busy_marker: str, ansible_playbook: str = "ansible-playbook"):
        self.repo_dir = Path(repo_dir)
        self.busy_marker = busy_marker
        self.bin = ansible_playbook

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
            env.update(req.extra_env)
            proc = subprocess.Popen(
                self.command(req),
                cwd=self.repo_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            tail: list[str] = []
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="", flush=True)  # the human log stays in the CI job output
                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)
            rc = proc.wait()
            return parse_file(events, self.busy_marker, rc=rc, log_tail="".join(tail))
