"""The waiver board.

The plan's §6 ranks waiver accuracy as the second-largest source of edge,
above flex decisions and start/sit — "most leagues are won here". So this tool
answers a specific question rather than dumping the free-agent pool: *given
what my roster is actually short of, who is worth a claim, and what should I
bid?*

Three things shape a claim, in descending order of how much they matter:

1. **Does it fill a hole?** A better bench WR when you already start three is
   worth far less than a startable RB when you have two for three slots.
2. **Is the usage going up?** Rest-of-season value on a waiver add is mostly a
   bet on role. A back whose snap share has climbed three weeks running is a
   different asset from one with the same season average and a falling trend.
3. **What does the schedule look like?** Weighted toward the fantasy playoffs,
   since a Week 16 add is only ever bought for Weeks 15-17.

The Sleeper trending-adds overlay is deliberately optional. It is crowd wisdom,
useful for gauging how contested a claim will be, and it lives behind a network
call that may not be reachable from every host — so the board is complete
without it and merely better with it.
"""

from __future__ import annotations

import logging
from typing import Any

from .scoring import ScoringSettings
from .usage import normalize_name

__all__ = ["roster_needs", "suggest_faab", "trending_adds", "score_free_agent"]

log = logging.getLogger("ff_assist.waivers")

SLEEPER_TRENDING_URL = "https://api.sleeper.app/v1/players/nfl/trending/add"

#: Bands are shares of the REMAINING budget, and coarse on purpose. A precise
#: bid from a model that has never seen your leaguemates bid is false
#: confidence; the useful signal is "splash" versus "a dollar".
_FAAB_BANDS = (
    "$0-1 — only if nobody else wants him",
    "1-4% — speculative",
    "5-12% — worth a real bid",
    "15-25% — a genuine starter, bid like you mean it",
)

#: Which roster positions can fill which starting slots.
_FILLS: dict[str, tuple[str, ...]] = {
    "QB": ("QB", "OP", "TQB"),
    "RB": ("RB", "RB/WR", "RB/WR/TE"),
    "WR": ("WR", "RB/WR", "WR/TE", "RB/WR/TE"),
    "TE": ("TE", "WR/TE", "RB/WR/TE"),
    "K": ("K",),
    "D/ST": ("D/ST",),
}


def roster_needs(
    roster: list[Any], starting_slots: list[str]
) -> dict[str, dict[str, Any]]:
    """Where the roster is thin, by position.

    "Thin" means at most one body beyond what the lineup demands. Depth of one
    is a bye week or a hamstring away from starting somebody you did not want
    to start, which is exactly when a waiver claim pays for itself.
    """
    demand: dict[str, int] = {}
    for position, slots in _FILLS.items():
        demand[position] = sum(1 for s in starting_slots if s in slots and s == position)
    # Flex slots create demand that any of their eligible positions can meet.
    flex_demand = sum(1 for s in starting_slots if s not in _FILLS and "/" in s)

    have: dict[str, int] = {}
    for player in roster:
        position = getattr(player, "position", None)
        if position in _FILLS:
            have[position] = have.get(position, 0) + 1

    out: dict[str, dict[str, Any]] = {}
    for position, required in demand.items():
        if required == 0 and position not in ("RB", "WR", "TE"):
            continue
        count = have.get(position, 0)
        # Flex pressure lands on the skill positions.
        effective = required + (flex_demand if position in ("RB", "WR", "TE") else 0) * 0.5
        surplus = count - effective
        if surplus <= 1:
            out[position] = {
                "rostered": count,
                "starting_demand": round(effective, 1),
                "status": "critical" if surplus <= 0 else "thin",
            }
    return out


def score_free_agent(
    player: Any,
    scoring: ScoringSettings,
    *,
    needs: dict[str, dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    ros: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One compact, decision-shaped row for a free agent."""
    position = getattr(player, "position", None)
    projected = getattr(player, "projected_avg_points", None)
    if projected is None or projected < 0:
        projected = getattr(player, "projected_total_points", 0.0)

    row: dict[str, Any] = {
        "name": player.name,
        "pos": position,
        "team": getattr(player, "proTeam", None),
        "espn_proj_avg": round(float(projected or 0.0), 2),
    }

    owned = getattr(player, "percent_owned", None)
    if owned is not None and owned >= 0:
        row["pct_owned"] = owned

    status = getattr(player, "injuryStatus", None)
    if status and status not in ("ACTIVE", "NORMAL"):
        row["injury"] = status

    if needs and position in needs:
        row["fills_need"] = needs[position]["status"]

    if usage:
        trend = usage.get("trend")
        if trend and trend != "insufficient_data":
            row["usage_trend"] = trend
        if usage.get("last"):
            row["usage"] = usage["last"]

    if ros and "ros_pts_allowed" in ros:
        row["ros_matchups"] = ros["ros_pts_allowed"]

    return row


def suggest_faab(row: dict[str, Any], *, contested: bool = False) -> str:
    """A FAAB band, expressed as a share of the remaining budget.

    Deliberately a range and deliberately coarse. Precise-looking bids from a
    model that has never seen your leaguemates bid would be false confidence —
    the useful signal is 'this is a splash' versus 'this is a dollar'.
    """
    projected = row.get("espn_proj_avg", 0.0)
    need = row.get("fills_need")
    rising = row.get("usage_trend") == "rising"

    # Tier on whether the player is *startable*, not on an opaque point total.
    # Raw production dominates roster shape: a 14-point-per-game free agent is
    # a league-winner whether or not you happen to be thin at his position,
    # because you can always start him over somebody or trade him.
    if projected >= 12.0:
        tier = 3
    elif projected >= 8.5:
        tier = 2
    elif projected >= 5.5:
        tier = 1
    else:
        tier = 0

    # Need and a rising role promote a borderline player. They never demote a
    # good one, which is why these only ever add.
    if need == "critical":
        tier += 1
    elif need == "thin" and rising:
        tier += 1
    elif rising and tier == 0:
        tier += 1

    # Being contested does not change what he is worth — it changes what it
    # costs to win him. Worth one tier, no more; chasing a bidding war on a
    # replacement-level player is how a season's budget disappears in October.
    if contested:
        tier += 1

    return _FAAB_BANDS[min(tier, 3)]


def trending_adds(limit: int = 25, *, fetch: Any = None) -> dict[str, int]:
    """Normalized player name -> adds in the last 24h, from Sleeper.

    Free, no auth, 90 req/min. Returns {} on any failure: this is a nice-to-have
    overlay for judging how contested a claim is, and the board must not fail
    because a third-party endpoint is unreachable.

    Sleeper keys its response by Sleeper player id, so the ids are resolved
    through nflverse's crosswalk and matched on the normalized names already
    validated in usage.py.
    """
    if fetch is None:

        def fetch(url: str, params: dict[str, Any]) -> Any:  # pragma: no cover
            import httpx

            return httpx.get(url, params=params, timeout=10).json()

    try:
        raw = fetch(SLEEPER_TRENDING_URL, {"limit": limit, "lookback_hours": 24})
    except Exception as exc:  # noqa: BLE001
        log.info("Sleeper trending unavailable: %s", type(exc).__name__)
        return {}

    counts: dict[str, int] = {}
    for entry in raw or []:
        sleeper_id = str(entry.get("player_id", ""))
        if sleeper_id:
            counts[sleeper_id] = int(entry.get("count", 0))
    if not counts:
        return {}

    try:
        import nflreadpy as nfl
        import polars as pl

        ids = nfl.load_ff_playerids()
        frame = ids.filter(pl.col("sleeper_id").is_not_null())
    except Exception as exc:  # noqa: BLE001
        log.info("player id crosswalk unavailable: %s", type(exc).__name__)
        return {}

    by_name: dict[str, int] = {}
    for row in frame.iter_rows(named=True):
        sleeper_id = str(row.get("sleeper_id") or "").split(".")[0]
        adds = counts.get(sleeper_id)
        if adds:
            name = row.get("name") or row.get("merge_name")
            if name:
                by_name[normalize_name(name)] = adds
    return by_name
