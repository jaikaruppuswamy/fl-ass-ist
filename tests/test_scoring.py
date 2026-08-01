"""Tests for the league scoring parser.

The isolation tests are the important ones. espn-api's own parser fails them,
which is why this module exists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist.scoring import RECEPTIONS, ScoringSettings  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"


def raw(ppr: float, *, te_ppr: float | None = None, pass_td: float = 4.0, extra=()):
    items = [
        {"statId": 53, "points": ppr},
        {"statId": 3, "points": 0.04},
        {"statId": 4, "points": pass_td},
        {"statId": 42, "points": 0.1},
        {"statId": 43, "points": 6},
        {"statId": 24, "points": 0.1},
        {"statId": 25, "points": 6},
        {"statId": 20, "points": -2},
        {"statId": 72, "points": -2},
        *extra,
    ]
    if te_ppr is not None:
        items[0] = {"statId": 53, "points": ppr, "pointsOverrides": {"4": te_ppr}}
    return {"scoringSettings": {"scoringItems": items, "scoringType": "H2H_POINTS"}}


# ---------------------------------------------------------------------------
# Isolation — the bug this module exists to avoid
# ---------------------------------------------------------------------------


def test_leagues_do_not_contaminate_each_other():
    full = ScoringSettings.from_raw(raw(1.0), "main")
    half = ScoringSettings.from_raw(raw(0.5), "work")
    std = ScoringSettings.from_raw(raw(0.0), "dynasty")

    # Parsed in order; each must still report its own value afterwards.
    assert full.ppr == 1.0
    assert half.ppr == 0.5
    assert std.ppr == 0.0


def test_rule_objects_are_not_shared_between_leagues():
    a = ScoringSettings.from_raw(raw(1.0), "a")
    b = ScoringSettings.from_raw(raw(0.5), "b")
    assert a.rules[53] is not b.rules[53]
    assert a.rules[53].points == 1.0


def test_parsing_does_not_mutate_the_espn_api_constant():
    from espn_api.football.constant import SETTINGS_SCORING_FORMAT_MAP

    before = dict(SETTINGS_SCORING_FORMAT_MAP[53])
    ScoringSettings.from_raw(raw(0.5), "x")
    assert SETTINGS_SCORING_FORMAT_MAP[53] == before
    assert "points" not in SETTINGS_SCORING_FORMAT_MAP[53]


def test_settings_are_immutable():
    s = ScoringSettings.from_raw(raw(1.0), "main")
    with pytest.raises(Exception):
        s.rules[53].points = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Scoring maths
# ---------------------------------------------------------------------------

# 7 rec, 96 rec yards, 1 rec TD, 3 rush for 12
STAT_LINE = {
    "receivingReceptions": 7,
    "receivingYards": 96,
    "receivingTouchdowns": 1,
    "rushingYards": 12,
}


def test_ppr_scoring_matches_hand_calculation():
    s = ScoringSettings.from_raw(raw(1.0))
    # 7*1 + 96*0.1 + 6 + 12*0.1 = 7 + 9.6 + 6 + 1.2
    assert s.score(STAT_LINE, "WR").points == pytest.approx(23.8)


def test_half_ppr_is_exactly_three_and_a_half_points_lower():
    full = ScoringSettings.from_raw(raw(1.0)).score(STAT_LINE, "WR").points
    half = ScoringSettings.from_raw(raw(0.5)).score(STAT_LINE, "WR").points
    assert round(full - half, 2) == 3.5


def test_te_premium_applies_only_to_tight_ends():
    s = ScoringSettings.from_raw(raw(1.0, te_ppr=1.5))
    assert s.ppr == 1.0
    assert s.te_premium == 0.5
    assert s.points_per(RECEPTIONS, "TE") == 1.5
    wr = s.score(STAT_LINE, "WR").points
    te = s.score(STAT_LINE, "TE").points
    assert round(te - wr, 2) == 3.5  # 7 receptions * 0.5


def test_negative_scoring_applies():
    s = ScoringSettings.from_raw(raw(1.0))
    assert s.score({"passingInterceptions": 2, "lostFumbles": 1}, "QB").points == pytest.approx(-6.0)


def test_stat_keys_accept_ids_names_and_numeric_strings():
    s = ScoringSettings.from_raw(raw(1.0))
    by_name = s.score({"receivingReceptions": 5}, "WR").points
    by_id = s.score({53: 5}, "WR").points
    by_str = s.score({"53": 5}, "WR").points
    assert by_name == by_id == by_str == 5.0


def test_unscored_categories_contribute_nothing():
    s = ScoringSettings.from_raw(raw(1.0))
    # passingAttempts (0) has no rule in this league
    assert s.score({"passingAttempts": 40}, "QB").points == 0.0


def test_empty_and_none_stat_lines():
    s = ScoringSettings.from_raw(raw(1.0))
    assert s.score(None, "WR").points == 0.0
    assert s.score({}, "WR").points == 0.0


def test_components_breakdown_sums_to_total():
    s = ScoringSettings.from_raw(raw(1.0))
    r = s.score(STAT_LINE, "WR")
    assert round(sum(r.components.values()), 2) == r.points
    assert r.components["REC"] == 7.0


# ---------------------------------------------------------------------------
# Honest failure on bucket categories
# ---------------------------------------------------------------------------


def test_bucket_categories_score_from_espns_precomputed_numeric_keys():
    """RY10 ('every 10 rushing yards') has no name in PLAYER_STATS_MAP, so ESPN
    ships it under the raw id. 67 rushing yards -> {'28': 6} -> 6 points."""
    s = ScoringSettings.from_raw({"scoringSettings": {"scoringItems": [
        {"statId": 28, "points": 1.0},
    ]}})
    assert s.score({"rushingYards": 67.0, "27": 13.0, "28": 6.0, "29": 3.0}, "RB").points == 6.0


def test_bucket_only_league_ignores_the_base_yardage_stat():
    """gladiator scores 28 (RY10) and has no rule for 24 (rushingYards). The
    raw yardage must contribute nothing rather than being counted twice."""
    s = ScoringSettings.from_raw({"scoringSettings": {"scoringItems": [
        {"statId": 28, "points": 1.0},
    ]}})
    assert s.score({"rushingYards": 67.0}, "RB").points == 0.0


def test_zero_valued_position_override_is_respected():
    """espn-api uses `override or points`, so an override of 0.0 falls through
    to the base value. gladiator really does zero out KR25/PR25 for D/ST."""
    s = ScoringSettings.from_raw({"scoringSettings": {"scoringItems": [
        {"statId": 117, "points": 1.0, "pointsOverrides": {"16": 0.0}},
    ]}})
    assert s.points_per(117, "D/ST") == 0.0
    assert s.points_per(117, "RB") == 1.0


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ppr,te,passtd,expected",
    [
        (1.0, None, 4.0, "PPR"),
        (0.5, None, 4.0, "Half-PPR"),
        (0.0, None, 4.0, "Standard"),
        (1.0, 1.5, 4.0, "PPR, TE+0.5"),
        (0.5, None, 6.0, "Half-PPR, 6pt passTD"),
    ],
)
def test_format_label(ppr, te, passtd, expected):
    assert ScoringSettings.from_raw(raw(ppr, te_ppr=te, pass_td=passtd)).format_label() == expected


# ---------------------------------------------------------------------------
# Real league data — skipped until dump_league_settings.py has been run
# ---------------------------------------------------------------------------

_dumps = sorted(SAMPLES.glob("league_*_dump.json")) if SAMPLES.is_dir() else []


@pytest.mark.skipif(not _dumps, reason="run scripts/dump_league_settings.py first")
@pytest.mark.parametrize("path", _dumps, ids=lambda p: p.stem)
def test_real_league_parses(path):
    payload = json.loads(path.read_text())
    s = ScoringSettings.from_raw(payload["settings"], payload["league_key"])
    assert s.rules, "no scoring rules parsed"
    assert s.ppr in (0.0, 0.5, 1.0) or s.ppr > 0


@pytest.mark.skipif(len(_dumps) < 2, reason="need 2+ league dumps")
def test_real_leagues_stay_independent():
    parsed = [
        ScoringSettings.from_raw(json.loads(p.read_text())["settings"], p.stem) for p in _dumps
    ]
    ppr_after = [s.ppr for s in parsed]
    reparsed = [
        ScoringSettings.from_raw(json.loads(p.read_text())["settings"], p.stem).ppr for p in _dumps
    ]
    assert ppr_after == reparsed


@pytest.mark.skipif(not _dumps, reason="run scripts/dump_league_settings.py first")
@pytest.mark.parametrize("path", _dumps, ids=lambda p: p.stem)
def test_rescoring_reproduces_espn_own_totals(path):
    """The acceptance test: re-score last season's real stat lines and compare
    to the points ESPN itself applied. If we match, the parser is right."""
    payload = json.loads(path.read_text())
    s = ScoringSettings.from_raw(payload["settings"], payload["league_key"])
    prior = payload.get("prior_season_sample", {})
    if not prior.get("available"):
        pytest.skip("no prior-season sample in this dump")

    lineup = (prior.get("boxscore_shape") or {}).get("home_lineup_sample") or []
    checked = 0
    for p in lineup:
        breakdown, espn_points = p.get("breakdown"), p.get("points")
        if not breakdown or espn_points is None or p.get("on_bye_week"):
            continue
        ours = s.score(breakdown, p.get("position"))
        assert ours.points == pytest.approx(espn_points, abs=0.11), (
            f"{p['name']} ({p.get('position')}): ours={ours.points} espn={espn_points} "
            f"components={ours.components}"
        )
        checked += 1

    if not checked:
        pytest.skip("no scoreable players in sample")


# ---------------------------------------------------------------------------
# Ambiguous stat names — regression for the six duplicate-id categories
# ---------------------------------------------------------------------------

AMBIGUOUS = {
    "passingYards": (3, 22),
    "rushingYards": (24, 40),
    "receivingYards": (42, 61),
    "receivingReceptions": (41, 53),
    "defensivePointsAllowed": (120, 187),
    "defensive2PtReturns": (205, 206),
}


@pytest.mark.parametrize("name,ids", AMBIGUOUS.items(), ids=list(AMBIGUOUS))
@pytest.mark.parametrize("which", [0, 1])
def test_ambiguous_stat_names_resolve_to_whichever_id_the_league_uses(name, ids, which):
    """A league scoring either id must score the same stat line either way."""
    stat_id = ids[which]
    s = ScoringSettings.from_raw({"scoringSettings": {"scoringItems": [
        {"statId": stat_id, "points": 2.0},
    ]}})
    assert s.score({name: 5}).points == pytest.approx(10.0), (
        f"{name} via id {stat_id} scored 0 — name resolved to the other id"
    )


# ---------------------------------------------------------------------------
# Yardage scheme normalization
# ---------------------------------------------------------------------------


def test_continuous_yardage_reports_granularity_one():
    s = ScoringSettings.from_raw(raw(1.0))
    y = s.yardage_scoring()
    assert y["receiving"] == {"per_yard": 0.1, "granularity": 1}
    assert y["passing"] == {"per_yard": 0.04, "granularity": 1}
    assert not s.has_bucket_yardage


def test_bucket_yardage_is_normalized_to_a_comparable_per_yard_rate():
    s = ScoringSettings.from_raw({"scoringSettings": {"scoringItems": [
        {"statId": 53, "points": 1.0},
        {"statId": 28, "points": 1.0},   # 1 pt per 10 rushing yards
        {"statId": 7, "points": 1.0},    # 1 pt per 20 passing yards
    ]}})
    y = s.yardage_scoring()
    assert y["rushing"] == {"per_yard": 0.1, "granularity": 10}
    assert y["passing"] == {"per_yard": 0.05, "granularity": 20}
    assert s.has_bucket_yardage
    assert "stepwise yardage (per 10yd/per 20yd)" in s.format_label()


def test_equivalent_average_rate_is_still_flagged_as_stepwise():
    """0.1/yd and 1pt/10yd average identically; only one is safe to treat as
    linear when modelling a floor."""
    cont = ScoringSettings.from_raw({"scoringSettings": {"scoringItems": [
        {"statId": 24, "points": 0.1}]}})
    step = ScoringSettings.from_raw({"scoringSettings": {"scoringItems": [
        {"statId": 28, "points": 1.0}]}})
    assert cont.yardage_scoring()["rushing"]["per_yard"] == step.yardage_scoring()["rushing"]["per_yard"]
    assert not cont.has_bucket_yardage
    assert step.has_bucket_yardage
