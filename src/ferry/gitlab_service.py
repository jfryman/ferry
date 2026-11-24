"""ferry serve for GitLab: GitLab's webhook intake in front of the shared service.

Only the fast path differs from GitHub (service.Service): how a webhook is authenticated, deduped
and turned into a Job. The queue, the worker, the workspace and the engine are the same code.

Events (one project webhook, "Merge request events" + "Comments", with a secret token):
  Merge Request Hook  open / reopen / update with new commits / draft -> ready   plan (plan rule)
  Merge Request Hook  close / merge                                            release the MR's locks
  Note Hook on an MR  "/ferry plan [inventory] [--sha C]"                       plan, if the author may
  Note Hook on an MR  "/ferry apply [inventory] [--sha C]"                      apply (the engine checks every gate)

Notes are pinned to the MR head the commenter saw: the hook's merge_request.last_commit must still be
the head, and that head's diff version must predate the note (or `--sha` names it).

GitLab doesn't sign the body: it sends the secret itself in X-Gitlab-Token, compared here in
constant time. Keep SSL verification on in the webhook so the secret only travels over TLS.
Deliveries are deduped on Idempotency-Key (stable across GitLab's retries), else X-Gitlab-Event-UUID,
and on a digest of the body, in the state file (so across restarts too). Events whose own timestamps
(the note's created_at/updated_at, the MR's updated_at) are older than max_event_age_minutes are refused.

Plan rule as on GitHub, with Developer+ for "write access": the MR author's Developer access decides
auto-plan; anyone else's MR waits for `/ferry plan` from a Developer. Draft and fork MRs (source
project != target project) aren't planned automatically.
"""

from __future__ import annotations

import hmac
import json
import logging

from .service import Job, Service, _valid_id, parse_time
from .webhook import COMMAND_RE

log = logging.getLogger("ferry.gitlab_service")

MR_PLAN_ACTIONS = {"open", "reopen"}
MR_CLOSE_ACTIONS = {"close", "merge"}


def verify_token(secret: bytes, header: str | None) -> bool:
    """X-Gitlab-Token must equal the webhook secret (constant-time compare)."""
    if not header or not secret:
        return False
    return hmac.compare_digest(header.encode(), secret)


def _became_ready(changes: dict) -> bool:
    for key in ("draft", "work_in_progress"):
        c = changes.get(key) or {}
        if c.get("previous") and not c.get("current"):
            return True
    return False


class GitLabService(Service):
    noun = "MR"
    access = "Developer access"

    def handle_request(self, headers, body: bytes) -> tuple[int, str]:
        delivery = headers.get("Idempotency-Key") or headers.get("X-Gitlab-Event-UUID") or ""
        return self.handle(headers.get("X-Gitlab-Event", ""), delivery, headers.get("X-Gitlab-Token"), body)

    def handle(self, event: str, delivery: str, token: str | None, body: bytes) -> tuple[int, str]:
        if not verify_token(self.secret, token):
            return 401, "bad token"
        return self._deduped(delivery, body, lambda: self._dispatch(event, delivery, body))

    def _dispatch(self, event: str, delivery: str, body: bytes) -> tuple[int, str]:
        try:
            payload = json.loads(body)
        except ValueError:
            return 400, "bad json"
        if not isinstance(payload, dict) or not self._our_project(payload.get("project") or {}):
            return 202, "not this service's project"
        if event == "Merge Request Hook":
            return self._on_merge_request(payload, delivery)
        if event == "Note Hook":
            return self._on_note(payload, delivery)
        return 202, f"ignored event {event}"

    def _our_project(self, project: dict) -> bool:
        path = str(project.get("path_with_namespace") or "")
        if path and path.lower() == self.settings.repo.lower():
            return True
        pid = self.settings.project_id
        return bool(pid) and str(project.get("id")) == str(pid)

    def _on_merge_request(self, p: dict, delivery: str) -> tuple[int, str]:
        mr = p.get("object_attributes") or {}
        action, iid = mr.get("action"), int(mr["iid"])
        # Every action, close included: a replayed close would release an open MR's locks.
        stale = self._stale(mr.get("updated_at"))
        if stale:
            return 202, stale
        sha = (mr.get("last_commit") or {}).get("id", "")
        if action in MR_CLOSE_ACTIONS:
            return self._enqueue(Job("unlock", iid, sha, delivery=delivery))
        new_commits = action == "update" and bool(mr.get("oldrev"))
        if new_commits:  # whoever pushed: their approval of this MR won't count (#25)
            self.state.saw_pusher(iid, (p.get("user") or {}).get("username") or "", self.clock())
        if not (action in MR_PLAN_ACTIONS or new_commits or
                (action == "update" and _became_ready(p.get("changes") or {}))):
            return 202, f"ignored merge_request.{action}"
        if mr.get("draft") or mr.get("work_in_progress"):
            return 202, "draft: not planned"
        if mr.get("source_project_id") != mr.get("target_project_id"):
            return 202, "fork MRs are not planned"
        cr = self.vcs.get_change_request(iid)  # the hook carries only author_id
        sha = sha or cr.head_sha
        if not self.vcs.can_apply(cr.author):
            self._say_waiting(iid, sha, cr.author)
            return 202, "waiting for /ferry plan"
        pusher = (p.get("user") or {}).get("username") or cr.author
        if new_commits and pusher != cr.author and not self.vcs.can_apply(pusher):
            self._say_waiting(iid, sha, pusher, who="pushes from users")
            return 202, "waiting for /ferry plan"
        return self._enqueue(Job("plan", iid, sha, cr.author, delivery=delivery))

    def _on_note(self, p: dict, delivery: str) -> tuple[int, str]:
        note = p.get("object_attributes") or {}
        if note.get("noteable_type") != "MergeRequest" or note.get("action", "create") != "create" \
                or "merge_request" not in p:
            return 202, "not a new MR comment"
        user = (p.get("user") or {}).get("username", "")
        if user == self.vcs.bot_username():
            return 202, "own comment"
        text = (note.get("note") or "").strip()
        m = COMMAND_RE.match(text.splitlines()[0]) if text else None
        if not m or not text.startswith("/ferry"):
            return 202, "not a ferry command"
        stale = self._stale(note.get("created_at"), note.get("updated_at"))
        if stale:
            return 202, stale
        if not _valid_id(note.get("id")):
            return 400, "note id missing or malformed"
        project = (p.get("project") or {}).get("id") or self.settings.project_id or self.settings.repo.lower()
        return self._once(self._comment_key("note", project, note["id"]), lambda: self._on_note_command(p, m, delivery))

    def _on_note_command(self, p: dict, m, delivery: str) -> tuple[int, str]:
        note = p["object_attributes"]
        user = (p.get("user") or {}).get("username", "")
        action, inventory, named = m.group(1), m.group(2), m.group(3)
        iid = int(p["merge_request"]["iid"])
        # Both commands need Developer access, checked here: a refused request never costs a checkout
        # or a credential (the engine checks again, along with every other apply gate).
        if not self.vcs.can_apply(user):
            self.vcs.post_note(iid, f"`/ferry {action}` needs Developer access to this project; @{user} doesn't have it.")
            return 202, f"{action} refused: no Developer access"
        cr = self.vcs.get_change_request(iid)
        if not self._open_or_say(cr, action):
            return 202, f"{action} refused: {self.noun} is {cr.state}"
        # The hook carries the MR's head as GitLab saw it; the MR's diff versions say when it arrived.
        payload_head = ((p["merge_request"].get("last_commit") or {}).get("id")) or None
        sha = self._pin_head(cr, action, parse_time(note["created_at"]), named, payload_head)
        if sha is None:
            return 202, f"{action} refused: the head isn't the one commented on"
        return self._enqueue(Job(action, iid, sha, user, inventory, delivery, pinned=True))

    def _head_since(self, number: int, sha: str) -> float | None:
        try:
            return self.vcs.head_arrived_at(number, sha)
        except Exception:  # can't tell: refuse (the caller says why), never guess
            log.warning("!%s: couldn't read the MR's diff versions", number, exc_info=True)
            return None
