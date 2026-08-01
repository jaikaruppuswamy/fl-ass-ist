"""The Phase 1 tool surface.

Design rule from the plan: **tools return decisions, not data.** Three leagues
of raw ESPN JSON is hundreds of KB and produces slow, expensive, mediocre
analysis. The server does the joins and the arithmetic; the model does the
judgement. Every response here should stay under ~5KB per league — if one
doesn't, it isn't aggregating enough.

These are plain functions returning plain dicts. server.py wraps them for MCP
and scripts/try_tool.py calls them directly, so iterating on the shape of a
response never requires restarting Claude.
"""

from __future__ import annotations

import math
from typing import Any

from .cache import Cache, key_for
from .config import LeagueConfig, Settings, get_settings
from .espn_client import ESPNError, get_league, my_team
from .scoring import ScoringSettings
from .slots import LineupSlots, optimize_lineup

__all__ = ["list_leagues", "get_matchup", "get_start_sit_slate"]

# Weekly fantasy team scores have a standard deviation around 25-30 points, so
# a margin (the difference of two) sits near 40. Used only to turn a projected
# margin into a rough win probability — it is a sanity anchor, not a model, and
# is labelled as such in the response.
_MARGIN_SIGMA = 40.0


def _cache(settings: Settings) -> Cache:
    return Cache(settings.cache_dir / "espn.sqlite")


def _win_probability(margin: float) -> float:
    """P(win) from a projected margin, via a normal CDF. Deliberately crude."""
    return round(0.5 * (1.0 + math.erf(margin / (_MARGIN_SIGMA * math.sqrt(2.0)))), 3)


def _resolve(league_key: str, settings: Settings | None = None) -> tuple[Settings, LeagueConfig]:
    settings = settings or get_settings()
    return settings, settings.league(league_key)


def _player_row(p: Any, scoring: ScoringSettings, *, slot: str | None = None) -> dict[str, Any]:
    """One compact, pre-scored row. Keep every field decision-relevant."""
    espn_proj = round(float(getattr(p, "projected_points", 0.0) or 0.0), 2)
    projected_breakdown = getattr(p, "projected_breakdown", None)
    position = getattr(p, "position", None)

    row: dict[str, Any] = {
        "name": p.name,
        "pos": position,
        "team": getattr(p, "proTeam", None),
        "opp": getattr(p, "pro_opponent", None),
        "espn_proj": espn_proj,
    }
    if slot is not None:
        row["slot"] = slot

    # Re-score ESPN's own projected stat line under THIS league's rules. When
    # the two disagree we surface it: usually it means the projection predates
    # a scoring quirk, and in a bucket-scoring league it is the norm.
    if projected_breakdown:
        ours = scoring.score(projected_breakdown, position)
        row["my_scoring_proj"] = ours.points
        if abs(ours.points - espn_proj) >= 0.5:
            row["proj_delta"] = round(ours.points - espn_proj, 2)

    status = getattr(p, "injuryStatus", None)
    if status and status not in ("ACTIVE", "NORMAL"):
        row["injury"] = status
    if getattr(p, "on_bye_week", False):
        row["bye"] = True
    pct = getattr(p, "percent_started", None)
    if pct is not None and pct >= 0:
        row["pct_started"] = pct
    return row


# ---------------------------------------------------------------------------
# 1. list_leagues
# ---------------------------------------------------------------------------


def list_leagues(settings: Settings | None = None) -> dict[str, Any]:
    """Every configured league: scoring format, roster shape, current week.

    This is the orientation call — it tells the model what the other two tools
    can be pointed at, and how the three leagues actually differ.
    """
    settings = settings or get_settings()
    cache = _cache(settings)
    out: list[dict[str, Any]] = []

    for cfg in settings.leagues:
        entry: dict[str, Any] = {"key": cfg.key, "league_id": cfg.league_id}
        try:
            lg = get_league(cfg, settings=settings)
            raw = cache.get_or_set(
                key_for(cfg.key, settings.season, "settings"),
                "settings",
                lambda lg=lg: lg.espn_request.get_league().get("settings", {}),
            )
            scoring = ScoringSettings.from_raw(raw, cfg.key)
            lineup = LineupSlots.from_raw(raw.get("rosterSettings", {}))

            entry.update(
                {
                    "name": cfg.label or getattr(lg.settings, "name", cfg.key),
                    "teams": len(lg.teams),
                    "current_week": lg.current_week,
                    "scoring": scoring.summary(),
                    "lineup": lineup.summary(),
                }
            )
            team = my_team(lg, cfg)
            if team is not None:
                entry["my_team"] = {
                    "name": team.team_name,
                    "record": f"{team.wins}-{team.losses}",
                    "roster_size": len(team.roster),
                }
            elif cfg.team_id is None:
                entry["note"] = f"FF_LEAGUE_{cfg.key.upper()}_TEAM_ID not set in .env"
        except ESPNError as exc:
            entry["error"] = str(exc).splitlines()[0]
        out.append(entry)

    cache.close()
    return {"season": settings.season, "leagues": out}


# ---------------------------------------------------------------------------
# 2. get_matchup
# ---------------------------------------------------------------------------


def get_matchup(
    league_key: str, week: int | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """Your lineup vs your opponent's, with a projected margin.

    The opponent's lineup is shown as *they have it set*, not as they should —
    knowing they have a bye-week player still in the flex is exactly the kind
    of edge worth seeing.
    """
    settings, cfg = _resolve(league_key, settings)
    lg = get_league(cfg, settings=settings)
    week = week or lg.current_week

    raw = lg.espn_request.get_league().get("settings", {})
    scoring = ScoringSettings.from_raw(raw, cfg.key)

    if cfg.team_id is None:
        return {"league": cfg.key, "error": f"FF_LEAGUE_{cfg.key.upper()}_TEAM_ID not set in .env"}

    try:
        boxes = lg.box_scores(week=week)
    except Exception as exc:  # noqa: BLE001 — preseason and bye weeks both land here
        return {"league": cfg.key, "week": week, "error": f"no box score available: {exc}"}

    for box in boxes:
        home_id = getattr(box.home_team, "team_id", None)
        away_id = getattr(box.away_team, "team_id", None)
        if cfg.team_id not in (home_id, away_id):
            continue
        mine_home = cfg.team_id == home_id
        my_lineup = box.home_lineup if mine_home else box.away_lineup
        opp_lineup = box.away_lineup if mine_home else box.home_lineup
        opp_team = box.away_team if mine_home else box.home_team

        def side(lineup: list[Any]) -> tuple[list[dict[str, Any]], float]:
            starters = [p for p in lineup if getattr(p, "slot_position", "") not in ("BE", "IR")]
            rows = [
                _player_row(p, scoring, slot=getattr(p, "slot_position", None)) for p in starters
            ]
            total = round(sum(r.get("my_scoring_proj", r["espn_proj"]) for r in rows), 2)
            return rows, total

        my_rows, my_total = side(my_lineup)
        opp_rows, opp_total = side(opp_lineup)
        margin = round(my_total - opp_total, 2)

        return {
            "league": cfg.key,
            "week": week,
            "me": {"projected": my_total, "starters": my_rows},
            "opponent": {
                "name": getattr(opp_team, "team_name", "?"),
                "projected": opp_total,
                "starters": opp_rows,
            },
            "projected_margin": margin,
            "win_probability": _win_probability(margin),
            "win_probability_note": (
                "normal approximation on a 40-point margin sigma; directional only"
            ),
        }

    return {"league": cfg.key, "week": week, "error": "no matchup found for your team"}


# ---------------------------------------------------------------------------
# 3. get_start_sit_slate
# ---------------------------------------------------------------------------


def get_start_sit_slate(
    league_key: str, week: int | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """The workhorse: one pre-scored row per lineup slot, plus the optimal set.

    Returns your current lineup, the optimal lineup under this league's own
    scoring, and the specific swaps between them. Phase 2 adds the columns that
    break ties — DvP, implied team total, weather, usage trend.
    """
    settings, cfg = _resolve(league_key, settings)
    lg = get_league(cfg, settings=settings)
    week = week or lg.current_week

    raw = lg.espn_request.get_league().get("settings", {})
    scoring = ScoringSettings.from_raw(raw, cfg.key)
    lineup_slots = LineupSlots.from_raw(raw.get("rosterSettings", {}))

    if cfg.team_id is None:
        return {"league": cfg.key, "error": f"FF_LEAGUE_{cfg.key.upper()}_TEAM_ID not set in .env"}

    try:
        boxes = lg.box_scores(week=week)
        roster = None
        for box in boxes:
            if getattr(box.home_team, "team_id", None) == cfg.team_id:
                roster = box.home_lineup
                break
            if getattr(box.away_team, "team_id", None) == cfg.team_id:
                roster = box.away_lineup
                break
        if roster is None:
            raise LookupError("team not in any box score")
    except Exception as exc:  # noqa: BLE001
        return {"league": cfg.key, "week": week, "error": f"no roster available: {exc}"}

    def projection(p: Any) -> float:
        breakdown = getattr(p, "projected_breakdown", None)
        if breakdown:
            return scoring.score(breakdown, getattr(p, "position", None)).points
        return float(getattr(p, "projected_points", 0.0) or 0.0)

    available = [p for p in roster if getattr(p, "slot_position", "") != "IR"]
    slots = lineup_slots.starting_slots
    optimal = optimize_lineup(slots, available, projection)

    current = {
        getattr(p, "slot_position", ""): p
        for p in roster
        if getattr(p, "slot_position", "") not in ("BE", "IR")
    }
    current_total = round(
        sum(projection(p) for p in roster if getattr(p, "slot_position", "") not in ("BE", "IR")), 2
    )
    optimal_total = round(sum(projection(p) for p in optimal if p is not None), 2)

    optimal_names = {p.name for p in optimal if p is not None}
    current_names = {p.name for p in current.values()}
    # Named from the reader's point of view: `start` is what to move in,
    # `bench` is what to move out. Getting these backwards in the response
    # would be worse than returning nothing at all.
    changes = (
        {
            "start": sorted(optimal_names - current_names),
            "bench": sorted(current_names - optimal_names),
            "points_gained": round(optimal_total - current_total, 2),
        }
        if optimal_names != current_names
        else None
    )

    rows: list[dict[str, Any]] = []
    for slot, player in zip(slots, optimal):
        if player is None:
            rows.append({"slot": slot, "recommended": None, "note": "no eligible player"})
            continue
        row = _player_row(player, scoring, slot=slot)
        row["recommended"] = True
        rows.append(row)

    bench = [
        _player_row(p, scoring)
        for p in available
        if p.name not in optimal_names and projection(p) > 0
    ]
    bench.sort(key=lambda r: -(r.get("my_scoring_proj", r["espn_proj"])))

    return {
        "league": cfg.key,
        "week": week,
        "scoring": scoring.format_label(),
        "current_projected": current_total,
        "optimal_projected": optimal_total,
        "changes": changes,
        "optimal_lineup": rows,
        "bench": bench[:8],
    }
