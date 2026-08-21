from scripts.refresh_vuelta_stage_predictions import (
    TOP_N,
    _hilly_attrition_factor,
    _mountain_finish_factor,
    _stage_selectivity,
    _startlist_change,
    _sprint_survival_score,
    _survival_factor,
    build_stage_top20,
)


def _projection() -> dict:
    riders = [
        {
            "rider": f"Rider {index}",
            "rider_slug": f"rider-{index}",
            "team": f"Team {index % 4}",
            "model_qualities": {
                "gc": 10 - index / 10,
                "climb": 9 - index / 10,
                "time_trial": index / 10,
                "sprint": index / 8,
                "punch": index / 9,
                "hill": index / 10,
            },
        }
        for index in range(1, 22)
    ]
    stages = [
        {
            "stage_no": stage_no,
            "date": f"2026-08-{stage_no:02d}",
            "distance_km": 9 if stage_no == 1 else 170,
            "profile_type": "itt" if stage_no == 1 else ("flat" if stage_no == 2 else "mountain"),
            "finish_type": "tt" if stage_no == 1 else ("sprint" if stage_no == 2 else "summit"),
        }
        for stage_no in range(1, 22)
    ]
    rankings = {
        str(stage_no): [
            {
                "rank": index,
                "rider": rider["rider"],
                "rider_slug": rider["rider_slug"],
                "score": 22 - index,
                "expected_finish_band": [max(1, index - 2), index + 2],
                "confidence": 0.7,
                "uncertainty": "medium-high",
                "role_assumption": "test",
                "evidence": "test evidence",
            }
            for index, rider in enumerate(riders, start=1)
        ]
        for stage_no in range(1, 22)
    }
    return {
        "generated_at": "2026-08-18T00:00:00+00:00",
        "model_version": "test",
        "riders": riders,
        "stages": stages,
        "stage_rankings": rankings,
        "sources": {"startlist": "https://example.test/startlist"},
    }


def test_startlist_change_reports_added_and_removed_riders() -> None:
    projection = _projection()
    live = [{"rider_slug": f"rider-{index}"} for index in range(2, 23)]

    added, removed = _startlist_change(projection, live)

    assert added == {"rider-22"}
    assert removed == {"rider-1"}


def test_stage_export_has_21_sets_of_20_unique_pcs_participants() -> None:
    expert = {
        "weight_applied": 0.15,
        "generated_at": "2026-08-18T00:00:00+00:00",
        "stage_breakdown": {
            "1": {"type": "ITT", "distance_km": 9},
            "2": {"type": "Sprint"},
            **{str(stage_no): {"type": "GC / Mountain"} for stage_no in range(3, 22)},
        },
    }
    news = {
        "generated_at": "2026-08-18T01:00:00+00:00",
        "market_snapshot": None,
        "selection_impacts": [
            {
                "rider_slug": "rider-1",
                "impact": "negative",
                "verification": "official_source",
                "decision_hint": "review_selection_and_lineup",
                "title": "Availability update",
                "url": "https://example.test/news",
                "published_at": "2026-08-18T00:30:00+00:00",
                "score": 99,
            }
        ],
    }

    report = build_stage_top20(_projection(), expert, news)

    assert len(report["stages"]) == 21
    assert report["known_pcs_participants"] == 21
    for stage in report["stages"]:
        assert len(stage["top_20"]) == TOP_N
        assert len({row["rider_slug"] for row in stage["top_20"]}) == TOP_N
        assert stage["expert_weight"] == 0.15
    mountain_rider = next(
        row for row in report["stages"][2]["top_20"] if row["rider_slug"] == "rider-1"
    )
    assert mountain_rider["news"]["decision_hint"] == "review_selection_and_lineup"
    assert mountain_rider["news_rank_adjustment"] == 0
    assert mountain_rider["news_multiplier"] == 0.10
    assert [row["scorito_stage_points"] for row in report["stages"][0]["top_20"]] == [
        50, 44, 40, 36, 32, 30, 28, 26, 24, 22,
        20, 18, 16, 14, 12, 10, 8, 6, 4, 1,
    ]
def test_selective_hilly_stage_rewards_durable_sprinter() -> None:
    stage = {
        "profile_type": "hilly",
        "finish_type": "flat",
        "vertical_meters": 3000,
        "gradient_final_km": 0.0,
    }
    notes = {"type": "Sprint / Breakaway", "climbs": "late 4km @ 6% climb"}
    pure_sprinter = {
        "model_qualities": {"punch": 3.0, "hill": 2.0, "climb": 1.0},
        "signals": {"classic": 0.1},
        "recent_evidence": {"profile_strength": {"hilly": 0.35, "mountain": 0.15}},
    }
    durable_sprinter = {
        "model_qualities": {"punch": 5.0, "hill": 4.0, "climb": 3.0},
        "signals": {"classic": 0.5},
        "recent_evidence": {"profile_strength": {"hilly": 1.20, "mountain": 0.65}},
    }

    selectivity = _stage_selectivity(stage, notes)
    pure_survival = _sprint_survival_score(pure_sprinter)
    durable_survival = _sprint_survival_score(durable_sprinter)

    assert selectivity >= 0.48
    assert durable_survival > pure_survival
    assert _survival_factor(selectivity, durable_survival) > _survival_factor(
        selectivity, pure_survival
    )
    assert _stage_selectivity(
        {"profile_type": "flat", "vertical_meters": 1000}, {"type": "Sprint"}
    ) == 0.0


def test_final_25km_audit_has_three_sprint_selectivity_levels() -> None:
    stage = {
        "profile_type": "hilly",
        "finish_type": "flat",
        "vertical_meters": 2800,
        "gradient_final_km": 0.0,
    }
    retained = {
        "type": "Sprint / Hilly",
        "final_50km": {"sprinters_retained": True},
    }
    weak_dropped = {
        "type": "Sprint / Hilly",
        "final_50km": {
            "sprinters_retained": True,
            "weak_sprinters_dropped": True,
        },
    }
    hard_drop = {
        "type": "Sprint / Hilly",
        "final_50km": {
            "sprinters_retained": False,
            "sprinter_drop_climb_last_25km": True,
        },
    }

    assert _stage_selectivity(stage, retained) == 0.28
    assert _stage_selectivity(stage, weak_dropped) == 0.58
    assert _stage_selectivity(stage, hard_drop) == 0.78


def test_mountain_finish_factor_rejects_unsupported_sprinter() -> None:
    stage = {
        "profile_type": "mountain",
        "finish_type": "summit",
        "vertical_meters": 4_500,
    }
    sprinter = {
        "signals": {"gc": 0.03, "climb": 0.0},
        "recent_evidence": {"contextual_results": []},
    }
    climber = {
        "signals": {"gc": 0.10, "climb": 0.12},
        "recent_evidence": {
            "contextual_results": [
                {
                    "source_url": f"https://example.test/mountain-{rank}",
                    "profile_type": "mountain",
                    "year": 2026,
                    "rank": rank,
                }
                for rank in (4, 8, 12)
            ]
        },
    }

    sprinter_factor, sprinter_credibility = _mountain_finish_factor(stage, sprinter)
    climber_factor, climber_credibility = _mountain_finish_factor(stage, climber)

    assert sprinter_credibility == 0.03
    assert sprinter_factor < 0.2
    assert climber_credibility == 1.0
    assert climber_factor == 1.0


def test_hilly_attrition_penalizes_only_low_survival_sprinters() -> None:
    stage = {"profile_type": "hilly", "finish_type": "flat"}
    weak_drop = {"final_50km": {"weak_sprinters_dropped": True}}
    hard_drop = {"final_50km": {"sprinter_drop_climb_last_25km": True}}
    gc_finish = {"type": "Punch / GC Finish"}

    assert _hilly_attrition_factor(stage, weak_drop, 0.26) == 0.35
    assert _hilly_attrition_factor(stage, weak_drop, 0.50) == 1.0
    assert _hilly_attrition_factor(stage, hard_drop, 0.26) == 0.08
    assert _hilly_attrition_factor(stage, hard_drop, 0.50) == 0.65
    assert _hilly_attrition_factor(stage, gc_finish, 0.26) == 0.25
    assert _hilly_attrition_factor(stage, gc_finish, 0.50) == 1.0

