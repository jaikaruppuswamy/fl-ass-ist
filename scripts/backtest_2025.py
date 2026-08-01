#!/usr/bin/env python3
"""Replay last season against the real ESPN data, and score ourselves.

    uv run scripts/backtest_2025.py                      # all leagues, all weeks
    uv run scripts/backtest_2025.py --weeks 1-8
    uv run scripts/backtest_2025.py --league inai -v

Two jobs, and the second is the interesting one.

**Does the code work on real data?** Everything downstream of a roster has only
ever been exercised against fixtures. Fixtures cannot tell you whether ESPN's
live ``projected_breakdown`` matches the shape we built against, whether the
optimizer copes with a real 16-man roster, or whether responses stay inside the
5KB budget once names and opponents are real. A completed season answers all
three, months before Week 1 can punish a wrong guess.

**Would it have helped?** For each completed week we compare three lineups:

* **actual** — what you really started, scored on what really happened
* **model** — what the optimizer would have started, using the projections that
  existed at the time, scored on what really happened
* **perfect** — the best lineup available in hindsight

``model - actual`` is the honest estimate of what this system is worth. It will
be a small positive number, and anyone promising otherwise is selling
something: you already set your own lineups competently. ``perfect - model`` is
the ceiling nobody reaches, and it exists here to keep the first number in
proportion.

A caveat worth stating plainly. ESPN stores the *final* projection for a week,
not the one visible on Sunday morning, so the model lineup benefits slightly
from projections that may already reflect late news. That biases ``model``
upward. Treat the result as an optimistic bound, not a promise.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist import tools  # noqa: E402
from ff_assist.config import ConfigError, load_settings  # noqa: E402
from ff_assist.espn_client import ESPNError, get_league  # noqa: E402
from ff_assist.scoring import ScoringSettings  # noqa: E402
from ff_assist.slots import LineupSlots, optimize_lineup  # noqa: E402

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)
OK, BAD, WARN = f"{GREEN}✓{RESET}", f"{RED}✗{RESET}", f"{YELLOW}!{RESET}"
BUDGET = 5 * 1024


def parse_weeks(spec: str) -> list[int]:
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(w) for w in spec.split(",") if w.strip()]


def actual(player: Any) -> float:
    return float(getattr(player, "points", 0.0) or 0.0)


def projected(player: Any, scoring: ScoringSettings) -> float:
    breakdown = getattr(player, "projected_breakdown", None)
    if breakdown:
        return scoring.score(breakdown, getattr(player, "position", None)).points
    return float(getattr(player, "projected_points", 0.0) or 0.0)


def week_result(lineup: list[Any], slots: list[str], scoring: ScoringSettings) -> dict[str, Any]:
    """Actual / model / perfect points for one team-week."""
    available = [p for p in lineup if getattr(p, "slot_position", "") != "IR"]
    started = [p for p in lineup if getattr(p, "slot_position", "") not in ("BE", "IR")]

    model = optimize_lineup(slots, available, lambda p: projected(p, scoring))
    perfect = optimize_lineup(slots, available, actual)

    return {
        "actual": round(sum(actual(p) for p in started), 2),
        "model": round(sum(actual(p) for p in model if p is not None), 2),
        "perfect": round(sum(actual(p) for p in perfect if p is not None), 2),
        "swaps": sorted(
            {p.name for p in model if p is not None} - {p.name for p in started}
        ),
    }


def check_tools(key: str, week: int, settings: Any, verbose: bool) -> list[str]:
    """Run the live tools for a real week and assert what should hold."""
    problems: list[str] = []

    slate = tools.get_start_sit_slate(key, week, settings=settings)
    if "error" in slate:
        return [f"get_start_sit_slate wk{week}: {slate['error']}"]

    size = len(json.dumps(slate).encode())
    if size > BUDGET:
        problems.append(f"slate wk{week} is {size:,}B, over the {BUDGET:,}B budget")
    if slate["optimal_projected"] < slate["current_projected"] - 0.01:
        problems.append(
            f"wk{week} optimal ({slate['optimal_projected']}) below current "
            f"({slate['current_projected']}) — the optimizer found a worse lineup"
        )
    named = [r["name"] for r in slate["optimal_lineup"] if r.get("name")]
    if len(named) != len(set(named)):
        problems.append(f"wk{week} started a player in two slots")

    change = slate.get("changes")
    if change:
        starting = set(named)
        for who in change["start"]:
            if who not in starting:
                problems.append(f"wk{week} 'start' names {who}, who is not in the lineup")
        for who in change["bench"]:
            if who in starting:
                problems.append(f"wk{week} 'bench' names {who}, who is still starting")

    matchup = tools.get_matchup(key, week, settings=settings)
    if "error" not in matchup:
        expected = round(matchup["me"]["projected"] - matchup["opponent"]["projected"], 2)
        if abs(expected - matchup["projected_margin"]) > 0.02:
            problems.append(f"wk{week} margin {matchup['projected_margin']} != {expected}")
        if not 0.0 <= matchup["win_probability"] <= 1.0:
            problems.append(f"wk{week} win probability out of range")
        if verbose:
            print(
                f"      {DIM}wk{week:>2}  slate {size:>5,}B   "
                f"margin {matchup['projected_margin']:+7.2f}   "
                f"p(win) {matchup['win_probability']:.2f}{RESET}"
            )

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2025)
    ap.add_argument("--weeks", default="1-17")
    ap.add_argument("--league", help="limit to one league key")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    try:
        base = load_settings()
    except ConfigError as exc:
        print(f"{BAD} {exc}")
        return 1

    settings = replace(base, season=args.season)
    leagues = [c for c in settings.leagues if not args.league or c.key == args.league]
    if not leagues:
        print(f"{BAD} no league matching {args.league!r}")
        return 1

    weeks = parse_weeks(args.weeks)
    print(f"{BOLD}Replaying {args.season}{RESET} — {len(leagues)} league(s), weeks {weeks[0]}-{weeks[-1]}\n")

    all_problems: list[str] = []
    grand = {"actual": 0.0, "model": 0.0, "perfect": 0.0, "weeks": 0}

    for cfg in leagues:
        print(f"{BOLD}{cfg.key}{RESET}")
        try:
            lg = get_league(cfg, settings=settings, year=args.season)
            raw = lg.espn_request.get_league().get("settings", {})
        except ESPNError as exc:
            print(f"  {BAD} {str(exc).splitlines()[0]}\n")
            all_problems.append(f"{cfg.key}: could not load {args.season}")
            continue

        scoring = ScoringSettings.from_raw(raw, cfg.key)
        slots = LineupSlots.from_raw(raw.get("rosterSettings", {})).starting_slots
        print(f"  {DIM}{scoring.format_label()} · {len(slots)} starters{RESET}")

        if cfg.team_id is None:
            print(f"  {WARN} no TEAM_ID configured — skipping backtest\n")
            continue

        totals = {"actual": 0.0, "model": 0.0, "perfect": 0.0}
        played = 0

        for week in weeks:
            try:
                boxes = lg.box_scores(week=week)
            except Exception:  # noqa: BLE001 — week not played, or ESPN hiccup
                continue

            mine = None
            for box in boxes:
                if getattr(box.home_team, "team_id", None) == cfg.team_id:
                    mine = box.home_lineup
                    break
                if getattr(box.away_team, "team_id", None) == cfg.team_id:
                    mine = box.away_lineup
                    break
            if not mine or not any(actual(p) for p in mine):
                continue  # bye, or a week that never happened

            result = week_result(mine, slots, scoring)
            for k in totals:
                totals[k] += result[k]
            played += 1

            if args.verbose and result["model"] != result["actual"]:
                delta = result["model"] - result["actual"]
                sign = GREEN if delta > 0 else RED
                print(
                    f"      wk{week:>2}  actual {result['actual']:>6.1f}  "
                    f"model {result['model']:>6.1f}  {sign}{delta:+6.1f}{RESET}  "
                    f"{DIM}{', '.join(result['swaps'][:2])}{RESET}"
                )

            all_problems += [
                f"{cfg.key} {p}" for p in check_tools(cfg.key, week, settings, args.verbose)
            ]

        if played:
            gain = totals["model"] - totals["actual"]
            ceiling = totals["perfect"] - totals["model"]
            colour = GREEN if gain > 0 else RED
            print(
                f"  {played} weeks · you {totals['actual']:.1f} · "
                f"model {totals['model']:.1f} · perfect {totals['perfect']:.1f}"
            )
            print(
                f"  {colour}model vs you: {gain:+.1f} pts total, "
                f"{gain / played:+.2f}/week{RESET}   "
                f"{DIM}(hindsight ceiling a further +{ceiling:.1f}){RESET}\n"
            )
            for k in totals:
                grand[k] += totals[k]
            grand["weeks"] += played
        else:
            print(f"  {WARN} no completed weeks found\n")

    if grand["weeks"]:
        gain = grand["model"] - grand["actual"]
        print(f"{BOLD}Across all leagues{RESET} — {grand['weeks']} team-weeks")
        print(f"  model vs you: {gain:+.1f} pts, {gain / grand['weeks']:+.2f}/week")
        print(
            f"  {DIM}ESPN stores final projections, so the model lineup may benefit\n"
            f"  from late news you would not have had. Read this as an optimistic\n"
            f"  bound on the edge, not a promise.{RESET}"
        )

    print()
    if all_problems:
        print(f"{BAD} {len(all_problems)} problem(s) found on real data:")
        for problem in all_problems[:20]:
            print(f"    {problem}")
        return 1

    print(f"{GREEN}No problems. Every tool behaved correctly on real {args.season} data.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
