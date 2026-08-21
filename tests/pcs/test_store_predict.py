from pathlib import Path

from scorito_agent.pcs.predict import find_similar_stages, predict_finishers, stage_similarity
from scorito_agent.pcs.store import StageStore

DATA_TEST_STORE = Path(__file__).parents[2] / "data" / "pcs" / "test_stages.json"


def _stage(stage_id: str, profile: str, finish: str, distance: float, vertical: int, results: list[dict]) -> dict:
    return {
        "id": stage_id,
        "race": "Synthetic Race",
        "stage_no": stage_id,
        "profile_type": profile,
        "finish_type": finish,
        "distance_km": distance,
        "vertical_meters": vertical,
        "results": results,
    }


def test_stage_store_upsert_and_query() -> None:
    if DATA_TEST_STORE.exists():
        DATA_TEST_STORE.unlink()
    store = StageStore(DATA_TEST_STORE)
    try:
        store.upsert_stage(_stage("mountain-1", "mountain", "summit", 180, 4200, []))
        store.upsert_stage(_stage("flat-1", "flat", "sprint", 160, 600, []))

        assert store.get("mountain-1")["profile_type"] == "mountain"
        assert [stage["id"] for stage in store.query(profile_type="flat")] == ["flat-1"]
    finally:
        if DATA_TEST_STORE.exists():
            DATA_TEST_STORE.unlink()


def test_predictor_prefers_similar_mountain_finish_and_filters_participants() -> None:
    past_stages = [
        _stage(
            "mountain-1",
            "mountain",
            "summit",
            185,
            3800,
            [
                {"rank": 1, "rider": "Jonas Vingegaard", "rider_slug": "jonas-vingegaard", "team": "Visma | Lease a Bike"},
                {"rank": 2, "rider": "Remco Evenepoel", "rider_slug": "remco-evenepoel", "team": "Soudal Quick-Step"},
                {"rank": 3, "rider": "Primož Roglič", "rider_slug": "primoz-roglic", "team": "Red Bull"},
            ],
        ),
        _stage(
            "flat-1",
            "flat",
            "sprint",
            170,
            550,
            [
                {"rank": 1, "rider": "Tim Merlier", "rider_slug": "tim-merlier", "team": "Soudal Quick-Step"},
                {"rank": 2, "rider": "Jonas Vingegaard", "rider_slug": "jonas-vingegaard", "team": "Visma | Lease a Bike"},
            ],
        ),
    ]
    target = {
        "profile_type": "mountain",
        "finish_type": "summit",
        "distance_km": 182,
        "vertical_meters": 3900,
        "participants": [
            {"rider": "Jonas Vingegaard", "rider_slug": "jonas-vingegaard", "team": "Visma | Lease a Bike"},
            {"rider": "Remco Evenepoel", "rider_slug": "remco-evenepoel", "team": "Soudal Quick-Step"},
            {"rider": "Tim Merlier", "rider_slug": "tim-merlier", "team": "Soudal Quick-Step"},
        ],
        "tactics": {"protected_riders": ["Remco Evenepoel"]},
    }

    assert stage_similarity(target, past_stages[0]) > stage_similarity(target, past_stages[1])
    similar = find_similar_stages(target, past_stages, k=1)
    assert similar[0]["id"] == "mountain-1"

    prediction = predict_finishers(target, past_stages, k=2, top_n=3)
    slugs = [row["rider_slug"] for row in prediction["predictions"]]

    assert slugs[0] == "jonas-vingegaard"
    assert "primoz-roglic" not in slugs
    assert set(slugs).issubset({"jonas-vingegaard", "remco-evenepoel", "tim-merlier"})
