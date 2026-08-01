#!/usr/bin/env python3
"""Dump anonymized league structure so the scoring parser can be built against
real data instead of guesses.

    uv run scripts/dump_league_settings.py

Writes one JSON file per league to data/samples/. What goes in:

  - scoring rules (statId -> points, including per-position overrides)
  - lineup slot counts, roster limits, playoff schedule shape
  - a sample of prior-season roster + box score STRUCTURE, so tool code can be
    written against real object shapes before this year's draft happens

What is deliberately left out:

  - your cookies (obviously)
  - league member names, owner names, team names, team abbreviations
    (every team becomes team1..teamN, keyed by a stable index)
  - anything in .env

NFL player names are kept — they're public and the parser needs realistic rows.
Read a file before sharing it if you want to confirm. They are safe to commit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist.config import ConfigError, find_repo_root, load_settings  # noqa: E402
from ff_assist.espn_client import ESPNError, get_league  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
OK, BAD, WARN = f"{GREEN}✓{RESET}", f"{RED}✗{RESET}", f"{YELLOW}!{RESET}"

OUT_DIR = find_repo_root() / "data" / "samples"

# Player attributes worth capturing to build tools against. Anything not here
# is dropped, which also keeps the files small.
PLAYER_FIELDS = (
    "name",
    "playerId",
    "position",
    "eligibleSlots",
    "lineupSlot",
    "proTeam",
    "injuryStatus",
    "injured",
    "percent_owned",
    "percent_started",
    "posRank",
    "total_points",
    "projected_total_points",
    "avg_points",
    "projected_avg_points",
    "active_status",
)


def _anonymize_teams(teams: list[Any]) -> dict[int, str]:
    """Stable team_id -> 'teamN' map, ordered by team_id for reproducibility."""
    return {t.team_id: f"team{i + 1}" for i, t in enumerate(sorted(teams, key=lambda t: t.team_id))}


def _dump_player(p: Any, weeks: int = 3) -> dict[str, Any]:
    out = {f: getattr(p, f, None) for f in PLAYER_FIELDS}
    stats = getattr(p, "stats", {}) or {}
    # Keep season totals (key 0) plus the first few real weeks — enough to see
    # the projected_breakdown / breakdown shape without dumping 18 weeks.
    keep = [k for k in sorted(stats) if k == 0][:1] + [k for k in sorted(stats) if k != 0][:weeks]
    out["stats_keys_available"] = sorted(stats)
    out["stats_sample"] = {str(k): stats[k] for k in keep}
    return out


def _dump_settings(raw: dict[str, Any]) -> dict[str, Any]:
    s = raw.get("settings", {}) or {}
    status = raw.get("status", {}) or {}
    return {
        "scoringSettings": s.get("scoringSettings", {}),
        "rosterSettings": s.get("rosterSettings", {}),
        "scheduleSettings": s.get("scheduleSettings", {}),
        "acquisitionSettings": s.get("acquisitionSettings", {}),
        "size": s.get("size"),
        "isPublic": s.get("isPublic"),
        "status": {
            "currentMatchupPeriod": status.get("currentMatchupPeriod"),
            "firstScoringPeriod": status.get("firstScoringPeriod"),
            "finalScoringPeriod": status.get("finalScoringPeriod"),
            "latestScoringPeriod": status.get("latestScoringPeriod"),
            "isActive": status.get("isActive"),
        },
        "scoringPeriodId": raw.get("scoringPeriodId"),
    }


def _prior_season_sample(cfg, settings, year: int) -> dict[str, Any]:
    """Best-effort: last season has real rosters and box scores; this season
    probably doesn't yet (drafts are late August)."""
    try:
        lg = get_league(cfg, settings=settings, year=year)
    except Exception as exc:  # noqa: BLE001 - best effort by design
        return {"year": year, "available": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}

    sample: dict[str, Any] = {"year": year, "available": True}
    alias = _anonymize_teams(lg.teams)

    try:
        team = lg.teams[0]
        sample["roster_shape"] = {
            "team": alias[team.team_id],
            "roster_size": len(team.roster),
            "players": [_dump_player(p) for p in team.roster[:4]],
        }
    except Exception as exc:  # noqa: BLE001
        sample["roster_shape"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    try:
        week = 5
        box = lg.box_scores(week=week)[0]
        sample["boxscore_shape"] = {
            "week": week,
            "home_team": alias.get(getattr(box.home_team, "team_id", None), "unknown"),
            "away_team": alias.get(getattr(box.away_team, "team_id", None), "unknown"),
            "home_score": box.home_score,
            "away_score": box.away_score,
            "home_lineup_slots": [getattr(p, "slot_position", None) for p in box.home_lineup],
            "home_lineup_sample": [
                {
                    "name": p.name,
                    "slot_position": getattr(p, "slot_position", None),
                    "position": getattr(p, "position", None),
                    "points": getattr(p, "points", None),
                    "projected_points": getattr(p, "projected_points", None),
                    "pro_opponent": getattr(p, "pro_opponent", None),
                    "game_played": getattr(p, "game_played", None),
                    "on_bye_week": getattr(p, "on_bye_week", None),
                    # These four are the heart of the re-scoring work: the raw
                    # stat line vs. what ESPN's own scoring turned it into.
                    "breakdown": getattr(p, "breakdown", None),
                    "points_breakdown": getattr(p, "points_breakdown", None),
                    "projected_breakdown": getattr(p, "projected_breakdown", None),
                    "projected_points_breakdown": getattr(p, "projected_points_breakdown", None),
                }
                for p in box.home_lineup[:6]
            ],
        }
    except Exception as exc:  # noqa: BLE001
        sample["boxscore_shape"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    try:
        fa = lg.free_agents(week=5, size=3)
        sample["free_agent_shape"] = [_dump_player(p, weeks=1) for p in fa]
    except Exception as exc:  # noqa: BLE001
        sample["free_agent_shape"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    return sample


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"{BAD} {exc}")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    prior_year = settings.season - 1
    failures = 0

    for cfg in settings.leagues:
        print(f"{DIM}--- {cfg.key} ---{RESET}")
        try:
            lg = get_league(cfg, settings=settings)
            raw = lg.espn_request.get_league()
        except ESPNError as exc:
            failures += 1
            print(f"{BAD} {cfg.key}: {str(exc).splitlines()[0]}")
            continue

        payload: dict[str, Any] = {
            "schema_version": 1,
            "league_key": cfg.key,
            "season": settings.season,
            "team_count": len(lg.teams),
            "current_week": lg.current_week,
            "settings": _dump_settings(raw),
            "espn_api_parsed": {
                # Captured to compare our parser against espn-api's, including
                # its known cross-league mutation bug.
                "position_slot_counts": dict(getattr(lg.settings, "position_slot_counts", {}) or {}),
                "scoring_format": [dict(x) for x in getattr(lg.settings, "scoring_format", [])],
            },
        }

        n_items = len(payload["settings"]["scoringSettings"].get("scoringItems", []))
        n_overrides = sum(
            1
            for i in payload["settings"]["scoringSettings"].get("scoringItems", [])
            if i.get("pointsOverrides")
        )
        print(f"{OK} settings: {n_items} scoring items, {n_overrides} with position overrides")

        print(f"{DIM}    pulling {prior_year} sample for object shapes…{RESET}")
        payload["prior_season_sample"] = _prior_season_sample(cfg, settings, prior_year)
        if not payload["prior_season_sample"].get("available"):
            print(f"{WARN} no {prior_year} history ({payload['prior_season_sample'].get('reason','')[:60]})")

        out = OUT_DIR / f"league_{cfg.key}_dump.json"
        out.write_text(json.dumps(payload, indent=2, default=str))
        size_kb = out.stat().st_size / 1024
        print(f"{OK} wrote {out.relative_to(find_repo_root())} ({size_kb:.0f} KB)\n")

    if failures:
        print(f"{BAD} {failures} league(s) failed.")
        return 1

    print(f"{GREEN}Done.{RESET} Files are in data/samples/ — no cookies, no owner or team names.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
