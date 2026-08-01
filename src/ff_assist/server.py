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
import os
import sys
from typing import Any

from . import tools
from .config import ConfigError, get_settings
from .datastore import DataStore, set_store

log = logging.getLogger("ff_assist.server")


def build_server(*, require_auth: bool = False) -> Any:
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("fastmcp is not installed. Run:  uv sync --extra mcp") from exc

    settings = get_settings()

    auth = None
    middleware = []
    if require_auth:
        import asyncio

        from .auth import (
            GitHubUserAllowlist,
            RateLimiter,
            build_github_auth,
            seed_claude_clients,
        )

        auth = build_github_auth(settings)
        # Claude sends a CIMD client_id and expects us to fetch the metadata
        # document from claude.ai. Cloudflare answers 403 to that fetch from a
        # datacenter address, so we register the client ourselves instead.
        try:
            asyncio.run(seed_claude_clients(auth))
        except Exception as exc:  # noqa: BLE001 — never block startup on this
            log.warning("could not pre-seed Claude OAuth clients: %s", exc)
        # Order matters: the allowlist must run before anything else, so an
        # unauthorised GitHub account cannot even enumerate the tools.
        middleware.append(GitHubUserAllowlist(set(settings.allowed_github_users)))
        middleware.append(RateLimiter())

    mcp = FastMCP(
        auth=auth,
        middleware=middleware or None,
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
    def health(probe_external: bool = False) -> dict[str, Any]:
        """Server status: configured leagues, and whether the nflverse frames
        are warm. Useful as the first call in a scheduled task, so a broken
        deploy surfaces immediately rather than as a confusing empty brief.

        Args:
            probe_external: also check Open-Meteo and Sleeper reachability
                *from the server*, which is a different network from your
                laptop. Adds a couple of seconds; leave it off for the
                routine check and turn it on when you want to know whether
                the weather and waiver-trending overlays actually work here.
        """
        from .datastore import get_store

        store = get_store()
        out: dict[str, Any] = {
            "ok": True,
            "season": settings.season,
            "leagues": list(settings.league_keys),
            "datastore": store.status() if store else "not warmed (stdio mode)",
        }
        if probe_external:
            out["external"] = _probe_external()
        return out

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

    @mcp.tool
    def get_waiver_board(
        league_key: str, top_n: int = 20, week: int | None = None
    ) -> dict[str, Any]:
        """Free agents worth claiming, ranked against what this roster is
        actually short of, with a suggested FAAB band per claim and drop
        candidates from the bench. Waiver accuracy is the second-largest
        source of edge in this system, above start/sit.

        Args:
            league_key: short league handle from list_leagues
            top_n: how many claims to return (default 20)
            week: NFL week; defaults to the league's current week
        """
        return tools.get_waiver_board(league_key, top_n, week)

    return mcp


def _probe_external() -> dict[str, Any]:
    """Reachability of the two optional data sources, from this host.

    Neither is load-bearing — weather is a tie-breaker and the Sleeper overlay
    only gauges how contested a waiver claim is, and both degrade to absent.
    But "absent because it is quiet" and "absent because it is blocked" look
    identical in a brief, so it is worth being able to ask.
    """
    import time

    import httpx

    from .weather import OPEN_METEO_URL

    results: dict[str, Any] = {}

    for label, url, params in (
        ("open_meteo", OPEN_METEO_URL, {"latitude": 42.77, "longitude": -78.79, "hourly": "wind_speed_10m"}),
        ("sleeper", "https://api.sleeper.app/v1/players/nfl/trending/add", {"limit": 5}),
    ):
        started = time.monotonic()
        try:
            response = httpx.get(url, params=params, timeout=15)
            results[label] = {
                "status": response.status_code,
                "ms": round((time.monotonic() - started) * 1000),
                "ok": response.status_code == 200,
            }
        except Exception as exc:  # noqa: BLE001
            results[label] = {"ok": False, "error": type(exc).__name__}

    # The id crosswalk is the fragile hop — a different host from the nflverse
    # releases, and one that has answered 403 from datacenter networks.
    try:
        from .waivers import trending_adds

        started = time.monotonic()
        names = trending_adds(limit=10)
        results["trending_overlay"] = {
            "ok": bool(names),
            "resolved": len(names),
            "ms": round((time.monotonic() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001
        results["trending_overlay"] = {"ok": False, "error": type(exc).__name__}

    return results


def main() -> int:
    """Entry point for both transports.

    stdio  — local development against the desktop app (the default).
    http   — the deployed server. Requires FF_MCP_BEARER_TOKEN and warms the
             nflverse frames at boot so the first real request is fast.

    Selected by FF_TRANSPORT, or --http. PORT is honoured because every PaaS
    injects it.
    """
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,  # stdout is the stdio transport — never log to it
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"Configuration problem:\n{exc}", file=sys.stderr)
        return 1

    logging.getLogger().setLevel(getattr(logging, settings.log_level, logging.INFO))

    transport = os.environ.get("FF_TRANSPORT", "stdio").lower()
    if "--http" in sys.argv:
        transport = "http"

    if transport != "http":
        build_server().run()
        return 0

    store = DataStore(settings.season)
    set_store(store)
    store.warm()

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    log.info("serving http on %s:%s", host, port)

    try:
        server = build_server(require_auth=True)
    except Exception as exc:  # noqa: BLE001 — a weak token lands here
        print(f"Refusing to start: {exc}", file=sys.stderr)
        return 1

    server.run(transport="http", host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
