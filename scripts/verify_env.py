#!/usr/bin/env python3
"""Offline .env check — no network, no ESPN calls.

Run this first after filling in .env:

    uv run scripts/verify_env.py

It reports every problem at once and never prints an unmasked secret.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist.config import ConfigError, env_path, load_settings, mask  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
OK, BAD, WARN = f"{GREEN}✓{RESET}", f"{RED}✗{RESET}", f"{YELLOW}!{RESET}"


def main() -> int:
    path = env_path()
    print(f"{DIM}reading {path}{RESET}\n")

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"{BAD} {exc}\n")
        print(f"{DIM}Fix the items above, then re-run.{RESET}")
        return 1

    print(f"{OK} ESPN_S2            {mask(settings.espn_s2)}")
    print(f"{OK} ESPN_SWID          {mask(settings.swid)}")
    print(f"{OK} FF_SEASON          {settings.season}")
    print(f"{OK} FF_CACHE_DIR       {settings.cache_dir}")
    print(f"{OK} FF_LOG_LEVEL       {settings.log_level}")

    token = settings.mcp_bearer_token
    marker = OK if token else WARN
    note = "" if token else f"  {DIM}(not needed until Phase 3){RESET}"
    print(f"{marker} FF_MCP_BEARER_TOKEN {mask(token)}{note}")

    print(f"\n{OK} {len(settings.leagues)} league(s) configured:")
    for lg in settings.leagues:
        team = lg.team_id if lg.team_id is not None else f"{YELLOW}not set{RESET}"
        label = f"  {DIM}{lg.label}{RESET}" if lg.label else ""
        print(f"    {lg.key:<12} id={lg.league_id:<12} team_id={team}{label}")

    missing_team = [lg.key for lg in settings.leagues if lg.team_id is None]
    if missing_team:
        print(
            f"\n{WARN} No TEAM_ID for: {', '.join(missing_team)}. "
            "Optional, but it removes guesswork about which roster is yours."
        )

    print(f"\n{GREEN}Env looks good.{RESET} Next: {DIM}uv run scripts/verify_leagues.py{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
