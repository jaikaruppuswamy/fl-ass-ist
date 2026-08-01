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
