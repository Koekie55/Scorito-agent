"""Audit source weights using immutable pre-stage Vuelta forecast archives.

The archive only contains the 20 riders forecast for each stage. Consequently,
this is a conservative calibration of source contributions within the already
selected candidate set, not a full-field model replacement.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "scorito" / "vuelta2026"
ARCHIVES = DATA_DIR / "stage_predictability" / "predictions"
EVALUATIONS = DATA_DIR / "stage_predictability" / "evaluations"
OUTPUT_PATH = DATA_DIR / "source_weight_calibration.json"
FEATURE_NAMES = (
    "pcs_model_score",
    "tv2_stars",
    "stage_expert_signal",
    "expert_chat_signal",
    "forum_signal",
    "course_factor",
)
DEFAULT_SIMULATIONS = 10_000
DEFAULT_SEED = 20260909
RIDGE_ALPHA = 2.0


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _actual_positions(path: Path) -> dict[int, int]:
    return {
        int(row["rider_id"]): int(row["actual_finish"])
        for row in _load(path)["actual_top_20"]
    }


def _course_factor(row: dict[str, Any]) -> float:
    return float(
        row.get("sprint_survival_factor", 1.0)
        * row.get("mountain_finish_factor", 1.0)
        * row.get("hilly_attrition_factor", 1.0)
        * row.get("conversion_factor", 1.0)
    )


def observations() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return standardized-fit inputs, scored outcome, stage IDs and archived rank."""
    features: list[list[float]] = []
    outcome: list[float] = []
    stages: list[int] = []
    archived_rank: list[int] = []
    for evaluation_path in sorted(EVALUATIONS.glob("stage_*.json")):
        stage_no = int(evaluation_path.stem.split("_")[1])
        archive_path = ARCHIVES / evaluation_path.name
        if not archive_path.exists():
            continue
        actual = _actual_positions(evaluation_path)
        for row in _load(archive_path)["top_20"]:
            actual_finish = actual.get(int(row["rider_id"]), 21)
            features.append([
                float(row.get("pcs_model_score") or 0.0),
                float(row.get("tv2_axelgaard_stars") or 0.0) / 5.0,
                float(row.get("stage_expert_signal") or 0.0),
                float(row.get("expert_chat_signal") or 0.0),
                float(row.get("wielerflits_forum_signal") or 0.0),
                _course_factor(row),
            ])
            # A top-20 finish is a decreasing, rank-sensitive target; misses score zero.
            outcome.append(max(0.0, 21.0 - actual_finish) / 20.0)
            stages.append(stage_no)
            archived_rank.append(int(row["predicted_finish"]))
    if not features:
        raise RuntimeError("no matched immutable archives/evaluations available")
    return (
        np.asarray(features, dtype=float),
        np.asarray(outcome, dtype=float),
        np.asarray(stages, dtype=int),
        np.asarray(archived_rank, dtype=int),
    )


def _fit_predict(
    train_x: np.ndarray, train_y: np.ndarray, test_x: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    means = train_x.mean(axis=0)
    scales = train_x.std(axis=0)
    scales[scales == 0.0] = 1.0
    standardized = (train_x - means) / scales
    design = np.column_stack((np.ones(len(standardized)), standardized))
    penalty = np.diag([0.0] + [RIDGE_ALPHA] * len(FEATURE_NAMES))
    coefficients = np.linalg.solve(design.T @ design + penalty, design.T @ train_y)
    test_design = np.column_stack((np.ones(len(test_x)), (test_x - means) / scales))
    return test_design @ coefficients, coefficients[1:] / scales


def _mean_matched_rank_error(predictions: np.ndarray, outcome: np.ndarray, stages: np.ndarray) -> float:
    errors = []
    for stage_no in np.unique(stages):
        mask = stages == stage_no
        predicted_order = np.argsort(-predictions[mask], kind="stable") + 1
        actual_order = np.argsort(-outcome[mask], kind="stable") + 1
        errors.append(float(np.mean(np.abs(predicted_order - actual_order))))
    return float(np.mean(errors))


def leave_one_stage_out(
    x: np.ndarray, y: np.ndarray, stages: np.ndarray, archived_rank: np.ndarray
) -> dict[str, Any]:
    learned = np.zeros(len(y), dtype=float)
    weights = []
    for stage_no in np.unique(stages):
        test = stages == stage_no
        prediction, coefficients = _fit_predict(x[~test], y[~test], x[test])
        learned[test] = prediction
        weights.append(coefficients)
    archived_score = -archived_rank.astype(float)
    learned_error = _mean_matched_rank_error(learned, y, stages)
    archived_error = _mean_matched_rank_error(archived_score, y, stages)
    return {
        "learned_predictions": learned,
        "coefficient_draws": np.asarray(weights),
        "archived_rank_error": archived_error,
        "learned_rank_error": learned_error,
        "rank_error_improvement": archived_error - learned_error,
    }


def bootstrap(x: np.ndarray, y: np.ndarray, stages: np.ndarray, *, simulations: int, seed: int) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    stage_values = np.unique(stages)
    weights = np.empty((simulations, len(FEATURE_NAMES)), dtype=float)
    for draw in range(simulations):
        sampled_stages = rng.choice(stage_values, size=len(stage_values), replace=True)
        indices = np.concatenate([np.flatnonzero(stages == stage_no) for stage_no in sampled_stages])
        _, coefficients = _fit_predict(x[indices], y[indices], x[indices])
        weights[draw] = coefficients
    return {
        name: {
            "coefficient": round(float(np.median(weights[:, index])), 6),
            "bootstrap_95ci": [
                round(float(np.percentile(weights[:, index], 2.5)), 6),
                round(float(np.percentile(weights[:, index], 97.5)), 6),
            ],
            "probability_positive": round(float(np.mean(weights[:, index] > 0.0)), 4),
        }
        for index, name in enumerate(FEATURE_NAMES)
    }


def calibrate(*, simulations: int, seed: int) -> dict[str, Any]:
    x, y, stages, archived_rank = observations()
    held_out = leave_one_stage_out(x, y, stages, archived_rank)
    sampled = bootstrap(x, y, stages, simulations=simulations, seed=seed)
    stable_positive = [
        name for name, result in sampled.items()
        if result["bootstrap_95ci"][0] > 0.0 and result["probability_positive"] >= 0.95
    ]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "completed immutable Vuelta stage archives; forecast top-20 candidates only",
        "method": (
            "Ridge regression of rank-sensitive realised top-20 outcome on archived, pre-stage "
            "source fields. Weight fit uses leave-one-stage-out validation; 10,000 stage-cluster "
            "bootstrap draws quantify coefficient uncertainty."
        ),
        "limitations": [
            "The archive omits riders outside the predicted top 20, so it cannot validate full-field recall.",
            "Ten evaluated stages are insufficient to safely replace production weights.",
            "Correlated PCS, course and expert signals make individual coefficients exploratory.",
        ],
        "ridge_alpha": RIDGE_ALPHA,
        "evaluated_stages": [int(value) for value in np.unique(stages)],
        "observations": int(len(y)),
        "leave_one_stage_out": {
            "archived_mean_rank_error": round(held_out["archived_rank_error"], 4),
            "learned_mean_rank_error": round(held_out["learned_rank_error"], 4),
            "improvement": round(held_out["rank_error_improvement"], 4),
        },
        "monte_carlo_weight_regression": {
            "simulations": simulations,
            "seed": seed,
            "coefficients": sampled,
        },
        "recommendation": {
            "stable_positive_sources": stable_positive,
            "decision": (
                "Do not change production weights from this Vuelta-only calibration unless at least "
                "one source is stable-positive and the held-out rank error improves. Use this output "
                "to prioritise a pre-2026 full-field replay, where CyclingOracle feature weights can "
                "be refit by stage profile."
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulations", type=int, default=DEFAULT_SIMULATIONS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    report = calibrate(simulations=args.simulations, seed=args.seed)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["leave_one_stage_out"], indent=2))
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())