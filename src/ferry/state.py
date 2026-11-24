"""The service's own state, next to its locks: the webhook deliveries it has already seen.

It lives in the SQLite file the service already owns (FERRY_LOCK_DB), so it survives a restart and
needs no extra volume. Every table is bounded: rows expire after a TTL and are capped in count.
Losing the file loses this history and frees every lock (see locks.SQLiteLocks), nothing else.

MemoryState is the same interface in memory, for tests and one-off runs.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import OrderedDict
from contextlib import closing
from pathlib import Path

DELIVERY_TTL = 7 * 24 * 3600  # GitHub and GitLab let a person redeliver for days
DELIVERY_MAX_ROWS = 20_000


class MemoryState:
    def __init__(self):
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, float] = OrderedDict()

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


class SQLiteState(MemoryState):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("CREATE TABLE IF NOT EXISTS deliveries (key TEXT PRIMARY KEY, seen REAL NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS deliveries_seen ON deliveries (seen)")

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
