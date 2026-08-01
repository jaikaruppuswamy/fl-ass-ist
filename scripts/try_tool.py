#!/usr/bin/env python3
"""Call the MCP tools straight from the terminal — no Claude restart needed.

    uv run scripts/try_tool.py list_leagues
    uv run scripts/try_tool.py get_matchup gladiator
    uv run scripts/try_tool.py get_start_sit_slate inai 3
    uv run scripts/try_tool.py all                 # every tool, every league

Prints the JSON a model would receive, plus its size. The plan's rule is that
a response over ~5KB per league isn't aggregating enough, so the size is part
of the output rather than something you have to go measuring.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist import tools  # noqa: E402
from ff_assist.config import ConfigError, load_settings  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
BUDGET = 5 * 1024

TOOLS = {
    "list_leagues": tools.list_leagues,
    "get_matchup": tools.get_matchup,
    "get_start_sit_slate": tools.get_start_sit_slate,
}


def show(name: str, result: object, elapsed: float) -> int:
    blob = json.dumps(result, indent=2, default=str)
    size = len(blob.encode())
    over = size > BUDGET
    colour = YELLOW if over else GREEN
    flag = "  <-- over the 5KB budget, aggregate harder" if over else ""
    print(blob)
    print(f"\n{colour}{name}: {size:,} bytes in {elapsed * 1000:.0f} ms{flag}{RESET}\n")
    return size


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"{RED}{exc}{RESET}")
        return 1

    name, args = argv[0], argv[1:]

    if name == "all":
        t = time.perf_counter()
        show("list_leagues", tools.list_leagues(), time.perf_counter() - t)
        for cfg in settings.leagues:
            for tool_name in ("get_matchup", "get_start_sit_slate"):
                t = time.perf_counter()
                try:
                    result = TOOLS[tool_name](cfg.key)
                except Exception as exc:  # noqa: BLE001
                    print(f"{RED}{tool_name}({cfg.key}): {type(exc).__name__}: {exc}{RESET}\n")
                    continue
                show(f"{tool_name}({cfg.key})", result, time.perf_counter() - t)
        return 0

    if name not in TOOLS:
        print(f"{RED}Unknown tool {name!r}.{RESET} Known: {', '.join(TOOLS)}, all")
        return 1

    kwargs: dict[str, object] = {}
    if args:
        kwargs["league_key"] = args[0]
    if len(args) > 1:
        kwargs["week"] = int(args[1])

    t = time.perf_counter()
    show(name, TOOLS[name](**kwargs), time.perf_counter() - t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
