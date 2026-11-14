"""Per-inventory apply locks, Atlantis style.

A plan takes the lock for its inventory and holds it until the MR merges or closes, so a second MR
can neither plan nor apply against the same hosts in between. A lock whose holder MR is no longer
open is stale and gets taken over, so a forgotten MR never wedges the repo.

GitLabTagLocks stores a lock as a lightweight-annotated tag `ferry-lock-<inventory>`: tag creation is
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


def new_lock(key: str, holder: int, sha: str, user: str) -> Lock:
    return Lock(key=key, holder=holder, sha=sha, user=user, since=time.time())
