"""Out-of-sample Stage 2 evaluation of expected Scorito teammate points."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from scorito_agent.scorito.loader import load_snapshot  # noqa: E402
from scorito_agent.scorito.team_points import (  # noqa: E402
    CLASSIFICATION_TEAM_POINT_TYPES,
    DEFAULT_CLASSIFICATION_RETENTION,
    STAGE_WIN_TEAM_POINTS,
    expected_team_points_by_rider,
    latest_classification_team_state,
    normalized_team_win_probabilities,
)

PREDICTION_GIT_PATH = "data/public/vuelta2026/top20_per_stage.json"
OUTPUT_PATH = ROOT / "data" / "scorito" / "vuelta2026" / "model_evaluation_stage2.json"
STAGE_NO = 2
LINEUP_SIZE = 9
DEFAULT_SEED = 20260823
DEFAULT_SIMULATIONS = 10_000


def _name_key(name: str) -> tuple[str, ...]:
    import re
    import unicodedata

    normalized = unicodedata.normalize("NFKD", str(name))
    ascii_name = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return tuple(sorted(re.findall(r"[a-z0-9]+", ascii_name.lower())))


def _load_pre_result_predictions(git_ref: str) -> tuple[dict[str, Any], str]:
    process = subprocess.run(
        ["git", "show", f"{git_ref}:{PREDICTION_GIT_PATH}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(process.stdout), hashlib.sha256(process.stdout.encode()).hexdigest()


def _stage_prediction(payload: dict[str, Any]) -> dict[str, Any]:
    return next(
        stage for stage in payload["stages"] if int(stage["stage_no"]) == STAGE_NO
    )


def _metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    errors = predicted - actual
    centered = actual - actual.mean()
    total_variance = float(np.dot(centered, centered))
    residual_variance = float(np.dot(errors, errors))
    correlation = float(np.corrcoef(actual, predicted)[0, 1])
    return {
        "mae": float(np.mean(np.abs(errors))),
        "rmse": float(math.sqrt(np.mean(errors**2))),
        "r_squared_identity": 1.0 - residual_variance / total_variance,
        "pearson": correlation if math.isfinite(correlation) else 0.0,
    }


def _fit_regression(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    design = np.column_stack((np.ones(len(predicted)), predicted))
    coefficients, *_ = np.linalg.lstsq(design, actual, rcond=None)
    fitted = design @ coefficients
    residual = actual - fitted
    total = actual - actual.mean()
    denominator = float(np.dot(total, total))
    return {
        "intercept": float(coefficients[0]),
        "slope": float(coefficients[1]),
        "r_squared": 1.0 - float(np.dot(residual, residual)) / denominator,
        "calibrated_rmse": float(math.sqrt(np.mean(residual**2))),
    }


def _percentile_summary(values: np.ndarray) -> dict[str, float]:
    return {
        "p025": float(np.percentile(values, 2.5)),
        "median": float(np.percentile(values, 50.0)),
        "p975": float(np.percentile(values, 97.5)),
    }


def cluster_bootstrap_regression(
    actual: np.ndarray,
    predictions: dict[str, np.ndarray],
    team_ids: np.ndarray,
    *,
    simulations: int,
    seed: int,
) -> dict[str, Any]:
    """Bootstrap trade teams and refit one-variable OLS models."""
    rng = np.random.default_rng(seed)
    unique_teams = np.unique(team_ids)
    indices_by_team = {
        team_id: np.flatnonzero(team_ids == team_id) for team_id in unique_teams
    }
    model_samples = {
        name: {"slope": [], "r_squared": [], "calibrated_rmse": []}
        for name in predictions
    }
    raw_rmse_delta = []
    lineup_delta = []
    for _ in range(simulations):
        sampled_teams = rng.choice(unique_teams, size=len(unique_teams), replace=True)
        sample_indices = np.concatenate(
            [indices_by_team[team_id] for team_id in sampled_teams]
        )
        sample_actual = actual[sample_indices]
        for name, predicted in predictions.items():
            fit = _fit_regression(sample_actual, predicted[sample_indices])
            for metric in model_samples[name]:
                model_samples[name][metric].append(fit[metric])
        baseline_error = predictions["individual_only"][sample_indices] - sample_actual
        enhanced_error = predictions["probability_weighted"][sample_indices] - sample_actual
        raw_rmse_delta.append(
            math.sqrt(float(np.mean(baseline_error**2)))
            - math.sqrt(float(np.mean(enhanced_error**2)))
        )
        base_top = np.argsort(-predictions["individual_only"][sample_indices])[:LINEUP_SIZE]
        enhanced_top = np.argsort(-predictions["probability_weighted"][sample_indices])[:LINEUP_SIZE]
        lineup_delta.append(
            float(sample_actual[enhanced_top].sum() - sample_actual[base_top].sum())
        )

    return {
        "method": "trade-team cluster bootstrap with replacement; OLS actual = intercept + slope * prediction",
        "seed": seed,
        "simulations": simulations,
        "models": {
            name: {
                metric: _percentile_summary(np.asarray(values))
                for metric, values in metrics.items()
            }
            for name, metrics in model_samples.items()
        },
        "probability_weighted_minus_individual": {
            "raw_rmse_improvement": _percentile_summary(np.asarray(raw_rmse_delta)),
            "probability_rmse_improves": float(np.mean(np.asarray(raw_rmse_delta) > 0)),
            "realized_top9_points_delta": _percentile_summary(np.asarray(lineup_delta)),
            "probability_top9_improves": float(np.mean(np.asarray(lineup_delta) > 0)),
        },
    }


def evaluate(*, git_ref: str, simulations: int, seed: int) -> dict[str, Any]:
    snapshot = load_snapshot("vuelta2026")
    stage = snapshot.stage_by_order(STAGE_NO)
    if stage is None:
        raise RuntimeError("Vuelta Stage 2 is missing")
    if not any(
        market_round_id == stage.market_round_id
        for market_round_id, _rider_id in snapshot.stage_point_components
    ):
        raise RuntimeError("Vuelta Stage 2 has no completed Scorito points")

    prediction_payload, prediction_hash = _load_pre_result_predictions(git_ref)
    predicted_stage = _stage_prediction(prediction_payload)
    riders_by_name = {_name_key(rider.name): rider for rider in snapshot.riders}
    individual_by_id = {rider.rider_id: 0.0 for rider in snapshot.riders}
    win_scores: dict[int, float] = {}
    prediction_rows = []
    for row in predicted_stage["top_20"]:
        rider = riders_by_name.get(_name_key(row["rider"]))
        if rider is None:
            continue
        individual_by_id[rider.rider_id] = float(row["scorito_stage_points"])
        win_scores[rider.rider_id] = float(row["combined_score"])
        prediction_rows.append(
            {
                "rider_id": rider.rider_id,
                "rider": rider.name,
                "predicted_finish": int(row["predicted_finish"]),
                "individual_points": float(row["scorito_stage_points"]),
                "win_score": float(row["combined_score"]),
            }
        )

    team_win_probabilities = normalized_team_win_probabilities(snapshot, win_scores)
    expected = expected_team_points_by_rider(
        snapshot,
        stage_order=STAGE_NO,
        team_win_probabilities=team_win_probabilities,
    )
    source_stage, prior_state = latest_classification_team_state(
        snapshot,
        before_stage_order=STAGE_NO,
    )
    predicted_winner_id = max(win_scores, key=win_scores.get)
    predicted_winner_team = snapshot.rider(predicted_winner_id).team_id

    rider_ids = np.asarray([rider.rider_id for rider in snapshot.riders], dtype=int)
    team_ids = np.asarray([rider.team_id for rider in snapshot.riders], dtype=int)
    actual = np.asarray(
        [snapshot.actual_points(rider_id, stage) for rider_id in rider_ids],
        dtype=float,
    )
    individual = np.asarray(
        [individual_by_id[int(rider_id)] for rider_id in rider_ids],
        dtype=float,
    )
    probability_weighted = np.asarray(
        [
            individual_by_id[int(rider_id)] + expected[int(rider_id)].total
            for rider_id in rider_ids
        ],
        dtype=float,
    )
    certain = np.asarray(
        [
            individual_by_id[int(rider_id)]
            + sum(prior_state.get(int(team_id), {}).values())
            + (STAGE_WIN_TEAM_POINTS if int(team_id) == predicted_winner_team else 0.0)
            for rider_id, team_id in zip(rider_ids, team_ids)
        ],
        dtype=float,
    )
    predictions = {
        "individual_only": individual,
        "full_certain": certain,
        "probability_weighted": probability_weighted,
    }

    model_results = {
        name: {
            "raw_metrics": _metrics(actual, values),
            "regression": _fit_regression(actual, values),
        }
        for name, values in predictions.items()
    }
    top_nine = {}
    for name, values in predictions.items():
        selected_indices = np.argsort(-values, kind="stable")[:LINEUP_SIZE]
        top_nine[name] = {
            "riders": [snapshot.rider(int(rider_ids[index])).name for index in selected_indices],
            "predicted_points": float(values[selected_indices].sum()),
            "realized_points": float(actual[selected_indices].sum()),
        }

    actual_components = {
        str(points_type): float(
            sum(
                snapshot.actual_point_components(int(rider_id), stage).get(points_type, 0.0)
                for rider_id in rider_ids
            )
        )
        for points_type in sorted(CLASSIFICATION_TEAM_POINT_TYPES | {2})
    }
    bootstrap = cluster_bootstrap_regression(
        actual,
        predictions,
        team_ids,
        simulations=simulations,
        seed=seed,
    )
    oliveira = next(rider for rider in snapshot.riders if rider.name == "Ivo Oliveira")
    oliveira_index = int(np.flatnonzero(rider_ids == oliveira.rider_id)[0])
    individual_ninth = float(np.sort(individual)[-LINEUP_SIZE])
    oliveira_case = {
        "rider_id": oliveira.rider_id,
        "rider": oliveira.name,
        "team_id": oliveira.team_id,
        "predicted_individual_points": float(individual[oliveira_index]),
        "expected_classification_team_points": expected[
            oliveira.rider_id
        ].classification_points,
        "expected_stage_win_team_points": expected[
            oliveira.rider_id
        ].stage_win_points,
        "expected_total_points": float(probability_weighted[oliveira_index]),
        "actual_points": float(actual[oliveira_index]),
        "actual_components": snapshot.actual_point_components(oliveira.rider_id, stage),
        "market_ninth_individual_prediction": individual_ninth,
        "would_replace_market_ninth": bool(
            probability_weighted[oliveira_index] > individual_ninth
        ),
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "evaluation_status": "out_of_sample_stage2",
        "stage_no": STAGE_NO,
        "prediction_snapshot": {
            "git_ref": git_ref,
            "git_path": PREDICTION_GIT_PATH,
            "sha256": prediction_hash,
            "generated_at": prediction_payload.get("generated_at"),
            "status": prediction_payload.get("status"),
            "frozen_stage_rows": prediction_rows,
        },
        "actual_source": "live Scorito points_totalpoints.json component snapshot",
        "actual_rider_count": len(rider_ids),
        "actual_component_totals": actual_components,
        "assumptions": {
            "classification_retention": {
                str(key): value for key, value in DEFAULT_CLASSIFICATION_RETENTION.items()
            },
            "classification_source_stage": source_stage,
            "classification_team_state": {
                str(team_id): {str(key): value for key, value in values.items()}
                for team_id, values in prior_state.items()
            },
            "team_win_probabilities": {
                str(key): value for key, value in team_win_probabilities.items()
            },
            "full_certain_stage_win_team": predicted_winner_team,
            "captain_points_excluded": True,
        },
        "models": model_results,
        "market_top9": top_nine,
        "oliveira_case_study": oliveira_case,
        "monte_carlo_regression": bootstrap,
        "conclusion_rule": (
            "Adopt for enrollment only when probability-weighted expected points exceed "
            "the ninth rider; keep captain ranking on individual win evidence."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--git-ref", default="HEAD")
    parser.add_argument("--simulations", type=int, default=DEFAULT_SIMULATIONS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    report = evaluate(
        git_ref=args.git_ref,
        simulations=args.simulations,
        seed=args.seed,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    delta = report["monte_carlo_regression"]["probability_weighted_minus_individual"]
    print(f"Wrote {args.output}")
    print(
        "P(weighted RMSE improves)="
        f"{delta['probability_rmse_improves']:.3f}; "
        "P(weighted top9 improves)="
        f"{delta['probability_top9_improves']:.3f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
