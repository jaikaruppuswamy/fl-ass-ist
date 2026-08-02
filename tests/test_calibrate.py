"""Phase 5: does the system's own advice hold up, week by week.

The tests that matter here are about *refusal*. A calibration log is only worth
keeping if every row in it was written down before the games — a reconstructed
recommendation is a guess about the past wearing a measurement's clothes, and
this project has shipped that shape of mistake more than once. So the freeze
must refuse to overwrite, refuse to run after kickoff, and the scorer must
refuse to invent a record that does not exist.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import calibrate  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(calibrate, "CALIB_DIR", tmp_path)
    monkeypatch.setattr(calibrate, "LOG", tmp_path / "log.csv")
    return tmp_path


def slate(changes=True):
    """A slate recommending Bench Stud over Weak Starter, by 4 projected points."""
    optimal = [
        {"name": "Bench Stud", "pos": "RB", "slot": "RB", "proj": 14.0,
         "disagree": 4.2, "recommended": True},
        {"name": "Solid Guy", "pos": "WR", "slot": "WR", "proj": 11.0},
    ]
    bench = [{"name": "Weak Starter", "pos": "RB", "proj": 10.0,
              "usage_trend": "falling"}]
    if not changes:
        optimal = [{"name": "Solid Guy", "pos": "WR", "slot": "WR", "proj": 11.0}]
        bench = [{"name": "Weak Starter", "pos": "RB", "proj": 4.0}]
    return {
        "league": "inai", "week": 3, "scoring": "PPR",
        "current_projected": 100.0, "optimal_projected": 104.0,
        "changes": ({"start": ["Bench Stud"], "bench": ["Weak Starter"],
                     "points_gained": 4.0} if changes else None),
        "optimal_lineup": optimal,
        "bench": bench,
        "environment_available": True,
        "projection_basis": "espn+sleeper (427/462)",
    }


def freeze(tmp_path, *, changes=True, week=3, season=2026):
    (tmp_path / f"recommendations_{season}_wk{week:02d}.json").write_text(
        json.dumps({
            "season": season, "week": week,
            "frozen_at": "2026-09-19T18:00:00+00:00",
            "leagues": {"inai": slate(changes)},
        })
    )


def wire(monkeypatch, actuals, *, finished=(16, 16)):
    cfg = type("C", (), {"key": "inai"})()
    monkeypatch.setattr(
        "ff_assist.config.load_settings", lambda: type("S", (), {"leagues": [cfg]})()
    )
    monkeypatch.setattr(calibrate, "actual_points", lambda c, s, w, st: actuals)
    monkeypatch.setattr(
        "snapshot_projections.games_played", lambda season, week: finished
    )


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_scoring_a_week_with_no_frozen_record_is_refused(isolated, capsys):
    """The whole point. Rebuilding what we 'would have said' after seeing the
    results is not a measurement of anything."""
    assert calibrate.score(2026, 3) == 1
    out = capsys.readouterr().out
    assert "no frozen recommendation" in out
    assert not calibrate.LOG.exists()


def test_freezing_twice_is_refused(isolated, monkeypatch, capsys):
    freeze(isolated)
    monkeypatch.setattr("snapshot_projections.games_played", lambda s, w: (0, 16))
    assert calibrate.record(2026, 3) == 1
    assert "refusing to overwrite" in capsys.readouterr().out


def test_freezing_after_kickoff_is_refused(isolated, monkeypatch, capsys):
    """A prediction recorded after the fact is not a prediction."""
    monkeypatch.setattr("snapshot_projections.games_played", lambda s, w: (4, 16))
    assert calibrate.record(2026, 3) == 1
    assert "Too late" in capsys.readouterr().out


def test_scoring_before_any_game_finishes_is_refused(isolated, monkeypatch, capsys):
    freeze(isolated)
    wire(monkeypatch, {}, finished=(0, 16))
    assert calibrate.score(2026, 3) == 1


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_a_correct_swap_is_recorded_as_right(isolated, monkeypatch):
    wire(monkeypatch, {"Bench Stud": 22.0, "Weak Starter": 9.0, "Solid Guy": 12.0})
    freeze(isolated)
    assert calibrate.score(2026, 3) == 0

    rows = list(csv.DictReader(calibrate.LOG.open()))
    assert len(rows) == 1
    row = rows[0]
    assert row["decision_type"] == "swap"
    assert row["recommended"] == "Bench Stud"
    assert row["alternative"] == "Weak Starter"
    assert float(row["projected_delta"]) == 4.0
    assert float(row["actual_delta"]) == 13.0
    assert row["was_right"] == "1"


def test_a_swap_that_backfired_is_recorded_as_wrong(isolated, monkeypatch):
    """A log that only records wins is a trophy cabinet."""
    wire(monkeypatch, {"Bench Stud": 3.0, "Weak Starter": 18.0, "Solid Guy": 12.0})
    freeze(isolated)
    calibrate.score(2026, 3)

    row = next(csv.DictReader(calibrate.LOG.open()))
    assert row["was_right"] == "0"
    assert float(row["actual_delta"]) == -15.0
    assert float(row["projected_delta"]) == 4.0


def test_leaving_the_lineup_alone_is_also_scored(isolated, monkeypatch):
    """Otherwise the log only measures the weeks the system spoke up, and a
    system that says nothing looks perfect."""
    wire(monkeypatch, {"Solid Guy": 14.0, "Weak Starter": 2.0})
    freeze(isolated, changes=False)
    calibrate.score(2026, 3)

    row = next(csv.DictReader(calibrate.LOG.open()))
    assert row["decision_type"] == "hold"
    assert row["was_right"] == "1"
    assert row["primary_reason"] == "lineup already optimal"


def test_the_reason_column_captures_what_drove_the_call(isolated, monkeypatch):
    wire(monkeypatch, {"Bench Stud": 22.0, "Weak Starter": 9.0, "Solid Guy": 12.0})
    freeze(isolated)
    calibrate.score(2026, 3)
    assert "sources split" in next(csv.DictReader(calibrate.LOG.open()))["primary_reason"]


def test_reasons_are_ranked_by_how_much_the_evidence_backs_them():
    """A disagreement between sources is the highest-value field on a row, so
    it outranks a usage trend, which outranks a Vegas line."""
    assert "sources split" in calibrate.primary_reason(
        {"disagree": 4.2, "usage_trend": "rising", "implied_total": 27.5}
    )
    assert "usage" in calibrate.primary_reason(
        {"usage_trend": "rising", "implied_total": 27.5}
    )
    assert "implied" in calibrate.primary_reason({"implied_total": 27.5})
    assert calibrate.primary_reason({}) == "projection only"


def test_the_log_appends_rather_than_replacing(isolated, monkeypatch):
    wire(monkeypatch, {"Bench Stud": 22.0, "Weak Starter": 9.0, "Solid Guy": 12.0})
    freeze(isolated, week=3)
    calibrate.score(2026, 3)
    freeze(isolated, week=4)
    calibrate.score(2026, 4)

    rows = list(csv.DictReader(calibrate.LOG.open()))
    assert len(rows) == 2
    assert {r["week"] for r in rows} == {"3", "4"}


def test_every_row_carries_the_season_and_the_freeze_time(isolated, monkeypatch):
    """Two seasons in one file must not silently merge, and a recommendation
    frozen an hour before kickoff is worth more than one frozen on Wednesday."""
    wire(monkeypatch, {"Bench Stud": 22.0, "Weak Starter": 9.0, "Solid Guy": 12.0})
    freeze(isolated)
    calibrate.score(2026, 3)

    row = next(csv.DictReader(calibrate.LOG.open()))
    assert row["season"] == "2026"
    assert row["frozen_at"].startswith("2026-09-19")
    assert "espn+sleeper" in row["projection_basis"]


def test_a_player_with_no_result_is_skipped_not_scored_as_zero(isolated, monkeypatch):
    """A name that failed to resolve is missing data, not a zero-point game."""
    wire(monkeypatch, {"Weak Starter": 9.0, "Solid Guy": 12.0})   # no Bench Stud
    freeze(isolated)
    calibrate.score(2026, 3)
    assert not calibrate.LOG.exists()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_the_report_calls_a_small_sample_unmeasured(isolated, monkeypatch, capsys):
    """Three right out of four is 75% and means nothing. Saying so is the
    difference between calibration and self-congratulation."""
    wire(monkeypatch, {"Bench Stud": 22.0, "Weak Starter": 9.0, "Solid Guy": 12.0})
    freeze(isolated)
    calibrate.score(2026, 3)
    capsys.readouterr()

    calibrate.report()
    out = capsys.readouterr().out
    assert "within noise" in out
    assert "95% band" in out


def test_the_report_survives_an_empty_log(isolated, capsys):
    calibrate.LOG.write_text(",".join(calibrate.FIELDS) + "\n")
    assert calibrate.report() == 0
    assert "empty" in capsys.readouterr().out


def test_the_report_measures_whether_predicted_gains_are_honest(isolated, monkeypatch, capsys):
    """The most useful number in the file: if the system says +4 and delivers
    +1 every time, the projections are systematically optimistic."""
    wire(monkeypatch, {"Bench Stud": 11.0, "Weak Starter": 10.0, "Solid Guy": 12.0})
    freeze(isolated)
    calibrate.score(2026, 3)
    capsys.readouterr()

    calibrate.report()
    out = capsys.readouterr().out
    assert "mean(actual - predicted)" in out
    assert "-3.00" in out          # said +4.0, delivered +1.0
