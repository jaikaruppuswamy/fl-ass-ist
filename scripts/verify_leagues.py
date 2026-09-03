#!/usr/bin/env python3
"""Phase 0 smoke test — proves the cookies work and all leagues read cleanly.

    uv run scripts/verify_leagues.py

This is also the check to wire into the Tuesday scheduled task later, so an
ESPN breakage or cookie expiry surfaces on Tuesday rather than ten minutes
before Sunday lock.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist.config import ConfigError, load_settings  # noqa: E402
from ff_assist.espn_client import ESPNError, get_league, my_team  # noqa: E402

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
OK, BAD = f"{GREEN}✓{RESET}", f"{RED}✗{RESET}"


def print_team_menu(lg, cfg) -> None:
    """List the teams so the fix is on screen, not in the ESPN app."""
    try:
        pairs = sorted((t.team_id, t.team_name) for t in lg.teams)
    except Exception as exc:  # noqa: BLE001
        print(f"    {DIM}could not list teams: {type(exc).__name__}: {exc}{RESET}")
        return
    if not pairs:
        print(f"    {DIM}this league reports no teams{RESET}")
        return
    var = f"FF_LEAGUE_{cfg.key.upper()}_TEAM_ID"
    print(f"    {DIM}teams in this league — set {var} to the id of yours:{RESET}")
    for tid, name in pairs:
        print(f"      {tid:>3}  {name}")


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"{BAD} {exc}")
        return 1

    print(f"{DIM}season {settings.season} · {len(settings.leagues)} league(s){RESET}\n")
    failures = 0

    for cfg in settings.leagues:
        try:
            lg = get_league(cfg, settings=settings)
        except ESPNError as exc:
            failures += 1
            print(f"{BAD} {cfg.key}")
            for line in str(exc).splitlines():
                print(f"    {line}")
            print()
            continue
        except Exception as exc:  # never let one league take down the whole run
            failures += 1
            print(f"{BAD} {cfg.key}")
            print(f"    unexpected {type(exc).__name__}: {exc}")
            print()
            continue

        name = getattr(lg.settings, "name", "?")
        scoring = getattr(lg.settings, "scoring_type", "?")
        print(f"{OK} {cfg.key:<12} {name}")
        print(f"    week {lg.current_week} · {len(lg.teams)} teams · scoring: {scoring}")

        team = my_team(lg, cfg)
        if team is not None:
            record = f"{team.wins}-{team.losses}"
            if getattr(team, "ties", 0):
                record += f"-{team.ties}"
            print(f"    your team: {team.team_name} ({record}) · {len(team.roster)} players")
            print(
                f"    {DIM}confirm that name is yours — a stale team id can collide with a"
                f" real team and read a stranger's roster without erroring{RESET}"
            )
        else:
            # Both branches print the roster of teams. Learning that your id is
            # wrong is only half an answer; the other half is the number to put
            # in its place, and this script is already holding it.
            if cfg.team_id is None:
                print(f"    {RED}FF_LEAGUE_{cfg.key.upper()}_TEAM_ID is not set{RESET}")
            else:
                failures += 1
                print(
                    f"    {RED}no team with team_id={cfg.team_id} in this league{RESET}"
                    f" {DIM}(a recreated league hands out fresh team ids){RESET}"
                )
            print_team_menu(lg, cfg)
        print()

    if failures:
        print(f"{BAD} {failures} league(s) failed.")
        return 1

    print(f"{GREEN}All leagues read cleanly. Phase 0 data access confirmed.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
