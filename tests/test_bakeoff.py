"""The bake-off's own arithmetic, and the one assumption everything rests on.

A backtest that leaks future information does not fail — it produces a
beautiful table and a wrong conclusion. So the first test here is not about
accuracy at all; it is about whether week W can see week W+1. The rest guard
the scoring functions, because a metric that is subtly inverted would make the
worst arm look like the best.

The network-touching test is skipped when nflverse is unreachable.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from projection_bakeoff import (  # noqa: E402
    Row,
    _best_lineup,
    arms,
    bench_points,
    common_subset,
    error_metrics,
    pair_accuracy,
    wins_per_point,
)


def row(**kw) -> Row:
    base = dict(
        player_id="x", name="X", position="WR", week=5, actual=10.0,
        szn_actual=10.0, last3_actual=10.0, szn_exp=10.0, last3_exp=10.0, games=4,
    )
    base.update(kw)
    return Row(**base)


# ---------------------------------------------------------------------------
# The assumption everything rests on
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not pytest.importorskip("nflreadpy", reason="nflverse unreachable"),
    reason="nflverse unreachable",
)
def test_features_never_see_the_week_they_predict():
    """The whole result is void if week W's features contain week W.

    Rather than trust the loop, this reconstructs the invariant from the output:
    for a player whose season is monotonically increasing, every lookback
    feature must be strictly below that week's actual. A single off-by-one in
    the slice would break it.
    """
    from projection_bakeoff import build_panel

    try:
        rows = build_panel(2025, 3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"nflverse unreachable: {type(exc).__name__}")

    assert rows, "panel is empty"
    by_player: dict[str, list[Row]] = {}
    for r in rows:
        by_player.setdefault(r.player_id, []).append(r)

    # Every player's first appearance must come after min_games weeks, and the
    # season-to-date average of a strictly rising series must lag the current
    # week. Checked on real data so a schema change is caught too.
    checked = 0
    for series in by_player.values():
        series.sort(key=lambda r: r.week)
        for i in range(1, len(series)):
            assert series[i].week > series[i - 1].week
        rising = [r for r in series if r.actual > r.szn_actual > 0]
        for r in rising:
            assert r.szn_actual < r.actual
            checked += 1
    assert checked > 100, f"only {checked} rows exercised the invariant"


def test_lookback_windows_are_computed_from_prior_weeks_only():
    """The same invariant, on a fixture where the answer is known by hand."""
    from projection_bakeoff import Row as R

    # weeks 1-5 scoring 1,2,3,4,100 — the blowup is week 5
    hist = [1.0, 2.0, 3.0, 4.0, 100.0]
    i = 4  # predicting week 5
    r = R(
        player_id="p", name="P", position="RB", week=5, actual=hist[i],
        szn_actual=sum(hist[:i]) / i,
        last3_actual=sum(hist[i - 3:i]) / 3,
        szn_exp=sum(hist[:i]) / i,
        last3_exp=sum(hist[i - 3:i]) / 3,
        games=i,
    )
    assert r.szn_actual == 2.5           # (1+2+3+4)/4 — no 100
    assert r.last3_actual == 3.0         # (2+3+4)/3 — no 100
    assert r.actual == 100.0


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_error_metrics_ignore_arms_with_no_opinion():
    rows = [row(actual=10.0, espn=None), row(actual=20.0, espn=18.0)]
    mae, _rmse, n = error_metrics(rows, lambda r: r.espn)
    assert n == 1 and mae == 2.0


def test_error_metrics_are_zero_for_a_perfect_predictor():
    rows = [row(actual=v) for v in (1.0, 9.0, 22.5)]
    mae, rmse, n = error_metrics(rows, lambda r: r.actual)
    assert (mae, rmse, n) == (0.0, 0.0, 3)


def test_pair_accuracy_is_1_for_an_oracle_and_0_for_its_inverse():
    rows = [
        row(player_id="a", name="A", actual=20.0, szn_actual=20.0),
        row(player_id="b", name="B", actual=10.0, szn_actual=10.0),
        row(player_id="c", name="C", actual=5.0, szn_actual=5.0),
    ]
    acc, n, _, _ = pair_accuracy(rows, lambda r: r.actual, 100.0)
    assert acc == 1.0 and n == 3
    acc, _, _, _ = pair_accuracy(rows, lambda r: -r.actual, 100.0)
    assert acc == 0.0


def test_pairs_only_form_within_a_position_and_week():
    """A WR and a RB are never a start/sit decision against each other, and
    neither are two players in different weeks."""
    rows = [
        row(player_id="a", position="WR", week=1, actual=20.0),
        row(player_id="b", position="RB", week=1, actual=10.0),
        row(player_id="c", position="WR", week=2, actual=10.0),
    ]
    _, n, _, _ = pair_accuracy(rows, lambda r: r.actual, 100.0)
    assert n == 0


def test_dead_heats_are_dropped_rather_than_scored():
    """Two players who finished level was never a decision, so counting it
    either way would move the accuracy number for no reason."""
    rows = [
        row(player_id="a", actual=12.0, szn_actual=15.0),
        row(player_id="b", actual=12.0, szn_actual=3.0),
    ]
    _, n, _, _ = pair_accuracy(rows, lambda r: r.szn_actual, 100.0)
    assert n == 0


def test_close_calls_are_a_subset_defined_by_the_predictors_own_gap():
    rows = [
        row(player_id="a", actual=20.0, szn_actual=18.0),
        row(player_id="b", actual=10.0, szn_actual=17.0),  # gap 1 -> close
        row(player_id="c", actual=1.0, szn_actual=2.0),    # gap 15+ -> not
    ]
    _acc, total, _close_acc, close_total = pair_accuracy(rows, lambda r: r.szn_actual, 3.0)
    assert total == 3
    assert close_total == 1


# ---------------------------------------------------------------------------
# Lineups
# ---------------------------------------------------------------------------


def _pool() -> list[Row]:
    spec = [
        ("qb1", "QB", 25.0), ("qb2", "QB", 20.0),
        ("rb1", "RB", 18.0), ("rb2", "RB", 15.0), ("rb3", "RB", 14.0),
        ("wr1", "WR", 17.0), ("wr2", "WR", 16.0), ("wr3", "WR", 13.0),
        ("te1", "TE", 12.0), ("te2", "TE", 4.0),
    ]
    return [row(player_id=k, name=k, position=p, actual=v, szn_actual=v) for k, p, v in spec]


def test_lineup_fills_every_slot_without_reusing_a_player():
    picked = _best_lineup(_pool(), lambda r: r.actual)
    assert len(picked) == 7
    assert len({p.player_id for p in picked}) == 7


def test_flex_takes_the_best_leftover_skill_player_not_a_quarterback():
    picked = _best_lineup(_pool(), lambda r: r.actual)
    ids = {p.player_id for p in picked}
    assert ids == {"qb1", "rb1", "rb2", "wr1", "wr2", "te1", "rb3"}
    assert "qb2" not in ids, "a second QB is not flex-eligible"


def test_a_perfect_predictor_leaves_nothing_on_the_bench():
    rows = _pool() * 5  # enough bodies for the simulator's pool floor
    for i, r in enumerate(rows):
        r.player_id = f"{r.player_id}-{i}"
    _score, gap, _sd = bench_points(rows, lambda r: r.actual, rosters=20, seed=1)
    assert gap == pytest.approx(0.0, abs=1e-9)


def test_bench_loss_is_never_negative():
    """The hindsight lineup is optimal by construction, so any real predictor
    must land at or below it. A negative gap means the optimizer or the scorer
    disagree about what a lineup is."""
    rows = _pool() * 5
    for i, r in enumerate(rows):
        r.player_id = f"{r.player_id}-{i}"
        r.szn_actual = r.actual + (i % 7) - 3  # a deliberately mediocre arm
    _score, gap, sd = bench_points(rows, lambda r: r.szn_actual, rosters=20, seed=1)
    assert gap >= 0.0
    assert sd >= 0.0


# ---------------------------------------------------------------------------
# The conversion that carries the recommendation
# ---------------------------------------------------------------------------


def test_a_point_a_week_is_worth_less_when_scores_are_noisier():
    """The whole argument against paying for projections is that this number is
    small, and it gets smaller as weekly variance grows."""
    assert wins_per_point(24.0) < wins_per_point(12.0)
    assert 0.1 < wins_per_point(24.0) < 0.25  # ~0.17 at a realistic spread


def test_wins_scale_with_the_season_length():
    assert wins_per_point(24.0, games=28) == pytest.approx(2 * wins_per_point(24.0, games=14))


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


def test_the_benchmark_arm_is_present_and_labelled_as_not_a_candidate():
    """It knows the future. If it ever stops being labelled, someone will read
    it as the winner."""
    catalogue = arms([row()], use_espn=False, use_sleeper=False)
    reference = [k for k in catalogue if k.startswith("[benchmark]")]
    assert len(reference) == 1
    assert catalogue[reference[0]](row(full_season=9.0)) == 9.0


def test_implied_scaling_is_neutral_at_the_league_average():
    """A team priced exactly at par must come back unchanged — otherwise the
    adjustment is a level shift wearing a matchup adjustment's clothes."""
    rows = [row(implied=20.0), row(implied=24.0)]  # par = 22.0
    catalogue = arms(rows, use_espn=False, use_sleeper=False)
    at_par = row(implied=22.0, szn_exp=10.0, szn_actual=10.0)
    assert catalogue["blend x implied 50%"](at_par) == pytest.approx(10.0)
    assert catalogue["blend x implied 100%"](at_par) == pytest.approx(10.0)


def test_implied_scaling_moves_in_the_right_direction_and_halves_correctly():
    rows = [row(implied=20.0), row(implied=24.0)]  # par = 22.0
    catalogue = arms(rows, use_espn=False, use_sleeper=False)
    base = dict(szn_exp=10.0, szn_actual=10.0)
    hot = row(implied=33.0, **base)   # 1.5x par
    cold = row(implied=11.0, **base)  # 0.5x par
    assert catalogue["blend x implied 100%"](hot) == pytest.approx(15.0)
    assert catalogue["blend x implied 50%"](hot) == pytest.approx(12.5)
    assert catalogue["blend x implied 100%"](cold) == pytest.approx(5.0)
    assert catalogue["blend x implied 50%"](cold) == pytest.approx(7.5)


def test_an_unpriced_game_falls_back_to_the_unscaled_blend():
    """Books price about three weeks out. A missing line must not be read as an
    implied total of zero, which would bench the entire team."""
    rows = [row(implied=22.0)]
    catalogue = arms(rows, use_espn=False, use_sleeper=False)
    unpriced = row(implied=None, szn_exp=12.0, szn_actual=8.0)
    assert catalogue["blend x implied 50%"](unpriced) == pytest.approx(10.0)


def test_external_arms_appear_only_when_enabled():
    catalogue = arms([row()], use_espn=False, use_sleeper=False)
    assert "ESPN" not in catalogue and "Sleeper" not in catalogue
    assert not [k for k in catalogue if "ENSEMBLE" in k or k == "ESPN + Sleeper"]

    # One external is enough for the three-way mean, but the two-way ESPN +
    # Sleeper arm needs both — otherwise it would silently be a single source
    # wearing an ensemble's name.
    catalogue = arms([row()], use_espn=True, use_sleeper=False)
    assert "ENSEMBLE (all 3)" in catalogue
    assert "ESPN + Sleeper" not in catalogue

    catalogue = arms([row()], use_espn=True, use_sleeper=True)
    assert {"ESPN", "Sleeper", "ENSEMBLE (all 3)", "ESPN + Sleeper"} <= set(catalogue)


# ---------------------------------------------------------------------------
# Fair comparison — the bug that made ESPN look terrible
# ---------------------------------------------------------------------------


def test_common_subset_keeps_only_rows_every_arm_can_score():
    catalogue = arms([row()], use_espn=True, use_sleeper=True)
    rows = [
        row(player_id="both", espn=10.0, sleeper=11.0),
        row(player_id="espn_only", espn=10.0, sleeper=None),
        row(player_id="neither", espn=None, sleeper=None),
    ]
    kept = common_subset(rows, catalogue)
    assert [r.player_id for r in kept] == ["both"]


def test_common_subset_is_everything_when_no_arm_abstains():
    catalogue = arms([row()], use_espn=False, use_sleeper=False)
    rows = [row(player_id=str(i)) for i in range(5)]
    assert len(common_subset(rows, catalogue)) == 5


def test_restricting_to_better_players_inflates_error_on_its_own():
    """The artefact this whole mechanism exists to defeat, in miniature.

    The same predictor, with the same *relative* skill, scores a much larger
    MAE on a population of high scorers — because absolute error scales with
    the size of the numbers. An arm covering only rostered players is therefore
    penalised for its coverage, not its accuracy. That is exactly how ESPN came
    out two points of MAE behind a trailing average in the first real run.
    """
    def predict(r: Row) -> float:
        return r.actual * 0.8  # uniformly 20% low, whatever the level

    scrubs = [row(player_id=f"s{i}", actual=5.0) for i in range(50)]
    stars = [row(player_id=f"t{i}", actual=25.0) for i in range(50)]

    mae_scrubs, _, _ = error_metrics(scrubs, predict)
    mae_stars, _, _ = error_metrics(stars, predict)
    assert mae_stars == pytest.approx(5.0)
    assert mae_scrubs == pytest.approx(1.0)
    assert mae_stars > 4 * mae_scrubs


def test_pair_accuracy_also_degrades_on_a_narrower_population():
    """The second half of the artefact: ranking two similar players is harder
    than ranking a good one against a scrub, so an arm restricted to rostered
    players loses accuracy for free."""
    def predict(r: Row) -> float:
        return r.szn_actual

    # Wide field: projections track results, easy pairs included.
    wide = [
        row(player_id=f"w{i}", actual=float(i), szn_actual=float(i) + (1 if i % 2 else -1))
        for i in range(2, 22)
    ]
    # Narrow field: the same noise, but everyone is within a point of everyone.
    narrow = [
        row(player_id=f"n{i}", actual=20.0 + i * 0.1,
            szn_actual=20.0 + i * 0.1 + (1 if i % 2 else -1))
        for i in range(2, 22)
    ]
    wide_acc, _, _, _ = pair_accuracy(wide, predict, 100.0)
    narrow_acc, _, _, _ = pair_accuracy(narrow, predict, 100.0)
    assert wide_acc > narrow_acc


def test_lookahead_audit_catches_an_arm_that_knows_who_sat():
    """The check that has to pass before anything is built on an external feed.

    Both external arms are fetched today for a season that already finished. If
    a provider restates — or just serves the last value it held for a player who
    was ruled out on Sunday morning — its numbers are inflated by information no
    forecaster had. A scoreless week is the tell.
    """
    from projection_bakeoff import lookahead_audit

    rows = []
    for i in range(60):
        sat = i % 2 == 0
        rows.append(
            row(
                player_id=f"p{i}",
                actual=0.0 if sat else 12.0,
                szn_actual=10.0,
                szn_exp=10.0,
                # honest: same guess either way. cheating: near zero when he sat.
                espn=10.0,
                sleeper=0.2 if sat else 10.0,
            )
        )

    catalogue = arms(rows, use_espn=True, use_sleeper=True)
    result = {
        label: ratio
        for label, _d, _l, ratio, _n in lookahead_audit(
            rows, catalogue, ("ESPN", "Sleeper"), min_scoreless=10
        )
    }

    assert result["ESPN"] == pytest.approx(1.0), "an honest arm guesses the same either way"
    assert result["Sleeper"] < 0.1, "a cheating arm projects near zero for the weeks he sat"
    assert result["Sleeper"] < result["ESPN"] * 0.75, "must clear the flagging threshold"


def test_lookahead_audit_skips_the_benchmark_arm():
    """It cheats by construction. Flagging it would train the eye to ignore the
    column, which is the one column that must not be ignored."""
    from projection_bakeoff import lookahead_audit

    rows = [
        row(player_id=f"p{i}", actual=0.0 if i % 2 else 9.0, full_season=4.0, sleeper=5.0)
        for i in range(80)
    ]
    catalogue = arms(rows, use_espn=False, use_sleeper=True)
    labels = [
        label
        for label, *_ in lookahead_audit(
            rows, catalogue, ("Sleeper",), min_scoreless=10
        )
    ]
    assert not any(label.startswith("[benchmark]") for label in labels)
    assert any(label == "Sleeper" for label in labels)


def test_lookahead_audit_stays_quiet_without_enough_scoreless_weeks():
    """Thirty zero-point weeks is not a sample. A ratio computed on four rows
    would be noise presented as an accusation."""
    from projection_bakeoff import lookahead_audit

    rows = [
        row(player_id=f"p{i}", actual=0.0 if i < 3 else 10.0, sleeper=8.0)
        for i in range(40)
    ]
    catalogue = arms(rows, use_espn=False, use_sleeper=True)
    reported = lookahead_audit(rows, catalogue, ("Sleeper",), min_scoreless=10)
    assert [label for label, *_ in reported] == ["Sleeper  (only 3 scoreless — skipped)"]


def test_a_missing_league_season_does_not_kill_the_whole_espn_arm(monkeypatch):
    """A league you did not play in a given season 404s, and that is history,
    not a failure. The first version abandoned the arm on the first miss, which
    silently cost the 2024 comparison every league that did exist."""
    import projection_bakeoff as bo

    class FakePlayer:
        def __init__(self, name):
            self.name, self.position = name, "WR"
            self.projected_breakdown = None
            self.projected_points = 12.0

    class FakeBox:
        home_lineup = [FakePlayer("X")]
        away_lineup = []

    class FakeLeague:
        def __init__(self, key):
            self.key = key
            self.espn_request = type("R", (), {"get_league": staticmethod(lambda: {})})()

        def box_scores(self, week):
            return [FakeBox()]

    cfgs = [type("C", (), {"key": k})() for k in ("dead", "alive")]

    def fake_get_league(cfg, settings, year):
        if cfg.key == "dead":
            raise LookupError("LeagueNotFound")
        return FakeLeague(cfg.key)

    monkeypatch.setattr(
        "ff_assist.config.load_settings",
        lambda: type("S", (), {"leagues": cfgs})(),
    )
    monkeypatch.setattr("ff_assist.espn_client.get_league", fake_get_league)
    monkeypatch.setattr(
        "ff_assist.scoring.ScoringSettings.from_raw", staticmethod(lambda raw, key: None)
    )

    rows = [row(name="X", week=5)]
    hits, note = bo.attach_espn(rows, 2024, None)
    assert hits == 1, note
    assert "alive" in note
    assert "dead" in note, "a skipped league must be reported, not swallowed"
    assert rows[0].espn == 12.0


def test_espn_arm_reports_zero_hits_rather_than_a_cheerful_message(monkeypatch):
    """Zero coverage must read as failure to the caller. The first version
    string-matched on the word 'matched', so '0/4,215 matched' counted as
    success and left a dead arm enabled."""
    import projection_bakeoff as bo

    monkeypatch.setattr(
        "ff_assist.config.load_settings",
        lambda: type("S", (), {"leagues": [type("C", (), {"key": "x"})()]})(),
    )

    def always_dead(cfg, settings, year):
        raise LookupError("LeagueNotFound")

    monkeypatch.setattr("ff_assist.espn_client.get_league", always_dead)
    hits, note = bo.attach_espn([row()], 2024, None)
    assert hits == 0
    assert "2024" in note


def test_the_ensemble_abstains_rather_than_quietly_becoming_the_blend():
    """If every external source is missing for a player, the ensemble must
    return None. Silently degrading to the free blend would make the ensemble
    look identical to it and hide the fact that coverage was the problem."""
    catalogue = arms([row()], use_espn=True, use_sleeper=True)
    ensemble = catalogue["ENSEMBLE (all 3)"]
    assert ensemble(row(espn=None, sleeper=None)) is None
    assert ensemble(row(szn_exp=10.0, szn_actual=10.0, espn=14.0, sleeper=None)) == 12.0

    # The two-way arm abstains unless BOTH sources are present, so it is never
    # quietly reporting one source's opinion as a consensus.
    pair = catalogue["ESPN + Sleeper"]
    assert pair(row(espn=14.0, sleeper=None)) is None
    assert pair(row(espn=None, sleeper=10.0)) is None
    assert pair(row(espn=14.0, sleeper=10.0)) == 12.0


# ---------------------------------------------------------------------------
# The forward test's own guard
# ---------------------------------------------------------------------------


def test_diff_refuses_to_conclude_before_the_games(tmp_path, monkeypatch, capsys):
    """Run the diff the same afternoon you take the snapshot and of course
    nothing has moved — which reads as 'the archive is honest' and is no finding
    at all. That exact false all-clear happened once; this is the guard."""
    import json

    import snapshot_projections as sp

    monkeypatch.setattr(sp, "SNAPSHOT_DIR", tmp_path)
    (tmp_path / "sleeper_2026_wk01.json").write_text(
        json.dumps({"season": 2026, "week": 1, "taken_at": "2026-08-01T21:18:00+00:00",
                    "source": "x", "projections": {"joshallen": 23.9}})
    )
    monkeypatch.setattr(sp, "games_played", lambda season, week: (0, 16))

    def must_not_run(*a, **k):  # pragma: no cover
        raise AssertionError("fetched the archive before the games were played")

    monkeypatch.setattr(sp, "fetch_sleeper", must_not_run)

    assert sp.diff(2026, 1) == 0
    out = capsys.readouterr().out
    assert "has not been played" in out
    assert "honest" not in out


def test_diff_declines_to_certify_when_it_cannot_check_the_schedule(
    tmp_path, monkeypatch, capsys
):
    """nflverse unreachable means 'I do not know whether the games happened',
    which must not round to 'the archive is honest'."""
    import json

    import snapshot_projections as sp

    monkeypatch.setattr(sp, "SNAPSHOT_DIR", tmp_path)
    (tmp_path / "sleeper_2025_wk05.json").write_text(
        json.dumps({"season": 2025, "week": 5, "taken_at": "2025-10-04T18:00:00+00:00",
                    "source": "x", "projections": {"joshallen": 23.9}})
    )
    monkeypatch.setattr(sp, "games_played", lambda season, week: None)
    monkeypatch.setattr(sp, "fetch_sleeper", lambda season, week: {"joshallen": 23.9})

    sp.diff(2025, 5)
    out = capsys.readouterr().out
    assert "could not" in out
    assert "The archive is honest" not in out


def test_diff_certifies_only_once_the_week_is_final(tmp_path, monkeypatch, capsys):
    import json

    import snapshot_projections as sp

    monkeypatch.setattr(sp, "SNAPSHOT_DIR", tmp_path)
    (tmp_path / "sleeper_2025_wk05.json").write_text(
        json.dumps({"season": 2025, "week": 5, "taken_at": "2025-10-04T18:00:00+00:00",
                    "source": "x", "projections": {"joshallen": 23.9, "bijanrobinson": 17.1}})
    )
    monkeypatch.setattr(sp, "games_played", lambda season, week: (16, 16))
    monkeypatch.setattr(
        sp, "fetch_sleeper", lambda season, week: {"joshallen": 23.9, "bijanrobinson": 17.1}
    )

    assert sp.diff(2025, 5) == 0
    assert "The archive is honest" in capsys.readouterr().out


def test_diff_calls_out_wholesale_downward_revision(tmp_path, monkeypatch, capsys):
    """The failure mode worth catching: an archive that has absorbed who did
    not play marks those players down and touches nobody else."""
    import json

    import snapshot_projections as sp

    monkeypatch.setattr(sp, "SNAPSHOT_DIR", tmp_path)
    before = {f"p{i}": 12.0 for i in range(20)}
    after = dict(before)
    for i in range(5):
        after[f"p{i}"] = 0.2  # ruled out, retroactively
    (tmp_path / "sleeper_2025_wk05.json").write_text(
        json.dumps({"season": 2025, "week": 5, "taken_at": "x", "source": "x",
                    "projections": before})
    )
    monkeypatch.setattr(sp, "games_played", lambda season, week: (16, 16))
    monkeypatch.setattr(sp, "fetch_sleeper", lambda season, week: after)

    assert sp.diff(2025, 5) == 1, "a restating archive must exit non-zero"
    assert "Restated after the fact" in capsys.readouterr().out


def test_an_early_snapshot_can_be_replaced_by_a_closer_one(tmp_path, monkeypatch, capsys):
    """A snapshot taken 39 days out measures camp-news revision, not hindsight.
    It has to be replaceable, or the first over-eager run poisons that week."""
    import json

    import snapshot_projections as sp

    monkeypatch.setattr(sp, "SNAPSHOT_DIR", tmp_path)
    target = tmp_path / "sleeper_2026_wk01.json"
    target.write_text(
        json.dumps({"season": 2026, "week": 1, "taken_at": "2026-08-01T21:18:00+00:00",
                    "days_before_kickoff": 39, "source": "x",
                    "projections": {"joshallen": 23.9}})
    )
    monkeypatch.setattr(sp, "first_kickoff", lambda season, week: date(2026, 9, 9))
    monkeypatch.setattr(sp, "fetch_sleeper", lambda season, week: {"joshallen": 24.4})

    class FakeDT:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 9, 5, 12, 0, tzinfo=tz)

    monkeypatch.setattr(sp, "datetime", FakeDT)
    assert sp.take(2026, 1) == 0
    assert "replacing a snapshot" in capsys.readouterr().out
    assert json.loads(target.read_text())["days_before_kickoff"] == 4


def test_a_fresh_snapshot_is_never_silently_overwritten(tmp_path, monkeypatch, capsys):
    """Once a snapshot is taken close to kickoff it is the only copy of the
    pre-game value. Re-running must not destroy it."""
    import json

    import snapshot_projections as sp

    monkeypatch.setattr(sp, "SNAPSHOT_DIR", tmp_path)
    target = tmp_path / "sleeper_2026_wk01.json"
    target.write_text(
        json.dumps({"season": 2026, "week": 1, "taken_at": "2026-09-05T12:00:00+00:00",
                    "days_before_kickoff": 2, "source": "x",
                    "projections": {"joshallen": 23.9}})
    )
    monkeypatch.setattr(sp, "first_kickoff", lambda season, week: date(2026, 9, 9))

    def must_not_run(*a, **k):  # pragma: no cover
        raise AssertionError("refetched over a good snapshot")

    monkeypatch.setattr(sp, "fetch_sleeper", must_not_run)

    class FakeDT:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 9, 8, 12, 0, tzinfo=tz)

    monkeypatch.setattr(sp, "datetime", FakeDT)
    assert sp.take(2026, 1) == 1
    assert "refusing to overwrite" in capsys.readouterr().out
    assert json.loads(target.read_text())["projections"]["joshallen"] == 23.9


def test_a_snapshot_after_kickoff_is_refused_outright(tmp_path, monkeypatch, capsys):
    import snapshot_projections as sp

    monkeypatch.setattr(sp, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(sp, "first_kickoff", lambda season, week: date(2026, 9, 9))

    class FakeDT:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 9, 10, 12, 0, tzinfo=tz)

    monkeypatch.setattr(sp, "datetime", FakeDT)
    assert sp.take(2026, 1) == 1
    assert "Too late" in capsys.readouterr().out
