"""Backtest arithmetic: the actual / model / perfect comparison.

Pure logic, no network. The ordering is the invariant that matters — hindsight
can never be worse than projections. If `model` ever exceeded `perfect` the
backtest would be flattering the system, which is the single failure mode that
would make the whole exercise worthless.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from backtest_2025 import week_result  # noqa: E402
from ff_assist.scoring import ScoringSettings  # noqa: E402

PPR = ScoringSettings.from_raw({"scoringSettings": {"scoringItems": [{"statId": 53, "points": 1.0}]}})

RB = ("RB", "RB/WR/TE")
WR = ("WR", "RB/WR/TE")
SLOTS = ["RB", "WR", "RB/WR/TE"]


@dataclass
class P:
    name: str
    position: str
    slot_position: str
    points: float
    projected_points: float
    eligibleSlots: tuple = field(default_factory=tuple)
    projected_breakdown: dict = field(default_factory=dict)


def test_a_misfired_start_shows_up_as_model_gain():
    """Started the RB with the better projection; the benched one scored more."""
    lineup = [
        P("StartedRB", "RB", "RB", 5.0, 12.0, RB),
        P("StartedWR", "WR", "WR", 10.0, 10.0, WR),
        P("FlexWR", "WR", "RB/WR/TE", 8.0, 9.0, WR),
        P("BenchRB", "RB", "BE", 20.0, 11.0, RB),
    ]
    r = week_result(lineup, SLOTS, PPR)
    assert r["actual"] == 23.0            # 5 + 10 + 8
    assert r["model"] == 35.0             # projections put BenchRB in the flex
    assert r["perfect"] == 38.0           # hindsight also drops StartedRB
    assert "BenchRB" in r["swaps"]


def test_hindsight_is_never_worse_than_the_model():
    lineup = [
        P("StartedRB", "RB", "RB", 5.0, 12.0, RB),
        P("StartedWR", "WR", "WR", 10.0, 10.0, WR),
        P("FlexWR", "WR", "RB/WR/TE", 8.0, 9.0, WR),
        P("BenchRB", "RB", "BE", 20.0, 11.0, RB),
    ]
    r = week_result(lineup, SLOTS, PPR)
    assert r["perfect"] >= r["model"], "a backtest that flatters the model is worthless"
    assert r["perfect"] >= r["actual"]


def test_injured_reserve_is_never_startable():
    lineup = [
        P("A", "RB", "RB", 10.0, 10.0, RB),
        P("B", "WR", "WR", 10.0, 10.0, WR),
        P("C", "WR", "RB/WR/TE", 10.0, 10.0, WR),
        P("Hurt", "RB", "IR", 99.0, 99.0, RB),
    ]
    r = week_result(lineup, SLOTS, PPR)
    assert "Hurt" not in r["swaps"]
    assert r["perfect"] == 30.0, "an IR player leaked into the optimal lineup"


def test_an_already_optimal_lineup_shows_no_gain():
    """The common case. A backtest that always finds improvements is broken."""
    lineup = [
        P("A", "RB", "RB", 10.0, 10.0, RB),
        P("B", "WR", "WR", 10.0, 10.0, WR),
        P("C", "WR", "RB/WR/TE", 10.0, 10.0, WR),
        P("D", "WR", "BE", 1.0, 1.0, WR),
    ]
    r = week_result(lineup, SLOTS, PPR)
    assert r["actual"] == r["model"] == 30.0
    assert r["swaps"] == []


def test_model_can_lose_to_actual_when_projections_were_wrong():
    """Honest backtests report losses. A gut call that beat the projection is a
    real outcome, not a bug to be smoothed away."""
    lineup = [
        P("Gut", "RB", "RB", 25.0, 6.0, RB),
        P("Chalk", "RB", "BE", 3.0, 15.0, RB),
        P("W1", "WR", "WR", 10.0, 10.0, WR),
        P("W2", "WR", "RB/WR/TE", 10.0, 10.0, WR),
    ]
    r = week_result(lineup, SLOTS, PPR)
    assert r["model"] < r["actual"]
