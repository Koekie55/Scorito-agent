import csv
from pathlib import Path

import pytest

from scorito_agent.breakaway import (
    climber_break_dependence,
    historical_breakaway_prior,
    summit_breakaway_rider_factor,
)
from scripts.project_vuelta import _field_percentile, _stage_signal_components

ROOT = Path(__file__).resolve().parents[1]


def test_vuelta_stage_three_prior_uses_smoothed_unipuerto_history_only() -> None:
    with (ROOT / "data" / "pcs" / "gt_summit_breakaway_labels.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        records = list(csv.DictReader(handle))

    prior = historical_breakaway_prior(
        records,
        {
            "stage_no": 3,
            "profile_type": "mountain",
            "finish_type": "summit",
            "vertical_meters": 2639,
        },
    )

    assert prior["global_rate"] == pytest.approx(0.5)
    assert prior["early_rate"] == pytest.approx(4 / 9)
    assert prior["unipuerto_rate"] == pytest.approx(3 / 9)
    assert prior["probability"] == pytest.approx(5 / 12)
    assert prior["early_total"] == 5
    assert prior["unipuerto_total"] == 5

def test_stage_number_does_not_change_prior_when_early_effect_fails_holdout() -> None:
    with (ROOT / "data" / "pcs" / "gt_summit_breakaway_labels.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        records = list(csv.DictReader(handle))

    stage = {
        "stage_no": 3,
        "profile_type": "mountain",
        "finish_type": "summit",
        "vertical_meters": 2639,
    }
    early = historical_breakaway_prior(records, stage)
    later = historical_breakaway_prior(records, {**stage, "stage_no": 19})

    assert early["early_rate"] != early["global_rate"]
    assert early["probability"] == later["probability"]



def _summit_signals() -> dict[str, dict[str, float]]:
    return {
        "gc": {"target": 0.30, "a": 0.90, "b": 0.80, "c": 0.70, "d": 0.10},
        "climb": {"target": 0.90, "a": 0.40, "b": 0.30, "c": 0.20, "d": 0.10},
        # Target is a strong climber but a weak time-trialist, so the GC-defence
        # threat gate min(gc%, tt%) keeps its break dependence high.
        "tt": {"target": 0.70, "a": 0.90, "b": 0.80, "c": 0.20, "d": 0.10},
    }


def test_field_percentile_normalises_raw_signal_to_unit_interval() -> None:
    signals = _summit_signals()
    assert _field_percentile(signals, "gc", "target") == pytest.approx(0.30)
    assert _field_percentile(signals, "climb", "target") == pytest.approx(0.90)


def test_summit_rider_factor_suppresses_break_dependent_climber_when_compressed() -> None:
    # Unipuerto prior: survival below the global rate -> space_ratio < 1.
    prior = {"probability": 5 / 12, "global_rate": 0.5}
    climber = summit_breakaway_rider_factor(prior, gc_strength=0.30, climb_strength=0.90)
    assert climber["space_ratio"] == pytest.approx((5 / 12) / 0.5)
    assert climber["break_dependence"] == pytest.approx(0.60)
    assert climber["entry_attempt_factor"] > 1.0
    assert climber["marking_factor"] < 1.0
    assert climber["factor"] < 1.0
    leader = summit_breakaway_rider_factor(prior, gc_strength=0.95, climb_strength=0.95)
    assert leader["break_dependence"] == pytest.approx(0.0)
    assert leader["factor"] == pytest.approx(1.0)


def test_summit_rider_factor_is_neutral_when_break_survives_at_baseline() -> None:
    prior = {"probability": 0.5, "global_rate": 0.5}
    result = summit_breakaway_rider_factor(prior, gc_strength=0.30, climb_strength=0.90)
    assert result["space_ratio"] == pytest.approx(1.0)
    assert result["marking_factor"] == pytest.approx(1.0)
    assert result["factor"] == pytest.approx(1.0)


def test_climber_break_dependence_is_positive_gap_of_climb_over_gc() -> None:
    assert climber_break_dependence(0.30, 0.90) == pytest.approx(0.60)
    assert climber_break_dependence(0.90, 0.90) == pytest.approx(0.0)
    assert climber_break_dependence(0.95, 0.20) == pytest.approx(0.0)


def test_stage_signal_components_apply_breakaway_permission_on_unipuerto_summit() -> None:
    signals = _summit_signals()
    evidence = {
        "profile_strength": {"mountain": 0.0},
        "profile_confidence": {"mountain": 0.0},
        "trajectory_factor": 1.0,
    }
    compressed_stage = {
        "profile_type": "mountain",
        "finish_type": "summit",
        "breakaway_history": {"global_rate": 0.5},
        "breakaway_survival_probability": 5 / 12,
    }
    neutral_stage = {**compressed_stage, "breakaway_survival_probability": 0.5}
    compressed = _stage_signal_components(compressed_stage, "target", signals, 0.0, evidence)
    neutral = _stage_signal_components(neutral_stage, "target", signals, 0.0, evidence)
    assert compressed["breakaway_rider_factor"] < 1.0
    assert neutral["breakaway_rider_factor"] == pytest.approx(1.0)
    assert compressed["breakaway_break_dependence"] == pytest.approx(0.60)
    # Base score is identical; only the surviving-break permission differs.
    assert compressed["score"] == pytest.approx(
        neutral["score"] * compressed["breakaway_rider_factor"]
    )


def test_tt_capable_gc_threat_is_not_suppressed_on_compressed_unipuerto() -> None:
    # Same strong-climb rider, but now a genuine all-rounder who also time-trials
    # well: the GC-defence gate min(gc%, tt%) is high, so break dependence -> 0
    # and the compressed-stage permission penalty does not apply.
    signals = {
        "gc": {"target": 0.90, "a": 0.30, "b": 0.80, "c": 0.70, "d": 0.10},
        "climb": {"target": 0.90, "a": 0.40, "b": 0.30, "c": 0.20, "d": 0.10},
        "tt": {"target": 0.90, "a": 0.30, "b": 0.20, "c": 0.70, "d": 0.10},
    }
    evidence = {
        "profile_strength": {"mountain": 0.0},
        "profile_confidence": {"mountain": 0.0},
        "trajectory_factor": 1.0,
    }
    compressed_stage = {
        "profile_type": "mountain",
        "finish_type": "summit",
        "breakaway_history": {"global_rate": 0.5},
        "breakaway_survival_probability": 5 / 12,
    }
    result = _stage_signal_components(compressed_stage, "target", signals, 0.0, evidence)
    assert result["breakaway_break_dependence"] == pytest.approx(0.0)
    assert result["breakaway_rider_factor"] == pytest.approx(1.0)

