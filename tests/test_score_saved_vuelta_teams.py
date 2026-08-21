from pathlib import Path
from types import SimpleNamespace

from scripts.score_saved_vuelta_teams import name_token_key, score_saved_squads


def test_name_token_key_ignores_accents_case_and_order() -> None:
    assert name_token_key("Tadej Pogačar") == name_token_key("POGAČAR Tadej")
    assert name_token_key("Raúl García Pierna") == name_token_key("GARCÍA PIERNA Raúl")


def test_saved_team_scoring_selects_nine_and_doubles_captain(monkeypatch) -> None:
    riders = [
        {"rider": f"Rider {index}", "model_qualities": {}}
        for index in range(1, 21)
    ]
    prediction_rows = [
        {"rider": f"Rider {index}", "predicted_finish": index}
        for index in range(1, 21)
    ]
    predictions = {
        "generated_at": "2026-08-19T00:00:00+00:00",
        "known_pcs_participants": 20,
        "stages": [
            {"stage_no": stage_no, "profile_type": "itt" if stage_no == 1 else "flat", "top_20": prediction_rows}
            for stage_no in range(1, 22)
        ],
    }
    projection = {
        "riders": riders,
        "decision_review": [
            {"rider": row["rider"], "classification_jersey_points": 1.0}
            for row in riders
        ],
    }
    snapshot_riders = [
        SimpleNamespace(name=f"Rider {index}", price=1_000_000, team_id=index)
        for index in range(1, 21)
    ]
    snapshot = SimpleNamespace(
        riders=snapshot_riders,
        budget=48_000_000,
        captain_factor=2,
        market_id=310,
    )
    report = score_saved_squads(
        predictions,
        projection,
        [{"team": "test", "sources": ["test.csv"], "riders": [f"Rider {index}" for index in range(1, 21)]}],
        snapshot,
    )

    team = report["teams"][0]
    assert len(team["lineups"]) == 21
    assert all(len(stage["lineup"]) == 9 for stage in team["lineups"])
    assert team["lineups"][0]["captain"] == "Rider 1"
    expected_stage = sum((50, 44, 40, 36, 32, 30, 28, 26, 24)) + 50
    assert team["projected_enrolled_stage_points"] == 21 * expected_stage
    assert team["projected_classification_jersey_points"] == 20.0
    assert team["final_projected_point_score"] == 21 * expected_stage + 20.0
    assert team["legal_current_market"] is True
