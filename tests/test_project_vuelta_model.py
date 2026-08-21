from pathlib import Path

import pytest

from scorito_agent.expert_chat import apply_signal
from scripts.recommend_vuelta_live import (
    _blended_quality_ratings,
    _captain_eligible_ids,
    _conditional_team_bonus_upside,
    _load_cyclingoracle_classifications,
    _objective_stage_lineup,
    _stage_analysis_weight,
)
from scripts.recommend_stage_plan import ROOT as STAGE_PLAN_ROOT
from scripts.recommend_stage_plan import _display_output_path
from scripts.recommend_stage_plan import _resolve_output_path
from scripts.project_vuelta import (
    _course_similarity,
    _field_strength,
    _gradual_quality_ratings,
)


def test_field_strength_uses_exact_field_quality_and_size() -> None:
    strong = {
        "event": "WorldTour race (2.UWT)",
        "course_context": {
            "startlist_quality_score": 1500,
            "startlist_count": 170,
            "race_ranking": 2,
        },
    }
    weak = {
        "event": "WorldTour race (2.UWT)",
        "course_context": {
            "startlist_quality_score": 180,
            "startlist_count": 65,
            "race_ranking": 240,
        },
    }

    assert _field_strength(strong) > _field_strength(weak)
    assert 0.0 < _field_strength(weak) < _field_strength(strong) <= 1.0


def test_course_similarity_rewards_matching_course_shape() -> None:
    target = {
        "profile_type": "mountain",
        "finish_type": "summit",
        "distance_km": 175.0,
        "vertical_meters": 4700,
        "profile_score": 410,
        "gradient_final_km": 8.5,
    }
    mountain_result = {
        "profile_type": "mountain",
        "finish_type": "summit",
        "course_context": {
            "distance_km": 168.0,
            "vertical_meters": 4500,
            "profile_score": 390,
            "gradient_final_km": 9.0,
        },
    }
    flat_result = {
        "profile_type": "flat",
        "finish_type": "sprint",
        "course_context": {
            "distance_km": 170.0,
            "vertical_meters": 700,
            "profile_score": 20,
            "gradient_final_km": 0.0,
        },
    }

    assert _course_similarity(mountain_result, target) > _course_similarity(flat_result, target)


def test_quality_ratings_are_gradual_tenths_not_even_buckets() -> None:
    slug = "test-rider"
    signals = {
        "overall": {slug: 0.52},
        "gc": {slug: 0.43},
        "climb": {slug: 0.37},
        "sprint": {slug: 0.28},
        "tt": {slug: 0.34},
        "prologue": {slug: 0.22},
        "classic": {slug: 0.46},
        "previous_vuelta": {slug: 0.31},
    }
    evidence = {
        "profile_strength": {
            "flat": 0.24,
            "hilly": 0.36,
            "mountain": 0.30,
            "itt": 0.27,
        }
    }

    ratings = _gradual_quality_ratings(signals, slug, evidence)

    assert set(ratings) == {
        "gc",
        "climb",
        "time_trial",
        "sprint",
        "punch",
        "hill",
        "cobbles",
    }
    assert all(0.0 <= value <= 10.0 for value in ratings.values())
    assert any(value not in {0.0, 2.0, 4.0, 6.0, 8.0, 10.0} for value in ratings.values())



def test_cyclingoracle_classification_loader_matches_names_and_probabilities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text(
        '{"source_url":"https://example/jongeren-klassement","rider_name":"Oscar Onley","win_probability":0.6758}\n'
        '{"source_url":"https://example/punten-klassement","rider_name":"Oscar Onley","win_probability":0.12}\n',
        encoding="utf-8",
    )
    snapshot = type(
        "SnapshotStub",
        (),
        {
            "riders": [
                type("RiderStub", (), {"name": "Oscar Onley", "rider_id": 42})()
            ]
        },
    )()
    monkeypatch.setattr(
        "scripts.recommend_vuelta_live.CYCLINGORACLE_PATH", prediction_path
    )

    probabilities, source = _load_cyclingoracle_classifications(snapshot)

    assert probabilities == {42: {"youth": 0.6758, "points": 0.12}}
    assert "2 rows; 0 unmatched" in source


def test_team_bonus_is_conditional_and_excludes_automatic_kom_points() -> None:
    assert _conditional_team_bonus_upside(
        rider_team_id=14, winner_team_id=14, red_jersey_team_id=14
    ) == 18.0
    assert _conditional_team_bonus_upside(
        rider_team_id=14, winner_team_id=30, red_jersey_team_id=14
    ) == 8.0
    assert _conditional_team_bonus_upside(
        rider_team_id=30, winner_team_id=30, red_jersey_team_id=14
    ) == 10.0
    assert _conditional_team_bonus_upside(
        rider_team_id=30, winner_team_id=14, red_jersey_team_id=14
    ) == 0.0


def test_stage_analysis_rejects_mismatched_stage_numbers() -> None:
    weight, status = _stage_analysis_weight(
        {"profile_type": "itt", "finish_type": "tt", "distance_km": 9.4},
        {"type": "GC / Mountain", "distance_km": 155},
        0.15,
    )
    assert weight == 0.0
    assert status == "ignored_distance_mismatch"

    weight, status = _stage_analysis_weight(
        {"profile_type": "hilly", "finish_type": "flat", "distance_km": 177.4},
        {"type": "Sprint", "distance_km": 181},
        0.15,
    )
    assert weight == 0.15
    assert status == "applied"


def test_stage_plan_relative_output_resolves_from_project_root() -> None:
    relative = Path("data") / "scorito" / "tdf2026" / "smoke.json"

    assert _resolve_output_path(str(relative), "tdf2026") == STAGE_PLAN_ROOT / relative
    assert _resolve_output_path("", "tdf2026") == (
        STAGE_PLAN_ROOT / "data" / "scorito" / "tdf2026" / "stage_plan.json"
    )
    assert _display_output_path(STAGE_PLAN_ROOT / relative) == relative
    assert _display_output_path(Path("C:\\outside\\stage-plan.json")) == Path(
        "C:\\outside\\stage-plan.json"
    )


def test_flat_finish_uses_sprint_captain_unless_punch_analysis_is_valid() -> None:
    squad_ids = {1, 2, 3}
    sprint_ids = {2}
    projection = {"profile_type": "hilly", "finish_type": "flat"}

    assert _captain_eligible_ids(
        projection,
        {"type": "Sprint / Transition"},
        "applied",
        squad_ids,
        sprint_ids,
    ) == {2}
    assert _captain_eligible_ids(
        projection,
        {"type": "GC / Mountain"},
        "applied",
        squad_ids,
        sprint_ids,
    ) == squad_ids
    assert _captain_eligible_ids(
        projection,
        {"type": "GC / Mountain"},
        "ignored_distance_mismatch",
        squad_ids,
        sprint_ids,
    ) == {2}


def test_stage_lineup_uses_deterministic_objective_top_nine() -> None:
    squad_ids = list(range(1, 11))
    points = {rider_id: float(11 - rider_id) for rider_id in squad_ids}

    lineup = _objective_stage_lineup(
        object(),
        squad_ids,
        points,
        captain_eligible_ids={2, 3},
        lineup_size=9,
        captain_factor=2,
    )

    assert lineup.rider_ids == list(range(1, 10))
    assert lineup.captain_id == 2


def test_course_specific_captain_ranking_does_not_change_lineup() -> None:
    squad_ids = list(range(1, 11))
    points = {rider_id: float(11 - rider_id) for rider_id in squad_ids}

    lineup = _objective_stage_lineup(
        object(),
        squad_ids,
        points,
        captain_eligible_ids={2, 3},
        captain_rank_scores={2: 0.8, 3: 0.9},
        lineup_size=9,
        captain_factor=2,
    )

    assert lineup.rider_ids == list(range(1, 10))
    assert lineup.captain_id == 3
    assert lineup.captain_points == points[3]


def test_captain_restrictions_do_not_change_lineup_membership() -> None:
    squad_ids = list(range(1, 11))
    points = {rider_id: float(11 - rider_id) for rider_id in squad_ids}

    lineup = _objective_stage_lineup(
        object(),
        squad_ids,
        points,
        captain_eligible_ids={9, 10},
        lineup_size=9,
        captain_factor=2,
    )

    assert lineup.rider_ids == list(range(1, 10))
    assert lineup.captain_id == 9


def test_stage_lineup_fails_when_objective_top_nine_has_no_eligible_captain() -> None:
    squad_ids = list(range(1, 11))
    points = {rider_id: float(11 - rider_id) for rider_id in squad_ids}

    with pytest.raises(RuntimeError, match="no eligible captain"):
        _objective_stage_lineup(
            object(),
            squad_ids,
            points,
            captain_eligible_ids={10},
            lineup_size=9,
            captain_factor=2,
        )


def test_maximum_chat_signal_cannot_rescue_materially_inferior_rider() -> None:
    adjusted = {
        1: apply_signal(100.0, -1.0),
        2: apply_signal(70.0, 1.0),
    }

    lineup = _objective_stage_lineup(
        object(),
        [1, 2],
        adjusted,
        lineup_size=1,
        captain_factor=2,
    )

    assert adjusted == pytest.approx({1: 88.0, 2: 78.4})
    assert lineup.rider_ids == [1]
    assert lineup.captain_id == 1


def test_live_and_model_qualities_blend_into_gradual_ratings() -> None:
    projected = {
        "model_qualities": {
            "gc": 4.3,
            "climb": 5.1,
            "time_trial": 6.2,
            "sprint": 3.7,
            "punch": 5.4,
            "hill": 4.8,
            "cobbles": 5.9,
        }
    }
    raw = {
        "Qualities": [
            {"Type": 2, "Value": 8},
            {"Type": 3, "Value": 7},
            {"Type": 4, "Value": 8},
        ]
    }

    ratings = _blended_quality_ratings(projected, raw)

    assert ratings["sprint"] == 5.2
    assert ratings["time_trial"] == 7.0
    assert ratings["gc"] == 2.4
    assert any(value not in {0.0, 2.0, 4.0, 6.0, 8.0, 10.0} for value in ratings.values())
    assert _blended_quality_ratings(projected, {"Qualities": []}) == projected["model_qualities"]
