# fl-ass-ist

A fantasy football analyst for ESPN leagues, built as an MCP server you run
yourself. It reads your leagues, does the arithmetic, and hands a model
pre-scored rows so the conversation is about judgement rather than JSON.

Designed for managing **several leagues at once**. The interesting problems only
show up in the plural: three leagues have three different scoring systems, and a
player who is a clear start in one can be a coin flip in another.

Read-only by design. Nothing here sets a lineup or submits a waiver claim.

---

## What you get

Eight tools, exposed over MCP:

| Tool | Returns |
|---|---|
| `list_leagues` | Every league: scoring format, roster shape, current week, your team |
| `get_matchup` | Your starters vs your opponent's, projected margin, rough win probability |
| `get_start_sit_slate` | Your lineup, the optimal lineup, and the swaps — ranked on the mean of ESPN's and Sleeper's projections, with disagreements flagged |
| `get_waiver_board` | Free agents ranked against your roster holes, with FAAB bands |
| `get_game_environment` | Vegas implied team totals, spreads, weather where it matters |
| `get_player_trend` | Snap share, targets, target share, air yards, carries + direction |
| `get_defense_vs_position` | DvP expressed in *your* league's scoring, not generic PPR |
| `get_ros_schedule_strength` | Remaining schedule difficulty, playoff weeks weighted 2× |
| `health` | Server and data status — call it first in scheduled tasks |

Plus a `fantasy-analyst` skill that encodes house rules (when to chase ceiling,
how to treat a Questionable tag) and five scheduled tasks for the weekly rhythm.

### The design rule everything follows

**Tools return decisions, not data.** Three leagues of raw ESPN JSON is hundreds
of KB and produces slow, expensive, mediocre analysis. The server does the joins
and the arithmetic; the model does the judgement. Every response stays under
~5KB per league — if one doesn't, it isn't aggregating enough.

---

## What you need

- **An ESPN fantasy football account** with at least one league
- **Python 3.11+** and [uv](https://docs.astral.sh/uv/) (`brew install uv`)
- **Claude with Cowork** — the desktop app for local use
- *Optional, for the remote deployment:* a [Fly.io](https://fly.io) account with
  a card on file (a trial machine stops after 5 minutes, which defeats the point)
  and a GitHub account for OAuth

Free apart from hosting, which runs about $2–5/month. All the data sources —
ESPN, [nflverse](https://nflreadpy.nflverse.com/), Open-Meteo, Sleeper — cost
nothing.

---

## Quick start (local, ~15 minutes)

This gets the tools working against your leagues through the Claude desktop app.
No hosting, no OAuth.

### 1. Clone and install

```bash
git clone https://github.com/<you>/fl-ass-ist.git
cd fl-ass-ist
uv sync --extra mcp
```

### 2. Get your ESPN cookies

Two cookies authenticate you to private leagues. They are bearer credentials —
anyone holding them can act as you on ESPN, so treat them like a password.

1. Log in at <https://fantasy.espn.com> in Chrome
2. DevTools (`Cmd/Ctrl + Shift + I`) → **Application** → **Storage** →
   **Cookies** → `https://fantasy.espn.com`
3. Copy the **Value** of these two:

   | Cookie | Looks like | Notes |
   |---|---|---|
   | `espn_s2` | `AEBxK9...%2Bq7Z`, 300+ chars | Click the row and copy from the detail pane — the grid column truncates it |
   | `SWID` | `{1A2B3C4D-5E6F-...}` | Keep the curly braces |

   Do **not** URL-decode `espn_s2`; the `%2B` sequences are part of the value.

### 3. Configure

```bash
cp .env.example .env
```

Fill in the cookies, then your leagues. `FF_LEAGUE_KEYS` is a comma-separated
list of short handles you'll use in conversation — rename them to whatever you
call your leagues. For each one, read the IDs out of the ESPN URL:

```
https://fantasy.espn.com/football/team?leagueId=1234567&teamId=4&seasonId=2026
                                                 ^^^^^^^        ^
                                                 _ID            _TEAM_ID
```

### 4. Verify

```bash
uv run scripts/verify_env.py       # offline: format and completeness, secrets masked
uv run scripts/verify_leagues.py   # hits ESPN: proves the cookies work
```

`verify_env.py` reports every problem at once rather than one per run.

### 5. Connect to Claude Desktop

```bash
which uv    # you need the absolute path
```

Merge `claude_desktop_config.example.json` into
`~/Library/Application Support/Claude/claude_desktop_config.json`, substituting
that absolute `uv` path and your repo path. The desktop app does not inherit
your shell `PATH`, so a bare `uv` fails with ENOENT.

Restart Claude Desktop, then set the tools to "Always allow" in
Settings → Connectors.

### 6. Try it

```bash
uv run scripts/try_tool.py list_leagues
uv run scripts/try_tool.py all        # every tool, every league, with response sizes
```

`try_tool.py` calls the tools directly, so you can iterate on a response shape
without restarting Claude.

---

## Going remote (optional)

The local setup only works while the desktop app is open. To reach your leagues
from your phone, or to run scheduled tasks with your laptop shut, deploy the
server and register it as a Custom Connector.

**See [DEPLOY.md](./DEPLOY.md)** for the full walkthrough. The short version:
build the Docker image, deploy to Fly, authenticate with a GitHub OAuth app, and
restrict access to your own GitHub username.

One thing worth knowing before you start: **Claude's custom-connector UI accepts
OAuth only.** There is no bearer-token or custom-header field, and the request
for one ([claude-ai-mcp#112](https://github.com/anthropics/claude-ai-mcp/issues/112))
is closed as not planned. That is why this proxies OAuth to GitHub rather than
using a simple shared secret.

`.github/workflows/fly-deploy.yml` deploys on every push to `main` once you add
a `FLY_API_TOKEN` repository secret.

---

## The skill and the scheduled tasks

`skills/fantasy-analyst.skill` encodes the judgement the tools deliberately
leave out — when to prefer floor over ceiling, how to treat a Questionable tag,
what each league's quirks mean. **It is written around the author's leagues and
preferences.** Fork it and change the league table and the house rules; that
file is the point at which this becomes *your* system rather than a generic one.

The weekly rhythm, as five scheduled tasks:

| When | Task |
|---|---|
| Tue 8:00am | Waiver brief — ranked claims, FAAB bands, drop candidates |
| Thu 4:00pm | TNF lock check — anyone in the Thursday game |
| Sun 9:30am | Pre-game brief — final lineups across every league |
| Sun 11:45am | Inactives sweep — 15 minutes to lock, late scratches only |
| Tue 9:00am | Postmortem — what was wrong, logged for calibration |

Create them in Cowork with the prompts you want; the Tuesday brief should call
`health` first so an expired cookie surfaces on a Tuesday rather than on a
Sunday morning.

**Cron caveat:** scheduled tasks run on UTC. If your local time observes DST,
the tasks drift by an hour when the clocks change, and the inactives sweep is
the one that matters — it stops being 15 minutes before kickoff.

---

## Layout

```
src/ff_assist/
  config.py        .env / environment loading, validation, secret masking
  espn_client.py   read-only espn-api wrapper with typed errors
  scoring.py       league scoring rules and re-scoring a stat line
  slots.py         lineup shape + exact optimal-lineup assignment
  game_env.py      Vegas implied totals + the ESPN/nflverse team-code bridge
  usage.py         usage trends + the ESPN/nflverse player-name join
  dvp.py           defense-vs-position and rest-of-season schedule strength
  waivers.py       roster-hole detection, FAAB bands, Sleeper overlay
  projections.py   Sleeper's second opinion, re-scored in each league's rules
  weather.py       stadium venue table + Open-Meteo, gated on roof
  cache.py         SQLite TTL cache
  datastore.py     warm nflverse frames, refreshed daily
  tools.py         the eight tools, as plain functions
  server.py        FastMCP wrapper — stdio locally, HTTP when deployed
  auth.py          GitHub OAuth, user allowlist, rate limiting
scripts/
  verify_env.py            offline .env check
  verify_leagues.py        ESPN smoke test
  dump_league_settings.py  anonymized league dump → data/samples/
  try_tool.py              call any tool from the terminal
  check_external.py        Open-Meteo + Sleeper reachability, from anywhere
  backtest_2025.py         replay last season; does this actually help?
  projection_bakeoff.py    score rival projections against each other
  snapshot_projections.py  freeze a week's projections before kickoff
docs/
  projections.md           what the bake-off found, and why not to buy a feed
tests/                     ~370 tests
data/samples/              anonymized league fixtures (committed)
```

`data/samples/` holds anonymized dumps from the author's leagues — scoring
rules, roster shapes and prior-season object shapes, with team and owner names
stripped. They exist so the test suite has something real to run against.
Generate your own with `uv run scripts/dump_league_settings.py`.

---

## Testing

```bash
uv run pytest                    # ~370 tests
uv run pytest -m "not slow"      # skip the ones that bind a socket
```

Tests that need the network are skipped when it is unavailable, so the suite
stays green offline.

Third-party reachability is checked separately, because it differs by network:

```bash
uv run scripts/check_external.py                                              # your machine
fly ssh console -C "/app/.venv/bin/python /app/scripts/check_external.py --quick"   # the deployed host
```

Or call `health(probe_external=true)` over MCP to get the same answer from the
server without ssh. The distinction matters — a provider can serve a laptop
happily and answer 403 to a datacenter address.

The most interesting one is not a unit test:

```bash
uv run scripts/backtest_2025.py -v
```

It replays last season against real ESPN data and compares three lineups per
week — what you actually started, what the optimizer would have picked, and the
best available in hindsight. It validates every tool against real rosters *and*
tells you whether the system would have earned its keep. Expect a small positive
number; anyone promising a big one is selling something.

Its companion asks the question one level up — not "did the optimizer help" but
"would a better projection have helped":

```bash
uv run scripts/projection_bakeoff.py --espn --sleeper --pool-top 150
```

Eight candidate projections replayed week by week under strict lookback, scored
on error, on start/sit accuracy, and on points left on the bench — plus a
benchmark arm that knows each player's true season-long level, as par rather
than as a ceiling.
[`docs/projections.md`](docs/projections.md) is what it found, mistakes
included — and there are nine of them, which is the useful part. The short
version: rank on the mean of ESPN and Sleeper, both free; the pair beats
anything you can build from nflverse history by 1.6-2.8 points of lineup per
week across two seasons; and no paid feed is worth it, because the remaining
headroom is well under a win a season.

One companion runs on a clock rather than on demand:

```bash
uv run scripts/snapshot_projections.py --week 1     # every Saturday
uv run scripts/snapshot_projections.py --diff 1     # after that week is played
```

Projection endpoints are fetched today for seasons that ended long ago, and a
provider that quietly restates would make any backtest of it worthless. This
freezes a week's numbers before kickoff so the archive can be checked against
them afterwards. A snapshot not taken before the first Sunday cannot be taken
later.

---

## Things worth knowing if you fork this

Most of the work here was not writing features. It was discovering that
plausible-looking data is quietly wrong.

**`espn-api`'s scoring parser is not multi-league safe.** It mutates a
module-level dict, so with three leagues loaded the last one's scoring silently
overwrites the others — a full-PPR league starts reporting 0.5/reception the
moment a half-PPR league is constructed. It also treats a `pointsOverrides`
value of `0.0` as absent. `scoring.py` parses `scoringItems` itself for both
reasons, and the tests assert exact agreement with ESPN's own applied points.

**Two independent name/code joins fail silently.** ESPN says `LAR` and `WSH`
where nflverse says `LA` and `WAS` — unbridged, two teams vanish from every
lookup with no error. ESPN says `D.K. Metcalf` where nflverse says `DK Metcalf`,
and an unresolved player looks exactly like a player with no usage. Both have
explicit bridges and tests.

**ESPN's scoring categories are subtler than they look.** "Every 100 rushing
yards" is a bucket that accumulates; "100-199 yard rushing game" is a one-off
bonus, and they are *different stat IDs*. Yardage buckets are floored, not
rounded. Six stat names map to two IDs each. Getting any of these wrong produces
a number that looks right.

**Lineup optimisation needs to be exact, not greedy.** Greedy fills the RB slot
before knowing what the flex needs, and loses points on precisely the close flex
calls that matter. `slots.py` uses the Hungarian algorithm, verified against
brute force.

**Vegas lines are a near-term signal.** Books price about three weeks ahead, so
anything rest-of-season has to lean on defensive quality instead.

**DvP-in-your-own-scoring may not be worth much.** It is correct here, and
measured across three full-PPR leagues the rankings barely move between formats.
It would matter more with a half-PPR or TE-premium league in the mix. Treat a
soft matchup as a nudge, not a reason.

---

## Status

- [x] **Phase 0** — credentials, league access, verification scripts
- [x] **Phase 1** — scoring parser, lineup optimizer, cache, first three tools, stdio server
- [x] **Phase 2** — implied totals, usage trends, DvP, rest-of-season SoS, weather
- [x] **Phase 3** — HTTP transport, GitHub OAuth, rate limiting, Fly deployment
- [x] **Phase 4** — waiver board, `fantasy-analyst` skill, five scheduled tasks
- [ ] **Phase 5** — in-season calibration: log every recommendation and its
      outcome, review monthly. Almost nobody does this step, and it is the
      difference between a system that improves and one that just sounds
      confident.

### Known gaps

- **The ESPN side of the Sleeper join is unproven.** Sleeper, the nflverse id
  crosswalk and name resolution all verified against the live APIs from both a
  laptop and the deployed host. What has not been exercised is the final hop —
  matching those names to players in an ESPN free-agent pool — because that pool
  does not exist until leagues draft. Re-run `check_external.py` afterwards.
- **No projections of our own.** Everything leans on ESPN's, which are mediocre
  at TE and DST. Implied team total and usage trend are weighted above them, but
  a real projection model is the obvious next step.
- **Floor/median/ceiling is not modelled.** Bucket-scoring leagues need it most,
  since their yardage distribution is stepwise rather than linear.

---

## Credits

Built on [`espn-api`](https://github.com/cwendt94/espn-api),
[`nflreadpy`](https://nflreadpy.nflverse.com/) (nflverse data, CC-BY 4.0),
[FastMCP](https://gofastmcp.com), and [Open-Meteo](https://open-meteo.com/).

The original build plan is in
[`fantasy-football-cowork-plan.md`](./fantasy-football-cowork-plan.md).

## A note on the ESPN API

It is unofficial and undocumented. The base URL moved in 2024 and could move
again. `espn-api` is pinned to an exact version for that reason — bump it
deliberately. If everything breaks one Tuesday, that is the first place to look.
