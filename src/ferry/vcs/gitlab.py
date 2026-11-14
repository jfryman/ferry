"""GitLab backend (REST v4, stdlib only).

Auth: a project access token (api scope, Maintainer so it can create/delete lock tags). CI_JOB_TOKEN
can't post MR notes, so ferry needs its own token (FERRY_GITLAB_TOKEN).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from .base import ChangeRequest, Note

# detailed_merge_status values that mean "this MR could not be merged as it stands".
# ci_must_pass / ci_still_running are fine: the apply job itself runs inside the MR pipeline.
BLOCKING_MERGE_STATUS = {
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

Transport = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]


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
    def __init__(self, api_url: str, token: str, transport: Transport = urllib_transport):
        self.api_url = api_url.rstrip("/")
        self._token = token
        self._transport = transport

    def request(self, method: str, path: str, params: dict | None = None, data: dict | None = None):
        url = f"{self.api_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"PRIVATE-TOKEN": self._token, "Accept": "application/json"}
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


class GitLabBackend:
    def __init__(self, client: GitLabClient, project: str | int):
        self.client = client
        self.project = urllib.parse.quote(str(project), safe="")
        self._bot: str | None = None

    @property
    def _p(self) -> str:
        return f"/projects/{self.project}"

    def get_change_request(self, cr_id: int) -> ChangeRequest:
        mr = self.client.request("GET", f"{self._p}/merge_requests/{cr_id}")
        status = mr.get("detailed_merge_status") or mr.get("merge_status") or ""
        blocked = sorted({status} & BLOCKING_MERGE_STATUS)
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

    def changed_files(self, cr_id: int) -> list[str]:
        files: set[str] = set()
        for d in self.client.paged(f"{self._p}/merge_requests/{cr_id}/diffs"):
            for key in ("old_path", "new_path"):
                if d.get(key):
                    files.add(d[key])
        return sorted(files)

    def approvers(self, cr_id: int) -> list[str]:
        data = self.client.request("GET", f"{self._p}/merge_requests/{cr_id}/approvals")
        return sorted(a["user"]["username"] for a in data.get("approved_by", []) or [])

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
