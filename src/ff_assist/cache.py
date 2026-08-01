"""SQLite TTL cache.

ESPN publishes no rate limits but will throttle, and a Sunday brief that pulls
three leagues several times over an hour has no business re-fetching rosters
each time. Everything cached here is regenerable — deleting the file is always
safe, and :data:`TTL` documents how stale each data class is allowed to get.

SQLite rather than DuckDB on purpose: this is a key/value problem with
expiries, which SQLite does well and concurrently. DuckDB arrives in Phase 2
for the nflverse analytical joins, where columnar scans actually earn it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["TTL", "Cache"]

#: Seconds, by data class. Tuned to how fast each source actually changes.
TTL: dict[str, int] = {
    "settings": 24 * 3600,  # scoring rules change ~never mid-season
    "roster": 15 * 60,  # the plan's number; enough for waiver churn
    "matchup": 15 * 60,
    "free_agents": 15 * 60,
    "player_news": 10 * 60,  # injury news is the one thing worth chasing
    "schedule": 7 * 24 * 3600,
    "nflverse": 24 * 3600,
    "weather": 3 * 3600,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    data_class TEXT NOT NULL,
    stored_at  REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at);
"""


class Cache:
    """Tiny JSON TTL cache. Never raises on a cache problem — a broken cache
    degrades to a cache miss, because a stale-data bug on Sunday morning is
    much worse than an extra ESPN round trip."""

    def __init__(self, path: str | Path, *, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        if self.enabled:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._conn = sqlite3.connect(self.path, check_same_thread=False)
                self._conn.executescript(_SCHEMA)
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.commit()
            except (sqlite3.Error, OSError):
                # An unwritable cache directory must degrade to "no cache",
                # never take the server down on startup.
                self._conn = None
                self.enabled = False

    # -- core ---------------------------------------------------------------

    def get(self, key: str) -> Any | None:
        """Return the cached value, or None if absent or expired."""
        if not self._conn:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
                ).fetchone()
            if row is None or row[1] < time.time():
                return None
            return json.loads(row[0])
        except (sqlite3.Error, json.JSONDecodeError):
            return None

    def set(self, key: str, value: Any, data_class: str = "roster") -> None:
        if not self._conn:
            return
        ttl = TTL.get(data_class, 900)
        now = time.time()
        try:
            payload = json.dumps(value, default=str)
        except (TypeError, ValueError):
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?, ?)",
                    (key, payload, data_class, now, now + ttl),
                )
                self._conn.commit()
        except sqlite3.Error:
            pass

    def get_or_set(self, key: str, data_class: str, producer: Callable[[], Any]) -> Any:
        """Return the cached value, else call ``producer`` and cache its result.

        A producer exception propagates and nothing is cached — we never want
        an error object memoized for 15 minutes.
        """
        hit = self.get(key)
        if hit is not None:
            return hit
        value = producer()
        if value is not None:
            self.set(key, value, data_class)
        return value

    # -- maintenance --------------------------------------------------------

    def invalidate(self, prefix: str = "") -> int:
        """Drop keys starting with ``prefix`` (all keys if empty). Returns count."""
        if not self._conn:
            return 0
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM cache WHERE key LIKE ?", (f"{prefix}%",)
                )
                self._conn.commit()
                return cur.rowcount
        except sqlite3.Error:
            return 0

    def purge_expired(self) -> int:
        if not self._conn:
            return 0
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM cache WHERE expires_at < ?", (time.time(),)
                )
                self._conn.commit()
                return cur.rowcount
        except sqlite3.Error:
            return 0

    def stats(self) -> dict[str, Any]:
        if not self._conn:
            return {"enabled": False}
        try:
            with self._lock:
                total, live = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(expires_at > ?), 0) FROM cache",
                    (time.time(),),
                ).fetchone()
            return {
                "enabled": True,
                "path": str(self.path),
                "entries": total,
                "live": live,
                "expired": total - live,
            }
        except sqlite3.Error:
            return {"enabled": False}

    def close(self) -> None:
        if self._conn:
            with self._lock:
                self._conn.close()
            self._conn = None

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def key_for(*parts: Any) -> str:
    """Build a cache key. Keep league key first so invalidate('main:') works."""
    return ":".join(str(p) for p in parts if p is not None)
