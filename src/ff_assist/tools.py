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
from .dvp import defense_vs_position, dvp_for_team, ros_schedule_strength
from .game_env import load_week_environment, team_environment_map
from .scoring import ScoringSettings
from .slots import LineupSlots, optimize_lineup
from .usage import player_usage, usage_for_roster

__all__ = [
    "list_leagues",
    "get_matchup",
    "get_start_sit_slate",
    "get_game_environment",
    "get_player_trend",
    "get_defense_vs_position",
    "get_ros_schedule_strength",
]

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

    # Phase 2 columns. Both degrade to absent rather than failing the call:
    # a slate with no Vegas line is still worth having.
    env = _safe_environment(settings.season, week)
    usage = _safe_usage(available, settings.season)

    rows: list[dict[str, Any]] = []
    for slot, player in zip(slots, optimal):
        if player is None:
            rows.append({"slot": slot, "recommended": None, "note": "no eligible player"})
            continue
        row = _player_row(player, scoring, slot=slot)
        _enrich(row, player, env, usage)
        row["recommended"] = True
        rows.append(row)

    bench = []
    for p in available:
        if p.name in optimal_names or projection(p) <= 0:
            continue
        row = _player_row(p, scoring)
        _enrich(row, p, env, usage)
        bench.append(row)
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
        "environment_available": bool(env),
    }


# ---------------------------------------------------------------------------
# Phase 2 helpers
# ---------------------------------------------------------------------------


def _safe_weather(game: Any) -> dict[str, Any] | None:
    """Forecast for one game, or None when it is played under a roof.

    Only reports what is worth acting on — a calm, dry forecast returns {} and
    the caller omits the field entirely rather than padding every row.
    """
    from .weather import forecast_for_game

    kickoff = f"{game.gameday}T{game.gametime}" if game.gameday and game.gametime else None
    out = forecast_for_game(game.stadium, game.roof, kickoff)
    if out is None:
        return None
    if "error" in out or "unknown_venue" in out:
        return {}
    keep = {k: v for k, v in out.items() if k in ("wind_mph", "precip_pct", "verdict", "caveat")}
    if not out.get("verdict") and (out.get("precip_pct") or 0) < 40:
        return {}
    return keep


def _safe_environment(season: int, week: int) -> dict[str, dict[str, Any]]:
    """Vegas environment for the week, or {} if nflverse is unreachable or the
    books have not priced this far ahead (they run about three weeks out)."""
    try:
        return team_environment_map(season, week)
    except Exception:  # noqa: BLE001
        return {}


def _safe_usage(players: list[Any], season: int) -> dict[str, dict[str, Any]]:
    """Recent usage per player. Falls back to last season when the current one
    has not started — in September, last year's target share is the only usage
    signal that exists, and saying nothing would be worse."""
    names = [
        (p.name, getattr(p, "position", None), getattr(p, "proTeam", None)) for p in players
    ]
    for candidate in (season, season - 1):
        try:
            out = usage_for_roster(names, candidate, weeks=4)
        except Exception:  # noqa: BLE001
            # A season that has not started 404s. That is "no data yet", not
            # "unreachable" — keep going and try the prior season.
            continue
        if any("error" not in v for v in out.values()):
            if candidate != season:
                for v in out.values():
                    v["season"] = candidate
                    v["stale"] = True
            return out
    return {}


def _enrich(
    row: dict[str, Any],
    player: Any,
    env: dict[str, dict[str, Any]],
    usage: dict[str, dict[str, Any]],
) -> None:
    """Attach game environment and usage trend to a player row, in place."""
    team_env = env.get(getattr(player, "proTeam", "") or "")
    if team_env:
        if team_env.get("implied_total") is not None:
            row["implied_total"] = team_env["implied_total"]
            row["spread"] = team_env["spread"]
        if team_env.get("weather_relevant") is False:
            row["indoors"] = True

    u = usage.get(row["name"])
    if u and "error" not in u:
        if u.get("trend") and u["trend"] != "insufficient_data":
            row["usage_trend"] = u["trend"]
        if u.get("last"):
            row["usage"] = u["last"]
        if u.get("stale"):
            row["usage_season"] = u.get("season")


# ---------------------------------------------------------------------------
# 4. get_game_environment
# ---------------------------------------------------------------------------


def get_game_environment(week: int | None = None, settings: Settings | None = None) -> dict[str, Any]:
    """Vegas implied team totals, spreads and venue for every game in a week.

    The plan calls the implied team total the highest-signal single variable in
    the system. Note it is a near-term signal: books price roughly three weeks
    ahead, so late-season weeks come back unpriced rather than wrong.
    """
    settings = settings or get_settings()
    week = week or 1
    try:
        games = load_week_environment(settings.season, week)
    except Exception as exc:  # noqa: BLE001
        return {"week": week, "error": f"nflverse unreachable: {exc}"}

    if not games:
        return {"week": week, "error": f"no games found for {settings.season} week {week}"}

    rows = []
    for g in games:
        row: dict[str, Any] = {
            "matchup": f"{g.away}@{g.home}",
            "total": g.total_line,
            "spread_home": g.spread_line,
            "implied_home": g.implied_home,
            "implied_away": g.implied_away,
        }
        wx = _safe_weather(g)
        if wx is None:
            row["indoors"] = True
        elif wx:
            row["weather"] = wx
        rows.append(row)

    priced = [g for g in games if g.implied_home is not None]
    return {
        "season": settings.season,
        "week": week,
        "games": rows,
        "priced": f"{len(priced)}/{len(games)}",
        "note": (
            "Odds are posted about three weeks ahead; unpriced games show null "
            "totals rather than estimates."
        )
        if len(priced) < len(games)
        else None,
    }


# ---------------------------------------------------------------------------
# 5. get_player_trend
# ---------------------------------------------------------------------------


def get_player_trend(
    player: str, weeks: int = 6, settings: Settings | None = None
) -> dict[str, Any]:
    """Week-by-week usage for one player: snaps, targets, target share, air
    yards share, carries — and the direction of the headline metric.

    Usage leads results, so a rising target share is information ESPN's
    projection does not contain.
    """
    settings = settings or get_settings()
    problems: list[str] = []
    for season in (settings.season, settings.season - 1):
        try:
            out = player_usage(player, season, weeks=weeks)
        except Exception as exc:  # noqa: BLE001
            # nflverse 404s a season that has not started yet. Falling through
            # to last season is the whole point — in September that is the only
            # usage data that exists.
            problems.append(f"{season}: {type(exc).__name__}")
            continue
        if "error" not in out:
            if season != settings.season:
                out["note"] = f"{settings.season} has no games yet; showing {season} usage."
            return out
        problems.append(f"{season}: {out['error']}")
    return {"player": player, "error": "; ".join(problems)}


# ---------------------------------------------------------------------------
# 6. get_defense_vs_position
# ---------------------------------------------------------------------------


def get_defense_vs_position(
    league_key: str,
    window: int = 4,
    position: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Points allowed per game to each position, by defence, in one league's
    own scoring.

    Vendors publish this in generic PPR, which is the wrong denominator for a
    league that scores yardage in buckets or a half point per reception — the
    ranking genuinely reorders. Rank 1 is the softest matchup.

    Args:
        league_key: which league's scoring to express the table in
        window: rolling window in weeks; 0 for season-long
        position: limit to one of QB/RB/WR/TE
    """
    settings, cfg = _resolve(league_key, settings)
    try:
        lg = get_league(cfg, settings=settings)
        scoring = ScoringSettings.from_raw(
            lg.espn_request.get_league().get("settings", {}), cfg.key
        )
    except ESPNError as exc:
        return {"league": cfg.key, "error": str(exc).splitlines()[0]}

    positions = (position,) if position else ("QB", "RB", "WR", "TE")
    for season in (settings.season, settings.season - 1):
        try:
            out = defense_vs_position(
                scoring, season, window=window or None, positions=positions
            )
        except Exception as exc:  # noqa: BLE001
            continue
        if "error" not in out:
            out["league"] = cfg.key
            if season != settings.season:
                out["note"] = f"{settings.season} has no games yet; showing {season}."
            return out
    return {"league": cfg.key, "error": "no nflverse data available"}


# ---------------------------------------------------------------------------
# 7. get_ros_schedule_strength
# ---------------------------------------------------------------------------


def get_ros_schedule_strength(
    league_key: str, from_week: int | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """Remaining-schedule difficulty for every player on your roster, in this
    league's scoring, with the fantasy playoff weeks (15-17) weighted double.

    A player with an easy October and a brutal December is worse than his
    season-long matchup average suggests, and that only shows up if the
    playoff weeks are weighted.
    """
    settings, cfg = _resolve(league_key, settings)
    try:
        lg = get_league(cfg, settings=settings)
        raw = lg.espn_request.get_league().get("settings", {})
        scoring = ScoringSettings.from_raw(raw, cfg.key)
    except ESPNError as exc:
        return {"league": cfg.key, "error": str(exc).splitlines()[0]}

    if cfg.team_id is None:
        return {"league": cfg.key, "error": f"FF_LEAGUE_{cfg.key.upper()}_TEAM_ID not set in .env"}

    team = my_team(lg, cfg)
    if team is None or not getattr(team, "roster", None):
        return {
            "league": cfg.key,
            "error": "roster is empty — nothing to schedule until the draft",
        }

    from .game_env import to_nflverse_team

    entries = [
        (p.name, to_nflverse_team(getattr(p, "proTeam", None)), getattr(p, "position", None))
        for p in team.roster
        if getattr(p, "position", None) in ("QB", "RB", "WR", "TE")
    ]

    dvp_season = settings.season
    dvp = {}
    for season in (settings.season, settings.season - 1):
        try:
            dvp = defense_vs_position(scoring, season, window=None)
        except Exception:  # noqa: BLE001
            continue
        if "error" not in dvp:
            dvp_season = season
            break
    if not dvp or "error" in dvp:
        return {"league": cfg.key, "error": "no nflverse data for defensive strength"}

    week = from_week or max(1, lg.current_week or 1)
    try:
        rows = ros_schedule_strength(entries, dvp, settings.season, from_week=week)
    except Exception as exc:  # noqa: BLE001
        return {"league": cfg.key, "error": f"schedule unavailable: {exc}"}

    return {
        "league": cfg.key,
        "from_week": week,
        "scoring": scoring.format_label(),
        "dvp_basis_season": dvp_season,
        "players": rows,
        "note": (
            "ros_pts_allowed is what the remaining opponents give up to that "
            "position; higher is an easier schedule. Weeks 15-17 count double."
        ),
    }
