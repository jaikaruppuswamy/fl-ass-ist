# Fantasy Football Analyst — Cowork Build Plan

**Target:** three ESPN leagues, weekly pre-game analysis, start/sit + waiver decisions
**Window:** ~6 weeks to Week 1 (drafts late Aug, Week 1 ~Sept 10, 2026)
**Verdict:** build one remote MCP server, drive it from a Cowork project + scheduled tasks

---

## 1. Build vs. buy

### What already exists (and why it isn't enough)

| Service | What it does | Gap for you |
|---|---|---|
| **FantasyPros My Playbook / Start-Sit Assistant** (~$8–12/mo) | Syncs ESPN, expert consensus rankings (ECR), start/sit, waiver, trade analyzer, auto-swap inactives | Consensus, not *your* reasoning. Can't interrogate it. Doesn't know your opponent's roster construction. |
| **Dynasty Nerds Lineup Optimizer** (~$70/yr) | Syncs Sleeper + ESPN, optimizes flex/bye across leagues | Optimizer only — no narrative, no opponent modelling |
| **StartBench / LeagueVision / FantasySP** | AI start-sit wrappers, some use Vegas props | Black box. ESPN private league sync often needs a Chrome extension step. |
| **RotoBaller / FFToday AI optimizers** | Matchup + usage-based projections | Generic; not tied to your scoring settings |

**Recommendation:** keep a FantasyPros sub as a *cross-check oracle* (~$100/yr, worth it for ECR and injury speed) and build the analysis layer yourself. The thing none of them do is reason across **all three of your leagues at once**, against **your specific scoring settings**, with **your opponent's roster** as an input, and then explain itself.

### Existing MCP servers worth knowing

| Repo | Stack | Use it? |
|---|---|---|
| `ebhattad/nfl-mcp` | nflreadpy + DuckDB, local, `uvx nfl-mcp` | **Yes — install as-is.** Play-by-play 2013+, rosters, injuries, snap counts, target share / air-yards share via `ff_opportunity`. Best free NFL data MCP. |
| `KBThree13/mcp_espn_ff` | FastMCP + `espn-api` | **Read the source, don't depend on it.** Good reference for tool shape; thin on analytics. |
| `JayMishra-source/Fantasy-Football-AI-CoManager` | TypeScript MCP + GitHub Actions | Most complete ESPN one. Worth mining for its lineup-optimizer logic. |
| `anthonybaldwin/sleeper-api-mcp` | Sleeper | Wrong platform, but its trade/matchup tool signatures are a good template |
| Sportradar NFL MCP | Official, paid | Overkill unless you want licensed real-time feeds |

---

## 2. Data sources

### Free tier — everything you actually need

| Need | Source | Notes |
|---|---|---|
| League settings, rosters, opponent roster, matchups, FA pool, ESPN projections | ESPN v3 API via **`espn-api`** (`pip install espn-api`) | Requires `espn_s2` + `SWID` cookies for private leagues. Base URL moved to `lm-api-reads.fantasy.espn.com` in 2024 — pin the package version. |
| Play-by-play, weekly player stats, snap counts, target share, air yards, rosters, depth charts, injuries | **`nflreadpy`** (nflverse) | `nfl_data_py` is deprecated — use `nflreadpy`. Polars-based; `.to_pandas()` if you prefer. CC-BY 4.0. |
| **Points allowed to opposing positions (DvP)** | Compute from nflverse weekly stats | Don't buy this. Computing it yourself lets you express it *in your league's scoring* (PPR vs half vs TE-premium), which no vendor does. |
| Strength of schedule (rest-of-season) | nflverse `load_schedules()` + your DvP table | Roll DvP forward across remaining opponents per player |
| **Vegas spread + game total** | `nfl.load_schedules()` — includes `spread_line` and `total_line` | Free. Gives you implied team totals without any odds API. This is the highest-signal single variable in the whole system. |
| Weather | **Open-Meteo** — no API key, 10k calls/day free | Join on stadium lat/long. Gate on roof type (dome/retractable → skip). Wind >15mph is the variable that actually moves fantasy points; temperature mostly doesn't. |
| Injury / practice participation | nflverse injuries + ESPN player news endpoint | |
| Market signal (adds/drops) | **Sleeper API** — no auth, 90 req/min | `/v1/players/nfl/trending/add` — free crowd wisdom for waivers |

### Paid — optional, in priority order

1. **FantasyPros** consumer sub (~$100/yr) — ECR as a cross-check. Best value.
2. **Fantasy Nerds API** ($399.95/yr) — projections, DvP ordinals, depth charts, inactives, expert picks in one clean API. Convenient, not necessary.
3. **The Odds API** ($99/mo Business tier for NFL player props) — props are the sharpest per-player projection that exists, but the price is hard to justify for three home leagues. Skip year one.

**Total year-one cost if you follow this plan: ~$100 (FantasyPros) + ~$5/mo hosting.**

---

## 3. Architecture

### Three Cowork constraints that drive the design

1. **Cowork runs remotely on Anthropic's servers.** Local stdio MCP servers only reach it through the desktop app, and only while it's open.
2. **Scheduled tasks run remotely** — they won't see a local server or local files. A Sunday 10am lineup brief must not depend on your laptop being awake.
3. **Cowork memory only persists inside projects.** Chat memory doesn't carry over.

→ **Build one remote MCP server (HTTP transport + bearer auth), register it as a Custom Connector, and run everything from a dedicated Cowork project.** That gets you chat, Cowork, scheduled tasks, and mobile from a single build.

### The single most important design decision

**Tools must return decisions, not data.**

Three leagues × (16 roster + 16 opponent + ~150 free agents) is hundreds of KB of raw JSON. Dumping that into context produces slow, expensive, mediocre analysis. The server does the joins and the math; the model does the judgement.

Bad: `get_roster()` → raw ESPN player objects
Good: `get_start_sit_slate(league_key, week)` → one pre-scored row per lineup slot, ~2–3KB per league

```
slot: FLEX
candidates:
  - name, pos, team, opp
  - espn_proj, my_scoring_proj          # re-scored for THIS league's settings
  - dvp_rank_vs_pos, dvp_pts_allowed    # computed in this league's scoring
  - implied_team_total, spread
  - wind_mph, precip_pct, roof          # null if dome
  - snap_pct_l3, target_share_l3, trend # usage direction
  - injury_status, practice_participation
  - floor / median / ceiling
```

### Proposed tool surface (8 tools)

| Tool | Returns |
|---|---|
| `list_leagues()` | The three leagues, scoring settings, roster slots, current week |
| `get_matchup(league_key, week?)` | Your lineup vs. opponent's lineup, projected margin, win probability |
| `get_start_sit_slate(league_key, week?)` | The table above — the workhorse |
| `get_waiver_board(league_key, top_n=25)` | FA pool scored on ROS value, filtered to your roster holes, with Sleeper trending overlay |
| `get_defense_vs_position(position, weeks=4, scoring)` | DvP table in a given scoring format |
| `get_game_environment(week?)` | Per-game: implied totals, spread, weather, roof, pace |
| `get_player_trend(player, weeks=6)` | Snap %, route %, target share, red-zone touches — usage over time |
| `get_ros_schedule_strength(league_key)` | Rest-of-season SoS per rostered player, playoff weeks (15–17) weighted |

**Do not build a write tool.** Keep it read-only. Auto-setting lineups through an unofficial API risks your ESPN account, and most leagues consider it poor form. You approve every move by hand — the system's job is to make the call obvious in 90 seconds.

---

## 4. Phased plan

### Phase 0 — Foundation (this week, ~1 hour)

- [ ] Pull `espn_s2` and `SWID` cookies (DevTools → Application → Cookies → `fantasy.espn.com`, or the open-source Chrome extension linked in `cwendt94/espn-api` discussion #150)
- [ ] Store them in a `.env` / secret manager — **never in the repo**
- [ ] Verify all three leagues read cleanly:
  ```python
  from espn_api.football import League
  for lid in [L1, L2, L3]:
      lg = League(league_id=lid, year=2026, espn_s2=S2, swid=SWID)
      print(lg.settings.name, lg.current_week, len(lg.teams))
  ```
- [ ] `uvx nfl-mcp init` — installs the nflverse/DuckDB MCP locally, gives you immediate exploratory power
- [ ] Create a Cowork **project** called "Fantasy Football" (memory + instructions persist there)

### Phase 1 — Core server (one weekend, ~4–6 hrs)

- [ ] `ff-mcp` repo, Python + FastMCP, stdio transport for now
- [ ] Implement `list_leagues`, `get_matchup`, `get_start_sit_slate` (ESPN projections only, no derived analytics yet)
- [ ] **Scoring-settings parser** — read each league's scoring rules from ESPN and build a re-scoring function. This is the piece everything else depends on; get it right early.
- [ ] Cache layer: DuckDB or SQLite, TTL by data class (rosters 15min, nflverse daily, weather 3h, schedules weekly)
- [ ] Test in Cowork desktop

### Phase 2 — Derived analytics (~3 hrs)

- [ ] DvP computed from nflverse weekly stats, per scoring format, rolling 4-week + season-long
- [ ] Weather join: stadium lat/long table + roof type + Open-Meteo forecast, gated so domes return null
- [ ] Implied team totals from `spread_line` / `total_line`
- [ ] Usage trends: snap %, target share, air yards share from `ff_opportunity`
- [ ] Rest-of-season SoS with weeks 15–17 weighted 2×

### Phase 3 — Go remote (~2–3 hrs)

- [ ] Switch FastMCP to streamable HTTP transport
- [ ] Bearer-token auth + rate limiting
- [ ] Deploy: Fly.io / Railway / Cloudflare (~$5/mo). Single small instance is plenty.
- [ ] Register as a **Custom Connector** in Claude settings → now available in chat, Cowork, scheduled tasks, and mobile
- [ ] Set connector tool permissions to "Always allow" (read-only server, so this is safe and prevents scheduled runs stalling on approval prompts)

### Phase 4 — Skill + schedule (~2 hrs)

Write a `fantasy-analyst` skill in the Cowork project encoding your house rules — how you weight floor vs. ceiling, when you chase upside (only when you're an underdog by >8 projected points), your risk tolerance on questionable tags. This is where your judgement lives, and it's what makes the output *yours* rather than generic.

Then `/schedule` these:

| When | Task |
|---|---|
| **Tue 8am** | Waiver brief — all 3 leagues, ranked claims with FAAB bids, drop candidates, injury fallout from Sunday |
| **Thu 4pm** | TNF lock check — anyone in a Thursday game, go/no-go |
| **Sun 9:30am** | **The big one.** Full pre-game brief: final lineups for all 3 leagues, inactives check, weather re-pull, opponent's projected lineup, flagged swaps with one-line reasoning |
| **Sun 11:45am** | Inactives sweep — 15 min before lock, catch late scratches only |
| **Tue 9am** | Postmortem — what the model got wrong, logged for calibration |

Note: Cowork sessions burn usage faster than chat. Batch all three leagues into one session per task rather than three separate runs.

### Phase 5 — Calibration (all season, 10 min/week)

Log every recommendation and its outcome to a CSV in the project:

```
week, league, decision_type, recommended, alternative,
projected_delta, actual_delta, was_right, primary_reason
```

Review monthly. This is the part almost nobody does, and it's the difference between a system that improves and one that just sounds confident. After ~6 weeks you'll know whether your weather adjustment is real or noise, and whether you're systematically over-trusting ESPN projections at any position (you probably are — usually TE and DST).

---

## 5. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **ESPN API is unofficial and undocumented** — base URL changed in 2024, could break any time | Pin `espn-api` version. Write a smoke test on the Tue schedule so a break surfaces Tuesday, not 10 minutes before Sunday lock. Keep a manual fallback path. |
| **Cookies expire** | Treat rotation as routine. Have the smoke test detect `ESPNAccessDenied` and notify you. |
| **Rate limits / getting blocked** | Cache aggressively. ESPN publishes no limits but will throttle. One roster pull per league per 15 min is plenty. Sleeper is 90 req/min. |
| **Garbage-in on projections** | ESPN's own projections are mediocre. Treat them as one input, not the base case. Weight implied team total and usage trend higher. |
| **Over-automation** | Read-only. You approve every move. |
| **Context bloat degrading analysis** | The pre-scored-slate design above is the fix. If a tool response exceeds ~5KB, it's doing too little aggregation. |
| **Scheduled task stalls on permission prompt** | Run each task manually once and select "always allow" for each tool. |

---

## 6. Reality check on edge

Be honest about where the wins actually are. Ranked by expected points gained:

1. **Never miss a late inactive** — the Sunday 11:45 sweep alone is worth more than any projection model
2. **Waiver wire speed and accuracy** — most leagues are won here, not in start/sit
3. **Flex decisions** — genuinely close calls where implied total + usage trend break the tie
4. **Bench/starter swaps** — you're usually right already; the model catches the 1-2 per season you'd have gotten wrong

Weather and DvP are real but small — worth including because they break ties, not because they'll win you a league. The infrastructure is the point: three leagues managed in 15 minutes on Sunday instead of two hours.

---

## Reference links

- `espn-api` — https://github.com/cwendt94/espn-api ([wiki](https://github.com/cwendt94/espn-api/wiki), [cookie instructions](https://github.com/cwendt94/espn-api/discussions/150))
- `nflreadpy` — https://nflreadpy.nflverse.com/
- `nfl-mcp` — https://github.com/ebhattad/nfl-mcp
- `mcp_espn_ff` — https://github.com/KBThree13/mcp_espn_ff
- Fantasy-Football-AI-CoManager — https://github.com/JayMishra-source/Fantasy-Football-AI-CoManager
- Sleeper API — https://docs.sleeper.com/
- Open-Meteo — https://open-meteo.com/
- ESPN endpoint reference — https://github.com/pseudo-r/Public-ESPN-API
- Cowork docs — https://support.claude.com/en/articles/13345190-get-started-with-claude-cowork
- Cowork scheduled tasks — https://support.claude.com/en/articles/13854387-schedule-recurring-tasks-in-cowork
- Custom connectors (remote MCP) — https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
