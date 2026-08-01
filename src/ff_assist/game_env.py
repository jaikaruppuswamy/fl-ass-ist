"""Game environment: implied team totals, spreads, venue.

The plan calls the Vegas implied team total "the highest-signal single variable
in the whole system", and it costs nothing — nflverse ships ``spread_line`` and
``total_line`` on the schedule, published for the whole season before Week 1.

Two derived numbers matter:

    implied_home = total/2 + spread/2
    implied_away = total/2 - spread/2

A back on a team implied for 27 points is in a different game script from one
implied for 16, and that gap moves projections more than any matchup stat.

Sign convention is verified, not assumed: against 2025's completed games,
``corr(spread_line, home_margin) = +0.506`` and the mean of
``actual_margin - spread_line`` is 0.61, so ``spread_line`` is from the HOME
team's perspective and is close to unbiased. See tests/test_game_env.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import polars as pl

__all__ = [
    "ESPN_TO_NFLVERSE",
    "GameEnvironment",
    "load_week_environment",
    "team_environment_map",
    "to_nflverse_team",
]

# ESPN and nflverse disagree on exactly two team codes. Left unbridged, the
# Rams and Commanders silently vanish from every environment lookup — no error,
# just two teams' worth of players quietly missing their implied total. A test
# asserts all 32 ESPN codes resolve.
ESPN_TO_NFLVERSE: dict[str, str] = {"LAR": "LA", "WSH": "WAS"}
NFLVERSE_TO_ESPN: dict[str, str] = {v: k for k, v in ESPN_TO_NFLVERSE.items()}


def to_nflverse_team(espn_code: str | None) -> str | None:
    if not espn_code:
        return None
    return ESPN_TO_NFLVERSE.get(espn_code, espn_code)


def to_espn_team(nflverse_code: str | None) -> str | None:
    if not nflverse_code:
        return None
    return NFLVERSE_TO_ESPN.get(nflverse_code, nflverse_code)


# ``roof`` in nflverse is not purely architectural — it doubles as a
# weather-availability flag, and every 'dome' and 'closed' game has null
# temp/wind. Observed values and what they actually mean for us:
#
#   outdoors  open air, weather applies
#   dome      fixed roof; ALSO applied to some international venues that are
#             in fact open air, so never gate weather on this alone — see
#             weather.py's venue override table
#   closed    a retractable roof that was shut. Only known after the game.
#   null      a retractable-roof stadium whose state is not yet decided:
#             ARI, DAL, IND, HOU, ATL. Pre-game this is the honest answer,
#             and it means "weather might matter, or might not".
ROOF_WEATHER_APPLIES = {"outdoors": True, "dome": False, "closed": False}


@dataclass(frozen=True)
class GameEnvironment:
    game_id: str
    week: int
    home: str
    away: str
    spread_line: float | None
    total_line: float | None
    implied_home: float | None
    implied_away: float | None
    roof: str | None
    stadium: str | None
    gameday: str | None
    gametime: str | None

    def implied_for(self, team: str) -> float | None:
        team = to_nflverse_team(team) or team
        if team == self.home:
            return self.implied_home
        if team == self.away:
            return self.implied_away
        return None

    def opponent_of(self, team: str) -> str | None:
        team = to_nflverse_team(team) or team
        if team == self.home:
            return self.away
        if team == self.away:
            return self.home
        return None

    @property
    def weather_relevant(self) -> bool | None:
        """True outdoors, False under a roof, None when the roof state is
        genuinely unknown (a retractable that hasn't been called yet)."""
        if self.roof is None:
            return None
        return ROOF_WEATHER_APPLIES.get(self.roof)


def implied_totals(spread_line: float | None, total_line: float | None) -> tuple[float | None, float | None]:
    """(home, away) implied team totals. ``spread_line`` is home-perspective."""
    if spread_line is None or total_line is None:
        return (None, None)
    half = total_line / 2.0
    edge = spread_line / 2.0
    return (round(half + edge, 2), round(half - edge, 2))


def load_week_environment(
    season: int, week: int | None = None, schedules: pl.DataFrame | None = None
) -> list[GameEnvironment]:
    """Every game in a week, with implied totals attached.

    ``schedules`` is injectable so tests never touch the network.
    """
    if schedules is None:
        import nflreadpy as nfl

        schedules = nfl.load_schedules()

    frame = schedules.filter(pl.col("season") == season)
    if week is not None:
        frame = frame.filter(pl.col("week") == week)

    out: list[GameEnvironment] = []
    for row in frame.iter_rows(named=True):
        home_implied, away_implied = implied_totals(row.get("spread_line"), row.get("total_line"))
        out.append(
            GameEnvironment(
                game_id=row.get("game_id"),
                week=row.get("week"),
                home=row.get("home_team"),
                away=row.get("away_team"),
                spread_line=row.get("spread_line"),
                total_line=row.get("total_line"),
                implied_home=home_implied,
                implied_away=away_implied,
                roof=row.get("roof"),
                stadium=row.get("stadium"),
                gameday=str(row.get("gameday")) if row.get("gameday") else None,
                gametime=row.get("gametime"),
            )
        )
    return out


def team_environment_map(
    season: int, week: int, schedules: pl.DataFrame | None = None
) -> dict[str, dict[str, Any]]:
    """ESPN team code -> that team's environment for the week.

    Keyed by ESPN codes because that is what the roster data speaks. Teams on
    bye are simply absent, which callers should treat as "on bye", not as an
    error.
    """
    games = load_week_environment(season, week, schedules)
    out: dict[str, dict[str, Any]] = {}
    for g in games:
        for team, implied, opp, home in (
            (g.home, g.implied_home, g.away, True),
            (g.away, g.implied_away, g.home, False),
        ):
            espn_code = to_espn_team(team)
            if espn_code is None:
                continue
            spread = g.spread_line if home else (-g.spread_line if g.spread_line is not None else None)
            out[espn_code] = {
                "opp": to_espn_team(opp),
                "home": home,
                "implied_total": implied,
                "game_total": g.total_line,
                "spread": spread,
                "roof": g.roof,
                "stadium": g.stadium,
                "weather_relevant": g.weather_relevant,
                "kickoff": f"{g.gameday} {g.gametime}" if g.gameday else None,
            }
    return out
