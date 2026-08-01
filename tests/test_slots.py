"""Lineup slot parsing and the optimal-assignment optimizer."""

from __future__ import annotations

import itertools
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist.slots import LineupSlots, optimal_points, optimize_lineup  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"


@dataclass
class P:
    name: str
    eligibleSlots: tuple[str, ...]
    proj: float


def val(p: P) -> float:
    return p.proj


def brute_force(slots, players):
    """Exhaustive best assignment — the ground truth for small cases.

    A slot may be left empty (None): with no eligible player available that is
    the only legal outcome, and scoring such a lineup as zero would make this
    reference disagree with reality rather than with the optimizer. Safe here
    because the generated projections are non-negative, so leaving a fillable
    slot empty never wins.
    """
    best = 0.0
    options = [*range(len(players)), None]
    for combo in itertools.product(options, repeat=len(slots)):
        chosen = [c for c in combo if c is not None]
        if len(chosen) != len(set(chosen)):
            continue
        total = 0.0
        ok = True
        for slot, idx in zip(slots, combo):
            if idx is None:
                continue
            if slot not in players[idx].eligibleSlots:
                ok = False
                break
            total += players[idx].proj
        if ok:
            best = max(best, total)
    return round(best, 2)


# ---------------------------------------------------------------------------
# LineupSlots
# ---------------------------------------------------------------------------


def test_parses_lineup_slot_counts():
    # inai: 1 QB, 2 RB, 2 WR, 1 TE, 1 FLEX, 1 DST, 1 K, 5 BE, 2 IR
    ls = LineupSlots.from_raw({"lineupSlotCounts": {
        "0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "16": 1, "17": 1, "20": 5, "21": 2,
    }})
    assert ls.starting_slots == ["QB", "RB", "RB", "WR", "WR", "TE", "RB/WR/TE", "K", "D/ST"]
    assert ls.starters == 9
    assert ls.bench == 5
    assert ls.ir == 2
    assert ls.flex_slots == ["RB/WR/TE"]
    assert ls.summary()["roster_size"] == 14


def test_zero_count_slots_are_dropped():
    ls = LineupSlots.from_raw({"lineupSlotCounts": {"0": 1, "1": 0, "7": 0, "20": 3}})
    assert ls.starting_slots == ["QB"]


def test_empty_roster_settings():
    assert LineupSlots.from_raw({}).starting_slots == []


# ---------------------------------------------------------------------------
# The flex case greedy gets wrong
# ---------------------------------------------------------------------------


def test_beats_greedy_on_the_canonical_flex_trap():
    slots = ["RB", "RB/WR"]
    players = [
        P("RB1", ("RB", "RB/WR"), 18.0),
        P("RB2", ("RB", "RB/WR"), 17.0),
        P("WR1", ("WR", "RB/WR"), 15.0),
    ]
    # Greedy fills RB with RB1, then flex with best remaining eligible = WR1 -> 33.0
    assert optimal_points(slots, players, val) == 35.0
    names = [p.name for p in optimize_lineup(slots, players, val)]
    assert set(names) == {"RB1", "RB2"}


def test_flex_prefers_the_other_position_when_that_is_actually_better():
    slots = ["RB", "RB/WR"]
    players = [
        P("RB1", ("RB", "RB/WR"), 18.0),
        P("RB2", ("RB", "RB/WR"), 9.0),
        P("WR1", ("WR", "RB/WR"), 15.0),
    ]
    assert optimal_points(slots, players, val) == 33.0


# ---------------------------------------------------------------------------
# Correctness vs brute force
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(60))
def test_matches_brute_force_on_random_rosters(seed):
    rng = random.Random(seed)
    slot_pool = ["QB", "RB", "WR", "TE", "RB/WR", "RB/WR/TE", "WR/TE"]
    slots = [rng.choice(slot_pool) for _ in range(rng.randint(1, 4))]

    eligibility = {
        "QB": ("QB",),
        "RB": ("RB", "RB/WR", "RB/WR/TE"),
        "WR": ("WR", "RB/WR", "WR/TE", "RB/WR/TE"),
        "TE": ("TE", "WR/TE", "RB/WR/TE"),
    }
    players = []
    for i in range(rng.randint(1, 6)):
        pos = rng.choice(list(eligibility))
        players.append(P(f"{pos}{i}", eligibility[pos], round(rng.uniform(0, 25), 1)))

    assert optimal_points(slots, players, val) == brute_force(slots, players)


def test_unfillable_slot_is_left_empty_not_stuffed():
    slots = ["QB", "D/ST"]
    players = [P("QB1", ("QB",), 20.0)]
    filled = optimize_lineup(slots, players, val)
    assert filled[0].name == "QB1"
    assert filled[1] is None
    assert optimal_points(slots, players, val) == 20.0


def test_more_slots_than_players():
    slots = ["RB", "RB", "WR"]
    players = [P("RB1", ("RB",), 10.0)]
    filled = optimize_lineup(slots, players, val)
    assert sum(p is not None for p in filled) == 1


def test_no_players_and_no_slots():
    assert optimize_lineup([], [], val) == []
    assert optimize_lineup(["QB"], [], val) == [None]


def test_negative_projections_still_fill_a_required_slot():
    """A K projected below zero still has to start if he's the only one."""
    slots = ["K"]
    players = [P("K1", ("K",), -1.0)]
    assert optimize_lineup(slots, players, val)[0].name == "K1"


def test_players_are_never_assigned_twice():
    slots = ["RB", "RB/WR", "RB/WR/TE"]
    players = [P(f"RB{i}", ("RB", "RB/WR", "RB/WR/TE"), 10.0 + i) for i in range(5)]
    filled = optimize_lineup(slots, players, val)
    names = [p.name for p in filled if p]
    assert len(names) == len(set(names)) == 3
    assert optimal_points(slots, players, val) == 14.0 + 13.0 + 12.0


# ---------------------------------------------------------------------------
# Real leagues
# ---------------------------------------------------------------------------

_dumps = sorted(SAMPLES.glob("league_*_dump.json")) if SAMPLES.is_dir() else []


@pytest.mark.skipif(not _dumps, reason="run scripts/dump_league_settings.py first")
@pytest.mark.parametrize("path", _dumps, ids=lambda p: p.stem)
def test_real_lineups_parse(path):
    payload = json.loads(path.read_text())
    ls = LineupSlots.from_raw(payload["settings"]["rosterSettings"])
    assert 7 <= ls.starters <= 12, ls.starting_slots
    assert ls.bench > 0
    assert "QB" in ls.starting_slots


def test_dst_is_not_treated_as_a_flex_slot():
    """'D/ST' contains a slash but accepts exactly one position."""
    ls = LineupSlots.from_raw({"lineupSlotCounts": {"16": 1, "23": 1, "20": 5}})
    assert ls.flex_slots == ["RB/WR/TE"]
