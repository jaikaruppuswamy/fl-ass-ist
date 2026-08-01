"""Weather, gated on whether it can possibly matter.

Per the plan's own reality check this is a tie-breaker, not a difference-maker.
Wind is the variable that moves fantasy points; temperature mostly does not.
So the output is deliberately small: wind, precipitation probability, and a
plain-language flag, or nothing at all when the game is indoors.

Two things this module refuses to trust:

**nflverse's ``roof`` field.** It doubles as a weather-availability flag rather
than a description of the building. Every ``dome`` and ``closed`` game has null
temp/wind, and some open-air international venues are labelled ``dome``. A
``null`` roof means a retractable whose state is not yet decided — which is
honest, and must not be read as "indoors".

**``stadium_id``.** nflverse reuses the home team's id for that team's neutral
site games: ``ATL97`` is both Mercedes-Benz Stadium and, for one 2026 game, the
Bernabéu in Madrid. Keying a forecast on it would confidently return Atlanta's
weather for a game in Spain. VENUES is keyed on the stadium *name*.

Open-Meteo needs no API key and allows 10k calls/day.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["VENUES", "Venue", "wind_verdict", "forecast_for_game", "OPEN_METEO_URL"]

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


@dataclass(frozen=True)
class Venue:
    lat: float
    lon: float
    #: IANA timezone, used purely as a coordinate sanity check — Open-Meteo
    #: echoes back the timezone it resolved, so a transposed sign or a wrong
    #: continent shows up immediately. See scripts/check_weather.py.
    tz: str
    #: True when the field is open to the sky, False when permanently covered,
    #: None when the venue has a retractable roof whose state we cannot know
    #: in advance.
    open_air: bool | None


# Keyed by the `stadium` string nflverse reports. Coordinates are stadium
# locations to ~2 decimal places, which is far more precision than a wind
# forecast needs. Verified by scripts/check_weather.py, which asserts the
# timezone Open-Meteo resolves matches `tz` for every entry.
VENUES: dict[str, Venue] = {
    # --- open air -----------------------------------------------------------
    "Highmark Stadium": Venue(42.77, -78.79, "America/New_York", True),
    "Gillette Stadium": Venue(42.09, -71.26, "America/New_York", True),
    "MetLife Stadium": Venue(40.81, -74.07, "America/New_York", True),
    "Hard Rock Stadium": Venue(25.96, -80.24, "America/New_York", True),
    "M&T Bank Stadium": Venue(39.28, -76.62, "America/New_York", True),
    "Paycor Stadium": Venue(39.10, -84.52, "America/New_York", True),
    "Huntington Bank Field": Venue(41.51, -81.70, "America/New_York", True),
    "Cleveland Browns Stadium": Venue(41.51, -81.70, "America/New_York", True),
    "Acrisure Stadium": Venue(40.45, -80.02, "America/New_York", True),
    "Nissan Stadium": Venue(36.17, -86.77, "America/Chicago", True),
    "EverBank Stadium": Venue(30.32, -81.64, "America/New_York", True),
    "Arrowhead Stadium": Venue(39.05, -94.48, "America/Chicago", True),
    "GEHA Field at Arrowhead Stadium": Venue(39.05, -94.48, "America/Chicago", True),
    "Empower Field at Mile High": Venue(39.74, -105.02, "America/Denver", True),
    "Soldier Field": Venue(41.86, -87.62, "America/Chicago", True),
    "Lambeau Field": Venue(44.50, -88.06, "America/Chicago", True),
    "Bank of America Stadium": Venue(35.23, -80.85, "America/New_York", True),
    "Raymond James Stadium": Venue(27.98, -82.50, "America/New_York", True),
    "Lincoln Financial Field": Venue(39.90, -75.17, "America/New_York", True),
    "FedExField": Venue(38.91, -76.86, "America/New_York", True),
    "Northwest Stadium": Venue(38.91, -76.86, "America/New_York", True),
    "Levi's Stadium": Venue(37.40, -121.97, "America/Los_Angeles", True),
    "Lumen Field": Venue(47.60, -122.33, "America/Los_Angeles", True),
    # --- fixed roof ---------------------------------------------------------
    "Ford Field": Venue(42.34, -83.05, "America/Detroit", False),
    "Caesars Superdome": Venue(29.95, -90.08, "America/Chicago", False),
    "U.S. Bank Stadium": Venue(44.97, -93.26, "America/Chicago", False),
    "Allegiant Stadium": Venue(36.09, -115.18, "America/Los_Angeles", False),
    "SoFi Stadium": Venue(33.95, -118.34, "America/Los_Angeles", False),
    # --- retractable: state unknown until game day --------------------------
    "AT&T Stadium": Venue(32.75, -97.09, "America/Chicago", None),
    "Lucas Oil Stadium": Venue(39.76, -86.16, "America/Indiana/Indianapolis", None),
    "State Farm Stadium": Venue(33.53, -112.26, "America/Phoenix", None),
    "NRG Stadium": Venue(29.68, -95.41, "America/Chicago", None),
    "Reliant Stadium": Venue(29.68, -95.41, "America/Chicago", None),
    "Mercedes-Benz Stadium": Venue(33.76, -84.40, "America/New_York", None),
    # --- international ------------------------------------------------------
    # nflverse labels the first three of these `dome`. They are open-air
    # venues: roofs cover the stands, not the field. Trusting the field would
    # skip the weather check on a November game in Munich.
    "Melbourne Cricket Ground": Venue(-37.82, 144.98, "Australia/Melbourne", True),
    "Stade de France": Venue(48.92, 2.36, "Europe/Paris", True),
    "FC Bayern Munich Stadium": Venue(48.22, 11.62, "Europe/Berlin", True),
    "Tottenham Hotspur Stadium": Venue(51.60, -0.07, "Europe/London", True),
    "Wembley Stadium": Venue(51.56, -0.28, "Europe/London", True),
    "Estadio Azteca": Venue(19.30, -99.15, "America/Mexico_City", True),
    "Maracana Stadium": Venue(-22.91, -43.23, "America/Sao_Paulo", True),
    "Arena Corinthians": Venue(-23.55, -46.47, "America/Sao_Paulo", True),
    "Bernabeu": Venue(40.45, -3.69, "Europe/Madrid", None),  # retractable roof
    "Croke Park": Venue(53.36, -6.25, "Europe/Dublin", True),
}

#: Wind speeds in mph. Below the first threshold nothing measurable happens to
#: fantasy scoring; above the second, deep passing and kicking degrade sharply.
WIND_NOTABLE = 15.0
WIND_SEVERE = 20.0


def wind_verdict(wind_mph: float | None) -> str | None:
    """A plain-language read, or None when there is nothing worth saying."""
    if wind_mph is None:
        return None
    if wind_mph >= WIND_SEVERE:
        return f"{wind_mph:.0f} mph wind — downgrade deep passing and kicking"
    if wind_mph >= WIND_NOTABLE:
        return f"{wind_mph:.0f} mph wind — mild drag on passing games"
    return None


def venue_for(stadium: str | None) -> Venue | None:
    if not stadium:
        return None
    return VENUES.get(stadium.strip())


def weather_applies(stadium: str | None, roof: str | None) -> bool | None:
    """Whether to bother fetching a forecast.

    Our venue table wins over nflverse's ``roof``, because that field is wrong
    for open-air international venues. Returns None when the answer genuinely
    depends on a retractable roof nobody has closed yet.
    """
    venue = venue_for(stadium)
    if venue is not None:
        return venue.open_air
    if roof == "outdoors":
        return True
    if roof in ("dome", "closed"):
        return False
    return None


def forecast_for_game(
    stadium: str | None,
    roof: str | None,
    kickoff_iso: str | None,
    *,
    fetch: Any = None,
) -> dict[str, Any] | None:
    """Wind and precipitation at kickoff, or None when it cannot matter.

    ``fetch`` is injectable so tests never touch the network; it receives
    ``(url, params)`` and returns parsed JSON.
    """
    applies = weather_applies(stadium, roof)
    if applies is False:
        return None

    venue = venue_for(stadium)
    if venue is None:
        return {"unknown_venue": stadium}

    if fetch is None:
        def fetch(url: str, params: dict[str, Any]) -> Any:  # pragma: no cover
            import httpx

            return httpx.get(url, params=params, timeout=15).json()

    params = {
        "latitude": venue.lat,
        "longitude": venue.lon,
        "hourly": "wind_speed_10m,precipitation_probability,temperature_2m",
        "wind_speed_unit": "mph",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "forecast_days": 16,
    }
    try:
        data = fetch(OPEN_METEO_URL, params)
    except Exception as exc:  # noqa: BLE001 — weather is a nice-to-have
        return {"error": f"forecast unavailable: {type(exc).__name__}"}

    hourly = (data or {}).get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return {"error": "no forecast data"}

    index = 0
    if kickoff_iso:
        target = kickoff_iso[:13]  # YYYY-MM-DDTHH
        matches = [i for i, t in enumerate(times) if t[:13] == target]
        if matches:
            index = matches[0]
        else:
            return {"error": "kickoff outside the forecast window"}

    def at(key: str) -> float | None:
        series = hourly.get(key) or []
        return series[index] if index < len(series) else None

    wind = at("wind_speed_10m")
    out: dict[str, Any] = {
        "wind_mph": round(wind, 1) if wind is not None else None,
        "precip_pct": at("precipitation_probability"),
        "temp_f": at("temperature_2m"),
        "resolved_timezone": (data or {}).get("timezone"),
    }
    verdict = wind_verdict(wind)
    if verdict:
        out["verdict"] = verdict
    if applies is None:
        out["caveat"] = "retractable roof — irrelevant if closed on the day"
    return out
