"""Per-inventory apply locks, Atlantis style.

A plan takes the lock for its inventory and holds it until the MR merges or closes, so a second MR
can neither plan nor apply against the same hosts in between. A lock whose holder MR is no longer
open is stale and gets taken over, so a forgotten MR never wedges the repo.

GitLabTagLocks (and GitHubTagLocks, the same on GitHub) stores a lock as a lightweight-annotated tag `ferry-lock-<inventory>`: tag creation is
atomic on the server (a second create fails), the message carries the holder, and anyone can see who
holds the lock in the repo's tag list. Exclude these tags from pipelines (workflow rules).
A service deployment can swap in a database-backed LockStore without touching the engine.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Protocol

from .vcs.github import GitHubClient, GitHubError
from .vcs.gitlab import GitLabClient, GitLabError

TAG_PREFIX = "ferry-lock-"


@dataclass
class Lock:
    key: str
    holder: int  # change request id
    sha: str
    user: str
    since: float


class LockStore(Protocol):
    def get(self, key: str) -> Lock | None: ...

    def try_acquire(self, lock: Lock) -> Lock | None:
        """Take the lock; return None on success or the current holder's Lock on conflict."""
        ...

    def release(self, key: str, holder: int) -> bool: ...


class MemoryLocks:
    def __init__(self):
        self.locks: dict[str, Lock] = {}

    def get(self, key):
        return self.locks.get(key)

    def try_acquire(self, lock):
        cur = self.locks.get(lock.key)
        if cur is not None:
            return cur
        self.locks[lock.key] = lock
        return None

    def release(self, key, holder):
        cur = self.locks.get(key)
        if cur is None or cur.holder != holder:
            return False
        del self.locks[key]
        return True


class GitLabTagLocks:
    def __init__(self, client: GitLabClient, project: str | int):
        self.client = client
        self.project = urllib.parse.quote(str(project), safe="")

    def _tag(self, key: str) -> str:
        return f"{TAG_PREFIX}{key}"

    def _path(self, key: str) -> str:
        return f"/projects/{self.project}/repository/tags/{urllib.parse.quote(self._tag(key), safe='')}"

    def get(self, key):
        try:
            tag = self.client.request("GET", self._path(key))
        except GitLabError as e:
            if e.status == 404:
                return None
            raise
        try:
            data = json.loads(tag.get("message") or "{}")
            return Lock(**data)
        except (TypeError, ValueError):
            # Unreadable lock: treat as held by nobody we know (id 0), never silently free.
            return Lock(key=key, holder=0, sha=tag.get("target", ""), user="?", since=0)

    def try_acquire(self, lock):
        try:
            self.client.request(
                "POST",
                f"/projects/{self.project}/repository/tags",
                data={"tag_name": self._tag(lock.key), "ref": lock.sha, "message": json.dumps(asdict(lock))},
            )
            return None
        except GitLabError as e:
            if e.status in (400, 409):  # already exists
                cur = self.get(lock.key)
                if cur is not None:
                    return cur
            raise

    def release(self, key, holder):
        cur = self.get(key)
        if cur is None or cur.holder != holder:
            return False
        self.client.request("DELETE", self._path(key))
        return True


class GitHubTagLocks:
    """Same idea as GitLabTagLocks on GitHub: an annotated tag `ferry-lock-<inventory>`.

    GitHub makes a tag in two steps: a tag object (carrying the holder as its message), then the ref
    refs/tags/ferry-lock-<inventory> pointing at it. Creating the ref is the atomic step (422 if it
    exists); a tag object left behind by a lost race is unreferenced and harmless.
    """

    def __init__(self, client: GitHubClient, repo: str):
        self.client = client
        owner, name = repo.split("/")
        self._r = f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"

    def _ref(self, key: str) -> str:
        return f"tags/{TAG_PREFIX}{key}"

    def get(self, key):
        try:
            ref = self.client.request("GET", f"{self._r}/git/ref/{self._ref(key)}")
        except GitHubError as e:
            if e.status == 404:
                return None
            raise
        obj = ref.get("object", {})
        message, target = "", obj.get("sha", "")
        if obj.get("type") == "tag":
            tag = self.client.request("GET", f"{self._r}/git/tags/{obj['sha']}")
            message, target = tag.get("message", ""), tag.get("object", {}).get("sha", target)
        try:
            return Lock(**json.loads(message or "{}"))
        except (TypeError, ValueError):
            # Unreadable lock: treat as held by nobody we know (id 0), never silently free.
            return Lock(key=key, holder=0, sha=target, user="?", since=0)

    def try_acquire(self, lock):
        tag = self.client.request(
            "POST",
            f"{self._r}/git/tags",
            data={"tag": f"{TAG_PREFIX}{lock.key}", "message": json.dumps(asdict(lock)), "object": lock.sha,
                  "type": "commit"},
        )
        try:
            self.client.request(
                "POST", f"{self._r}/git/refs", data={"ref": f"refs/{self._ref(lock.key)}", "sha": tag["sha"]}
            )
            return None
        except GitHubError as e:
            if e.status == 422:  # Reference already exists
                cur = self.get(lock.key)
                if cur is not None:
                    return cur
            raise

    def release(self, key, holder):
        cur = self.get(key)
        if cur is None or cur.holder != holder:
            return False
        self.client.request("DELETE", f"{self._r}/git/refs/{self._ref(key)}")
        return True


def new_lock(key: str, holder: int, sha: str, user: str) -> Lock:
    return Lock(key=key, holder=holder, sha=sha, user=user, since=time.time())
