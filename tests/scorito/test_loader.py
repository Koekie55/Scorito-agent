"""Loader tests against the committed TdF 2026 snapshot.

These assert the raw Scorito API snapshot parses into the normalized domain
objects with the expected shapes and ground-truth values (decoded during the
data-preservation work — see ``markets_registry.json``).
"""

from pathlib import Path

import pytest

from scorito_agent.scorito import load_snapshot

DATA_ROOT = Path(__file__).parents[2] / "data" / "scorito"
TDF = DATA_ROOT / "tdf2026"

pytestmark = pytest.mark.skipif(
    not TDF.exists(), reason="tdf2026 snapshot not present"
)

# Ground-truth constants for TdF 2026 (market 309).
# The leaderboard total reconciles as: per-stage points + classification bonus.
POGACAR_ID = 6432
POGACAR_STAGE_TOTAL = 878.0  # sum of 21 per-stage points (points_totalpoints)
POGACAR_CLASSIFICATION = 179.0  # end-of-race GC/jersey bonus (points_market)
POGACAR_SEASON_TOTAL = 1057.0  # leaderboard total (marketpoints), Rank 1
POGACAR_STAGE1_POINTS = 42.0  # opening TTT


@pytest.fixture(scope="module")
def snap():
    return load_snapshot("tdf2026")


def test_snapshot_headline_numbers(snap) -> None:
    assert snap.market_id == 309
    assert snap.slug == "tdf2026"
    assert snap.budget == 45_000_000
    assert snap.budget_m == 45.0
    assert snap.captain_factor == 2
    assert len(snap.riders) == 206
    assert len(snap.stages) == 21


def test_stages_sorted_by_order(snap) -> None:
    orders = [s.order for s in snap.stages]
    assert orders == sorted(orders)
    assert orders[0] == 1
    # Stage 1 is a team time trial for TdF 2026.
    assert snap.stages[0].is_ttt


def test_itt_stage_present(snap) -> None:
    itt = [s for s in snap.stages if s.is_itt]
    assert len(itt) == 1  # stage 16 individual time trial


def test_rider_lookup_and_qualities(snap) -> None:
    pog = snap.rider(POGACAR_ID)
    assert pog is not None
    assert "Pogačar" in pog.name
    assert pog.price > 0
    # Pogačar is a GC leader with strong climbing/GC qualities.
    assert pog.quality(0) > 0  # GC
    assert pog.quality(1) > 0  # Climbing


def test_actual_points_double_nested_parse(snap) -> None:
    """The double-nested RiderPointsCollection must be flattened correctly."""
    # Per-stage parse: the opening TTT is a known ground-truth value, and the
    # 21 stages sum to exactly the points_totalpoints basis (not the
    # leaderboard total — that also includes end-of-race classification bonuses).
    assert snap.actual_points(POGACAR_ID, snap.stages[0]) == pytest.approx(
        POGACAR_STAGE1_POINTS
    )
    stage_total = sum(snap.actual_points(POGACAR_ID, s) for s in snap.stages)
    assert stage_total == pytest.approx(POGACAR_STAGE_TOTAL)
    assert snap.stage_total(POGACAR_ID) == pytest.approx(POGACAR_STAGE_TOTAL)


def test_leaderboard_reconciles_stage_plus_classification(snap) -> None:
    """Leaderboard total = per-stage points + end-of-race classification bonus."""
    assert snap.classification_bonus(POGACAR_ID) == pytest.approx(
        POGACAR_CLASSIFICATION
    )
    # The two separate Scorito endpoints must add up to the leaderboard.
    assert snap.season_total(POGACAR_ID) == pytest.approx(POGACAR_SEASON_TOTAL)
    assert snap.market_totals[POGACAR_ID] == pytest.approx(
        snap.season_total(POGACAR_ID)
    )


def test_market_totals_leaderboard(snap) -> None:
    assert snap.market_totals.get(POGACAR_ID) == pytest.approx(
        POGACAR_SEASON_TOTAL
    )
