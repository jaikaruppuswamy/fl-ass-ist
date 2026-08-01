"""Waiver board: roster-hole detection, ranking, FAAB bands."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist.scoring import ScoringSettings  # noqa: E402
from ff_assist.waivers import (  # noqa: E402
    roster_needs,
    score_free_agent,
    suggest_faab,
    trending_adds,
)

PPR = ScoringSettings.from_raw({"scoringSettings": {"scoringItems": [
    {"statId": 53, "points": 1.0}, {"statId": 42, "points": 0.1},
]}}, "test")

SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "RB/WR/TE", "K", "D/ST"]


@dataclass
class P:
    name: str
    position: str
    projected_avg_points: float = 8.0
    proTeam: str = "BUF"
    percent_owned: float = 12.0
    injuryStatus: str = "ACTIVE"
    lineupSlot: str = "BE"
    eligibleSlots: tuple = field(default_factory=tuple)


def roster(**counts):
    out = []
    for pos, n in counts.items():
        out += [P(f"{pos}{i}", pos) for i in range(n)]
    return out


# --- roster needs ----------------------------------------------------------


def test_two_backs_for_three_slots_is_flagged():
    needs = roster_needs(roster(QB=2, RB=2, WR=5, TE=2, K=1), SLOTS)
    assert "RB" in needs
    assert needs["RB"]["status"] in ("thin", "critical")


def test_deep_position_is_not_flagged():
    needs = roster_needs(roster(QB=2, RB=6, WR=7, TE=3, K=1), SLOTS)
    assert "WR" not in needs
    assert "RB" not in needs


def test_missing_position_entirely_is_critical():
    needs = roster_needs(roster(QB=1, RB=4, WR=5, TE=0, K=1), SLOTS)
    assert needs["TE"]["status"] == "critical"


def test_empty_roster_reports_every_position_as_short():
    needs = roster_needs([], SLOTS)
    assert {"QB", "RB", "WR", "TE"} <= set(needs)


# --- FAAB bands ------------------------------------------------------------


def test_startable_player_filling_a_critical_hole_gets_the_top_band():
    band = suggest_faab(
        {"espn_proj_avg": 13.0, "fills_need": "critical", "usage_trend": "rising"}
    )
    assert "15-25%" in band


def test_marginal_player_on_a_deep_roster_gets_the_bottom_band():
    assert "$0-1" in suggest_faab({"espn_proj_avg": 2.0})


def test_a_startable_free_agent_gets_the_top_band_regardless_of_roster_shape():
    """14 points a game is a league-winner. Whether you happen to be thin at
    his position does not change that — you can start him over somebody."""
    assert "15-25%" in suggest_faab({"espn_proj_avg": 14.0})
    assert "15-25%" in suggest_faab({"espn_proj_avg": 14.0, "fills_need": "thin"})


def test_bands_never_go_backwards_as_projection_climbs():
    rank = {"$0-1": 0, "1-4%": 1, "5-12%": 2, "15-25%": 3}
    def tier(band):
        return next(v for k, v in rank.items() if band.startswith(k))
    tiers = [tier(suggest_faab({"espn_proj_avg": p})) for p in (1, 4, 6, 9, 12, 18)]
    assert tiers == sorted(tiers), tiers


def test_need_and_trend_promote_a_borderline_player_but_never_demote_a_good_one():
    plain = suggest_faab({"espn_proj_avg": 6.0})
    promoted = suggest_faab({"espn_proj_avg": 6.0, "fills_need": "thin", "usage_trend": "rising"})
    assert plain.startswith("1-4%") and promoted.startswith("5-12%")  # promoted one tier
    # a genuine starter stays top-band with no help at all
    assert "15-25%" in suggest_faab({"espn_proj_avg": 13.0})


def test_being_contested_raises_the_bid_by_one_tier():
    """Contested changes what it costs to win him, not what he is worth."""
    base = {"espn_proj_avg": 9.0}
    assert suggest_faab(base).startswith("5-12%")
    assert suggest_faab(base, contested=True).startswith("15-25%")


def test_contest_premium_is_capped_at_one_tier():
    """Chasing a bidding war on a replacement-level player is how a budget
    disappears in October."""
    assert suggest_faab({"espn_proj_avg": 1.0}, contested=True).startswith("1-4%")


# --- rows ------------------------------------------------------------------


def test_row_marks_that_a_player_fills_a_hole():
    needs = roster_needs(roster(QB=1, RB=2, WR=5, TE=2, K=1), SLOTS)
    row = score_free_agent(P("Some Back", "RB", 11.0), PPR, needs=needs)
    assert row["fills_need"] in ("thin", "critical")
    assert row["espn_proj_avg"] == 11.0


def test_row_omits_a_healthy_players_injury_field():
    row = score_free_agent(P("Fit Guy", "WR"), PPR)
    assert "injury" not in row


def test_row_surfaces_a_real_injury_tag():
    row = score_free_agent(P("Hurt Guy", "WR", injuryStatus="QUESTIONABLE"), PPR)
    assert row["injury"] == "QUESTIONABLE"


def test_negative_projection_placeholder_is_not_reported_as_ownership():
    row = score_free_agent(P("Nobody", "WR", percent_owned=-1), PPR)
    assert "pct_owned" not in row


# --- Sleeper overlay -------------------------------------------------------


def test_trending_degrades_to_empty_when_sleeper_is_unreachable():
    """The board must not fail because a third-party endpoint is down."""

    def boom(url, params):
        raise TimeoutError("no route")

    assert trending_adds(fetch=boom) == {}


def test_trending_handles_an_empty_response():
    assert trending_adds(fetch=lambda u, p: []) == {}
    assert trending_adds(fetch=lambda u, p: None) == {}


# --- id resolution: two paths, because the first one is fragile -------------


def test_falls_back_to_sleeper_dictionary_when_the_crosswalk_is_blocked(monkeypatch):
    """nflverse's crosswalk lives on a host that has 403'd from datacenter
    networks. The overlay must survive that, not vanish."""
    import ff_assist.waivers as w

    monkeypatch.setattr(w, "_names_from_crosswalk", lambda counts: {})

    def fake_fetch(url, params):
        if "trending" in url:
            return [{"player_id": "4034", "count": 12000}]
        return {"4034": {"full_name": "Christian McCaffrey", "position": "RB"}}

    out = w.trending_adds(fetch=fake_fetch)
    assert out == {w.normalize_name("Christian McCaffrey"): 12000}


def test_crosswalk_is_preferred_when_it_works(monkeypatch):
    import ff_assist.waivers as w

    monkeypatch.setattr(w, "_names_from_crosswalk", lambda counts: {"someone": 5})
    called = []

    def fake_fetch(url, params):
        if "trending" in url:
            return [{"player_id": "1", "count": 5}]
        called.append(url)
        return {}

    assert w.trending_adds(fetch=fake_fetch) == {"someone": 5}
    assert not called, "should not download the 5MB dictionary when the crosswalk works"


def test_both_paths_failing_degrades_to_empty(monkeypatch):
    import ff_assist.waivers as w

    monkeypatch.setattr(w, "_names_from_crosswalk", lambda counts: {})

    def fake_fetch(url, params):
        if "trending" in url:
            return [{"player_id": "1", "count": 5}]
        raise TimeoutError("blocked")

    assert w.trending_adds(fetch=fake_fetch) == {}


def test_sleeper_records_without_full_name_are_assembled_from_parts(monkeypatch):
    import ff_assist.waivers as w

    monkeypatch.setattr(w, "_names_from_crosswalk", lambda counts: {})

    def fake_fetch(url, params):
        if "trending" in url:
            return [{"player_id": "9", "count": 3}]
        return {"9": {"first_name": "Puka", "last_name": "Nacua"}}

    assert w.trending_adds(fetch=fake_fetch) == {w.normalize_name("Puka Nacua"): 3}
