#!/usr/bin/env python3
"""Prove the third-party data sources are reachable — from wherever you run it.

    uv run scripts/check_external.py              # everything
    uv run scripts/check_external.py --quick      # fast subset, good for Fly
    uv run scripts/check_external.py --sleeper    # just the waiver overlay chain
    uv run scripts/check_external.py --weather    # just the venue table

**Run it in both places.** Your laptop and the deployed machine are different
network environments, and the one that matters for scheduled tasks is the
deployed one:

    uv run scripts/check_external.py --quick                       # your machine
    fly ssh console -C "/app/.venv/bin/python /app/scripts/check_external.py --quick"

That distinction is not theoretical. Cloudflare in front of claude.ai answers
403 to requests from Fly datacenter addresses while the same request succeeds
from a laptop — that is a real bug this project already hit. Sleeper and
Open-Meteo may behave the same way, and finding out at 8am on a Tuesday in
September is the outcome worth avoiding.

Each hop is checked separately so a failure says *which* one broke rather than
just "the waiver overlay is empty".
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist.weather import OPEN_METEO_URL, VENUES, wind_verdict  # noqa: E402

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)
OK, BAD, WARN = f"{GREEN}✓{RESET}", f"{RED}✗{RESET}", f"{YELLOW}!{RESET}"

SLEEPER_TRENDING = "https://api.sleeper.app/v1/players/nfl/trending/add"
SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl"


def hop(name: str, detail: str = "", ok: bool = True, warn: bool = False) -> bool:
    mark = OK if ok else (WARN if warn else BAD)
    print(f"  {mark} {name:<34} {DIM if ok else ''}{detail}{RESET}")
    return ok


# ---------------------------------------------------------------------------
# Open-Meteo
# ---------------------------------------------------------------------------


def check_weather(quick: bool) -> list[str]:
    print(f"\n{BOLD}Open-Meteo{RESET} {DIM}(weather — no API key, 10k calls/day){RESET}")
    import httpx

    failures: list[str] = []
    venues = list(VENUES.items())
    if quick:
        # One open-air, one southern hemisphere: enough to prove reachability
        # and that a sign error would be caught.
        keep = ("Highmark Stadium", "Melbourne Cricket Ground")
        venues = [(n, v) for n, v in venues if n in keep]

    probe = venues[0][1]
    try:
        t = time.monotonic()
        r = httpx.get(
            OPEN_METEO_URL,
            params={"latitude": probe.lat, "longitude": probe.lon, "hourly": "wind_speed_10m"},
            timeout=20,
        )
        ms = (time.monotonic() - t) * 1000
        if r.status_code != 200:
            hop("reachable", f"HTTP {r.status_code} — likely blocked from this network", ok=False)
            return [f"open-meteo HTTP {r.status_code}"]
        hop("reachable", f"HTTP 200 in {ms:.0f}ms")
    except Exception as exc:  # noqa: BLE001
        hop("reachable", f"{type(exc).__name__}: {exc}"[:70], ok=False)
        return [f"open-meteo unreachable: {type(exc).__name__}"]

    print(f"  {DIM}checking {len(venues)} venue coordinate(s) by timezone…{RESET}")
    for name, venue in venues:
        try:
            data = httpx.get(
                OPEN_METEO_URL,
                params={
                    "latitude": venue.lat,
                    "longitude": venue.lon,
                    "hourly": "wind_speed_10m,temperature_2m",
                    "wind_speed_unit": "mph",
                    "temperature_unit": "fahrenheit",
                    "timezone": "auto",
                    "forecast_days": 1,
                },
                timeout=20,
            ).json()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}")
            hop(name[:32], f"{type(exc).__name__}", ok=False)
            continue

        resolved = data.get("timezone")
        wind = (data.get("hourly", {}).get("wind_speed_10m") or [None])[0]
        temp = (data.get("hourly", {}).get("temperature_2m") or [None])[0]

        if resolved != venue.tz:
            failures.append(f"{name}: expected {venue.tz}, got {resolved}")
            hop(name[:32], f"timezone {resolved} != {venue.tz}", ok=False)
            continue
        if wind is not None and not (0 <= wind <= 80):
            failures.append(f"{name}: implausible wind {wind}")
            hop(name[:32], f"wind {wind} mph implausible", ok=False)
            continue
        hop(name[:32], f"{resolved} · {wind} mph, {temp}F")
        if not quick:
            time.sleep(0.12)

    if not failures:
        print(f"  {DIM}wind thresholds: notable >=15, severe >=20 · "
              f"e.g. {wind_verdict(22.0)}{RESET}")
    return failures


# ---------------------------------------------------------------------------
# Sleeper + the id crosswalk
# ---------------------------------------------------------------------------


def check_sleeper() -> list[str]:
    print(f"\n{BOLD}Sleeper trending adds{RESET} "
          f"{DIM}(waiver overlay — no auth, 90 req/min){RESET}")
    import httpx

    failures: list[str] = []

    # Hop 1 — the trending endpoint itself.
    try:
        t = time.monotonic()
        r = httpx.get(SLEEPER_TRENDING, params={"limit": 10, "lookback_hours": 24}, timeout=15)
        ms = (time.monotonic() - t) * 1000
        if r.status_code != 200:
            hop("trending endpoint", f"HTTP {r.status_code}", ok=False)
            return [f"sleeper trending HTTP {r.status_code}"]
        raw = r.json() or []
        hop("trending endpoint", f"HTTP 200 in {ms:.0f}ms · {len(raw)} players")
    except Exception as exc:  # noqa: BLE001
        hop("trending endpoint", f"{type(exc).__name__}: {exc}"[:70], ok=False)
        return [f"sleeper unreachable: {type(exc).__name__}"]

    if not raw:
        hop("has data", "empty list — normal in the deep offseason", warn=True, ok=True)
        return []

    # Hop 2 — the id crosswalk. This one is fragile: it lives on a different
    # host from the nflverse releases and has 403'd from at least one
    # datacenter network.
    ids_ok = False
    try:
        import nflreadpy as nfl
        import polars as pl

        t = time.monotonic()
        ids = nfl.load_ff_playerids()
        ms = (time.monotonic() - t) * 1000
        have = ids.filter(pl.col("sleeper_id").is_not_null()).height
        hop("nflverse id crosswalk", f"{have:,} sleeper ids in {ms:.0f}ms")
        ids_ok = True
    except Exception as exc:  # noqa: BLE001
        hop(
            "nflverse id crosswalk",
            f"{type(exc).__name__} — will fall back to Sleeper's own dictionary",
            warn=True,
            ok=True,
        )

    # Hop 3 — the fallback, so we know at least one path resolves names.
    if not ids_ok:
        try:
            t = time.monotonic()
            r = httpx.get(SLEEPER_PLAYERS, timeout=90)
            ms = (time.monotonic() - t) * 1000
            size = len(r.content) / 1e6
            if r.status_code != 200:
                hop("sleeper player dictionary", f"HTTP {r.status_code}", ok=False)
                failures.append("no id source available — overlay cannot resolve names")
            else:
                hop("sleeper player dictionary", f"{size:.1f}MB in {ms:.0f}ms")
        except Exception as exc:  # noqa: BLE001
            hop("sleeper player dictionary", f"{type(exc).__name__}", ok=False)
            failures.append("no id source available — overlay cannot resolve names")

    # Hop 4 — the whole thing, as the waiver board actually calls it.
    from ff_assist.waivers import trending_adds

    t = time.monotonic()
    names = trending_adds(limit=25)
    ms = (time.monotonic() - t) * 1000
    if not names:
        hop(
            "end to end",
            "returned {} — the board still works, just without the contest signal",
            warn=True,
            ok=True,
        )
        failures.append("trending overlay resolves to nothing")
    else:
        sample = sorted(names.items(), key=lambda kv: -kv[1])[:3]
        hop("end to end", f"{len(names)} players in {ms:.0f}ms")
        # Shown in normalized form because that is the key the waiver board
        # joins on — a pretty name here would hide a join that is subtly off.
        joined = ", ".join(f"{n} ({c:,})" for n, c in sample)
        print(f"    {DIM}most added (join keys): {joined}{RESET}")
        print(f"    {DIM}note: the ESPN side of this join is only exercised once a")
        print(f"    free-agent pool exists — re-run after your draft.{RESET}")

    return failures


# ---------------------------------------------------------------------------


def check_projections(season: int, week: int, diagnose: bool = False) -> list[str]:
    """The second opinion the slate now ranks on. Load-bearing, unlike the rest
    of this file — if it is unreachable the lineup falls back to ESPN alone,
    which is a measurably worse answer rather than a missing footnote."""
    print(f"\n{BOLD}Sleeper projections{RESET} {DIM}(the second opinion in every slate){RESET}")

    from ff_assist.projections import fetch_week, infer_scoring, verify_crosswalk

    t = time.monotonic()
    week_data = fetch_week(season, week)
    ms = (time.monotonic() - t) * 1000

    if not week_data:
        hop(
            "reachable",
            "empty — blocked here, or the week is not published yet",
            warn=True,
            ok=True,
        )
        return ["sleeper projections unavailable — slates fall back to ESPN alone"]
    hop(f"{season} week {week}", f"{week_data.coverage} players usable, {ms:.0f}ms")

    # The guard that runs on the live path, not just here. Players whose
    # published total we cannot rebuild are dropped and fall back to ESPN.
    if week_data.rejected:
        rate = len(week_data.rejected) / (len(week_data.rejected) + len(week_data.lines))
        hop(
            "reproducible from stats",
            f"{len(week_data.rejected)} dropped ({rate:.0%}), "
            f"mean residual {week_data.mean_residual}",
            warn=True,
            ok=True,
        )
        worst_name, (ours, theirs) = max(
            week_data.rejected.items(), key=lambda kv: abs(kv[1][0] - kv[1][1])
        )
        print(f"    {DIM}worst: {worst_name} — we rebuild {ours}, Sleeper says {theirs}")
        print(f"    those players get no second opinion; the rest are unaffected.{RESET}")
    else:
        hop("reproducible from stats", "every player rebuilt from their own stat line")

    # The crosswalk is falsifiable against a number we did not compute: Sleeper
    # publishes its own PPR total alongside the components. If our translation
    # of the components does not reproduce it, some category is being dropped
    # and every projection built from it is wrong by an unknown amount.
    report = verify_crosswalk(week_data)
    failures: list[str] = []
    if report.get("failed"):
        worst = report.get("worst", {})
        hop(
            "stat crosswalk",
            f"{report['failed']}/{report['checked']} disagree with Sleeper's own total",
            ok=False,
        )
        if worst:
            print(f"    {DIM}worst: {worst['player']} — we say {worst['ours']}, "
                  f"Sleeper says {worst['sleeper']}{RESET}")
        failures.append("sleeper stat crosswalk is wrong — projections are understated")
    else:
        hop("stat crosswalk", f"{report['checked']} players match Sleeper's own PPR total")

    if diagnose:
        # Which stat key, exactly. Least squares over Sleeper's own published
        # totals recovers the per-unit value of every stat it returns, so the
        # answer is read off rather than guessed at across several rounds.
        print(f"\n  {BOLD}Inferred scoring{RESET} "
              f"{DIM}(solved from Sleeper's own totals){RESET}")
        inferred = infer_scoring(week_data)
        if "error" in inferred:
            print(f"    {WARN} {inferred['error']}")
        else:
            print(f"    {DIM}fitted over {inferred['players']} players{RESET}")
            missing = inferred["unmapped_but_scored"]
            wrong = inferred["mapped_but_mispriced"]
            if missing:
                print(f"    {RED}stat keys Sleeper scores that we do not map:{RESET}")
                for k, v in sorted(missing.items(), key=lambda kv: -abs(kv[1])):
                    print(f"      {k:<24}{v:>8.3f} pts/unit")
            if wrong:
                print(f"    {RED}keys whose assumed price is wrong "
                      f"(our reference ruleset, not the crosswalk):{RESET}")
                for k, v in wrong.items():
                    print(f"      {k:<24}assumed {v['assumed']:>6.2f} "
                          f"-> fitted {v['fitted']:>6.2f}")
            if not missing and not wrong:
                print(f"    {OK} nothing unexplained — the crosswalk and the "
                      f"reference ruleset both agree with Sleeper")

    if report.get("unmapped"):
        hop(
            "stat coverage",
            f"{len(report['unmapped'])} unknown keys: {', '.join(report['unmapped'][:5])}",
            warn=True,
            ok=True,
        )
        print(f"    {DIM}unknown keys score zero. Add them to SLEEPER_TO_NFLVERSE.{RESET}")
    else:
        hop("stat coverage", "every returned stat key is mapped or knowingly ignored")

    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true", help="two venues instead of all 39")
    ap.add_argument("--sleeper", action="store_true", help="only the Sleeper chain")
    ap.add_argument("--weather", action="store_true", help="only Open-Meteo")
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--week", type=int, default=1)
    ap.add_argument("--diagnose", action="store_true",
                    help="solve for the scoring rule Sleeper's own totals imply")
    args = ap.parse_args()

    do_weather = args.weather or not args.sleeper
    do_sleeper = args.sleeper or not args.weather

    try:
        import httpx  # noqa: F401
    except ImportError:
        print(f"{BAD} httpx missing — run: uv sync")
        return 1

    print(f"{BOLD}External data source check{RESET}")
    print(f"{DIM}Run this on your machine AND on the deployed host — they are")
    print(f"different networks, and the deployed one is what scheduled tasks use.{RESET}")

    failures: list[str] = []
    if do_weather:
        failures += check_weather(args.quick)
    if do_sleeper:
        failures += check_sleeper()
        failures += check_projections(args.season, args.week, args.diagnose)

    print()
    if failures:
        print(f"{BAD} {len(failures)} problem(s):")
        for f in failures:
            print(f"    {f}")
        print(
            f"\n{DIM}Weather is a tie-breaker and the trending overlay only gauges how\n"
            f"contested a claim is; both degrade to absent and say so. Sleeper's\n"
            f"projections are different — the slate ranks on the mean of ESPN and\n"
            f"Sleeper, so losing them is a measurably worse lineup rather than a\n"
            f"missing footnote. Knowing now beats discovering it in week 1.{RESET}"
        )
        return 1

    print(f"{GREEN}All external sources reachable from here.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
