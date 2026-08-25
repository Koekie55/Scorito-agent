"""Optimizer tests: synthetic solver coverage + real-snapshot back-analysis.

The synthetic tests exercise the in-process scipy/HiGHS MILP path (the only
solver that works in this locked-down environment — CBC subprocess is blocked)
without needing the committed snapshot. The real-snapshot tests validate the
end-to-end hindsight benchmark and per-stage regret.
"""

from pathlib import Path

import pytest

from scorito_agent.scorito import (
    Rider,
    Snapshot,
    Stage,
    back_analysis,
    best_stage_lineup,
    joint_enrolled_squad,
    optimal_hindsight_squad,
    pick_squad,
    stage_regret,
)

DATA_ROOT = Path(__file__).parents[2] / "data" / "scorito"
TDF = DATA_ROOT / "tdf2026"


def _rider(rid: int, price: int, *, team_id: int = 1) -> Rider:
    return Rider(
        rider_id=rid,
        event_rider_id=rid,
        name=f"R{rid}",
        team_id=team_id,
        price=price,
        role=6,
        nationality="XX",
        age=27,
    )


def _snapshot(riders: list[Rider], *, budget: int) -> Snapshot:
    return Snapshot(
        market_id=0,
        slug="synthetic",
        budget=budget,
        captain_factor=2,
        riders=riders,
        stages=[Stage(1, 1, 1, 1, 1)],
    )


def test_pick_squad_selects_top_value_within_cardinality() -> None:
    # Uniform price; budget exactly fits the squad -> must take highest values.
    riders = [_rider(i, price=5_000_000) for i in range(30)]
    snap = _snapshot(riders, budget=50_000_000)
    values = {i: float(i) for i in range(30)}

    plan = pick_squad(snap, values, squad_size=10)

    assert len(plan.rider_ids) == 10
    assert plan.total_price <= snap.budget
    # Highest-value riders are ids 20..29.
    assert set(plan.rider_ids) == set(range(20, 30))
    assert plan.value == pytest.approx(sum(range(20, 30)))


def test_pick_squad_respects_budget_excludes_unaffordable() -> None:
    # One hugely valuable but unaffordable rider must be dropped.
    riders = [_rider(i, price=1_000_000) for i in range(9)]
    riders.append(_rider(99, price=100_000_000))  # too expensive to fit
    snap = _snapshot(riders, budget=10_000_000)
    values = {i: 1.0 for i in range(9)}
    values[99] = 1000.0

    plan = pick_squad(snap, values, squad_size=5)

    assert len(plan.rider_ids) == 5
    assert 99 not in plan.rider_ids
    assert plan.total_price <= snap.budget


def test_pick_squad_rejects_fewer_candidates_than_squad_size() -> None:
    riders = [_rider(i, price=1_000_000) for i in range(3)]
    snap = _snapshot(riders, budget=10_000_000)
    values = {i: 1.0 for i in range(3)}

    with pytest.raises(ValueError, match="cannot select 20 riders from 3"):
        pick_squad(snap, values, squad_size=20)


def test_snapshot_rejects_duplicate_rider_ids() -> None:
    with pytest.raises(ValueError, match=r"duplicate rider IDs in snapshot: \[1\]"):
        _snapshot([_rider(1, 1_000_000), _rider(1, 2_000_000)], budget=3_000_000)


def test_best_stage_lineup_applies_captain_factor() -> None:
    stage = Stage(1, 1, 1, 1, 1)
    squad = [1, 2, 3, 4]
    points = {1: 10.0, 2: 30.0, 3: 5.0, 4: 1.0}

    lu = best_stage_lineup(stage, squad, points, lineup_size=3, captain_factor=2)

    # Top 3 by points = {2,1,3}; captain = highest (rider 2).
    assert lu.captain_id == 2
    assert lu.captain_points == 30.0
    # base = 30+10+5 = 45; captain doubles the 30 -> +30 => 75.
    assert lu.total == pytest.approx(75.0)
    assert 4 not in lu.rider_ids


def test_best_stage_lineup_rejects_undersized_squad() -> None:
    with pytest.raises(ValueError, match="cannot select a 9-rider lineup"):
        best_stage_lineup(Stage(1, 1, 1, 1, 1), [1, 2, 3], {}, lineup_size=9)


def _multi_stage_snapshot() -> Snapshot:
    """Two-stage snapshot where specialists beat the season-total pick.

    Rider real points (stage1, stage2):
        1 (A) = (10, 10)  season 20, but never the best on either stage
        2 (B) = (15,  0)  stage-1 specialist
        3 (C) = ( 0, 15)  stage-2 specialist
        4 (D) = ( 5,  5)  filler
    With squad_size=2, lineup_size=1 you can only enrol one rider per stage,
    so the enrolled-aware optimum is {B, C} = 15 + 15 = 30, even though a
    season-total optimiser would rather buy A (the highest season total).
    """
    riders = [_rider(i, price=1_000_000) for i in (1, 2, 3, 4)]
    stages = [Stage(101, 1, 1, 1, 1), Stage(102, 2, 2, 1, 1)]
    stage_points = {
        (101, 1): 10.0, (102, 1): 10.0,
        (101, 2): 15.0, (102, 2): 0.0,
        (101, 3): 0.0, (102, 3): 15.0,
        (101, 4): 5.0, (102, 4): 5.0,
    }
    return Snapshot(
        market_id=0,
        slug="synthetic-multi",
        budget=10_000_000,
        captain_factor=1,  # captain doubling contributes 0 -> isolates enrolment
        riders=riders,
        stages=stages,
        stage_points=stage_points,
    )


def test_joint_enrolled_squad_prefers_stage_specialists() -> None:
    snap = _multi_stage_snapshot()

    plan = joint_enrolled_squad(
        snap, snap.actual_points, squad_size=2, lineup_size=1
    )

    assert plan is not None
    assert set(plan.rider_ids) == {2, 3}  # the two specialists, not season-leader A
    assert plan.value == pytest.approx(30.0)
    assert plan.total_price <= snap.budget
    # A season-total squad (buys rider 1) enrols to strictly less than the
    # enrolled-aware optimum under the SAME lineup_size, proving the joint
    # model is doing something extra than just maximising season totals.
    season_plan = pick_squad(
        snap, {r.rider_id: snap.stage_total(r.rider_id) for r in snap.riders},
        squad_size=2,
    )
    season_enrolled = sum(
        best_stage_lineup(
            stage,
            season_plan.rider_ids,
            {rid: snap.actual_points(rid, stage) for rid in season_plan.rider_ids},
            lineup_size=1,
            captain_factor=snap.captain_factor,
        ).total
        for stage in snap.stages
    )
    assert plan.value > season_enrolled


def test_joint_enrolled_squad_includes_selection_only_bonus() -> None:
    snap = _multi_stage_snapshot()

    plan = joint_enrolled_squad(
        snap,
        snap.actual_points,
        squad_size=2,
        lineup_size=1,
        selection_values={4: 30.0},
    )

    assert plan is not None
    assert 4 in plan.rider_ids
    assert plan.value == pytest.approx(50.0)


def test_joint_enrolled_squad_keeps_exact_lineup_for_all_zero_points() -> None:
    riders = [_rider(rid, 1_000_000, team_id=rid) for rid in range(1, 5)]
    snap = _snapshot(riders, budget=10_000_000)

    plan = joint_enrolled_squad(
        snap,
        lambda _rider_id, _stage: 0.0,
        squad_size=3,
        lineup_size=2,
    )

    assert plan is not None
    assert len(plan.rider_ids) == 3
    assert plan.value == pytest.approx(0.0)


def test_joint_enrolled_squad_keeps_exact_lineup_for_negative_points() -> None:
    riders = [_rider(rid, 1_000_000, team_id=rid) for rid in range(1, 5)]
    snap = _snapshot(riders, budget=10_000_000)

    plan = joint_enrolled_squad(
        snap,
        lambda rider_id, _stage: -float(rider_id),
        squad_size=3,
        lineup_size=2,
    )

    assert plan is not None
    assert len(plan.rider_ids) == 3
    assert plan.value == pytest.approx(-4.0)


def test_joint_enrolled_squad_returns_none_when_too_few_riders() -> None:
    riders = [_rider(1, price=1_000_000)]
    snap = Snapshot(
        market_id=0,
        slug="synthetic-tiny",
        budget=10_000_000,
        captain_factor=2,
        riders=riders,
        stages=[Stage(101, 1, 1, 1, 1)],
        stage_points={(101, 1): 10.0},
    )

    assert joint_enrolled_squad(
        snap, snap.actual_points, squad_size=2, lineup_size=1
    ) is None


def test_joint_enrolled_squad_enforces_trade_team_cap() -> None:
    riders = [
        *[_rider(rid, 1_000_000, team_id=1) for rid in (1, 2, 3, 4)],
        *[_rider(rid, 1_000_000, team_id=2) for rid in (5, 6, 7, 8)],
    ]
    snap = _snapshot(riders, budget=10_000_000)
    values = {rid: 100.0 - rid for rid in range(1, 9)}

    plan = joint_enrolled_squad(
        snap,
        lambda rider_id, _stage: values[rider_id],
        squad_size=4,
        lineup_size=4,
        max_riders_per_team=2,
    )

    assert plan is not None
    team_counts = {
        team_id: sum(snap.rider(rider_id).team_id == team_id for rider_id in plan.rider_ids)
        for team_id in (1, 2)
    }
    assert team_counts == {1: 2, 2: 2}


def test_joint_enrolled_squad_enforces_minimum_coverage() -> None:
    riders = [_rider(rid, 1_000_000, team_id=rid) for rid in range(1, 9)]
    snap = _snapshot(riders, budget=10_000_000)
    values = {rid: 100.0 - rid for rid in range(1, 9)}
    sprint_ids = {6, 7, 8}

    plan = joint_enrolled_squad(
        snap,
        lambda rider_id, _stage: values[rider_id],
        squad_size=4,
        lineup_size=4,
        coverage_constraints=[(sprint_ids, 2)],
    )

    assert plan is not None
    assert len(set(plan.rider_ids) & sprint_ids) >= 2


def test_joint_enrolled_squad_excludes_rider() -> None:
    riders = [_rider(rid, 1_000_000, team_id=rid) for rid in range(1, 7)]
    snap = _snapshot(riders, budget=10_000_000)

    plan = joint_enrolled_squad(
        snap,
        lambda rider_id, _stage: 1_000.0 if rider_id == 1 else float(rider_id),
        squad_size=3,
        lineup_size=3,
        excluded_rider_ids={1},
    )

    assert plan is not None
    assert 1 not in plan.rider_ids


@pytest.mark.skipif(not TDF.exists(), reason="tdf2026 snapshot not present")
class TestRealSnapshot:
    @staticmethod
    @pytest.fixture(scope="class")
    def snap():
        from scorito_agent.scorito import load_snapshot

        return load_snapshot("tdf2026")

    def test_optimal_hindsight_squad(self, snap) -> None:
        plan = optimal_hindsight_squad(snap)
        assert len(plan.rider_ids) == 20
        assert plan.total_price <= snap.budget
        assert plan.season_total is not None and plan.season_total > 0
        assert plan.lineups is not None and len(plan.lineups) == len(snap.stages)
        # Every stage lineup enrols exactly 9 riders.
        assert all(len(lu.rider_ids) == 9 for lu in plan.lineups)

    def test_back_analysis_matches_hindsight(self, snap) -> None:
        plan = optimal_hindsight_squad(snap)
        redo = back_analysis(snap, plan.rider_ids)
        assert redo.season_total == pytest.approx(plan.season_total)

    def test_stage_regret_non_negative(self, snap) -> None:
        plan = optimal_hindsight_squad(snap)
        stage = snap.stages[0]
        owned, market, gap = stage_regret(snap, plan.rider_ids, stage)
        assert owned is not None
        assert market is not None
        assert gap >= 0.0

    def test_joint_enrolled_squad_beats_season_total_ceiling(self, snap) -> None:
        plan = joint_enrolled_squad(snap, snap.actual_points)
        assert plan is not None
        assert len(plan.rider_ids) == 20
        assert plan.total_price <= snap.budget
        # The enrolled-aware ceiling must beat the season-total-optimal squad
        # enrolled the same way (the +665 finding: ~7675 vs ~7010).
        season_ceiling = optimal_hindsight_squad(snap).season_total
        assert plan.value >= season_ceiling
        # Re-enrolling the joint squad reproduces its own objective value.
        redo = back_analysis(snap, plan.rider_ids)
        assert redo.season_total == pytest.approx(plan.value)
