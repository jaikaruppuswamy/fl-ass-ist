"""Warm nflverse frames, refreshed daily.

Every DvP or usage call needs the same season-long frames. Fetching them per
request would put a multi-second stall in front of the Sunday brief, so they
are loaded once at boot and refreshed on a timer.

Measured footprint with schedules + player stats + snap counts all resident:
~126MB RSS, of which the frames themselves are ~19MB. A 512MB instance has
plenty of headroom.

Season handling matters more than it looks. nflverse 404s a season that has not
kicked off, so between February and September the current season simply does
not exist. The store resolves that once, at warm time, and every caller gets a
consistent answer rather than each rediscovering it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import polars as pl

__all__ = ["DataStore", "get_store", "set_store"]

log = logging.getLogger("ff_assist.datastore")

DEFAULT_REFRESH_SECONDS = 24 * 3600

#: Projections are not like schedules. A Thursday number is stale by Sunday
#: morning, and a stale projection is a wrong answer rather than a slow one.
SLEEPER_REFRESH_SECONDS = 6 * 3600


class DataStore:
    """Thread-safe, lazily-refreshed nflverse frames."""

    def __init__(
        self,
        season: int,
        *,
        refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
        loader: Any = None,
    ) -> None:
        self.season = season
        self.refresh_seconds = refresh_seconds
        self._loader = loader
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}
        #: The season that actually has player data — self.season once the year
        #: is under way, otherwise the one before it.
        self.stats_season: int | None = None

    # -- internals ----------------------------------------------------------

    @property
    def nfl(self) -> Any:
        if self._loader is None:
            import nflreadpy

            self._loader = nflreadpy
        return self._loader

    def _fresh(self, key: str, ttl: int | None = None) -> Any | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at > (ttl or self.refresh_seconds):
            return None
        return value

    def _get(self, key: str, produce: Callable[[], Any], *, ttl: int | None = None) -> Any:
        hit = self._fresh(key, ttl)
        if hit is not None:
            return hit
        with self._lock:
            hit = self._fresh(key, ttl)  # another thread may have won the race
            if hit is not None:
                return hit
            value = produce()
            self._cache[key] = (time.monotonic(), value)
            return value

    # -- frames -------------------------------------------------------------

    def schedules(self) -> pl.DataFrame:
        return self._get("schedules", lambda: self.nfl.load_schedules())

    def player_stats(self, season: int | None = None) -> pl.DataFrame:
        season = season or self.resolve_stats_season()
        return self._get(f"player_stats:{season}", lambda: self.nfl.load_player_stats([season]))

    def snap_counts(self, season: int | None = None) -> pl.DataFrame | None:
        season = season or self.resolve_stats_season()

        def produce() -> pl.DataFrame | None:
            try:
                return self.nfl.load_snap_counts([season])
            except Exception:  # noqa: BLE001 — snap share is a bonus column
                return None

        return self._get(f"snap_counts:{season}", produce)

    def sleeper_projections(self, season: int, week: int) -> Any:
        """Sleeper's projected stat lines for one week.

        Not an nflverse frame, but it belongs here for the same reason the
        others do: every player row in a slate wants it, refetching per request
        would put a network round trip in front of the Sunday brief, and this
        class already has the TTL and the lock.

        Cached for six hours rather than the usual day. Projections move during
        the week — a Thursday number is stale by Sunday morning, and staleness
        here is not a slower answer but a wrong one.
        """
        from .projections import fetch_week

        return self._get(
            f"sleeper:{season}:{week}",
            lambda: fetch_week(season, week),
            ttl=SLEEPER_REFRESH_SECONDS,
        )

    # -- season resolution --------------------------------------------------

    def resolve_stats_season(self) -> int:
        """The most recent season with player data.

        Between February and September the current season 404s; falling back
        one year is the difference between "no usage data" and "last year's
        usage data", and the latter is the only signal that exists in September.
        """
        if self.stats_season is not None:
            return self.stats_season
        for candidate in (self.season, self.season - 1):
            try:
                self.nfl.load_player_stats([candidate])
            except Exception:  # noqa: BLE001
                continue
            self.stats_season = candidate
            return candidate
        # Nothing reachable; report the current season and let callers surface
        # the failure rather than silently pretending it is last year.
        self.stats_season = self.season
        return self.season

    # -- lifecycle ----------------------------------------------------------

    def warm(self) -> dict[str, Any]:
        """Preload everything. Called once at boot so the first real request
        is not the one that pays for the download."""
        started = time.monotonic()
        report: dict[str, Any] = {"season": self.season}
        try:
            self.schedules()
            report["schedules"] = "ok"
        except Exception as exc:  # noqa: BLE001
            report["schedules"] = f"failed: {type(exc).__name__}"

        try:
            season = self.resolve_stats_season()
            report["stats_season"] = season
            self.player_stats(season)
            report["player_stats"] = "ok"
            report["snap_counts"] = "ok" if self.snap_counts(season) is not None else "unavailable"
        except Exception as exc:  # noqa: BLE001
            report["player_stats"] = f"failed: {type(exc).__name__}"

        report["seconds"] = round(time.monotonic() - started, 2)
        log.info("datastore warm: %s", report)
        return report

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "season": self.season,
            "stats_season": self.stats_season,
            "refresh_seconds": self.refresh_seconds,
            "entries": {
                key: {"age_seconds": round(now - at)} for key, (at, _v) in self._cache.items()
            },
        }


_store: DataStore | None = None


def get_store(season: int | None = None) -> DataStore | None:
    """The process-wide store, if one has been installed.

    Returns None when running without one (tests, the CLI harness), and every
    caller falls back to loading frames directly — slower, but never wrong.
    """
    global _store
    if _store is None and season is not None:
        _store = DataStore(season)
    return _store


def set_store(store: DataStore | None) -> None:
    global _store
    _store = store
