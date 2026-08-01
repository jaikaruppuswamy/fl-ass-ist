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
        venues = [(n, v) for n, v in venues if n in ("Highmark Stadium", "Melbourne Cricket Ground")]

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
        print(f"  {DIM}wind thresholds: notable >=15, severe >=20 · e.g. {wind_verdict(22.0)}{RESET}")
    return failures


# ---------------------------------------------------------------------------
# Sleeper + the id crosswalk
# ---------------------------------------------------------------------------


def check_sleeper() -> list[str]:
    print(f"\n{BOLD}Sleeper trending adds{RESET} {DIM}(waiver overlay — no auth, 90 req/min){RESET}")
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
        print(f"    {DIM}most added: " + ", ".join(f"{n} ({c:,})" for n, c in sample) + RESET)

    return failures


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true", help="two venues instead of all 39")
    ap.add_argument("--sleeper", action="store_true", help="only the Sleeper chain")
    ap.add_argument("--weather", action="store_true", help="only Open-Meteo")
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

    print()
    if failures:
        print(f"{BAD} {len(failures)} problem(s):")
        for f in failures:
            print(f"    {f}")
        print(
            f"\n{DIM}Neither source is load-bearing: weather is a tie-breaker and the\n"
            f"trending overlay only gauges how contested a claim is. Both degrade to\n"
            f"absent and say so. But knowing now beats discovering it in week 1.{RESET}"
        )
        return 1

    print(f"{GREEN}All external sources reachable from here.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
