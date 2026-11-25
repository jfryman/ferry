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

from . import github_protection as protection
from .base import ChangeRequest, Note

API = "https://api.github.com"

# mergeable_state values that mean "this PR could not be merged as it stands". "unstable" (a
# non-required check failing) and "has_hooks" are mergeable. "unknown" means GitHub hasn't computed it
# yet: refuse and let the user retry rather than apply on a guess. "blocked" (branch protection or a
# ruleset) is left to merge_rule_blockers, which names the unmet rule and doesn't let ferry's own
# required check deadlock the apply.
BLOCKING_MERGE_STATE = {"dirty", "behind", "draft", "unknown"}

# App permissions behind each API ferry reads for the merge-rule check (shown when one is missing).
PERMISSION_FOR = {
    "graphql": "Pull requests: read",
    "checks": "Checks: read and Commit statuses: read",
    "rules": "Metadata: read",
    "protection": "Administration: read",
}

# Collaborator roles that may trigger applies (GitLab's Developer+).
APPLY_ROLES = {"admin", "maintain", "write"}

Transport = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]
Token = str | Callable[[], str]


class GitHubError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, body: str):
        super().__init__(f"GitHub {method} {path} -> {status}: {body[:300]}")
        self.status = status
        self.body = body


class GraphQLError(RuntimeError):
    def __init__(self, errors: list[dict]):
        super().__init__("GitHub GraphQL: " + "; ".join(str(e.get("message", e))[:200] for e in errors[:3]))
        self.errors = errors

    @property
    def forbidden(self) -> bool:
        return any(e.get("type") == "FORBIDDEN" or "not accessible by integration" in str(e.get("message", ""))
                   for e in self.errors)

    @property
    def paths(self) -> list[str]:
        return [str(p) for e in self.errors for p in (e.get("path") or [])]


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

    def request(self, method: str, path: str, params: dict | None = None, data: dict | None = None,
                url: str | None = None):
        url = url or f"{self.api_url}{path}"
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

    def graphql_url(self) -> str:
        # GitHub Enterprise Server: REST at /api/v3, GraphQL at /api/graphql.
        if self.api_url.endswith("/api/v3"):
            return self.api_url[: -len("/v3")] + "/graphql"
        return self.api_url + "/graphql"

    def graphql(self, query: str, variables: dict) -> dict:
        """POST a GraphQL query; any `errors` entry raises (partial data could hide a rule)."""
        out = self.request("POST", "/graphql", data={"query": query, "variables": variables},
                           url=self.graphql_url()) or {}
        if out.get("errors"):
            raise GraphQLError(out["errors"])
        return out.get("data") or {}

    def paged(self, path: str, params: dict | None = None, max_pages: int = 50) -> list:
        out = []
        for page in range(1, max_pages + 1):
            batch = self.request("GET", path, {**(params or {}), "per_page": 100, "page": page})
            out.extend(batch or [])
            if not batch or len(batch) < 100:
                break
        return out


class _Unreadable(Exception):
    def __init__(self, what: str, error: GitHubError):
        super().__init__(what)
        self.what, self.error = what, error


class GitHubBackend:
    def __init__(self, client: GitHubClient, repo: str, bot_login: str | None = None,
                 own_checks: list[str] | tuple[str, ...] = (), app_id: int | None = None):
        """`own_checks`: names of ferry's own required checks (e.g. the CI job that runs ferry apply).
        Checks posted by ferry's App (`app_id`, or `bot_login`'s slug) count as its own automatically.
        Set these from the service's environment, never from the repo: the PR could change the repo.
        """
        if repo.count("/") != 1:
            raise ValueError(f"repo must be OWNER/NAME, got {repo!r}")
        self.client = client
        owner, name = repo.split("/")
        self._owner, self._name = owner, name
        self._r = f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"
        self._bot = bot_login
        self.own_checks = {c.strip() for c in own_checks if c.strip()}
        self.app_id = app_id

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

    def _is_own_check(self, name: str, ctx: protection.Context | None, app_id: int | None) -> bool:
        if name in self.own_checks:
            return True
        if self.app_id is not None and app_id == self.app_id:
            return True  # the rule pins the check to ferry's App
        if ctx is None:
            return False
        bot = self._bot or ""
        slug = bot[: -len("[bot]")] if bot.endswith("[bot]") else None
        if self.app_id is not None and ctx.app_id == self.app_id:
            return True
        if slug and ctx.app_slug == slug:
            return True
        # GraphQL names an App's status creator by its slug (`ferry-acme`, __typename Bot); REST and
        # older fixtures use `ferry-acme[bot]`. Only a Bot may match the slug: a user can hold that login.
        if slug and ctx.creator_is_bot and ctx.creator == slug:
            return True
        return bool(bot) and ctx.creator == bot

    def _requirements(self, branch: str) -> protection.Requirements:
        """Raises _Unreadable(permission key, error) when a rule source can't be read."""
        req = protection.Requirements()
        b = urllib.parse.quote(branch, safe="")
        try:
            protection.from_rules(self.client.paged(f"{self._r}/rules/branches/{b}"), req)
            protected = bool(self.client.request("GET", f"{self._r}/branches/{b}").get("protected"))
        except GitHubError as e:
            raise _Unreadable("rules", e) from e
        if protected:
            try:
                classic = self.client.request("GET", f"{self._r}/branches/{b}/protection")
            except GitHubError as e:
                # Protected only by rulesets: no classic protection to read.
                if e.status == 404 and "not protected" in e.body.lower():
                    classic = None
                else:
                    raise _Unreadable("protection", e) from e
            protection.from_classic(classic, req)
        return req

    def merge_rule_blockers(self, cr: ChangeRequest) -> list[str]:
        """Reasons GitHub's branch protection or rulesets would refuse to merge this PR right now.

        Fails closed: if ferry can't read a rule (missing App permission, API error), that's a reason.
        """
        try:
            req = self._requirements(cr.target_branch)
        except _Unreadable as u:
            return [self._unreadable(u.what, u.error)]
        try:
            data = self.client.graphql(protection.PR_QUERY,
                                       {"owner": self._owner, "name": self._name, "number": cr.id})
        except GraphQLError as e:
            what = "checks" if any(p in ("statusCheckRollup", "contexts", "checkSuite") for p in e.paths) else "graphql"
            if e.forbidden:
                return [f"ferry can't read the PR's review/check state on GitHub: the App needs "
                        f"**{PERMISSION_FOR[what]}**"]
            return [f"ferry can't read the PR's review/check state on GitHub ({e}); refusing rather than guessing"]
        except GitHubError as e:
            return [self._unreadable("graphql", e)]
        pr = ((data.get("repository") or {}).get("pullRequest")) or {}
        if not pr:
            return ["ferry can't read the PR's review/check state on GitHub; refusing rather than guessing"]
        if pr.get("headRefOid") and pr["headRefOid"] != cr.head_sha:
            return [f"the PR head moved to `{pr['headRefOid'][:8]}` while ferry was checking; re-run for the newest commit"]
        return protection.evaluate(pr, req, self._is_own_check)

    @staticmethod
    def _unreadable(what: str, e: GitHubError) -> str:
        if e.status in (401, 403) or (e.status == 404 and what == "protection"):
            return (f"ferry can't read the target branch's {'rulesets' if what == 'rules' else 'protection rules'} "
                    f"(HTTP {e.status}): the App needs **{PERMISSION_FOR[what]}**; refusing until it can")
        return f"ferry can't read GitHub's merge rules (HTTP {e.status}); refusing rather than guessing"

    def behind_by(self, cr: ChangeRequest) -> int:
        """Commits on the base the head doesn't have: the compare API's behind_by (base...head)."""
        out = self.client.request(
            "GET", f"{self._r}/compare/{urllib.parse.quote(cr.target_branch, safe='')}...{cr.head_sha}") or {}
        behind = out.get("behind_by")
        if not isinstance(behind, int):
            raise ValueError("GitHub's compare API didn't say how far behind the PR is")
        return behind

    def contributors(self, cr: ChangeRequest) -> set[str]:
        """Logins GitHub links to the PR's commits as author or committer (web-flow is GitHub's own
        committer for web edits). GitHub lists at most 250 commits; past that, ferry can't tell."""
        commits = self.client.paged(f"{self._r}/pulls/{cr.id}/commits", max_pages=3)
        if len(commits) >= 250:
            raise ValueError("the PR has 250+ commits; GitHub doesn't list them all")
        out = set()
        for c in commits:
            for role in ("author", "committer"):
                login = (c.get(role) or {}).get("login")
                if login and login != "web-flow":
                    out.add(login)
        return out

    def set_status(self, sha: str, name: str, state: str, description: str, url: str | None = None) -> None:
        """A commit status (needs the App's "Commit statuses: write"). GitHub has no "running"."""
        data = {"state": {"running": "pending"}.get(state, state), "context": name,
                "description": description[:140]}
        if url:
            data["target_url"] = url
        self.client.request("POST", f"{self._r}/statuses/{sha}", data=data)

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

    def edit_note(self, cr_id: int, note_id: int, body: str) -> Note:
        """Replace the body of a comment ferry posted (#89). Raises GitHubError (404 if it was deleted)."""
        c = self.client.request("PATCH", f"{self._r}/issues/comments/{int(note_id)}", data={"body": body})
        return Note(c["id"], c["user"]["login"], c.get("body") or "", c.get("created_at", ""))

    def react(self, cr_id: int, note_id: int, emoji: str = "eyes") -> None:
        """A reaction on a PR comment (#89): GitHub's reaction names (`eyes`, `+1`, ...)."""
        self.client.request("POST", f"{self._r}/issues/comments/{int(note_id)}/reactions", data={"content": emoji})

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
