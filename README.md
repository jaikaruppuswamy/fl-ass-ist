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
  scoring.py          league scoring rules + re-scoring a stat line
  slots.py            lineup shape + exact optimal-lineup assignment
  cache.py            SQLite TTL cache
  game_env.py         Vegas implied team totals + the ESPN/nflverse team bridge
  usage.py            usage trends + the ESPN/nflverse player-name join
  dvp.py              defense-vs-position and rest-of-season SoS
  weather.py          stadium venue table + Open-Meteo, gated on roof
  tools.py            the three Phase 1 tools (plain functions)
  server.py           FastMCP stdio wrapper around tools.py
scripts/
  verify_env.py       offline .env check
  verify_leagues.py   ESPN smoke test
  dump_league_settings.py   anonymized league dump -> data/samples/
  try_tool.py         call any tool from the terminal
  check_weather.py    validate stadium coords against live Open-Meteo
tests/                138 tests; `uv run pytest`
data/samples/         anonymized league fixtures (committed)
data/cache/           SQLite cache — gitignored
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


## Phase 1 — the tools

Three read-only tools, wrapped for MCP in `server.py`:

| Tool | Returns |
|---|---|
| `list_leagues()` | Every league: scoring format, roster shape, current week, your team |
| `get_matchup(league_key, week?)` | Your starters vs your opponent's, projected margin, rough win probability |
| `get_start_sit_slate(league_key, week?)` | Your lineup, the optimal lineup in that league's scoring, and the swaps between |
| `get_game_environment(week?)` | Implied team totals, spreads, wind/precip per game |
| `get_player_trend(player, weeks?)` | Snap share, targets, target share, air yards share, carries + direction |
| `get_defense_vs_position(league_key, window?, position?)` | DvP in that league's scoring; rank 1 = softest |
| `get_ros_schedule_strength(league_key, from_week?)` | Remaining schedule difficulty, playoff weeks weighted 2x |

Iterate without restarting Claude:

```bash
uv run scripts/try_tool.py list_leagues
uv run scripts/try_tool.py get_start_sit_slate gladiator
uv run scripts/try_tool.py all          # every tool, every league, with sizes
```

`try_tool.py` prints each response's byte size and flags anything over 5KB —
the plan's rule is that a response above that isn't aggregating enough.

### Register with Claude desktop

```bash
uv sync --extra mcp
which uv                                 # need the absolute path
cat claude_desktop_config.example.json   # merge into your desktop config
```

Then restart Claude desktop. Set the tools to "always allow" — the server is
read-only, and a scheduled task in Phase 4 will hang forever on an approval
prompt nobody is awake to see.

### Two things worth knowing

**`espn-api`'s scoring parser is not multi-league safe.** It mutates a
module-level dict, so with three leagues loaded the last one's scoring
overwrites the others. It also treats a `pointsOverrides` value of `0.0` as
absent (`override or points`), which misreports `gladiator`'s zeroed-out
return yardage for D/ST. `scoring.py` parses `scoringItems` itself for both
reasons. `tests/test_scoring.py` re-scores real 2025 stat lines and asserts an
exact match against ESPN's own applied points.

**`gladiator` scores yardage in buckets** — a point per 10 rushing yards, per
20 passing — with no per-yard rule at all. That averages to the same rate as
`inai`'s 0.1/yard but behaves differently at the margin: rushing yards 61
through 69 are worth nothing. `ScoringSettings.yardage_scoring()` reports
`per_yard` alongside a `granularity` so this stays visible, and Phase 2's
floor/median/ceiling needs to respect it.

## Next

- [x] Phase 0 — `.env`, cookies verified, all three leagues reading
- [x] Phase 1 — scoring parser, lineup optimizer, cache, three tools, stdio server
- [ ] Register the server in Claude desktop and exercise it in a real session
- [ ] Re-run `try_tool.py all` after your late-August drafts — until then the
      rosters are empty and the tools have nothing to chew on
- [ ] `uvx nfl-mcp init` — nflverse/DuckDB MCP for exploratory work
- [ ] Create the "Fantasy Football" Cowork project (memory + instructions persist there)
- [x] Phase 2 — implied totals, usage trends, DvP, rest-of-season SoS, weather
- [ ] Run `uv run scripts/check_weather.py` to validate the stadium coordinates
- [ ] Phase 3 — HTTP transport, bearer auth, deploy, register as a Custom Connector
