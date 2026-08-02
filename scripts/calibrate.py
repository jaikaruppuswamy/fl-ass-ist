#!/usr/bin/env python3
"""Score this system against what actually happened, week by week.

    uv run scripts/calibrate.py --record 3      # Saturday, before kickoff
    uv run scripts/calibrate.py --score 3       # Tuesday, after the games
    uv run scripts/calibrate.py --report        # any time

Phase 5 of the plan, and the part almost nobody does. A backtest answers "would
this have helped last season"; only this answers "is it helping now". The
difference between a system that improves and one that merely sounds confident.

**Why it needs two steps.** The plan assumed the Tuesday postmortem could look
back and score the week. It cannot, for two reasons that only became obvious
once the rest was built:

* The recommendations were made in a *conversation*. Nothing persists them, and
  a scheduled task starting from nothing cannot read what was said on Sunday.
* ESPN overwrites its projections with finals, so "what did we project" is
  unrecoverable after kickoff. The same problem that made the projection
  bake-off an optimistic bound.

So the recommendation has to be frozen *before* the games, exactly like
``snapshot_projections.py`` freezes Sleeper's numbers, and for the same reason:
the evidence that settles the question is only available beforehand.

**What gets recorded.** For every league, the whole slate as it stood: the
current lineup, the optimal lineup, and each specific swap with the points it
was supposed to gain. Afterwards, each of those is scored against what the
players actually did in that league's own scoring.

**What honesty requires.** ``--score`` refuses to run on a week that has no
frozen record, rather than reconstructing one — a reconstructed recommendation
is a guess about the past dressed as a measurement, and this project has been
bitten by that shape of mistake more than once. It also refuses to score a week
whose games have not finished.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)
OK, BAD, WARN = f"{GREEN}✓{RESET}", f"{RED}✗{RESET}", f"{YELLOW}!{RESET}"

CALIB_DIR = Path(__file__).resolve().parents[1] / "data" / "calibration"
LOG = CALIB_DIR / "log.csv"

#: The plan's schema, plus the three fields it turned out to need: the season
#: (so two years of log do not silently merge), when the recommendation was
#: frozen (so a late freeze is visible), and the source basis (so a week where
#: Sleeper was down is not compared against one where it was up).
FIELDS = (
    "season", "week", "league", "decision_type", "recommended", "alternative",
    "projected_delta", "actual_delta", "was_right", "primary_reason",
    "frozen_at", "projection_basis",
)


def record_path(season: int, week: int) -> Path:
    return CALIB_DIR / f"recommendations_{season}_wk{week:02d}.json"


# ---------------------------------------------------------------------------
# Freeze
# ---------------------------------------------------------------------------


def primary_reason(row: dict[str, Any]) -> str:
    """The one thing most likely to have driven this call.

    Ordered by how much the evidence says each signal is worth, so a monthly
    review can ask "are the calls I make for reason X actually right more
    often" — which is the whole point of keeping the column.
    """
    if row.get("disagree") is not None:
        return f"sources split {row['disagree']:+}"
    if row.get("injury"):
        return f"injury {row['injury']}"
    if row.get("usage_trend") in ("rising", "falling"):
        return f"usage {row['usage_trend']}"
    if row.get("implied_total") is not None:
        return f"implied {row['implied_total']}"
    if row.get("pts_allowed_rank") is not None:
        return f"matchup rank {row['pts_allowed_rank']}"
    return "projection only"


def record(season: int, week: int) -> int:
    """Freeze every league's slate before the games."""
    from snapshot_projections import games_played

    from ff_assist import tools
    from ff_assist.config import ConfigError, load_settings

    target = record_path(season, week)
    if target.exists():
        print(f"{BAD} {target.name} already exists — refusing to overwrite.")
        print(f"  {DIM}A recommendation is only evidence if it predates the games.")
        print(f"  Re-freezing later would quietly replace it with hindsight.{RESET}")
        return 1

    played = games_played(season, week)
    if played and played[0]:
        print(f"{BAD} {played[0]} of {played[1]} games are already final.")
        print(f"  {DIM}Too late to freeze a prediction for this week.{RESET}")
        return 1

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"{BAD} {exc}")
        return 1

    leagues: dict[str, Any] = {}
    for cfg in settings.leagues:
        slate = tools.get_start_sit_slate(cfg.key, week, settings=settings)
        if "error" in slate:
            print(f"  {WARN} {cfg.key}: {slate['error']}")
            continue
        leagues[cfg.key] = slate
        change = slate.get("changes")
        moves = len(change["start"]) if change else 0
        print(f"  {OK} {cfg.key:<12} {len(slate['optimal_lineup'])} slots, "
              f"{moves} change(s), {slate['projection_basis']}")

    if not leagues:
        print(f"{BAD} no league produced a slate — nothing frozen.")
        return 1

    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "season": season,
                "week": week,
                "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "leagues": leagues,
            },
            indent=1,
        )
    )
    print(f"\n{OK} frozen -> {target.name}. Commit it, then --score {week} "
          f"once the week is final.")
    return 0


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------


def actual_points(cfg: Any, season: int, week: int, settings: Any) -> dict[str, float]:
    """name -> points actually scored, in that league's own rules."""
    from ff_assist.espn_client import get_league

    league = get_league(cfg, settings=settings, year=season)
    out: dict[str, float] = {}
    for box in league.box_scores(week=week):
        for player in list(box.home_lineup) + list(box.away_lineup):
            out[player.name] = float(getattr(player, "points", 0.0) or 0.0)
    return out


def score(season: int, week: int) -> int:
    """Grade the frozen recommendations against what happened."""
    from snapshot_projections import games_played

    from ff_assist.config import ConfigError, load_settings

    source = record_path(season, week)
    if not source.exists():
        print(f"{BAD} no frozen recommendation for {season} week {week}.")
        print(f"  {DIM}Nothing to score. Reconstructing one now would be a guess")
        print("  about the past dressed as a measurement — run --record next")
        print(f"  Saturday instead.{RESET}")
        return 1

    played = games_played(season, week)
    if played and played[0] < played[1]:
        print(f"{WARN} only {played[0]} of {played[1]} games are final — "
              f"scoring now would be partial.")
        if played[0] == 0:
            return 1

    saved = json.loads(source.read_text())
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"{BAD} {exc}")
        return 1

    rows: list[dict[str, Any]] = []
    for cfg in settings.leagues:
        slate = saved["leagues"].get(cfg.key)
        if slate is None:
            continue
        try:
            actual = actual_points(cfg, season, week, settings)
        except Exception as exc:  # noqa: BLE001
            print(f"  {WARN} {cfg.key}: cannot read results ({type(exc).__name__})")
            continue

        by_name = {
            r["name"]: r
            for r in slate["optimal_lineup"] + slate["bench"]
            if r.get("name")
        }
        base = {
            "season": season,
            "week": week,
            "league": cfg.key,
            "frozen_at": saved["frozen_at"],
            "projection_basis": slate.get("projection_basis", ""),
        }

        change = slate.get("changes")
        if change and change["start"]:
            # Pair each promotion with a demotion. They are not strictly
            # one-to-one when several slots move at once, so pairing by rank
            # keeps the comparison honest without inventing a mapping.
            ins = sorted(change["start"], key=lambda n: -by_name.get(n, {}).get("proj", 0))
            outs = sorted(change["bench"], key=lambda n: -by_name.get(n, {}).get("proj", 0))
            for i, name in enumerate(ins):
                other = outs[i] if i < len(outs) else None
                if other is None or name not in actual or other not in actual:
                    continue
                projected = round(
                    by_name.get(name, {}).get("proj", 0.0)
                    - by_name.get(other, {}).get("proj", 0.0), 2
                )
                delta = round(actual[name] - actual[other], 2)
                rows.append({
                    **base,
                    "decision_type": "swap",
                    "recommended": name,
                    "alternative": other,
                    "projected_delta": projected,
                    "actual_delta": delta,
                    "was_right": int(delta > 0),
                    "primary_reason": primary_reason(by_name.get(name, {})),
                })
        else:
            # "Leave it alone" is a decision and has to be scored too, or the
            # log only ever measures the weeks the system spoke up.
            started = [r["name"] for r in slate["optimal_lineup"] if r.get("name")]
            benched = [r["name"] for r in slate["bench"] if r.get("name")]
            if started and benched:
                best_bench = max(benched, key=lambda n: actual.get(n, 0.0))
                worst_start = min(started, key=lambda n: actual.get(n, 0.0))
                if best_bench in actual and worst_start in actual:
                    delta = round(actual[worst_start] - actual[best_bench], 2)
                    rows.append({
                        **base,
                        "decision_type": "hold",
                        "recommended": worst_start,
                        "alternative": best_bench,
                        "projected_delta": round(
                            by_name.get(worst_start, {}).get("proj", 0.0)
                            - by_name.get(best_bench, {}).get("proj", 0.0), 2
                        ),
                        "actual_delta": delta,
                        "was_right": int(delta > 0),
                        "primary_reason": "lineup already optimal",
                    })

    if not rows:
        print(f"{WARN} nothing scoreable — no changes were recommended and no "
              f"results resolved.")
        return 0

    fresh = not LOG.exists()
    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if fresh:
            writer.writeheader()
        writer.writerows(rows)

    right = sum(r["was_right"] for r in rows)
    print(f"{OK} scored {len(rows)} decision(s), {right} right "
          f"({right / len(rows):.0%}) -> {LOG.name}")
    for row in rows:
        mark = GREEN if row["was_right"] else RED
        print(f"    {row['league']:<12}{row['decision_type']:<6}"
              f"{row['recommended'][:18]:<19}over {row['alternative'][:18]:<19}"
              f"{mark}{row['actual_delta']:+7.1f}{RESET}"
              f" {DIM}(said {row['projected_delta']:+.1f}){RESET}")
    return 0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def report() -> int:
    if not LOG.exists():
        print(f"{WARN} no log yet. Run --record on a Saturday and --score after.")
        return 0

    with LOG.open() as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        print(f"{WARN} log is empty.")
        return 0

    def summarise(label: str, subset: list[dict[str, str]]) -> None:
        if not subset:
            return
        right = sum(int(r["was_right"]) for r in subset)
        gained = sum(float(r["actual_delta"]) for r in subset)
        said = sum(float(r["projected_delta"]) for r in subset)
        band = 1.96 * (0.25 / len(subset)) ** 0.5
        flag = "" if abs(right / len(subset) - 0.5) > band else f" {DIM}(within noise){RESET}"
        print(f"  {label:<26}{len(subset):>4} calls{right / len(subset):>8.0%} right"
              f"{gained:>+9.1f} pts{DIM} vs {said:+.1f} predicted{RESET}{flag}")

    print(f"{BOLD}Calibration — {len(rows)} decisions{RESET}")
    weeks = sorted({(r['season'], r['week']) for r in rows})
    print(f"{DIM}{len(weeks)} week(s), {weeks[0][0]} wk{weeks[0][1]} to "
          f"{weeks[-1][0]} wk{weeks[-1][1]}{RESET}\n")

    summarise("all decisions", rows)
    print()
    for kind in sorted({r["decision_type"] for r in rows}):
        summarise(f"  {kind}", [r for r in rows if r["decision_type"] == kind])
    print()
    for league in sorted({r["league"] for r in rows}):
        summarise(f"  {league}", [r for r in rows if r["league"] == league])
    print()
    for reason in sorted({r["primary_reason"].split()[0] for r in rows}):
        summarise(f"  {reason}…", [r for r in rows if r["primary_reason"].startswith(reason)])

    deltas = [float(r["actual_delta"]) - float(r["projected_delta"]) for r in rows]
    print(f"\n{BOLD}Is the projection honest?{RESET}")
    print(f"  mean(actual - predicted) = {statistics.mean(deltas):+.2f} points")
    if len(deltas) > 1:
        print(f"  {DIM}spread {statistics.stdev(deltas):.1f}. A mean far from zero means "
              f"the projected\n  gains are systematically optimistic or timid; the spread "
              f"is how much\n  any single week tells you, which is almost nothing.{RESET}")
    print(
        f"\n  {DIM}A coin flip is 50%. With {len(rows)} calls the 95% band is "
        f"±{1.96 * (0.25 / len(rows)) ** 0.5:.0%}, so treat\n"
        f"  anything inside it as unmeasured rather than as working. Six weeks is\n"
        f"  about the earliest this says anything at all.{RESET}"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--record", type=int, metavar="WEEK",
                    help="freeze every league's slate before kickoff")
    ap.add_argument("--score", type=int, metavar="WEEK",
                    help="grade a frozen week against the results")
    ap.add_argument("--report", action="store_true", help="summarise the log")
    args = ap.parse_args()

    if args.record:
        return record(args.season, args.record)
    if args.score:
        return score(args.season, args.score)
    if args.report:
        return report()
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
