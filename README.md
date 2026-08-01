# fl-ass-ist

Fantasy football analyst for three ESPN leagues. ESPN + nflverse data behind a
read-only MCP server, driven from a Cowork project and scheduled tasks.

Full design: [`fantasy-football-cowork-plan.md`](./fantasy-football-cowork-plan.md).
**Current state: Phase 0** — credentials and data access.

---

## Setup

### 1. Install dependencies

```bash
cd /Volumes/OWSD/jaikaruppuswamy/development/fl-ass-ist
uv sync
```

`uv` will fetch Python 3.11+ itself if you don't have it, and write `uv.lock` on
first run. Nothing is installed globally — everything lands in `.venv/`.

### 2. Getting your ESPN cookies

Two cookies authenticate you to private leagues. They're bearer credentials:
anyone holding them can act as you on ESPN, so treat them like a password.

1. Log in at <https://fantasy.espn.com> in Chrome.
2. Open DevTools — `Cmd + Opt + I`.
3. **Application** tab → left sidebar → **Storage** → **Cookies** →
   `https://fantasy.espn.com`.
4. Find these two rows and copy the **Value** column:

   | Cookie | Looks like | Notes |
   |---|---|---|
   | `espn_s2` | `AEBxK9...%2Bq7Z` — 300+ chars | Click the row and copy from the detail pane at the bottom. The grid column truncates it. |
   | `SWID` | `{1A2B3C4D-5E6F-7081-92A3-B4C5D6E7F809}` | Keep the curly braces. |

5. Paste them into `.env` (next step). **Do not URL-decode `espn_s2`** — the
   `%2B` and `%3D` sequences are part of the value.

> Safari: enable the Develop menu → Develop → Show Web Inspector → Storage → Cookies.

**Rotation.** These expire — typically after about a year, sooner if you change
your ESPN password or log out everywhere. There is no revocation API, so if they
ever leak the fix is: log out of ESPN on all devices, log back in, re-copy.
Phase 4's Tuesday scheduled task is where expiry should surface, not Sunday
morning.

### 3. Fill in `.env`

`.env` already exists and is gitignored. Open it and fill in the two blocks
marked `<-- FILL THESE IN`:

- **ESPN auth** — the two cookies above.
- **Leagues** — for each league, open it in ESPN and read the URL:

  ```
  https://fantasy.espn.com/football/team?leagueId=1234567&teamId=4&seasonId=2026
                                                   ^^^^^^^        ^
                                                   _ID            _TEAM_ID
  ```

  `FF_LEAGUE_KEYS` is the list of short handles you'll actually say out loud —
  "check the waiver board for **dynasty**". Rename `main` / `dynasty` / `work`
  to whatever you call your leagues, and rename the matching
  `FF_LEAGUE_<KEY>_*` variables to match.

### 4. Verify

```bash
uv run scripts/verify_env.py      # offline — format + completeness, secrets masked
uv run scripts/verify_leagues.py  # hits ESPN — proves the cookies actually work
```

`verify_env.py` reports every problem at once rather than one per run.
Expected output from `verify_leagues.py`:

```
✓ main         Dude Wheres My Carr
    week 1 · 12 teams · scoring: PPR
    your team: Team Karuppuswamy (0-0) · 16 players
```

---

## Layout

```
.env                  secrets — gitignored, never commit
.env.example          committed template, no real values
pyproject.toml        deps; espn-api pinned exactly (unofficial API)
src/ff_assist/
  config.py           .env loading, validation, masking
  espn_client.py      read-only espn-api wrapper + typed errors
scripts/
  verify_env.py       Phase 0 offline check
  verify_leagues.py   Phase 0 ESPN smoke test
data/cache/           local cache — gitignored
```

## Secrets policy

- `.env` is gitignored, along with `.env.*` (except `.env.example`), `*.pem`,
  `*.key`, and `secrets/`.
- `config.mask()` exists so cookies can appear in output safely. Nothing in this
  repo prints an unmasked cookie — keep it that way.
- Never paste real values into a chat, issue, or screenshot. If you do, rotate.
- At Phase 3 these values become environment variables on the host
  (Fly/Railway). Set them with `fly secrets set` — never bake them into an image
  or a Dockerfile.
- `FF_MCP_BEARER_TOKEN` is separate from the ESPN cookies: it's what Claude
  presents to *your* server. Generate one when you reach Phase 3:

  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

## Design constraints worth remembering

- **Read-only.** No lineup-setting, no waiver claims. Automating writes through
  an unofficial API risks the account, and you approve every move anyway.
- **Tools return decisions, not data.** If a tool response exceeds ~5KB it isn't
  aggregating enough. See §3 of the plan.
- **Pin `espn-api`.** The base URL moved to `lm-api-reads.fantasy.espn.com` in
  2024 and the API is undocumented. Bump the pin deliberately.

## Next

- [ ] Fill in `.env`, get both verify scripts green
- [ ] `uvx nfl-mcp init` — nflverse/DuckDB MCP for exploratory work
- [ ] Create the "Fantasy Football" Cowork project (memory + instructions persist there)
- [ ] Phase 1: `list_leagues`, `get_matchup`, `get_start_sit_slate` + the scoring-settings parser
