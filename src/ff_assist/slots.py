"""Lineup structure and optimal lineup assignment.

Two things live here:

1. :class:`LineupSlots` — what a league's starting lineup actually looks like,
   parsed from ``rosterSettings.lineupSlotCounts``.
2. :func:`optimize_lineup` — the best legal assignment of players to slots.

The optimizer is exact, not greedy, because greedy is wrong in precisely the
situation that matters most — a flex decision.

Take one RB slot and one RB/WR flex, with RB1 18.0, RB2 17.0, WR1 15.0.
Greedy fills the RB slot with the best RB (RB1), then fills the flex with the
best remaining eligible player (WR1) for 33.0. The optimum is RB1 + RB2 = 35.0.
Greedy loses two points because it commits a slot before knowing what the flex
will need. The error is small in any single week and relentless across a
season, and it lands exactly on the close flex calls the plan identifies as
the third-largest source of edge.

So: Hungarian algorithm, O(n³) on ~10 slots and ~20 players — microseconds.
Verified against brute force on 60 random rosters in tests/test_slots.py.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from espn_api.football.constant import POSITION_MAP

__all__ = ["BENCH_SLOTS", "FLEX_SLOTS", "LineupSlots", "optimize_lineup", "optimal_points"]

#: Slots that are not part of the scoring lineup.
BENCH_SLOTS: frozenset[str] = frozenset({"BE", "IR", "ER", "Rookie", ""})

#: Slots that accept more than one position. Listed explicitly rather than
#: sniffed for a "/" — "D/ST" has a slash and is emphatically not a flex.
FLEX_SLOTS: frozenset[str] = frozenset(
    {"RB/WR", "WR/TE", "RB/WR/TE", "OP", "DP", "DL", "DB"}
)

#: Display order for a lineup, so briefs read the way ESPN shows them.
_SLOT_ORDER = [
    "QB", "TQB", "RB", "RB/WR", "WR", "WR/TE", "TE", "RB/WR/TE", "OP",
    "K", "P", "D/ST", "DT", "DE", "LB", "DL", "CB", "S", "DB", "DP", "HC",
]


def _slot_sort_key(slot: str) -> tuple[int, str]:
    try:
        return (_SLOT_ORDER.index(slot), slot)
    except ValueError:
        return (len(_SLOT_ORDER), slot)


@dataclass(frozen=True)
class LineupSlots:
    """A league's roster shape."""

    counts: dict[str, int]

    @classmethod
    def from_raw(cls, roster_settings: dict[str, Any]) -> LineupSlots:
        raw = roster_settings.get("lineupSlotCounts", {}) or {}
        counts: dict[str, int] = {}
        for slot_id, n in raw.items():
            if not n:
                continue
            name = POSITION_MAP.get(int(slot_id))
            if name is None:
                continue
            counts[name] = counts.get(name, 0) + int(n)
        return cls(counts=counts)

    @property
    def starting_slots(self) -> list[str]:
        """Every starting slot, expanded and in display order.

        ``['QB', 'RB', 'RB/WR', 'RB/WR', 'WR', 'TE', 'RB/WR/TE', 'K', 'D/ST']``
        """
        out: list[str] = []
        for slot in sorted(self.counts, key=_slot_sort_key):
            if slot in BENCH_SLOTS:
                continue
            out.extend([slot] * self.counts[slot])
        return out

    @property
    def starters(self) -> int:
        return len(self.starting_slots)

    @property
    def bench(self) -> int:
        return self.counts.get("BE", 0)

    @property
    def ir(self) -> int:
        return self.counts.get("IR", 0)

    @property
    def flex_slots(self) -> list[str]:
        return [s for s in self.starting_slots if s in FLEX_SLOTS]

    def summary(self) -> dict[str, Any]:
        return {
            "starters": self.starting_slots,
            "bench": self.bench,
            "ir": self.ir,
            "roster_size": self.starters + self.bench,
        }


# ---------------------------------------------------------------------------
# Optimal assignment
# ---------------------------------------------------------------------------

_UNFILLABLE = 1e9


def _hungarian(cost: list[list[float]]) -> list[int]:
    """Min-cost assignment for a rectangular matrix (rows <= cols).

    Standard O(n²m) potentials implementation. Returns column index per row,
    or -1 where a row is unassigned.
    """
    n, m = len(cost), len(cost[0])
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, -1
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j], way[j] = cur, j0
                if minv[j] < delta:
                    delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1

    assignment = [-1] * n
    for j in range(1, m + 1):
        if p[j] > 0:
            assignment[p[j] - 1] = j - 1
    return assignment


def optimize_lineup(
    slots: Sequence[str],
    players: Sequence[Any],
    value: Callable[[Any], float],
    eligible: Callable[[Any], Iterable[str]] = lambda p: getattr(p, "eligibleSlots", ()) or (),
) -> list[Any | None]:
    """Assign players to slots to maximize total ``value``.

    Returns a list parallel to ``slots``; ``None`` where no eligible player was
    available. Players not assigned to any slot are the optimal bench.
    """
    if not slots:
        return []
    if not players:
        return [None] * len(slots)

    eligibility = [set(eligible(p)) for p in players]
    n, m = len(slots), len(players)

    # Pad with dummy players so the matrix is never wider than it is tall.
    width = max(n, m)
    cost: list[list[float]] = []
    for slot in slots:
        row = []
        for j in range(width):
            if j >= m or slot not in eligibility[j]:
                row.append(_UNFILLABLE)
            else:
                row.append(-float(value(players[j])))
        cost.append(row)

    assignment = _hungarian(cost)
    out: list[Any | None] = []
    for i, j in enumerate(assignment):
        if j < 0 or j >= m or cost[i][j] >= _UNFILLABLE:
            out.append(None)
        else:
            out.append(players[j])
    return out


def optimal_points(
    slots: Sequence[str],
    players: Sequence[Any],
    value: Callable[[Any], float],
    eligible: Callable[[Any], Iterable[str]] = lambda p: getattr(p, "eligibleSlots", ()) or (),
) -> float:
    filled = optimize_lineup(slots, players, value, eligible)
    return round(sum(value(p) for p in filled if p is not None), 2)
