from scorito_agent.gc_bunch_mingling import (
    GRADIENT_CEILING_PCT,
    VERTICAL_METERS_MAX,
    VERTICAL_METERS_MIN,
    gc_bunch_credibility,
    is_bunch_mingling_eligible,
)


def _vuelta2026_stage2() -> dict:
    """The real stage that motivated this gate: hilly/uphill, top 25 tied on time."""
    return {
        "profile_type": "hilly",
        "finish_type": "uphill",
        "vertical_meters": 3028,
        "gradient_final_km": 3.6,
    }


def test_target_stage_is_eligible() -> None:
    assert is_bunch_mingling_eligible(_vuelta2026_stage2()) is True


def test_itt_stage_is_never_eligible() -> None:
    stage = {"profile_type": "itt", "finish_type": "tt", "vertical_meters": 100, "gradient_final_km": 0.0}
    assert is_bunch_mingling_eligible(stage) is False


def test_flat_stage_is_never_eligible() -> None:
    stage = {"profile_type": "flat", "finish_type": "sprint", "vertical_meters": 400, "gradient_final_km": 0.0}
    assert is_bunch_mingling_eligible(stage) is False


def test_summit_mountain_stage_is_never_eligible() -> None:
    stage = {"profile_type": "mountain", "finish_type": "summit", "vertical_meters": 4500, "gradient_final_km": 8.0}
    assert is_bunch_mingling_eligible(stage) is False


def test_steep_uphill_at_or_above_gradient_ceiling_is_not_eligible() -> None:
    """Grounded: 0/6 historical hilly-uphill stages >=6.0%/km kept a 10+ front group."""
    stage = dict(_vuelta2026_stage2())
    stage["gradient_final_km"] = GRADIENT_CEILING_PCT
    assert is_bunch_mingling_eligible(stage) is False


def test_flat_finish_type_on_a_hilly_stage_is_not_eligible() -> None:
    stage = dict(_vuelta2026_stage2())
    stage["finish_type"] = "flat"
    assert is_bunch_mingling_eligible(stage) is False


def test_vertical_outside_the_observed_band_is_not_eligible() -> None:
    too_low = dict(_vuelta2026_stage2())
    too_low["vertical_meters"] = VERTICAL_METERS_MIN - 1
    too_high = dict(_vuelta2026_stage2())
    too_high["vertical_meters"] = VERTICAL_METERS_MAX + 1
    assert is_bunch_mingling_eligible(too_low) is False
    assert is_bunch_mingling_eligible(too_high) is False


def test_gc_leader_gets_credibility_a_pure_sprinter_does_not() -> None:
    stage = _vuelta2026_stage2()
    gc_leader = {
        "signals": {"gc": 0.0, "climb": 0.0},
        "recent_evidence": {"profile_strength": {"hilly": 1.6116}},
    }
    pure_sprinter = {
        "signals": {"gc": 0.0, "climb": 0.0},
        "recent_evidence": {"profile_strength": {"hilly": 0.05}},
    }
    assert gc_bunch_credibility(stage, gc_leader) == 1.0
    assert gc_bunch_credibility(stage, pure_sprinter) == 0.05


def test_credibility_is_zero_when_stage_is_not_eligible() -> None:
    stage = {"profile_type": "mountain", "finish_type": "summit", "vertical_meters": 4500, "gradient_final_km": 8.0}
    gc_leader = {
        "signals": {"gc": 0.9, "climb": 0.9},
        "recent_evidence": {"profile_strength": {"hilly": 1.5}},
    }
    assert gc_bunch_credibility(stage, gc_leader) == 0.0


def test_credibility_uses_signals_when_stronger_than_profile_strength() -> None:
    stage = _vuelta2026_stage2()
    rider = {
        "signals": {"gc": 0.7, "climb": 0.4},
        "recent_evidence": {"profile_strength": {"hilly": 0.2}},
    }
    assert gc_bunch_credibility(stage, rider) == 0.7