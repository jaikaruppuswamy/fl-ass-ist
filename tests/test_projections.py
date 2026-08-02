"""The second opinion: crosswalk, consensus, and the fallback that must hold.

The load-bearing test here is `test_crosswalk_reproduces_sleepers_own_total`.
Sleeper publishes both a projected stat line and its own standard-PPR points
for the same player, which makes our translation of that stat line falsifiable
against a number we did not compute. Every other test in this file guards a
specific way the integration could go quietly wrong — a bucket league scoring
zero yardage, a missing source reading as a projection of nought, an unmapped
stat key silently deflating a projection.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist.projections import (  # noqa: E402
    DISAGREEMENT_POINTS,
    SLEEPER_TO_NFLVERSE,
    consensus,
    disagreement,
    fetch_week,
    infer_scoring,
    score_line,
    verify_crosswalk,
)
from ff_assist.scoring import ScoringSettings  # noqa: E402


def rules(items: dict[int, float], key: str = "test") -> ScoringSettings:
    return ScoringSettings.from_raw(
        {"scoringSettings": {"scoringItems": [
            {"statId": sid, "points": pts} for sid, pts in items.items()
        ]}},
        key,
    )


#: inai / naperville: ordinary full PPR, per-yard.
PPR = rules({3: 0.04, 4: 4.0, 20: -2.0, 24: 0.1, 25: 6.0, 53: 1.0, 42: 0.1, 43: 6.0, 72: -2.0})

#: gladiator: yardage in buckets and NO per-yard rule at all. 7 = every 20
#: passing yards, 28 = every 10 rushing, 48 = every 10 receiving.
BUCKETS = rules({7: 1.0, 4: 4.0, 20: -2.0, 28: 1.0, 25: 6.0, 53: 1.0, 48: 1.0, 43: 6.0}, "glad")


def payload(name: str = "Josh Allen", **stats):
    base = {"pass_yd": 300, "pass_td": 2, "pass_int": 1, "rush_yd": 40, "rush_td": 1,
            "rec": 6, "rec_yd": 80, "rec_td": 1, "pts_ppr": 48.0}
    base.update(stats)
    return [{"player": {"full_name": name}, "stats": base}]


def week_from(entries):
    return fetch_week(2026, 1, fetch=lambda url, params: entries)


# ---------------------------------------------------------------------------
# The crosswalk, proved against a number we did not compute
# ---------------------------------------------------------------------------


def test_crosswalk_reproduces_sleepers_own_total():
    """Sleeper gives us the components AND its own PPR total. Scoring the
    components under standard PPR must land on that total; if it does not, a
    category is being dropped or double-counted and every projection built from
    it is wrong by an amount nobody can see."""
    week = week_from(payload())
    report = verify_crosswalk(week)
    assert report["checked"] == 1
    assert report["failed"] == 0, report
    assert week.line_for("Josh Allen") is not None


def test_a_player_we_cannot_reproduce_is_dropped_rather_than_used(monkeypatch):
    """The guard that was missing from the first version. `verify_crosswalk`
    existed and check_external ran it, but nothing on the path that sets a
    lineup consulted it — so a broken map produced a quietly understated
    projection. Now the player is dropped at fetch time and falls back to ESPN.
    """
    broken = dict(SLEEPER_TO_NFLVERSE)
    broken.pop("rec_yd")
    monkeypatch.setattr("ff_assist.projections.SLEEPER_TO_NFLVERSE", broken)

    week = week_from(payload())
    assert week.line_for("Josh Allen") is None, "an unreproducible line must not be used"
    assert "joshallen" in week.rejected
    ours, theirs = week.rejected["joshallen"]
    assert ours == pytest.approx(40.0)      # 48 - the missing 8 receiving yards
    assert theirs == pytest.approx(48.0)


def test_the_report_still_names_the_failure_after_the_drop(monkeypatch):
    """Dropping the bad rows must not make the verifier look clean — that is
    how a check stops being able to fail."""
    broken = dict(SLEEPER_TO_NFLVERSE)
    broken.pop("rec_yd")
    monkeypatch.setattr("ff_assist.projections.SLEEPER_TO_NFLVERSE", broken)

    report = verify_crosswalk(week_from(payload()))
    assert report["checked"] == 1
    assert report["failed"] == 1
    assert report["worst"]["ours"] == pytest.approx(40.0)
    assert report["worst"]["sleeper"] == pytest.approx(48.0)


def test_a_small_error_on_many_players_is_called_out_as_a_yardstick_problem():
    """Rejections have two causes and only one is Sleeper's fault. A missing
    stat key is our translation; a wrong coefficient in the reference ruleset
    is our yardstick, and rejecting on that discards perfectly good data."""
    entries = []
    for i in range(40):
        st = {"rec": 5, "rec_yd": 60, "pass_int": 1}
        # Sleeper prices the interception at -1; our reference assumes -2.
        st["pts_ppr"] = 5 + 6 - 1
        entries.append({"player": {"full_name": f"Player {i}"}, "stats": st})

    report = verify_crosswalk(week_from(entries))
    assert report["failed"] == 40
    assert "hint" in report
    assert "_STANDARD_PPR_RAW" in report["hint"]


def test_every_mapped_column_is_one_dvp_understands():
    """The map routes through nflverse's vocabulary so `to_espn_stat_line` can
    derive the bucket categories. A typo here would silently drop a stat."""
    from ff_assist.dvp import _BUCKETS, _FUMBLE_COLUMNS, NFLVERSE_TO_ESPN_STAT

    known = set(NFLVERSE_TO_ESPN_STAT) | set(_BUCKETS) | set(_FUMBLE_COLUMNS)
    unknown = set(SLEEPER_TO_NFLVERSE.values()) - known
    assert not unknown, f"columns dvp.py will ignore: {sorted(unknown)}"


def test_unrecognised_stat_keys_are_reported_not_swallowed():
    """An unmapped key contributes zero, which deflates a projection with no
    visible symptom. It has to surface."""
    week = week_from(payload(brand_new_stat=7))
    assert "brand_new_stat" in week.unmapped


def test_an_unmapped_key_worth_nothing_is_not_worth_a_warning():
    """A key present at zero contributes nothing whether we map it or not.
    Flagging those made 19 harmless keys look like 19 problems and trains the
    eye to skip the warning that matters."""
    week = week_from(payload(def_kr_yd=0, idp_int=0, pass_inc=0))
    assert week.unmapped == set()


def test_known_noise_keys_do_not_raise_a_false_alarm():
    """Sleeper returns rate stats and its own points totals. Flagging those
    would train the eye to ignore the warning that matters."""
    week = week_from(payload(cmp_pct=0.66, pass_rtg=101.2, pts_std=40.0, gp=1))
    assert week.unmapped == set()


# ---------------------------------------------------------------------------
# Scoring in each league's own rules
# ---------------------------------------------------------------------------


def test_a_bucket_league_scores_yardage_rather_than_zero():
    """The reason we score the stat line instead of taking Sleeper's pts_ppr.
    gladiator has no per-yard rule, so a raw yardage total would find no rule
    and contribute nothing — every quarterback silently worth 20 points less."""
    line = week_from(payload()).line_for("Josh Allen")
    assert score_line(line, BUCKETS, "QB") == pytest.approx(51.0)
    #  300/20=15  +  2td*4=8  -  int 2  +  40/10=4  +  6  +  6rec  +  80/10=8  +  6


def test_the_two_scoring_formats_genuinely_differ():
    """If these came out equal the bucket handling would be untested by the
    test above — the whole point is that gladiator is not standard PPR."""
    line = week_from(payload()).line_for("Josh Allen")
    assert score_line(line, PPR, "QB") != score_line(line, BUCKETS, "QB")


def test_a_missing_line_is_none_and_never_zero():
    """Zero means 'projected to score nothing'. None means 'no opinion'. Fusing
    them would bench a player because a name failed to match."""
    assert score_line(None, PPR, "RB") is None
    assert score_line({}, PPR, "RB") is None
    assert score_line({"rushing_yards": 0}, PPR, "RB") == 0.0


def test_names_resolve_across_the_two_sources_spellings():
    week = week_from(payload(name="D.K. Metcalf"))
    assert week.line_for("DK Metcalf") is not None
    assert week.line_for("Amon-Ra St. Brown") is None


# ---------------------------------------------------------------------------
# Failure is absence
# ---------------------------------------------------------------------------


def test_an_unreachable_endpoint_yields_an_empty_week_not_an_exception():
    def boom(url, params):
        raise ConnectionError("403 from a datacenter address")

    week = fetch_week(2026, 1, fetch=boom)
    assert not week
    assert week.lines == {}


def test_a_malformed_payload_does_not_take_the_slate_down():
    for junk in (None, [], [{}], [{"player": {}, "stats": {}}], [{"stats": None}]):
        assert not fetch_week(2026, 1, fetch=lambda url, params, j=junk: j)


def test_verify_reports_the_absence_rather_than_claiming_success():
    report = verify_crosswalk(fetch_week(2026, 1, fetch=lambda u, p: []))
    assert report["checked"] == 0
    assert "error" in report


# ---------------------------------------------------------------------------
# Solving for the scoring rule instead of guessing at it
# ---------------------------------------------------------------------------


def _synthetic(n: int, *, hidden_rate: float, collinear: bool):
    """Players scored by a rule we deliberately do not fully map.

    ``collinear`` is the important switch. Real projections are not independent
    draws — a projected passing line is one latent "how much will he play"
    variable scaled across yards, attempts, completions, incompletions, first
    downs and sacks. Generating those independently makes the design matrix
    orthogonal, which is the one condition under which a least-squares fit
    cannot go wrong, and the first version of these tests did exactly that. It
    passed while the real solver was returning interceptions worth +2 points.
    """
    import random

    rng = random.Random(11)
    entries = []
    for i in range(n):
        if collinear:
            volume = rng.uniform(0.3, 1.0)      # one latent driver
            attempts = round(38 * volume)
            st = {
                "pass_att": attempts,
                "pass_cmp": round(attempts * 0.64),
                "pass_inc": attempts - round(attempts * 0.64),
                "pass_yd": round(attempts * 7.2),
                "pass_fd": round(attempts * 0.55),
                "pass_sack": round(attempts * 0.06),
                "pass_td": round(attempts * 0.055),
                "pass_int": round(attempts * 0.025),
                "pass_cmp_40p": round(attempts * 0.05),
            }
        else:
            st = {
                "pass_yd": rng.randint(150, 380), "pass_td": rng.randint(0, 4),
                "pass_int": rng.randint(0, 2), "rush_yd": rng.randint(0, 60),
                "rec": rng.randint(0, 9), "rec_yd": rng.randint(0, 120),
                "pass_cmp_40p": rng.randint(0, 3),
                "pass_inc": rng.randint(5, 20),
            }
        st["pts_ppr"] = round(
            0.04 * st.get("pass_yd", 0) + 4 * st.get("pass_td", 0)
            - 2.0 * st.get("pass_int", 0) + 0.1 * st.get("rush_yd", 0)
            + 1.0 * st.get("rec", 0) + 0.1 * st.get("rec_yd", 0)
            + hidden_rate * st.get("pass_cmp_40p", 0), 2
        )
        entries.append({"player": {"full_name": f"Player {i}"}, "stats": st})
    return week_from(entries)


def test_inference_names_the_stat_key_we_failed_to_map():
    """Guessing which key is missing takes rounds; solving the residual against
    the unmapped keys reads the answer off directly."""
    result = infer_scoring(_synthetic(300, hidden_rate=0.5, collinear=False))
    assert result["unmapped_but_scored"].get("pass_cmp_40p") == pytest.approx(0.5, abs=0.05)


def test_inference_gives_a_worthless_stat_no_coefficient():
    result = infer_scoring(_synthetic(300, hidden_rate=0.5, collinear=False))
    assert "pass_inc" not in result["unmapped_but_scored"]


def test_inference_makes_no_confident_wrong_claim_on_collinear_columns():
    """The failure the first version shipped, and the property that matters.

    When every passing stat is a multiple of one latent volume, a fit will
    happily attribute a real effect to whichever correlated column it likes and
    print confident nonsense — interceptions worth +2 points, first downs worth
    -2. Here `pass_cmp_40p` is genuinely worth 0.5 and five other columns move
    with it.

    The requirement is not that it always finds the right key — sometimes the
    data cannot say. It is that it never names a *wrong* one. Anything it
    cannot separate must land in `unstable`, not in the findings.
    """
    result = infer_scoring(_synthetic(300, hidden_rate=0.5, collinear=True))
    decoys = {"pass_inc", "pass_fd", "pass_sack"}
    claimed = set(result["unmapped_but_scored"])
    assert not (claimed & decoys), (
        f"named a collinear decoy as a finding: {claimed & decoys}"
    )
    if "pass_cmp_40p" in claimed:
        assert result["unmapped_but_scored"]["pass_cmp_40p"] > 0, "sign must be right"


def test_inference_reports_what_it_could_not_separate():
    """Silence and 'I cannot tell' look identical in a findings list, and this
    document has already been bitten twice by a check that answered when it
    should have abstained. Inseparable columns get named."""
    result = infer_scoring(_synthetic(300, hidden_rate=0.5, collinear=True))
    assert result["unstable"] or result["unmapped_but_scored"], (
        "a real hidden effect must show up somewhere — as a finding or as unstable"
    )


def test_draft_position_never_enters_the_fit():
    """ADP runs into the hundreds and would dominate the regression while
    meaning nothing. It was left in, and it wrecked the first real run."""
    week = _synthetic(300, hidden_rate=0.5, collinear=False)
    for stats in week.raw.values():
        stats["adp_dd_ppr"] = 143.0
        stats["pos_adp_dd_ppr"] = 22.0
    result = infer_scoring(week)
    assert "adp_dd_ppr" not in result["unmapped_but_scored"]
    assert "adp_dd_ppr" not in result["unstable"]


def test_a_category_we_assume_is_unscored_can_still_be_caught_out():
    """`pass_fd` sits in the assumed-unscored list. If Sleeper turns out to pay
    for it, the diagnostic has to be able to say so — otherwise the assumption
    is unfalsifiable because it is the reason we stopped looking."""
    from ff_assist.projections import _ASSUMED_UNSCORED, _is_noise_key

    assert "pass_fd" in _ASSUMED_UNSCORED
    assert not _is_noise_key("pass_fd"), "assumed-unscored must stay in the fit"
    assert _is_noise_key("adp_dd_ppr"), "structural noise must not"


def test_inference_counts_each_player_once():
    """Rejected players stay in `raw`. An earlier version added them again from
    `rejected` and reported 497 rows for a 462-player week."""
    result = infer_scoring(_synthetic(300, hidden_rate=0.5, collinear=False))
    assert result["players"] == 300


def test_inference_declines_on_too_few_players():
    """Twenty players and twenty unknowns fits anything. It has to abstain."""
    assert "error" in infer_scoring(_synthetic(20, hidden_rate=0.5, collinear=False))


# ---------------------------------------------------------------------------
# Consensus
# ---------------------------------------------------------------------------


def test_consensus_is_the_plain_mean_when_both_sources_speak():
    assert consensus(18.0, 22.0) == (20.0, "both")


def test_consensus_falls_back_to_whichever_source_exists():
    assert consensus(18.0, None) == (18.0, "espn")
    assert consensus(None, 22.0) == (22.0, "sleeper")


def test_consensus_of_nothing_is_none_not_zero():
    """A player neither source has an opinion on must not be ranked as a zero —
    that is a benching disguised as arithmetic."""
    assert consensus(None, None) == (None, "none")


def test_consensus_weights_the_two_sources_equally():
    """Equal weighting is the finding, not a shortcut: the twelve-season study
    and the bake-off both had the simple average beating weighted ones."""
    assert consensus(0.0, 30.0)[0] == 15.0
    assert consensus(30.0, 0.0)[0] == 15.0


# ---------------------------------------------------------------------------
# Disagreement
# ---------------------------------------------------------------------------


def test_disagreement_is_silent_when_the_sources_agree():
    assert disagreement(18.0, 19.0) is None
    assert disagreement(18.0, 18.0) is None


def test_disagreement_surfaces_a_real_split_with_its_direction():
    assert disagreement(18.0, 22.0) == 4.0
    assert disagreement(22.0, 18.0) == -4.0


def test_disagreement_needs_both_sources_to_have_an_opinion():
    assert disagreement(18.0, None) is None
    assert disagreement(None, 18.0) is None


def test_the_threshold_is_where_a_flex_decision_starts_to_turn():
    assert disagreement(10.0, 10.0 + DISAGREEMENT_POINTS) is not None
    assert disagreement(10.0, 10.0 + DISAGREEMENT_POINTS - 0.01) is None


# ---------------------------------------------------------------------------
# Diagnosing a mispriced MAPPED key — the case regression gets wrong
# ---------------------------------------------------------------------------


def _mispriced_yardstick(n: int = 400):
    """Sleeper does not penalise fumbles lost; our reference charges -2.

    The confounder that matters: `fum` (total fumbles) is unmapped and moves
    with `fum_lost`, so a regression has nowhere to put the residual except on
    `fum` — and blames a stat that cannot possibly be worth positive points.
    """
    import random

    rng = random.Random(5)
    entries = []
    for i in range(n):
        lost = rng.choice([0] * 11 + [1])
        st = {
            "pass_yd": rng.randint(120, 380), "pass_td": rng.randint(0, 4),
            "pass_int": rng.randint(0, 2), "rush_yd": rng.randint(0, 60),
            "rec": rng.randint(0, 8), "rec_yd": rng.randint(0, 110),
            "fum_lost": lost, "fum": lost * 2 + rng.choice([0, 0, 1]),
        }
        st["pts_ppr"] = round(
            0.04 * st["pass_yd"] + 4 * st["pass_td"] - 2 * st["pass_int"]
            + 0.1 * st["rush_yd"] + st["rec"] + 0.1 * st["rec_yd"], 2
        )
        entries.append({"player": {"full_name": f"Player {i}"}, "stats": st})
    return week_from(entries)


def test_contrast_finds_the_mapped_key_the_regression_blames_elsewhere():
    """`contrast_rejected` estimates nothing — it asks which keys are present
    in the failures and absent from the successes. Collinearity cannot move
    that, so it names `fum_lost` where the fit named `fum`."""
    from ff_assist.projections import contrast_rejected

    rows = contrast_rejected(_mispriced_yardstick())
    assert rows, "there are failures to contrast"
    assert rows[0]["key"] in {"fum_lost", "fum"}
    by_key = {r["key"]: r for r in rows}
    assert by_key["fum_lost"]["in_failures"] == 1.0
    assert by_key["fum_lost"]["in_successes"] == 0.0
    assert by_key["fum_lost"]["mapped"] is True


def test_the_fix_search_repairs_every_failure_and_says_where_the_bug_is():
    """The decisive tool: propose a specific plausible price and count the
    players it repairs. An outcome you can count cannot be misattributed."""
    from ff_assist.projections import propose_fixes

    fixes = propose_fixes(_mispriced_yardstick())
    assert fixes, "a single price change should repair these"
    best = fixes[0]
    assert best["still_broken"] == 0, best
    assert best["key"] in {"fum_lost", "fum"}
    winners = {f["key"]: f for f in fixes if f["still_broken"] == 0}
    assert "fum_lost" in winners
    assert winners["fum_lost"]["to"] == 0.0
    assert winners["fum_lost"]["where"] == "yardstick"


def test_the_fix_search_only_offers_prices_real_formats_use():
    """It cannot return 'interceptions are worth +2.04' because it never tries
    a value no scoring system uses."""
    from ff_assist.projections import _CANDIDATE_PRICES, propose_fixes

    for fix in propose_fixes(_mispriced_yardstick()):
        assert fix["to"] in _CANDIDATE_PRICES


def test_the_fix_search_stays_quiet_when_nothing_is_broken():
    from ff_assist.projections import propose_fixes

    assert propose_fixes(week_from(payload())) == []


def test_contrast_reports_nothing_when_there_is_nothing_to_contrast():
    from ff_assist.projections import contrast_rejected

    assert contrast_rejected(week_from(payload())) == []


# ---------------------------------------------------------------------------
# When the failures are a whole position, no statistic can name the cause
# ---------------------------------------------------------------------------


def _quarterbacks_only_fail(n_qb: int = 35, n_other: int = 427):
    """Sleeper pays 0.05 a passing yard; our yardstick assumes 0.04.

    Only quarterbacks have passing stats, so only quarterbacks fail — and
    inside that group `pass_att`, `pass_cmp`, `pass_yd`, `pass_td`, `pass_int`,
    `pass_fd` and `pass_sack` are all present on every single row. Perfectly
    confounded: no contrast, regression or single-key search can tell them
    apart, and each will confidently pick a different one.
    """
    import random

    rng = random.Random(7)
    entries = []
    for i in range(n_qb + n_other):
        if i < n_qb:
            att = rng.randint(28, 40)
            st = {
                "pass_att": att, "pass_cmp": round(att * 0.64),
                "pass_yd": round(att * 7.3), "pass_td": round(att * 0.06),
                "pass_int": round(att * 0.025), "pass_fd": round(att * 0.55),
                "pass_inc": att - round(att * 0.64), "pass_sack": round(att * 0.06),
                "rush_yd": rng.randint(0, 40),
            }
            st["pts_ppr"] = round(
                0.05 * st["pass_yd"] + 4 * st["pass_td"] - 2 * st["pass_int"]
                + 0.1 * st["rush_yd"], 2
            )
        else:
            st = {"rec": rng.randint(1, 9), "rec_yd": rng.randint(5, 120),
                  "rush_yd": rng.randint(0, 90)}
            st["pts_ppr"] = round(
                st["rec"] + 0.1 * st["rec_yd"] + 0.1 * st["rush_yd"], 2
            )
        entries.append({"player": {"full_name": f"Player {i}"}, "stats": st})
    return week_from(entries)


def test_only_the_affected_position_is_dropped():
    week = _quarterbacks_only_fail()
    assert len(week.rejected) == 35
    assert len(week.lines) == 427


def test_co_occurring_keys_are_flagged_as_a_subgroup_not_as_causes():
    """Six rows all reading 100%/0% are one finding — a position — and
    presenting them as six competing explanations invites picking one."""
    from ff_assist.projections import contrast_rejected

    rows = contrast_rejected(_quarterbacks_only_fail())
    flagged = [r["key"] for r in rows if r.get("subgroup")]
    assert len(flagged) >= 3, "co-occurring keys must be marked, not ranked"
    assert all(r["in_failures"] > 0.9 for r in rows if r.get("subgroup"))


def test_several_incompatible_single_key_fixes_all_claim_a_full_repair():
    """The tell that a fix search has been defeated: more than one mutually
    exclusive change repairs everything. That is a knob absorbing a subgroup
    effect, not a diagnosis — and it is why the real run offered an
    interception worth +1.5 points."""
    from ff_assist.projections import propose_fixes

    perfect = [f for f in propose_fixes(_quarterbacks_only_fail()) if f["still_broken"] == 0]
    assert len(perfect) > 1, (
        "with a confounded subgroup, several keys should each 'fix' everything"
    )


def test_the_arithmetic_dump_reads_the_answer_off_directly():
    """Where inference stops, one row of arithmetic finishes it. The gap must
    equal the planted error — 0.01 a passing yard — exactly."""
    from ff_assist.projections import explain_player

    week = _quarterbacks_only_fail()
    detail = explain_player(week)
    assert detail["gap"] > 0
    passing = next(t for t in detail["scored"] if t["stat"] == "pass_yd")
    assert detail["gap"] == pytest.approx(passing["value"] * 0.01, abs=0.02)
    assert detail["ours"] + detail["gap"] == pytest.approx(detail["sleeper"], abs=0.01)


def test_the_dump_lists_what_it_did_not_use():
    """A value present in the stat line and absent from our sum is the first
    place to look, so it has to be shown rather than silently skipped."""
    from ff_assist.projections import explain_player

    detail = explain_player(_quarterbacks_only_fail())
    unused = {t["stat"] for t in detail["not_used"]}
    assert {"pass_fd", "pass_inc", "pass_sack"} & unused


def test_the_dump_can_be_pointed_at_a_named_player():
    from ff_assist.projections import explain_player

    detail = explain_player(_quarterbacks_only_fail(), "Player 3")
    assert detail["player"] == "player3"
    assert "error" in explain_player(_quarterbacks_only_fail(), "Nobody At All")


def test_residual_separates_the_true_fix_from_a_lucky_one():
    """Counting repairs is not enough. Several wrong prices can drag every
    player inside a half-point tolerance; only the true one drives the error to
    zero. On a planted `pass_yd 0.04 -> 0.05` the correct change left 0.0000
    and three impostors left 0.07 to 0.29 — all four 'repaired' everything."""
    from ff_assist.projections import propose_fixes

    fixes = propose_fixes(_quarterbacks_only_fail())
    perfect = [f for f in fixes if f["still_broken"] == 0]
    assert len(perfect) > 1, "the tolerance should admit several candidates"

    exact = [f for f in perfect if f["residual_left"] < 0.02]
    assert len(exact) == 1, f"exactly one should be exact, got {exact}"
    assert exact[0]["key"] == "pass_yd"
    assert exact[0]["to"] == 0.05
    assert fixes[0]["key"] == "pass_yd", "and it must sort first"


def test_every_proposal_carries_its_residual():
    from ff_assist.projections import propose_fixes

    for fix in propose_fixes(_quarterbacks_only_fail(), depth=2):
        assert "residual_left" in fix
        assert fix["residual_left"] >= 0


def test_failures_are_reported_by_position():
    """It took several rounds of statistics to notice that six 'candidate
    causes' in the contrast table were six passing stats — i.e. one position.
    Saying it directly saves the next reader that detour."""
    entries = []
    for i in range(20):
        qb = i < 5
        if qb:
            st = {"pass_yd": 250, "pass_td": 2, "pass_int": 1}
            st["pts_ppr"] = 0.04 * 250 + 4 * 2 - 1 * 1        # we assume -2
            pos = "QB"
        else:
            st = {"rec": 5, "rec_yd": 60}
            st["pts_ppr"] = 5 + 6.0
            pos = "WR"
        entries.append(
            {"player": {"full_name": f"Player {i}", "position": pos}, "stats": st}
        )

    week = week_from(entries)
    breakdown = week.rejected_by_position()
    assert breakdown["QB"] == (5, 5), "every QB fails"
    assert breakdown["WR"] == (0, 15), "no receiver does"


def test_position_falls_back_to_fantasy_positions():
    entries = [{
        "player": {"full_name": "A B", "fantasy_positions": ["TE"]},
        "stats": {"rec": 4, "rec_yd": 40, "pts_ppr": 8.0},
    }]
    week = week_from(entries)
    assert week.positions["ab"] == "TE"


def test_a_payload_without_positions_still_works():
    """Reporting is a nicety; the reconstruction must not depend on it."""
    week = week_from(payload())
    assert week.positions == {}
    assert week.rejected_by_position() == {}
    assert week.line_for("Josh Allen") is not None
