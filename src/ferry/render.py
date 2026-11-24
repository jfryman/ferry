"""Markdown for MR comments. Everything user-controlled or host-derived goes through the Redactor."""

from __future__ import annotations

import json
import re

from .results import RunResult, diff_paths, render_diff
from .sanitize import MASK, Redactor
from .scope import Scope

MARKER_RE = re.compile(r"<!-- ferry:(plan|apply|lock) (\{.*?\}) -->", re.DOTALL)
ICON = {"ok": "✅", "changed": "🟡", "failed": "❌", "unreachable": "📵", "blocked": "⏸️"}


def marker(kind: str, data: dict) -> str:
    return f"<!-- ferry:{kind} {json.dumps(data, sort_keys=True)} -->"


def parse_markers(body: str) -> list[tuple[str, dict]]:
    out = []
    for kind, blob in MARKER_RE.findall(body or ""):
        try:
            out.append((kind, json.loads(blob)))
        except ValueError:
            continue
    return out


def _fence(text: str, lang: str = "") -> str:
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}{lang}\n{text.rstrip()}\n{ticks}"


def _host_table(run: RunResult) -> str:
    if not run.hosts:
        return "_No hosts matched._"
    rows = ["| host | state | changed | failed |", "|---|---|---|---|"]
    for name in sorted(run.hosts):
        h = run.hosts[name]
        state = h.state + (f" ({h.blocked})" if h.blocked else "")
        rows.append(f"| `{name}` | {ICON.get(h.state, '')} {state} | {h.changed} | {h.failed} |")
    return "\n".join(rows)


def _changes(run: RunResult, red: Redactor, budget: int) -> str:
    parts = []
    used = 0
    omitted = 0
    for c in run.changes:
        title = f"{c.host}: {c.task}" + (f" ({c.role})" if c.role else "")
        if c.status == "changed":
            if c.no_log:
                body = "_no_log task: changed, details hidden._"
            else:
                diffs = []
                for d in c.diffs:
                    paths = diff_paths(d)
                    if any(red.sensitive_path(p) for p in paths):
                        diffs.append(f"{', '.join(paths)}: {MASK} (sensitive path, diff hidden)")
                        continue
                    text = render_diff(d)
                    if text:
                        diffs.append(red.text(text))
                body = _fence("\n".join(diffs), "diff") if diffs else "_changed (no diff reported by the module)_"
        elif c.status == "blocked":
            body = f"Host refused changes: {red.text(c.msg or '')}"
        else:
            msg = "_details hidden (no_log)_" if c.no_log else _fence(red.text(str(c.msg or "")) or "")
            body = f"**{c.status}**\n\n{msg}"
        block = f"<details><summary>{ICON.get(c.status, '')} {red.text(title)}</summary>\n\n{body}\n\n</details>"
        if used + len(block) > budget:
            omitted += 1
            continue
        used += len(block)
        parts.append(block)
    if omitted:
        parts.append(f"_{omitted} more task results omitted (comment size limit); see the CI job log._")
    return "\n".join(parts) if parts else "_No changes._"


def _scope_block(scope: Scope, red: Redactor) -> str:
    lines = [f"- `{red.text(f)}`: {red.text(why)}" for f, why in scope.reasons]
    return (
        f"**Scope:** {scope.describe()}\n\n<details><summary>why</summary>\n\n"
        + ("\n".join(lines) or "_no changed files_")
        + "\n\n</details>"
    )


def plan_comment(
    inventory: str, sha: str, scope: Scope, run: RunResult | None, meta: dict, red: Redactor,
    max_chars: int, job_url: str = "", apply_how: str = "run the manual `ferry:apply` job in this MR's pipeline",
) -> str:
    head = f"### ferry plan: `{inventory}` @ `{sha[:8]}`"
    if run is None:
        body = "Nothing to plan: no host is affected by this change."
    else:
        t = run.totals()
        verdict = "✅ plan is clean" if run.clean else "❌ plan is **not** clean (apply will be refused)"
        body = (
            f"{verdict}: {t['changed']} host(s) would change, {t['ok']} unchanged, "
            f"{t['failed']} failed, {t['unreachable']} unreachable, {t['blocked']} blocked.\n\n"
            + _host_table(run)
            + "\n\n"
            + _changes(run, red, budget=max_chars - 4000)
        )
        if run.rc not in (0, 2) and not run.hosts:
            body += "\n\nansible-playbook failed before any host ran:\n\n" + _fence(red.text(run.log_tail) or "")
    foot = (
        f"\n\n---\nApply: {apply_how}. It re-checks that this "
        "plan is for the MR head commit, is clean and recent, that the MR is approved and mergeable, and "
        "that this MR holds the inventory lock."
    )
    if job_url:
        foot += f" [plan job]({job_url})"
    return "\n\n".join([head, _scope_block(scope, red), body]) + foot + "\n\n" + marker("plan", meta)


def apply_comment(inventory: str, sha: str, run: RunResult, meta: dict, red: Redactor, max_chars: int,
                  job_url: str = "") -> str:
    t = run.totals()
    ok = run.clean
    head = f"### ferry apply: `{inventory}` @ `{sha[:8]}` {'✅ applied' if ok else '❌ apply had failures'}"
    body = (
        f"{t['changed']} host(s) changed, {t['ok']} unchanged, {t['failed']} failed, "
        f"{t['unreachable']} unreachable, {t['blocked']} blocked.\n\n"
        + _host_table(run)
        + "\n\n"
        + _changes(run, red, budget=max_chars - 3000)
    )
    if job_url:
        body += f"\n\n[apply job]({job_url})"
    body += "\n\nThe inventory lock stays with this MR until it is merged or closed."
    return f"{head}\n\n{body}\n\n{marker('apply', meta)}"


def refusal_comment(action: str, inventory: str, sha: str, reasons: list[str], red: Redactor) -> str:
    items = "\n".join(f"- {red.text(r)}" for r in reasons)
    return f"### ferry {action}: `{inventory}` @ `{sha[:8]}` refused\n\n{items}"
