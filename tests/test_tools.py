"""Tool-shape tests.

ESPN is not reachable from CI, and in August the 2026 leagues have not drafted,
so these drive the tools with a fake League built from each real league's
*actual* settings plus a synthetic roster. That still exercises everything that
can be wrong in our own code: scoring integration, the optimizer, row building,
swap detection and the response-size budget.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist import tools  # noqa: E402
from ff_assist.config import LeagueConfig, Settings  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"
_dumps = sorted(SAMPLES.glob("league_*_dump.json")) if SAMPLES.is_dir() else []

pytestmark = pytest.mark.skipif(not _dumps, reason="run scripts/dump_league_settings.py first")


# --- fakes -----------------------------------------------------------------


@dataclass
class FakePlayer:
    name: str
    position: str
    eligibleSlots: tuple[str, ...]
    slot_position: str
    projected_points: float
    projected_breakdown: dict = field(default_factory=dict)
    proTeam: str = "BUF"
    pro_opponent: str = "MIA"
    injuryStatus: str = "ACTIVE"
    on_bye_week: bool = False
    percent_started: float = 50.0


@dataclass
class FakeTeam:
    team_id: int
    team_name: str = "Test Team"
    wins: int = 1
    losses: int = 0
    ties: int = 0
    roster: list = field(default_factory=list)


@dataclass
class FakeBox:
    home_team: FakeTeam
    away_team: FakeTeam
    home_lineup: list
    away_lineup: list
    home_score: float = 0.0
    away_score: float = 0.0


class FakeRequest:
    def __init__(self, settings):
        self._settings = settings

    def get_league(self):
        return {"settings": self._settings}


class FakeLeague:
    def __init__(self, settings, lineup, opp_lineup, team_id=1):
        self.espn_request = FakeRequest(settings)
        self.current_week = 3
        self.settings = type("S", (), {"name": "Fake League"})()
        self.teams = [FakeTeam(team_id, roster=lineup), FakeTeam(99, "Opponent")]
        self._box = FakeBox(FakeTeam(team_id), FakeTeam(99, "Opponent"), lineup, opp_lineup)

    def box_scores(self, week=None):
        return [self._box]


def make_roster(starting_slots):
    """A roster that is deliberately mis-set: the best RB is on the bench."""
    elig = {
        "QB": ("QB",),
        "RB": ("RB", "RB/WR", "RB/WR/TE"),
        "WR": ("WR", "RB/WR", "WR/TE", "RB/WR/TE"),
        "TE": ("TE", "WR/TE", "RB/WR/TE"),
        "K": ("K",),
        "D/ST": ("D/ST",),
    }
    lineup = []
    for i, slot in enumerate(starting_slots):
        pos = slot if slot in elig else ("RB" if "RB" in slot else "WR")
        lineup.append(
            FakePlayer(
                name=f"Starter{i}-{pos}",
                position=pos,
                eligibleSlots=elig[pos],
                slot_position=slot,
                projected_points=10.0,
                projected_breakdown={"receivingReceptions": 5, "receivingYards": 60}
                if pos in ("WR", "TE", "RB")
                else {},
            )
        )
    # A clearly better RB stranded on the bench.
    lineup.append(
        FakePlayer(
            name="BenchStud-RB",
            position="RB",
            eligibleSlots=elig["RB"],
            slot_position="BE",
            projected_points=30.0,
            projected_breakdown={"rushingYards": 120, "28": 12, "receivingReceptions": 4},
        )
    )
    return lineup


@pytest.fixture(params=_dumps, ids=lambda p: p.stem)
def league_env(request, monkeypatch, tmp_path):
    payload = json.loads(request.param.read_text())
    key = payload["league_key"]
    raw_settings = payload["settings"]

    from ff_assist.slots import LineupSlots

    slots = LineupSlots.from_raw(raw_settings["rosterSettings"]).starting_slots
    lineup = make_roster(slots)
    opp = make_roster(slots)

    settings = Settings(
        espn_s2="x" * 300,
        swid="{00000000-0000-0000-0000-000000000001}",
        season=payload["season"],
        leagues=(LeagueConfig(key=key, league_id=1, team_id=1, label=key),),
        cache_dir=tmp_path,
        log_level="INFO",
        mcp_bearer_token="",
        fantasypros_api_key="",
    )
    fake = FakeLeague(raw_settings, lineup, opp)
    monkeypatch.setattr(tools, "get_league", lambda *a, **k: fake)
    monkeypatch.setattr(tools, "my_team", lambda lg, cfg: lg.teams[0])
    return settings, key, slots


# --- tests -----------------------------------------------------------------


def test_list_leagues_shape_and_size(league_env):
    settings, key, _ = league_env
    out = tools.list_leagues(settings)
    lg = out["leagues"][0]
    assert lg["key"] == key
    assert lg["scoring"]["format"].startswith(("PPR", "Half-PPR", "Standard"))
    assert lg["lineup"]["starters"]
    assert len(json.dumps(out).encode()) < 5 * 1024


def test_start_sit_promotes_the_stranded_bench_player(league_env):
    settings, key, slots = league_env
    out = tools.get_start_sit_slate(key, settings=settings)
    assert out["optimal_projected"] > out["current_projected"]
    assert out["changes"], "a 30-point RB on the bench must produce a change"
    assert out["changes"]["points_gained"] > 0


def test_change_directions_are_not_inverted(league_env):
    """`start` must name players to move IN and `bench` players to move OUT.
    An inverted response is worse than no response."""
    settings, key, _ = league_env
    out = tools.get_start_sit_slate(key, settings=settings)
    change = out["changes"]
    started = {r["name"] for r in out["optimal_lineup"] if r.get("name")}
    # BenchStud-RB is the best player on the roster and starts nowhere now.
    assert "BenchStud-RB" in change["start"]
    assert "BenchStud-RB" in started
    for name in change["start"]:
        assert name in started, f"{name} is listed to start but is not in the lineup"
    for name in change["bench"]:
        assert name not in started, f"{name} is listed to bench but is still starting"


def test_start_sit_fills_every_fillable_slot_exactly_once(league_env):
    settings, key, slots = league_env
    out = tools.get_start_sit_slate(key, settings=settings)
    assert [r["slot"] for r in out["optimal_lineup"]] == slots
    named = [r["name"] for r in out["optimal_lineup"] if r.get("name")]
    assert len(named) == len(set(named)), "a player was started in two slots"


def test_start_sit_respects_the_response_budget(league_env):
    settings, key, _ = league_env
    out = tools.get_start_sit_slate(key, settings=settings)
    assert len(json.dumps(out).encode()) < 5 * 1024


def test_matchup_margin_and_probability_are_consistent(league_env):
    settings, key, _ = league_env
    out = tools.get_matchup(key, settings=settings)
    assert out["projected_margin"] == pytest.approx(
        out["me"]["projected"] - out["opponent"]["projected"], abs=0.01
    )
    assert 0.0 <= out["win_probability"] <= 1.0
    # identical rosters on both sides -> a coin flip
    assert out["win_probability"] == pytest.approx(0.5, abs=0.01)
    assert len(json.dumps(out).encode()) < 5 * 1024


def test_missing_team_id_is_reported_not_guessed(league_env):
    settings, key, _ = league_env
    no_team = Settings(**{**settings.__dict__, "leagues": (LeagueConfig(key=key, league_id=1),)})
    assert "error" in tools.get_start_sit_slate(key, settings=no_team)
    assert "error" in tools.get_matchup(key, settings=no_team)


def test_win_probability_is_monotonic():
    from ff_assist.tools import _win_probability

    assert _win_probability(0) == 0.5
    assert _win_probability(20) > 0.5 > _win_probability(-20)
    assert _win_probability(100) > _win_probability(40)
    assert 0.0 <= _win_probability(-500) and _win_probability(500) <= 1.0


def test_predraft_error_is_readable_not_a_raw_keyerror():
    """Before a draft, espn-api raises KeyError('rosterForCurrentScoringPeriod').
    Surfacing that verbatim makes a normal August state look like a crash."""
    from ff_assist.tools import _no_roster_reason

    class League:
        current_week = 0

    message = _no_roster_reason(League(), KeyError("rosterForCurrentScoringPeriod"))
    assert "has not drafted yet" in message
    assert "rosterForCurrentScoringPeriod" not in message
    assert "re-run after your draft" in message


def test_a_genuine_midseason_failure_still_shows_the_detail():
    """In September an error probably IS real — do not swallow it."""
    from ff_assist.tools import _no_roster_reason

    class League:
        current_week = 6

    message = _no_roster_reason(League(), ValueError("upstream exploded"))
    assert "week 6" in message
    assert "upstream exploded" in message


# --- the Sleeper second opinion --------------------------------------------
#
# Everything above runs with Sleeper unreachable, which is the fallback path and
# must stay byte-identical to the pre-integration behaviour. These exercise the
# other branch: the consensus actually changing a lineup, and saying so.


def fake_sleeper(**by_name):
    """A SleeperWeek whose scored projection for each named player is fixed.

    Built through the real constructor rather than a stub so the name
    normalisation and the None-vs-zero distinction are exercised too.
    """
    from ff_assist.projections import SleeperWeek
    from ff_assist.usage import normalize_name

    week = SleeperWeek(season=2026, week=3)
    for name, receiving_yards in by_name.items():
        week.lines[normalize_name(name)] = {"receiving_yards": receiving_yards}
    return week


def test_sleeper_absent_leaves_the_slate_exactly_as_it_was(league_env, monkeypatch):
    """The whole design promise: an outage at somebody else's undocumented
    endpoint costs the second opinion and nothing else."""
    settings, key, _ = league_env
    monkeypatch.setattr(tools, "_safe_sleeper", lambda season, week: None)
    without = tools.get_start_sit_slate(key, 3, settings=settings)

    assert without["projection_basis"] == "espn"
    for row in without["optimal_lineup"]:
        if row.get("name"):
            assert "sleeper_proj" not in row
            assert "disagree" not in row
            # The ranking number still exists and still equals ESPN's.
            assert row["proj"] == row.get("my_scoring_proj", row["espn_proj"])


def test_the_consensus_is_the_mean_of_the_two_sources(league_env, monkeypatch):
    settings, key, _ = league_env
    monkeypatch.setattr(
        tools, "_safe_sleeper", lambda season, week: fake_sleeper(**{"Starter0-QB": 100})
    )
    out = tools.get_start_sit_slate(key, 3, settings=settings)

    rows = {r["name"]: r for r in out["optimal_lineup"] + out["bench"] if r.get("name")}
    row = rows.get("Starter0-QB")
    if row is None:
        pytest.skip("this league's slot shape does not produce that starter")
    espn = row.get("my_scoring_proj", row["espn_proj"])
    assert row["proj"] == pytest.approx(round((espn + row["sleeper_proj"]) / 2, 2), abs=0.01)


def test_a_big_split_between_the_sources_is_flagged(league_env, monkeypatch):
    settings, key, _ = league_env
    monkeypatch.setattr(
        tools, "_safe_sleeper", lambda season, week: fake_sleeper(**{"BenchStud-RB": 400})
    )
    out = tools.get_start_sit_slate(key, 3, settings=settings)

    flagged = [
        r for r in out["optimal_lineup"] + out["bench"]
        if r.get("disagree") is not None
    ]
    assert flagged, "a 40-point gap between the sources must be surfaced"
    assert all(abs(r["disagree"]) >= 3.0 for r in flagged)


def test_agreement_is_not_reported(league_env, monkeypatch):
    """Where the sources agree the consensus already says everything, and a
    `disagree: 0.4` on every row is noise in a 5KB budget.

    The fixture is built per-league rather than with a fixed stat line, because
    an identical stat line does NOT score identically everywhere: gladiator
    prices yardage in buckets, so 60 receiving yards is worth 6 there and 6.0
    under per-yard PPR only by coincidence. Agreement has to be constructed in
    points, not in yards.
    """
    settings, key, _ = league_env

    # First pass: learn what ESPN says for each player in this league.
    monkeypatch.setattr(tools, "_safe_sleeper", lambda season, week: None)
    baseline = tools.get_start_sit_slate(key, 3, settings=settings)
    espn_by_name = {
        r["name"]: r["proj"]
        for r in baseline["optimal_lineup"] + baseline["bench"]
        if r.get("name")
    }

    class Echo:
        """A Sleeper that agrees with ESPN exactly, whatever the scoring."""

        coverage = "all/all"

        def __bool__(self):
            return True

        def line_for(self, name):
            return {"__echo__": espn_by_name.get(name)}

    monkeypatch.setattr(tools, "_safe_sleeper", lambda season, week: Echo())
    monkeypatch.setattr(
        tools, "score_line", lambda line, scoring, position: (line or {}).get("__echo__")
    )

    out = tools.get_start_sit_slate(key, 3, settings=settings)
    reported = [r for r in out["optimal_lineup"] + out["bench"] if r.get("sleeper_proj")]
    assert reported, "the echo fixture should have produced sleeper columns"
    for row in reported:
        assert "disagree" not in row, row
        assert row["proj"] == pytest.approx(row["sleeper_proj"], abs=0.01)


def test_the_optimizer_ranks_on_the_consensus_not_on_espn(league_env, monkeypatch):
    """The load-bearing claim of the whole integration: the lineup is chosen by
    the consensus number, and the reported total is the sum of exactly those
    rows. If the optimizer sorted by one number and the rows printed another,
    every recommendation would be subtly unjustified by its own evidence."""
    settings, key, _ = league_env
    monkeypatch.setattr(
        tools, "_safe_sleeper", lambda season, week: fake_sleeper(**{"BenchStud-RB": 0})
    )
    with_sleeper = tools.get_start_sit_slate(key, 3, settings=settings)

    started = [r for r in with_sleeper["optimal_lineup"] if r.get("name")]
    assert with_sleeper["optimal_projected"] == pytest.approx(
        round(sum(r["proj"] for r in started), 2), abs=0.01
    )

    # No benched player may out-rank a started one on the number being ranked.
    if started and with_sleeper["bench"]:
        assert max(r["proj"] for r in with_sleeper["bench"]) <= max(
            r["proj"] for r in started
        )

    monkeypatch.setattr(tools, "_safe_sleeper", lambda season, week: None)
    espn_only = tools.get_start_sit_slate(key, 3, settings=settings)
    assert with_sleeper["optimal_projected"] < espn_only["optimal_projected"], (
        "zeroing the stud on one side must pull the consensus total down"
    )


def test_the_matchup_margin_uses_the_same_number_as_the_slate(league_env, monkeypatch):
    """If the slate ranked on the consensus and the matchup summed ESPN, the two
    tools would contradict each other inside one brief."""
    settings, key, _ = league_env
    monkeypatch.setattr(
        tools, "_safe_sleeper", lambda season, week: fake_sleeper(**{"Starter0-QB": 100})
    )
    matchup = tools.get_matchup(key, 3, settings=settings)
    assert "error" not in matchup
    assert matchup["me"]["projected"] == pytest.approx(
        round(sum(r["proj"] for r in matchup["me"]["starters"]), 2), abs=0.01
    )


def test_the_extra_columns_stay_inside_the_budget(league_env, monkeypatch):
    settings, key, _ = league_env

    def everything(season, week):
        from ff_assist.projections import SleeperWeek
        from ff_assist.usage import normalize_name

        week_data = SleeperWeek(season=2026, week=3)
        for i in range(20):
            for pos in ("QB", "RB", "WR", "TE", "K", "D/ST"):
                week_data.lines[normalize_name(f"Starter{i}-{pos}")] = {
                    "receiving_yards": 90, "receptions": 7
                }
        week_data.lines[normalize_name("BenchStud-RB")] = {"rushing_yards": 200}
        return week_data

    monkeypatch.setattr(tools, "_safe_sleeper", everything)
    out = tools.get_start_sit_slate(key, 3, settings=settings)
    assert out["projection_basis"].startswith("espn+sleeper")
    assert len(json.dumps(out).encode()) < 5 * 1024


def test_the_basis_reports_coverage_not_just_presence(league_env, monkeypatch):
    """Sleeper drops players whose published total it cannot rebuild, so
    "espn+sleeper" can still mean some rows had one source. A boolean would
    read as full coverage when it is not."""
    settings, key, _ = league_env

    def partial(season, week):
        from ff_assist.projections import SleeperWeek
        from ff_assist.usage import normalize_name

        wk = SleeperWeek(season=2026, week=3)
        wk.lines[normalize_name("BenchStud-RB")] = {"rushing_yards": 100}
        wk.rejected["someoneelse"] = (12.0, 19.4)
        return wk

    monkeypatch.setattr(tools, "_safe_sleeper", partial)
    out = tools.get_start_sit_slate(key, 3, settings=settings)
    assert out["projection_basis"] == "espn+sleeper (1/2)"
