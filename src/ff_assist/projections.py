"""A second opinion on every projection, from Sleeper.

`docs/projections.md` has the evidence. The short version: across 2024 and
2025, replayed week by week under strict lookback, Sleeper and ESPN both beat
every projection this codebase can build from nflverse history by 2–3 points of
lineup per week, and **the mean of the two** was at or near the top in both
seasons — it swept 2024 outright and sat inside the standard error of the best
arm in 2025. Neither single source was consistent; the average was.

So this module exists to supply the second number, and the ranking uses the
mean. Three things about the design are deliberate.

**It scores Sleeper's stat line, not Sleeper's points.** The endpoint hands
back `pts_ppr` and it is tempting to use it. That number is standard full PPR:
1 point per 25 passing yards. `gladiator` scores a point per *20*, in buckets,
with no per-yard rule at all — so its quarterbacks would be understated by 25%
and its bucket categories would score zero, silently. Instead the projected
*stat line* is translated into ESPN's stat-id space and re-scored under each
league's own rules, exactly as ESPN's own `projected_breakdown` is. Everything
in this codebase is expressed in the league's own scoring; this is no exception.

**The translation is proved rather than trusted.** Sleeper reports both the
components and its own `pts_ppr`, which makes the crosswalk falsifiable: score
the components under standard PPR and the answer must reproduce `pts_ppr`.
:func:`verify_crosswalk` does that and is wired into ``check_external.py``.
A stat key we fail to map contributes zero and would quietly deflate a
projection, which is the exact failure mode this project keeps finding, so
unmapped keys are collected and reported rather than dropped.

**Absence is not zero.** Every function here degrades to ``None`` rather than a
number when Sleeper is unreachable, and the consensus falls back to whichever
source it does have. A missing second opinion should cost you the second
opinion, not the lineup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .dvp import to_espn_stat_line
from .scoring import ScoringSettings
from .usage import normalize_name

__all__ = [
    "SLEEPER_PROJECTIONS_URL",
    "SLEEPER_TO_NFLVERSE",
    "DISAGREEMENT_POINTS",
    "SleeperWeek",
    "fetch_week",
    "score_line",
    "consensus",
    "verify_crosswalk",
    "infer_scoring",
    "RECONSTRUCTION_TOLERANCE",
]

log = logging.getLogger("ff_assist.projections")

SLEEPER_PROJECTIONS_URL = "https://api.sleeper.com/projections/nfl/{season}/{week}"

#: Sleeper's stat key -> the nflverse column name of the same quantity.
#:
#: Routing through nflverse's vocabulary rather than straight to ESPN stat ids
#: is not indirection for its own sake: ``dvp.to_espn_stat_line`` already knows
#: how to derive ESPN's bucket and milestone categories from raw yardage, and
#: that logic is already under test. Duplicating it here is how the two copies
#: start to disagree.
SLEEPER_TO_NFLVERSE: dict[str, str] = {
    "pass_att": "attempts",
    "pass_cmp": "completions",
    "pass_yd": "passing_yards",
    "pass_td": "passing_tds",
    "pass_int": "passing_interceptions",
    "pass_2pt": "passing_2pt_conversions",
    "rush_att": "carries",
    "rush_yd": "rushing_yards",
    "rush_td": "rushing_tds",
    "rush_2pt": "rushing_2pt_conversions",
    "rec": "receptions",
    "rec_tgt": "targets",
    "rec_yd": "receiving_yards",
    "rec_td": "receiving_tds",
    "rec_2pt": "receiving_2pt_conversions",
    "fum_lost": "rushing_fumbles_lost",
}

#: Keys Sleeper returns that we knowingly ignore, so they never show up as
#: "unmapped" and send someone hunting for a bug. These are either summary
#: points totals (which we deliberately recompute), rate stats, or categories
#: no league here scores.
_IGNORED_KEYS = frozenset(
    {
        "pts_ppr", "pts_half_ppr", "pts_std", "pts_ppr_dk", "pts_ppr_fd",
        "pts_std_dk", "pts_std_fd", "pts_half_ppr_dk", "pts_half_ppr_fd",
        "gp", "gms_active", "gs", "off_snp", "tm_off_snp", "cmp_pct",
        "pass_rtg", "pass_ypa", "pass_ypc", "rush_ypa", "rec_ypr", "rec_ypt",
        "pass_fd", "rush_fd", "rec_fd", "fum", "pass_sack", "pass_sack_yds",
        "bonus_rec_te", "bonus_rush_yd_100", "bonus_rec_yd_100",
        "bonus_pass_yd_300", "bonus_rush_yd_200", "bonus_rec_yd_200",
        "bonus_pass_yd_400", "bonus_pass_cmp_25", "bonus_rush_att_20",
        "bonus_rec_rb", "bonus_rec_wr", "rush_rec_yd", "anytime_tds",
        "st_snp", "def_snp", "tm_def_snp", "tm_st_snp",
    }
)

#: How far apart ESPN and Sleeper have to be before the disagreement is worth a
#: sentence. Three points is roughly where a flex decision starts to turn, and
#: below it the two sources are agreeing to within their own noise.
DISAGREEMENT_POINTS = 3.0

#: How far our reconstruction of a Sleeper stat line may sit from Sleeper's own
#: published total before we stop trusting it for that player. Half a point is
#: generous against rounding and mean nothing to a lineup; beyond it we are
#: missing a scoring category and the projection is understated by an amount we
#: cannot see.
RECONSTRUCTION_TOLERANCE = 0.5

#: Standard full-PPR scoring, used only to prove the crosswalk against Sleeper's
#: own published total. Not used for anything a user sees.
_STANDARD_PPR_RAW = {
    "scoringSettings": {
        "scoringItems": [
            {"statId": 3, "points": 0.04},    # passing yards
            {"statId": 4, "points": 4.0},     # passing TD
            {"statId": 20, "points": -2.0},   # interception
            {"statId": 19, "points": 2.0},    # passing 2pt
            {"statId": 24, "points": 0.1},    # rushing yards
            {"statId": 25, "points": 6.0},    # rushing TD
            {"statId": 26, "points": 2.0},    # rushing 2pt
            {"statId": 53, "points": 1.0},    # receptions
            {"statId": 42, "points": 0.1},    # receiving yards
            {"statId": 43, "points": 6.0},    # receiving TD
            {"statId": 44, "points": 2.0},    # receiving 2pt
            {"statId": 72, "points": -2.0},   # fumbles lost
        ]
    }
}


@dataclass
class SleeperWeek:
    """One week of Sleeper projections, keyed by normalized player name.

    ``lines`` holds only players whose stat line we could reconstruct to within
    :data:`RECONSTRUCTION_TOLERANCE` of Sleeper's own published total. Players
    who fail that check are moved to ``rejected`` and get no second opinion at
    all, which is the honest outcome: a projection we cannot reproduce is a
    number we do not understand, and ranking on it would be worse than falling
    back to ESPN for that one player.
    """

    season: int
    week: int
    #: normalized name -> {nflverse column: value}, ready for to_espn_stat_line
    lines: dict[str, dict[str, float]] = field(default_factory=dict)
    #: normalized name -> Sleeper's own standard-PPR total, for verification
    ppr: dict[str, float] = field(default_factory=dict)
    #: normalized name -> the raw stat dict, kept for infer_scoring()
    raw: dict[str, dict[str, float]] = field(default_factory=dict)
    #: Sleeper stat keys we did not recognise **and which carried a non-zero
    #: value**. A key present at zero contributes nothing whether we map it or
    #: not, and flagging those trains the eye to ignore the warning.
    unmapped: set[str] = field(default_factory=set)
    #: normalized name -> (our score, Sleeper's own total) for players dropped
    #: because we could not reproduce their published points.
    rejected: dict[str, tuple[float, float]] = field(default_factory=dict)
    #: mean |our score - Sleeper's own| across every player we could check.
    mean_residual: float = 0.0

    def __bool__(self) -> bool:
        return bool(self.lines)

    def line_for(self, name: str) -> dict[str, float] | None:
        return self.lines.get(normalize_name(name))

    @property
    def coverage(self) -> str:
        """Players we trust, over players Sleeper returned."""
        total = len(self.lines) + len(self.rejected)
        return f"{len(self.lines)}/{total}"


def fetch_week(
    season: int, week: int, *, positions: tuple[str, ...] = ("QB", "RB", "WR", "TE"),
    fetch: Any = None,
) -> SleeperWeek:
    """Sleeper's projected stat lines for a week.

    Returns an empty :class:`SleeperWeek` on any failure — unreachable, empty,
    or malformed. The caller is expected to carry on with ESPN alone.

    ``fetch`` is injectable so tests never touch the network; it receives
    ``(url, params)`` and returns parsed JSON.
    """
    if fetch is None:

        def fetch(url: str, params: dict[str, Any]) -> Any:  # pragma: no cover
            import httpx

            return httpx.get(url, params=params, timeout=20).json()

    out = SleeperWeek(season=season, week=week)
    try:
        payload = fetch(
            SLEEPER_PROJECTIONS_URL.format(season=season, week=week),
            {"season_type": "regular", "position[]": list(positions),
             "order_by": "pts_ppr"},
        )
    except Exception as exc:  # noqa: BLE001 — a second opinion is optional
        log.info("Sleeper projections unavailable: %s", type(exc).__name__)
        return out

    for entry in payload or []:
        player = (entry or {}).get("player") or {}
        name = player.get("full_name") or " ".join(
            x for x in (player.get("first_name"), player.get("last_name")) if x
        )
        stats = (entry or {}).get("stats") or {}
        if not name or not stats:
            continue

        key = normalize_name(name)
        line: dict[str, float] = {}
        for stat_key, value in stats.items():
            column = SLEEPER_TO_NFLVERSE.get(stat_key)
            if column is not None:
                if value:
                    line[column] = float(value)
            elif value and stat_key not in _IGNORED_KEYS:
                # Only a key carrying an actual value can deflate a projection.
                # Flagging the zeroes made 19 keys look like 19 problems.
                out.unmapped.add(stat_key)

        if line:
            out.lines[key] = line
            out.raw[key] = {k: float(v) for k, v in stats.items() if isinstance(v, int | float)}
        if stats.get("pts_ppr") is not None:
            out.ppr[key] = float(stats["pts_ppr"])

    _reject_unreproducible(out)

    if out.unmapped:
        log.warning(
            "Sleeper returned %d unrecognised stat keys carrying values (%s) — they "
            "score zero, so projections are understated. Update SLEEPER_TO_NFLVERSE.",
            len(out.unmapped), ", ".join(sorted(out.unmapped)[:8]),
        )
    if out.rejected:
        log.warning(
            "Dropped %d of %d Sleeper projections we could not reproduce from their "
            "own stat lines (mean residual %.2f). Those players fall back to ESPN.",
            len(out.rejected), len(out.rejected) + len(out.lines), out.mean_residual,
        )
    return out


def _reject_unreproducible(week: SleeperWeek) -> None:
    """Drop players whose published total we cannot rebuild from their stats.

    This is the guard that matters, and its absence was the real defect in the
    first version. ``verify_crosswalk`` existed and ``check_external.py`` ran
    it, but nothing on the path that actually sets a lineup consulted it — so a
    broken crosswalk produced a quietly understated projection with no symptom.
    A check that only runs in a diagnostic is not a guard.

    Per player rather than per week on purpose. When 17% of a slate fails, the
    other 83% is still a good second opinion, and throwing the source away
    entirely would cost more than the bug does.
    """
    standard = ScoringSettings.from_raw(_STANDARD_PPR_RAW, "standard-ppr")
    residuals: list[float] = []

    for name in list(week.lines):
        expected = week.ppr.get(name)
        if expected is None:
            continue  # nothing to check against; keep it and say so elsewhere
        ours = standard.score(to_espn_stat_line(week.lines[name])).points
        residual = abs(ours - expected)
        residuals.append(residual)
        if residual > RECONSTRUCTION_TOLERANCE:
            week.rejected[name] = (round(ours, 2), round(expected, 2))
            del week.lines[name]

    if residuals:
        week.mean_residual = round(sum(residuals) / len(residuals), 3)


def score_line(
    line: dict[str, float] | None,
    scoring: ScoringSettings,
    position: str | None = None,
) -> float | None:
    """Score a Sleeper stat line under one league's rules.

    None means "no line", which is different from a projection of zero and must
    stay different all the way to the caller.
    """
    if not line:
        return None
    return scoring.score(to_espn_stat_line(line), position).points


def consensus(
    espn: float | None, sleeper: float | None
) -> tuple[float | None, str]:
    """The number to rank on, and a one-word account of where it came from.

    Equal weighting, which is not laziness: the twelve-season Fantasy Football
    Analytics study found the simple average beat weighted averages, and the
    bake-off here reproduced it. Weighting invites tuning on one season.
    """
    have = [v for v in (espn, sleeper) if v is not None]
    if not have:
        return None, "none"
    if len(have) == 1:
        return round(have[0], 2), "espn" if sleeper is None else "sleeper"
    return round((espn + sleeper) / 2.0, 2), "both"


def disagreement(espn: float | None, sleeper: float | None) -> float | None:
    """Signed gap, Sleeper minus ESPN, when it is large enough to mention.

    Where the two sources agree there is nothing to say — the consensus already
    carries it. Where they split by several points on a startable player, that
    is the one moment in the week a second opinion earns its keep.
    """
    if espn is None or sleeper is None:
        return None
    gap = sleeper - espn
    return round(gap, 2) if abs(gap) >= DISAGREEMENT_POINTS else None


def verify_crosswalk(week: SleeperWeek) -> dict[str, Any]:
    """Report how well :data:`SLEEPER_TO_NFLVERSE` reproduced Sleeper's totals.

    Sleeper publishes both the projected components and `pts_ppr`, so scoring
    the components under standard full PPR must land on that total. That check
    now runs at fetch time in :func:`_reject_unreproducible` — this function
    only *reports* what it found.

    That split matters. An earlier version recomputed the check here, over
    ``week.lines`` — which by then held only the players that had already
    passed. It could not fail, and a verifier that cannot fail is decoration.

    A high failure rate is ambiguous between two very different bugs, and
    :func:`infer_scoring` is what tells them apart: a stat key we never mapped
    (Sleeper's data is fine, our translation is short) versus a wrong
    coefficient in :data:`_STANDARD_PPR_RAW` (our translation is fine, our
    *yardstick* is wrong and the rejections are false).
    """
    checked = len(week.lines) + len(week.rejected)
    if not checked:
        return {"checked": 0, "error": "no projections to verify"}

    out: dict[str, Any] = {
        "checked": checked,
        "failed": len(week.rejected),
        "mean_residual": week.mean_residual,
        "unmapped": sorted(week.unmapped),
    }
    if week.rejected:
        name, (ours, theirs) = max(
            week.rejected.items(), key=lambda kv: abs(kv[1][0] - kv[1][1])
        )
        out["worst"] = {"player": name, "ours": ours, "sleeper": theirs}
        # A failure rate this high with a modest mean residual is the signature
        # of a systematically wrong yardstick, not of missing data.
        rate = len(week.rejected) / checked
        if rate > 0.10 and week.mean_residual < 2.0:
            out["hint"] = (
                "many players off by a little — this looks like _STANDARD_PPR_RAW "
                "mispricing a category rather than the crosswalk missing one. "
                "Run infer_scoring() before changing the map."
            )
    return out


def infer_scoring(
    week: SleeperWeek, *, ridge: float = 1e-6, min_players: int = 50
) -> dict[str, Any]:
    """Solve for the scoring rule Sleeper's own `pts_ppr` actually implies.

    Guessing which stat key we failed to map is a game that takes several
    rounds and can be skipped entirely. Sleeper hands us, for every player, a
    vector of projected stats and the points total it derives from them. That
    is a linear system: least squares over the raw stat keys recovers the
    per-unit value of each one.

    A key with a substantial fitted coefficient that is missing from
    :data:`SLEEPER_TO_NFLVERSE` is exactly the bug. A key we *do* map whose
    fitted coefficient differs from what :data:`_STANDARD_PPR_RAW` assumes is a
    different bug — the reference ruleset, not the crosswalk — and the first
    version of the verifier could not tell those two apart.

    Pure Python on purpose: normal equations with a ridge term, twenty-odd
    unknowns and several hundred rows. numpy is not a dependency of this
    project and is not worth becoming one for this.
    """
    rows = [(week.raw[n], week.ppr[n]) for n in week.raw if n in week.ppr]
    rows += [
        (week.raw[n], week.ppr[n]) for n in week.rejected if n in week.raw and n in week.ppr
    ]
    if len(rows) < min_players:
        return {"error": f"only {len(rows)} players with both stats and a total"}

    # Candidate regressors: every numeric key that ever carries a value, minus
    # the points totals themselves (which would fit with coefficient 1 and
    # explain everything while teaching nothing).
    excluded = {k for k in _IGNORED_KEYS if k.startswith(("pts_", "adp_"))}
    keys = sorted(
        {k for stats, _ in rows for k, v in stats.items() if v and k not in excluded}
    )
    if not keys:
        return {"error": "no candidate stat keys"}

    n = len(keys)
    # Normal equations: (XtX + ridge*I) beta = Xty
    xtx = [[0.0] * n for _ in range(n)]
    xty = [0.0] * n
    for stats, total in rows:
        vec = [float(stats.get(k) or 0.0) for k in keys]
        for i in range(n):
            if not vec[i]:
                continue
            xty[i] += vec[i] * total
            for j in range(n):
                if vec[j]:
                    xtx[i][j] += vec[i] * vec[j]
    for i in range(n):
        xtx[i][i] += ridge

    beta = _solve(xtx, xty)
    if beta is None:
        return {"error": "singular system — stat keys are collinear"}

    fitted = {k: round(b, 4) for k, b in zip(keys, beta, strict=True)}

    # What the current code believes, for comparison.
    assumed = {
        "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -2.0, "pass_2pt": 2.0,
        "rush_yd": 0.1, "rush_td": 6.0, "rush_2pt": 2.0,
        "rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0, "rec_2pt": 2.0,
        "fum_lost": -2.0,
    }

    missing = {
        k: v for k, v in fitted.items()
        if k not in SLEEPER_TO_NFLVERSE and abs(v) >= 0.01
    }
    wrong = {
        k: {"assumed": assumed[k], "fitted": v}
        for k, v in fitted.items()
        if k in assumed and abs(v - assumed[k]) > max(0.02, abs(assumed[k]) * 0.1)
    }
    return {
        "players": len(rows),
        "fitted": fitted,
        "unmapped_but_scored": missing,
        "mapped_but_mispriced": wrong,
    }


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting. Returns None if singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        inv = 1.0 / m[col][col]
        for r in range(n):
            if r == col:
                continue
            factor = m[r][col] * inv
            if factor:
                for c in range(col, n + 1):
                    m[r][c] -= factor * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]
