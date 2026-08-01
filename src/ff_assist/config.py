"""Configuration and secret loading for fl-ass-ist.

Everything secret lives in ``.env`` at the repo root, which is gitignored.
Nothing in this module ever returns or logs an unmasked cookie — use
:func:`mask` whenever a value needs to appear in output.

Typical use::

    from ff_assist.config import load_settings

    settings = load_settings()
    for lg in settings.leagues:
        print(lg.key, lg.league_id)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

__all__ = [
    "ConfigError",
    "LeagueConfig",
    "Settings",
    "find_repo_root",
    "load_settings",
    "mask",
]

# A SWID looks like {1A2B3C4D-5E6F-7081-92A3-B4C5D6E7F809}
_SWID_RE = re.compile(
    r"^\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}$"
)
_PLACEHOLDER_SWID = "{00000000-0000-0000-0000-000000000000}"
_KEY_RE = re.compile(r"^[a-z0-9_]+$")

# espn_s2 is long. Anything much shorter than this is a truncated copy/paste.
_MIN_S2_LEN = 100


class ConfigError(RuntimeError):
    """Raised when .env is missing, incomplete, or malformed."""


# ---------------------------------------------------------------------------
# Locating the repo root / .env
# ---------------------------------------------------------------------------


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` (or this file) looking for the project root.

    The root is the first directory containing ``pyproject.toml``. Falls back to
    the directory two levels above this module (``src/ff_assist/config.py``).
    """
    candidates = []
    if start is not None:
        candidates.append(Path(start).resolve())
    candidates.append(Path.cwd().resolve())
    candidates.append(Path(__file__).resolve())

    for candidate in candidates:
        for directory in [candidate, *candidate.parents]:
            if (directory / "pyproject.toml").is_file():
                return directory

    return Path(__file__).resolve().parents[2]


def env_path() -> Path:
    """Path to the .env file. Override with the ``FF_ENV_FILE`` env var."""
    override = os.environ.get("FF_ENV_FILE")
    if override:
        return Path(override).expanduser().resolve()
    return find_repo_root() / ".env"


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


def _clean(value: str | None) -> str:
    """Strip whitespace and any stray matching quotes left by hand-editing."""
    if value is None:
        return ""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def _get(name: str) -> str:
    return _clean(os.environ.get(name))


def mask(value: str | None, keep: int = 4) -> str:
    """Render a secret safely for logs and CLI output."""
    value = _clean(value)
    if not value:
        return "<empty>"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-keep:]} ({len(value)} chars)"


def normalize_swid(raw: str) -> str:
    """Ensure the SWID is wrapped in curly braces, as ESPN's cookie expects."""
    swid = _clean(raw)
    if not swid:
        return ""
    if not swid.startswith("{"):
        swid = "{" + swid
    if not swid.endswith("}"):
        swid = swid + "}"
    return swid


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeagueConfig:
    key: str
    league_id: int
    team_id: int | None = None
    label: str = ""

    @property
    def display_name(self) -> str:
        return self.label or self.key


@dataclass(frozen=True)
class Settings:
    espn_s2: str
    swid: str
    season: int
    leagues: tuple[LeagueConfig, ...]
    cache_dir: Path
    log_level: str
    mcp_bearer_token: str
    fantasypros_api_key: str

    @property
    def league_keys(self) -> tuple[str, ...]:
        return tuple(lg.key for lg in self.leagues)

    def league(self, key: str) -> LeagueConfig:
        key = key.strip().lower()
        for lg in self.leagues:
            if lg.key == key:
                return lg
        known = ", ".join(self.league_keys) or "(none)"
        raise KeyError(f"Unknown league key {key!r}. Known keys: {known}")

    def redacted(self) -> dict[str, object]:
        """A dict safe to print or log."""
        return {
            "espn_s2": mask(self.espn_s2),
            "swid": mask(self.swid),
            "season": self.season,
            "leagues": [
                {
                    "key": lg.key,
                    "league_id": lg.league_id,
                    "team_id": lg.team_id,
                    "label": lg.label,
                }
                for lg in self.leagues
            ],
            "cache_dir": str(self.cache_dir),
            "log_level": self.log_level,
            "mcp_bearer_token": mask(self.mcp_bearer_token),
            "fantasypros_api_key": mask(self.fantasypros_api_key),
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _parse_leagues(errors: list[str]) -> tuple[LeagueConfig, ...]:
    raw_keys = _get("FF_LEAGUE_KEYS")
    if not raw_keys:
        errors.append(
            "FF_LEAGUE_KEYS is empty — list your league handles, e.g. 'main,dynasty,work'."
        )
        return ()

    leagues: list[LeagueConfig] = []
    seen: set[str] = set()

    for piece in raw_keys.split(","):
        key = piece.strip().lower().replace("-", "_").replace(" ", "_")
        if not key:
            continue
        if not _KEY_RE.match(key):
            errors.append(
                f"League key {piece.strip()!r} is invalid — "
                "use lowercase letters, digits, underscores."
            )
            continue
        if key in seen:
            errors.append(f"League key {key!r} is listed twice in FF_LEAGUE_KEYS.")
            continue
        seen.add(key)

        prefix = f"FF_LEAGUE_{key.upper()}"
        raw_id = _get(f"{prefix}_ID")
        raw_team = _get(f"{prefix}_TEAM_ID")
        label = _get(f"{prefix}_LABEL")

        if not raw_id:
            errors.append(f"{prefix}_ID is empty — paste the leagueId from that league's ESPN URL.")
            continue
        if not raw_id.isdigit():
            errors.append(f"{prefix}_ID must be digits only, got {raw_id!r}.")
            continue

        team_id: int | None = None
        if raw_team:
            if not raw_team.isdigit():
                errors.append(f"{prefix}_TEAM_ID must be digits only, got {raw_team!r}.")
            else:
                team_id = int(raw_team)

        leagues.append(LeagueConfig(key=key, league_id=int(raw_id), team_id=team_id, label=label))

    return tuple(leagues)


def load_settings(*, require_espn: bool = True, reload: bool = True) -> Settings:
    """Read ``.env`` and return validated :class:`Settings`.

    Args:
        require_espn: when False, missing/placeholder cookies are tolerated.
            Useful for offline tooling that only needs league IDs.
        reload: re-read the .env file, overriding anything already in
            ``os.environ`` from a previous load.

    Raises:
        ConfigError: with every problem listed at once, so you can fix the
            whole file in one pass instead of one error per run.
    """
    path = env_path()
    if not path.is_file():
        raise ConfigError(
            f"No .env file at {path}.\n"
            f"  Create one with:  cp .env.example .env\n"
            f"  Then fill in your ESPN cookies and league IDs."
        )

    load_dotenv(path, override=reload)

    errors: list[str] = []

    espn_s2 = _get("ESPN_S2")
    swid = normalize_swid(_get("ESPN_SWID"))

    if require_espn:
        if not espn_s2:
            errors.append("ESPN_S2 is empty — see README > Getting your ESPN cookies.")
        elif espn_s2.startswith("espn_s2="):
            errors.append("ESPN_S2 includes the 'espn_s2=' prefix — paste only the value.")
        elif len(espn_s2) < _MIN_S2_LEN:
            errors.append(
                f"ESPN_S2 looks truncated ({len(espn_s2)} chars; expected 300+). "
                "Copy the full cookie Value, not what's shown in the narrow column."
            )

        if not swid:
            errors.append("ESPN_SWID is empty — see README > Getting your ESPN cookies.")
        elif swid == _PLACEHOLDER_SWID:
            errors.append("ESPN_SWID is still the placeholder UUID — replace it with your real SWID.")
        elif not _SWID_RE.match(swid):
            errors.append(f"ESPN_SWID doesn't look like {{UUID}}: {swid!r}")

    raw_season = _get("FF_SEASON") or "2026"
    if not raw_season.isdigit() or not (2000 <= int(raw_season) <= 2100):
        errors.append(f"FF_SEASON must be a 4-digit year, got {raw_season!r}.")
        season = 2026
    else:
        season = int(raw_season)

    leagues = _parse_leagues(errors)

    cache_dir = Path(_get("FF_CACHE_DIR") or "data/cache")
    if not cache_dir.is_absolute():
        cache_dir = find_repo_root() / cache_dir

    log_level = (_get("FF_LOG_LEVEL") or "INFO").upper()

    if errors:
        bullets = "\n".join(f"  - {e}" for e in errors)
        raise ConfigError(f"Problems in {path}:\n{bullets}")

    return Settings(
        espn_s2=espn_s2,
        swid=swid,
        season=season,
        leagues=leagues,
        cache_dir=cache_dir,
        log_level=log_level,
        mcp_bearer_token=_get("FF_MCP_BEARER_TOKEN"),
        fantasypros_api_key=_get("FANTASYPROS_API_KEY"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached :func:`load_settings` for long-running processes (the MCP server)."""
    return load_settings()
