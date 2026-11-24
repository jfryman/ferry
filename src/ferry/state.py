"""The service's own state, next to its locks: the webhook deliveries it has already seen, when
each PR head arrived (from the code host's signed pull_request events), and the last N jobs.

It lives in the SQLite file the service already owns (FERRY_LOCK_DB), so it survives a restart and
needs no extra volume. Every table is bounded: rows expire after a TTL and are capped in count.
Losing the file loses this history and frees every lock (see locks.SQLiteLocks), nothing else.

MemoryState is the same interface in memory, for tests and one-off runs.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import OrderedDict
from contextlib import closing
from pathlib import Path

DELIVERY_TTL = 7 * 24 * 3600  # GitHub and GitLab let a person redeliver for days
DELIVERY_MAX_ROWS = 20_000
HEAD_TTL = 30 * 24 * 3600
HEAD_MAX_ROWS = 20_000
JOB_HISTORY = 200
# What the job history keeps: no error text or refusal reasons (they can name internal hosts and
# paths, and the history is served unauthenticated); those are on the PR and in the service log.
JOB_FIELDS = ("id", "action", "pr", "sha", "user", "inventory", "result", "started", "seconds", "trigger", "run_id", "subject", "limit", "mode", "certificate_serial", "counts", "display_name", "playbook", "reason")


class MemoryState:
    def __init__(self, job_history: int = JOB_HISTORY):
        self.job_history = max(1, job_history)
        self._jobs: list[dict] = []
        self._next_job = 1
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._heads: dict[int, tuple[str, float]] = {}  # pr -> (head, when it became the head)
        self._pushers: dict[int, set[str]] = {}  # pr -> users ferry saw push to it (#25)

    def first_sight(self, keys: list[str], now: float) -> bool:
        """Record these delivery keys; False if any of them was seen before (a duplicate)."""
        keys = [k for k in keys if k]
        with self._lock:
            for k, at in list(self._seen.items()):
                if at >= now - DELIVERY_TTL:
                    break
                del self._seen[k]
            if any(k in self._seen for k in keys):
                return False
            for k in keys:
                self._seen[k] = now
            while len(self._seen) > DELIVERY_MAX_ROWS:
                self._seen.popitem(last=False)
            return True

    def forget(self, keys: list[str]) -> None:
        """Un-see keys whose delivery wasn't acted on (queue full), so the code host's retry counts."""
        with self._lock:
            for k in keys:
                self._seen.pop(k, None)


    def saw_head(self, pr: int, sha: str, at: float) -> None:
        """The code host said `sha` was PR `pr`'s head at `at`. Keeps when the current head *became*
        the head: an event for the same head doesn't move that time; a different head (a push, a
        force-push back to an older commit) restarts it. Older events than the record are ignored."""
        with self._lock:
            cur = self._heads.get(pr)
            if cur is None or (cur[0] != sha and at > cur[1]):
                self._heads[pr] = (sha, at)

    def head_since(self, pr: int, sha: str) -> float | None:
        """When `sha` became PR `pr`'s head, if it's the head ferry last heard of; else None."""
        with self._lock:
            cur = self._heads.get(pr)
            return cur[1] if cur and cur[0] == sha else None


    def saw_pusher(self, pr: int, user: str, at: float) -> None:
        """A webhook said `user` pushed to PR `pr`. Their approvals of it won't count."""
        if user:
            with self._lock:
                self._pushers.setdefault(pr, set()).add(user)

    def pushers(self, pr: int) -> set[str]:
        with self._lock:
            return set(self._pushers.get(pr, set()))

    def record_job(self, job: dict) -> int:
        """Append a finished job (JOB_FIELDS only), keeping the newest job_history."""
        with self._lock:
            row = {f: job.get(f) for f in JOB_FIELDS}
            row["id"] = self._next_job
            self._next_job += 1
            self._jobs.append(row)
            del self._jobs[:-self.job_history]
            return row["id"]

    def jobs(self, limit: int = 50) -> list[dict]:
        """The newest jobs first."""
        with self._lock:
            return [dict(j) for j in reversed(self._jobs[-max(0, limit):])] if limit > 0 else []


class SQLiteState(MemoryState):
    def __init__(self, path: str | Path, job_history: int = JOB_HISTORY):
        self.job_history = max(1, job_history)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("CREATE TABLE IF NOT EXISTS deliveries (key TEXT PRIMARY KEY, seen REAL NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS deliveries_seen ON deliveries (seen)")
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT,"
                       " pr INTEGER, sha TEXT, user TEXT, inventory TEXT, result TEXT, started REAL, seconds REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS heads (pr INTEGER PRIMARY KEY, sha TEXT NOT NULL, at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS pushers (pr INTEGER NOT NULL, user TEXT NOT NULL, at REAL NOT NULL,"
                       " PRIMARY KEY (pr, user))")

        with closing(self._connect()) as db:
            existing = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            for col in JOB_FIELDS[9:]:
                if col not in existing:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN "{col}" TEXT')

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: we issue BEGIN IMMEDIATE ourselves. timeout waits out another writer.
        return sqlite3.connect(self.path, timeout=30, isolation_level=None)

    def first_sight(self, keys, now):
        keys = [k for k in keys if k]
        if not keys:
            return True
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM deliveries WHERE seen < ?", (now - DELIVERY_TTL,))
                marks = ",".join("?" * len(keys))
                if db.execute(f"SELECT 1 FROM deliveries WHERE key IN ({marks}) LIMIT 1", keys).fetchone():
                    db.execute("ROLLBACK")
                    return False
                db.executemany("INSERT INTO deliveries (key, seen) VALUES (?, ?)", [(k, now) for k in keys])
                db.execute("DELETE FROM deliveries WHERE key IN (SELECT key FROM deliveries"
                           " ORDER BY seen DESC LIMIT -1 OFFSET ?)", (DELIVERY_MAX_ROWS,))
                db.execute("COMMIT")
                return True
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def forget(self, keys):
        with closing(self._connect()) as db:
            db.executemany("DELETE FROM deliveries WHERE key = ?", [(k,) for k in keys if k])

    def saw_head(self, pr, sha, at):
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT sha, at FROM heads WHERE pr = ?", (pr,)).fetchone()
                if row is None or (row[0] != sha and at > row[1]):
                    db.execute("INSERT OR REPLACE INTO heads (pr, sha, at) VALUES (?, ?, ?)", (pr, sha, at))
                db.execute("DELETE FROM heads WHERE at < ?", (at - HEAD_TTL,))
                db.execute("DELETE FROM heads WHERE pr IN (SELECT pr FROM heads ORDER BY at DESC LIMIT -1 OFFSET ?)",
                           (HEAD_MAX_ROWS,))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def head_since(self, pr, sha):
        with closing(self._connect()) as db:
            row = db.execute("SELECT at FROM heads WHERE pr = ? AND sha = ?", (pr, sha)).fetchone()
        return row[0] if row else None

    def saw_pusher(self, pr, user, at):
        if not user:
            return
        with closing(self._connect()) as db:
            db.execute("INSERT OR REPLACE INTO pushers (pr, user, at) VALUES (?, ?, ?)", (pr, user, at))
            db.execute("DELETE FROM pushers WHERE at < ?", (at - HEAD_TTL,))

    def pushers(self, pr):
        with closing(self._connect()) as db:
            return {r[0] for r in db.execute("SELECT user FROM pushers WHERE pr = ?", (pr,))}

    def record_job(self, job):
        cols = JOB_FIELDS[1:]
        with closing(self._connect()) as db:
            cur = db.execute(f"INSERT INTO jobs ({', '.join(chr(34) + c + chr(34) for c in cols)}) VALUES ({', '.join('?' * len(cols))})",
                             [json.dumps(job.get(c)) if c == "counts" and job.get(c) is not None else job.get(c) for c in cols])
            db.execute("DELETE FROM jobs WHERE id <= ?", (cur.lastrowid - self.job_history,))
            return cur.lastrowid

    def jobs(self, limit=50):
        if limit <= 0:
            return []
        with closing(self._connect()) as db:
            rows = db.execute(f"SELECT {', '.join(chr(34) + c + chr(34) for c in JOB_FIELDS)} FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        jobs = [dict(zip(JOB_FIELDS, r)) for r in rows]
        for job in jobs:
            if job.get('counts') is not None:
                job['counts'] = json.loads(job['counts'])
        return jobs
