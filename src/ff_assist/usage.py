"""Usage trends from nflverse weekly data.

Usage is the input that leads results. A receiver whose target share has gone
18% -> 24% -> 29% is a different asset from one averaging 24% and drifting
down, even when their season lines are identical. ESPN's projection sees the
average; this module sees the direction.

Joining ESPN rosters to nflverse is a name-matching problem, and it fails
silently when it fails at all — ESPN says "D.K. Metcalf", nflverse says
"DK Metcalf", and an exact match just returns nothing for that player. So
names are normalized (punctuation, generational suffixes) before matching,
and genuine ambiguity is reported rather than resolved by guessing: there
really are two Michael Carters and two Byron Murphys.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import polars as pl

__all__ = [
    "normalize_name",
    "build_name_index",
    "resolve_player",
    "player_usage",
    "usage_for_roster",
]

_SUFFIXES = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def normalize_name(name: str | None) -> str:
    """Fold the spelling differences between ESPN and nflverse.

    Separators are removed entirely rather than collapsed to spaces, because
    the two sources disagree in both directions: ESPN writes "D.K. Metcalf"
    where nflverse writes "DK Metcalf" (a space appears), and "Amon-Ra
    St. Brown" where nflverse has "Amon-Ra St.Brown" (a space disappears).
    Any rule that preserves spacing gets one of those pairs wrong.

    Generational suffixes go first, while word boundaries still exist:
    "Travis Etienne Jr." matches "Travis Etienne".

        >>> normalize_name("D.K. Metcalf") == normalize_name("DK Metcalf")
        True
    """
    if not name:
        return ""
    s = name.lower().replace("'", "").replace("-", " ").replace(".", " ")
    s = _SUFFIXES.sub("", s)
    return re.sub(r"[^a-z0-9]", "", s)


def build_name_index(frame: pl.DataFrame, name_col: str = "player_display_name") -> dict[str, list[str]]:
    """normalized name -> the distinct source spellings that produced it."""
    index: dict[str, list[str]] = {}
    for name in frame[name_col].unique().to_list():
        if not name:
            continue
        index.setdefault(normalize_name(name), []).append(name)
    return {k: sorted(set(v)) for k, v in index.items()}


def resolve_player(
    name: str,
    frame: pl.DataFrame,
    *,
    position: str | None = None,
    team: str | None = None,
    name_col: str = "player_display_name",
) -> tuple[str | None, str | None]:
    """Map an ESPN name to its nflverse spelling.

    Returns ``(resolved_name, problem)``. ``problem`` is non-None when we could
    not resolve confidently — an unresolved player must surface as unknown
    rather than as a player with no usage, which reads like a benching.
    """
    key = normalize_name(name)
    if not key:
        return (None, "empty name")

    candidates = build_name_index(frame, name_col).get(key)
    if not candidates:
        return (None, f"no nflverse player matching {name!r}")
    if len(candidates) == 1:
        return (candidates[0], None)

    # Genuine homonyms (Michael Carter RB vs Michael Carter II S). Narrow by
    # position and team before giving up.
    narrowed = frame.filter(pl.col(name_col).is_in(candidates))
    if position and "position" in narrowed.columns:
        narrowed = narrowed.filter(pl.col("position") == position)
    if team and "team" in narrowed.columns:
        narrowed = narrowed.filter(pl.col("team") == team)

    remaining = sorted({n for n in narrowed[name_col].to_list() if n})
    if len(remaining) == 1:
        return (remaining[0], None)
    return (None, f"ambiguous: {name!r} matches {candidates}")


@dataclass(frozen=True)
class UsageWeek:
    week: int
    opponent: str | None
    snap_pct: float | None
    targets: int | None
    target_share: float | None
    air_yards_share: float | None
    wopr: float | None
    carries: int | None
    ppr_points: float | None


def _trend(values: list[float | None]) -> dict[str, Any]:
    """Direction of a usage series: mean of the last three vs the three before.

    Needs at least four observations. Fewer than that and the honest answer is
    "not enough data" — a two-game "trend" is noise with a direction attached.
    """
    seen = [v for v in values if v is not None]
    if len(seen) < 4:
        return {"direction": "insufficient_data", "n": len(seen)}
    recent = seen[-3:]
    prior = seen[-6:-3] or seen[:-3]
    if not prior:
        return {"direction": "insufficient_data", "n": len(seen)}
    r, p = sum(recent) / len(recent), sum(prior) / len(prior)
    delta = r - p
    # 15% relative change is the threshold for calling it a move rather than
    # week-to-week noise.
    if p and abs(delta) / abs(p) < 0.15:
        direction = "flat"
    else:
        direction = "rising" if delta > 0 else "falling"
    return {
        "direction": direction,
        "recent_avg": round(r, 3),
        "prior_avg": round(p, 3),
        "delta": round(delta, 3),
        "n": len(seen),
    }


def player_usage(
    name: str,
    season: int,
    *,
    weeks: int = 6,
    position: str | None = None,
    team: str | None = None,
    stats: pl.DataFrame | None = None,
    snaps: pl.DataFrame | None = None,
) -> dict[str, Any]:
    """Recent usage for one player, with a direction on the headline metric."""
    if stats is None:
        import nflreadpy as nfl

        stats = nfl.load_player_stats([season])
    if snaps is None:
        try:
            import nflreadpy as nfl

            snaps = nfl.load_snap_counts([season])
        except Exception:  # noqa: BLE001 — snap counts are a bonus, not required
            snaps = None

    stats = stats.filter(pl.col("season_type") == "REG") if "season_type" in stats.columns else stats
    resolved, problem = resolve_player(name, stats, position=position, team=team)
    if resolved is None:
        return {"player": name, "season": season, "error": problem}

    rows = stats.filter(pl.col("player_display_name") == resolved).sort("week")
    snap_by_week: dict[int, float] = {}
    if snaps is not None and "player" in snaps.columns:
        s_resolved, _ = resolve_player(name, snaps, position=position, team=team, name_col="player")
        if s_resolved:
            for r in snaps.filter(pl.col("player") == s_resolved).iter_rows(named=True):
                snap_by_week[r["week"]] = r.get("offense_pct")

    def get(row: dict[str, Any], col: str) -> Any:
        return row.get(col) if col in rows.columns else None

    history: list[UsageWeek] = [
        UsageWeek(
            week=r["week"],
            opponent=get(r, "opponent_team"),
            snap_pct=snap_by_week.get(r["week"]),
            targets=get(r, "targets"),
            target_share=round(v, 4) if (v := get(r, "target_share")) is not None else None,
            air_yards_share=round(v, 4) if (v := get(r, "air_yards_share")) is not None else None,
            wopr=round(v, 4) if (v := get(r, "wopr")) is not None else None,
            carries=get(r, "carries"),
            ppr_points=get(r, "fantasy_points_ppr"),
        )
        for r in rows.iter_rows(named=True)
    ]
    window = history[-weeks:]

    # Headline metric depends on how the player is actually used: a back's
    # workload lives in snaps and carries, a receiver's in target share.
    pos = position or (rows["position"][0] if rows.height and "position" in rows.columns else None)
    if pos == "RB":
        headline, series = "carries", [w.carries for w in history]
    elif pos == "QB":
        headline, series = "ppr_points", [w.ppr_points for w in history]
    else:
        headline, series = "target_share", [w.target_share for w in history]

    return {
        "player": resolved,
        "position": pos,
        "season": season,
        "weeks": [
            {k: v for k, v in w.__dict__.items() if v is not None} for w in window
        ],
        "headline_metric": headline,
        "trend": _trend(series),
        "snap_pct_last3": [w.snap_pct for w in window[-3:] if w.snap_pct is not None] or None,
    }


def usage_for_roster(
    names: list[tuple[str, str | None, str | None]],
    season: int,
    *,
    weeks: int = 4,
    stats: pl.DataFrame | None = None,
    snaps: pl.DataFrame | None = None,
) -> dict[str, dict[str, Any]]:
    """Compact usage summaries for a whole roster, keyed by the ESPN name.

    Loads the season's frames once and reuses them — the per-player loader
    would otherwise re-fetch for every row on the slate.
    """
    if stats is None:
        import nflreadpy as nfl

        stats = nfl.load_player_stats([season])
    if snaps is None:
        try:
            import nflreadpy as nfl

            snaps = nfl.load_snap_counts([season])
        except Exception:  # noqa: BLE001
            snaps = None

    out: dict[str, dict[str, Any]] = {}
    for name, position, team in names:
        u = player_usage(
            name, season, weeks=weeks, position=position, team=team, stats=stats, snaps=snaps
        )
        if "error" in u:
            out[name] = {"error": u["error"]}
            continue
        recent = u["weeks"][-1] if u["weeks"] else {}
        out[name] = {
            "trend": u["trend"]["direction"],
            "headline": u["headline_metric"],
            "last": {
                k: recent.get(k)
                for k in ("target_share", "carries", "snap_pct", "wopr")
                if recent.get(k) is not None
            },
        }
    return out
