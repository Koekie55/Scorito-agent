import pytest

from scorito_agent.forum_opinion import (
    FORUM_OPINION_SHARE,
    OPINION_MAX_ADJUSTMENT,
    blend_opinion_signals,
    compile_forum_opinion,
    forum_signal_for_stage,
)


def test_forum_claim_kinds_and_categories_remain_separate() -> None:
    digest = compile_forum_opinion(
        {
            "claims": [
                {
                    "rider": "Test Rider",
                    "rider_slug": "test-rider",
                    "kind": "reported_fact",
                    "category": "team_role",
                    "signal": 0.5,
                    "confidence": 1.0,
                    "stages": [5],
                },
                {
                    "rider": "Test Rider",
                    "rider_slug": "test-rider",
                    "kind": "interpretation",
                    "category": "form",
                    "signal": -0.5,
                    "confidence": 1.0,
                },
                {
                    "rider": "Test Rider",
                    "rider_slug": "test-rider",
                    "kind": "interpretation",
                    "category": "value",
                    "signal": 0.8,
                    "confidence": 1.0,
                },
                {
                    "rider": "Test Rider",
                    "rider_slug": "test-rider",
                    "kind": "guess",
                    "category": "team_role",
                    "signal": 1.0,
                    "confidence": 1.0,
                    "stages": [6],
                },
            ]
        }
    )

    rider = digest["riders"]["test-rider"]
    assert rider["performance_signal"] == -0.19
    assert rider["value_signal"] == pytest.approx(0.304)
    assert forum_signal_for_stage(rider, 5) == pytest.approx(0.17)
    assert forum_signal_for_stage(rider, 6) == -0.19
    assert forum_signal_for_stage(rider, 7) == -0.19
    assert digest["summary"]["zero_weight"] == 1


def test_forum_is_exactly_thirty_percent_of_opinion_blend() -> None:
    assert FORUM_OPINION_SHARE == 0.30
    assert blend_opinion_signals(0.4, 0.8) == pytest.approx(0.52)
    assert blend_opinion_signals(0.0, 1.0) == 0.30
    assert blend_opinion_signals(1.0, 1.0) == 1.0

def test_combined_opinion_cap_is_sixteen_percent() -> None:
    assert OPINION_MAX_ADJUSTMENT == 0.16
    assert OPINION_MAX_ADJUSTMENT * FORUM_OPINION_SHARE == pytest.approx(0.048)


def test_material_form_and_value_concerns_create_high_uncertainty() -> None:
    digest = compile_forum_opinion(
        {
            "claims": [
                {
                    "rider": "Risky Sprinter",
                    "rider_slug": "risky-sprinter",
                    "kind": "reported_fact",
                    "category": "form",
                    "signal": -0.45,
                    "confidence": 0.8,
                    "stages": [16, 17],
                    "text": "No recent sprints and uncertain form.",
                },
                {
                    "rider": "Risky Sprinter",
                    "rider_slug": "risky-sprinter",
                    "kind": "interpretation",
                    "category": "value",
                    "signal": -0.25,
                    "confidence": 0.75,
                    "text": "Too uncertain for the price.",
                },
            ]
        }
    )

    uncertainty = digest["riders"]["risky-sprinter"]["uncertainty"]
    assert uncertainty["level"] == "high"
    assert uncertainty["worst_stage_signal"] == pytest.approx(-0.2592)
    assert len(uncertainty["reasons"]) == 2