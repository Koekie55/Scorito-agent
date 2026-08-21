"""Scoring-model tests: heuristic relevance + calibrated StageScorer."""

from pathlib import Path

import pytest

from scorito_agent.scorito import (
    Rider,
    Stage,
    StageScorer,
    heuristic_score,
    load_snapshot,
    quality_relevance,
)
from scorito_agent.scorito.scoring import (
    Q_CLIMB,
    Q_GC,
    Q_PUNCH,
    Q_SPRINT,
    Q_TT,
)

DATA_ROOT = Path(__file__).parents[2] / "data" / "scorito"
TDF = DATA_ROOT / "tdf2026"


def _rider(name: str, qualities: dict[int, int]) -> Rider:
    return Rider(
        rider_id=abs(hash(name)) % 100000,
        event_rider_id=0,
        name=name,
        team_id=1,
        price=5_000_000,
        role=1,
        nationality="XX",
        age=27,
        qualities=qualities,
    )


def _stage(order: int, stage_type: int, terrain_type: int) -> Stage:
    return Stage(
        market_round_id=order,
        stage_id=order,
        order=order,
        stage_type=stage_type,
        terrain_type=terrain_type,
    )


def test_relevance_itt_favours_tt() -> None:
    rel = quality_relevance(_stage(16, stage_type=2, terrain_type=1))
    assert rel[Q_TT] == 1.0
    assert rel.get(Q_SPRINT, 0.0) == 0.0


def test_relevance_flat_favours_sprint() -> None:
    rel = quality_relevance(_stage(2, stage_type=1, terrain_type=1))
    assert rel[Q_SPRINT] == 1.0
    assert Q_CLIMB not in rel


def test_relevance_mountain_favours_climb_and_gc() -> None:
    rel = quality_relevance(_stage(6, stage_type=1, terrain_type=3))
    assert rel[Q_CLIMB] == 1.0
    assert rel[Q_GC] == 1.0
    assert rel.get(Q_SPRINT, 0.0) == 0.0


def test_heuristic_ranks_specialist_above_mismatch() -> None:
    flat = _stage(2, stage_type=1, terrain_type=1)
    mountain = _stage(6, stage_type=1, terrain_type=3)
    sprinter = _rider("Sprinter", {Q_SPRINT: 9, Q_PUNCH: 4})
    climber = _rider("Climber", {Q_CLIMB: 9, Q_GC: 8})

    # On a flat stage the sprinter should out-score the climber, and vice versa.
    assert heuristic_score(sprinter, flat) > heuristic_score(climber, flat)
    assert heuristic_score(climber, mountain) > heuristic_score(sprinter, mountain)


def test_heuristic_zero_when_no_relevant_quality() -> None:
    mountain = _stage(6, stage_type=1, terrain_type=3)
    pure_sprinter = _rider("PureSprinter", {Q_SPRINT: 9})
    assert heuristic_score(pure_sprinter, mountain) == 0.0


@pytest.mark.skipif(not TDF.exists(), reason="tdf2026 snapshot not present")
def test_stage_scorer_fits_and_predicts_nonnegative() -> None:
    snap = load_snapshot("tdf2026")
    scorer = StageScorer().fit(snap)
    pog = snap.rider(6432)
    stage0 = snap.stages[0]
    exp = scorer.expected(pog, stage0)
    assert exp >= 0.0
    # Season value is the sum of expected points across all stages.
    assert scorer.season_value(pog, snap) >= exp
