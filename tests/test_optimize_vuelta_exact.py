from types import SimpleNamespace

from scripts.optimize_vuelta_exact import _score_fixed_squad


def test_exact_scorer_uses_bonuses_for_lineup_and_captain() -> None:
    stage = SimpleNamespace(order=1, stage_id=101)
    riders = {
        rider_id: SimpleNamespace(
            rider_id=rider_id,
            name=f"Rider {rider_id}",
            price=1_000_000,
            team_id=1 if rider_id in {1, 10} else rider_id,
        )
        for rider_id in range(1, 11)
    }
    snapshot = SimpleNamespace(
        stages=[stage],
        rider=lambda rider_id: riders[rider_id],
    )
    base = {
        (rider_id, stage.stage_id): float(11 - rider_id)
        for rider_id in riders
    }
    conditional = {
        (rider_id, stage.stage_id): points
        + (10.0 if riders[rider_id].team_id == 1 else 0.0)
        + (8.0 if riders[rider_id].team_id == 1 else 0.0)
        for (rider_id, _stage_id), points in base.items()
    }
    inputs = {
        "snapshot": snapshot,
        "classification": {rider_id: 0.0 for rider_id in riders},
        "base_points": base,
        "conditional_points": conditional,
        "stage_context": {
            stage.stage_id: {
                "stage_no": 1,
                "profile_type": "flat",
                "winner": "Rider 1",
                "winner_team_id": 1,
            }
        },
        "red_team_id": 1,
    }

    result = _score_fixed_squad(inputs, list(riders), conditional=True)

    assert "Rider 10" in result["stages"][0]["lineup"]
    assert "Rider 9" not in result["stages"][0]["lineup"]
    assert result["stages"][0]["captain"] == "Rider 1"
    assert result["stages"][0]["captain_bonus_points"] == 28.0
    assert result["stages"][0]["team_bonus_points"] == 20.0
    assert result["stages"][0]["red_jersey_team_bonus_points"] == 16.0