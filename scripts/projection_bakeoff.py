#!/usr/bin/env python3
"""How good does a projection have to be before it changes a lineup?

    uv run scripts/projection_bakeoff.py                    # free arms only
    uv run scripts/projection_bakeoff.py --espn             # + your ESPN projections
    uv run scripts/projection_bakeoff.py --sleeper          # + Sleeper's projections
    uv run scripts/projection_bakeoff.py --espn --sleeper --season 2025

This exists because "ESPN's projections are bad" is a claim, and claims about
accuracy are cheap until someone scores them. The script scores them.

**What it does.** It rebuilds last season one week at a time, using only what
was knowable before that week kicked off, and asks each candidate predictor to
guess every player's PPR points. Then it grades them three ways:

* **MAE / RMSE** — how far off, on average. Familiar, and almost useless on its
  own: nobody starts a player because his projection had low error.
* **Start/sit accuracy** — given two players at the same position in the same
  week, did the predictor rank them in the order the results did? This is the
  question a lineup actually asks. Reported twice: over all pairs, and over the
  *close calls* — the pairs the predictor itself could not separate by more than
  ``--close`` points. The close-call column is the honest one. The rest are
  decisions you would get right by accident.
* **Points left on the bench** — the currency. Random 15-man rosters are drawn
  from the real player pool, each predictor sets its own optimal lineup, and the
  lineups are scored on what really happened. The gap to the hindsight-perfect
  lineup is what a better projection is competing to close.

**Why the arms are what they are.** The free candidates are all built from
nflverse: trailing actual production, and ``ff_opportunity``'s expected fantasy
points, which reprice a player's real usage against league-average efficiency
and so shed some of the touchdown luck that makes raw production jumpy. The
paid-adjacent candidates are ESPN's own numbers (your leagues' historical
projections, so they cost nothing but a network round trip) and Sleeper's public
projections endpoint. Anything that beats the free arms by less than the
close-call noise band is not worth money.

**Read the close-call column first.** If every arm sits near 55%, the ceiling on
this whole category of work is low, and the right conclusion is to stop shopping
for projections and spend the effort somewhere the leverage is real.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)
OK, BAD, WARN = f"{GREEN}✓{RESET}", f"{RED}✗{RESET}", f"{YELLOW}!{RESET}"

POSITIONS = ("QB", "RB", "WR", "TE")

#: A generic full-PPR lineup. Two of Jai's three leagues are exactly this; the
#: third differs only in how it buckets yardage, which does not move a ranking.
LINEUP_SLOTS = ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX")
FLEX_ELIGIBLE = ("RB", "WR", "TE")

SLEEPER_PROJECTIONS = "https://api.sleeper.com/projections/nfl/{season}/{week}"


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------


@dataclass
class Row:
    """One player-week, with everything knowable before kickoff attached."""

    player_id: str
    name: str
    position: str
    week: int
    actual: float
    # history, strictly from weeks < this one
    szn_actual: float
    last3_actual: float
    szn_exp: float
    last3_exp: float
    games: int
    #: the player's whole-season average, target week included. Look-ahead on
    #: purpose — it is the benchmark arm, not a candidate. See arms().
    full_season: float = 0.0
    #: Vegas' implied points for this player's team, this week. The one input
    #: the house rules claim to rank above ESPN's projections, so it gets tested.
    implied: float | None = None
    # externals, filled in only when their arm is enabled
    espn: float | None = None
    sleeper: float | None = None


def build_panel(season: int, min_games: int) -> list[Row]:
    """Every player-week with at least ``min_games`` of prior history."""
    import nflreadpy as nfl
    import polars as pl

    stats = (
        nfl.load_player_stats(seasons=[season])
        .filter((pl.col("season_type") == "REG") & pl.col("position").is_in(POSITIONS))
        .select(
            "player_id", "player_display_name", "position", "week", "team",
            pl.col("fantasy_points_ppr").fill_null(0.0).alias("actual"),
        )
    )

    opp = nfl.load_ff_opportunity(seasons=[season], stat_type="weekly")
    exp_cols = [c for c in ("pass_fantasy_points_exp", "rec_fantasy_points_exp",
                            "rush_fantasy_points_exp") if c in opp.columns]
    opp = opp.select(
        pl.col("player_id").cast(pl.Utf8),
        pl.col("week").cast(pl.Int32),
        sum(pl.col(c).fill_null(0.0) for c in exp_cols).alias("expected"),
    ).group_by("player_id", "week").agg(pl.col("expected").sum())
    stats = stats.with_columns(
        pl.col("player_id").cast(pl.Utf8), pl.col("week").cast(pl.Int32)
    )

    frame = stats.join(opp, on=["player_id", "week"], how="left").with_columns(
        pl.col("expected").fill_null(pl.col("actual"))
    ).sort("player_id", "week")

    implied = _implied_totals(season)

    # Per player, walk forward. Everything below is a strict lookback: week W's
    # features only ever see weeks < W. Getting this wrong is the single easiest
    # way to produce a backtest that looks brilliant and predicts nothing.
    rows: list[Row] = []
    for (pid,), group in frame.group_by("player_id", maintain_order=True):
        weeks = group["week"].to_list()
        actuals = group["actual"].to_list()
        expects = group["expected"].to_list()
        teams = group["team"].to_list()
        name = group["player_display_name"][0]
        position = group["position"][0]
        whole = sum(actuals) / len(actuals)

        for i, week in enumerate(weeks):
            if i < min_games:
                continue
            hist_a, hist_e = actuals[:i], expects[:i]
            rows.append(
                Row(
                    player_id=pid,
                    name=name,
                    position=position,
                    week=week,
                    actual=actuals[i],
                    szn_actual=sum(hist_a) / len(hist_a),
                    last3_actual=sum(hist_a[-3:]) / len(hist_a[-3:]),
                    szn_exp=sum(hist_e) / len(hist_e),
                    last3_exp=sum(hist_e[-3:]) / len(hist_e[-3:]),
                    games=i,
                    full_season=whole,
                    implied=implied.get((teams[i], week)),
                )
            )
    return rows


def _implied_totals(season: int) -> dict[tuple[str, int], float]:
    """(team, week) -> Vegas' implied points for that team."""
    import nflreadpy as nfl
    import polars as pl

    sched = nfl.load_schedules().filter(
        (pl.col("season") == season)
        & pl.col("spread_line").is_not_null()
        & pl.col("total_line").is_not_null()
    )
    out: dict[tuple[str, int], float] = {}
    for game in sched.iter_rows(named=True):
        total, spread, week = game["total_line"], game["spread_line"], int(game["week"])
        out[(game["home_team"], week)] = total / 2 + spread / 2
        out[(game["away_team"], week)] = total / 2 - spread / 2
    return out


# ---------------------------------------------------------------------------
# External arms
# ---------------------------------------------------------------------------


def attach_sleeper(rows: list[Row], season: int) -> tuple[int, str]:
    """Sleeper's public projections, joined on normalized name.

    Undocumented but stable and unauthenticated. Blocked from some datacenter
    networks — if this returns a failure string, run it from your laptop.
    """
    import httpx

    from ff_assist.usage import normalize_name

    by_week: dict[int, dict[str, float]] = {}
    weeks = sorted({r.week for r in rows})
    for week in weeks:
        try:
            payload = httpx.get(
                SLEEPER_PROJECTIONS.format(season=season, week=week),
                params={"season_type": "regular", "position[]": list(POSITIONS),
                        "order_by": "pts_ppr"},
                timeout=20,
            ).json()
        except Exception as exc:  # noqa: BLE001
            return 0, f"{type(exc).__name__} — try from a laptop, not a datacenter"
        table: dict[str, float] = {}
        for entry in payload or []:
            player = entry.get("player") or {}
            name = player.get("full_name") or " ".join(
                x for x in (player.get("first_name"), player.get("last_name")) if x
            )
            points = (entry.get("stats") or {}).get("pts_ppr")
            if name and points is not None:
                table[normalize_name(name)] = float(points)
        by_week[week] = table

    hits = 0
    for row in rows:
        value = by_week.get(row.week, {}).get(normalize_name(row.name))
        if value is not None:
            row.sleeper = value
            hits += 1
    return hits, f"{hits:,}/{len(rows):,} player-weeks matched"


def attach_espn(
    rows: list[Row], season: int, league_key: str | None
) -> tuple[int, str]:
    """ESPN's own weekly projections, read out of your leagues' box scores.

    A caveat that matters: ESPN stores the *final* projection for a week, which
    may already reflect Sunday-morning news you would not have had on Saturday.
    That biases this arm upward. If ESPN wins here it has been given a head
    start; if it loses here, it really loses.
    """
    from ff_assist.config import load_settings
    from ff_assist.espn_client import get_league
    from ff_assist.scoring import ScoringSettings

    settings = load_settings()
    leagues = [c for c in settings.leagues if not league_key or c.key == league_key]
    if not leagues:
        return 0, "no matching league configured"

    index: dict[tuple[str, int], float] = {}
    from ff_assist.usage import normalize_name

    # A league you did not play in a given season 404s, and that is a normal
    # fact about history rather than a failure. Abandoning the whole arm on the
    # first one silently cost the 2024 comparison every league that *did* exist.
    used: list[str] = []
    skipped: list[str] = []

    for cfg in leagues:
        try:
            league = get_league(cfg, settings=settings, year=season)
            raw = league.espn_request.get_league().get("settings", {})
            scoring = ScoringSettings.from_raw(raw, cfg.key)
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{cfg.key} ({type(exc).__name__})")
            continue
        used.append(cfg.key)

        for week in sorted({r.week for r in rows}):
            try:
                boxes = league.box_scores(week=week)
            except Exception:  # noqa: BLE001
                continue
            for box in boxes:
                for player in list(box.home_lineup) + list(box.away_lineup):
                    breakdown = getattr(player, "projected_breakdown", None)
                    if breakdown:
                        points = scoring.score(
                            breakdown, getattr(player, "position", None)
                        ).points
                    else:
                        points = float(getattr(player, "projected_points", 0.0) or 0.0)
                    if points:
                        # First league to see a player wins; all three are full
                        # PPR, so the number is the same either way.
                        index.setdefault((normalize_name(player.name), week), points)

    if not used:
        return 0, f"no league has {season} history: " + ", ".join(skipped)

    hits = 0
    for row in rows:
        value = index.get((normalize_name(row.name), row.week))
        if value is not None:
            row.espn = value
            hits += 1
    note = f"{hits:,}/{len(rows):,} player-weeks matched from {', '.join(used)}"
    if skipped:
        note += f" · no {season} history for {', '.join(skipped)}"
    return hits, note


# ---------------------------------------------------------------------------
# The arms
# ---------------------------------------------------------------------------

Predictor = Callable[[Row], float | None]


def _blend(r: Row) -> float:
    """The best free arm, and the baseline every paid option has to beat."""
    return 0.5 * r.szn_exp + 0.5 * r.szn_actual


def arms(rows: list[Row], use_espn: bool, use_sleeper: bool) -> dict[str, Predictor]:
    priced = [r.implied for r in rows if r.implied is not None]
    par = sum(priced) / len(priced) if priced else 22.5

    def implied_scaled(strength: float) -> Predictor:
        """Scale a projection by how many points Vegas expects the team to score.

        No parameter is fitted — full strength is strict proportionality, half
        strength is the obvious hedge. Fitting the exponent on the same season
        being scored would manufacture an edge that does not exist in September.
        """

        def predict(r: Row) -> float:
            base = _blend(r)
            if r.implied is None:
                return base
            return base * (1.0 + strength * (r.implied / par - 1.0))

        return predict

    out: dict[str, Predictor] = {
        "season avg (actual)": lambda r: r.szn_actual,
        "last 3 (actual)": lambda r: r.last3_actual,
        "season avg (expected)": lambda r: r.szn_exp,
        "last 3 (expected)": lambda r: r.last3_exp,
        "blend: exp + last3": lambda r: 0.5 * r.szn_exp + 0.5 * r.last3_actual,
        "blend: exp + season": _blend,
        "blend x implied 100%": implied_scaled(1.0),
        "blend x implied 50%": implied_scaled(0.5),
    }
    if use_espn:
        out["ESPN"] = lambda r: r.espn
    if use_sleeper:
        out["Sleeper"] = lambda r: r.sleeper

    if use_espn or use_sleeper:
        # The one finding that replicates across every published study: the mean
        # of several independent projections beats any single one of them. If
        # that does not hold here, be suspicious of the panel, not the finding.
        def ensemble(r: Row) -> float | None:
            parts = [_blend(r)]
            if use_espn and r.espn is not None:
                parts.append(r.espn)
            if use_sleeper and r.sleeper is not None:
                parts.append(r.sleeper)
            return sum(parts) / len(parts) if len(parts) > 1 else None

        out["ENSEMBLE (all 3)"] = ensemble

    if use_espn and use_sleeper:
        # The three-way mean drags in the free blend, which the first fair run
        # showed to be the weakest of the three. Averaging is only free when the
        # inputs are comparable; a weak member pulls the mean toward itself. So
        # the two-way mean of the arms that actually earned their place is a
        # distinct candidate, not a rounding difference.
        def pair_mean(r: Row) -> float | None:
            if r.espn is None or r.sleeper is None:
                return None
            return (r.espn + r.sleeper) / 2.0

        out["ESPN + Sleeper"] = pair_mean

    # Not a candidate, and NOT an upper bound — a distinction the first draft of
    # this script got wrong. It knows each player's average over the whole
    # season, target week included, so it is what you would score knowing every
    # player's true *level* and nothing else. A real projection can beat it,
    # because it also knows things the season average cannot contain: the
    # opponent, the weather, who else is hurt this week. Arms that exceed it are
    # extracting genuine week-specific signal; arms below it have not yet
    # matched a static estimate of player quality. Read it as par, not a wall.
    out["[benchmark] true mean"] = lambda r: r.full_season
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def error_metrics(rows: list[Row], predict: Predictor) -> tuple[float, float, int]:
    errors = [
        abs(p - r.actual)
        for r in rows
        if (p := predict(r)) is not None
    ]
    squares = [
        (p - r.actual) ** 2
        for r in rows
        if (p := predict(r)) is not None
    ]
    if not errors:
        return math.nan, math.nan, 0
    return sum(errors) / len(errors), math.sqrt(sum(squares) / len(squares)), len(errors)


def pair_accuracy(
    rows: list[Row], predict: Predictor, close: float
) -> tuple[float, int, float, int]:
    """Did the predictor rank two same-position players the right way round?

    Ties in the actual result are dropped rather than counted as half — a pair
    that finished level was never a decision.
    """
    buckets: dict[tuple[int, str], list[Row]] = {}
    for row in rows:
        if predict(row) is not None:
            buckets.setdefault((row.week, row.position), []).append(row)

    right = total = close_right = close_total = 0
    for group in buckets.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a.actual == b.actual:
                    continue
                pa, pb = predict(a), predict(b)
                if pa == pb:
                    continue
                correct = (pa > pb) == (a.actual > b.actual)
                total += 1
                right += correct
                if abs(pa - pb) <= close:
                    close_total += 1
                    close_right += correct
    return (
        right / total if total else math.nan,
        total,
        close_right / close_total if close_total else math.nan,
        close_total,
    )


def bench_points(
    rows: list[Row], predict: Predictor, *, rosters: int, seed: int, pool_top: int = 0
) -> tuple[float, float, float]:
    """Points a predictor leaves on the bench, per lineup, versus hindsight.

    Random 15-man rosters drawn from the same week's player pool. The absolute
    number is sensitive to how those rosters are drawn; the *differences between
    arms* are not, which is the only thing being compared here.

    ``pool_top`` restricts the draw to the N best players by prior-weeks scoring,
    which is a closer stand-in for a drafted roster than a uniform sample of
    everyone who touched the ball. It uses only weeks < W, so it introduces no
    look-ahead. Run it both ways: if a conclusion flips between them, it was
    an artefact of the sampling, not a fact about the projections.
    """
    by_week: dict[int, list[Row]] = {}
    for row in rows:
        if predict(row) is not None:
            by_week.setdefault(row.week, []).append(row)

    rng = random.Random(seed)
    gaps: list[float] = []
    scores: list[float] = []
    for _week, pool in sorted(by_week.items()):
        if pool_top:
            pool = sorted(pool, key=lambda r: -r.szn_actual)[:pool_top]
        if len(pool) < 40:
            continue
        for _ in range(rosters):
            roster = rng.sample(pool, 15)
            chosen = _best_lineup(roster, predict)
            perfect = _best_lineup(roster, lambda r: r.actual)
            got = sum(r.actual for r in chosen)
            scores.append(got)
            gaps.append(sum(r.actual for r in perfect) - got)
    if not gaps:
        return math.nan, math.nan, math.nan
    spread = (
        math.sqrt(sum((x - sum(scores) / len(scores)) ** 2 for x in scores) / len(scores))
        if len(scores) > 1
        else 0.0
    )
    return sum(scores) / len(scores), sum(gaps) / len(gaps), spread


#: Below this many scoreless player-weeks the ratio is noise with an accusation
#: attached. The first version used 30, which the common subset happened to
#: supply exactly — and 30 answered nothing.
MIN_SCORELESS = 150


def _dead_live_ratio(
    rows: list[Row], predict: Predictor
) -> tuple[float, float, float, int] | None:
    dead = [p for r in rows if r.actual == 0.0 and (p := predict(r)) is not None]
    live = [p for r in rows if r.actual > 0.0 and (p := predict(r)) is not None]
    if not dead or not live:
        return None
    mean_dead, mean_live = sum(dead) / len(dead), sum(live) / len(live)
    return mean_dead, mean_live, mean_dead / mean_live, len(dead)


def lookahead_audit(
    rows: list[Row],
    candidates: dict[str, Predictor],
    externals: tuple[str, ...],
    min_scoreless: int = MIN_SCORELESS,
) -> list[tuple[str, float, float, float, int]]:
    """Does an arm already know who was inactive?

    Both external arms are fetched *today*, for seasons that finished months
    ago, from endpoints that may restate. If a provider revises after the fact —
    or simply serves the last value it held, which for a player ruled out on a
    Sunday morning is near zero — then that arm is graded on information no
    forecaster had, and every number it posts is inflated.

    The tell is scoreless weeks. A player with zero actual points either sat or
    played and did nothing; a forecast made in advance cannot tell those apart,
    so an honest arm projects him about the same as in the weeks he scored. An
    arm with hindsight projects him near zero.

    **The ratio is not population-invariant**, which is what broke the first
    attempt. On the full panel a scoreless week is usually a deep-bench body
    projected for 2; among rostered starters it is an injury, and he was
    projected for 10. Same arms, ratios of 0.26 and 0.79. So each external arm
    is audited on *its own coverage*, against a free-arm baseline recomputed on
    exactly those rows — the comparison is per-row identical, and the sample is
    as large as that arm allows rather than as small as the scarcest arm allows.

    Returns (label, mean on scoreless weeks, mean on scoring weeks, ratio,
    n scoreless), with the matched baseline emitted as a synthetic row named
    ``baseline for <arm>`` immediately before each external.
    """
    free = [
        label for label in candidates
        if not label.startswith(("[", "ESPN", "Sleeper", "ENSEMBLE"))
    ]
    out: list[tuple[str, float, float, float, int]] = []

    for label in externals:
        predict = candidates.get(label)
        if predict is None:
            continue
        covered = [r for r in rows if predict(r) is not None]
        stats = _dead_live_ratio(covered, predict)
        if stats is None or stats[3] < min_scoreless:
            n = stats[3] if stats else 0
            out.append((f"{label}  (only {n} scoreless — skipped)", 0.0, 0.0, 0.0, n))
            continue

        # The baseline, on the very same rows.
        ratios = [
            s[2] for f in free if (s := _dead_live_ratio(covered, candidates[f]))
        ]
        if ratios:
            out.append(
                (f"baseline for {label}", 0.0, 0.0, sum(ratios) / len(ratios), stats[3])
            )
        out.append((label, *stats))
    return out


def common_subset(rows: list[Row], candidates: dict[str, Predictor]) -> list[Row]:
    """The player-weeks every arm has an opinion about.

    Without this, each arm is graded on whatever it happens to cover, and the
    comparison is meaningless in a way that is very hard to see. ESPN only knows
    about players somebody rostered — a population that is both smaller and
    *better*, and better players have larger absolute errors and are harder to
    rank against each other. Scored that way ESPN looked two points of MAE worse
    than a trailing average; scored on the shared subset it wins.
    """
    return [r for r in rows if all(f(r) is not None for f in candidates.values())]


def wins_per_point(score_sd: float, games: int = 14) -> float:
    """Extra wins a season, per point per week of lineup improvement.

    Head-to-head is a step function: points only matter when they cross the
    margin. Two independent teams with the same score deviation give a margin
    deviation of ``sd * sqrt(2)``, and near a margin of zero the density of that
    normal is what converts points into flipped games. It is a first-order
    approximation and it is close enough — the point of the number is that it is
    *small*, and no refinement makes it big.
    """
    margin_sd = score_sd * math.sqrt(2.0)
    return games / (margin_sd * math.sqrt(2.0 * math.pi))


def _best_lineup(roster: list[Row], value: Predictor) -> list[Row]:
    """Greedy is exact here: slots are strictly nested (FLEX accepts anything a
    RB/WR/TE slot does), so filling the tightest slots first cannot strand a
    better player. The production optimizer uses Hungarian because real leagues
    have overlapping slots where greedy genuinely goes wrong."""
    remaining = sorted(roster, key=lambda r: -(value(r) or 0.0))
    used: set[str] = set()
    picked: list[Row] = []
    for slot in LINEUP_SLOTS:
        eligible = FLEX_ELIGIBLE if slot == "FLEX" else (slot,)
        for row in remaining:
            if row.player_id not in used and row.position in eligible:
                used.add(row.player_id)
                picked.append(row)
                break
    return picked


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", type=int, default=2025)
    ap.add_argument("--min-games", type=int, default=3,
                    help="games of history required before a player-week counts")
    ap.add_argument("--close", type=float, default=3.0,
                    help="a 'close call' is a pair the predictor separates by <= this")
    ap.add_argument("--rosters", type=int, default=200,
                    help="random rosters simulated per week for the bench metric")
    ap.add_argument("--pool-top", type=int, default=0, metavar="N",
                    help="draw those rosters from the N best players only "
                         "(0 = everyone; ~150 approximates a drafted roster)")
    ap.add_argument("--seed", type=int, default=20260801)
    ap.add_argument("--espn", action="store_true", help="add your ESPN projections")
    ap.add_argument("--league", help="limit the ESPN arm to one league key")
    ap.add_argument("--sleeper", action="store_true", help="add Sleeper's projections")
    ap.add_argument("--no-common", action="store_true",
                    help="score each arm on its own coverage instead of the shared "
                         "subset. Almost always wrong — see the note it prints.")
    ap.add_argument("--by-position", action="store_true",
                    help="also break the top arms down by position")
    ap.add_argument("--audit", action="store_true",
                    help="check whether any arm already knows who was inactive")
    args = ap.parse_args()

    print(f"{BOLD}Projection bake-off — {args.season}{RESET}")
    print(f"{DIM}strict lookback: week W is predicted from weeks < W only{RESET}\n")

    rows = build_panel(args.season, args.min_games)
    print(f"  panel: {len(rows):,} player-weeks "
          f"({len({r.player_id for r in rows}):,} players, "
          f"weeks {min(r.week for r in rows)}-{max(r.week for r in rows)}, "
          f"min {args.min_games} games of history)")

    if args.sleeper:
        print(f"  {DIM}fetching Sleeper projections…{RESET}", end="\r")
        hits, note = attach_sleeper(rows, args.season)
        ok = hits > 0
        print(f"  {OK if ok else WARN} Sleeper: {note}" + " " * 20)
        if not ok:
            args.sleeper = False
    if args.espn:
        print(f"  {DIM}reading ESPN box scores…{RESET}", end="\r")
        hits, note = attach_espn(rows, args.season, args.league)
        ok = hits > 0
        print(f"  {OK if ok else WARN} ESPN: {note}" + " " * 20)
        if not ok:
            args.espn = False

    priced = sum(r.implied is not None for r in rows)
    print(f"  {DIM}implied team totals attached to {priced:,}/{len(rows):,} rows{RESET}")

    candidates = arms(rows, args.espn, args.sleeper)

    # Every arm must sit the same exam. An arm that only covers rostered players
    # is being graded on a harder population — better players score more, so the
    # absolute error is mechanically larger, and two good players are genuinely
    # harder to rank than a good one against a scrub. Scoring each arm on its own
    # coverage made ESPN look far worse than it is; on the shared subset the
    # ordering reverses. Restricting to the intersection is not a nicety.
    scored = rows
    if (args.espn or args.sleeper) and not args.no_common:
        scored = common_subset(rows, candidates)
        dropped = len(rows) - len(scored)
        if not scored:
            print(f"\n{BAD} no player-week is covered by every arm — nothing to compare")
            return 1
        print(
            f"  {OK} common subset: {len(scored):,} player-weeks "
            f"({dropped:,} dropped, covered by some arms but not all)"
        )
        thin = len(scored) / len(rows)
        if thin < 0.5:
            print(
                f"    {YELLOW}note{RESET} {DIM}that is only {thin:.0%} of the panel, and it is\n"
                f"    the better-player end of it — rostered players are rostered for a\n"
                f"    reason. Absolute numbers here are worse than the full-panel run and\n"
                f"    are not comparable to it. The ranking between arms is what reads.{RESET}"
            )
    elif args.no_common and (args.espn or args.sleeper):
        print(
            f"  {WARN} {YELLOW}--no-common{RESET}{DIM}: arms are scored on different populations\n"
            f"    and the numbers below cannot be compared to each other.{RESET}"
        )

    header = (
        f"\n  {'predictor':<22}{'MAE':>7}{'RMSE':>7}"
        f"{'start/sit':>11}{'close calls':>13}{'bench loss':>12}"
    )
    print(header)
    print(f"  {DIM}{'-' * (len(header) - 3)}{RESET}")

    table: list[tuple[str, float, float, float, float, int, float]] = []
    score_sd = 0.0
    for label, predict in candidates.items():
        mae, rmse, n = error_metrics(scored, predict)
        if not n:
            print(f"  {label:<22}{DIM}no coverage{RESET}")
            continue
        acc, _pairs, close_acc, close_pairs = pair_accuracy(scored, predict, args.close)
        _, gap, spread = bench_points(scored, predict, rosters=args.rosters,
                                      seed=args.seed, pool_top=args.pool_top)
        score_sd = max(score_sd, spread)
        table.append((label, mae, rmse, acc, close_acc, close_pairs, gap))
        tint = DIM if label.startswith("[benchmark]") else ""
        print(
            f"  {tint}{label:<22}{mae:>7.2f}{rmse:>7.2f}"
            f"{acc:>10.1%}{close_acc:>12.1%}{gap:>11.2f}{RESET}"
        )

    if not table:
        print(f"\n{BAD} no arm produced a prediction — is nflverse reachable?")
        return 1

    print(
        f"\n  {DIM}close call = the two players are within {args.close:.0f} projected "
        f"points of each other, by that arm's own numbers.\n"
        f"  bench loss = points/lineup below the hindsight-perfect lineup, over "
        f"{args.rosters} random rosters per week"
        + (f",\n  drawn from the {args.pool_top} best players by prior scoring."
           if args.pool_top else " drawn from the whole pool.") + RESET
    )

    real = [t for t in table if not t[0].startswith("[benchmark]")]
    ref = next((t for t in table if t[0].startswith("[benchmark]")), None)

    if args.by_position:
        # A single aggregate hides the thing the published research is loudest
        # about: sources are uneven *by position*. Start/sit is always a
        # within-position decision, so this is the cut that maps onto a lineup.
        # The benchmark row is included because "51.7% at QB" is unreadable
        # without knowing what a static estimate of player quality scores there.
        ranked = [t[0] for t in sorted(real, key=lambda t: t[6])[:5]]
        if ref:
            ranked.append(ref[0])
        print(f"\n{BOLD}Close calls by position{RESET} {DIM}(best five arms by bench loss){RESET}")
        print(f"  {'predictor':<22}" + "".join(f"{p:>9}" for p in POSITIONS))
        print(f"  {DIM}{'-' * (22 + 9 * len(POSITIONS))}{RESET}")
        counts: dict[str, int] = {}
        for label in ranked:
            cells = []
            for position in POSITIONS:
                subset = [r for r in scored if r.position == position]
                _acc, _n, close_acc, close_n = pair_accuracy(
                    subset, candidates[label], args.close
                )
                counts[position] = close_n
                cells.append("     n/a" if close_n < 200 else f"{close_acc:>8.1%}")
            tint = DIM if label.startswith("[benchmark]") else ""
            print(f"  {tint}{label:<22}" + " ".join(cells) + RESET)
        # Per-position samples are a fraction of the whole, so the noise band is
        # correspondingly wider. Printing it stops a 2-point gap being read as a
        # finding when it is a coin landing the same way twice.
        bands = " ".join(
            f"{position}±{1.96 * math.sqrt(0.25 / max(counts.get(position, 1), 1)) * 100:.1f}pp"
            for position in POSITIONS
        )
        print(f"  {DIM}95% noise band per position: {bands}")
        print(f"  n/a = fewer than 200 close pairs at that position to judge on.{RESET}")

    if args.audit:
        externals = tuple(
            label for label in ("ESPN", "Sleeper", "ESPN + Sleeper", "ENSEMBLE (all 3)")
            if label in candidates
        )
        # Deliberately audited on the FULL panel, not the common subset: each
        # arm brings its own coverage and its own matched baseline, so there is
        # no need to shrink everyone to the scarcest arm — and shrinking is what
        # left the first attempt with 30 scoreless weeks and no answer.
        audit = lookahead_audit(rows, candidates, externals)
        if audit:
            print(f"\n{BOLD}Look-ahead audit{RESET} "
                  f"{DIM}(projection on scoreless weeks vs weeks they scored){RESET}")
            print(f"  {'predictor':<28}{'scoreless':>11}{'scoring':>10}{'ratio':>9}"
                  f"{'n':>8}")
            print(f"  {DIM}{'-' * 66}{RESET}")
            par = None
            for label, dead, live, ratio, n in audit:
                if label.startswith("baseline for"):
                    par = ratio
                    print(f"  {DIM}{label:<28}{'':>11}{'':>10}{ratio:>9.2f}{n:>8,}{RESET}")
                    continue
                if "skipped" in label:
                    print(f"  {WARN} {DIM}{label}{RESET}")
                    continue
                flag = ""
                if par is not None and ratio < par * 0.93:
                    flag = f"  {YELLOW}<- sees something the free arms do not{RESET}"
                print(f"  {label:<28}{dead:>11.2f}{live:>10.2f}{ratio:>9.2f}{n:>8,}{flag}")
            print(
                f"  {DIM}Each arm is scored on its own coverage against a free-arm baseline\n"
                f"  recomputed on exactly those rows. The ratio is NOT comparable across\n"
                f"  populations — only to the baseline directly above it.{RESET}"
            )
            # An earlier version of this flag said "knows who sat", which was
            # over-claiming: three different things produce an identical
            # signature and only one of them invalidates the arm.
            print(
                f"\n  {BOLD}A low ratio has three explanations and this test cannot"
                f" separate them:{RESET}\n"
                f"  {DIM}1. the archive restates — the endpoint is serving a value revised\n"
                f"     after kickoff, which would make every number this arm posts fake;\n"
                f"  2. the live projection legitimately knew Friday's injury report, which\n"
                f"     is not cheating at all — it is exactly what you want on a Sunday;\n"
                f"  3. it is simply better at spotting busts, which is skill.\n"
                f"  Two of the three are edges worth having. Only a forward test tells them\n"
                f"  apart: snapshot a week's projections before kickoff, then re-fetch the\n"
                f"  same week later and diff. See scripts/snapshot_projections.py.{RESET}"
            )

    best_mae = min(real, key=lambda t: t[1])
    best_close = max(real, key=lambda t: t[4])
    best_bench = min(real, key=lambda t: t[6])
    spread = max(t[1] for t in real) - min(t[1] for t in real)

    print(f"\n{BOLD}Verdict{RESET}")
    print(f"  lowest error         {best_mae[0]} ({best_mae[1]:.2f} MAE)")
    print(f"  best close calls     {best_close[0]} ({best_close[4]:.1%})")
    print(f"  fewest bench points  {best_bench[0]} ({best_bench[6]:.2f}/lineup)")
    print(f"  spread across arms   {spread:.2f} MAE, "
          f"{max(t[6] for t in real) - min(t[6] for t in real):.2f} bench points")

    # The bar that matters. Coin-flip on close calls means the decisions you
    # actually agonise over are not decidable from projections at all.
    band = 1.96 * math.sqrt(0.25 / max(best_close[5], 1))
    print(
        f"\n  {DIM}A coin flip on close calls is 50%. With {best_close[5]:,} such pairs the\n"
        f"  95% band is ±{band:.1%}, so anything under {50 + band * 100:.1f}% is not\n"
        f"  distinguishable from guessing.{RESET}"
    )

    if not ref:
        return 0

    # Everything above is in points. This converts points into the only unit a
    # season is scored in, and it is where the argument for spending money on
    # projections usually dies.
    headroom = best_bench[6] - ref[6]
    per_point = wins_per_point(score_sd)
    print(f"\n{BOLD}What is actually on the table{RESET}")
    print(
        f"  Knowing every player's true season-long level — and nothing else about\n"
        f"  the week — scores {ref[1]:.2f} MAE, {ref[4]:.1%} on close calls, and leaves\n"
        f"  {ref[6]:.1f} points on the bench. The best arm here is "
        f"{BOLD}{headroom:+.1f} points{RESET} from it."
    )
    if headroom <= 0:
        print(
            f"  {DIM}Ahead of the benchmark, which is allowed: a real projection also\n"
            f"  knows the opponent, the weather and who else is hurt, none of which a\n"
            f"  season average contains. It is par, not a wall.{RESET}"
        )
    print(
        f"\n  Weekly lineup scores in this simulation have a spread of {score_sd:.0f} points,\n"
        f"  so head to head, one point a week is worth {per_point:.2f} wins a season:"
    )
    for gain in [g for g in (1.0, 2.0, headroom) if g > 0]:
        tail = f"{DIM}  ← to the benchmark{RESET}" if abs(gain - headroom) < 1e-9 else ""
        print(f"    +{gain:>4.1f} pts/week   {gain * per_point:>5.2f} extra wins{tail}")
    print(
        f"\n  {DIM}A paid feed has to beat the best arm above, not the free blend, and\n"
        f"  would capture only a fraction of what is left. Price the subscription\n"
        f"  against the wins column, not against the MAE column.{RESET}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
