# Deploying to Fly.io

## Before you start

Replace `<your-app>` throughout with your own Fly app name — app names are
globally unique, so `ff-assist` is probably taken. Set the same name as `app =`
in `fly.toml`, as `FF_PUBLIC_URL`, and in your GitHub OAuth app's callback URL.
All three must agree on the hostname or the OAuth round trip fails.


The result is a Custom Connector usable from chat, Cowork, scheduled tasks and
mobile, with your laptop closed.

## Why OAuth, not a bearer token

Claude's custom-connector UI accepts **OAuth only** — there is no field for a
bearer token or a custom header, and the request for one
([anthropics/claude-ai-mcp#112](https://github.com/anthropics/claude-ai-mcp/issues/112))
is closed as not planned. Claude Desktop, Code and Cursor can send headers via
their config files, but the *connector* path is the one that reaches Cowork,
scheduled tasks and mobile — which is the entire point of deploying.

So the server proxies OAuth to GitHub: Claude gets the flow it expects, GitHub
does the real authentication, and no password touches this codebase.

GitHub will happily authenticate *any* account, so a second layer restricts
access by username. Without it, anyone with a GitHub account who found the URL
could sign in and read your leagues.

## 1. Create a GitHub OAuth app

<https://github.com/settings/developers> → OAuth Apps → New OAuth App

| Field | Value |
|---|---|
| Application name | anything, e.g. `ff-assist` |
| Homepage URL | `https://<your-app>.fly.dev` |
| Authorization callback URL | `https://<your-app>.fly.dev/auth/callback` |

Generate a client secret and keep both values to hand. Personal OAuth apps are
free, and nobody but you will ever use this one.

## 2. Generate a token-signing key

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

This matters more than it looks. `fly.toml` scales to zero, and without a
stable signing key every cold start invalidates the tokens Claude is holding —
the connector would break roughly daily, most likely mid scheduled-task.

## 3. Set secrets

### If the app already exists

`fly launch` is first-time-only. Once the app is created it will tell you so
and offer no way to re-run it, which is correct — skip straight to the secrets
below, then `fly deploy`. Nothing needs recreating.

```bash
fly status              # confirms the app name and region
fly volumes list        # ff_data should already be there
```

### If this is a fresh setup

```bash
fly apps create <your-app>        # Fly app names are globally unique
fly volumes create ff_data --size 1 --region <your-region>
```

### Either way — set the secrets before deploying

Order matters. The server refuses to start without the OAuth settings, so
deploying new code first gives you a boot loop and a rollback. Setting secrets
against the currently-running build is harmless; it ignores names it does not
read yet.

On a first setup, include the ESPN and league values too:

```bash
fly secrets set \
  ESPN_S2="$(grep -E '^ESPN_S2=' .env | cut -d= -f2- | tr -d "'")" \
  ESPN_SWID="$(grep -E '^ESPN_SWID=' .env | cut -d= -f2- | tr -d "'")" \
  FF_SEASON=2026 \
  FF_LEAGUE_KEYS="main,dynasty,work" \
  FF_LEAGUE_MAIN_ID=<leagueId>    FF_LEAGUE_MAIN_TEAM_ID=<teamId> \
  FF_LEAGUE_DYNASTY_ID=<leagueId> FF_LEAGUE_DYNASTY_TEAM_ID=<teamId> \
  FF_LEAGUE_WORK_ID=<leagueId>    FF_LEAGUE_WORK_TEAM_ID=<teamId>
```

The OAuth settings, needed on every setup:

```bash
fly secrets set \
  FF_PUBLIC_URL="https://<your-app>.fly.dev" \
  FF_GITHUB_CLIENT_ID="<from step 1>" \
  FF_GITHUB_CLIENT_SECRET="<from step 1>" \
  FF_ALLOWED_GITHUB_USERS="<your github username>" \
  FF_JWT_SIGNING_KEY="<from step 2>"
```

Secrets never enter the image — `.dockerignore` keeps `.env` out of the build
context, and these arrive as environment variables at runtime.

## 4. Deploy and check

```bash
fly deploy
fly logs            # look for "datastore warm: {...}"
```

### Do not judge it from a browser

Visiting `https://<your-app>.fly.dev` returns **404, and that is correct**. The
server mounts `/mcp` plus the OAuth routes; there is no homepage. Browsing to
`/mcp` will not look healthy either — MCP needs a POST carrying
`Accept: application/json, text/event-stream`. Use curl.

### The three checks that mean something

**One — the door is shut.** Must print 401:

```bash
curl -s -o /dev/null -w 'no auth: %{http_code}\n' -X POST https://<your-app>.fly.dev/mcp \
  -H "Content-Type: application/json" -d '{}'
```

**Two — Claude can discover how to log in.** The 401 must carry a
`WWW-Authenticate` header pointing at the resource metadata:

```bash
curl -s -D- -o /dev/null -X POST https://<your-app>.fly.dev/mcp \
  -H "Content-Type: application/json" -d '{}' | grep -i www-authenticate
# Bearer resource_metadata="https://<your-app>.fly.dev/.well-known/oauth-protected-resource/mcp"
```

**Three — the OAuth server advertises dynamic registration.** Claude registers
itself, so `registration_endpoint` must be present:

```bash
curl -s https://<your-app>.fly.dev/.well-known/oauth-authorization-server | python3 -m json.tool
```

Expect `authorization_endpoint`, `token_endpoint` and `registration_endpoint`.
Without the last one, Claude cannot complete setup.

### Failures worth recognising

| Symptom | Cause |
|---|---|
| Startup fails, logs say `Missing OAuth configuration` | One of `FF_PUBLIC_URL` / `FF_GITHUB_CLIENT_ID` / `FF_GITHUB_CLIENT_SECRET` is unset |
| Logs say `FF_ALLOWED_GITHUB_USERS resolved to an empty list` | Set it to your GitHub username, or nobody can get in |
| OAuth completes, every tool call says "not authorised" | Username mismatch — use the exact GitHub login, not your display name |
| Works, then breaks a day later | `FF_JWT_SIGNING_KEY` unset, so a cold start invalidated the tokens |
| GitHub says `redirect_uri mismatch` | Callback URL must be exactly `https://<your-app>.fly.dev/auth/callback` |
| Hangs ~5s then answers | Cold start from scale-to-zero, not a fault |

## 5. Register as a Custom Connector

Claude → Settings → Connectors → Add custom connector

- URL: `https://<your-app>.fly.dev/mcp`
- Leave **OAuth Client ID and Secret blank**. The server supports dynamic
  client registration, so Claude registers itself. Those fields exist for
  servers that require a pre-issued client; ours does not.

Claude sends you through a GitHub sign-in. Approve it, and the allowlist
confirms the account is yours.

Then set every tool to "Always allow" (Settings → Connectors → ff-assist). The
server is read-only, so this is safe — and a scheduled task hangs forever on an
approval prompt nobody is awake to click.

Note: that per-tool setting is known to reset to "Ask" after some Claude
Desktop updates. Have the Tuesday scheduled task call `health` first, so a
reset surfaces on a Tuesday rather than on a Sunday morning.

## Cold starts

`fly.toml` sets `auto_stop_machines = "suspend"` with `min_machines_running = 0`,
which keeps the bill near zero but means an idle machine wakes on demand.
Suspend restores process memory, so the warm nflverse frames usually survive,
and `/data` persists both the SQLite cache and the OAuth client registrations
across a full restart.

If a Sunday 9:30am brief ever feels slow, set `min_machines_running = 1` —
about $2/month, well inside the plan's ~$5 budget.

## Rotating credentials

### GitHub OAuth secret

Rotate it in the GitHub OAuth app settings, then:

```bash
fly secrets set FF_GITHUB_CLIENT_SECRET="..."
```

You will need to reconnect the connector once.

### ESPN cookies

They expire roughly yearly, and sooner if you change your password.

```bash
uv run scripts/verify_leagues.py      # confirm the new ones work locally
fly secrets set ESPN_S2="..." ESPN_SWID="..."
```

`fly secrets set` restarts the app automatically.

## What is deliberately not here

No write tools. Nothing sets a lineup or submits a claim. Automating writes
through an unofficial API risks the ESPN account, and you approve every move
anyway — the system's job is to make the call obvious in 90 seconds.
