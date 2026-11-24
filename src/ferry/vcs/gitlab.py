"""GitLab backend (REST v4, stdlib only).

Auth: a project access token with the api scope. The CI driver keeps its locks in tags, so its
token needs Maintainer (create/delete tags); the service keeps locks in SQLite, so Developer is
enough there (plus read_repository for the checkouts). CI_JOB_TOKEN can't post MR notes, so ferry
needs its own token (FERRY_GITLAB_TOKEN). `token` may be a callable returning the current token.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Callable

from .base import ChangeRequest, Note

log = logging.getLogger("ferry.gitlab")

# detailed_merge_status values that mean "this MR could not be merged as it stands".
# unchecked / checking / preparing mean GitLab hasn't worked it out yet: refuse and let the user
# retry rather than apply on a guess (like GitHub's "unknown").
BLOCKING_MERGE_STATUS = {
    "unchecked",
    "checking",
    "preparing",
    "blocked_status",
    "broken_status",
    "conflict",
    "draft_status",
    "discussions_not_resolved",
    "need_rebase",
    "not_open",
    "requested_changes",
    "jira_association_missing",
    "merge_request_blocked",
    "security_policy_violations",
    "locked_paths",
    "locked_lfs_files",
}

# The MR's pipeline hasn't succeeded (yet). In the CI driver the apply job runs inside that pipeline,
# so it is necessarily still running: not a blocker there. The service runs outside CI, so there
# these block, as GitHub's required checks do (mergeable_state "blocked").
PIPELINE_MERGE_STATUS = {"ci_must_pass", "ci_still_running"}

Transport = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]
Token = str | Callable[[], str]


class GitLabError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, body: str):
        super().__init__(f"GitLab {method} {path} -> {status}: {body[:300]}")
        self.status = status


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class GitLabClient:
    def __init__(self, api_url: str, token: Token, transport: Transport = urllib_transport):
        self.api_url = api_url.rstrip("/")
        self._token = token
        self._transport = transport

    def request(self, method: str, path: str, params: dict | None = None, data: dict | None = None):
        url = f"{self.api_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        token = self._token() if callable(self._token) else self._token
        headers = {"PRIVATE-TOKEN": token, "Accept": "application/json"}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        status, raw = self._transport(method, url, headers, body)
        if status >= 400:
            raise GitLabError(status, method, path, raw.decode(errors="replace"))
        return json.loads(raw) if raw else None

    def paged(self, path: str, params: dict | None = None, max_pages: int = 50) -> list:
        out = []
        for page in range(1, max_pages + 1):
            batch = self.request("GET", path, {**(params or {}), "per_page": 100, "page": page})
            out.extend(batch or [])
            if not batch or len(batch) < 100:
                break
        return out


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitLabBackend:
    """pipeline_blocks: treat a pending or failed MR pipeline as a merge blocker (the service; see
    PIPELINE_MERGE_STATUS). The CI driver leaves it off: its own job is part of that pipeline."""

    def __init__(self, client: GitLabClient, project: str | int, pipeline_blocks: bool = False):
        self.client = client
        self.project = urllib.parse.quote(str(project), safe="")
        self.pipeline_blocks = pipeline_blocks
        self._bot: str | None = None

    @property
    def _p(self) -> str:
        return f"/projects/{self.project}"

    def get_change_request(self, cr_id: int) -> ChangeRequest:
        mr = self.client.request("GET", f"{self._p}/merge_requests/{cr_id}")
        status = mr.get("detailed_merge_status") or mr.get("merge_status") or ""
        blocking = BLOCKING_MERGE_STATUS | (PIPELINE_MERGE_STATUS if self.pipeline_blocks else set())
        blocked = sorted({status} & blocking)
        return ChangeRequest(
            id=mr["iid"],
            head_sha=mr["sha"],
            source_branch=mr["source_branch"],
            target_branch=mr["target_branch"],
            author=mr["author"]["username"],
            state=mr["state"],
            draft=bool(mr.get("draft") or mr.get("work_in_progress")),
            has_conflicts=bool(mr.get("has_conflicts")),
            discussions_resolved=bool(mr.get("blocking_discussions_resolved", True)),
            merge_status=status,
            blocked_reasons=blocked,
            web_url=mr.get("web_url", ""),
        )

    def merge_rule_blockers(self, cr: ChangeRequest) -> list[str]:
        return []  # GitLab's detailed_merge_status already reports its rules in blocked_reasons

    def behind_by(self, cr: ChangeRequest) -> int:
        """GitLab's diverged_commits_count: commits on the target branch the MR doesn't have."""
        mr = self.client.request("GET", f"{self._p}/merge_requests/{cr.id}",
                                 {"include_diverged_commits_count": "true"}) or {}
        n = mr.get("diverged_commits_count")
        if not isinstance(n, int):
            raise ValueError("GitLab didn't report diverged_commits_count for the MR")
        if mr.get("sha") != cr.head_sha:
            raise ValueError("the MR head moved while ferry was checking")
        return n

    def contributors(self, cr: ChangeRequest) -> set[str]:
        """Who pushed to the MR's source branch: GitLab's project push events, which GitLab records
        itself (commit author fields are whatever the committer typed). From a week before the MR
        was opened, so pushes that made the branch count too. The MR author is always included."""
        mr = self.client.request("GET", f"{self._p}/merge_requests/{cr.id}") or {}
        created = mr.get("created_at")
        params = {"action": "pushed"}
        if created:
            params["after"] = (_ts(created) - timedelta(days=8)).date().isoformat()
        out = {cr.author}
        for ev in self.client.paged(f"{self._p}/events", params):
            push = ev.get("push_data") or {}
            if push.get("ref_type") == "branch" and push.get("ref") == cr.source_branch:
                user = (ev.get("author") or {}).get("username") or ev.get("author_username")
                if user:
                    out.add(user)
        return out

    def changed_files(self, cr_id: int) -> list[str]:
        files: set[str] = set()
        for d in self.client.paged(f"{self._p}/merge_requests/{cr_id}/diffs"):
            for key in ("old_path", "new_path"):
                if d.get(key):
                    files.add(d[key])
        return sorted(files)

    def approvers(self, cr_id: int) -> list[str]:
        """Users who approved the MR's current head.

        GitLab doesn't record which commit an approval was for. With `approved_at` (newer GitLab),
        an approval counts if it was given at or after the current head arrived (the created_at of
        the MR diff version whose head_commit_sha is the MR's sha). Without it, approvals count only
        if the project resets approvals on push (then any remaining approval is for the head);
        otherwise none count, because an approval of an older commit would look the same.
        """
        sha = self.client.request("GET", f"{self._p}/merge_requests/{cr_id}")["sha"]
        data = self.client.request("GET", f"{self._p}/merge_requests/{cr_id}/approvals") or {}
        approved = [a for a in data.get("approved_by") or [] if a.get("user")]
        if not approved:
            return []
        if all(a.get("approved_at") for a in approved):
            since = self._head_arrived(cr_id, sha)
            if since is None:
                log.warning("!%s: no diff version for head %s; not counting approvals", cr_id, sha[:8])
                return []
            return sorted(a["user"]["username"] for a in approved if _ts(a["approved_at"]) >= since)
        if self._resets_approvals_on_push():
            return sorted(a["user"]["username"] for a in approved)
        log.warning("!%s: GitLab doesn't say when approvals were given and the project keeps approvals "
                    "on push, so ferry can't tell an approval of the head from an older one; counting none. "
                    "Enable 'Remove all approvals when commits are added' to fix this.", cr_id)
        return []

    def head_arrived_at(self, cr_id: int, sha: str) -> float | None:
        """When `sha` last became the MR's head, as epoch seconds (GitLab's own diff versions)."""
        t = self._head_arrived(cr_id, sha)
        return t.timestamp() if t else None

    def _head_arrived(self, cr_id: int, sha: str) -> datetime | None:
        """When `sha` last became the MR's head (newest diff version with that head)."""
        times = [_ts(v["created_at"]) for v in self.client.paged(f"{self._p}/merge_requests/{cr_id}/versions")
                 if v.get("head_commit_sha") == sha and v.get("created_at")]
        return max(times) if times else None

    def _resets_approvals_on_push(self) -> bool:
        try:
            cfg = self.client.request("GET", f"{self._p}/approvals") or {}
        except GitLabError as e:
            if e.status in (403, 404):
                return False
            raise
        return bool(cfg.get("reset_approvals_on_push"))

    def list_notes(self, cr_id: int) -> list[Note]:
        notes = self.client.paged(
            f"{self._p}/merge_requests/{cr_id}/notes", {"sort": "asc", "order_by": "created_at"}
        )
        return [
            Note(n["id"], n["author"]["username"], n.get("body", ""), n.get("created_at", ""))
            for n in notes
            if not n.get("system")
        ]

    def post_note(self, cr_id: int, body: str) -> Note:
        n = self.client.request("POST", f"{self._p}/merge_requests/{cr_id}/notes", data={"body": body})
        return Note(n["id"], n["author"]["username"], n.get("body", ""), n.get("created_at", ""))

    def bot_username(self) -> str:
        if self._bot is None:
            self._bot = self.client.request("GET", "/user")["username"]
        return self._bot

    def can_apply(self, username: str) -> bool:
        users = self.client.request("GET", "/users", {"username": username}) or []
        if not users:
            return False
        try:
            member = self.client.request("GET", f"{self._p}/members/all/{users[0]['id']}")
        except GitLabError as e:
            if e.status == 404:
                return False
            raise
        return int(member.get("access_level", 0)) >= 30  # Developer

    def change_requests_for_commit(self, sha: str) -> list[int]:
        mrs = self.client.request("GET", f"{self._p}/repository/commits/{sha}/merge_requests") or []
        return sorted(m["iid"] for m in mrs)
