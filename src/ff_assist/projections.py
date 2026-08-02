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

Known limitation, bounded and deliberate
----------------------------------------

**Quarterbacks get ESPN only.** Sleeper's published `pts_ppr` for a QB is about
3-4 points higher than we can rebuild from the stat line it returns alongside
it — every quarterback, no other position. Those players fail the reconstruction
check and fall back to ESPN, which is the designed behaviour rather than an
outage.

It is not for want of looking. Twelve rounds of diagnosis, four purpose-built
tools and a search over every plausible price for every returned stat produced
no rule that closes the gap: the best fit is an interception worth *plus* two
points, and it only looks tidy because projected interceptions sit near 0.96 for
every starter, so 0.96 x 4 lands on the gap by arithmetic accident. Nothing else
lands on a value any scoring format uses.

The likely explanation is terminal by construction: the missing category is not
in the payload. Reconstructing a total from its components cannot close if a
component is never returned, no matter how the remaining columns are reweighted.

The cost is about 0.07 bench points per lineup per week, roughly a hundredth of
a win a season. `check_external.py --sleeper --diagnose` re-tests it in seconds
if Sleeper ever starts returning the missing field.
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
    "contrast_rejected",
    "propose_fixes",
    "explain_player",
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

#: Keys that are structurally not scoring categories at all: draft positions,
#: ranks, rate stats, snap counts, and the points totals we deliberately
#: recompute. Nothing here could ever be worth points, so it is excluded from
#: both the unmapped warning and the diagnostic regression.
_NON_SCORING_KEYS = frozenset(
    {
        "pts_ppr", "pts_half_ppr", "pts_std", "pts_ppr_dk", "pts_ppr_fd",
        "pts_std_dk", "pts_std_fd", "pts_half_ppr_dk", "pts_half_ppr_fd",
        "gp", "gms_active", "gs", "off_snp", "tm_off_snp", "cmp_pct",
        "pass_rtg", "pass_ypa", "pass_ypc", "rush_ypa", "rec_ypr", "rec_ypt",
        "pass_sack_yds", "rush_rec_yd", "anytime_tds",
        "st_snp", "def_snp", "tm_def_snp", "tm_st_snp",
    }
)

#: Real stat categories that standard full PPR is *believed* not to pay for.
#: They are suppressed from the unmapped warning to keep it readable — but they
#: stay in the diagnostic regression, deliberately.
#:
#: The distinction matters. An earlier version lumped these in with the
#: structural noise above and excluded them from the fit too, which made the
#: diagnostic incapable of discovering that one of them *is* scored: ignored
#: because assumed unscored, assumption unfalsifiable because ignored. A tool
#: built to challenge an assumption must not inherit it.
_ASSUMED_UNSCORED = frozenset(
    {
        "pass_fd", "rush_fd", "rec_fd", "fum", "pass_sack",
        "bonus_rec_te", "bonus_rush_yd_100", "bonus_rec_yd_100",
        "bonus_pass_yd_300", "bonus_rush_yd_200", "bonus_rec_yd_200",
        "bonus_pass_yd_400", "bonus_pass_cmp_25", "bonus_rush_att_20",
        "bonus_rec_rb", "bonus_rec_wr",
    }
)

_IGNORED_KEYS = _NON_SCORING_KEYS | _ASSUMED_UNSCORED

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
    #: normalized name -> position, so a failure can be described by who it hit
    positions: dict[str, str] = field(default_factory=dict)
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

    def rejected_by_position(self) -> dict[str, tuple[int, int]]:
        """position -> (rejected, total). Names the shape of a failure.

        When every rejection lands on one position the diagnosis is a sentence,
        not a table — and it took several rounds of statistics to notice that
        the six "candidate causes" in the contrast output were six passing
        stats, i.e. one position. Saying it directly saves the next reader that
        detour.
        """
        counts: dict[str, list[int]] = {}
        for name in list(self.lines) + list(self.rejected):
            position = self.positions.get(name)
            if not position:
                continue
            row = counts.setdefault(position, [0, 0])
            row[1] += 1
            if name in self.rejected:
                row[0] += 1
        return {k: (v[0], v[1]) for k, v in sorted(counts.items())}


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
            position = player.get("position") or next(
                iter(player.get("fantasy_positions") or []), None
            )
            if position:
                out.positions[key] = position
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
    week: SleeperWeek, *, ridge: float = 1.0, min_players: int = 50
) -> dict[str, Any]:
    """Which stat key are we failing to map? Solved, not guessed.

    Sleeper hands us a stat vector and the points total it derives from them,
    for every player. Subtract what standard PPR says those stats are worth and
    the leftover is, by construction, the contribution of whatever we did not
    map. Regressing that residual on the unmapped keys names the category.

    **Only the residual is fitted, and only against the unmapped keys.** A first
    version solved for all scoring coefficients at once and produced nonsense —
    interceptions worth *plus* two points, passing yards at six times their real
    rate — because a projected stat line is close to rank-deficient. Yards,
    attempts, completions, incompletions, first downs and sacks are one latent
    "how much will he play" variable wearing six hats, and least squares splits
    a real effect arbitrarily across columns that move together. Holding the
    known coefficients fixed removes most of that; there is nothing to learn
    about the price of a receiving yard.

    A **split-half stability** check follows: the fit is repeated on two halves
    and a coefficient is only reported if both agree on sign and rough size.

    **That check is weaker than it sounds, and this docstring used to oversell
    it.** Split-half measures *sampling* variability. Collinearity bias is not
    sampling noise — correlated columns are correlated in both halves
    identically, so both halves make the same wrong attribution, agree with
    each other, and the coefficient is reported as stable. On real data this
    function still returned a fumble worth +1.1 points and a 40-yard run worth
    minus half a point, and the stability check passed them.

    So treat the output as a list of *suspects*, never as measurements. The
    tools that actually decide are :func:`contrast_rejected`, which estimates
    nothing, and :func:`propose_fixes`, which tests a specific hypothesis
    against a countable outcome. Prefer both to this.
    """
    standard = ScoringSettings.from_raw(_STANDARD_PPR_RAW, "standard-ppr")

    # Every player exactly once. `raw` already holds the rejected ones — an
    # earlier version added them again from `rejected` and reported 497 rows
    # for a 462-player week.
    rows: list[tuple[dict[str, float], float]] = []
    for name, stats in week.raw.items():
        total = week.ppr.get(name)
        if total is None:
            continue
        line = week.lines.get(name)
        if line is None:
            line = {
                col: stats[k]
                for k, col in SLEEPER_TO_NFLVERSE.items()
                if stats.get(k)
            }
        ours = standard.score(to_espn_stat_line(line)).points
        rows.append((stats, total - ours))

    if len(rows) < min_players:
        return {"error": f"only {len(rows)} players with both stats and a total"}

    keys = sorted(
        {
            k
            for stats, _ in rows
            for k, v in stats.items()
            if v and k not in SLEEPER_TO_NFLVERSE and not _is_noise_key(k)
        }
    )
    if not keys:
        return {"error": "no unmapped stat keys carry a value"}

    full = _ridge_fit(rows, keys, ridge)
    if full is None:
        return {"error": "singular system"}

    half_a = _ridge_fit(rows[0::2], keys, ridge)
    half_b = _ridge_fit(rows[1::2], keys, ridge)

    residuals = [r for _s, r in rows]
    mean_r = sum(residuals) / len(residuals)
    ss_tot = sum((r - mean_r) ** 2 for r in residuals)
    ss_res = sum(
        (r - sum(full[k] * (stats.get(k) or 0.0) for k in keys)) ** 2
        for stats, r in rows
    )

    stable: dict[str, float] = {}
    unstable: list[str] = []
    for k in keys:
        coefficient = full[k]
        if abs(coefficient) < 0.01:
            continue
        a, b = (half_a or {}).get(k), (half_b or {}).get(k)
        if a is None or b is None or a * b <= 0 or _disagree(a, b):
            unstable.append(k)
        else:
            stable[k] = round(coefficient, 3)

    return {
        "players": len(rows),
        "mean_residual": round(mean_r, 3),
        "variance_explained": round(1 - ss_res / ss_tot, 3) if ss_tot else None,
        "unmapped_but_scored": dict(
            sorted(stable.items(), key=lambda kv: -abs(kv[1]))
        ),
        "unstable": sorted(unstable),
    }


def _disagree(a: float, b: float) -> bool:
    """True when two half-sample estimates are too far apart to report."""
    lo, hi = sorted((abs(a), abs(b)))
    return hi > max(2.0 * lo, lo + 0.05)


def _is_noise_key(key: str) -> bool:
    """Structurally incapable of being a scoring category.

    Draft positions, ranks, rate stats and points totals. ADP values run into
    the hundreds and would dominate a least-squares fit numerically while
    meaning nothing; leaving them in was a real bug.

    Note this checks :data:`_NON_SCORING_KEYS`, **not** ``_IGNORED_KEYS``.
    Categories we merely *assume* are unscored stay in the regression so the
    assumption can be caught out.
    """
    return (
        key in _NON_SCORING_KEYS
        or "adp" in key
        or key.startswith(("pts_", "rank_", "pos_rank"))
    )


def _ridge_fit(
    rows: list[tuple[dict[str, float], float]], keys: list[str], ridge: float
) -> dict[str, float] | None:
    """Ridge regression on standardised columns, returned in original units."""
    n = len(keys)
    if not rows:
        return None

    scale = []
    for k in keys:
        values = [abs(stats.get(k) or 0.0) for stats, _ in rows]
        peak = max(values) or 1.0
        scale.append(peak)

    xtx = [[0.0] * n for _ in range(n)]
    xty = [0.0] * n
    for stats, target in rows:
        vec = [(stats.get(keys[i]) or 0.0) / scale[i] for i in range(n)]
        for i in range(n):
            if not vec[i]:
                continue
            xty[i] += vec[i] * target
            for j in range(n):
                if vec[j]:
                    xtx[i][j] += vec[i] * vec[j]
    for i in range(n):
        xtx[i][i] += ridge

    beta = _solve(xtx, xty)
    if beta is None:
        return None
    return {keys[i]: beta[i] / scale[i] for i in range(n)}


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


#: What :data:`_STANDARD_PPR_RAW` believes each Sleeper key is worth. Kept
#: separately from the ESPN-stat-id form because the fix search needs to reason
#: in Sleeper's own vocabulary.
_ASSUMED_PRICE: dict[str, float] = {
    "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -2.0, "pass_2pt": 2.0,
    "rush_yd": 0.1, "rush_td": 6.0, "rush_2pt": 2.0,
    "rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0, "rec_2pt": 2.0,
    "fum_lost": -2.0,
}

#: Prices worth trying. Every value a real fantasy format actually uses, plus
#: zero for "this category is not scored at all".
_CANDIDATE_PRICES = (
    0.0, 0.025, 0.04, 0.05, 0.1, 0.2, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0,
    -0.25, -0.5, -1.0, -2.0, -3.0,
)


def contrast_rejected(week: SleeperWeek, *, top: int = 10) -> list[dict[str, Any]]:
    """What do the players we cannot reproduce have that the others do not?

    A regression estimates coefficients and can be fooled by correlated
    columns. This does not estimate anything: it asks which stat keys are
    *present* in the failures and absent from the successes. A key that appears
    in every broken player and almost no working one is the culprit, and no
    amount of collinearity changes that.

    Returned rows carry ``implied`` — the residual divided by the key's value,
    which is what its price would have to be if that key alone explained the
    gap. Read it as a hypothesis to test with :func:`propose_fixes`, not as a
    measurement.
    """
    kept = [n for n in week.lines if n in week.raw]
    broken = [n for n in week.rejected if n in week.raw]
    if not broken or not kept:
        return []

    keys = {
        k
        for n in broken + kept
        for k, v in week.raw[n].items()
        if v and not _is_noise_key(k)
    }

    rows: list[dict[str, Any]] = []
    for key in keys:
        in_broken = [n for n in broken if week.raw[n].get(key)]
        if not in_broken:
            continue
        p_broken = len(in_broken) / len(broken)
        p_kept = sum(1 for n in kept if week.raw[n].get(key)) / len(kept)
        implied = sorted(
            (week.rejected[n][1] - week.rejected[n][0]) / week.raw[n][key]
            for n in in_broken
        )
        rows.append(
            {
                "key": key,
                "in_failures": round(p_broken, 3),
                "in_successes": round(p_kept, 3),
                "lift": round(p_broken - p_kept, 3),
                "implied": round(implied[len(implied) // 2], 3),
                "mapped": key in SLEEPER_TO_NFLVERSE,
            }
        )
    rows.sort(key=lambda r: -r["lift"])

    # When the leading keys all sit near 100%/0% together they are not competing
    # explanations — they co-occur, and the failures are a *subgroup* rather than
    # a category. Six rows saying "every passing stat" is one finding: the
    # quarterbacks. Saying so stops the next tool from trying to pick between
    # columns that are perfectly confounded.
    leaders = [r for r in rows if r["lift"] > 0.9]
    if len(leaders) >= 3:
        for row in leaders:
            row["subgroup"] = True
    return rows[:top]


def propose_fixes(
    week: SleeperWeek,
    *,
    tolerance: float = RECONSTRUCTION_TOLERANCE,
    top: int = 6,
    depth: int = 1,
) -> list[dict[str, Any]]:
    """Try concrete price changes and count how many players each one repairs.

    The decisive tool, and the one that should have been written first. Rather
    than inferring what a stat is worth — which correlated columns make
    unreliable — it proposes a specific, plausible price for a specific key and
    measures the only thing that matters: how many of the players we currently
    cannot reproduce would reconstruct correctly if that were true.

    Immune to collinearity by construction. Two correlated keys will both
    appear to help, but only the one that repairs *every* failure repairs every
    failure, and the count says so.

    Candidate prices are the values real fantasy formats actually use, so the
    search cannot return something like "interceptions are worth +2.04".
    """
    residuals: dict[str, float] = {
        name: theirs - ours for name, (ours, theirs) in week.rejected.items()
    }
    standard = ScoringSettings.from_raw(_STANDARD_PPR_RAW, "standard-ppr")
    for name, line in week.lines.items():
        expected = week.ppr.get(name)
        if expected is not None:
            residuals[name] = expected - standard.score(to_espn_stat_line(line)).points
    if not residuals:
        return []

    baseline = sum(1 for r in residuals.values() if abs(r) > tolerance)
    if not baseline:
        return []
    # The residual is scored over the FAILING players only. Averaging across
    # all 462 — of whom 427 already reconstruct perfectly — divides the signal
    # by thirteen and collapses the gap between the true fix and a lucky one
    # from 0.07 to 0.005.
    failing_now = [n for n, r in residuals.items() if abs(r) > tolerance]

    keys = {
        k
        for n in residuals
        if n in week.raw
        for k, v in week.raw[n].items()
        if v and not _is_noise_key(k)
    }

    out: list[dict[str, Any]] = []
    for key in keys:
        current = _ASSUMED_PRICE.get(key, 0.0)
        for price in _CANDIDATE_PRICES:
            if price == current:
                continue
            delta = price - current
            # Every scored category is linear in its own stat under standard
            # PPR, so a price change shifts the residual by a known amount and
            # nothing has to be re-scored.
            left = [
                abs(r - delta * (week.raw.get(n, {}).get(key) or 0.0))
                for n, r in residuals.items()
            ]
            broken = sum(1 for x in left if x > tolerance)
            on_failures = [
                abs(residuals[n] - delta * (week.raw.get(n, {}).get(key) or 0.0))
                for n in failing_now
            ]
            if broken < baseline:
                out.append(
                    {
                        "key": key,
                        "from": current,
                        "to": price,
                        "repairs": baseline - broken,
                        "still_broken": broken,
                        # The discriminator that counting repairs misses. Several
                        # wrong prices can drag every player inside a half-point
                        # tolerance; only the true one drives the error to zero.
                        # On a planted bug the correct fix left 0.0000 and the
                        # three impostors left 0.07 to 0.29.
                        "residual_left": round(
                            sum(on_failures) / len(on_failures), 4
                        ),
                        "where": "crosswalk" if key not in _ASSUMED_PRICE else "yardstick",
                    }
                )

    out.sort(key=lambda r: (-r["repairs"], r["residual_left"], abs(r["to"])))
    # One row per key — the best price for each — so six suggestions are six
    # ideas rather than one idea at six prices.
    seen: set[str] = set()
    best: list[dict[str, Any]] = []
    for row in out:
        if row["key"] in seen:
            continue
        seen.add(row["key"])
        best.append(row)
        if len(best) >= top:
            break

    if depth < 2:
        return best
    return best + _propose_pairs(week, residuals, keys, tolerance, baseline, top)


def _propose_pairs(
    week: SleeperWeek,
    residuals: dict[str, float],
    keys: set[str],
    tolerance: float,
    baseline: int,
    top: int,
) -> list[dict[str, Any]]:
    """Two simultaneous price changes, scored across every failing player.

    A single knob can absorb a whole-subgroup effect, and on real data thirty
    different pairs each reproduced one quarterback's gap exactly. One player is
    one equation with a dozen unknowns; thirty-five players are thirty-five
    equations, and a combination that satisfies all of them is not a
    coincidence.

    Pruned to keys that actually appear in the failures, and scored on the
    failing players first — a pair that cannot fix them is not worth checking
    against the ones that already work.
    """
    from itertools import combinations

    failing = [n for n, r in residuals.items() if abs(r) > tolerance]
    if not failing:
        return []

    live = sorted(
        k for k in keys
        if sum(1 for n in failing if week.raw.get(n, {}).get(k)) > len(failing) * 0.5
    )
    moves = [
        (k, price, price - _ASSUMED_PRICE.get(k, 0.0))
        for k in live
        for price in _CANDIDATE_PRICES
        if price != _ASSUMED_PRICE.get(k, 0.0)
    ]

    found: list[dict[str, Any]] = []
    for (k1, p1, d1), (k2, p2, d2) in combinations(moves, 2):
        if k1 == k2:
            continue
        broken_fail = sum(
            1
            for n in failing
            if abs(
                residuals[n]
                - d1 * (week.raw.get(n, {}).get(k1) or 0.0)
                - d2 * (week.raw.get(n, {}).get(k2) or 0.0)
            )
            > tolerance
        )
        if broken_fail:
            continue  # must repair every failure before it is worth anything
        left = [
            abs(
                r
                - d1 * (week.raw.get(n, {}).get(k1) or 0.0)
                - d2 * (week.raw.get(n, {}).get(k2) or 0.0)
            )
            for n, r in residuals.items()
        ]
        broken_all = sum(1 for x in left if x > tolerance)
        on_failures = [
            abs(
                residuals[n]
                - d1 * (week.raw.get(n, {}).get(k1) or 0.0)
                - d2 * (week.raw.get(n, {}).get(k2) or 0.0)
            )
            for n in failing
        ]
        if broken_all < baseline:
            found.append(
                {
                    "key": f"{k1} + {k2}",
                    "from": f"{_ASSUMED_PRICE.get(k1, 0.0)}, {_ASSUMED_PRICE.get(k2, 0.0)}",
                    "to": f"{p1}, {p2}",
                    "repairs": baseline - broken_all,
                    "still_broken": broken_all,
                    "residual_left": round(sum(on_failures) / len(on_failures), 4),
                    "where": "pair",
                }
            )
    found.sort(key=lambda r: (r["residual_left"], -r["repairs"]))
    return found[:top]


def explain_player(week: SleeperWeek, name: str | None = None) -> dict[str, Any]:
    """Every term in one player's reconstruction, beside Sleeper's own total.

    The end of the road for inference. When the failures turn out to be an
    entire position — every quarterback, say — the six passing stats are
    perfectly confounded: every QB has all of them, so no statistical method
    can say which is at fault. :func:`contrast_rejected` correctly identifies
    the subgroup and correctly stops there; :func:`propose_fixes` will happily
    absorb a whole-position effect into whichever single knob fits, which is
    how it came to suggest an interception worth plus one and a half points.

    What settles it is not another estimate. It is reading one row of
    arithmetic: here is the stat line, here is what each category contributed
    under our reference ruleset, here is the total, here is Sleeper's, here is
    the difference, and here is every value we did not use.

    ``name`` defaults to the worst offender.
    """
    if not week.rejected and name is None:
        return {"error": "nothing was rejected — no reconstruction to explain"}

    if name is None:
        key = max(week.rejected, key=lambda n: abs(week.rejected[n][1] - week.rejected[n][0]))
    else:
        key = normalize_name(name)
    stats = week.raw.get(key)
    if stats is None:
        return {"error": f"no stat line for {name or key!r}"}

    standard = ScoringSettings.from_raw(_STANDARD_PPR_RAW, "standard-ppr")
    line = {
        col: stats[sleeper_key]
        for sleeper_key, col in SLEEPER_TO_NFLVERSE.items()
        if stats.get(sleeper_key)
    }
    scored = standard.score(to_espn_stat_line(line))
    theirs = week.ppr.get(key)

    used = []
    for sleeper_key, column in sorted(SLEEPER_TO_NFLVERSE.items()):
        value = stats.get(sleeper_key)
        if not value:
            continue
        price = _ASSUMED_PRICE.get(sleeper_key, 0.0)
        used.append(
            {
                "stat": sleeper_key,
                "value": value,
                "price": price,
                "points": round(price * value, 3),
                "column": column,
            }
        )

    ignored = sorted(
        ({"stat": k, "value": v} for k, v in stats.items() if v and not _is_noise_key(k)
         and k not in SLEEPER_TO_NFLVERSE),
        key=lambda r: -abs(r["value"]),
    )

    return {
        "player": key,
        "ours": scored.points,
        "sleeper": theirs,
        "gap": round((theirs - scored.points), 2) if theirs is not None else None,
        "scored": used,
        "components": scored.components,
        "not_used": ignored,
    }
