"""League scoring settings and re-scoring.

Why this module exists rather than using ``league.settings.scoring_format``:

``espn_api.football.settings.Settings`` looks up each stat in the module-level
``SETTINGS_SCORING_FORMAT_MAP`` and then *mutates the dict it got back*::

    scoring_type = SETTINGS_SCORING_FORMAT_MAP.get(stat_id, {...})
    scoring_type['points'] = ...          # <- mutates the shared constant

Every ``League`` in the process therefore shares one set of rule dicts, and the
most recently constructed league silently overwrites the scoring of all the
others. With three leagues open at once — the entire point of this project —
a full-PPR league starts reporting 0.5 points per reception the moment a
half-PPR league is loaded. It also only reads ``pointsOverrides['16']``, so
TE-premium leagues parse as if they were standard.

So we parse ``scoringSettings.scoringItems`` ourselves into frozen dataclasses.

The payoff is :meth:`ScoringSettings.score`, which turns a raw stat line into
points *in a specific league's rules*. That's what makes "who's the better flex
play across my three leagues" answerable, and it's what lets DvP in Phase 2 be
expressed in each league's own scoring instead of generic PPR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from espn_api.football.constant import PLAYER_STATS_MAP, SETTINGS_SCORING_FORMAT_MAP

__all__ = [
    "DEFAULT_POSITION_IDS",
    "ScoreResult",
    "ScoringRule",
    "ScoringSettings",
    "stat_ids_for",
]

# ESPN's `defaultPositionId` space, which is what `pointsOverrides` is keyed by.
# NOTE: this is a DIFFERENT numbering from `lineupSlotId` (espn-api's
# POSITION_MAP). They coincide at 16 = D/ST, which is exactly why espn-api's
# hardcoded `pointsOverrides['16']` appears to work and quietly fails for TE
# premium. Confirmed against real league dumps in tests/test_scoring.py.
DEFAULT_POSITION_IDS: dict[str, int] = {
    "QB": 1,
    "RB": 2,
    "WR": 3,
    "TE": 4,
    "K": 5,
    "P": 7,
    "D/ST": 16,
    "DT": 9,
    "DE": 10,
    "LB": 11,
    "CB": 12,
    "S": 13,
    "HC": 14,
}

# breakdown dicts from espn-api are keyed by stat NAME; scoringItems by stat ID.
#
# The mapping is NOT one-to-one: six names have two ids each —
#   passingYards [3, 22]        rushingYards [24, 40]
#   receivingYards [42, 61]     receivingReceptions [41, 53]
#   defensivePointsAllowed [120, 187]   defensive2PtReturns [205, 206]
#
# Collapsing to a single id (either the first or the last) silently scores one
# of those categories as zero, depending on which id the league happens to use.
# That reads as "yardage just isn't worth much here" rather than as a bug. So
# we keep every candidate and resolve against the league's own rules at score
# time — see ScoringSettings._resolve.
_STAT_NAME_TO_IDS: dict[str, tuple[int, ...]] = {}
for _sid, _name in PLAYER_STATS_MAP.items():
    if isinstance(_sid, int):
        _STAT_NAME_TO_IDS[_name] = tuple(sorted((*_STAT_NAME_TO_IDS.get(_name, ()), _sid)))
del _sid, _name

# 99 of the 235 scoring categories are "every N yards / N receptions" buckets
# (RY10 = 'Every 10 rushing yards', REY25, PY20, ...) and have no entry in
# PLAYER_STATS_MAP, so espn-api leaves them in the breakdown under their raw
# numeric id rather than a name.
#
# They are NOT something we have to derive: ESPN pre-computes them and ships
# them in the stat line alongside the base stat. Verified against a real 2025
# game — 67 rushing yards arrives as {'rushingYards': 67, '27': 13, '28': 6,
# '29': 3, '30': 2, '31': 1}, i.e. floor(yards / N) for N in 5/10/20/25/50.
#
# So scoring a bucket league needs no special handling at all; the numeric keys
# resolve through stat_ids_for() like any other id. This matters because one of
# the three leagues here (gladiator) scores yardage exclusively in buckets and
# has no per-yard rule whatsoever.
#
# espn-api's stat name for a reception. Not "receptions" — worth a constant so
# the PPR helpers below don't silently return 0.0 if the name ever changes.
RECEPTIONS = "receivingReceptions"

# Per-yard stat id, then the bucket ids and how many yards each bucket is worth.
# Used to express any league's yardage scoring as a comparable points-per-yard
# plus a granularity, so "0.1/yd continuous" and "1 pt per 10 yds" can be told
# apart — they average the same but behave very differently at the margin.
_YARDAGE_SCHEMES: dict[str, tuple[int, dict[int, int]]] = {
    "passing": (3, {5: 5, 6: 10, 7: 20, 8: 25, 9: 50}),
    "rushing": (24, {27: 5, 28: 10, 29: 20, 30: 25, 31: 50}),
    "receiving": (42, {47: 5, 48: 10, 49: 20, 50: 25, 51: 50}),
}


def stat_ids_for(key: int | str) -> tuple[int, ...]:
    """Candidate stat ids for a key (id, numeric string, or espn-api stat name).

    Returns more than one only for the ambiguous names listed above.
    """
    if isinstance(key, int):
        return (key,)
    if key.isdigit():
        return (int(key),)
    return _STAT_NAME_TO_IDS.get(key, ())


@dataclass(frozen=True)
class ScoringRule:
    stat_id: int
    abbr: str
    label: str
    points: float
    #: defaultPositionId -> points, for TE premium and similar
    overrides: Mapping[int, float] = field(default_factory=dict)
    is_reverse: bool = False

    def points_for(self, position: str | int | None = None) -> float:
        """Points per unit, honouring a position-specific override if present."""
        if position is None or not self.overrides:
            return self.points
        pos_id = position if isinstance(position, int) else DEFAULT_POSITION_IDS.get(position)
        if pos_id is None:
            return self.points
        return self.overrides.get(pos_id, self.points)


@dataclass(frozen=True)
class ScoreResult:
    """The outcome of re-scoring a stat line, with its own caveats attached."""

    points: float
    #: stat abbreviation -> points contributed, non-zero only
    components: dict[str, float]

    def __float__(self) -> float:
        return self.points


@dataclass(frozen=True)
class ScoringSettings:
    league_key: str
    rules: Mapping[int, ScoringRule]
    scoring_type: str = ""
    home_team_bonus: float = 0.0

    # -- construction --------------------------------------------------------

    @classmethod
    def from_raw(cls, raw_settings: dict[str, Any], league_key: str = "") -> ScoringSettings:
        """Parse ESPN's ``settings`` blob (the whole thing, or just
        ``scoringSettings``)."""
        scoring = raw_settings.get("scoringSettings", raw_settings)
        rules: dict[int, ScoringRule] = {}

        for item in scoring.get("scoringItems", []) or []:
            stat_id = item.get("statId")
            if stat_id is None:
                continue
            meta = SETTINGS_SCORING_FORMAT_MAP.get(stat_id) or {}
            overrides = {
                int(k): float(v)
                for k, v in (item.get("pointsOverrides") or {}).items()
                if str(k).lstrip("-").isdigit()
            }
            rules[stat_id] = ScoringRule(
                stat_id=stat_id,
                # dict(meta) copies — never hold a reference into the shared
                # constant, which is the whole bug we're routing around.
                abbr=str(meta.get("abbr", f"STAT{stat_id}")),
                label=str(meta.get("label", f"stat {stat_id}")),
                points=float(item.get("points", 0.0) or 0.0),
                overrides=overrides,
                is_reverse=bool(item.get("isReverseItem", False)),
            )

        return cls(
            league_key=league_key,
            rules=rules,
            scoring_type=str(scoring.get("scoringType", "") or ""),
            home_team_bonus=float(scoring.get("homeTeamBonus", 0.0) or 0.0),
        )

    @classmethod
    def from_league(cls, league: Any, league_key: str = "") -> ScoringSettings:
        """Parse straight from a live ``espn_api`` League, bypassing its own
        (buggy) ``settings.scoring_format``."""
        raw = league.espn_request.get_league()
        return cls.from_raw(raw.get("settings", {}), league_key=league_key)

    # -- scoring -------------------------------------------------------------

    def score(
        self,
        stat_line: Mapping[int | str, float] | None,
        position: str | int | None = None,
    ) -> ScoreResult:
        """Re-score a raw stat line under this league's rules.

        ``stat_line`` may be keyed by stat id, numeric string, or espn-api stat
        name — ``player.stats[week]['breakdown']`` works as-is.

        Stats with no matching rule contribute nothing, which is correct: a
        league that doesn't score pass attempts should ignore pass attempts.
        Bucket categories (RY10 and friends) arrive pre-computed from ESPN
        under their numeric id and need no special handling.
        """
        if not stat_line:
            return ScoreResult(0.0, {})

        total = 0.0
        components: dict[str, float] = {}

        for key, value in stat_line.items():
            stat_id = self._resolve(key)
            if stat_id is None:
                continue
            rule = self.rules.get(stat_id)
            if rule is None or not value:
                continue
            contribution = rule.points_for(position) * float(value)
            if contribution:
                total += contribution
                components[rule.abbr] = round(components.get(rule.abbr, 0.0) + contribution, 4)

        return ScoreResult(round(total, 2), components)

    # -- introspection -------------------------------------------------------

    def _resolve(self, key: int | str) -> int | None:
        """Pick the stat id this league actually scores, for ambiguous names."""
        candidates = stat_ids_for(key)
        for candidate in candidates:
            if candidate in self.rules:
                return candidate
        return candidates[0] if candidates else None

    def rule(self, key: int | str) -> ScoringRule | None:
        stat_id = self._resolve(key)
        return self.rules.get(stat_id) if stat_id is not None else None

    def points_per(self, key: int | str, position: str | int | None = None) -> float:
        rule = self.rule(key)
        return rule.points_for(position) if rule else 0.0

    @property
    def ppr(self) -> float:
        """Points per reception for a WR. The single most useful summary number."""
        return self.points_per(RECEPTIONS, "WR")

    @property
    def te_premium(self) -> float:
        """Extra points per reception a TE gets over a WR. 0.0 in most leagues."""
        return round(self.points_per(RECEPTIONS, "TE") - self.ppr, 3)

    @property
    def passing_td_points(self) -> float:
        return self.points_per("passingTouchdowns", "QB")

    def yardage_scoring(self) -> dict[str, dict[str, float]]:
        """How each yardage type is scored, normalized for cross-league compare.

        ``per_yard`` is the effective long-run rate; ``granularity`` is how many
        yards it takes to earn anything. Granularity 1 is continuous scoring;
        anything higher is stepwise, which is a real strategic difference —
        under 'a point per 10 rushing yards', yards 61-69 are worth exactly
        nothing, so a back on a 65-yard pace is a materially worse play than
        his projection suggests, and one on 68 is a coin-flip on a single
        carry. Phase 2's floor/median/ceiling has to account for this.
        """
        out: dict[str, dict[str, float]] = {}
        for kind, (base_id, buckets) in _YARDAGE_SCHEMES.items():
            base = self.rules.get(base_id)
            if base and base.points:
                out[kind] = {"per_yard": round(base.points, 4), "granularity": 1}
                continue
            for stat_id, yards in buckets.items():
                rule = self.rules.get(stat_id)
                if rule and rule.points:
                    out[kind] = {
                        "per_yard": round(rule.points / yards, 4),
                        "granularity": yards,
                    }
                    break
        return out

    @property
    def has_bucket_yardage(self) -> bool:
        return any(v["granularity"] > 1 for v in self.yardage_scoring().values())

    def format_label(self) -> str:
        """Short human label: 'PPR', 'Half-PPR', 'Standard', with modifiers."""
        ppr = self.ppr
        base = {1.0: "PPR", 0.5: "Half-PPR", 0.0: "Standard"}.get(ppr, f"{ppr}-PPR")
        bits = [base]
        if self.te_premium:
            bits.append(f"TE+{self.te_premium}")
        if self.passing_td_points and self.passing_td_points != 4.0:
            bits.append(f"{self.passing_td_points:g}pt passTD")
        if self.has_bucket_yardage:
            steps = sorted(
                {int(v["granularity"]) for v in self.yardage_scoring().values() if v["granularity"] > 1}
            )
            bits.append("stepwise yardage (" + "/".join(f"per {s}yd" for s in steps) + ")")
        return ", ".join(bits)

    def summary(self) -> dict[str, Any]:
        """Compact, model-friendly description. Kept small on purpose — the
        full 200-rule table is never worth the context."""
        notable = {r.abbr: r.points_for() for r in self.rules.values() if r.points}
        out: dict[str, Any] = {
            "format": self.format_label(),
            "ppr": self.ppr,
            "te_premium": self.te_premium or None,
            "passing_td": self.passing_td_points,
            "scoring_type": self.scoring_type,
            "rules_total": len(self.rules),
            "yardage": self.yardage_scoring(),
            "notable": dict(sorted(notable.items(), key=lambda kv: -abs(kv[1]))[:24]),
        }
        return out
