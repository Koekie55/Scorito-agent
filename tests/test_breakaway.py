import csv
from pathlib import Path

import pytest

from scorito_agent.breakaway import historical_breakaway_prior

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



from scorito_agent.breakaway import (
    climber_break_dependence,
    summit_breakaway_rider_factor,
)


def _stage3_prior() -> dict[str, float]:
    return {"probability": 5 / 12, "global_rate": 0.5}


def test_break_dependent_climber_is_suppressed_on_compressed_summit() -> None:
    factor = summit_breakaway_rider_factor(
        _stage3_prior(), gc_strength=0.2, climb_strength=0.8
    )
    assert factor["break_dependence"] == pytest.approx(0.6)
    assert factor["space_ratio"] == pytest.approx(5 / 6)
    # KOM ambition raises entry attempts; compression lowers permission; net < 1.
    assert factor["entry_attempt_factor"] > 1.0
    assert factor["marking_factor"] < 1.0
    assert factor["factor"] == pytest.approx(0.89728, abs=1e-4)


def test_gc_favourite_is_unaffected_by_summit_prior() -> None:
    factor = summit_breakaway_rider_factor(
        _stage3_prior(), gc_strength=0.85, climb_strength=0.5
    )
    assert climber_break_dependence(0.85, 0.5) == pytest.approx(0.0)
    assert factor["factor"] == pytest.approx(1.0)


def test_neutral_summit_prior_never_inflates_a_rider() -> None:
    factor = summit_breakaway_rider_factor(
        {"probability": 0.5, "global_rate": 0.5}, gc_strength=0.2, climb_strength=0.8
    )
    # space_ratio == 1: entry ambition alone must not push the factor above 1.0.
    assert factor["space_ratio"] == pytest.approx(1.0)
    assert factor["factor"] == pytest.approx(1.0)


def test_gc_favourite_selection_mechanism_reports_early_vs_late() -> None:
    from scripts.analyze_gt_breakaway_suppression import (
        gc_favourite_selection,
        load_records,
    )

    selection = gc_favourite_selection(load_records())
    assert selection["early"]["gc_group_wins"] == 3
    assert selection["later"]["gc_group_wins"] == 11
    # Direction is suggestive (harder early selection) but the sample is tiny.
    assert selection["early"]["mean_winner_gc_rank"] <= selection["later"]["mean_winner_gc_rank"]
