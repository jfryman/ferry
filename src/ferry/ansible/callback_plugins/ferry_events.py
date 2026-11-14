# ferry_events: write one JSON line per task result to $FERRY_EVENTS_FILE.
#
# An aggregate callback, so the normal stdout callback still prints the human log in CI.
# Ansible hands callbacks a clean copy of each result: for no_log tasks that copy is already
# censored (no diff, no output). We additionally drop diff/msg whenever the result says it was
# censored, so a no_log task can only ever show up as "changed".
from __future__ import annotations

DOCUMENTATION = """
    name: ferry_events
    type: aggregate
    short_description: structured task results for ferry
    description: Writes JSON lines (task results and final stats) to the file named by FERRY_EVENTS_FILE.
    requirements:
      - enable in configuration (ferry does this itself via ANSIBLE_CALLBACKS_ENABLED)
"""

import json
import os

from ansible.plugins.callback import CallbackBase


def _res(result):
    # ansible-core >= 2.19 exposes .result; older versions only ._result.
    for attr in ("result", "_result"):
        value = getattr(result, attr, None)
        if isinstance(value, dict):
            return value
    return {}


def _host(result):
    host = getattr(result, "host", None) or getattr(result, "_host", None)
    return host.get_name() if host is not None else "?"


def _task(result):
    return getattr(result, "task", None) or getattr(result, "_task", None)


def _diffs(res):
    out = []
    diff = res.get("diff")
    if isinstance(diff, dict):
        out.append(diff)
    elif isinstance(diff, list):
        out.extend(d for d in diff if isinstance(d, dict))
    for item in res.get("results", []) or []:
        if isinstance(item, dict) and not item.get("censored"):
            out.extend(_diffs(item))
    return out


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "ferry_events"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        path = os.environ.get("FERRY_EVENTS_FILE")
        self._fh = open(path, "a", encoding="utf-8") if path else None
        self._play = None

    def _emit(self, event):
        if self._fh is None:
            return
        self._fh.write(json.dumps(event, default=str, sort_keys=True) + "\n")
        self._fh.flush()

    def v2_playbook_on_play_start(self, play):
        self._play = play.get_name()

    def _result(self, result, status, **extra):
        res = _res(result)
        task = _task(result)
        censored = bool(res.get("censored")) or bool(res.get("_ansible_no_log"))
        role = None
        if task is not None and getattr(task, "_role", None) is not None:
            role = task._role.get_name()
        event = {
            "type": "result",
            "play": self._play,
            "host": _host(result),
            "task": task.get_name() if task is not None else "?",
            "action": getattr(task, "action", None),
            "role": role,
            "status": status,
            "changed": bool(res.get("changed")),
            "no_log": censored,
            "diff": [] if censored else _diffs(res),
            "msg": None if censored else res.get("msg"),
        }
        event.update(extra)
        self._emit(event)

    def v2_runner_on_ok(self, result):
        res = _res(result)
        self._result(result, "changed" if res.get("changed") else "ok")

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._result(result, "ignored" if ignore_errors else "failed")

    def v2_runner_on_skipped(self, result):
        self._result(result, "skipped")

    def v2_runner_on_unreachable(self, result):
        self._result(result, "unreachable")

    def v2_playbook_on_stats(self, stats):
        hosts = {}
        for host in sorted(stats.processed.keys()):
            hosts[host] = stats.summarize(host)
        self._emit({"type": "stats", "hosts": hosts})
        if self._fh is not None:
            self._fh.close()
            self._fh = None
