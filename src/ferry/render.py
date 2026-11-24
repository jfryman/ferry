"""Markdown for MR comments. Everything user-controlled or host-derived goes through the Redactor."""

from __future__ import annotations

import hashlib
import hmac
import json
import re

from .results import RunResult, diff_paths, render_diff
from .sanitize import MASK, Redactor
from .scope import Scope

MARKER_RE = re.compile(r"<!-- ferry:(plan|apply|lock|unlock) (\{.*?\}) -->", re.DOTALL)
# The one marker ferry trusts: the whole last line of a comment (#24).
LAST_LINE_MARKER_RE = re.compile(r"\A<!-- ferry:(plan|apply|lock|unlock) (\{[^\n]*\}) -->\Z")
ICON = {"ok": "✅", "changed": "🟡", "failed": "❌", "unreachable": "📵", "blocked": "⏸️"}

# Characters that are markup in GitHub/GitLab markdown or HTML, as numeric entities. Applied to
# PR- and host-controlled text outside code fences, so a task name can't close ferry's <details>,
# fake a verdict line, link somewhere or mention people.
_INLINE_ESCAPES = {c: f"&#{ord(c)};" for c in "&<>`*_[]()~#|\\!@:"}


def inline(text: str | None) -> str:
    """Untrusted text for use outside code fences: rendered literally, on one line."""
    s = " ".join(str(text or "").split())
    return "".join(_INLINE_ESCAPES.get(c, c) for c in s)


def _mac(kind: str, data: dict, key: bytes) -> str:
    blob = json.dumps({"kind": kind, **{k: v for k, v in data.items() if k != "mac"}}, sort_keys=True)
    return hmac.new(key, blob.encode(), hashlib.sha256).hexdigest()


def marker(kind: str, data: dict, key: bytes | None = None) -> str:
    """The hidden marker ferry reads back. With a key (the service), it carries an HMAC that only
    the service can make, so a marker copied or forged into a comment never counts."""
    if key is not None:
        data = {**data, "mac": _mac(kind, data, key)}
    return f"<!-- ferry:{kind} {json.dumps(data, sort_keys=True)} -->"


def trusted_marker(body: str, key: bytes | None = None) -> tuple[str, dict] | None:
    """The marker on the last line of a comment ferry posted, if it is the whole line (and, with a
    key, correctly signed). Markers anywhere else in a comment are PR- or host-controlled text."""
    lines = (body or "").rstrip().splitlines()
    if not lines:
        return None
    m = LAST_LINE_MARKER_RE.match(lines[-1])
    if not m:
        return None
    try:
        data = json.loads(m.group(2))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if key is not None:
        mac = data.get("mac")
        if not isinstance(mac, str) or not hmac.compare_digest(mac, _mac(m.group(1), data, key)):
            return None
    return m.group(1), data


def neutralize(text: str) -> str:
    """No HTML comment can start or end inside text ferry didn't write (code fences included)."""
    return text.replace("<!--", "<!\u200b--").replace("-->", "--\u200b>")


def parse_markers(body: str) -> list[tuple[str, dict]]:
    out = []
    for kind, blob in MARKER_RE.findall(body or ""):
        try:
            out.append((kind, json.loads(blob)))
        except ValueError:
            continue
    return out


def _fence(text: str, lang: str = "") -> str:
    text = neutralize(text)
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}{lang}\n{text.rstrip()}\n{ticks}"


_ORDER = {"failed": 0, "unreachable": 1, "blocked": 2, "changed": 3, "ok": 4}


def _host_table(run: RunResult, budget: int = 1 << 30) -> str:
    if not run.hosts:
        return "_No hosts matched._"
    rows = ["| host | state | changed | failed |", "|---|---|---|---|"]
    used = sum(len(r) + 1 for r in rows)
    # Worst first, so a cut table still shows what went wrong.
    names = sorted(run.hosts, key=lambda n: (_ORDER.get(run.hosts[n].state, 5), n))
    for i, name in enumerate(names):
        h = run.hosts[name]
        state = h.state + (f" ({inline(h.blocked)})" if h.blocked else "")
        row = f"| {inline(name)} | {ICON.get(h.state, '')} {state} | {h.changed} | {h.failed} |"
        if used + len(row) + 1 > budget - 80:
            rest = len(names) - i
            rows.append(f"\n_{rest} more host(s) not listed (comment size limit)._")
            break
        used += len(row) + 1
        rows.append(row)
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
            body = f"Host refused changes: {inline(red.text(c.msg or ''))}"
        else:
            msg = "_details hidden (no_log)_" if c.no_log else _fence(red.text(str(c.msg or "")) or "")
            body = f"**{c.status}**\n\n{msg}"
        block = f"<details><summary>{ICON.get(c.status, '')} {inline(red.text(title))}</summary>\n\n{body}\n\n</details>"
        if used + len(block) > budget:
            omitted += 1
            continue
        used += len(block)
        parts.append(block)
    if omitted:
        parts.append(f"_{omitted} more task results omitted (comment size limit); see the CI job log._")
    return "\n".join(parts) if parts else "_No changes._"


def _scope_block(scope: Scope, red: Redactor, budget: int = 1 << 30) -> str:
    lines, used = [], 0
    for i, (f, why) in enumerate(scope.reasons):
        line = f"- {inline(red.text(f))}: {inline(red.text(why))}"
        if used + len(line) + 1 > budget:
            lines.append(f"- _{len(scope.reasons) - i} more file(s) not listed (comment size limit)_")
            break
        used += len(line) + 1
        lines.append(line)
    desc = inline(red.text(scope.describe()))
    if len(desc) > max(budget // 4, 200):
        desc = desc[: max(budget // 4, 200)] + " … (cut)"
    return (
        f"**Scope:** {desc}\n\n<details><summary>why</summary>\n\n"
        + ("\n".join(lines) or "_no changed files_")
        + "\n\n</details>"
    )


def plan_comment(
    inventory: str, sha: str, scope: Scope, run: RunResult | None, meta: dict, red: Redactor,
    max_chars: int, job_url: str = "", apply_how: str = "run the manual `ferry:apply` job in this MR's pipeline",
    warnings: list[str] | tuple[str, ...] = (), marker_key: bytes | None = None,
) -> str:
    head = f"### ferry plan: `{inventory}` @ `{sha[:8]}`"
    for w in warnings:
        head += f"\n\n> ⚠️ {neutralize(red.text(w))}"
    foot = (
        f"\n\n---\nApply: {apply_how}. It re-checks that this "
        "plan is for the MR head commit, is clean and recent, that the MR is approved and mergeable, and "
        "that this MR holds the inventory lock."
    )
    if job_url:
        foot += f" [plan job]({job_url})"
    tail = foot + "\n\n" + marker("plan", meta, marker_key)
    # The whole comment stays under max_chars (#24): the scope list, the host table and the task
    # results each get a share of what's left, and say what they left out.
    room = max(max_chars - len(head) - len(tail) - 200, 2000)
    scope_block = _scope_block(scope, red, budget=room // 5)
    room -= len(scope_block)
    if run is None:
        body = "Nothing to plan: no host is affected by this change."
    else:
        t = run.totals()
        verdict = "✅ plan is clean" if run.clean else "❌ plan is **not** clean (apply will be refused)"
        summary = (f"{verdict}: {t['changed']} host(s) would change, {t['ok']} unchanged, "
                   f"{t['failed']} failed, {t['unreachable']} unreachable, {t['blocked']} blocked.")
        table = _host_table(run, budget=room // 3)
        body = summary + "\n\n" + table + "\n\n"
        extra = ""
        if run.rc not in (0, 2) and not run.hosts:
            extra = "\n\nansible-playbook failed before any host ran:\n\n" + _fence((red.text(run.log_tail) or "")[-2000:])
        body += _changes(run, red, budget=max(room - len(body) - len(extra) - 200, 500)) + extra
    return _bounded("\n\n".join([head, scope_block, body]), tail, max_chars)


def _bounded(text: str, tail: str, max_chars: int) -> str:
    """text + tail within max_chars; the tail (with the marker) is never cut."""
    if len(text) + len(tail) <= max_chars:
        return text + tail
    note = "\n\n_… cut here (comment size limit)._"
    return text[: max(max_chars - len(tail) - len(note), 0)] + note + tail


def apply_comment(inventory: str, sha: str, run: RunResult, meta: dict, red: Redactor, max_chars: int,
                  job_url: str = "", marker_key: bytes | None = None) -> str:
    t = run.totals()
    ok = run.clean
    head = f"### ferry apply: `{inventory}` @ `{sha[:8]}` {'✅ applied' if ok else '❌ apply had failures'}"
    tail = ""
    if job_url:
        tail += f"\n\n[apply job]({job_url})"
    tail += "\n\nThe inventory lock stays with this MR until it is merged or closed."
    tail += f"\n\n{marker('apply', meta, marker_key)}"
    room = max(max_chars - len(head) - len(tail) - 200, 2000)
    summary = (f"{t['changed']} host(s) changed, {t['ok']} unchanged, {t['failed']} failed, "
               f"{t['unreachable']} unreachable, {t['blocked']} blocked.")
    table = _host_table(run, budget=room // 3)
    body = summary + "\n\n" + table + "\n\n"
    body += _changes(run, red, budget=max(room - len(body) - 200, 500))
    return _bounded(f"{head}\n\n{body}", tail, max_chars)


def refusal_comment(action: str, inventory: str, sha: str, reasons: list[str], red: Redactor) -> str:
    items = "\n".join(f"- {neutralize(red.text(r))}" for r in reasons)
    return f"### ferry {action}: `{inventory}` @ `{sha[:8]}` refused\n\n{items}"


HELP = """### ferry commands

- `/ferry plan [inventory] [--sha COMMIT]`: check-mode run of this change (needs write access).
- `/ferry apply [inventory] [--sha COMMIT]`: apply the latest clean plan, once it's approved.
- `/ferry unlock [inventory]`: release the inventory lock this change holds, so another change can
  plan. The plan on this change can't be applied after that: plan it again first.
- `/ferry help`: this list.

Only comments that start with `/ferry` are read; the command is the first line."""


def help_comment(bad: str | None = None) -> str:
    if bad is None:
        return HELP
    return f"ferry didn't understand that command: {inline(bad[:80])}\n\n{HELP}"


def unlock_comment(inventory: str, user: str, at: float, key: bytes | None = None) -> str:
    # inventory and user are command-parsed names ([A-Za-z0-9_.-]) and a code-host login.
    body = (f"### ferry unlock: `{inventory}`\n\n@{inline(user)} released the lock on "
            f"`{inventory}`; another change can plan it now. The plan on this change no "
            f"longer applies: run `/ferry plan` again before `/ferry apply`.")
    return body + "\n\n" + marker("unlock", {"v": 1, "inventory": inventory, "user": user, "at": at}, key)
