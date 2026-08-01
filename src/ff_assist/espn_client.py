"""Thin, read-only wrapper around ``espn-api``.

Deliberately has no write path. Per the build plan, we never set lineups or
submit claims programmatically — the system's job is to make the call obvious,
not to make it for you.
"""

from __future__ import annotations

import requests
from espn_api.football import League
from espn_api.requests.espn_requests import (
    ESPNAccessDenied,
    ESPNInvalidLeague,
    ESPNUnknownError,
)

from .config import LeagueConfig, Settings, get_settings

__all__ = [
    "CookieExpired",
    "ESPNError",
    "ESPNUnreachable",
    "LeagueNotFound",
    "get_league",
    "get_all_leagues",
    "my_team",
]


class ESPNError(RuntimeError):
    """Base class for anything that went wrong talking to ESPN."""


class CookieExpired(ESPNError):
    """ESPN rejected our cookies. Time to re-pull espn_s2 and SWID."""


class LeagueNotFound(ESPNError):
    """The league ID is wrong, or this account can't see that league/season."""


class ESPNUnreachable(ESPNError):
    """Network-level failure — DNS, TLS, proxy, timeout, or ESPN being down."""


def get_league(
    league: LeagueConfig | str,
    settings: Settings | None = None,
    year: int | None = None,
) -> League:
    """Build an authenticated :class:`espn_api.football.League`."""
    settings = settings or get_settings()
    cfg = settings.league(league) if isinstance(league, str) else league
    season = year or settings.season

    try:
        return League(
            league_id=cfg.league_id,
            year=season,
            espn_s2=settings.espn_s2,
            swid=settings.swid,
        )
    except ESPNAccessDenied as exc:
        raise CookieExpired(
            f"ESPN denied access to league {cfg.league_id} ({cfg.display_name}).\n"
            "  Most likely your espn_s2 / SWID cookies expired.\n"
            "  Re-pull them from DevTools and update .env, then re-run."
        ) from exc
    except ESPNInvalidLeague as exc:
        raise LeagueNotFound(
            f"ESPN says league {cfg.league_id} ({cfg.display_name}) doesn't exist for {season}.\n"
            "  Check FF_LEAGUE_*_ID and FF_SEASON in .env. Note that a league's ID\n"
            "  stays the same year to year, but the season must be one you played."
        ) from exc
    except ESPNUnknownError as exc:
        raise ESPNError(
            f"ESPN returned an unexpected error for league {cfg.league_id}: {exc}\n"
            "  If this persists, the unofficial v3 API may have changed — check for an\n"
            "  espn-api release before assuming your config is wrong."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise ESPNUnreachable(
            f"Couldn't reach ESPN for league {cfg.league_id} ({cfg.display_name}).\n"
            f"  {type(exc).__name__}: {exc}\n"
            "  This is a network/ESPN-availability problem, not a config problem."
        ) from exc


def get_all_leagues(settings: Settings | None = None, year: int | None = None) -> dict[str, League]:
    """Load every configured league. Raises on the first failure."""
    settings = settings or get_settings()
    return {cfg.key: get_league(cfg, settings=settings, year=year) for cfg in settings.leagues}


def my_team(lg: League, cfg: LeagueConfig):
    """Return the configured team, or ``None`` if TEAM_ID isn't set."""
    if cfg.team_id is None:
        return None
    for team in lg.teams:
        if team.team_id == cfg.team_id:
            return team
    return None
