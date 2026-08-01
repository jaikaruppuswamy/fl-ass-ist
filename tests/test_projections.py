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


def _synthetic(n: int, *, hidden_rate: float, int_price: float):
    """Players scored by a rule we deliberately do not fully map."""
    import random

    rng = random.Random(11)
    entries = []
    for i in range(n):
        st = {
            "pass_yd": rng.randint(150, 380), "pass_td": rng.randint(0, 4),
            "pass_int": rng.randint(0, 2), "rush_yd": rng.randint(0, 60),
            "rec": rng.randint(0, 9), "rec_yd": rng.randint(0, 120),
            "pass_cmp_40p": rng.randint(0, 3),   # unmapped and genuinely scored
            "pass_inc": rng.randint(5, 20),      # unmapped and NOT scored
        }
        st["pts_ppr"] = round(
            0.04 * st["pass_yd"] + 4 * st["pass_td"] + int_price * st["pass_int"]
            + 0.1 * st["rush_yd"] + 1.0 * st["rec"] + 0.1 * st["rec_yd"]
            + hidden_rate * st["pass_cmp_40p"], 2
        )
        entries.append({"player": {"full_name": f"Player {i}"}, "stats": st})
    return week_from(entries)


def test_inference_names_the_stat_key_we_failed_to_map():
    """Guessing which key is missing takes rounds; least squares over Sleeper's
    own totals reads the answer off directly."""
    result = infer_scoring(_synthetic(300, hidden_rate=0.5, int_price=-2.0))
    assert result["unmapped_but_scored"] == pytest.approx({"pass_cmp_40p": 0.5})


def test_inference_separates_a_wrong_price_from_a_missing_key():
    """The distinction the old verifier could not make: our crosswalk being
    short a category, versus our reference ruleset pricing one wrongly."""
    result = infer_scoring(_synthetic(300, hidden_rate=0.0, int_price=-1.0))
    assert not result["unmapped_but_scored"]
    assert result["mapped_but_mispriced"]["pass_int"]["fitted"] == pytest.approx(-1.0)
    assert result["mapped_but_mispriced"]["pass_int"]["assumed"] == -2.0


def test_inference_gives_a_worthless_stat_a_coefficient_of_zero():
    result = infer_scoring(_synthetic(300, hidden_rate=0.5, int_price=-2.0))
    assert result["fitted"]["pass_inc"] == pytest.approx(0.0, abs=1e-3)


def test_inference_declines_on_too_few_players():
    """Twenty players and twenty unknowns fits anything. It has to abstain."""
    assert "error" in infer_scoring(_synthetic(20, hidden_rate=0.5, int_price=-2.0))


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
