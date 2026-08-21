"""Forward squad recommender tests (scripts/recommend.py).

The recommender is a *script* (not part of the ``scorito_agent`` package), so it
is imported here by file path. These tests validate the forward "build a winning
team" contract on the committed real snapshots:

* the blind squad is a legal 20-rider team priced within budget;
* its graded real-points total never exceeds the hindsight ceiling; and
* the out-of-sample flag is wired correctly (train Giro -> pick Tour).

They are skipped automatically when the snapshots are absent.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]
DATA_ROOT = REPO / "data" / "scorito"
TDF = DATA_ROOT / "tdf2026"
GIRO = DATA_ROOT / "giro2026"
RECOMMEND_PY = REPO / "scripts" / "recommend.py"


def _load_recommend():
    spec = importlib.util.spec_from_file_location("recommend", RECOMMEND_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_valid_recommendation(rec: dict, snap_budget: int | None = None) -> None:
    # Structural keys the JSON artifact / CLI depend on.
    for key in (
        "market_id",
        "slug",
        "train_slug",
        "out_of_sample",
        "budget",
        "captain_factor",
        "ceiling_season_total",
        "models",
        "recommended_model",
        "recommended_squad",
        "recommended_price",
        "recommended_real_season_total",
    ):
        assert key in rec, f"missing key {key!r}"

    # A legal squad: at most 20 riders, priced within budget.
    squad = rec["recommended_squad"]
    assert 0 < len(squad) <= 20
    assert len({r["rider_id"] for r in squad}) == len(squad)  # no duplicates
    assert rec["recommended_price"] <= rec["budget"]
    if snap_budget is not None:
        assert rec["budget"] == snap_budget

    # Both models graded; ceiling is a genuine upper bound.
    ceiling = rec["ceiling_season_total"]
    assert ceiling > 0
    for name in ("heuristic", "fitted"):
        m = rec["models"][name]
        assert m["price"] <= rec["budget"]
        assert 0 <= m["real_season_total"] <= ceiling + 1e-6
        assert 0.0 <= m["pct_of_ceiling"] <= 100.0 + 1e-6

    # The recommended model is the better-grading one, and no squad beats the ceiling.
    assert rec["recommended_model"] in ("heuristic", "fitted")
    assert rec["recommended_real_season_total"] <= ceiling + 1e-6


@pytest.mark.skipif(not TDF.exists(), reason="tdf2026 snapshot not present")
def test_build_recommendation_in_sample() -> None:
    mod = _load_recommend()
    rec = mod.build_recommendation("tdf2026", None)

    assert rec["out_of_sample"] is False
    assert rec["train_slug"] == "tdf2026"
    _assert_valid_recommendation(rec, snap_budget=45_000_000)


@pytest.mark.skipif(
    not (TDF.exists() and GIRO.exists()),
    reason="tdf2026/giro2026 snapshots not present",
)
def test_build_recommendation_out_of_sample() -> None:
    mod = _load_recommend()
    rec = mod.build_recommendation("tdf2026", "giro2026")

    assert rec["out_of_sample"] is True
    assert rec["train_slug"] == "giro2026"
    _assert_valid_recommendation(rec, snap_budget=45_000_000)
