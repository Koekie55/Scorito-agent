"""Out-of-sample validation of the TV 2 Axelgaard star ranking.

Cluster-bootstrap ("Monte Carlo") regression of realised Scorito stage points on
Axelgaard's pre-stage star tier. The resulting weight is derived from the
evidence, so it stays near zero until enough completed stages support it.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from scorito_agent.scorito import load_snapshot  # noqa: E402
from scorito_agent.tv2_axelgaard import is_usable_before_stage, name_key  # noqa: E402

PREVIEW_DIR = ROOT / "data" / "tv2_axelgaard"
OUTPUT_PATH = ROOT / "data" / "scorito" / "vuelta2026" / "tv2_axelgaard_validation.json"
MAX_WEIGHT = 0.12
SHRINKAGE_STAGES = 5.0
MIN_PROBABILITY = 0.60
DEFAULT_SIMULATIONS = 10_000
DEFAULT_SEED = 20260824
LINEUP_SIZE = 9


def load_previews(directory: Path, completed: list[int]) -> list[dict[str, Any]]:
    previews = []
    for path in sorted(directory.glob("stage-*.json")):
        preview = json.loads(path.read_text(encoding="utf-8"))
        if int(preview.get("stage_number") or 0) in completed and is_usable_before_stage(preview):
            previews.append(preview)
    return previews


def build_observations(snapshot, previews: list[dict[str, Any]]) -> dict[str, Any]:
    riders_by_key: dict[tuple[str, ...], Any] = {}
    for rider in snapshot.riders:
        riders_by_key.setdefault(name_key(rider.name), rider)

    stars: list[float] = []
    points: list[float] = []
    teams: list[int] = []
    stages: list[int] = []
    unmatched: dict[str, list[str]] = {}
    for preview in previews:
        stage_no = int(preview["stage_number"])
        stage = snapshot.stage_by_order(stage_no)
        if stage is None:
            continue
        tier_by_key: dict[tuple[str, ...], float] = {}
        for row in preview.get("rider_tiers", []):
            key = name_key(str(row["rider"]))
            if key in riders_by_key:
                tier_by_key[key] = max(tier_by_key.get(key, 0.0), float(row["stars"]))
            else:
                unmatched.setdefault(str(stage_no), []).append(str(row["rider"]))
        for key, rider in riders_by_key.items():
            stars.append(tier_by_key.get(key, 0.0))
            points.append(float(snapshot.actual_points(rider.rider_id, stage)))
            teams.append(int(rider.team_id))
            stages.append(stage_no)
    return {
        "stars": np.asarray(stars, dtype=float),
        "points": np.asarray(points, dtype=float),
        "teams": np.asarray(teams, dtype=int),
        "stages": np.asarray(stages, dtype=int),
        "unmatched_rider_names": unmatched,
        "stage_numbers": sorted({int(p["stage_number"]) for p in previews}),
    }


def _fit(actual: np.ndarray, predictor: np.ndarray) -> dict[str, float]:
    design = np.column_stack((np.ones(len(predictor)), predictor))
    coefficients, *_ = np.linalg.lstsq(design, actual, rcond=None)
    residual = actual - design @ coefficients
    centered = actual - actual.mean()
    denominator = float(np.dot(centered, centered)) or 1.0
    return {
        "intercept": float(coefficients[0]),
        "slope": float(coefficients[1]),
        "r_squared": 1.0 - float(np.dot(residual, residual)) / denominator,
        "rmse": float(math.sqrt(float(np.mean(residual**2)))),
    }


def _percentiles(values: np.ndarray) -> dict[str, float]:
    return {
        "p025": float(np.percentile(values, 2.5)),
        "median": float(np.percentile(values, 50.0)),
        "p975": float(np.percentile(values, 97.5)),
    }


def bootstrap(
    observations: dict[str, Any], *, simulations: int, seed: int
) -> dict[str, Any]:
    """Resample trade teams within each stage and refit the star regression."""
    rng = np.random.default_rng(seed)
    stars = observations["stars"]
    points = observations["points"]
    teams = observations["teams"]
    stage_numbers = observations["stages"]

    index_by_stage_team: dict[tuple[int, int], np.ndarray] = {}
    for stage_no in np.unique(stage_numbers):
        for team_id in np.unique(teams):
            selected = np.flatnonzero((stage_numbers == stage_no) & (teams == team_id))
            if selected.size:
                index_by_stage_team[(int(stage_no), int(team_id))] = selected

    slopes, r_squared, top_gain = [], [], []
    for _ in range(simulations):
        sample_indices = []
        for stage_no in np.unique(stage_numbers):
            keys = [key for key in index_by_stage_team if key[0] == int(stage_no)]
            drawn = rng.choice(len(keys), size=len(keys), replace=True)
            sample_indices.append(
                np.concatenate([index_by_stage_team[keys[i]] for i in drawn])
            )
        indices = np.concatenate(sample_indices)
        fit = _fit(points[indices], stars[indices])
        slopes.append(fit["slope"])
        r_squared.append(fit["r_squared"])
        sampled_stars = stars[indices]
        sampled_points = points[indices]
        ranked = np.argsort(-sampled_stars, kind="stable")[:LINEUP_SIZE]
        top_gain.append(float(sampled_points[ranked].mean() - sampled_points.mean()))

    slope_samples = np.asarray(slopes)
    probability_positive = float(np.mean(slope_samples > 0.0))
    return {
        "method": (
            "trade-team cluster bootstrap within each completed stage; "
            "OLS realised Scorito stage points = intercept + slope * Axelgaard stars"
        ),
        "simulations": simulations,
        "seed": seed,
        "slope": _percentiles(slope_samples),
        "r_squared": _percentiles(np.asarray(r_squared)),
        "top9_points_above_field_average": _percentiles(np.asarray(top_gain)),
        "probability_slope_positive": probability_positive,
        "probability_top9_beats_field": float(np.mean(np.asarray(top_gain) > 0.0)),
    }


def recommended_weight(probability_positive: float, stages: int) -> dict[str, Any]:
    """Grow the weight with evidence; no completed stages means no influence."""
    evidence = max(0.0, (probability_positive - 0.5) / 0.5)
    shrinkage = stages / (stages + SHRINKAGE_STAGES) if stages else 0.0
    weight = 0.0
    if stages >= 1 and probability_positive >= MIN_PROBABILITY:
        weight = round(MAX_WEIGHT * evidence * shrinkage, 6)
    return {
        "weight": weight,
        "max_weight": MAX_WEIGHT,
        "evidence_component": round(evidence, 6),
        "sample_shrinkage": round(shrinkage, 6),
        "minimum_probability": MIN_PROBABILITY,
        "validated_stages": stages,
        "rule": (
            "weight = max_weight * (P(slope>0)-0.5)/0.5 * stages/(stages+5), "
            f"and 0 unless P(slope>0) >= {MIN_PROBABILITY}"
        ),
    }


def scored_stages(snapshot, completed: list[int]) -> list[int]:
    """Keep only stages whose Scorito points are credited; results can lag the finish."""
    scored = []
    for stage_no in completed:
        stage = snapshot.stage_by_order(stage_no)
        if stage is None:
            continue
        if any(snapshot.actual_points(rider.rider_id, stage) for rider in snapshot.riders):
            scored.append(stage_no)
    return scored


def evaluate(*, simulations: int, seed: int, slug: str) -> dict[str, Any]:
    from scripts.daily_vuelta_refresh import _completed_and_next_stage

    finished, _ = _completed_and_next_stage()
    snapshot = load_snapshot(slug)
    completed = scored_stages(snapshot, finished)
    awaiting_points = [stage_no for stage_no in finished if stage_no not in completed]
    previews = load_previews(PREVIEW_DIR / slug, completed)
    observations = build_observations(snapshot, previews)
    stages = len(observations["stage_numbers"])

    if not stages or observations["stars"].size == 0:
        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "insufficient_evidence",
            "completed_stages": completed,
            "finished_stages": finished,
            "stages_awaiting_points": awaiting_points,
            "validated_stages": [],
            "recommended_signal": recommended_weight(0.0, 0),
            "data_gaps": ["No completed stage has an archived pre-stage TV 2 preview."],
        }

    sample = bootstrap(observations, simulations=simulations, seed=seed)
    point_fit = _fit(observations["points"], observations["stars"])
    signal = recommended_weight(sample["probability_slope_positive"], stages)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "validated" if signal["weight"] > 0 else "insufficient_evidence",
        "source": "tv2_axelgaard",
        "completed_stages": completed,
        "finished_stages": finished,
        "stages_awaiting_points": awaiting_points,
        "validated_stages": observations["stage_numbers"],
        "observations": int(observations["stars"].size),
        "ranked_rider_observations": int((observations["stars"] > 0).sum()),
        "unmatched_rider_names": observations["unmatched_rider_names"],
        "point_estimate": point_fit,
        "monte_carlo_regression": sample,
        "recommended_signal": signal,
        "uncertainty": (
            "Star tiers are a ranked opinion, not a probability. With few completed "
            "stages the slope interval is wide and the derived weight stays small."
        ),
        "limitations": [
            "Only stages whose preview was last edited before the stage start are used.",
            "Riders absent from the star list are treated as zero stars, not as excluded.",
            "Scorito points mix stage placing and classification effects.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulations", type=int, default=DEFAULT_SIMULATIONS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--slug", default="vuelta2026")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args(argv)

    report = evaluate(simulations=args.simulations, seed=args.seed, slug=args.slug)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Status: {report['status']}")
    print(f"Validated stages: {report.get('validated_stages')}")
    if "monte_carlo_regression" in report:
        sample = report["monte_carlo_regression"]
        print(f"Slope 95% CI: {sample['slope']['p025']:.3f} .. {sample['slope']['p975']:.3f}")
        print(f"P(slope>0) = {sample['probability_slope_positive']:.3f}")
    print(f"Recommended weight: {report['recommended_signal']['weight']}")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
