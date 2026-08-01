"""Usage trends, and the ESPN <-> nflverse name join.

The name join is the failure mode worth guarding: it breaks silently, and a
player who fails to resolve looks identical to a player with no usage.
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist.usage import (  # noqa: E402
    _trend,
    build_name_index,
    normalize_name,
    player_usage,
    resolve_player,
)

FIXTURE = pl.DataFrame(
    [
        {"player_display_name": "DK Metcalf", "position": "WR", "team": "PIT"},
        {"player_display_name": "A.J. Brown", "position": "WR", "team": "PHI"},
        {"player_display_name": "Travis Etienne", "position": "RB", "team": "JAX"},
        {"player_display_name": "Michael Carter", "position": "RB", "team": "ARI"},
        {"player_display_name": "Michael Carter II", "position": "S", "team": "NYJ"},
        {"player_display_name": "Amon-Ra St. Brown", "position": "WR", "team": "DET"},
    ]
)


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "espn,nflverse",
    [
        ("D.K. Metcalf", "DK Metcalf"),
        ("A.J. Brown", "A.J. Brown"),
        ("Travis Etienne Jr.", "Travis Etienne"),
        ("Kenneth Walker III", "Kenneth Walker"),
        ("Brian Thomas Jr.", "Brian Thomas"),
        ("De'Von Achane", "DeVon Achane"),
        ("Jaxon Smith-Njigba", "Jaxon Smith Njigba"),
        ("Amon-Ra St. Brown", "Amon-Ra St.Brown"),
    ],
)
def test_espn_and_nflverse_spellings_normalize_together(espn, nflverse):
    assert normalize_name(espn) == normalize_name(nflverse)


def test_normalize_handles_empty_and_none():
    assert normalize_name(None) == ""
    assert normalize_name("") == ""


def test_distinct_players_do_not_normalize_together():
    assert normalize_name("Justin Jefferson") != normalize_name("Jerry Jeudy")
    assert normalize_name("Michael Carter") == normalize_name("Michael Carter II")  # known clash


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_resolves_punctuation_mismatch():
    name, problem = resolve_player("D.K. Metcalf", FIXTURE)
    assert name == "DK Metcalf"
    assert problem is None


def test_resolves_suffix_mismatch():
    name, problem = resolve_player("Travis Etienne Jr.", FIXTURE)
    assert name == "Travis Etienne"
    assert problem is None


def test_unknown_player_reports_a_problem_rather_than_none_silently():
    name, problem = resolve_player("Nobody Here", FIXTURE)
    assert name is None
    assert "no nflverse player" in problem


def test_homonyms_are_split_by_position():
    """Two real Michael Carters. Without disambiguation one of them gets the
    other's usage, which is worse than returning nothing."""
    name, problem = resolve_player("Michael Carter", FIXTURE, position="RB")
    assert name == "Michael Carter"
    assert problem is None

    name, problem = resolve_player("Michael Carter", FIXTURE, position="S")
    assert name == "Michael Carter II"


def test_ambiguity_without_a_discriminator_refuses_to_guess():
    name, problem = resolve_player("Michael Carter", FIXTURE)
    assert name is None
    assert "ambiguous" in problem


def test_name_index_groups_variants():
    index = build_name_index(FIXTURE)
    assert index[normalize_name("Michael Carter")] == ["Michael Carter", "Michael Carter II"]


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------


def test_rising_usage_is_detected():
    assert _trend([0.15, 0.16, 0.18, 0.24, 0.27, 0.30])["direction"] == "rising"


def test_falling_usage_is_detected():
    assert _trend([0.30, 0.28, 0.26, 0.18, 0.15, 0.14])["direction"] == "falling"


def test_flat_usage_is_not_called_a_trend():
    assert _trend([0.22, 0.24, 0.23, 0.24, 0.22, 0.23])["direction"] == "flat"


def test_short_history_refuses_to_call_a_trend():
    """Two games with a direction is noise, not a trend."""
    assert _trend([0.10, 0.30])["direction"] == "insufficient_data"
    assert _trend([])["direction"] == "insufficient_data"
    assert _trend([None, None, 0.2])["direction"] == "insufficient_data"


def test_trend_ignores_missing_weeks():
    out = _trend([0.15, None, 0.16, 0.18, None, 0.24, 0.27, 0.30])
    assert out["direction"] == "rising"
    assert out["n"] == 6


# ---------------------------------------------------------------------------
# Live nflverse
# ---------------------------------------------------------------------------


def _stats():
    try:
        import nflreadpy as nfl

        return nfl.load_player_stats([2025])
    except Exception:  # noqa: BLE001
        return None


_STATS = _stats()
needs_net = pytest.mark.skipif(_STATS is None, reason="nflverse unreachable")


@needs_net
@pytest.mark.parametrize(
    "espn_name",
    [
        "D.K. Metcalf",
        "Justin Jefferson",
        "Amon-Ra St. Brown",
        "Brian Thomas Jr.",
        "Kenneth Walker III",
        "Jaxon Smith-Njigba",
        "Bijan Robinson",
        "Puka Nacua",
        "Ashton Jeanty",
    ],
)
def test_real_espn_names_resolve_against_nflverse(espn_name):
    name, problem = resolve_player(espn_name, _STATS)
    assert name, f"{espn_name}: {problem}"


@needs_net
def test_usage_returns_a_usable_series():
    out = player_usage("Justin Jefferson", 2025, weeks=6, stats=_STATS)
    assert out["position"] == "WR"
    assert out["headline_metric"] == "target_share"
    assert 1 <= len(out["weeks"]) <= 6
    assert out["trend"]["direction"] in ("rising", "falling", "flat", "insufficient_data")
    assert all(0.0 <= w["target_share"] <= 1.0 for w in out["weeks"] if "target_share" in w)


@needs_net
def test_running_backs_are_measured_on_carries_not_targets():
    out = player_usage("Bijan Robinson", 2025, stats=_STATS)
    assert out["headline_metric"] == "carries"


@needs_net
def test_unresolvable_player_produces_an_error_not_an_empty_series():
    out = player_usage("Definitely Not Aplayer", 2025, stats=_STATS)
    assert "error" in out
    assert "weeks" not in out


# ---------------------------------------------------------------------------
# Tool-level season fallback
# ---------------------------------------------------------------------------


@needs_net
def test_trend_falls_back_to_last_season_before_kickoff():
    """nflverse 404s a season that has not started. In September that must
    degrade to last year's usage, not to an 'unreachable' error."""
    import pathlib
    import tempfile

    from ff_assist import tools
    from ff_assist.config import Settings

    st = Settings(
        espn_s2="x" * 300,
        swid="{00000000-0000-0000-0000-000000000001}",
        season=2026,
        leagues=(),
        cache_dir=pathlib.Path(tempfile.mkdtemp()),
        log_level="INFO",
        mcp_bearer_token="",
        fantasypros_api_key="",
    )
    out = tools.get_player_trend("D.K. Metcalf", weeks=3, settings=st)
    assert "error" not in out, out.get("error")
    assert out["season"] == 2025
    assert "no games yet" in out["note"]
    assert out["weeks"]


@needs_net
def test_genuinely_unknown_player_still_errors_after_fallback():
    import pathlib
    import tempfile

    from ff_assist import tools
    from ff_assist.config import Settings

    st = Settings(
        espn_s2="x" * 300,
        swid="{00000000-0000-0000-0000-000000000001}",
        season=2026,
        leagues=(),
        cache_dir=pathlib.Path(tempfile.mkdtemp()),
        log_level="INFO",
        mcp_bearer_token="",
        fantasypros_api_key="",
    )
    out = tools.get_player_trend("Definitely Not Aplayer", settings=st)
    assert "error" in out
    assert "no nflverse player" in out["error"]
