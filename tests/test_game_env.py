"""Game environment: implied totals, the team-code bridge, roof semantics.

The network-touching tests are marked and skipped when nflverse is unreachable,
so the suite stays green offline. Everything about our own arithmetic runs
against an inline fixture.
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist.game_env import (  # noqa: E402
    ESPN_TO_NFLVERSE,
    implied_totals,
    load_week_environment,
    team_environment_map,
    to_espn_team,
    to_nflverse_team,
)

FIXTURE = pl.DataFrame(
    [
        # SEA favoured by 3.5 at home, 44.5 total -> 24.0 / 20.5
        {"season": 2026, "week": 1, "game_id": "2026_01_NE_SEA", "home_team": "SEA",
         "away_team": "NE", "spread_line": 3.5, "total_line": 44.5, "roof": "outdoors",
         "stadium": "Lumen Field", "gameday": "2026-09-10", "gametime": "20:20"},
        # CHI favoured by 2.5 on the road -> CAR 22.0 / CHI 24.5
        {"season": 2026, "week": 1, "game_id": "2026_01_CHI_CAR", "home_team": "CAR",
         "away_team": "CHI", "spread_line": -2.5, "total_line": 46.5, "roof": "outdoors",
         "stadium": "Bank of America Stadium", "gameday": "2026-09-13", "gametime": "13:00"},
        # the two teams whose codes differ between ESPN and nflverse
        {"season": 2026, "week": 1, "game_id": "2026_01_WAS_LA", "home_team": "LA",
         "away_team": "WAS", "spread_line": 1.0, "total_line": 50.0, "roof": "dome",
         "stadium": "SoFi Stadium", "gameday": "2026-09-13", "gametime": "16:05"},
        # retractable roof, state not yet known
        {"season": 2026, "week": 1, "game_id": "2026_01_NYG_DAL", "home_team": "DAL",
         "away_team": "NYG", "spread_line": 6.0, "total_line": 42.0, "roof": None,
         "stadium": "AT&T Stadium", "gameday": "2026-09-13", "gametime": "16:25"},
        # odds not posted yet
        {"season": 2026, "week": 18, "game_id": "2026_18_X_Y", "home_team": "BUF",
         "away_team": "MIA", "spread_line": None, "total_line": None, "roof": "outdoors",
         "stadium": "Highmark Stadium", "gameday": "2027-01-03", "gametime": "13:00"},
    ]
)


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------


def test_implied_totals_split_the_line_around_the_spread():
    assert implied_totals(3.5, 44.5) == (24.0, 20.5)
    assert implied_totals(-2.5, 46.5) == (22.0, 24.5)
    assert implied_totals(0.0, 48.0) == (24.0, 24.0)


def test_implied_totals_always_sum_to_the_game_total():
    for spread, total in [(3.5, 44.5), (-7.0, 51.0), (0.0, 40.0), (14.5, 55.5)]:
        home, away = implied_totals(spread, total)
        assert round(home + away, 2) == total


def test_favourite_always_gets_the_larger_implied_total():
    home, away = implied_totals(7.0, 45.0)  # home favoured
    assert home > away
    home, away = implied_totals(-7.0, 45.0)  # away favoured
    assert away > home


def test_missing_odds_yield_none_not_zero():
    """A None implied total means 'not posted'. Zero would read as 'this team
    is expected to be shut out', which is a very different claim."""
    assert implied_totals(None, 44.5) == (None, None)
    assert implied_totals(3.5, None) == (None, None)


# ---------------------------------------------------------------------------
# The team-code bridge
# ---------------------------------------------------------------------------


def test_team_code_bridge_round_trips():
    for espn, nflverse in ESPN_TO_NFLVERSE.items():
        assert to_nflverse_team(espn) == nflverse
        assert to_espn_team(nflverse) == espn


def test_unmapped_codes_pass_through_untouched():
    assert to_nflverse_team("BUF") == "BUF"
    assert to_espn_team("BUF") == "BUF"
    assert to_nflverse_team(None) is None


def test_rams_and_commanders_resolve_in_the_environment_map():
    """The regression this bridge exists for: without it these two teams are
    absent from every lookup, with no error raised."""
    env = team_environment_map(2026, 1, FIXTURE)
    assert "LAR" in env, "Rams missing — ESPN says LAR, nflverse says LA"
    assert "WSH" in env, "Commanders missing — ESPN says WSH, nflverse says WAS"
    assert env["LAR"]["opp"] == "WSH"
    assert env["WSH"]["opp"] == "LAR"


# ---------------------------------------------------------------------------
# Environment map
# ---------------------------------------------------------------------------


def test_environment_map_is_consistent_from_both_sides():
    env = team_environment_map(2026, 1, FIXTURE)
    assert env["SEA"]["implied_total"] == 24.0
    assert env["NE"]["implied_total"] == 20.5
    assert env["SEA"]["home"] is True
    assert env["NE"]["home"] is False
    # spread is expressed from each team's own perspective
    assert env["SEA"]["spread"] == 3.5
    assert env["NE"]["spread"] == -3.5


def test_road_favourite_keeps_its_favourite_status():
    env = team_environment_map(2026, 1, FIXTURE)
    assert env["CHI"]["spread"] == 2.5
    assert env["CHI"]["implied_total"] > env["CAR"]["implied_total"]


def test_bye_teams_are_simply_absent():
    env = team_environment_map(2026, 1, FIXTURE)
    assert "GB" not in env


def test_weather_relevance_by_roof():
    games = {g.home: g for g in load_week_environment(2026, 1, FIXTURE)}
    assert games["SEA"].weather_relevant is True  # outdoors
    assert games["LA"].weather_relevant is False  # dome
    assert games["DAL"].weather_relevant is None  # retractable, undecided


def test_unknown_roof_is_none_not_false():
    """A retractable whose state isn't set must not be treated as 'indoors'.
    False would skip the weather check on a game that may well be open."""
    env = team_environment_map(2026, 1, FIXTURE)
    assert env["DAL"]["weather_relevant"] is None
    assert env["LAR"]["weather_relevant"] is False


def test_games_without_odds_still_appear():
    env = team_environment_map(2026, 18, FIXTURE)
    assert env["BUF"]["implied_total"] is None
    assert env["BUF"]["opp"] == "MIA"


# ---------------------------------------------------------------------------
# Live nflverse — skipped when offline
# ---------------------------------------------------------------------------


def _schedules():
    try:
        import nflreadpy as nfl

        return nfl.load_schedules()
    except Exception:  # noqa: BLE001
        return None


_SCHED = _schedules()
needs_net = pytest.mark.skipif(_SCHED is None, reason="nflverse unreachable")


@needs_net
def test_all_32_espn_team_codes_resolve_against_real_schedule():
    from espn_api.football.constant import PRO_TEAM_MAP

    espn_codes = {v for k, v in PRO_TEAM_MAP.items() if isinstance(k, int) and v != "None"}
    env = team_environment_map(2026, 1, _SCHED)
    # 32 teams, some on bye in any given week — check across the first 3 weeks
    seen: set[str] = set()
    for wk in (1, 2, 3):
        seen |= set(team_environment_map(2026, wk, _SCHED))
    missing = espn_codes - seen
    assert not missing, f"ESPN team codes with no nflverse match: {sorted(missing)}"


@needs_net
def test_spread_line_is_from_the_home_perspective():
    """Guards the sign convention the implied-total maths depends on. If
    nflverse ever flipped it, every favourite would become an underdog."""
    done = _SCHED.filter((pl.col("season") == 2025) & pl.col("home_score").is_not_null())
    done = done.with_columns((pl.col("home_score") - pl.col("away_score")).alias("margin"))
    assert done.select(pl.corr("spread_line", "margin")).item() > 0.3
    bias = done.select((pl.col("margin") - pl.col("spread_line")).mean()).item()
    assert abs(bias) < 3.0, f"spread looks biased by {bias:.2f} points"


@needs_net
def test_odds_exist_for_near_weeks_only():
    """Books post roughly three weeks ahead, so implied totals are a near-term
    signal by nature. Anything rest-of-season (SoS, playoff-week planning) has
    to lean on defensive quality instead — the number simply isn't there yet.
    A completed season fills in fully, which is why 2025 is the control.
    """
    games = load_week_environment(2026, None, _SCHED)
    assert len(games) > 250

    priced_by_week: dict[int, float] = {}
    for wk in range(1, 19):
        wk_games = [g for g in games if g.week == wk]
        if wk_games:
            priced_by_week[wk] = sum(g.implied_home is not None for g in wk_games) / len(wk_games)

    assert priced_by_week[1] == 1.0, "week 1 should always be priced"
    assert priced_by_week[17] < 0.5, "late weeks are not priced this far out"

    done = _SCHED.filter(pl.col("season") == 2025)
    assert done["spread_line"].is_not_null().mean() > 0.99, "a finished season should be fully priced"


@needs_net
def test_published_implied_totals_are_physically_plausible():
    for g in load_week_environment(2026, None, _SCHED):
        if g.implied_home is None:
            continue
        assert 5.0 < g.implied_home < 45.0, f"{g.game_id} implied {g.implied_home}"
        assert 5.0 < g.implied_away < 45.0, f"{g.game_id} implied {g.implied_away}"


@needs_net
def test_unpriced_weeks_degrade_to_none_without_error():
    env = team_environment_map(2026, 17, _SCHED)
    assert env, "week 17 fixtures should still be listed"
    assert all(v["implied_total"] is None for v in env.values())
    assert all(v["opp"] for v in env.values()), "opponents are known even unpriced"


@needs_net
def test_game_environment_tool_stays_within_budget():
    import pathlib
    import tempfile

    from ff_assist import tools
    from ff_assist.config import Settings

    st = Settings(
        espn_s2="x" * 300,
        swid="{00000000-0000-0000-0000-000000000001}",
        season=2026,
        leagues=(),
        cache_dir=pathlib.Path(tempfile.mkdtemp()),
        log_level="INFO",
        mcp_bearer_token="",
        fantasypros_api_key="",
    )
    import json

    out = tools.get_game_environment(1, settings=st)
    assert out["priced"] == "16/16"
    assert len(out["games"]) == 16
    assert len(json.dumps(out).encode()) < 5 * 1024
    # a week the books have not reached yet must not fabricate numbers
    late = tools.get_game_environment(17, settings=st)
    assert late["priced"].startswith("0/")
    assert all(g["implied_home"] is None for g in late["games"])


@needs_net
def test_game_environment_names_teams_in_espn_codes():
    """Every other tool speaks ESPN codes. If this one emitted nflverse's LA
    and WAS, a model asked about LAR or WSH would find nothing."""
    import pathlib
    import tempfile

    from ff_assist import tools
    from ff_assist.config import Settings

    st = Settings(
        espn_s2="x" * 300,
        swid="{1A2B3C4D-5E6F-7081-92A3-B4C5D6E7F809}",
        season=2026,
        leagues=(),
        cache_dir=pathlib.Path(tempfile.mkdtemp()),
        log_level="INFO",
        mcp_bearer_token="",
        fantasypros_api_key="",
    )
    seen: set[str] = set()
    for week in (1, 2, 3):
        for game in tools.get_game_environment(week, settings=st)["games"]:
            seen.update(game["matchup"].split("@"))
    assert "LAR" in seen and "LA" not in seen, "Rams must be LAR, not nflverse's LA"
    assert "WSH" in seen and "WAS" not in seen, "Commanders must be WSH, not WAS"
