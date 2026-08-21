"""Tests for the parts-2/3 -> scorer integration adapter (external.py).

Two layers:

* **Synthetic-fixture unit tests** (no snapshot on disk): a tiny hand-built
  ``Snapshot`` with known per-stage points exercises the whole adapter surface
  — the empirical rank->points curve, its clamping/sparse lookup, the
  ``ExternalStageScorer`` drop-in, and the PCS/cyclingoracle normalisers.

* **"Perfect oracle" back-test** on the committed real ``tdf2026`` snapshot
  (skipped when absent): feed the snapshot's own derived finishing ranks back
  through the curve and confirm the reconstructed squad is a legal, in-budget
  20-rider team that lands near the hindsight ceiling — proving the
  rank -> points -> optimizer plumbing is wired end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scorito_agent.scorito.external import (
    ExternalStageScorer,
    RankPointsCurve,
    name_key,
    perfect_oracle_predictions,
    predictions_from_cyclingoracle,
    predictions_from_pcs,
)
from scorito_agent.scorito.models import Rider, Snapshot, Stage

REPO = Path(__file__).parents[2]
TDF = REPO / "data" / "scorito" / "tdf2026"


# --------------------------------------------------------------------------- #
# Synthetic fixture
# --------------------------------------------------------------------------- #


def _rider(rid: int, name: str) -> Rider:
    return Rider(
        rider_id=rid,
        event_rider_id=rid,
        name=name,
        team_id=1,
        price=1_000_000,
        role=1,
        nationality="XX",
        age=25,
        qualities={},
    )


ALPHA = _rider(1, "Rider Alpha")
BRAVO = _rider(2, "Rider Bravo")
CHARLIE = _rider(3, "Rider Charlie")
DELTA = _rider(4, "Rider Delta")

# Two road stages + one ITT stage.
STAGE_R1 = Stage(market_round_id=101, stage_id=1001, order=1, stage_type=1, terrain_type=1)
STAGE_R2 = Stage(market_round_id=102, stage_id=1002, order=2, stage_type=1, terrain_type=1)
STAGE_ITT = Stage(market_round_id=103, stage_id=1003, order=3, stage_type=2, terrain_type=1)


def _synthetic_snapshot() -> Snapshot:
    # Hand-set per-stage points. Riders with 0 points are simply absent.
    stage_points = {
        # Road R1: Alpha 60 > Bravo 40 > Charlie 20
        (101, 1): 60.0, (101, 2): 40.0, (101, 3): 20.0,
        # Road R2: Bravo 50 > Alpha 30 > Charlie 10
        (102, 2): 50.0, (102, 1): 30.0, (102, 3): 10.0,
        # ITT: Alpha 65 > Charlie 45 > Bravo 25
        (103, 1): 65.0, (103, 3): 45.0, (103, 2): 25.0,
    }
    return Snapshot(
        market_id=999,
        slug="synthetic",
        budget=4_000_000,
        captain_factor=2,
        riders=[ALPHA, BRAVO, CHARLIE, DELTA],
        stages=[STAGE_R1, STAGE_R2, STAGE_ITT],
        stage_points=stage_points,
    )


# --------------------------------------------------------------------------- #
# name_key
# --------------------------------------------------------------------------- #


def test_name_key_normalises_accents_and_punctuation() -> None:
    assert name_key("Tadej Pogačar") == "tadejpogacar"
    assert name_key("rider-charlie") == "ridercharlie"
    assert name_key("Rider Charlie") == "ridercharlie"
    assert name_key("") == ""
    assert name_key(None) == ""


# --------------------------------------------------------------------------- #
# RankPointsCurve
# --------------------------------------------------------------------------- #


def test_curve_from_snapshot_is_monotonic_per_type() -> None:
    curve = RankPointsCurve.from_snapshot(_synthetic_snapshot())

    # Both stage types present.
    assert set(curve.curves) == {1, 2}

    # Road: r1=(60+50)/2=55, r2=(40+30)/2=35, r3=(20+10)/2=15
    road = curve.curves[1]
    assert road == {1: 55.0, 2: 35.0, 3: 15.0}

    # ITT (single stage): r1=65, r2=45, r3=25
    assert curve.curves[2] == {1: 65.0, 2: 45.0, 3: 25.0}

    # Monotonic non-increasing by rank for every stage type.
    for per_rank in curve.curves.values():
        vals = [per_rank[r] for r in sorted(per_rank)]
        assert vals == sorted(vals, reverse=True)


def test_curve_enforces_monotonicity_across_stage_averages() -> None:
    # Cross-stage averaging can invert the raw curve: here rank 1 is sampled on
    # a sparse stage (only 10 pts) and a rich one (100 pts) -> mean 55, while
    # rank 2 is only sampled on the rich stage -> mean 90. Without the clamp
    # rank 2 (55<90) would out-pay rank 1; from_snapshot must flatten it.
    snap = Snapshot(
        market_id=1,
        slug="noisy",
        budget=1,
        captain_factor=2,
        riders=[ALPHA, BRAVO, CHARLIE],
        stages=[STAGE_R1, STAGE_R2],
        stage_points={
            (101, 1): 10.0,                    # R1: only Alpha scores -> rank1=10
            (102, 2): 100.0, (102, 3): 90.0,   # R2: Bravo rank1=100, Charlie rank2=90
        },
    )
    curve = RankPointsCurve.from_snapshot(snap)
    road = curve.curves[1]
    # rank1 mean=(10+100)/2=55; rank2 raw mean=90 -> clamped down to 55.
    assert road == {1: 55.0, 2: 55.0}
    assert road[1] >= road[2]


def test_points_for_clamps_low_and_high_ranks() -> None:
    curve = RankPointsCurve.from_snapshot(_synthetic_snapshot())

    # rank < 1 clamps to rank 1.
    assert curve.points_for(1, 0) == 55.0
    assert curve.points_for(1, -3) == 55.0
    assert curve.points_for(1, 1) == 55.0
    assert curve.points_for(1, 2) == 35.0
    assert curve.points_for(1, 3) == 15.0

    # rank beyond the last points-paying position -> 0.
    assert curve.points_for(1, 4) == 0.0
    assert curve.points_for(1, 999) == 0.0

    # None -> 0.
    assert curve.points_for(1, None) == 0.0


def test_points_for_sparse_curve_uses_nearest_lower_rank() -> None:
    # Manually-built curve with a gap at rank 2.
    curve = RankPointsCurve(curves={1: {1: 100.0, 3: 50.0}})
    assert curve.points_for(1, 1) == 100.0
    assert curve.points_for(1, 2) == 100.0  # nearest known rank <= 2 is rank 1
    assert curve.points_for(1, 3) == 50.0
    assert curve.points_for(1, 4) == 0.0  # beyond max known rank


def test_points_for_stage_type_fallback() -> None:
    # A curve that only knows road (type 1) still scores an ITT via fallback.
    road_only = RankPointsCurve(curves={1: {1: 10.0, 2: 5.0}})
    assert road_only.points_for(2, 1) == 10.0  # ITT falls back to road curve

    # A curve with no road and no requested type falls back to *any* curve.
    other_only = RankPointsCurve(curves={5: {1: 7.0}})
    assert other_only.points_for(2, 1) == 7.0

    # Empty curve -> 0.
    assert RankPointsCurve().points_for(1, 1) == 0.0


# --------------------------------------------------------------------------- #
# ExternalStageScorer
# --------------------------------------------------------------------------- #


def test_external_scorer_matched_rider_uses_curve() -> None:
    snap = _synthetic_snapshot()
    curve = RankPointsCurve.from_snapshot(snap)
    preds = {
        STAGE_R1.stage_id: {
            name_key("Rider Alpha"): 1,
            name_key("Rider Charlie"): 2,
        }
    }
    scorer = ExternalStageScorer(preds, curve)

    assert scorer.expected(ALPHA, STAGE_R1) == 55.0  # curve rank 1
    assert scorer.expected(CHARLIE, STAGE_R1) == 35.0  # curve rank 2
    # Bravo not predicted on this stage, no fallback -> 0.
    assert scorer.expected(BRAVO, STAGE_R1) == 0.0
    # No predictions at all for R2 -> everyone 0.
    assert scorer.expected(ALPHA, STAGE_R2) == 0.0

    # season_value sums expected over every stage (only R1 has predictions).
    assert scorer.season_value(ALPHA, snap) == 55.0
    assert scorer.season_value(CHARLIE, snap) == 35.0
    assert scorer.season_value(BRAVO, snap) == 0.0


def test_external_scorer_uses_fallback_for_unmatched() -> None:
    snap = _synthetic_snapshot()
    curve = RankPointsCurve.from_snapshot(snap)

    class ConstFallback:
        def expected(self, rider: Rider, stage: Stage) -> float:
            return 9.0

        def season_value(self, rider: Rider, snapshot: Snapshot) -> float:
            return 9.0 * len(snapshot.stages)

    preds = {STAGE_R1.stage_id: {name_key("Rider Alpha"): 1}}
    scorer = ExternalStageScorer(preds, curve, fallback=ConstFallback())

    # Matched rider still uses the curve.
    assert scorer.expected(ALPHA, STAGE_R1) == 55.0
    # Unmatched rider defers to the fallback.
    assert scorer.expected(BRAVO, STAGE_R1) == 9.0


# --------------------------------------------------------------------------- #
# Prediction normalisers
# --------------------------------------------------------------------------- #


def test_predictions_from_pcs_shape_and_enumeration_fallback() -> None:
    pcs = {
        1001: {
            "predictions": [
                {"rider": "Rider Alpha", "rider_slug": "rider-alpha", "predicted_rank": 1},
                {"rider": "Rider Bravo", "rider_slug": "rider-bravo", "predicted_rank": 2},
            ]
        },
        # No predicted_rank -> rank derived from list order.
        1002: {"predictions": [{"rider": "Rider Charlie"}]},
    }
    out = predictions_from_pcs(pcs)
    assert out == {
        1001: {"rideralpha": 1, "riderbravo": 2},
        1002: {"ridercharlie": 1},
    }


def test_predictions_from_cyclingoracle_shape_and_name_keys() -> None:
    co = {
        1001: [
            {"rider": "Rider Alpha", "model_rank": 1},
            {"name": "Rider Bravo", "model_rank": 3},
        ],
        # rider_slug fallback + enumeration rank.
        1002: [{"rider_slug": "rider-charlie"}],
    }
    out = predictions_from_cyclingoracle(co)
    assert out == {
        1001: {"rideralpha": 1, "riderbravo": 3},
        1002: {"ridercharlie": 1},
    }


def test_perfect_oracle_predictions_recovers_ranks() -> None:
    snap = _synthetic_snapshot()
    preds = perfect_oracle_predictions(snap)

    # Road R1: Alpha 60 > Bravo 40 > Charlie 20.
    assert preds[STAGE_R1.stage_id] == {
        name_key("Rider Alpha"): 1,
        name_key("Rider Bravo"): 2,
        name_key("Rider Charlie"): 3,
    }
    # ITT: Alpha 65 > Charlie 45 > Bravo 25.
    assert preds[STAGE_ITT.stage_id] == {
        name_key("Rider Alpha"): 1,
        name_key("Rider Charlie"): 2,
        name_key("Rider Bravo"): 3,
    }
    # Delta never scores -> never appears.
    for ranks in preds.values():
        assert name_key("Rider Delta") not in ranks


def test_perfect_oracle_round_trips_through_the_scorer() -> None:
    # Feeding derived ranks back through the curve should reproduce each
    # rider's mean-curve season value and preserve the ranking order.
    snap = _synthetic_snapshot()
    curve = RankPointsCurve.from_snapshot(snap)
    preds = perfect_oracle_predictions(snap)
    scorer = ExternalStageScorer(preds, curve)

    va = scorer.season_value(ALPHA, snap)
    vb = scorer.season_value(BRAVO, snap)
    vc = scorer.season_value(CHARLIE, snap)
    vd = scorer.season_value(DELTA, snap)

    # Alpha is best or tied-best everywhere -> highest value; Delta scores 0.
    assert va > 0 and vd == 0.0
    assert va >= vb and va >= vc


# --------------------------------------------------------------------------- #
# "Perfect oracle" back-test on the real snapshot
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not TDF.exists(), reason="tdf2026 snapshot not present")
def test_perfect_oracle_back_test_recovers_near_ceiling() -> None:
    from scorito_agent.scorito.loader import load_snapshot
    from scorito_agent.scorito.optimizer import (
        back_analysis,
        expected_total_values,
        optimal_hindsight_squad,
        pick_squad,
    )

    snap = load_snapshot("tdf2026")

    curve = RankPointsCurve.from_snapshot(snap)
    preds = perfect_oracle_predictions(snap)
    scorer = ExternalStageScorer(preds, curve)

    values = expected_total_values(snap, scorer)
    plan = pick_squad(snap, values)

    # Legal squad: 20 riders, no duplicates, within budget.
    assert len(plan.rider_ids) == 20
    assert len(set(plan.rider_ids)) == 20
    assert plan.total_price <= snap.budget

    # Real season total achievable with the recovered squad.
    realised = back_analysis(snap, plan.rider_ids).season_total
    ceiling = optimal_hindsight_squad(snap).season_total
    assert ceiling and ceiling > 0

    # The oracle-derived squad should land near the hindsight ceiling.
    assert realised >= 0.80 * ceiling, (
        f"perfect-oracle squad {realised:.0f} < 80% of ceiling {ceiling:.0f}"
    )
