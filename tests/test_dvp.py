"""DvP, and the nflverse -> ESPN stat translation it rests on.

The translation is validated against two independent sources:

1. nflverse's own ``fantasy_points_ppr`` for a standard PPR league, and
2. ESPN's applied points for real 2025 games in Jai's actual leagues, one of
   which scores yardage entirely in buckets.

Agreeing with both means the mapping and the bucket synthesis are right.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist.dvp import (  # noqa: E402
    defense_vs_position,
    dvp_for_team,
    score_nflverse_row,
    to_espn_stat_line,
)
from ff_assist.scoring import ScoringSettings  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"

# nflverse's fantasy_points_ppr convention, as an explicit league.
STANDARD_PPR = ScoringSettings.from_raw({"scoringSettings": {"scoringItems": [
    {"statId": 3, "points": 0.04},
    {"statId": 4, "points": 4},
    {"statId": 20, "points": -2},
    {"statId": 24, "points": 0.1},
    {"statId": 25, "points": 6},
    {"statId": 42, "points": 0.1},
    {"statId": 43, "points": 6},
    {"statId": 53, "points": 1.0},
    {"statId": 72, "points": -2},
    {"statId": 19, "points": 2},
    {"statId": 26, "points": 2},
    {"statId": 44, "points": 2},
    # nflverse's fantasy_points_ppr credits return touchdowns at 6.
    {"statId": 101, "points": 6},
    {"statId": 102, "points": 6},
]}}, "standard_ppr")


# ---------------------------------------------------------------------------
# Stat translation
# ---------------------------------------------------------------------------


def test_basic_columns_map_to_espn_ids():
    line = to_espn_stat_line({"receptions": 5, "receiving_yards": 60, "receiving_tds": 1})
    assert line[53] == 5
    assert line[42] == 60
    assert line[43] == 1


def test_bucket_categories_use_floor_division():
    """Verified against a real ESPN payload: 67 rushing yards arrives as
    27:13, 28:6, 29:3, 30:2, 31:1."""
    line = to_espn_stat_line({"rushing_yards": 67})
    assert line[27] == 13  # every 5
    assert line[28] == 6  # every 10
    assert line[29] == 3  # every 20
    assert line[30] == 2  # every 25
    assert line[31] == 1  # every 50


def test_partial_buckets_earn_nothing():
    """9 rushing yards is not worth a 10-yard bucket. Rounding here would
    inflate every stat line in a bucket league."""
    line = to_espn_stat_line({"rushing_yards": 9})
    assert 28 not in line
    assert line[27] == 1


def test_fumbles_lost_are_summed_across_the_three_columns():
    line = to_espn_stat_line(
        {"rushing_fumbles_lost": 1, "receiving_fumbles_lost": 1, "sack_fumbles_lost": 0}
    )
    assert line[72] == 2


def test_yardage_milestones_are_ranges_not_thresholds():
    """ESPN's labels are explicit: 56 is "100-199 yard receiving game" and 57
    is "200+". A 210-yard game must score 57 only — awarding both would
    silently overpay every big game."""
    assert to_espn_stat_line({"receiving_yards": 99}).get(56) is None
    assert to_espn_stat_line({"receiving_yards": 123})[56] == 1
    assert 57 not in to_espn_stat_line({"receiving_yards": 123})
    big = to_espn_stat_line({"receiving_yards": 210})
    assert big[57] == 1 and 56 not in big
    assert to_espn_stat_line({"rushing_yards": 150})[37] == 1
    assert to_espn_stat_line({"passing_yards": 350})[17] == 1


def test_every_100_yards_is_a_bucket_not_a_game_bonus():
    """32/52/10 are 'Every 100 yards' — they accumulate. Treating one as a
    one-off bonus would under-score a 240-yard game by a full bucket."""
    assert to_espn_stat_line({"receiving_yards": 240})[52] == 2
    assert to_espn_stat_line({"rushing_yards": 100})[32] == 1
    assert to_espn_stat_line({"passing_yards": 350})[10] == 3


def test_special_teams_td_uses_the_leagues_own_return_rule():
    from ff_assist.dvp import _special_teams_td_points

    exact = ScoringSettings.from_raw({"scoringSettings": {"scoringItems": [
        {"statId": 105, "points": 6}]}})
    assert _special_teams_td_points(exact) == 6

    split = ScoringSettings.from_raw({"scoringSettings": {"scoringItems": [
        {"statId": 101, "points": 6}, {"statId": 102, "points": 6}]}})
    assert _special_teams_td_points(split) == 6

    # Priced differently and nflverse cannot say which it was — take the lower.
    odd = ScoringSettings.from_raw({"scoringSettings": {"scoringItems": [
        {"statId": 101, "points": 6}, {"statId": 102, "points": 4}]}})
    assert _special_teams_td_points(odd) == 4

    none = ScoringSettings.from_raw({"scoringSettings": {"scoringItems": []}})
    assert _special_teams_td_points(none) == 0.0


def test_empty_row_yields_an_empty_line():
    assert to_espn_stat_line({}) == {}
    assert to_espn_stat_line({"receptions": 0, "receiving_yards": 0}) == {}


# ---------------------------------------------------------------------------
# Cross-check 1: nflverse's own PPR totals
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
def test_our_ppr_scoring_reproduces_nflverse_fantasy_points_ppr():
    """If the column->stat-id mapping is wrong anywhere that matters, this
    diverges. Kickers and defences are excluded: nflverse does not compute
    fantasy_points_ppr for them the way ESPN does."""
    frame = (
        _STATS.filter(pl.col("season_type") == "REG")
        .filter(pl.col("position").is_in(["QB", "RB", "WR", "TE"]))
        .filter(pl.col("fantasy_points_ppr").is_not_null())
        .head(4000)
    )
    mismatches = []
    for row in frame.iter_rows(named=True):
        ours = score_nflverse_row(row, STANDARD_PPR)
        theirs = row["fantasy_points_ppr"]
        if abs(ours - theirs) > 0.101:
            mismatches.append((row["player_display_name"], row["week"], ours, theirs))
    assert not mismatches, f"{len(mismatches)}/{frame.height} rows differ: {mismatches[:5]}"


# ---------------------------------------------------------------------------
# Cross-check 2: ESPN's applied points, in Jai's real leagues
# ---------------------------------------------------------------------------

_dumps = sorted(SAMPLES.glob("league_*_dump.json")) if SAMPLES.is_dir() else []


@needs_net
@pytest.mark.skipif(not _dumps, reason="no league dumps")
@pytest.mark.parametrize("path", _dumps, ids=lambda p: p.stem)
def test_nflverse_derived_points_match_espns_own(path):
    """The strongest check available: score nflverse's stats in a real league's
    rules and compare against what ESPN actually awarded for that same game.

    Two independent data providers, one scoring engine. Agreement means the
    translation, the bucket synthesis and the scoring parser are all correct.
    """
    payload = json.loads(path.read_text())
    scoring = ScoringSettings.from_raw(payload["settings"], payload["league_key"])
    box = (payload.get("prior_season_sample") or {}).get("boxscore_shape") or {}
    week = box.get("week")
    lineup = box.get("home_lineup_sample") or []
    if not week or not lineup:
        pytest.skip("no box score sample")

    frame = _STATS.filter((pl.col("week") == week) & (pl.col("season_type") == "REG"))
    checked = 0
    for player in lineup:
        if player.get("position") not in ("QB", "RB", "WR", "TE"):
            continue  # nflverse weekly stats do not cover K or D/ST
        rows = frame.filter(pl.col("player_display_name") == player["name"]).to_dicts()
        if not rows:
            continue
        ours = score_nflverse_row(rows[0], scoring)
        espn = player["points"]
        assert abs(ours - espn) < 0.11, (
            f"{player['name']} in {payload['league_key']} wk{week}: "
            f"nflverse-derived={ours} espn={espn}"
        )
        checked += 1
    if not checked:
        pytest.skip("no overlapping skill players")


# ---------------------------------------------------------------------------
# The DvP table
# ---------------------------------------------------------------------------


@needs_net
def test_dvp_table_shape_and_ranking():
    dvp = defense_vs_position(STANDARD_PPR, 2025, window=4, stats=_STATS)
    assert "error" not in dvp
    assert len(dvp["weeks"]) <= 4
    table = dvp["points_allowed_per_game"]
    assert 25 <= len(table) <= 32, f"expected most defences, got {len(table)}"

    rb = {d: v["RB"] for d, v in table.items() if "RB" in v}
    softest = max(rb, key=rb.get)
    assert dvp["rank"][softest]["RB"] == 1, "rank 1 must be the softest matchup"
    toughest = min(rb, key=rb.get)
    assert dvp["rank"][toughest]["RB"] == len(rb)


@needs_net
def test_dvp_reorders_between_scoring_formats():
    """The reason we compute this rather than buying it: a bucket-scoring
    league does not rank defences the same way generic PPR does."""
    if not _dumps:
        pytest.skip("no league dumps")
    glad = next((p for p in _dumps if "gladiator" in p.name), None)
    if glad is None:
        pytest.skip("no gladiator dump")
    bucket_scoring = ScoringSettings.from_raw(json.loads(glad.read_text())["settings"], "gladiator")

    a = defense_vs_position(STANDARD_PPR, 2025, window=None, stats=_STATS)
    b = defense_vs_position(bucket_scoring, 2025, window=None, stats=_STATS)
    order_a = sorted(a["rank"], key=lambda d: a["rank"][d].get("RB", 99))
    order_b = sorted(b["rank"], key=lambda d: b["rank"][d].get("RB", 99))
    assert order_a != order_b, "two very different scoring systems produced identical DvP ranks"


@needs_net
def test_dvp_cell_lookup():
    dvp = defense_vs_position(STANDARD_PPR, 2025, window=4, stats=_STATS)
    defense = next(iter(dvp["points_allowed_per_game"]))
    cell = dvp_for_team(dvp, defense, "RB")
    assert cell is None or {"dvp_pts_allowed", "dvp_rank"} <= set(cell)
    assert dvp_for_team(dvp, "NOT_A_TEAM", "RB") is None


@needs_net
def test_empty_window_reports_an_error_rather_than_an_empty_table():
    out = defense_vs_position(STANDARD_PPR, 2025, through_week=0, stats=_STATS)
    assert "error" in out


# ---------------------------------------------------------------------------
# Rest-of-season schedule strength
# ---------------------------------------------------------------------------

_SCHED_FIXTURE = pl.DataFrame(
    [
        {"season": 2026, "week": w, "home_team": "BUF", "away_team": opp}
        for w, opp in {10: "MIA", 11: "NE", 15: "NYJ", 16: "MIA", 17: "NE"}.items()
    ]
)

_DVP_FIXTURE = {
    "points_allowed_per_game": {
        "MIA": {"RB": 20.0},
        "NE": {"RB": 10.0},
        "NYJ": {"RB": 30.0},
    },
    "rank": {},
}


def test_ros_weights_playoff_weeks_double():
    from ff_assist.dvp import ros_schedule_strength

    out = ros_schedule_strength(
        [("Player X", "BUF", "RB")], _DVP_FIXTURE, 2026,
        from_week=10, schedules=_SCHED_FIXTURE,
    )[0]
    # weeks 10 MIA 20, 11 NE 10 (weight 1); 15 NYJ 30, 16 MIA 20, 17 NE 10 (weight 2)
    # (20 + 10 + 60 + 40 + 20) / (1 + 1 + 2 + 2 + 2) = 150/8 = 18.75
    assert out["ros_pts_allowed"] == 18.75
    assert out["games_remaining"] == 5
    assert [d["week"] for d in out["playoff_weeks"]] == [15, 16, 17]


def test_ros_unweighted_would_differ_proving_the_weighting_bites():
    from ff_assist.dvp import ros_schedule_strength

    weighted = ros_schedule_strength(
        [("X", "BUF", "RB")], _DVP_FIXTURE, 2026, from_week=10,
        schedules=_SCHED_FIXTURE)[0]["ros_pts_allowed"]
    flat = ros_schedule_strength(
        [("X", "BUF", "RB")], _DVP_FIXTURE, 2026, from_week=10,
        schedules=_SCHED_FIXTURE, playoff_weight=1.0)[0]["ros_pts_allowed"]
    assert flat == 18.0
    assert weighted != flat


def test_ros_reports_remaining_byes():
    from ff_assist.dvp import ros_schedule_strength

    out = ros_schedule_strength(
        [("X", "BUF", "RB")], _DVP_FIXTURE, 2026, from_week=10,
        schedules=_SCHED_FIXTURE)[0]
    assert out["byes_remaining"] == [12, 13, 14]


def test_ros_sorts_easiest_schedule_first():
    from ff_assist.dvp import ros_schedule_strength

    sched = pl.DataFrame([
        {"season": 2026, "week": 10, "home_team": "BUF", "away_team": "NYJ"},
        {"season": 2026, "week": 10, "home_team": "KC", "away_team": "NE"},
    ])
    out = ros_schedule_strength(
        [("Hard", "KC", "RB"), ("Easy", "BUF", "RB")], _DVP_FIXTURE, 2026,
        from_week=10, schedules=sched)
    assert [r["player"] for r in out] == ["Easy", "Hard"]


def test_ros_handles_a_team_with_no_games_left():
    from ff_assist.dvp import ros_schedule_strength

    out = ros_schedule_strength(
        [("X", "BUF", "RB")], _DVP_FIXTURE, 2026, from_week=18,
        schedules=_SCHED_FIXTURE)[0]
    assert "error" in out
