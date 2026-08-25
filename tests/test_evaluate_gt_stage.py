import json
from datetime import datetime

import pytest

from scripts import evaluate_gt_stage
from scorito_agent.stage_evaluation import archive_pre_stage_prediction
from scorito_agent.teams_graph import TeamsGraphConfig


def _write(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _race_calendar(tmp_path):
    race_dir = tmp_path / "testrace"
    race_dir.mkdir()
    rounds = []
    for stage_no in range(1, 21):
        stage_id = 1000 + stage_no
        rounds.append(
            {
                "MarketRoundId": 2000 + stage_no,
                "StageId": stage_id,
                "StageOrder": stage_no,
            }
        )
        _write(
            race_dir / f"stage_{stage_id}.json",
            {
                "Content": {
                    "StartDate": f"2026-08-{stage_no:02d}T11:00:00",
                    "StartLocation": "Start",
                    "FinishLocation": "Finish",
                }
            },
        )
    _write(race_dir / "marketroundstage.json", {"Content": rounds})
    return race_dir


def test_non_stage_day_skips_before_refresh(tmp_path, monkeypatch) -> None:
    _race_calendar(tmp_path)
    monkeypatch.setattr(evaluate_gt_stage, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(
        evaluate_gt_stage,
        "_refresh_results",
        lambda slug: pytest.fail("refresh must not run on a rest day"),
    )

    assert evaluate_gt_stage.main(
        ["--slug", "testrace", "--date", "2026-08-23", "--refresh-results"]
    ) == 0


def test_missing_archive_blocks_before_result_refresh(tmp_path, monkeypatch) -> None:
    _race_calendar(tmp_path)
    monkeypatch.setattr(evaluate_gt_stage, "DATA_ROOT", tmp_path)
    refreshed = []
    monkeypatch.setattr(evaluate_gt_stage, "_refresh_results", refreshed.append)

    assert evaluate_gt_stage.main(
        ["--slug", "testrace", "--date", "2026-08-02", "--refresh-results"]
    ) == 2
    assert refreshed == []


def _evaluated_stage_one(tmp_path, monkeypatch):
    race_dir = _race_calendar(tmp_path)
    _write(
        race_dir / "eventriderenriched.json",
        {
            "Content": [
                {"RiderId": rider_id, "FirstName": "Rider", "LastName": str(rider_id)}
                for rider_id in range(1, 31)
            ]
        },
    )
    predictions_path = race_dir / "stage_top20_predictions.json"
    _write(
        predictions_path,
        {
            "race": "Test Grand Tour",
            "generated_at": "2026-07-31T08:00:00+00:00",
            "stages": [
                {
                    "stage_no": 1,
                    "top_20": [
                        {"predicted_finish": rank, "rider": f"{rider_id} R\u00edder"}
                        for rank, rider_id in enumerate(range(1, 21), start=1)
                    ],
                }
            ],
        },
    )
    archive_pre_stage_prediction(
        race_dir, predictions_path, 1, now=datetime(2026, 7, 31, 10, 0)
    )
    _write(
        race_dir / "stageresult_rider_1001.json",
        {"Content": [{"RiderId": rider_id, "Rank": rider_id} for rider_id in range(1, 21)]},
    )
    monkeypatch.setattr(evaluate_gt_stage, "DATA_ROOT", tmp_path)
    return race_dir


def test_incomplete_teams_config_does_not_fail_a_successful_evaluation(
    tmp_path, monkeypatch, capsys
) -> None:
    _evaluated_stage_one(tmp_path, monkeypatch)

    def _raise_config_error():
        raise ValueError("Teams configuration is incomplete: SCORITO_TEAMS_SELF_CHAT_ID")

    monkeypatch.setattr(TeamsGraphConfig, "from_environment", _raise_config_error)

    exit_code = evaluate_gt_stage.main(
        [
            "--slug", "testrace", "--date", "2026-08-01",
            "--send-teams-if-configured", "--env-file", str(tmp_path / "missing.env"),
        ]
    )

    assert exit_code == 0
    assert "TEAMS NOT SENT" in capsys.readouterr().err


def test_teams_send_failure_does_not_fail_a_successful_evaluation(
    tmp_path, monkeypatch, capsys
) -> None:
    _evaluated_stage_one(tmp_path, monkeypatch)
    dummy_config = TeamsGraphConfig(
        access_token="short-lived-token",
        self_chat_id="48:notes",
        expected_user_id="user-id",
        expected_upn="user@example.com",
    )
    monkeypatch.setattr(TeamsGraphConfig, "from_environment", lambda: dummy_config)

    def _raise_send_error(config, message):
        raise RuntimeError("Graph request failed: 503 Service Unavailable")

    monkeypatch.setattr(evaluate_gt_stage, "send_to_teams_self_chat", _raise_send_error)

    exit_code = evaluate_gt_stage.main(
        [
            "--slug", "testrace", "--date", "2026-08-01",
            "--send-teams-if-configured", "--env-file", str(tmp_path / "missing.env"),
        ]
    )

    assert exit_code == 0
    assert "TEAMS NOT SENT" in capsys.readouterr().err
