# Are the projections the weak point?

*Research note. Started 1 August 2026, rewritten once the evidence settled.*

Reproduce everything with:

```bash
uv run scripts/projection_bakeoff.py --rosters 200 --pool-top 150            # free arms
uv run scripts/projection_bakeoff.py --espn --sleeper --pool-top 150 --by-position
uv run scripts/projection_bakeoff.py --espn --sleeper --pool-top 150 --season 2024
```

---

## The answer

**Rank on the mean of ESPN's and Sleeper's projections.** Both free. Shipped —
see [What was built](#what-was-built).

Across 2024 and 2025, replayed week by week under strict lookback, the pair is
at or near the top of everything measured: it swept all three headline metrics
in 2024 and sits inside the standard error of the best arm in 2025. Neither
single source is consistent — Sleeper beat ESPN by a point in 2025 and tied it
in 2024 — but the average is reliable in both. That is the textbook ensemble
result and it is what the twelve-season Fantasy Football Analytics study
predicted.

Against the best projection this codebase can build from nflverse history, the
pair is worth **1.6–2.8 points of lineup per week**. Against what the system
used before this work (ESPN alone), **0.4–0.9 points**, or about 0.1 wins a
season. Small, free, and real.

**Do not buy a projection feed.** The reasoning is in
[What it is worth](#what-it-is-worth); the market survey is in
[If you want to spend money anyway](#if-you-want-to-spend-money-anyway). The
short version is that the whole remaining prize is under a win a season and the
best-performing source in the published literature gives away a personal-use API
key.

**One question is still open.** Sleeper marks down players who go on to score
nothing, more than any arm that could not have known. That is *either* an
archive that restates after the games *or* legitimate injury-report knowledge
*or* skill at spotting busts — and historical data cannot separate them. The
[forward test](#the-open-question) settles it, and it has a September deadline.

---

## What was built

`src/ff_assist/projections.py`, wired into `get_start_sit_slate` and
`get_matchup`. Three decisions in it are worth knowing about.

### It scores Sleeper's stat line, not Sleeper's points

The endpoint returns `pts_ppr` and using it directly would have been wrong.
That number is standard full PPR: **one point per 25 passing yards**.
`gladiator` scores a point per *20*, in buckets, with no per-yard rule at all —
so its quarterbacks would have been understated by 25% and its bucket categories
would have scored zero, silently, with no symptom but a slightly wrong lineup.

Instead the projected *stat line* is translated into ESPN's stat-id space and
re-scored under each league's own rules, exactly as ESPN's own
`projected_breakdown` already is. The translation routes through nflverse's
column names so `dvp.to_espn_stat_line` can derive the bucket and milestone
categories — logic that already existed and was already tested. Duplicating it
is how two copies start to disagree.

### The translation is proved, not trusted

Sleeper publishes both the components and its own `pts_ppr`, which makes the
crosswalk falsifiable against a number nobody here computed: score the
components under standard PPR and the answer must reproduce `pts_ppr`.
`verify_crosswalk()` does that, `check_external.py` runs it against the live
endpoint, and a test proves the check can actually fail (drop receiving yards
from the map and it catches the missing 8 points).

Stat keys that fail to map contribute zero and would quietly deflate a
projection, so unmapped keys are collected and reported rather than dropped —
the same failure mode as the bucket problem above, caught the same way.

### Absence is absence, never zero

Every function returns `None` rather than a number when Sleeper is unreachable,
and the consensus falls back to whichever source it has. A missing second
opinion costs the second opinion, not the lineup. `projection_basis: "espn"` on
the response says so out loud. Sleeper is an undocumented endpoint on somebody
else's infrastructure and no lineup should depend on it being up.

**Reachability, confirmed:** Sleeper answers 200 from Fly (`fly ssh console`
tested) and from a laptop. It 403s only from this sandbox's datacenter address.
An earlier draft of this note claimed datacenters generally were blocked — they
are not.

### New fields on every player row

| field | meaning |
|---|---|
| `proj` | the mean of the two sources, in this league's scoring. **Everything ranks and sums on this.** |
| `espn_proj` / `my_scoring_proj` | ESPN raw, and ESPN re-scored under this league's rules |
| `sleeper_proj` | the second source, re-scored the same way |
| `disagree` | signed Sleeper-minus-ESPN, **only** when they split by 3+ points |
| `projection_basis` | `espn+sleeper`, or `espn` when Sleeper was unreachable |

`get_matchup` sums the same `proj`, so the two tools cannot contradict each
other inside one brief.

---

## The evidence

Both seasons, common subsets, every arm scored on identical rows. 2024's ESPN
arm covers `naperville` only — the other two leagues did not exist yet.

| predictor | MAE ’25 | ’24 | close ’25 | ’24 | bench ’25 | ’24 |
|---|---:|---:|---:|---:|---:|---:|
| season avg (actual) | 6.17 | 6.13 | 53.3% | 54.4% | 26.15 | 25.02 |
| last 3 (actual) | 6.59 | 6.59 | 53.0% | 52.3% | 26.87 | 27.15 |
| blend: exp + season | 6.05 | 6.09 | 53.1% | **54.6%** | 25.81 | **24.51** |
| blend × implied 50% | 6.05 | 6.07 | **53.8%** | 54.3% | **24.99** | 24.58 |
| ESPN | 6.04 | 5.99 | 55.3% | 56.0% | 23.06 | 23.32 |
| Sleeper | 5.95 | 5.93 | 55.4% | 56.0% | **22.06** | 23.34 |
| ENSEMBLE (all 3) | 5.92 | 5.92 | 55.4% | 56.2% | 22.75 | 23.07 |
| **ESPN + Sleeper** | 5.94 | **5.90** | 55.4% | **56.5%** | 22.18 | **22.94** |
| *[benchmark] true mean* | *5.59* | *5.55* | *56.7%* | *56.6%* | *19.04* | *18.40* |

Bold marks the best real arm in each column, per season. The free arms are
abbreviated; the full eight are in the script's own output.

**Close calls** are pairs the predictor itself cannot separate by more than 3
points — the decisions you actually stare at on a Sunday. **Bench loss** is
points per lineup below the hindsight-best lineup, over 200 random 15-man
rosters a week drawn from the 150 best players.

Every number is worse than a full-panel run because the common subset is 43–44%
of the panel and it is the better-player end: rostered players are rostered for
a reason, absolute error scales with the size of the numbers, and ranking two
starters is harder than ranking a starter against a waiver body. **Only the
ordering within a column is readable.** Comparing these to a full-panel figure
is the mistake that broke the first attempt at this table.

### The benchmark row is not a ceiling

`[benchmark] true mean` knows each player's true season-long *level* — his
whole-season average, target week included — and nothing else about the week. It
is what you would score knowing exactly how good everyone is.

It is **not** perfect knowledge and **not** an upper bound. A real projection
also knows the opponent, the weather and who else is hurt, none of which a
season average can contain, so beating it is possible. In 2024 two arms did, at
QB and RB, inside the noise band. Read it as par, not a wall.

This matters because an earlier draft used "the whole prize is N points" to
argue nothing was worth buying. The correct statement is *"the best arm is N
points from a static-quality benchmark, and can go past it"* — a weaker claim.
The recommendation survives because the pairing is free either way.

---

## What replicated, and what did not

Two seasons is a small sample, and the honest way to read it is to separate
the findings that appeared twice from the ones that appeared once. This is the
part of the note most worth trusting.

### Replicated

**Both external sources beat everything built from history.** ESPN and Sleeper
each beat every free arm on every decision metric in both seasons, by 1.5–2.9
bench points. That is the finding the whole exercise produced.

**Averaging the two is the safe choice.** Top or joint-top in both seasons,
where each single source won one and tied the other. In 2024 it beat both alone
on close calls (56.5% against 56.0% for each), which clears the ±0.8pp band.

**Trailing three games is worse than season-to-date.** `last 3 (actual)` is the
worst arm in the table in both seasons, on every metric, by 0.5 MAE and 1.9–2.6
bench points. Recency is noise wearing signal's clothes. The practical version:
when a player's last three weeks look different from his season, believe the
season unless the *usage* moved too.

**Blending two weak signals beats either.** `blend: exp + season` is the best or
second-best free arm in both seasons, ahead of both of its own components. The
same shape as the ESPN/Sleeper result, one level down.

**Scaling by implied team total helps, at half strength, modestly.** On the full
panels it improves bench loss by 0.46 (2024) and 0.76 (2025) over the unscaled
blend. Full strength makes MAE worse in both. No parameter was fitted — full and
half were both written down before either ran.

**Close calls are near coin flips and stay that way.** 53–56% for every real
arm; 56.6–56.7% for the benchmark. There is no version of this where the hard
start/sit decisions get easy, and any tool that implies otherwise is lying.

### Did not replicate

**ESPN's positional fingerprint.** This one I got wrong in a previous draft and
propagated into the skill file. In 2025 ESPN was 4.2pp below the benchmark at
quarterback — well outside the ±2.5pp band — and exactly at it for receivers,
which matched Fantasy Football Analytics' twelve-season finding beautifully. In
2024 it does not hold at all:

| position | ESPN − benchmark, 2025 | 2024 | replicates? |
|---|---:|---:|---|
| QB | −4.2 *(outside band)* | +0.3 | no |
| RB | −2.8 *(outside band)* | −0.1 | no |
| WR | +0.0 | −1.2 *(outside band)* | no |
| TE | −1.0 | −0.1 | no |

Not one position agrees in direction *and* significance across the two seasons.
The 2025 table was one season of noise that happened to look like a published
result, which is exactly how a spurious finding gets believed. **There is no
per-position rule to apply**, and the skill has been corrected.

**Expected points beating raw production.** `ff_opportunity`'s expected fantasy
points beat season-to-date actuals by 0.62 bench points in 2025 and *lost* by
0.73 in 2024. The 2025-only story — that expected points strip touchdown luck
and leave a clean slot for the market adjustment — is a nice mechanism with one
season of support and one season against. The blend containing them is still
good; the component alone is not established.

**Which free arm is best.** `blend × implied 50%` in 2025, `blend: exp + season`
in 2024, separated by less than a standard error either way. Neither is "the
best free arm" — they are jointly about equal, and both are comfortably behind
either external source. None of them is in the shipped objective.

---

## What it is worth

Weekly lineup scores in the simulation have a spread of 23–24 points, which
makes a margin standard deviation near 33 and puts one point a week at about
**0.16 wins over a 14-game season**.

| improvement | extra wins/season |
|---|---:|
| +0.4 to +0.9 pts/week — ESPN alone → the pair *(what shipped)* | 0.06–0.15 |
| +1.6 to +2.8 pts/week — best free arm → the pair | 0.26–0.45 |
| +3.0 to +4.5 pts/week — the pair → the static-quality benchmark | 0.49–0.72 |

The last row is the headroom a paid feed would be competing for, and it would
capture a fraction of it. That is the argument against buying anything, and it
holds without needing the benchmark to be a hard ceiling.

---

## What the published research says, and what it does not

The only long-running public accuracy study is Fantasy Football Analytics',
twelve seasons, 2014–2025:

- **FantasyPros leads at three of four positions** historically (QB 61.0, WR
  40.2, TE 31.4 MAE); CBS leads at RB (52.2).
- **ESPN is uneven rather than bad** — recently tied for best at TE (29.6
  against FFToday's 29.4) and last at QB (86.7 against NFL's 71.2).
- **Aggregation beats every individual source.** The simple average outperformed
  individual sources in 69% of comparisons and beat *weighted* averages. Equal
  weighting is both simpler and better.

The aggregation finding is the one this exercise reproduced, and it is the basis
of the recommendation.

Two caveats. **Those are preseason season-total projections**, not weekly ones —
"MAE 61.0" is 61 points across a whole season, and the study measures a
different task. And its positional rankings are what my own 2025 table appeared
to confirm before 2024 refused to; a match against a study measuring something
else is weaker evidence than it feels like.

Sleeper does not appear in the study at all, which is why it needed testing
here rather than trusting.

---

## The open question

The look-ahead audit, on 548 scoreless player-weeks:

```
  baseline for Sleeper                                  0.30     548
  Sleeper                            2.46      9.72     0.25     548
  ! ESPN  (only 30 scoreless — skipped)
```

The ratio is a projection on weeks a player scored nothing, over the same arm's
projection on weeks he scored. An arm that cannot know who sat projects him
about the same either way. Sleeper marks those weeks down 17% more than the free
arms do.

Three things produce that signature and this test cannot separate them:

1. **The archive restates** — the endpoint hands back a value revised after the
   games. Every Sleeper number above would be inflated and the arm is out.
2. **The live projection read Friday's injury report** — not cheating at all. It
   is what a Sunday-morning projection is *for*, and the free arms lack it
   because they are trailing averages, not because Sleeper is peeking.
3. **It is better at spotting busts** — a returning starter ahead of him, a
   brutal matchup, a collapsing snap share. Skill.

Two of the three are reasons to use Sleeper. The absolute numbers lean away from
the bad case: Sleeper projects 2.46 on scoreless weeks where the free arms
project about 2.9, and a source serving zeroes for ruled-out players would show
a far larger gap than 0.45 points. Note also that **ESPN could not be tested at
all** — only 30 scoreless weeks in its coverage — so holding Sleeper to a bar
ESPN has not cleared would be inconsistent.

No further analysis of last season resolves it, because the distinguishing
evidence was never recorded: what the endpoint said *before* the games.

### The forward test, and its deadline

```bash
uv run scripts/snapshot_projections.py --week N     # the Saturday before week N
uv run scripts/snapshot_projections.py --diff N     # the Monday after
```

Freezes a week's Sleeper projections to a committed JSON file, then diffs the
archive against it once the games are final. Nothing moved → the archive is
honest and Sleeper's edge is a real forward-looking edge. Players who got hurt
quietly marked down → the arm leaves the ranking, which is a one-line change
because the mean already falls back to whichever source is present.

**Two timing rules the script now enforces**, both learned the hard way:

- It refuses to certify a week that has not been played. A diff run before
  kickoff can only ever say "unchanged", which is not a weak result but no
  result wearing the costume of one.
- A snapshot taken more than three days before kickoff is a weak test, because
  Sleeper legitimately revises all through camp and that movement swamps the
  signal. The script warns, and lets a closer run replace an early one.

**Status: the Week 1 2026 snapshot taken on 1 August is 39 days early and does
not count.** Week 1 kicks off 9 September. Re-run it between **5 and 8
September**; the script will replace the early file. Then `--diff 1` on Monday
15 September for the verdict.

---

## If you want to spend money anyway

Ranked by whether I would actually do it. None of it is necessary.

| Option | Price | Verdict |
|---|---|---|
| **FantasyPros API** | **free** for personal use | Best-performing source in the twelve-season study, and there is a self-serve free tier for personal non-commercial use — key at `secure.fantasypros.com/api-keys/request/`. `Settings.fantasypros_api_key` already exists in the config and is still empty. The obvious third source if you want one; costs an email address. |
| **The Odds API** | free tier | Not for projections: for *player props*, a market-priced forecast of the exact quantities fantasy scores. Event-odds cost is `markets × regions` per event, so a 16-game slate at 4 markets in 1 region is 64 credits — **the free 500/month covers a weekly pull with room to spare.** The $30/mo 20K plan is only for intra-week line movement. |
| **FantasyPros HOF** | $107.88/yr | Production API access and higher limits over the free key. For one person pulling weekly, the free tier will not bind. |
| **FFA Insider** | $5.99/mo | The historical-projection archive back to 2008 is the one thing here that would let this bake-off run across a decade instead of two seasons — which, given how much did not replicate across two, is the single most valuable thing on this list. Buy it for a month as a research tool, not as a feed. |
| **Fantasy Nerds API** | $399.95/yr | Clean API, real projections. $400 for a slice of half a win. No. |
| **SportsDataIO / Sportradar** | ~$16k/yr and up | Enterprise licensing. Not a consumer product. |

One more free source, not yet tried: **FantasyPros ECR via nflverse**
(`load_ff_rankings()`) — no key at all, weekly, with a standard deviation and
best/worst per player. That spread is arguably more useful than the rank: it is
the consensus' own uncertainty, which is exactly what the floor-versus-ceiling
house rule wants and does not currently have.

---

## What this exercise cost, in wrong answers

Worth recording, because the pattern is more portable than the result.

1. **"ESPN is the weak point"** — Jai's prior, and my first instinct too. Half
   right: ESPN is beatable, but only by another free source and by less than
   either of us assumed.
2. **"Every projection ties, don't bother"** — mine, confidently argued, from
   eight variations of one idea all built by me from the same nflverse data. A
   field of near-identical candidates is evidence about the candidates, not
   about the problem. Two genuinely independent sources broke it immediately.
3. **"ESPN is terrible, look at the table"** — arms scored on whatever
   population each happened to cover. ESPN sees only rostered players, who are
   fewer and better; that inflated its error by roughly the size of the effect
   being measured. Fixed by `common_subset()`, and a synthetic check pins the
   mechanism: a deliberately *worse* arm looked 1.96 MAE behind on its own
   coverage and 0.26 behind on the shared subset.
4. **"ESPN has no 2024 history"** — a `LeagueNotFound` short-circuit. One league
   Jai did not play in 2024 disqualified the two that had data. `naperville` had
   it all along, and it produced the strongest result in this document.
5. **"Perfect knowledge is worth one win"** — the benchmark arm is not perfect
   knowledge. It knows player quality and nothing about the week, so a real
   projection can pass it, and in 2024 two did. The conclusion held for other
   reasons; the argument for it did not.
6. **"The audit says Sleeper is clean"** — it said nothing, at n=30, because the
   fix for mistake 3 shrank the sample, and because the ratio turns out not to
   be population-invariant at all (0.26 on the full panel, 0.79 on rostered
   starters — same arms).
7. **"The audit says Sleeper cheats"** — the rewritten test fired at n=548 and I
   had written the flag to announce `knows who sat`. But knowing who sat is what
   a Sunday projection is *for*. I built the check to catch a mistake and put a
   mistake in the check.
8. **"The archive is honest"** — the forward test, on a week not yet played,
   minutes after the snapshot. The guard against mistakes 1–7 needed a guard.
9. **"ESPN is weakest at quarterback"** — one season's position table, outside
   the noise band, agreeing with a published study. It did not replicate in
   2024, at any position, in direction or significance. I had already written it
   into the skill file as measured fact before checking the second season.
   Corrected there too.

Four of the nine — 3, 6, 7, 8 — are the same failure: **a check that returned a
confident answer where the correct output was "I cannot tell from this."** Two
more (2 and 9) are the neighbouring failure: **generalising from a sample that
only looked large.** None was a coding error in the arithmetic sense; every one
was a population, a label, or a claim that did not quite match what had actually
been computed.

Tests catch the first kind. Only re-reading the claim against the computation
catches the second — which is why this section exists, and why it is the part of
this document to read first next time.

---

## Where the edge actually is

If the whole projection question is worth well under a win, the honest follow-up
is what is worth more.

1. **Waivers.** Adding a player who scores 12/week over one who scores 6 is
   worth 6 points a week in a starting slot — more than the entire projection
   headroom, every week, for the rest of the season.
2. **Being right about injuries by 11:45 on Sunday.** Starting an inactive costs
   the full slot, ~12 points. One catch a season outweighs a year of projection
   tuning. The inactives sweep already does this.
3. **Bye and playoff-week planning.** Structural, decided weeks ahead, and no
   projection helps with it.

Projections are the part of this system that feels most improvable and is least
improvable. Worth knowing before spending a season on it.
