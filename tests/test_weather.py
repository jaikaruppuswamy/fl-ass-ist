"""Weather gating and the Open-Meteo join.

No network: the fetch callable is injected. Coordinate correctness is checked
separately by scripts/check_weather.py, which runs on a machine that can
actually reach Open-Meteo.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist.weather import (  # noqa: E402
    VENUES,
    forecast_for_game,
    venue_for,
    weather_applies,
    wind_verdict,
)

FAKE = {
    "timezone": "America/New_York",
    "hourly": {
        "time": ["2026-09-13T12:00", "2026-09-13T13:00", "2026-09-13T14:00"],
        "wind_speed_10m": [8.0, 22.4, 30.0],
        "precipitation_probability": [5, 60, 80],
        "temperature_2m": [70.0, 68.0, 66.0],
    },
}


def fake_fetch(url, params):
    return FAKE


# --- gating ----------------------------------------------------------------


def test_roofed_stadium_skips_the_forecast_entirely():
    assert weather_applies("Ford Field", "dome") is False
    assert forecast_for_game("Ford Field", "dome", None, fetch=fake_fetch) is None


def test_open_air_stadium_fetches():
    assert weather_applies("Highmark Stadium", "outdoors") is True


def test_retractable_roof_is_unknown_not_indoors():
    """A null roof means nobody has decided yet. Treating it as indoors would
    skip the check on a game that may well be played in the open."""
    assert weather_applies("AT&T Stadium", None) is None
    out = forecast_for_game("AT&T Stadium", None, None, fetch=fake_fetch)
    assert "caveat" in out


def test_our_venue_table_overrides_nflverse_roof():
    """nflverse labels these open-air international grounds 'dome'. Trusting
    it would skip weather on a November game in Munich."""
    for stadium in ("Melbourne Cricket Ground", "Stade de France", "FC Bayern Munich Stadium"):
        assert weather_applies(stadium, "dome") is True, stadium
        assert forecast_for_game(stadium, "dome", None, fetch=fake_fetch) is not None


def test_unknown_venue_is_reported_rather_than_assumed():
    out = forecast_for_game("Some New Stadium", "outdoors", None, fetch=fake_fetch)
    assert out == {"unknown_venue": "Some New Stadium"}


def test_falls_back_to_nflverse_roof_for_unlisted_venues():
    assert weather_applies("Unlisted Park", "outdoors") is True
    assert weather_applies("Unlisted Park", "closed") is False
    assert weather_applies("Unlisted Park", None) is None


# --- forecast --------------------------------------------------------------


def test_picks_the_hour_matching_kickoff():
    out = forecast_for_game(
        "Highmark Stadium", "outdoors", "2026-09-13T13:00", fetch=fake_fetch
    )
    assert out["wind_mph"] == 22.4
    assert out["precip_pct"] == 60
    assert "downgrade deep passing" in out["verdict"]


def test_kickoff_outside_the_window_errors_rather_than_returning_hour_zero():
    """Silently returning the first hour of a 16-day window would attach
    today's weather to a game three weeks out."""
    out = forecast_for_game(
        "Highmark Stadium", "outdoors", "2026-12-25T13:00", fetch=fake_fetch
    )
    assert "error" in out


def test_network_failure_degrades_to_a_note():
    def boom(url, params):
        raise TimeoutError("nope")

    out = forecast_for_game("Highmark Stadium", "outdoors", None, fetch=boom)
    assert "error" in out


def test_empty_payload_is_handled():
    out = forecast_for_game("Highmark Stadium", "outdoors", None, fetch=lambda u, p: {})
    assert "error" in out


# --- wind thresholds -------------------------------------------------------


@pytest.mark.parametrize("mph,expected", [(0, None), (14.9, None), (15, "mild"), (19.9, "mild"), (20, "downgrade"), (35, "downgrade")])
def test_wind_thresholds(mph, expected):
    verdict = wind_verdict(mph)
    if expected is None:
        assert verdict is None
    else:
        assert expected in verdict


def test_no_wind_reading_says_nothing():
    assert wind_verdict(None) is None


# --- the venue table itself ------------------------------------------------


def test_every_venue_has_plausible_coordinates():
    for name, v in VENUES.items():
        assert -90 <= v.lat <= 90, name
        assert -180 <= v.lon <= 180, name
        assert "/" in v.tz, f"{name}: {v.tz} is not an IANA zone"


def test_southern_hemisphere_venues_have_negative_latitude():
    """A sign error on Melbourne or Rio puts the game in the wrong hemisphere
    and returns weather from the opposite season."""
    for name in ("Melbourne Cricket Ground", "Maracana Stadium", "Arena Corinthians"):
        assert VENUES[name].lat < 0, name


def test_us_venues_have_negative_longitude():
    for name, v in VENUES.items():
        if v.tz.startswith("America/") and "Sao_Paulo" not in v.tz:
            assert v.lon < 0, f"{name} is in the Americas but has longitude {v.lon}"


def test_venue_lookup_tolerates_whitespace_and_misses():
    assert venue_for(" Ford Field ") is VENUES["Ford Field"]
    assert venue_for(None) is None
    assert venue_for("nope") is None
