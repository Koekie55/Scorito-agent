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

