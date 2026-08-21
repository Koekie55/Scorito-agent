"""Normalized domain objects for the Scorito cycling game.

These wrap the raw Scorito API snapshot (``data/scorito/<slug>/``) in typed,
convenient objects for the scoring model and the ILP optimiser.

Enum meaning (authoritative map from the jvdlaar/scorito PHP source, validated
against the TdF 2026 market 309 snapshot — see
``data/scorito/markets_registry.json`` ``enums`` / ``points_schema``):

Rider ``Type`` (role): 0=Other, 1=GC, 2=Climber, 3=TT, 4=Sprinter,
5=Attacker, 6=Support, 7=Cobbles, 8=Hills. (The TdF grand-tour snapshot only
contains 1-6; 0/7/8 appear in classics markets.) Validated against named
riders: Type 1 = Pogačar/Vingegaard (GC), 2 = Kuss/Arensman (climbers),
3 = Ganna/Tarling (TT), 4 = Philipsen (sprinter), 5 = van der Poel/van Aert
(attackers), 6 = 142 domestiques (support). Role is display-only — the scoring
model and optimiser key off ``Qualities`` and stage terrain, never ``Type``.

Rider ``Qualities[].Type`` (skill, Value 2-10): 0=GC, 1=Climb, 2=Time trial,
3=Sprint, 4=Punch, 5=Hill, 6=Cobbles.

Stage ``StageType``: 1=Road, 2=ITT, 3=TTT.
Stage ``TerrainType``: 1=Flat, 2=Hilly, 3=Mountain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- Enum labels (kept in code so the module is self-describing) ------------

ROLE_LABELS = {
    0: "Other",
    1: "GC",
    2: "Climber",
    3: "TT",
    4: "Sprinter",
    5: "Attacker",
    6: "Support",
    7: "Cobbles",
    8: "Hills",
}

QUALITY_LABELS = {
    0: "GC",
    1: "Climb",
    2: "Time trial",
    3: "Sprint",
    4: "Punch",
    5: "Hill",
    6: "Cobbles",
}

STAGE_TYPE_LABELS = {1: "Road", 2: "ITT", 3: "TTT"}
TERRAIN_LABELS = {1: "Flat", 2: "Hilly", 3: "Mountain"}


@dataclass(frozen=True)
class Rider:
    """A single rider in a Scorito market, with price and quality ratings."""

    rider_id: int
    event_rider_id: int
    name: str
    team_id: int
    price: int
    role: int
    nationality: str
    age: int | None
    qualities: dict[int, int] = field(default_factory=dict)

    def quality(self, qtype: int) -> int:
        """Rating (0 if the rider has no such quality)."""
        return self.qualities.get(qtype, 0)

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, f"role{self.role}")

    @property
    def price_m(self) -> float:
        return self.price / 1_000_000

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Rider({self.name!r}, {self.price_m:.1f}M, {self.role_label})"


@dataclass(frozen=True)
class Stage:
    """One stage: links a market round to its route profile."""

    market_round_id: int
    stage_id: int
    order: int
    stage_type: int
    terrain_type: int

    @property
    def is_ttt(self) -> bool:
        return self.stage_type == 3

    @property
    def is_itt(self) -> bool:
        return self.stage_type == 2

    @property
    def is_road(self) -> bool:
        return self.stage_type == 1

    @property
    def stage_type_label(self) -> str:
        return STAGE_TYPE_LABELS.get(self.stage_type, f"type{self.stage_type}")

    @property
    def terrain_label(self) -> str:
        return TERRAIN_LABELS.get(self.terrain_type, f"terrain{self.terrain_type}")

    @property
    def label(self) -> str:
        return f"S{self.order:02d} {self.stage_type_label}/{self.terrain_label}"

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Stage({self.label}, stage_id={self.stage_id})"


@dataclass
class Snapshot:
    """A fully-loaded Scorito market snapshot."""

    market_id: int
    slug: str
    budget: int
    captain_factor: int
    riders: list[Rider]
    stages: list[Stage]
    # (market_round_id, rider_id) -> summed stage points (ground truth)
    stage_points: dict[tuple[int, int], float] = field(default_factory=dict)
    # rider_id -> market-wide total points (leaderboard)
    market_totals: dict[int, float] = field(default_factory=dict)
    # rider_id -> end-of-race classification/jersey bonus points.
    # These are awarded once at the end of the race (final GC, points, KOM,
    # youth jerseys, etc.) and are NOT attributed to any individual stage in
    # ``stage_points``. The leaderboard reconciles as::
    #     market_totals[r] == sum(stage_points over stages) + classification[r]
    classification_points: dict[int, float] = field(default_factory=dict)

    # -- convenience lookups -------------------------------------------------

    def rider(self, rider_id: int) -> Rider | None:
        return self._by_id.get(rider_id)

    def stage_by_order(self, order: int) -> Stage | None:
        for s in self.stages:
            if s.order == order:
                return s
        return None

    def actual_points(self, rider_id: int, stage: Stage) -> float:
        """Real Scorito points a rider scored on a stage (0 if none)."""
        return self.stage_points.get((stage.market_round_id, rider_id), 0.0)

    def stage_total(self, rider_id: int) -> float:
        """Sum of a rider's real per-stage points over all loaded stages."""
        return sum(self.actual_points(rider_id, s) for s in self.stages)

    def classification_bonus(self, rider_id: int) -> float:
        """End-of-race classification/jersey bonus points (0 if none)."""
        return self.classification_points.get(rider_id, 0.0)

    def season_total(self, rider_id: int) -> float:
        """Full leaderboard-equivalent total: stage points + classification.

        Reconstructs the market leaderboard total from its components so it
        matches ``market_totals`` even though the two come from separate
        Scorito endpoints.
        """
        return self.stage_total(rider_id) + self.classification_bonus(rider_id)

    @property
    def budget_m(self) -> float:
        return self.budget / 1_000_000

    def __post_init__(self) -> None:
        self._by_id: dict[int, Rider] = {r.rider_id: r for r in self.riders}
        self.stages.sort(key=lambda s: s.order)
