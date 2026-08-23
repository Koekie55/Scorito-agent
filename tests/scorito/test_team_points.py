from __future__ import annotations

import csv
from types import SimpleNamespace

import pytest

from scorito_agent.scorito.loader import load_snapshot
from scorito_agent.scorito.models import Rider, Snapshot, Stage
from scorito_agent.scorito.team_points import (
    DEFAULT_CLASSIFICATION_RETENTION,
    estimate_classification_retention,
    expected_team_points_by_rider,
    normalized_team_win_probabilities,
)
from scripts import recommend_vuelta_live
from scripts.recommend_vuelta_live import _common_columns, _objective_stage_lineup


def _snapshot() -> Snapshot:
    riders = [
        Rider(
            rider_id=rider_id,
            event_rider_id=rider_id,
            name=f"Rider {rider_id}",
            team_id=14 if rider_id == 10 else 30,
            price=1_000_000,
            role=6,
            nationality="NL",
            age=30,
        )
        for rider_id in range(1, 11)
    ]
    stages = [
        Stage(101, 1001, 1, 1, 2),
        Stage(102, 1002, 2, 1, 3),
    ]
    return Snapshot(
        market_id=310,
        slug="test",
        budget=48_000_000,
        captain_factor=2,
        riders=riders,
        stages=stages,
        stage_point_components={(101, 10): {201: 8.0, 202: 6.0}},
    )


def test_expected_floor_combines_retained_classifications_and_win_probability() -> None:
    snapshot = _snapshot()

    projections = expected_team_points_by_rider(
        snapshot,
        stage_order=2,
        team_win_probabilities={14: 0.2},
        retention_probabilities={201: 1.0, 202: 1.0},
    )

    assert projections[10].classification_points == 14.0
    assert projections[10].stage_win_points == 2.0
    assert projections[10].total == 16.0
    assert projections[1].total == 0.0


def test_expected_floor_replaces_weaker_ninth_rider_and_keeps_eligible_captain() -> None:
    snapshot = _snapshot()
    individual = {rider_id: float(21 - rider_id) for rider_id in range(1, 10)}
    individual[10] = 0.0
    projections = expected_team_points_by_rider(
        snapshot,
        stage_order=2,
        team_win_probabilities={14: 0.2},
        retention_probabilities={201: 1.0, 202: 1.0},
    )
    expected = {
        rider_id: individual[rider_id] + projections[rider_id].total
        for rider_id in individual
    }

    lineup = _objective_stage_lineup(
        snapshot.stages[1],
        list(individual),
        expected,
        captain_eligible_ids={1},
        captain_rank_scores=individual,
        lineup_size=9,
        captain_factor=2,
    )

    assert 10 in lineup.rider_ids
    assert 9 not in lineup.rider_ids
    assert lineup.captain_id == 1
    assert lineup.captain_id in lineup.rider_ids


def test_expected_floor_does_not_replace_ninth_rider_above_threshold() -> None:
    snapshot = _snapshot()
    individual = {rider_id: float(26 - rider_id) for rider_id in range(1, 10)}
    individual[10] = 0.0
    projections = expected_team_points_by_rider(
        snapshot,
        stage_order=2,
        team_win_probabilities={14: 0.2},
        retention_probabilities={201: 1.0, 202: 1.0},
    )
    expected = {
        rider_id: individual[rider_id] + projections[rider_id].total
        for rider_id in individual
    }

    lineup = _objective_stage_lineup(
        snapshot.stages[1],
        list(individual),
        expected,
        captain_eligible_ids={1},
        captain_rank_scores=individual,
        lineup_size=9,
    )

    assert lineup.rider_ids == list(range(1, 10))


def test_classification_retention_compounds_for_future_stages() -> None:
    snapshot = _snapshot()

    projections = expected_team_points_by_rider(
        snapshot,
        stage_order=3,
        team_win_probabilities={},
        retention_probabilities={201: 0.5, 202: 0.5},
    )

    assert projections[10].classification_points == pytest.approx(3.5)
    assert projections[10].stage_win_points == 0.0


def test_team_win_scores_are_normalized_before_ten_point_weight() -> None:
    snapshot = _snapshot()

    probabilities = normalized_team_win_probabilities(snapshot, {1: 3.0, 10: 1.0})
    projections = expected_team_points_by_rider(
        snapshot,
        stage_order=1,
        team_win_probabilities=probabilities,
    )

    assert probabilities == pytest.approx({30: 0.75, 14: 0.25})
    assert projections[10].classification_points == 0.0
    assert projections[10].stage_win_points == 2.5


def test_committed_tour_giro_retention_matches_default_calibration() -> None:
    estimates = estimate_classification_retention(
        [load_snapshot("tdf2026"), load_snapshot("giro2026")]
    )

    assert {points_type: rate for points_type, (*_, rate) in estimates.items()} == (
        pytest.approx(DEFAULT_CLASSIFICATION_RETENTION)
    )
    assert all(transitions == 40 for _, transitions, _ in estimates.values())


def test_audit_columns_report_expected_team_bonus() -> None:
    snapshot = _snapshot()
    result = {
        "snapshot": snapshot,
        "plan": SimpleNamespace(total_price=47_000_000, value=1234.5),
        "news_data": {},
        "selected_ids": {10},
        "uae_ids": {10},
        "snapshot_at": "2026-08-23T00:00:00+00:00",
        "projection": {"generated_at": "2026-08-22T00:00:00+00:00"},
        "game_info": {"MarketName": "Vuelta 2026"},
        "plan_source": "test",
        "cyclingoracle_source": None,
        "expert_weight": 0.1,
        "uncovered_riders": [],
        "stage_analysis_source": None,
        "expert_chat_source": None,
        "conditional_team_bonus_total": 8.0,
        "projected_individual_stage_total": 100.0,
        "projected_team_bonus_total": 12.5,
        "projected_stage_total": 112.5,
        "projected_classification_total": 20.0,
    }

    columns = _common_columns(result)

    assert columns["projected_team_bonus_points"] == "12.50"
    assert "expected teammate points are included" in columns["value_source"]
    assert "Conditional upside remains separate" in columns["uae_team_bonus_assumption"]


def test_stage_lineup_csv_reports_expected_team_bonus(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot()
    stage = snapshot.stages[0]
    lineup = SimpleNamespace(
        rider_ids=list(range(1, 10)),
        captain_id=1,
        captain_points=40.0,
        total=180.0,
    )
    expected_bonus = {
        (rider_id, stage.stage_id): float(rider_id) / 2
        for rider_id in range(1, 11)
    }
    result = {
        "snapshot": snapshot,
        "plan": SimpleNamespace(rider_ids=[], total_price=0, value=0.0),
        "scenarios": [],
        "season_values": {rider_id: 0.0 for rider_id in range(1, 11)},
        "lineups": [{
            "stage": stage,
            "lineup": lineup,
            "live": {
                "StartDate": "2026-08-22",
                "StartLocation": "A",
                "FinishLocation": "B",
                "Distance": 100,
            },
            "projection": {"profile_type": "flat", "finish_type": "flat"},
            "winner_team_id": 30,
            "uae_team_bonus_per_rider": 0.0,
            "ideal_ids": lineup.rider_ids,
            "individual_total": 160.0,
            "team_bonus_total": 20.0,
            "conditional_team_bonus_total": 10.0,
        }],
        "team_names": {30: "Team 30"},
        "model_projected_points": {
            (rider_id, stage.stage_id): 10.0 for rider_id in range(1, 11)
        },
        "expert_chat_signals": {},
        "individual_projected_points": {
            (rider_id, stage.stage_id): 10.0 for rider_id in range(1, 11)
        },
        "team_bonus_points": expected_bonus,
        "projected_points": {
            (rider_id, stage.stage_id): 10.0 + expected_bonus[(rider_id, stage.stage_id)]
            for rider_id in range(1, 11)
        },
        "stage_analysis_weights": {1: 1.0},
        "stage_analysis_statuses": {1: "test"},
    }
    monkeypatch.setattr(recommend_vuelta_live, "_common_columns", lambda _: {})
    monkeypatch.setattr(recommend_vuelta_live, "_rider_columns", lambda *_: {})
    output = tmp_path / "recommendation.csv"

    recommend_vuelta_live.write_combined_csv(result, output)

    rows = list(csv.DictReader(output.open(encoding="utf-8-sig")))
    stage_row = next(row for row in rows if row["record_type"] == "stage_lineup")
    assert stage_row["projected_team_bonus_points"] == "20.00"
    assert "Rider 1=0.50" in stage_row["lineup_team_bonus_points"]
    assert "Rider 9=4.50" in stage_row["lineup_team_bonus_points"]
    assert "Expected teammate points are included" in stage_row["uncertainty"]
    assert "scenario-dependent teammate upside is excluded" in stage_row["uncertainty"]
