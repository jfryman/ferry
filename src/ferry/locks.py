"""Per-inventory apply locks, Atlantis style.

A plan takes the lock for its inventory and holds it until the MR merges or closes, so a second MR
can neither plan nor apply against the same hosts in between. A lock whose holder MR is no longer
open is stale and gets taken over, so a forgotten MR never wedges the repo.

GitLabTagLocks stores a lock as a lightweight-annotated tag `ferry-lock-<inventory>`: tag creation is
atomic on the server (a second create fails), the message carries the holder, and anyone can see who
holds the lock in the repo's tag list. Exclude these tags from pipelines (workflow rules).
That suits the CI-only GitLab driver, which has no storage of its own. A service keeps locks in its
own store instead (SQLiteLocks, a file like Atlantis's BoltDB), outside the code host.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
from contextlib import closing
from pathlib import Path
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

    def release_all(self, holder: int) -> list[str]:
        """Release every lock `holder` has; return their keys. Needs nothing but the store itself."""
        ...


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

    def release_all(self, holder):
        keys = sorted(k for k, lock in self.locks.items() if lock.holder == holder)
        for k in keys:
            del self.locks[k]
        return keys


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

    def release_all(self, holder):
        released = []
        for tag in self.client.paged(f"/projects/{self.project}/repository/tags", {"search": f"^{TAG_PREFIX}"}):
            key = tag.get("name", "")[len(TAG_PREFIX):]
            if tag.get("name", "").startswith(TAG_PREFIX) and self.release(key, holder):
                released.append(key)
        return sorted(released)


class SQLiteLocks:
    """Locks in a SQLite file the service owns, like Atlantis's BoltDB file in its data dir.

    Lives outside the code host, so locks need no repo permissions and leave nothing in the repo. A
    row per inventory; the primary key makes acquiring atomic, and BEGIN IMMEDIATE serializes
    writers across processes (a webhook handler and a CLI run on the same host). Keep the file on
    persistent storage (a Nomad host volume): losing it frees every lock. One ferry service per file.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS locks ("
                " key TEXT PRIMARY KEY, holder INTEGER NOT NULL, sha TEXT NOT NULL,"
                " user TEXT NOT NULL, since REAL NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: we issue BEGIN IMMEDIATE ourselves. timeout waits out another writer.
        return sqlite3.connect(self.path, timeout=30, isolation_level=None)

    def get(self, key):
        with closing(self._connect()) as db:
            row = db.execute("SELECT key, holder, sha, user, since FROM locks WHERE key = ?", (key,)).fetchone()
        return Lock(*row) if row else None

    def try_acquire(self, lock):
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT key, holder, sha, user, since FROM locks WHERE key = ?", (lock.key,)
                ).fetchone()
                if row:
                    db.execute("ROLLBACK")
                    return Lock(*row)
                db.execute(
                    "INSERT INTO locks (key, holder, sha, user, since) VALUES (?, ?, ?, ?, ?)",
                    (lock.key, lock.holder, lock.sha, lock.user, lock.since),
                )
                db.execute("COMMIT")
                return None
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def release(self, key, holder):
        with closing(self._connect()) as db:
            cur = db.execute("DELETE FROM locks WHERE key = ? AND holder = ?", (key, holder))
            return cur.rowcount == 1

    def release_all(self, holder):
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                keys = sorted(r[0] for r in db.execute("SELECT key FROM locks WHERE holder = ?", (holder,)))
                db.execute("DELETE FROM locks WHERE holder = ?", (holder,))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return keys

    def count(self) -> int:
        with closing(self._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM locks").fetchone()[0]


def new_lock(key: str, holder: int, sha: str, user: str) -> Lock:
    return Lock(key=key, holder=holder, sha=sha, user=user, since=time.time())
