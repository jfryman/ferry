"""ferry serve for GitLab: GitLab's webhook intake in front of the shared service.

Only the fast path differs from GitHub (service.Service): how a webhook is authenticated, deduped
and turned into a Job. The queue, the worker, the workspace and the engine are the same code.

Events (one project webhook, "Merge request events" + "Comments", with a secret token):
  Merge Request Hook  open / reopen / update with new commits / draft -> ready   plan (plan rule)
  Merge Request Hook  close / merge                                            release the MR's locks
  Note Hook on an MR  "/ferry plan [inventory]"                                plan, if the author may
  Note Hook on an MR  "/ferry apply [inventory]"                               apply (the engine checks every gate)

GitLab doesn't sign the body: it sends the secret itself in X-Gitlab-Token, compared here in
constant time. Keep SSL verification on in the webhook so the secret only travels over TLS.
Deliveries are deduped on Idempotency-Key (stable across GitLab's retries), else X-Gitlab-Event-UUID.

Plan rule as on GitHub, with Developer+ for "write access": the MR author's Developer access decides
auto-plan; anyone else's MR waits for `/ferry plan` from a Developer. Draft and fork MRs (source
project != target project) aren't planned automatically.
"""

from __future__ import annotations

import hmac
import json
import threading

from .service import Job, Service
from .webhook import COMMAND_RE

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

    def __post_init__(self):
        super().__post_init__()
        self._dedupe_lock = threading.Lock()

    def handle_request(self, headers, body: bytes) -> tuple[int, str]:
        delivery = headers.get("Idempotency-Key") or headers.get("X-Gitlab-Event-UUID") or ""
        return self.handle(headers.get("X-Gitlab-Event", ""), delivery, headers.get("X-Gitlab-Token"), body)

    def handle(self, event: str, delivery: str, token: str | None, body: bytes) -> tuple[int, str]:
        if not verify_token(self.secret, token):
            return 401, "bad token"
        if self._duplicate(delivery):
            return 200, "duplicate delivery"
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

    def _duplicate(self, delivery: str) -> bool:
        if not delivery:
            return False
        with self._dedupe_lock:
            if delivery in self._seen:
                return True
            self._seen[delivery] = None
            while len(self._seen) > 1000:
                self._seen.popitem(last=False)
        return False

    def _our_project(self, project: dict) -> bool:
        path = str(project.get("path_with_namespace") or "")
        if path and path.lower() == self.settings.repo.lower():
            return True
        pid = self.settings.project_id
        return bool(pid) and str(project.get("id")) == str(pid)

    def _on_merge_request(self, p: dict, delivery: str) -> tuple[int, str]:
        mr = p.get("object_attributes") or {}
        action, iid = mr.get("action"), int(mr["iid"])
        sha = (mr.get("last_commit") or {}).get("id", "")
        if action in MR_CLOSE_ACTIONS:
            return self._enqueue(Job("unlock", iid, sha, delivery=delivery))
        new_commits = action == "update" and bool(mr.get("oldrev"))
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
        action, inventory = m.group(1), m.group(2)
        iid = int(p["merge_request"]["iid"])
        # Both commands need Developer access, checked here: a refused request never costs a checkout
        # or a credential (the engine checks again, along with every other apply gate).
        if not self.vcs.can_apply(user):
            self.vcs.post_note(iid, f"`/ferry {action}` needs Developer access to this project; @{user} doesn't have it.")
            return 202, f"{action} refused: no Developer access"
        cr = self.vcs.get_change_request(iid)
        return self._enqueue(Job(action, iid, cr.head_sha, user, inventory, delivery))
