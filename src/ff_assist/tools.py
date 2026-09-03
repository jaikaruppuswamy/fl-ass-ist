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
from .datastore import get_store
from .dvp import defense_vs_position, dvp_for_team, ros_schedule_strength
from .espn_client import ESPNError, get_league, my_team
from .game_env import (
    load_week_environment,
    team_environment_map,
    to_espn_team,
    to_nflverse_team,
)
from .projections import consensus, disagreement, fetch_week, score_line
from .scoring import ScoringSettings
from .slots import LineupSlots, optimize_lineup
from .usage import normalize_name, player_usage, usage_for_roster
from .waivers import roster_needs, score_free_agent, suggest_faab, trending_adds

__all__ = [
    "list_leagues",
    "get_matchup",
    "get_start_sit_slate",
    "get_game_environment",
    "get_player_trend",
    "get_defense_vs_position",
    "get_ros_schedule_strength",
    "get_waiver_board",
]

# Weekly fantasy team scores have a standard deviation around 25-30 points, so
# a margin (the difference of two) sits near 40. Used only to turn a projected
# margin into a rough win probability — it is a sanity anchor, not a model, and
# is labelled as such in the response.
_MARGIN_SIGMA = 40.0


def _no_roster_reason(league: Any, exc: Exception) -> str:
    """Turn espn-api's raw failure into something readable at 9:30 on a Sunday.

    Before a league drafts, ESPN has no roster for the scoring period and
    espn-api raises a bare KeyError('rosterForCurrentScoringPeriod'). Surfacing
    that verbatim makes a completely normal August state look like a crash —
    and worse, it trains you to ignore the message in September when it might
    mean something real.
    """
    week = getattr(league, "current_week", None)
    detail = str(exc).strip("'\"")
    if detail == "rosterForCurrentScoringPeriod" or not week:
        return (
            "this league has not drafted yet — ESPN has no roster for the current "
            "scoring period. Expected before late August; re-run after your draft."
        )
    return f"no roster available for week {week}: {type(exc).__name__}: {exc}"


def _team_menu(league: Any) -> str:
    """The teams actually in the league, so the fix is inside the error itself.

    Without this you get told your id is wrong and are left to go find the
    right one in the ESPN app — which is the slowest possible way to learn a
    number the server was already holding.
    """
    try:
        pairs = sorted((t.team_id, t.team_name) for t in league.teams)
    except Exception:  # noqa: BLE001 — a broken league object must not mask the real error
        return ""
    if not pairs:
        return " This league reports no teams at all."
    listing = "; ".join(f"{tid}={name}" for tid, name in pairs)
    return f" Teams in this league: {listing}."


def _team_or_error(league: Any, cfg: LeagueConfig) -> tuple[Any, dict[str, Any] | None]:
    """Resolve the configured team, or say precisely why we cannot.

    Two failures used to print the same sentence, and they need different ones.
    TEAM_ID unset is a setup step never done. TEAM_ID set but matching nothing
    means the id is stale — which is exactly what a league rebuild looks like,
    because a commissioner who discards a league and recreates it gets fresh
    team ids in the new one. Telling someone that a line they can see in their
    .env is "not set" sends them to stare at the one thing that isn't wrong.

    Worth naming the case this canNOT catch: team ids are small integers, so a
    stale id often collides with a real team in the new league. Then everything
    resolves and the advice is about a stranger's roster. The only defence is
    the team *name*, which list_leagues reports — check it after any league
    change.
    """
    var = f"FF_LEAGUE_{cfg.key.upper()}_TEAM_ID"
    if cfg.team_id is None:
        return None, {
            "league": cfg.key,
            "error": f"{var} is not set — add it so the server knows which team is yours."
            + _team_menu(league),
        }
    team = my_team(league, cfg)
    if team is None:
        return None, {
            "league": cfg.key,
            "error": (
                f"{var}={cfg.team_id} matches no team in league {cfg.league_id}. "
                f"A recreated league hands out fresh team ids, so this is what a "
                f"league rebuild looks like — the league id was updated and the "
                f"team id was not." + _team_menu(league)
            ),
        }
    return team, None


def _cache(settings: Settings) -> Cache:
    return Cache(settings.cache_dir / "espn.sqlite")


def _win_probability(margin: float) -> float:
    """P(win) from a projected margin, via a normal CDF. Deliberately crude."""
    return round(0.5 * (1.0 + math.erf(margin / (_MARGIN_SIGMA * math.sqrt(2.0)))), 3)


def _resolve(league_key: str, settings: Settings | None = None) -> tuple[Settings, LeagueConfig]:
    settings = settings or get_settings()
    return settings, settings.league(league_key)


def _espn_projection(p: Any, scoring: ScoringSettings) -> float:
    """ESPN's projection for a player, expressed in this league's scoring."""
    breakdown = getattr(p, "projected_breakdown", None)
    if breakdown:
        return scoring.score(breakdown, getattr(p, "position", None)).points
    return float(getattr(p, "projected_points", 0.0) or 0.0)


def _sleeper_projection(
    p: Any, scoring: ScoringSettings, sleeper: Any | None
) -> float | None:
    """Sleeper's projection for a player, in this league's scoring, or None."""
    if not sleeper:
        return None
    return score_line(sleeper.line_for(p.name), scoring, getattr(p, "position", None))


def _projector(scoring: ScoringSettings, sleeper: Any | None) -> Any:
    """The function every ranking in this module sorts by.

    The consensus of ESPN and Sleeper, which two seasons of replay say is at or
    near the best available — see docs/projections.md. Falls back to whichever
    source exists, so a Sleeper outage costs the second opinion and nothing
    else.
    """

    def projection(p: Any) -> float:
        value, _basis = consensus(
            _espn_projection(p, scoring), _sleeper_projection(p, scoring, sleeper)
        )
        return value if value is not None else 0.0

    return projection


def _player_row(
    p: Any,
    scoring: ScoringSettings,
    *,
    slot: str | None = None,
    sleeper: Any | None = None,
) -> dict[str, Any]:
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

    # The second opinion, and the number every ranking here actually uses.
    espn_scored = row.get("my_scoring_proj", espn_proj)
    sleeper_proj = _sleeper_projection(p, scoring, sleeper)
    value, basis = consensus(espn_scored, sleeper_proj)
    row["proj"] = value if value is not None else 0.0
    if sleeper_proj is not None:
        row["sleeper_proj"] = round(sleeper_proj, 2)
        # Only when they genuinely split. Agreement is already carried by the
        # consensus and would just be noise in a 5KB budget.
        gap = disagreement(espn_scored, sleeper_proj)
        if gap is not None:
            row["disagree"] = gap
    elif basis == "espn":
        row["proj_basis"] = "espn_only"

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
            else:
                # Previously only the unset case produced a note, so a stale
                # team id showed up as a league with no my_team key at all —
                # a silent omission in the one response every brief opens with.
                _, problem = _team_or_error(lg, cfg)
                entry["note"] = problem["error"] if problem else "team not resolved"
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

    _team, problem = _team_or_error(lg, cfg)
    if problem is not None:
        return problem

    try:
        boxes = lg.box_scores(week=week)
    except Exception as exc:  # noqa: BLE001 — preseason and bye weeks both land here
        return {"league": cfg.key, "week": week, "error": _no_roster_reason(lg, exc)}

    # Both sides use the same consensus the slate ranks on, or the projected
    # margin here would contradict the swaps recommended there.
    sleeper = _safe_sleeper(settings.season, week)

    def side(lineup: list[Any]) -> tuple[list[dict[str, Any]], float]:
        starters = [p for p in lineup if getattr(p, "slot_position", "") not in ("BE", "IR")]
        rows = [
            _player_row(p, scoring, slot=getattr(p, "slot_position", None), sleeper=sleeper)
            for p in starters
        ]
        return rows, round(sum(r["proj"] for r in rows), 2)

    for box in boxes:
        home_id = getattr(box.home_team, "team_id", None)
        away_id = getattr(box.away_team, "team_id", None)
        if cfg.team_id not in (home_id, away_id):
            continue
        mine_home = cfg.team_id == home_id
        my_lineup = box.home_lineup if mine_home else box.away_lineup
        opp_lineup = box.away_lineup if mine_home else box.home_lineup
        opp_team = box.away_team if mine_home else box.home_team

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

    _team, problem = _team_or_error(lg, cfg)
    if problem is not None:
        return problem

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
        return {"league": cfg.key, "week": week, "error": _no_roster_reason(lg, exc)}

    # Rank on the ESPN/Sleeper consensus. Two seasons of week-by-week replay put
    # the mean at or near the top of everything measured, and ahead of either
    # source alone in 2024 — docs/projections.md. Sleeper absent is not an
    # error; the consensus simply becomes ESPN.
    sleeper = _safe_sleeper(settings.season, week)
    projection = _projector(scoring, sleeper)

    available = [p for p in roster if getattr(p, "slot_position", "") != "IR"]
    slots = lineup_slots.starting_slots
    optimal = optimize_lineup(slots, available, projection)

    # A LIST, not a dict keyed by slot_position. Every one of these leagues has
    # two RB slots and two WR slots, so a slot-keyed dict silently collapses
    # nine starters into seven — and the two it drops come back out of the set
    # difference below as "start these", naming players who are already
    # starting. The totals still agreed, because current_total is summed from
    # `roster` directly, which is exactly why it survived a season of tests.
    current = [
        p for p in roster if getattr(p, "slot_position", "") not in ("BE", "IR")
    ]
    current_total = round(
        sum(projection(p) for p in roster if getattr(p, "slot_position", "") not in ("BE", "IR")), 2
    )
    optimal_total = round(sum(projection(p) for p in optimal if p is not None), 2)

    optimal_names = {p.name for p in optimal if p is not None}
    current_names = {p.name for p in current}
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
    dvp = _safe_dvp(scoring, settings.season)

    rows: list[dict[str, Any]] = []
    for slot, player in zip(slots, optimal, strict=True):
        if player is None:
            rows.append({"slot": slot, "recommended": None, "note": "no eligible player"})
            continue
        row = _player_row(player, scoring, slot=slot, sleeper=sleeper)
        _enrich(row, player, env, usage, dvp)
        row["recommended"] = True
        rows.append(row)

    bench = []
    for p in available:
        if p.name in optimal_names or projection(p) <= 0:
            continue
        row = _player_row(p, scoring, sleeper=sleeper)
        _enrich(row, p, env, usage, dvp)
        bench.append(row)
    bench.sort(key=lambda r: -r["proj"])

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
        # Say which sources the ranking used. "espn" alone is a degraded but
        # valid answer, and the reader should know which one they got.
        # Coverage rather than a bare flag: Sleeper drops any player whose
        # published total it cannot rebuild from their own stat line, so
        # "espn+sleeper" can still mean some rows had one source. Saying
        # 428/462 is honest where a boolean would not be.
        "projection_basis": (
            f"espn+sleeper ({sleeper.coverage})" if sleeper else "espn"
        ),
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


def _frames(season: int) -> dict[str, Any]:
    """Warm frames from the process store when the server has one, else None
    so each helper loads for itself. Deployed runs take the fast path; the CLI
    harness and tests take the slow one and behave identically."""
    store = get_store()
    if store is None:
        return {"schedules": None, "stats": None, "snaps": None, "stats_season": season}
    return {
        "schedules": store.schedules(),
        "stats": store.player_stats(),
        "snaps": store.snap_counts(),
        "stats_season": store.resolve_stats_season(),
    }


def _safe_sleeper(season: int, week: int) -> Any | None:
    """Sleeper's projections for the week, or None.

    None means "no second opinion", and every caller treats that as a degraded
    but valid state rather than an error — the consensus falls back to ESPN and
    the response says so. Sleeper is an undocumented endpoint on somebody
    else's infrastructure; a lineup must not depend on it being up.
    """
    store = get_store()
    try:
        week_data = (
            store.sleeper_projections(season, week)
            if store is not None
            else fetch_week(season, week)
        )
    except Exception:  # noqa: BLE001
        return None
    return week_data or None


def _safe_environment(season: int, week: int) -> dict[str, dict[str, Any]]:
    """Vegas environment for the week, or {} if nflverse is unreachable or the
    books have not priced this far ahead (they run about three weeks out)."""
    try:
        return team_environment_map(season, week, _frames(season)["schedules"])
    except Exception:  # noqa: BLE001
        return {}


def _safe_usage(players: list[Any], season: int) -> dict[str, dict[str, Any]]:
    """Recent usage per player. Falls back to last season when the current one
    has not started — in September, last year's target share is the only usage
    signal that exists, and saying nothing would be worse."""
    names = [
        (p.name, getattr(p, "position", None), getattr(p, "proTeam", None)) for p in players
    ]
    frames = _frames(season)
    warm_season = frames["stats_season"]

    candidates: list[int] = []
    for candidate in (warm_season, season, season - 1):
        if candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        warm = candidate == warm_season
        try:
            out = usage_for_roster(
                names,
                candidate,
                weeks=4,
                stats=frames["stats"] if warm else None,
                snaps=frames["snaps"] if warm else None,
            )
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


def _safe_dvp(scoring: ScoringSettings, season: int) -> dict[str, Any]:
    """Rolling 4-week defense-vs-position in this league's scoring, or {}.

    Note this is a tie-breaker, not a headline. Measured across Jai's three
    leagues the ranking barely moves between scoring formats, so treat a soft
    matchup as a nudge rather than a reason.
    """
    frames = _frames(season)
    for candidate in (frames["stats_season"], season - 1):
        try:
            out = defense_vs_position(
                scoring,
                candidate,
                window=4,
                stats=frames["stats"] if candidate == frames["stats_season"] else None,
            )
        except Exception:  # noqa: BLE001
            continue
        if "error" not in out:
            return out
    return {}


def _enrich(
    row: dict[str, Any],
    player: Any,
    env: dict[str, dict[str, Any]],
    usage: dict[str, dict[str, Any]],
    dvp: dict[str, Any] | None = None,
) -> None:
    """Attach game environment, usage trend and matchup to a player row."""
    team_env = env.get(getattr(player, "proTeam", "") or "")
    if team_env:
        if team_env.get("implied_total") is not None:
            row["implied_total"] = team_env["implied_total"]
            row["spread"] = team_env["spread"]
        if team_env.get("weather_relevant") is False:
            row["indoors"] = True
        if dvp:
            opponent = to_nflverse_team(team_env.get("opp"))
            cell = dvp_for_team(dvp, opponent, row.get("pos")) if opponent else None
            if cell:
                row.update(cell)

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


def get_game_environment(
    week: int | None = None, settings: Settings | None = None
) -> dict[str, Any]:
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
        # Emit ESPN codes. load_week_environment carries nflverse's raw codes,
        # so without this the one tool that names teams would say LA and WAS
        # while every other tool says LAR and WSH — and a model asked about
        # LAR would find nothing.
        row: dict[str, Any] = {
            "matchup": f"{to_espn_team(g.away)}@{to_espn_team(g.home)}",
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
        except Exception:  # noqa: BLE001
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

    team, problem = _team_or_error(lg, cfg)
    if problem is not None:
        return problem

    # Reaching here means the team resolved, so an empty roster really is an
    # undrafted league — it is no longer standing in for "your team id is stale".
    if not getattr(team, "roster", None):
        return {
            "league": cfg.key,
            "error": "roster is empty — nothing to schedule until the draft",
        }

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


# ---------------------------------------------------------------------------
# 8. get_waiver_board
# ---------------------------------------------------------------------------


def get_waiver_board(
    league_key: str,
    top_n: int = 20,
    week: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Free agents worth claiming, ranked against what this roster is short of.

    The plan ranks waiver accuracy as the second-biggest source of edge, above
    flex and start/sit. So this leads with roster holes rather than raw
    projections: a startable RB when you have two for three slots is worth far
    more than a better bench WR.

    Includes a FAAB band per claim and, when Sleeper is reachable, how many
    managers added the player in the last 24 hours — useful for judging how
    contested a claim will be.
    """
    settings, cfg = _resolve(league_key, settings)
    try:
        lg = get_league(cfg, settings=settings)
        raw = lg.espn_request.get_league().get("settings", {})
    except ESPNError as exc:
        return {"league": cfg.key, "error": str(exc).splitlines()[0]}

    scoring = ScoringSettings.from_raw(raw, cfg.key)
    lineup = LineupSlots.from_raw(raw.get("rosterSettings", {}))
    week = week or lg.current_week or 1

    team = my_team(lg, cfg)
    roster = list(getattr(team, "roster", []) or []) if team else []
    needs = roster_needs(roster, lineup.starting_slots) if roster else {}

    try:
        pool = lg.free_agents(week=week, size=max(top_n * 3, 60))
    except Exception as exc:  # noqa: BLE001
        return {"league": cfg.key, "week": week, "error": _no_roster_reason(lg, exc)}

    if not pool:
        return {
            "league": cfg.key,
            "week": week,
            "error": "free agent pool is empty — normal before the draft",
        }

    usage = _safe_usage(pool[: top_n * 2], settings.season)
    trending = trending_adds()

    rows: list[dict[str, Any]] = []
    for player in pool:
        row = score_free_agent(player, scoring, needs=needs, usage=usage.get(player.name))
        adds = trending.get(normalize_name(player.name))
        if adds:
            row["trending_adds_24h"] = adds
        row["faab"] = suggest_faab(row, contested=bool(adds and adds > 5000))
        rows.append(row)

    # Rank: filling a hole beats raw projection, and a rising role beats a
    # static one at the same number.
    def rank(row: dict[str, Any]) -> tuple:
        need = {"critical": 2, "thin": 1}.get(row.get("fills_need"), 0)
        rising = 1 if row.get("usage_trend") == "rising" else 0
        return (-need, -rising, -row.get("espn_proj_avg", 0.0))

    rows.sort(key=rank)

    # Drop candidates: rostered, not a starter, lowest projected.
    drops: list[dict[str, Any]] = []
    if roster:
        starters = {p.name for p in roster if getattr(p, "lineupSlot", "") not in ("BE", "IR", "")}
        bench = [p for p in roster if p.name not in starters]
        bench.sort(key=lambda p: float(getattr(p, "projected_avg_points", 0) or 0))
        drops = [
            {
                "name": p.name,
                "pos": getattr(p, "position", None),
                "espn_proj_avg": round(float(getattr(p, "projected_avg_points", 0) or 0), 2),
            }
            for p in bench[:4]
        ]

    out: dict[str, Any] = {
        "league": cfg.key,
        "week": week,
        "scoring": scoring.format_label(),
        "roster_needs": needs or "roster is empty or balanced",
        "claims": rows[:top_n],
        "drop_candidates": drops,
        "faab_note": (
            "Bands are shares of your REMAINING budget and are deliberately "
            "coarse — they have never seen your leaguemates bid."
        ),
    }
    if not trending:
        out["trending_unavailable"] = (
            "Sleeper add counts could not be fetched; claims are ranked without "
            "the crowd-contest signal."
        )
    return out
