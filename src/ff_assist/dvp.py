"""Defense vs. position, expressed in each league's own scoring.

Vendors publish DvP in generic PPR. That is the wrong denominator for a league
that scores a point per 10 rushing yards, or half a point per reception — the
ranking genuinely reorders. Computing it ourselves is the only way to express
"which defence is soft against RBs" in the scoring that will actually pay out,
which is what the plan means by not buying this.

The work is a translation problem. nflverse reports stats under readable column
names; ScoringSettings speaks ESPN's numeric stat ids. Two wrinkles:

* Bucket categories (RY10, REY25...) are not in nflverse at all — ESPN derives
  them. We synthesise them as ``floor(yards / N)``, which was verified against
  a real ESPN payload: 67 rushing yards arrives from ESPN as
  ``{'27': 13, '28': 6, '29': 3, '30': 2, '31': 1}``.
* Correctness is checked against two independent sources — nflverse's own
  ``fantasy_points_ppr`` for a standard league, and ESPN's applied points for
  a real 2025 game in a bucket-scoring league. See tests/test_dvp.py.
"""

from __future__ import annotations

import math
from typing import Any

import polars as pl

from .scoring import ScoringSettings

__all__ = [
    "NFLVERSE_TO_ESPN_STAT",
    "to_espn_stat_line",
    "score_nflverse_row",
    "defense_vs_position",
]

#: nflverse weekly column -> ESPN stat id.
NFLVERSE_TO_ESPN_STAT: dict[str, int] = {
    # passing
    "attempts": 0,
    "completions": 1,
    "passing_yards": 3,
    "passing_tds": 4,
    "passing_interceptions": 20,
    "passing_2pt_conversions": 19,
    # rushing
    "carries": 23,
    "rushing_yards": 24,
    "rushing_tds": 25,
    "rushing_2pt_conversions": 26,
    # receiving
    "receptions": 53,
    "targets": 58,
    "receiving_yards": 42,
    "receiving_tds": 43,
    "receiving_2pt_conversions": 44,
}

# NOTE: `special_teams_tds` is deliberately absent above. nflverse combines
# kickoff-return and punt-return touchdowns into one column, while ESPN scores
# them as separate categories (101 KRTD, 102 PRTD) — so the stat line genuinely
# cannot say which one happened. Mapping it to either id would be a guess that
# scores correctly only because leagues almost always price them the same.
# _special_teams_td_points() below resolves it against the league's own rules.

#: Fumbles lost arrive split across three columns; ESPN scores the total (72).
_FUMBLE_COLUMNS = ("rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost")

#: base column -> {espn bucket stat id: units per bucket}. "Every N" categories.
#: Note 10/32/52 are 'Every 100 yards' — buckets, NOT one-off game bonuses.
#: Those are _MILESTONES below, and conflating the two double-counts a big game.
_BUCKETS: dict[str, dict[int, int]] = {
    "passing_yards": {5: 5, 6: 10, 7: 20, 8: 25, 9: 50, 10: 100},
    "rushing_yards": {27: 5, 28: 10, 29: 20, 30: 25, 31: 50, 32: 100},
    "receiving_yards": {47: 5, 48: 10, 49: 20, 50: 25, 51: 50, 52: 100},
    "completions": {11: 5, 12: 10},
    "receptions": {54: 5, 55: 10},
    "carries": {33: 5, 34: 10},
}

#: base column -> [(espn stat id, lower, upper)] for "N-yard game" bonuses.
#: These are RANGES, not thresholds — ESPN's own labels say so ("100-199 yard
#: receiving game" vs "200+ yard receiving game"), so a 210-yard game scores
#: the 200+ bonus and NOT the 100-199 one.
_MILESTONES: dict[str, list[tuple[int, float, float]]] = {
    "passing_yards": [(17, 300, 400), (18, 400, float("inf"))],
    "rushing_yards": [(37, 100, 200), (38, 200, float("inf"))],
    "receiving_yards": [(56, 100, 200), (57, 200, float("inf"))],
}

#: Fantasy-relevant positions. Everything else is defensive/special teams noise
#: for our purposes.
SKILL_POSITIONS = ("QB", "RB", "WR", "TE")


def to_espn_stat_line(row: dict[str, Any]) -> dict[int, float]:
    """Translate one nflverse weekly row into an ESPN stat-id stat line.

    Includes the bucket and milestone categories ESPN derives internally, so a
    bucket-scoring league scores correctly rather than reading as all zeros.
    """
    line: dict[int, float] = {}

    for column, stat_id in NFLVERSE_TO_ESPN_STAT.items():
        value = row.get(column)
        if value:
            line[stat_id] = float(value)

    fumbles = sum(float(row.get(c) or 0) for c in _FUMBLE_COLUMNS)
    if fumbles:
        line[72] = fumbles

    for column, buckets in _BUCKETS.items():
        base = row.get(column)
        if not base:
            continue
        for stat_id, per in buckets.items():
            count = math.floor(float(base) / per)
            if count:
                line[stat_id] = float(count)

    for column, milestones in _MILESTONES.items():
        base = float(row.get(column) or 0)
        for stat_id, lower, upper in milestones:
            if lower <= base < upper:
                line[stat_id] = 1.0

    return line


def _special_teams_td_points(scoring: ScoringSettings) -> float:
    """Points for one return touchdown of unknown type.

    Preference order: 'Total Return TD' (105) is an exact match for nflverse's
    combined column. Failing that, fall back to the kickoff/punt return rules —
    they are priced identically in every league seen so far, and taking the
    lower of the two keeps an unknowable case from inflating a projection.
    """
    total = scoring.rules.get(105)
    if total and total.points:
        return total.points
    priced = [
        r.points for r in (scoring.rules.get(101), scoring.rules.get(102)) if r and r.points
    ]
    return min(priced) if priced else 0.0


def score_nflverse_row(row: dict[str, Any], scoring: ScoringSettings) -> float:
    """Fantasy points for one nflverse player-week in a league's own scoring."""
    points = scoring.score(to_espn_stat_line(row), row.get("position")).points
    returns = float(row.get("special_teams_tds") or 0)
    if returns:
        points += returns * _special_teams_td_points(scoring)
    return round(points, 2)


def defense_vs_position(
    scoring: ScoringSettings,
    season: int,
    *,
    window: int | None = 4,
    through_week: int | None = None,
    positions: tuple[str, ...] = SKILL_POSITIONS,
    stats: pl.DataFrame | None = None,
) -> dict[str, Any]:
    """Points allowed per game to each position, by defence, in this scoring.

    ``window`` limits to the most recent N weeks (None for season-long). The
    plan wants both: a rolling 4-week view catches a defence that has lost its
    best corner, the season view is less noisy.
    """
    if stats is None:
        import nflreadpy as nfl

        stats = nfl.load_player_stats([season])

    frame = stats
    if "season_type" in frame.columns:
        frame = frame.filter(pl.col("season_type") == "REG")
    frame = frame.filter(pl.col("position").is_in(list(positions)))
    frame = frame.filter(pl.col("opponent_team").is_not_null())

    if through_week is not None:
        frame = frame.filter(pl.col("week") <= through_week)
    if window is not None and frame.height:
        latest = frame["week"].max()
        frame = frame.filter(pl.col("week") > latest - window)

    if not frame.height:
        return {"season": season, "window": window, "error": "no data for this window"}

    weeks_covered = sorted(frame["week"].unique().to_list())

    # Score every player-week once, then aggregate.
    scored: dict[tuple[str, str, int], float] = {}
    for row in frame.iter_rows(named=True):
        key = (row["opponent_team"], row["position"], row["week"])
        scored[key] = scored.get(key, 0.0) + score_nflverse_row(row, scoring)

    per_defense: dict[str, dict[str, list[float]]] = {}
    for (defense, position, _week), points in scored.items():
        per_defense.setdefault(defense, {}).setdefault(position, []).append(points)

    table: dict[str, dict[str, float]] = {}
    for defense, by_pos in per_defense.items():
        table[defense] = {
            pos: round(sum(vals) / len(vals), 2) for pos, vals in by_pos.items() if vals
        }

    # Rank 1 = softest (most points allowed), which is how DvP is normally read.
    ranks: dict[str, dict[str, int]] = {}
    for position in positions:
        ordered = sorted(
            (d for d in table if position in table[d]),
            key=lambda d: -table[d][position],
        )
        for rank, defense in enumerate(ordered, start=1):
            ranks.setdefault(defense, {})[position] = rank

    return {
        "season": season,
        "scoring": scoring.format_label(),
        "window": window,
        "weeks": weeks_covered,
        "points_allowed_per_game": table,
        "rank": ranks,
        "note": "rank 1 = most points allowed to that position (softest matchup)",
    }


def dvp_for_team(dvp: dict[str, Any], defense: str, position: str) -> dict[str, Any] | None:
    """Pull one cell out of a DvP table, for attaching to a player row."""
    if "error" in dvp:
        return None
    allowed = dvp.get("points_allowed_per_game", {}).get(defense, {}).get(position)
    rank = dvp.get("rank", {}).get(defense, {}).get(position)
    if allowed is None:
        return None
    return {"dvp_pts_allowed": allowed, "dvp_rank": rank}


# ---------------------------------------------------------------------------
# Rest-of-season schedule strength
# ---------------------------------------------------------------------------

#: Fantasy playoffs in most leagues. Weighted double because a title is decided
#: in these three weeks — a player with an easy Week 6 and a brutal Week 16 is
#: worse than the season-long average makes him look.
PLAYOFF_WEEKS = (15, 16, 17)
PLAYOFF_WEIGHT = 2.0


def team_schedule_map(
    season: int, schedules: pl.DataFrame | None = None
) -> dict[str, dict[int, str]]:
    """nflverse team -> {week: opponent}. Bye weeks are simply absent."""
    if schedules is None:
        import nflreadpy as nfl

        schedules = nfl.load_schedules()
    frame = schedules.filter(pl.col("season") == season)
    out: dict[str, dict[int, str]] = {}
    for row in frame.iter_rows(named=True):
        home, away, week = row["home_team"], row["away_team"], row["week"]
        out.setdefault(home, {})[week] = away
        out.setdefault(away, {})[week] = home
    return out


def ros_schedule_strength(
    entries: list[tuple[str, str, str]],
    dvp: dict[str, Any],
    season: int,
    *,
    from_week: int,
    through_week: int = 17,
    schedules: pl.DataFrame | None = None,
    playoff_weeks: tuple[int, ...] = PLAYOFF_WEEKS,
    playoff_weight: float = PLAYOFF_WEIGHT,
) -> list[dict[str, Any]]:
    """Remaining-schedule difficulty per player, in this league's scoring.

    ``entries`` is ``[(player_name, nflverse_team, position), ...]``. Difficulty
    is the DvP points-allowed of each remaining opponent, averaged with playoff
    weeks counted double.

    Higher ``ros_pts_allowed`` = easier remaining schedule, since it is points
    the defences on the schedule tend to give up.
    """
    schedule = team_schedule_map(season, schedules)
    allowed = dvp.get("points_allowed_per_game", {})
    if not allowed:
        return []

    league_mean = {
        pos: (
            sum(v[pos] for v in allowed.values() if pos in v)
            / max(1, sum(1 for v in allowed.values() if pos in v))
        )
        for pos in {p for v in allowed.values() for p in v}
    }

    results: list[dict[str, Any]] = []
    for name, team, position in entries:
        weeks = schedule.get(team, {})
        remaining = {w: opp for w, opp in weeks.items() if from_week <= w <= through_week}
        if not remaining:
            results.append({"player": name, "team": team, "error": "no remaining games"})
            continue

        numer = denom = 0.0
        detail: list[dict[str, Any]] = []
        for week in sorted(remaining):
            opponent = remaining[week]
            value = allowed.get(opponent, {}).get(position)
            if value is None:
                continue
            weight = playoff_weight if week in playoff_weeks else 1.0
            numer += value * weight
            denom += weight
            detail.append({"week": week, "opp": opponent, "pts_allowed": round(value, 2)})

        if not denom:
            results.append({"player": name, "team": team, "error": "no DvP for opponents"})
            continue

        weighted = numer / denom
        mean = league_mean.get(position)
        results.append(
            {
                "player": name,
                "team": team,
                "position": position,
                "games_remaining": len(detail),
                "ros_pts_allowed": round(weighted, 2),
                "vs_league_avg": round(weighted - mean, 2) if mean else None,
                "playoff_weeks": [d for d in detail if d["week"] in playoff_weeks],
                "byes_remaining": sorted(
                    w for w in range(from_week, through_week + 1) if w not in weeks
                ),
            }
        )

    results.sort(key=lambda r: -(r.get("ros_pts_allowed") or 0))
    return results
