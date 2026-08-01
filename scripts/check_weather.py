#!/usr/bin/env python3
"""Validate the stadium coordinate table against live Open-Meteo.

    uv run scripts/check_weather.py

Run this on your Mac — the cloud sandbox this was written in cannot reach
Open-Meteo, so the table has never been checked against the real API.

For every venue it asks Open-Meteo for a forecast and compares the timezone
the API resolves from our latitude/longitude against the timezone we expect.
A transposed sign, a swapped lat/lon, or a stadium placed in the wrong city
all surface as a timezone mismatch — which is a far sharper check than eyeballing
decimal degrees. It also fetches one real forecast so unit handling is exercised.

Exit code is non-zero if any venue fails, so this is safe to wire into the
Tuesday smoke test later.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist.weather import OPEN_METEO_URL, VENUES, wind_verdict  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
OK, BAD, WARN = f"{GREEN}✓{RESET}", f"{RED}✗{RESET}", f"{YELLOW}!{RESET}"


def main() -> int:
    try:
        import httpx
    except ImportError:
        print(f"{BAD} httpx missing. Run: uv sync")
        return 1

    failures: list[str] = []
    print(f"{DIM}checking {len(VENUES)} venues against Open-Meteo…{RESET}\n")

    for name, venue in sorted(VENUES.items()):
        params = {
            "latitude": venue.lat,
            "longitude": venue.lon,
            "hourly": "wind_speed_10m,precipitation_probability,temperature_2m",
            "wind_speed_unit": "mph",
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
            "forecast_days": 1,
        }
        try:
            data = httpx.get(OPEN_METEO_URL, params=params, timeout=20).json()
        except Exception as exc:  # noqa: BLE001
            failures.append(name)
            print(f"{BAD} {name:<34} request failed: {type(exc).__name__}")
            continue

        resolved = data.get("timezone")
        wind = (data.get("hourly", {}).get("wind_speed_10m") or [None])[0]
        temp = (data.get("hourly", {}).get("temperature_2m") or [None])[0]

        if resolved != venue.tz:
            failures.append(name)
            print(
                f"{BAD} {name:<34} timezone mismatch: expected {venue.tz}, "
                f"Open-Meteo resolved {resolved} from ({venue.lat}, {venue.lon})"
            )
            continue

        roof = {True: "open air", False: "roofed", None: "retractable"}[venue.open_air]
        note = ""
        if wind is not None and (wind > 60 or wind < 0):
            note = f"  {WARN} implausible wind {wind}"
            failures.append(name)
        if temp is not None and not (-40 <= temp <= 130):
            note = f"  {WARN} implausible temp {temp}F"
            failures.append(name)
        print(f"{OK} {name:<34} {resolved:<28} {roof:<12} now: {wind} mph, {temp}F{note}")
        time.sleep(0.15)  # stay well inside the 10k/day free allowance

    print()
    if failures:
        print(f"{BAD} {len(failures)} venue(s) failed: {', '.join(sorted(set(failures)))}")
        print(f"{DIM}Fix the coordinates in src/ff_assist/weather.py and re-run.{RESET}")
        return 1

    print(f"{GREEN}All {len(VENUES)} venues resolved to the expected timezone.{RESET}")
    print(f"{DIM}Wind thresholds: notable >=15 mph, severe >=20 mph.{RESET}")
    print(f"{DIM}Sample verdict at 22 mph: {wind_verdict(22.0)}{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
