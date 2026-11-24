"""Test doubles: an in-memory VCS backend and a runner that returns canned ansible events."""

from __future__ import annotations

import json

from ferry.results import RunResult, parse_events
from ferry.runner import RunRequest
from ferry.vcs.base import ChangeRequest, Note, NotFound


class FakeVCS:
    def __init__(self, bot="ferry-bot"):
        self.bot = bot
        self.crs: dict[int, ChangeRequest] = {}
        self.files: dict[int, list[str]] = {}
        self.approvals: dict[int, list[str]] = {}
        self.notes: dict[int, list[Note]] = {}
        self.developers = {"james", "pip"}
        self.commit_mrs: dict[str, list[int]] = {}
        self.rule_blockers: dict[int, list[str]] = {}
        self.lookup_errors: dict[int, Exception] = {}  # get_change_request(id) raises this
        self._next = 1

    def add_cr(self, cr_id, sha="a" * 40, author="pip", files=None, **kw):
        self.crs[cr_id] = ChangeRequest(cr_id, sha, f"b{cr_id}", "main", author, kw.pop("state", "opened"), **kw)
        self.files[cr_id] = files or []
        self.approvals.setdefault(cr_id, [])
        self.notes.setdefault(cr_id, [])
        return self.crs[cr_id]

    def get_change_request(self, cr_id):
        if cr_id in self.lookup_errors:
            raise self.lookup_errors[cr_id]
        if cr_id not in self.crs:
            raise NotFound(f"no change request {cr_id}")
        return self.crs[cr_id]

    def merge_rule_blockers(self, cr):
        return list(self.rule_blockers.get(cr.id, []))

    def changed_files(self, cr_id):
        return list(self.files[cr_id])

    def approvers(self, cr_id):
        return list(self.approvals[cr_id])

    def list_notes(self, cr_id):
        return list(self.notes[cr_id])

    def post_note(self, cr_id, body, author=None):
        n = Note(self._next, author or self.bot, body)
        self._next += 1
        self.notes[cr_id].append(n)
        return n

    def bot_username(self):
        return self.bot

    def can_apply(self, username):
        return username in self.developers

    def change_requests_for_commit(self, sha):
        return self.commit_mrs.get(sha, [])


def ev_result(host, task, status, role=None, diff=None, msg=None, no_log=False):
    return json.dumps({
        "type": "result", "host": host, "task": task, "role": role, "status": status,
        "changed": status == "changed", "diff": diff or [], "msg": msg, "no_log": no_log, "action": "x",
        "play": "p",
    })


def ev_stats(**hosts):
    return json.dumps({"type": "stats", "hosts": hosts})


def stats(ok=1, changed=0, failures=0, unreachable=0, skipped=0):
    return {"ok": ok, "changed": changed, "failures": failures, "unreachable": unreachable, "skipped": skipped}


class FakeRunner:
    """Returns the next canned event list; records every request."""

    def __init__(self, *runs, busy_marker="FERRY_HOST_BUSY"):
        self.queue = list(runs)
        self.requests: list[RunRequest] = []
        self.busy_marker = busy_marker

    def run(self, req: RunRequest) -> RunResult:
        self.requests.append(req)
        lines, rc = self.queue.pop(0) if self.queue else ([ev_stats()], 0)
        return parse_events(lines, self.busy_marker, rc=rc)
