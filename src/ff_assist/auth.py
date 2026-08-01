"""Authentication for the remote server.

Claude's custom-connector UI accepts **OAuth only** — there is no field for a
bearer token or a custom header, and the request for one
(anthropics/claude-ai-mcp#112) is closed as not planned. Claude Desktop, Code
and Cursor can send headers via their config files, but the connector path is
what reaches Cowork, scheduled tasks and mobile, so OAuth it is.

Rather than implement an OAuth server, we proxy to GitHub via FastMCP's
built-in provider: Claude gets the OAuth dance it expects, GitHub does the
actual authentication, and no password ever touches this codebase.

Two things the stock provider does not do, both handled here:

**Restricting who gets in.** GitHubProvider authenticates *any* GitHub account.
On a public URL that is an open door, so :class:`GitHubUserAllowlist` rejects
every login that is not explicitly permitted.

**Surviving a restart.** ``fly.toml`` scales to zero. With in-memory client
registrations and a random signing key, every cold start would silently
invalidate the connector and the next scheduled task would fail to auth. Client
registrations persist to the mounted volume and the signing key comes from a
secret, so a restart is invisible.

Rate limiting is not about attackers — OAuth handles those. It is about a
runaway agent loop hammering ESPN and getting the account throttled on a
Sunday morning.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

from fastmcp.server.middleware import Middleware
from pydantic import AnyUrl

__all__ = [
    "GitHubUserAllowlist",
    "RateLimiter",
    "build_github_auth",
    "seed_claude_clients",
    "AuthConfigError",
]

log = logging.getLogger("ff_assist.auth")


class AuthConfigError(RuntimeError):
    """Raised at startup rather than serving traffic misconfigured."""


class GitHubUserAllowlist(Middleware):
    """Reject authenticated GitHub users who are not on the list.

    GitHub proves *who* someone is; it says nothing about whether they should
    see your leagues. Without this, anyone with a GitHub account who found the
    URL could complete the OAuth flow and read everything.
    """

    def __init__(self, allowed_logins: set[str]) -> None:
        # Normalize first, then validate. Checking the raw input would accept
        # `FF_ALLOWED_GITHUB_USERS="  "` or a stray trailing comma, leaving an
        # allowlist that is empty in practice. That fails closed — nobody gets
        # in — but it boots looking healthy and locks you out of your own
        # server with no clue why. Refuse instead.
        self.allowed = {login.strip().lower() for login in allowed_logins if login.strip()}
        log.info("GitHub allowlist active for: %s", ", ".join(sorted(self.allowed)) or "(none)")
        if not self.allowed:
            raise AuthConfigError(
                "FF_ALLOWED_GITHUB_USERS resolved to an empty list. Set it to your "
                "GitHub username (comma-separated for several). Left empty, this "
                "server would either admit every GitHub account or none at all — "
                "neither is what you want."
            )

    def _check(self) -> None:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
        if token is None:
            raise RuntimeError("Not authenticated.")
        login = (token.claims or {}).get("login")
        if not login or login.lower() not in self.allowed:
            # Log both sides. GitHub usernames are public, and a denial that
            # only names the rejected user leaves you guessing whether the
            # secret is wrong, unset, or holding a placeholder — which is
            # exactly the wrong thing to debug blind at 9am on a Sunday.
            log.warning(
                "denied GitHub user %r — FF_ALLOWED_GITHUB_USERS currently holds [%s]",
                login,
                ", ".join(sorted(self.allowed)),
            )
            raise RuntimeError(
                f"GitHub account {login!r} is not authorised. The server's allowlist "
                f"contains: {', '.join(sorted(self.allowed))}. "
                f"Fix with: fly secrets set FF_ALLOWED_GITHUB_USERS={login}"
            )

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        self._check()
        return await call_next(context)

    async def on_list_tools(self, context: Any, call_next: Any) -> Any:
        # Also gate discovery — an unauthorised user should not even learn the
        # tool surface, let alone the league names in the descriptions.
        self._check()
        return await call_next(context)


class RateLimiter(Middleware):
    """Sliding-window cap on tool calls, protecting the upstreams we don't own.

    ESPN publishes no limits but will throttle, and being throttled at 11:45 on
    a Sunday is the worst possible time for it.
    """

    def __init__(self, max_calls: int = 60, window_seconds: int = 60) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._calls: deque[float] = deque()

    def _allow(self) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            return False
        self._calls.append(now)
        return True

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        if not self._allow():
            log.warning("rate limit hit: %s calls in %ss", self.max_calls, self.window_seconds)
            raise RuntimeError(
                f"Rate limit: more than {self.max_calls} tool calls in "
                f"{self.window_seconds}s. This guards against a runaway loop "
                f"getting the ESPN account throttled. Wait a moment and retry."
            )
        return await call_next(context)


def build_github_auth(settings: Any) -> Any:
    """Construct the GitHub OAuth provider from settings.

    Raises :class:`AuthConfigError` with an actionable message rather than
    starting up half-configured.
    """
    missing = [
        name
        for name, value in (
            ("FF_GITHUB_CLIENT_ID", settings.github_client_id),
            ("FF_GITHUB_CLIENT_SECRET", settings.github_client_secret),
            ("FF_PUBLIC_URL", settings.public_url),
        )
        if not value
    ]
    if missing:
        raise AuthConfigError(
            "Missing OAuth configuration: "
            + ", ".join(missing)
            + "\n  See DEPLOY.md > 'GitHub OAuth app'. Claude's connector UI accepts\n"
            "  OAuth only, so these are required for the deployed server."
        )

    from fastmcp.server.auth.providers.github import GitHubProvider

    kwargs: dict[str, Any] = {
        "client_id": settings.github_client_id,
        "client_secret": settings.github_client_secret,
        "base_url": settings.public_url,
    }

    # A stable signing key means access tokens issued before a cold start are
    # still valid after it. Without this the connector silently breaks every
    # time the machine suspends.
    if settings.jwt_signing_key:
        kwargs["jwt_signing_key"] = settings.jwt_signing_key
    else:
        log.warning(
            "FF_JWT_SIGNING_KEY is unset — tokens are signed with an ephemeral key "
            "and will be invalidated on every restart. Set it before relying on "
            "scheduled tasks."
        )

    storage = _client_storage(settings)
    if storage is not None:
        kwargs["client_storage"] = storage

    return GitHubProvider(**kwargs)


def _client_storage(settings: Any) -> Any | None:
    """Persist OAuth client registrations to the mounted volume.

    Claude registers itself dynamically once. If that registration lives only
    in memory, the first cold start forgets it and the connector has to be
    re-added by hand.
    """
    try:
        from key_value.aio.stores.disk import DiskStore
    except ImportError:
        log.warning(
            "py-key-value-aio not installed — OAuth client registrations will be "
            "in-memory and lost on restart."
        )
        return None

    path = settings.cache_dir / "oauth-clients"
    try:
        path.mkdir(parents=True, exist_ok=True)
        return DiskStore(directory=str(path))
    except OSError as exc:
        log.warning("could not open OAuth client storage at %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Pre-seeding Claude's OAuth client
# ---------------------------------------------------------------------------

#: Claude does not use Dynamic Client Registration. It sends a Client ID
#: Metadata Document URL as the client_id and expects the server to fetch it.
CLAUDE_CIMD_CLIENT_IDS = (
    "https://claude.ai/oauth/mcp-oauth-client-metadata",
    "https://claude.com/oauth/mcp-oauth-client-metadata",
)

#: Where Claude expects the authorization code delivered.
CLAUDE_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)

#: Patterns the proxy will accept as redirect targets. Kept tight — a loose
#: pattern here is how an open redirect becomes a token-stealing bug.
CLAUDE_REDIRECT_PATTERNS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:*",  # Claude Code / Desktop local loopback flows
    "http://127.0.0.1:*",
)


async def seed_claude_clients(provider: Any) -> list[str]:
    """Register Claude's CIMD client ids directly, skipping the metadata fetch.

    Why this exists: FastMCP resolves a CIMD client_id by fetching the document
    from claude.ai. That fetch is SSRF-hardened — it pins DNS and requests the
    literal IP with `Host` and SNI set to claude.ai — and Cloudflare in front of
    claude.ai answers **403** to it from a Fly datacenter address. The fetch
    fails, `get_client` returns None, and `/authorize` rejects the request as an
    unregistered client. The error names the client id, which makes it look like
    a configuration problem; it is a blocked outbound request.

    ``OAuthProxy.get_client`` consults its client store *before* attempting any
    CIMD lookup, and only refreshes over the network when the stored record
    carries a ``cimd_document``. Seeding a record without one means the fetch is
    never attempted, so the 403 stops mattering.

    We are not inventing trust here. The redirect URIs are pinned to Claude's
    own callbacks, so an authorization code can only ever be delivered to
    Claude — which is exactly what the CIMD document would have told us.

    Safe to call on every boot; it overwrites its own entries and nothing else.
    """
    from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient

    store = getattr(provider, "_client_store", None)
    if store is None:
        log.warning("provider has no client store — cannot seed Claude clients")
        return []

    seeded: list[str] = []
    for client_id in CLAUDE_CIMD_CLIENT_IDS:
        try:
            existing = await store.get(key=client_id)
            if existing is not None and getattr(existing, "cimd_document", None) is None:
                seeded.append(client_id)
                continue  # already seeded by a previous boot

            client = ProxyDCRClient(
                client_id=client_id,
                client_secret=None,
                redirect_uris=[AnyUrl(u) for u in CLAUDE_REDIRECT_URIS],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",  # public client, uses PKCE
                scope="user",
                client_name="Claude",
                allowed_redirect_uri_patterns=list(CLAUDE_REDIRECT_PATTERNS),
                cimd_document=None,  # <- the point: never triggers a refetch
            )
            await store.put(key=client_id, value=client)
            seeded.append(client_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not seed client %s: %s", client_id, exc)

    if seeded:
        log.info("seeded %d Claude OAuth client id(s), CIMD fetch not required", len(seeded))
    return seeded
