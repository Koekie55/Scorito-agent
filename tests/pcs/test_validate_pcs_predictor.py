from scripts.validate_pcs_predictor import (
    _is_before_target,
    _production_training_pool,
)


def test_training_stage_must_precede_target_stage_number() -> None:
    target = {"stage_no": 2, "date": "2026-08-24"}

    assert _is_before_target({"stage_no": 1, "date": "2026-08-25"}, target)
    assert not _is_before_target({"stage_no": 2, "date": "2026-08-23"}, target)
    assert not _is_before_target({"stage_no": 3, "date": "2026-08-23"}, target)


def test_training_stage_uses_date_when_stage_numbers_are_missing() -> None:
    target = {"date": "2026-08-24"}

    assert _is_before_target({"date": "2026-08-23"}, target)
    assert not _is_before_target({"date": "2026-08-24"}, target)
    assert not _is_before_target({"date": "not-a-date"}, target)


def test_production_pool_uses_prior_races_but_not_target_race_stages() -> None:
    stages = [
        {"race": "vuelta2025", "year": 2025, "stage_no": 21, "results": [{}]},
        {"race": "giro2026", "year": 2026, "stage_no": 21, "results": [{}]},
        {"race": "tdf2026", "year": 2026, "stage_no": 1, "results": [{}]},
        {"race": "tdf2026", "year": 2026, "stage_no": 2, "results": [{}]},
        {"race": "vuelta2026", "year": 2026, "stage_no": 1, "results": [{}]},
    ]

    pool = _production_training_pool(stages, stages[3])

    assert [(stage["race"], stage["stage_no"]) for stage in pool] == [
        ("vuelta2025", 21),
        ("giro2026", 21),
    ]