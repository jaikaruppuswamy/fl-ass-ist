"""FastMCP server — stdio transport (Phase 1).

Phase 3 switches this to streamable HTTP with bearer auth and deploys it, at
which point it becomes a Custom Connector visible to chat, Cowork, scheduled
tasks and mobile. Until then it runs locally against the desktop app.

Read-only by design. There is no tool here that writes to ESPN, and there
should never be one: setting lineups through an unofficial API risks the
account for no real gain, since you approve every move anyway.

    uv run --extra mcp python -m ff_assist.server
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from .config import ConfigError, get_settings
from . import tools

log = logging.getLogger("ff_assist.server")


def build_server() -> Any:
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "fastmcp is not installed. Run:  uv sync --extra mcp"
        ) from exc

    mcp = FastMCP(
        name="ff-assist",
        instructions=(
            "Fantasy football analysis across the user's ESPN leagues. Call "
            "list_leagues first to learn the league keys, their scoring formats "
            "and roster shapes — the leagues differ in ways that change advice. "
            "All responses are pre-scored in each league's own rules; you do not "
            "need to convert between scoring formats yourself. Read-only: never "
            "claim a lineup or waiver move has been made."
        ),
    )

    @mcp.tool
    def list_leagues() -> dict[str, Any]:
        """List every configured league with its scoring format, roster shape,
        current week and your team. Call this before the other tools."""
        return tools.list_leagues()

    @mcp.tool
    def get_matchup(league_key: str, week: int | None = None) -> dict[str, Any]:
        """Your lineup against your opponent's for a given week, with a
        projected margin and a rough win probability.

        Args:
            league_key: short league handle from list_leagues, e.g. "gladiator"
            week: NFL week; defaults to the league's current week
        """
        return tools.get_matchup(league_key, week)

    @mcp.tool
    def get_start_sit_slate(league_key: str, week: int | None = None) -> dict[str, Any]:
        """Pre-scored start/sit table for one league: your current lineup, the
        optimal lineup under that league's scoring, and the swaps between them.

        Args:
            league_key: short league handle from list_leagues
            week: NFL week; defaults to the league's current week
        """
        return tools.get_start_sit_slate(league_key, week)

    @mcp.tool
    def get_game_environment(week: int | None = None) -> dict[str, Any]:
        """Vegas implied team totals, spreads and venue for every NFL game in a
        week. Implied team total is the single highest-signal input for a
        player's expected volume. Books price about three weeks ahead, so
        later weeks come back unpriced rather than estimated.

        Args:
            week: NFL week; defaults to week 1
        """
        return tools.get_game_environment(week)

    @mcp.tool
    def get_player_trend(player: str, weeks: int = 6) -> dict[str, Any]:
        """Week-by-week usage for one player — snap share, targets, target
        share, air yards share, carries — plus the direction of travel.
        Usage leads box-score results, so this is information ESPN's
        projection does not contain.

        Args:
            player: player name as it appears on your roster
            weeks: how many recent weeks to return (default 6)
        """
        return tools.get_player_trend(player, weeks)

    @mcp.tool
    def get_defense_vs_position(
        league_key: str, window: int = 4, position: str | None = None
    ) -> dict[str, Any]:
        """Points allowed per game to each position by each NFL defence,
        expressed in one league's own scoring rather than generic PPR — the
        ranking genuinely differs between scoring systems. Rank 1 is the
        softest matchup.

        Args:
            league_key: whose scoring to express the table in
            window: rolling window in weeks; 0 for season-long
            position: optionally limit to QB, RB, WR or TE
        """
        return tools.get_defense_vs_position(league_key, window, position)

    @mcp.tool
    def get_ros_schedule_strength(
        league_key: str, from_week: int | None = None
    ) -> dict[str, Any]:
        """Rest-of-season schedule difficulty for every player on your roster,
        in this league's scoring, with fantasy playoff weeks (15-17) weighted
        double. Higher ros_pts_allowed means an easier remaining schedule.

        Args:
            league_key: short league handle from list_leagues
            from_week: start week; defaults to the league's current week
        """
        return tools.get_ros_schedule_strength(league_key, from_week)

    return mcp


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, get_settings().log_level, logging.INFO)
        if _settings_ok()
        else logging.INFO,
        stream=sys.stderr,  # stdout is the MCP transport — never log to it
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        get_settings()
    except ConfigError as exc:
        print(f"Configuration problem:\n{exc}", file=sys.stderr)
        return 1
    build_server().run()
    return 0


def _settings_ok() -> bool:
    try:
        get_settings()
        return True
    except ConfigError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
