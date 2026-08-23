import json

import pytest

from scripts.daily_vuelta_refresh import (
    _apply_cyclingoracle_stage,
    _completed_and_next_stage,
    _require_cyclingoracle_for_email,
    _run_refresh_commands,
)


def _write(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _rounds(tmp_path) -> None:
    _write(
        tmp_path / "marketroundstage.json",
        {
            "Content": [
                {"StageId": 12, "StageOrder": 3},
                {"StageId": 10, "StageOrder": 1},
                {"StageId": 11, "StageOrder": 2},
            ]
        },
    )


def test_next_stage_follows_contiguous_completed_result_payloads(tmp_path) -> None:
    _rounds(tmp_path)
    _write(tmp_path / "stageresult_rider_10.json", {"Content": [{"Rank": 1}]})
    _write(tmp_path / "stageresult_rider_11.json", {"Content": [{"Rank": 1}]})
    _write(tmp_path / "stageresult_rider_12.json", {"Content": []})

    assert _completed_and_next_stage(tmp_path) == ([1, 2], 3)


def test_later_result_cannot_skip_an_unfinished_stage(tmp_path) -> None:
    _rounds(tmp_path)
    _write(tmp_path / "stageresult_rider_10.json", {"Content": [{"Rank": 1}]})
    _write(tmp_path / "stageresult_rider_11.json", {"Content": []})
    _write(tmp_path / "stageresult_rider_12.json", {"Content": [{"Rank": 1}]})

    assert _completed_and_next_stage(tmp_path) == ([1], 2)


def test_all_result_payloads_complete_the_race(tmp_path) -> None:
    _rounds(tmp_path)
    for stage_id in (10, 11, 12):
        _write(
            tmp_path / f"stageresult_rider_{stage_id}.json",
            {"Content": [{"Rank": 1}]},
        )

    assert _completed_and_next_stage(tmp_path) == ([1, 2, 3], None)


def test_refresh_rebuilds_race_book_after_personal_analysis(monkeypatch) -> None:
    commands = []

    def record(command, *, cwd, check) -> None:
        commands.append(command)
        assert check is True

    monkeypatch.setattr("scripts.daily_vuelta_refresh.subprocess.run", record)

    _run_refresh_commands()

    assert [command[1] for command in commands[-2:]] == [
        "scripts/analyze_my_vuelta_team.py",
        "scripts/export_vuelta_race_book.py",
    ]


def test_cyclingoracle_controls_published_ranks_and_pcs_fills_gaps(tmp_path) -> None:
    path = tmp_path / "cyclingoracle.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "stage_number": 3,
                    "model_rank": 1,
                    "rider_name": "Oracle Rider",
                    "win_probability_pct": 35.5,
                    "source_url": "https://example.test/stage-3",
                },
                {
                    "stage_number": 2,
                    "model_rank": 1,
                    "rider_name": "Wrong Stage",
                },
            )
        ),
        encoding="utf-8",
    )
    stage = {
        "stage_no": 3,
        "top_20": [
            {
                "rider": f"PCS Rider {rank}",
                "rider_slug": f"pcs-{rank}",
                "predicted_finish": rank,
                "scorito_stage_points": 50,
            }
            for rank in range(1, 21)
        ],
    }

    source = _apply_cyclingoracle_stage(stage, path)

    assert source == {
        "source_url": "https://example.test/stage-3",
        "prediction_count": 1,
        "file_updated_at": source["file_updated_at"],
    }
    assert [row["rider"] for row in stage["top_20"][:3]] == [
        "Oracle Rider",
        "PCS Rider 1",
        "PCS Rider 2",
    ]
    assert stage["top_20"][0]["scorito_stage_points"] == 50
    assert stage["top_20"][1]["scorito_stage_points"] == 44
    assert stage["top_20"][0]["prediction_source"] == "cyclingoracle"
    assert stage["top_20"][1]["prediction_source"] == "pcs_fallback"


def test_email_requires_target_stage_cyclingoracle_prediction() -> None:
    report = {
        "status": "forward_recommendation",
        "target_stage": {"stage_no": 4},
        "sources": {"cyclingoracle": None},
    }

    with pytest.raises(RuntimeError, match="email was not sent"):
        _require_cyclingoracle_for_email(report)

    report["sources"]["cyclingoracle"] = {"prediction_count": 15}
    _require_cyclingoracle_for_email(report)
