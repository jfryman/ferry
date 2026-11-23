"""GitHub backend (REST v3, stdlib only).

Auth: a token with contents:read and pull requests/issues write on the repo. In the service
deployment that's a GitHub App installation token, which expires after an hour, so `token` may be a
callable that returns a current one (minting it needs the App's private key and is the service's job).
App tokens can't call GET /user, so pass `bot_login` (the App's "<slug>[bot]" login).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from .base import ChangeRequest, Note

API = "https://api.github.com"

# mergeable_state values that mean "this PR could not be merged as it stands". Branch protection
# (required reviews/checks, resolved conversations) shows up as "blocked". "unstable" (a non-required
# check failing) and "has_hooks" are mergeable. "unknown" means GitHub hasn't computed it yet: refuse
# and let the user retry rather than apply on a guess.
BLOCKING_MERGE_STATE = {"dirty", "blocked", "behind", "draft", "unknown"}

# Collaborator roles that may trigger applies (GitLab's Developer+).
APPLY_ROLES = {"admin", "maintain", "write"}

Transport = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]
Token = str | Callable[[], str]


class GitHubError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, body: str):
        super().__init__(f"GitHub {method} {path} -> {status}: {body[:300]}")
        self.status = status


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class GitHubClient:
    def __init__(self, token: Token, api_url: str = API, transport: Transport = urllib_transport):
        self.api_url = api_url.rstrip("/")
        self._token = token
        self._transport = transport

    def _current_token(self) -> str:
        return self._token() if callable(self._token) else self._token

    def request(self, method: str, path: str, params: dict | None = None, data: dict | None = None):
        url = f"{self.api_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Authorization": f"Bearer {self._current_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ferry",
        }
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        status, raw = self._transport(method, url, headers, body)
        if status >= 400:
            raise GitHubError(status, method, path, raw.decode(errors="replace"))
        return json.loads(raw) if raw else None

    def paged(self, path: str, params: dict | None = None, max_pages: int = 50) -> list:
        out = []
        for page in range(1, max_pages + 1):
            batch = self.request("GET", path, {**(params or {}), "per_page": 100, "page": page})
            out.extend(batch or [])
            if not batch or len(batch) < 100:
                break
        return out


class GitHubBackend:
    def __init__(self, client: GitHubClient, repo: str, bot_login: str | None = None):
        if repo.count("/") != 1:
            raise ValueError(f"repo must be OWNER/NAME, got {repo!r}")
        self.client = client
        owner, name = repo.split("/")
        self._r = f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"
        self._bot = bot_login

    def _pr(self, cr_id: int) -> dict:
        return self.client.request("GET", f"{self._r}/pulls/{cr_id}")

    def get_change_request(self, cr_id: int) -> ChangeRequest:
        pr = self._pr(cr_id)
        if pr.get("merged") or pr.get("merged_at"):
            state = "merged"
        else:
            state = "opened" if pr["state"] == "open" else "closed"
        merge_state = pr.get("mergeable_state") or "unknown"
        return ChangeRequest(
            id=pr["number"],
            head_sha=pr["head"]["sha"],
            source_branch=pr["head"]["ref"],
            target_branch=pr["base"]["ref"],
            author=pr["user"]["login"],
            state=state,
            draft=bool(pr.get("draft")),
            has_conflicts=pr.get("mergeable") is False,
            # Unresolved conversations block only through branch protection, which GitHub reports
            # as mergeable_state "blocked" (counted below).
            discussions_resolved=True,
            merge_status=merge_state,
            blocked_reasons=sorted({merge_state} & BLOCKING_MERGE_STATE) if state == "opened" else [],
            web_url=pr.get("html_url", ""),
        )

    def changed_files(self, cr_id: int) -> list[str]:
        files: set[str] = set()
        for f in self.client.paged(f"{self._r}/pulls/{cr_id}/files"):
            for key in ("filename", "previous_filename"):  # renames count on both sides
                if f.get(key):
                    files.add(f[key])
        return sorted(files)

    def approvers(self, cr_id: int) -> list[str]:
        """Users whose latest decisive review approves the PR's current head.

        A COMMENTED review doesn't change a user's decision (as on GitHub). An approval of an older
        commit doesn't count: the change it approved isn't the one being applied.
        """
        head = self._pr(cr_id)["head"]["sha"]
        latest: dict[str, dict] = {}
        for rv in self.client.paged(f"{self._r}/pulls/{cr_id}/reviews"):
            if rv.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED") and rv.get("user"):
                latest[rv["user"]["login"]] = rv  # reviews come oldest first
        return sorted(u for u, rv in latest.items() if rv["state"] == "APPROVED" and rv.get("commit_id") == head)

    def list_notes(self, cr_id: int) -> list[Note]:
        comments = self.client.paged(f"{self._r}/issues/{cr_id}/comments")
        return [Note(c["id"], c["user"]["login"], c.get("body") or "", c.get("created_at", "")) for c in comments]

    def post_note(self, cr_id: int, body: str) -> Note:
        c = self.client.request("POST", f"{self._r}/issues/{cr_id}/comments", data={"body": body})
        return Note(c["id"], c["user"]["login"], c.get("body") or "", c.get("created_at", ""))

    def bot_username(self) -> str:
        if self._bot is None:
            self._bot = self.client.request("GET", "/user")["login"]
        return self._bot

    def can_apply(self, username: str) -> bool:
        try:
            perm = self.client.request(
                "GET", f"{self._r}/collaborators/{urllib.parse.quote(username, safe='')}/permission"
            )
        except GitHubError as e:
            if e.status == 404:
                return False
            raise
        role = perm.get("role_name") or perm.get("permission") or ""
        return role in APPLY_ROLES

    def change_requests_for_commit(self, sha: str) -> list[int]:
        prs = self.client.request("GET", f"{self._r}/commits/{sha}/pulls") or []
        return sorted(p["number"] for p in prs)
