import json
from datetime import datetime

import pytest

from scorito_agent.stage_evaluation import (
    _compound_name_matches,
    archive_pre_stage_prediction,
    calculate_predictability,
    evaluate_stage_archive,
    stage_on_date,
    write_evaluation_once,
)


def test_compound_surname_resolves_when_only_one_market_rider_fits() -> None:
    """PCS writes "DVERSNES LAVIK Fredrik" where Scorito has "Fredrik Dversnes"."""
    tokens_by_id = {
        7670: frozenset({"fredrik", "dversnes"}),
        7671: frozenset({"ben", "turner"}),
        7672: frozenset({"ben", "tulett"}),
    }

    assert _compound_name_matches("dversnes fredrik lavik", tokens_by_id) == [7670]
    assert _compound_name_matches("ben turner", tokens_by_id) == [7671]


def test_compound_surname_stays_ambiguous_when_several_riders_fit() -> None:
    tokens_by_id = {
        1: frozenset({"garcia", "raul"}),
        2: frozenset({"garcia", "raul", "pierna"}),
    }

    assert len(_compound_name_matches("garcia pierna raul", tokens_by_id)) == 2


def _write(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _market_files(tmp_path) -> None:
    _write(
        tmp_path / "marketroundstage.json",
        {
            "Content": [
                {
                    "MarketRoundId": 7319,
                    "StageId": 2820,
                    "StageOrder": 1,
                }
            ]
        },
    )
    _write(
        tmp_path / "stage_2820.json",
        {
            "Content": {
                "StartDate": "2026-08-22T11:00:00",
                "StartLocation": "Start",
                "FinishLocation": "Finish",
            }
        },
    )
    _write(
        tmp_path / "eventriderenriched.json",
        {
            "Content": [
                {
                    "RiderId": rider_id,
                    "FirstName": "Rider",
                    "LastName": str(rider_id),
                }
                for rider_id in range(1, 31)
            ]
        },
    )


def _predictions(tmp_path, rider_ids=range(1, 21)):
    path = tmp_path / "stage_top20_predictions.json"
    _write(
        path,
        {
            "race": "Test Grand Tour",
            "generated_at": "2026-08-21T08:00:00+00:00",
            "stages": [
                {
                    "stage_no": 1,
                    "top_20": [
                        {"predicted_finish": rank, "rider": f"{rider_id} R\u00edder"}
                        for rank, rider_id in enumerate(rider_ids, start=1)
                    ],
                }
            ],
        },
    )
    return path


def test_predictability_is_100_for_an_exact_top_20() -> None:
    actual = [
        {"RiderId": rider_id, "Rank": rider_id}
        for rider_id in range(1, 21)
    ]

    score = calculate_predictability(list(range(1, 21)), actual)

    assert score["overlap_pct"] == 100.0
    assert score["rank_accuracy_pct"] == 100.0
    assert score["predictability_pct"] == 100.0


def test_non_matches_contribute_zero_to_both_formula_halves() -> None:
    actual = [
        {"RiderId": rider_id, "Rank": rank}
        for rank, rider_id in enumerate([*range(1, 11), *range(21, 31)], start=1)
    ]

    score = calculate_predictability(list(range(1, 21)), actual)

    assert score["matched_riders"] == 10
    assert score["overlap_pct"] == 50.0
    assert score["rank_accuracy_pct"] == 50.0
    assert score["predictability_pct"] == 50.0


def test_archive_is_create_once_and_rejects_post_start_creation(tmp_path) -> None:
    _market_files(tmp_path)
    predictions_path = _predictions(tmp_path)

    path, first, created = archive_pre_stage_prediction(
        tmp_path,
        predictions_path,
        1,
        now=datetime(2026, 8, 22, 10, 0),
    )
    original_bytes = path.read_bytes()
    _predictions(tmp_path, range(11, 31))
    same_path, second, created_again = archive_pre_stage_prediction(
        tmp_path,
        predictions_path,
        1,
        now=datetime(2026, 8, 22, 10, 30),
    )

    assert created is True
    assert created_again is False
    assert same_path == path
    assert path.read_bytes() == original_bytes
    assert [row["rider_id"] for row in first["top_20"]] == list(range(1, 21))
    assert second == first

    path.unlink()
    with pytest.raises(RuntimeError, match="refusing to create a post-start"):
        archive_pre_stage_prediction(
            tmp_path,
            predictions_path,
            1,
            now=datetime(2026, 8, 22, 11, 1),
        )


def test_calendar_gate_and_evaluation_are_auditable(tmp_path) -> None:
    _market_files(tmp_path)
    predictions_path = _predictions(tmp_path)
    archive_path, _, _ = archive_pre_stage_prediction(
        tmp_path,
        predictions_path,
        1,
        now=datetime(2026, 8, 22, 10, 0),
    )
    result_path = tmp_path / "stageresult_rider_2820.json"
    _write(
        result_path,
        {
            "Content": [
                {"RiderId": rider_id, "Rank": rider_id}
                for rider_id in range(1, 21)
            ]
        },
    )

    assert stage_on_date(tmp_path, datetime(2026, 8, 22).date())["stage_no"] == 1
    assert stage_on_date(tmp_path, datetime(2026, 8, 23).date()) is None

    report = evaluate_stage_archive(
        archive_path,
        result_path,
        tmp_path / "eventriderenriched.json",
        evaluated_at=datetime(2026, 8, 22, 18, 15),
    )
    output = tmp_path / "stage_predictability" / "evaluations" / "stage_01.json"
    saved, created = write_evaluation_once(output, report)
    _, created_again = write_evaluation_once(output, {**report, "predictability_pct": 0})

    assert report["predictability_pct"] == 100.0
    assert len(report["prediction_source_sha256"]) == 64
    assert len(report["result_source_sha256"]) == 64
    assert created is True
    assert created_again is False
    assert saved == report


def test_evaluation_lists_predicted_misses_and_unpredicted_finishers(tmp_path) -> None:
    _market_files(tmp_path)
    predictions_path = _predictions(tmp_path, range(1, 21))
    archive_path, _, _ = archive_pre_stage_prediction(
        tmp_path,
        predictions_path,
        1,
        now=datetime(2026, 8, 22, 10, 0),
    )
    result_path = tmp_path / "stageresult_rider_2820.json"
    _write(
        result_path,
        {
            "Content": [
                {"RiderId": rider_id, "Rank": rank}
                for rank, rider_id in enumerate([*range(1, 11), *range(21, 31)], start=1)
            ]
        },
    )

    report = evaluate_stage_archive(
        archive_path,
        result_path,
        tmp_path / "eventriderenriched.json",
        evaluated_at=datetime(2026, 8, 22, 18, 15),
    )

    assert [row["rider_id"] for row in report["predicted_misses"]] == list(range(11, 21))
    assert [row["rider_id"] for row in report["unpredicted_finishers"]] == list(range(21, 31))
