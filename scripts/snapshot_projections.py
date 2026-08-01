#!/usr/bin/env python3
"""Freeze this week's projections before kickoff, so the backtest can be trusted.

    uv run scripts/snapshot_projections.py                 # the upcoming week
    uv run scripts/snapshot_projections.py --week 4
    uv run scripts/snapshot_projections.py --diff 3        # what changed since?

**Run this every Saturday.** It takes about ten seconds and it is the only way
to answer a question the historical bake-off cannot.

Here is the problem it exists for. Both external projection sources are fetched
*today*, for weeks that finished long ago, from endpoints that may or may not
restate. The look-ahead audit found Sleeper marking down players who went on to
score nothing, more than any arm that cannot know. That single observation has
three explanations:

1. **The archive restates.** The endpoint is handing back a value revised after
   the games were played. If so, every number Sleeper posted in the bake-off is
   inflated and the arm is disqualified.
2. **The live projection knew Friday's injury report.** Which is not cheating —
   it is precisely what a Sunday-morning projection is supposed to do, and an
   edge worth paying nothing for.
3. **It is better at spotting busts.** Also an edge, also legitimate.

Two of the three are reasons to use the source. One is a reason to throw it out.
No amount of re-analysis of last season separates them, because the evidence
that would separate them was never recorded: *what the endpoint said before the
games.* This script records it.

Then, any time after that week is played:

    uv run scripts/snapshot_projections.py --diff 4

If the archive matches the snapshot, the source is honest and the bake-off
numbers stand. If it has quietly revised the players who got hurt, you will see
exactly which ones and by how much.

Snapshots are small JSON files under ``data/snapshots/`` and are worth
committing — the whole value is that they were written down beforehand.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)
OK, BAD, WARN = f"{GREEN}✓{RESET}", f"{RED}✗{RESET}", f"{YELLOW}!{RESET}"

SNAPSHOT_DIR = Path(__file__).resolve().parents[1] / "data" / "snapshots"
SLEEPER_PROJECTIONS = "https://api.sleeper.com/projections/nfl/{season}/{week}"
POSITIONS = ("QB", "RB", "WR", "TE")

#: A revision this small is rounding or a late stat correction, not a restatement.
NOISE_FLOOR = 0.25


def fetch_sleeper(season: int, week: int) -> dict[str, float]:
    """Normalized name -> PPR projection, as the endpoint reports it right now."""
    import httpx

    from ff_assist.usage import normalize_name

    payload = httpx.get(
        SLEEPER_PROJECTIONS.format(season=season, week=week),
        params={"season_type": "regular", "position[]": list(POSITIONS),
                "order_by": "pts_ppr"},
        timeout=30,
    ).json()

    out: dict[str, float] = {}
    for entry in payload or []:
        player = entry.get("player") or {}
        name = player.get("full_name") or " ".join(
            x for x in (player.get("first_name"), player.get("last_name")) if x
        )
        points = (entry.get("stats") or {}).get("pts_ppr")
        if name and points is not None:
            out[normalize_name(name)] = float(points)
    return out


def path_for(season: int, week: int) -> Path:
    return SNAPSHOT_DIR / f"sleeper_{season}_wk{week:02d}.json"


def games_played(season: int, week: int) -> tuple[int, int] | None:
    """(finished, scheduled) for a week, or None if nflverse is unreachable.

    The diff is meaningless until the games have been played — the whole
    question is whether the provider revises *after* seeing results. Run it the
    same afternoon you take the snapshot and of course nothing has moved, which
    is exactly the false all-clear this function exists to prevent. It has
    already happened once.
    """
    try:
        import nflreadpy as nfl
        import polars as pl

        wk = nfl.load_schedules().filter(
            (pl.col("season") == season) & (pl.col("week") == week)
        )
        if not wk.height:
            return None
        return int(wk["home_score"].is_not_null().sum()), wk.height
    except Exception:  # noqa: BLE001
        return None


def first_kickoff(season: int, week: int) -> date | None:
    """The date of that week's earliest game, or None if nflverse is unreachable."""
    try:
        import nflreadpy as nfl
        import polars as pl

        wk = nfl.load_schedules().filter(
            (pl.col("season") == season) & (pl.col("week") == week)
        )
        if not wk.height:
            return None
        days = sorted(str(d) for d in wk["gameday"].to_list() if d)
        return date.fromisoformat(days[0]) if days else None
    except Exception:  # noqa: BLE001
        return None


#: A snapshot taken further ahead than this measures something else entirely.
FRESH_DAYS = 3


def take(season: int, week: int) -> int:
    target = path_for(season, week)
    kickoff = first_kickoff(season, week)
    lead = (kickoff - datetime.now(UTC).date()).days if kickoff else None

    if lead is not None and lead < 0:
        print(f"{BAD} {season} week {week} kicked off on {kickoff}. Too late.")
        print(f"  {DIM}A snapshot taken after the games cannot test anything.{RESET}")
        return 1

    if target.exists():
        existing = json.loads(target.read_text())
        was = existing.get("days_before_kickoff")
        # Overwriting a *good* snapshot destroys the only copy of the pre-kickoff
        # value, which is the whole asset. But a snapshot taken weeks early is
        # not that asset — it is a camp-news baseline — and replacing it with a
        # closer one is strictly an improvement.
        stale = was is None or was > FRESH_DAYS
        closer = lead is not None and was is not None and lead < was
        if not (stale and closer):
            print(f"{BAD} {target.name} already exists — refusing to overwrite.")
            print(f"  {DIM}The value of a snapshot is that it was written down before the")
            print(f"  games. Re-taking it later would silently destroy that.{RESET}")
            return 1
        print(f"{WARN} replacing a snapshot taken {was} days before kickoff with one "
              f"taken {lead} days before.")

    if lead is not None and lead > FRESH_DAYS:
        print(f"{WARN} {BOLD}{kickoff} is {lead} days away.{RESET}")
        print(f"  {DIM}Taking it now still works, but it is a weak test: Sleeper will")
        print("  legitimately revise these numbers all through camp, and that movement")
        print(f"  swamps the signal you are looking for. Come back within {FRESH_DAYS} days")
        print(f"  of kickoff and run this again — it will replace this file.{RESET}")

    try:
        projections = fetch_sleeper(season, week)
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD} Sleeper unreachable: {type(exc).__name__}")
        print(f"  {DIM}This 403s from datacenter addresses. Run it from a laptop.{RESET}")
        return 1

    if not projections:
        print(f"{WARN} Sleeper returned nothing for {season} week {week} — too early?")
        return 1

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "season": season,
                "week": week,
                "taken_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "days_before_kickoff": lead,
                "source": SLEEPER_PROJECTIONS.format(season=season, week=week),
                "projections": projections,
            },
            indent=1,
            sort_keys=True,
        )
    )
    top = sorted(projections.items(), key=lambda kv: -kv[1])[:3]
    print(f"{OK} {len(projections):,} projections frozen -> {target.name}")
    print(f"  {DIM}top: " + ", ".join(f"{n} {v:.1f}" for n, v in top) + RESET)
    print(f"  {DIM}commit this file. After the week is played: --diff {week}{RESET}")
    return 0


def diff(season: int, week: int) -> int:
    source = path_for(season, week)
    if not source.exists():
        print(f"{BAD} no snapshot for {season} week {week}.")
        print(f"  {DIM}Nothing to compare against — the pre-kickoff value was never")
        print(f"  recorded. That is the situation this script exists to prevent.{RESET}")
        return 1

    saved = json.loads(source.read_text())
    before: dict[str, float] = saved["projections"]

    # Gate the whole comparison on the games having happened. Without this the
    # script cheerfully reports "the archive is honest" for a week that has not
    # kicked off, which is not a weaker version of the finding — it is no
    # finding at all, dressed as one.
    played = games_played(season, week)
    if played is not None and played[0] == 0:
        print(f"{WARN} {BOLD}{season} week {week} has not been played "
              f"(0 of {played[1]} games final).{RESET}")
        print(f"  {DIM}Nothing to learn yet. The question is whether the provider revises")
        print("  its numbers *after* seeing the results, so a diff taken before kickoff")
        print("  can only ever say 'unchanged' — which would be a false all-clear.")
        print(f"  Come back on the Monday.{RESET}")
        return 0
    if played is not None and played[0] < played[1]:
        print(f"{WARN} only {played[0]} of {played[1]} games are final — "
              f"partial week, read the diff as provisional.\n")

    lead = saved.get("days_before_kickoff")
    if lead is None or lead > FRESH_DAYS:
        ago = "an unknown time" if lead is None else f"{lead} days"
        print(f"{WARN} this snapshot was taken {ago} before kickoff.")
        print(f"  {DIM}Most of what moved will be ordinary revision — depth charts, camp")
        print("  news, injuries that resolved — not hindsight. Read the *shape* of the")
        print(f"  movement below, not its size.{RESET}\n")

    try:
        after = fetch_sleeper(season, week)
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD} Sleeper unreachable: {type(exc).__name__}")
        return 1

    shared = sorted(set(before) & set(after))
    if not shared:
        print(f"{BAD} no overlapping players — the response shape probably changed.")
        return 1

    moved = [
        (name, before[name], after[name], after[name] - before[name])
        for name in shared
        if abs(after[name] - before[name]) > NOISE_FLOOR
    ]

    print(f"{BOLD}Snapshot vs archive — {season} week {week}{RESET}")
    print(f"  {DIM}frozen {saved['taken_at']}, {len(shared):,} players in both{RESET}\n")

    if not moved:
        if played is None:
            print(f"{WARN} Nothing moved — but nflverse was unreachable, so I could not")
            print(f"  {DIM}confirm the games have been played. If they have not, this is")
            print(f"  not a result. Check and re-run.{RESET}")
            return 0
        print(f"{OK} {BOLD}The archive is honest.{RESET} Not one projection moved by more")
        print(f"  than {NOISE_FLOOR} points, with {played[0]}/{played[1]} games final. The")
        print("  historical bake-off can be taken at face value, and Sleeper's edge in")
        print("  it is a real, forward-looking edge.")
        return 0

    down = [m for m in moved if m[3] < 0]
    print(f"{WARN} {len(moved):,} of {len(shared):,} projections changed "
          f"({len(moved) / len(shared):.1%}), {len(down):,} of them downward.")
    for name, was, now, delta in sorted(moved, key=lambda m: m[3])[:12]:
        colour = RED if delta < 0 else GREEN
        print(f"    {name:<28}{was:>7.1f} -> {now:>6.1f}  {colour}{delta:+.1f}{RESET}")
    if len(moved) > 12:
        print(f"    {DIM}... and {len(moved) - 12:,} more{RESET}")

    # The shape of the revision is the diagnosis. Wholesale downward marking is
    # the signature of an archive that has absorbed who did not play.
    big_down = [m for m in down if m[3] < -3.0]
    print()
    if len(down) > len(moved) * 0.8 and big_down:
        print(f"{BAD} {BOLD}Restated after the fact.{RESET} The revisions are overwhelmingly")
        print(f"  downward, with {len(big_down):,} of more than 3 points. Sleeper's numbers in")
        print("  the historical bake-off are inflated by hindsight and the arm should be")
        print("  dropped from the ranking. Live use is unaffected — a projection fetched")
        print("  on Saturday is still a projection.")
        return 1
    print(f"{WARN} Movement is mixed rather than one-directional, which looks more like")
    print("  ordinary revision than hindsight. Worth a second week before concluding.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--week", type=int, help="which week to freeze (default: guess)")
    ap.add_argument("--diff", type=int, metavar="WEEK",
                    help="compare a stored snapshot against the archive now")
    args = ap.parse_args()

    if args.diff:
        return diff(args.season, args.diff)

    week = args.week
    if week is None:
        # No clever date maths: guessing wrong here writes an unoverwritable
        # file for the wrong week. Ask for it.
        print(f"{WARN} pass --week explicitly. Which week are you freezing?")
        return 1
    return take(args.season, week)


if __name__ == "__main__":
    raise SystemExit(main())
