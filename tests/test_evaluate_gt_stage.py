import json

from scripts import evaluate_gt_stage


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