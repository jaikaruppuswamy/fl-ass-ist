"""Bearer auth, rate limiting, and the datastore.

The auth tests matter because this is the layer that stands between the open
internet and the ESPN account.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff_assist.auth import AuthConfigError, GitHubUserAllowlist, RateLimiter  # noqa: E402
from ff_assist.datastore import DataStore, get_store, set_store  # noqa: E402


# ---------------------------------------------------------------------------
# GitHub allowlist
#
# GitHub proves who someone is. It says nothing about whether they should see
# your leagues — without this layer, any GitHub account that finds the URL can
# complete the OAuth flow and read everything.
# ---------------------------------------------------------------------------


class FakeToken:
    def __init__(self, login):
        self.claims = {"login": login} if login is not None else {}


def _patch_token(monkeypatch, token):
    import fastmcp.server.dependencies as deps

    monkeypatch.setattr(deps, "get_access_token", lambda: token)


async def _ok(ctx):
    return "called"


def test_allowlist_refuses_to_start_empty():
    """An empty allowlist would authorise every GitHub account on earth."""
    with pytest.raises(AuthConfigError):
        GitHubUserAllowlist(set())
    with pytest.raises(AuthConfigError):
        GitHubUserAllowlist({"  "})


def test_permitted_user_passes(monkeypatch):
    _patch_token(monkeypatch, FakeToken("octocat"))
    guard = GitHubUserAllowlist({"octocat"})
    assert asyncio.run(guard.on_call_tool(None, _ok)) == "called"


def test_username_match_is_case_insensitive(monkeypatch):
    _patch_token(monkeypatch, FakeToken("OctoCat"))
    guard = GitHubUserAllowlist({"octocat"})
    assert asyncio.run(guard.on_call_tool(None, _ok)) == "called"


def test_valid_github_account_not_on_the_list_is_refused(monkeypatch):
    """The case that matters: real GitHub auth, wrong person."""
    _patch_token(monkeypatch, FakeToken("some-stranger"))
    guard = GitHubUserAllowlist({"octocat"})
    with pytest.raises(RuntimeError, match="not authorised"):
        asyncio.run(guard.on_call_tool(None, _ok))


def test_unauthorised_user_cannot_even_list_tools(monkeypatch):
    """Tool descriptions name the leagues, so discovery is gated too."""
    _patch_token(monkeypatch, FakeToken("some-stranger"))
    guard = GitHubUserAllowlist({"octocat"})
    with pytest.raises(RuntimeError):
        asyncio.run(guard.on_list_tools(None, _ok))


def test_missing_token_is_refused(monkeypatch):
    _patch_token(monkeypatch, None)
    guard = GitHubUserAllowlist({"octocat"})
    with pytest.raises(RuntimeError, match="Not authenticated"):
        asyncio.run(guard.on_call_tool(None, _ok))


def test_token_without_a_login_claim_is_refused(monkeypatch):
    _patch_token(monkeypatch, FakeToken(None))
    guard = GitHubUserAllowlist({"octocat"})
    with pytest.raises(RuntimeError):
        asyncio.run(guard.on_call_tool(None, _ok))


def test_oauth_build_reports_every_missing_setting_at_once():
    from ff_assist.auth import build_github_auth

    class Bare:
        github_client_id = ""
        github_client_secret = ""
        public_url = ""

    with pytest.raises(AuthConfigError) as exc:
        build_github_auth(Bare())
    for name in ("FF_GITHUB_CLIENT_ID", "FF_GITHUB_CLIENT_SECRET", "FF_PUBLIC_URL"):
        assert name in str(exc.value)


# --- rate limiting ---------------------------------------------------------


def test_rate_limiter_allows_up_to_the_cap():
    limiter = RateLimiter(max_calls=3, window_seconds=60)
    assert [limiter._allow() for _ in range(3)] == [True, True, True]


def test_rate_limiter_blocks_beyond_the_cap():
    limiter = RateLimiter(max_calls=3, window_seconds=60)
    for _ in range(3):
        limiter._allow()
    assert limiter._allow() is False


def test_rate_limiter_window_slides():
    limiter = RateLimiter(max_calls=2, window_seconds=60)
    limiter._allow()
    limiter._allow()
    assert limiter._allow() is False
    # age the recorded calls out of the window
    limiter._calls = type(limiter._calls)([t - 61 for t in limiter._calls])
    assert limiter._allow() is True


def test_rate_limited_call_raises_with_an_actionable_message():
    limiter = RateLimiter(max_calls=1, window_seconds=60)

    async def call_next(ctx):
        return "ok"

    assert asyncio.run(limiter.on_call_tool(None, call_next)) == "ok"
    with pytest.raises(RuntimeError, match="Rate limit"):
        asyncio.run(limiter.on_call_tool(None, call_next))


# --- datastore -------------------------------------------------------------


class FakeLoader:
    def __init__(self, has_current=True):
        self.calls = []
        self.has_current = has_current

    def load_schedules(self):
        self.calls.append("schedules")
        return "SCHEDULES"

    def load_player_stats(self, seasons):
        self.calls.append(f"stats:{seasons[0]}")
        if seasons[0] == 2026 and not self.has_current:
            raise RuntimeError("404")
        return f"STATS{seasons[0]}"

    def load_snap_counts(self, seasons):
        self.calls.append(f"snaps:{seasons[0]}")
        return f"SNAPS{seasons[0]}"


def test_frames_are_fetched_once_and_reused():
    loader = FakeLoader()
    store = DataStore(2026, loader=loader)
    store.schedules()
    store.schedules()
    store.schedules()
    assert loader.calls.count("schedules") == 1


def test_expired_entries_refetch():
    loader = FakeLoader()
    store = DataStore(2026, refresh_seconds=0, loader=loader)
    store.schedules()
    store.schedules()
    assert loader.calls.count("schedules") == 2


def test_season_resolution_falls_back_before_kickoff():
    """Between February and September the current season does not exist."""
    store = DataStore(2026, loader=FakeLoader(has_current=False))
    assert store.resolve_stats_season() == 2025


def test_season_resolution_is_decided_once():
    loader = FakeLoader(has_current=False)
    store = DataStore(2026, loader=loader)
    store.resolve_stats_season()
    before = len(loader.calls)
    store.resolve_stats_season()
    assert len(loader.calls) == before


def test_warm_reports_what_it_loaded():
    report = DataStore(2026, loader=FakeLoader(has_current=False)).warm()
    assert report["schedules"] == "ok"
    assert report["player_stats"] == "ok"
    assert report["stats_season"] == 2025
    assert "seconds" in report


def test_warm_survives_an_unreachable_source():
    class Broken(FakeLoader):
        def load_schedules(self):
            raise ConnectionError("no network")

    report = DataStore(2026, loader=Broken()).warm()
    assert "failed" in report["schedules"]


def test_store_is_absent_by_default_so_callers_fall_back():
    set_store(None)
    assert get_store() is None


# ---------------------------------------------------------------------------
# End-to-end: a real HTTP server, a real MCP client
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Production config: env vars, no .env file
# ---------------------------------------------------------------------------


def test_settings_load_from_environment_with_no_dotenv_file(tmp_path, monkeypatch):
    """On Fly there is no .env — `fly secrets set` injects the same names as
    environment variables. Requiring the file meant the container could not
    boot at all."""
    from ff_assist.config import get_settings, load_settings

    monkeypatch.setenv("FF_ENV_FILE", str(tmp_path / "does-not-exist.env"))
    monkeypatch.setenv("ESPN_S2", "x" * 320)
    monkeypatch.setenv("ESPN_SWID", "{1A2B3C4D-5E6F-7081-92A3-B4C5D6E7F809}")
    monkeypatch.setenv("FF_SEASON", "2026")
    monkeypatch.setenv("FF_LEAGUE_KEYS", "main")
    monkeypatch.setenv("FF_LEAGUE_MAIN_ID", "1234567")
    monkeypatch.setenv("FF_LEAGUE_MAIN_TEAM_ID", "3")
    get_settings.cache_clear()

    settings = load_settings()
    assert settings.league_keys == ("main",)
    assert settings.league("main").league_id == 1234567
    get_settings.cache_clear()


def test_no_dotenv_and_no_environment_still_errors_clearly(tmp_path, monkeypatch):
    from ff_assist.config import ConfigError, get_settings, load_settings

    monkeypatch.setenv("FF_ENV_FILE", str(tmp_path / "nope.env"))
    for name in ("ESPN_S2", "ESPN_SWID", "FF_LEAGUE_KEYS"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()

    with pytest.raises(ConfigError, match="No configuration found"):
        load_settings()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Pre-seeding Claude's OAuth client
#
# Claude sends a CIMD client_id and expects the server to fetch the metadata
# document from claude.ai. Cloudflare answers 403 to that fetch from a Fly
# datacenter address, `get_client` returns None, and /authorize rejects the
# request as an unregistered client. Seeding the record skips the fetch.
# ---------------------------------------------------------------------------


def _provider(tmp_path):
    from fastmcp.server.auth.providers.github import GitHubProvider
    from key_value.aio.stores.disk import DiskStore

    return GitHubProvider(
        client_id="Ov23liFAKE",
        client_secret="fake",
        base_url="https://example.fly.dev",
        jwt_signing_key="s" * 40,
        client_storage=DiskStore(directory=str(tmp_path / "oauth")),
    )


@pytest.mark.slow
def test_seeded_client_is_returned_without_any_network_fetch(tmp_path, monkeypatch):
    from ff_assist.auth import CLAUDE_CIMD_CLIENT_IDS, seed_claude_clients

    provider = _provider(tmp_path)
    client_id = CLAUDE_CIMD_CLIENT_IDS[0]
    asyncio.run(seed_claude_clients(provider))

    # Make any CIMD fetch explode. A seeded record must not reach for it.
    async def explode(*a, **k):
        raise AssertionError("CIMD fetch attempted despite a seeded client")

    if provider._cimd_manager is not None:
        monkeypatch.setattr(provider._cimd_manager, "get_client", explode)

    client = asyncio.run(provider.get_client(client_id))
    assert client is not None
    assert client.cimd_document is None, "a cimd_document would trigger a refetch"


@pytest.mark.slow
def test_seeded_client_accepts_claudes_callback_and_nothing_else(tmp_path):
    from pydantic import AnyUrl

    from ff_assist.auth import CLAUDE_CIMD_CLIENT_IDS, seed_claude_clients
    from fastmcp.server.auth.oauth_proxy.models import InvalidRedirectUriError

    provider = _provider(tmp_path)
    asyncio.run(seed_claude_clients(provider))
    client = asyncio.run(provider.get_client(CLAUDE_CIMD_CLIENT_IDS[0]))

    assert client.validate_redirect_uri(AnyUrl("https://claude.ai/api/mcp/auth_callback"))
    # An open redirect here is how an authorization code gets stolen.
    for hostile in (
        "https://evil.example.com/steal",
        "https://claude.ai.evil.example.com/steal",
        "http://example.com/cb",
    ):
        with pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl(hostile))


@pytest.mark.slow
def test_seeding_survives_a_restart(tmp_path):
    from ff_assist.auth import CLAUDE_CIMD_CLIENT_IDS, seed_claude_clients

    asyncio.run(seed_claude_clients(_provider(tmp_path)))
    fresh = _provider(tmp_path)  # new process, same volume
    assert asyncio.run(fresh.get_client(CLAUDE_CIMD_CLIENT_IDS[0])) is not None


@pytest.mark.slow
def test_seeding_is_idempotent(tmp_path):
    from ff_assist.auth import seed_claude_clients

    provider = _provider(tmp_path)
    first = asyncio.run(seed_claude_clients(provider))
    second = asyncio.run(seed_claude_clients(provider))
    assert first == second and len(first) == 2


def test_denial_names_the_configured_allowlist_not_just_the_rejected_user(monkeypatch):
    """Debugging a denial requires seeing both sides. GitHub usernames are
    public, so there is nothing to protect by hiding the expected value."""
    _patch_token(monkeypatch, FakeToken("octocat"))
    guard = GitHubUserAllowlist({"<your github username>"})  # the placeholder mistake
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(guard.on_list_tools(None, _ok))
    message = str(exc.value)
    assert "octocat" in message
    assert "<your github username>" in message, "must reveal what the server actually has"
    assert "fly secrets set" in message, "must say how to fix it"


def test_health_probe_reports_each_source_separately(monkeypatch):
    """A brief cannot tell 'quiet' from 'blocked'. The probe has to name which
    source failed, not just say the overlay was empty."""
    import ff_assist.server as srv

    class Resp:
        status_code = 200

    monkeypatch.setattr("httpx.get", lambda *a, **k: Resp())
    monkeypatch.setattr("ff_assist.waivers.trending_adds", lambda **k: {"a": 1, "b": 2})

    out = srv._probe_external()
    assert out["open_meteo"]["ok"] is True
    assert out["sleeper"]["ok"] is True
    assert out["trending_overlay"] == {"ok": True, "resolved": 2, "ms": out["trending_overlay"]["ms"]}


def test_health_probe_names_the_failing_source(monkeypatch):
    import ff_assist.server as srv

    def boom(*a, **k):
        raise TimeoutError("blocked")

    monkeypatch.setattr("httpx.get", boom)
    monkeypatch.setattr("ff_assist.waivers.trending_adds", lambda **k: {})

    out = srv._probe_external()
    assert out["open_meteo"]["ok"] is False
    assert out["open_meteo"]["error"] == "TimeoutError"
    assert out["trending_overlay"]["ok"] is False


def test_probe_is_off_by_default_so_scheduled_health_stays_fast(monkeypatch):
    """health() is the first call in every scheduled task. It must not pay for
    two network round trips unless asked."""
    import ff_assist.server as srv

    called = []
    monkeypatch.setattr(srv, "_probe_external", lambda: called.append(1) or {})
    import inspect

    sig = inspect.signature(srv.build_server)
    assert "require_auth" in sig.parameters
    # the default is encoded in the tool signature; assert it directly
    src = inspect.getsource(srv.build_server)
    assert "probe_external: bool = False" in src
