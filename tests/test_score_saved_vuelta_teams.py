from pathlib import Path
import json
from types import SimpleNamespace

from scorito_agent.scorito.team_points import TeamPointProjection
from scripts.score_saved_vuelta_teams import (
    load_saved_squads,
    name_token_key,
    score_saved_squads,
)


def test_name_token_key_ignores_accents_case_and_order() -> None:
    assert name_token_key("Tadej Pogačar") == name_token_key("POGAČAR Tadej")
    assert name_token_key("Raúl García Pierna") == name_token_key("GARCÍA PIERNA Raúl")

def test_load_saved_squads_prefers_canonical_locked_hawktuah_team(tmp_path) -> None:
    riders = [
        "João Almeida",
        "Pablo Castrillo",
        *[f"Locked Rider {index}" for index in range(1, 19)],
    ]
    (tmp_path / "hawktuah_team.json").write_text(
        json.dumps(
            {
                "status": "locked_canonical_squad",
                "riders": riders,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    squads = load_saved_squads(tmp_path)

    assert squads == [
        {
            "team": "Hawktuah / locked AI squad",
            "sources": ["hawktuah_team.json"],
            "riders": riders,
        }
    ]


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
        SimpleNamespace(rider_id=index, name=f"Rider {index}", price=1_000_000, team_id=index)
        for index in range(1, 21)
    ]
    snapshot = SimpleNamespace(
        riders=snapshot_riders,
        budget=48_000_000,
        captain_factor=2,
        market_id=310,
    )
    monkeypatch.setattr(
        "scripts.score_saved_vuelta_teams.expected_team_points_by_rider",
        lambda snapshot, **kwargs: {
            rider.rider_id: TeamPointProjection() for rider in snapshot.riders
        },
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


def test_expected_team_points_can_replace_bottom_two_lineup_riders(monkeypatch) -> None:
    riders = [
        SimpleNamespace(
            rider_id=index,
            name=f"Rider {index}",
            price=1_000_000,
            team_id=index,
        )
        for index in range(1, 21)
    ]
    snapshot = SimpleNamespace(
        riders=riders,
        budget=48_000_000,
        captain_factor=2,
        market_id=310,
    )
    predictions = {
        "stages": [
            {
                "stage_no": 3,
                "profile_type": "mountain",
                "top_20": [
                    {"rider": f"Rider {index}", "predicted_finish": index}
                    for index in range(1, 21)
                ],
            }
        ]
    }
    projection = {
        "riders": [{"rider": rider.name} for rider in riders],
        "decision_review": [],
    }
    monkeypatch.setattr(
        "scripts.score_saved_vuelta_teams.expected_team_points_by_rider",
        lambda snapshot, **kwargs: {
            rider.rider_id: TeamPointProjection(
                classification_points={10: 30.0, 11: 28.0}.get(rider.rider_id, 0.0)
            )
            for rider in snapshot.riders
        },
    )

    report = score_saved_squads(
        predictions,
        projection,
        [{"team": "test", "sources": ["test"], "riders": [rider.name for rider in riders]}],
        snapshot,
    )

    lineup = report["teams"][0]["lineups"][0]
    assert lineup["individual_only_lineup"] == [
        f"Rider {index}" for index in range(1, 10)
    ]
    assert [
        rider["individual_points"] for rider in lineup["individual_only_rider_points"]
    ] == [50, 44, 40, 36, 32, 30, 28, 26, 24]
    assert set(lineup["lineup"]) == {
        "Rider 1", "Rider 2", "Rider 3", "Rider 4", "Rider 5",
        "Rider 6", "Rider 7", "Rider 10", "Rider 11",
    }
    assert {"Rider 8", "Rider 9"}.issubset(
        {rider["rider"] for rider in lineup["reserves"]}
    )
    assert lineup["captain"] == "Rider 1"
    assert lineup["expected_team_points"] == 58.0
    assert [replacement["in"]["rider"] for replacement in lineup["team_point_replacements"]] == [
        "Rider 10", "Rider 11",
    ]
    assert [replacement["out"]["rider"] for replacement in lineup["team_point_replacements"]] == [
        "Rider 9", "Rider 8",
    ]
