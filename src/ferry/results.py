"""Parse ferry_events JSON lines into a per-host summary."""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TaskChange:
    host: str
    task: str
    role: str | None
    status: str  # changed | failed | unreachable | blocked
    diffs: list[dict] = field(default_factory=list)
    msg: str | None = None
    no_log: bool = False


@dataclass
class HostResult:
    host: str
    ok: int = 0
    changed: int = 0
    failed: int = 0
    unreachable: bool = False
    skipped: int = 0
    blocked: str | None = None  # reason, when a guard refused to touch the host

    @property
    def state(self) -> str:
        if self.blocked:
            return "blocked"
        if self.unreachable:
            return "unreachable"
        if self.failed:
            return "failed"
        if self.changed:
            return "changed"
        return "ok"


@dataclass
class RunResult:
    rc: int
    hosts: dict[str, HostResult] = field(default_factory=dict)
    changes: list[TaskChange] = field(default_factory=list)
    log_tail: str = ""

    @property
    def clean(self) -> bool:
        """No host failed, was unreachable or was blocked, and ansible itself exited cleanly."""
        if self.rc not in (0, 2):  # 2 = some host failed; that's reflected per host below
            return False
        return all(h.state in ("ok", "changed") for h in self.hosts.values())

    def totals(self) -> dict[str, int]:
        out = {"hosts": len(self.hosts), "changed": 0, "failed": 0, "unreachable": 0, "blocked": 0, "ok": 0}
        for h in self.hosts.values():
            out[h.state] = out.get(h.state, 0) + 1
        return out

    def counts(self) -> dict[str, int]:
        """Task-level counts, for the plan marker."""
        return {
            "changed_tasks": sum(h.changed for h in self.hosts.values()),
            "failed_tasks": sum(h.failed for h in self.hosts.values()),
            "hosts": len(self.hosts),
        }


def parse_events(lines, busy_marker: str, rc: int = 0, log_tail: str = "") -> RunResult:
    run = RunResult(rc=rc, log_tail=log_tail)

    def host(name: str) -> HostResult:
        if name not in run.hosts:
            run.hosts[name] = HostResult(name)
        return run.hosts[name]

    for line in lines:
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)
        if ev.get("type") == "stats":
            for name, s in ev.get("hosts", {}).items():
                h = host(name)
                h.ok = s.get("ok", 0)
                h.changed = s.get("changed", 0)
                h.skipped = s.get("skipped", 0)
                h.unreachable = h.unreachable or bool(s.get("unreachable", 0))
                # failures recorded per event below (so guard blocks aren't counted as failures)
            continue
        if ev.get("type") != "result":
            continue
        h = host(ev["host"])
        status = ev["status"]
        msg = ev.get("msg")
        if isinstance(msg, (list, dict)):
            msg = json.dumps(msg)
        if status == "failed" and isinstance(msg, str) and msg.startswith(busy_marker):
            h.blocked = msg[len(busy_marker):].lstrip(": ").strip() or "busy"
            run.changes.append(TaskChange(h.host, ev["task"], ev.get("role"), "blocked", msg=h.blocked))
            continue
        if status == "failed":
            h.failed += 1
            run.changes.append(
                TaskChange(h.host, ev["task"], ev.get("role"), "failed", msg=msg, no_log=ev.get("no_log", False))
            )
        elif status == "unreachable":
            h.unreachable = True
            run.changes.append(TaskChange(h.host, ev["task"], ev.get("role"), "unreachable", msg=msg))
        elif status == "changed":
            run.changes.append(
                TaskChange(
                    h.host,
                    ev["task"],
                    ev.get("role"),
                    "changed",
                    diffs=ev.get("diff") or [],
                    no_log=ev.get("no_log", False),
                )
            )
    return run


def parse_file(path: Path, busy_marker: str, rc: int = 0, log_tail: str = "") -> RunResult:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return parse_events(lines, busy_marker, rc=rc, log_tail=log_tail)


def _as_lines(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (dict, list)):
        value = json.dumps(value, indent=2, sort_keys=True)
    text = str(value)
    if text and not text.endswith("\n"):
        text += "\n"
    return text.splitlines(keepends=True)


def diff_paths(diff: dict) -> list[str]:
    return [str(diff[k]) for k in ("before_header", "after_header") if diff.get(k)]


def render_diff(diff: dict) -> str:
    """One Ansible diff dict -> unified diff text."""
    if "prepared" in diff and diff["prepared"]:
        return str(diff["prepared"]).rstrip("\n") + "\n"
    before = _as_lines(diff.get("before"))
    after = _as_lines(diff.get("after"))
    if before == after:
        return ""
    a = diff.get("before_header", "before")
    b = diff.get("after_header", "after")
    return "".join(difflib.unified_diff(before, after, fromfile=str(a), tofile=str(b)))
