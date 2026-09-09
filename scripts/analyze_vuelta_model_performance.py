"""Analyse archived Vuelta forecasts and render a performance email body.

This is deliberately evaluation-only. The proposed profile-aware uncertainty
layer does not alter rider ranks until a separately held-out historical replay
shows a rank or lineup benefit.
"""
from __future__ import annotations

import argparse
import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "scorito" / "vuelta2026"
EVALUATIONS_DIR = DATA_DIR / "stage_predictability" / "evaluations"
PREDICTIONS_PATH = DATA_DIR / "stage_top20_predictions.json"
OUTPUT_JSON = DATA_DIR / "model_performance_stage_analysis.json"
OUTPUT_HTML = DATA_DIR / "email" / "model_performance_by_stage.html"
DEFAULT_SIMULATIONS = 10_000
DEFAULT_SEED = 20260909


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _percentiles(values: np.ndarray) -> dict[str, float]:
    return {
        "p025": round(float(np.percentile(values, 2.5)), 4),
        "median": round(float(np.percentile(values, 50)), 4),
        "p975": round(float(np.percentile(values, 97.5)), 4),
    }


def _ols_coefficients(design: np.ndarray, outcome: np.ndarray) -> np.ndarray:
    coefficients, *_ = np.linalg.lstsq(design, outcome, rcond=None)
    return coefficients


def _stage_metadata() -> dict[int, dict[str, Any]]:
    payload = _load(PREDICTIONS_PATH)
    return {int(row["stage_no"]): row for row in payload["stages"]}


def _captain_actual_finish(report: dict[str, Any]) -> int | None:
    captain_id = int(report["predicted_top_20"][0]["rider_id"])
    for row in report["actual_top_20"]:
        if int(row["rider_id"]) == captain_id:
            return int(row["actual_finish"])
    return None


def _top9_capture(report: dict[str, Any]) -> int:
    predicted_ids = {int(row["rider_id"]) for row in report["predicted_top_20"]}
    actual_top9 = {
        int(row["rider_id"])
        for row in report["actual_top_20"]
        if int(row["actual_finish"]) <= 9
    }
    return len(predicted_ids & actual_top9)


def load_stage_reports() -> list[dict[str, Any]]:
    metadata = _stage_metadata()
    reports = []
    for path in sorted(EVALUATIONS_DIR.glob("stage_*.json")):
        report = _load(path)
        stage_no = int(report["stage_no"])
        stage = metadata.get(stage_no, {})
        reports.append(
            {
                "stage_no": stage_no,
                "date": report["stage_date"],
                "profile_type": str(stage.get("profile_type") or "unknown"),
                "finish_type": str(stage.get("finish_type") or "unknown"),
                "predictability_pct": float(report["predictability_pct"]),
                "overlap_pct": float(report["overlap_pct"]),
                "rank_accuracy_pct": float(report["rank_accuracy_pct"]),
                "matched_riders": int(report["matched_riders"]),
                "top9_capture": _top9_capture(report),
                "captain_actual_finish": _captain_actual_finish(report),
            }
        )
    if not reports:
        raise RuntimeError(f"no completed evaluations found in {EVALUATIONS_DIR}")
    return reports


def bootstrap_profile_regression(
    reports: list[dict[str, Any]], *, simulations: int, seed: int
) -> dict[str, Any]:
    """Stage-cluster bootstrap OLS for reliability by terrain group.

    Reference is a flat sprint or a mountain stage with a non-summit finish.
    With only 10 Vuelta stages this measures association, not causation.
    """
    outcome = np.asarray([row["predictability_pct"] for row in reports], dtype=float)
    hilly = np.asarray([row["profile_type"] == "hilly" for row in reports], dtype=float)
    summit = np.asarray([row["finish_type"] == "summit" for row in reports], dtype=float)
    design = np.column_stack((np.ones(len(reports)), hilly, summit))
    names = ("reference", "hilly_effect", "summit_effect")
    rng = np.random.default_rng(seed)
    samples = np.empty((simulations, len(names)), dtype=float)
    n = len(reports)
    for draw in range(simulations):
        indices = rng.integers(0, n, size=n)
        samples[draw] = _ols_coefficients(design[indices], outcome[indices])

    estimates = _ols_coefficients(design, outcome)
    effects = {
        name: {
            "estimate": round(float(estimate), 4),
            "bootstrap_95ci": _percentiles(samples[:, index]),
            "probability_below_zero": round(float(np.mean(samples[:, index] < 0)), 4),
        }
        for index, (name, estimate) in enumerate(zip(names, estimates, strict=True))
    }
    high_uncertainty = (hilly == 1) | (summit == 1)
    low_observed = outcome < 30.0
    true_positive = int(np.sum(high_uncertainty & low_observed))
    false_positive = int(np.sum(high_uncertainty & ~low_observed))
    false_negative = int(np.sum(~high_uncertainty & low_observed))
    return {
        "method": (
            "10,000-draw stage-cluster bootstrap; OLS predictability_pct = intercept + "
            "hilly indicator + summit-finish indicator. The reference is flat sprint or "
            "non-summit mountain stages."
        ),
        "simulations": simulations,
        "seed": seed,
        "stage_count": n,
        "effects": effects,
        "retrospective_uncertainty_alert": {
            "definition": "flag a hilly or summit-finish stage as lower confidence",
            "observed_low_reliability_threshold_pct": 30.0,
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "sensitivity": round(true_positive / (true_positive + false_negative), 4)
            if true_positive + false_negative
            else None,
            "precision": round(true_positive / (true_positive + false_positive), 4)
            if true_positive + false_positive
            else None,
            "warning": (
                "This alert threshold was assessed on the same ten-stage Vuelta sample; "
                "it is exploratory and must be confirmed on earlier Grand Tours."
            ),
        },
    }


def analyse(*, simulations: int, seed: int) -> dict[str, Any]:
    reports = load_stage_reports()
    bootstrap = bootstrap_profile_regression(reports, simulations=simulations, seed=seed)
    overall = {
        "mean_predictability_pct": round(float(np.mean([r["predictability_pct"] for r in reports])), 2),
        "mean_overlap_pct": round(float(np.mean([r["overlap_pct"] for r in reports])), 2),
        "mean_rank_accuracy_pct": round(float(np.mean([r["rank_accuracy_pct"] for r in reports])), 2),
        "mean_top9_capture": round(float(np.mean([r["top9_capture"] for r in reports])), 2),
        "captain_top3_rate": round(
            float(np.mean([
                r["captain_actual_finish"] is not None and r["captain_actual_finish"] <= 3
                for r in reports
            ])),
            4,
        ),
    }
    by_profile: dict[str, list[float]] = {}
    for row in reports:
        key = f"{row['profile_type']}/{row['finish_type']}"
        by_profile.setdefault(key, []).append(row["predictability_pct"])
    profile_summary = {
        key: {"n": len(values), "mean_predictability_pct": round(float(np.mean(values)), 2)}
        for key, values in sorted(by_profile.items())
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "immutable pre-stage Vuelta archives with completed Scorito result evaluation",
        "stage_reports": reports,
        "overall": overall,
        "by_profile": profile_summary,
        "monte_carlo_regression": bootstrap,
        "proposal": {
            "name": "profile-aware uncertainty layer",
            "production_change": (
                "Leave rider ordering unchanged. Mark hilly and summit finishes as lower "
                "confidence in the recommendation and require a same-day tactic/news review."
            ),
            "do_not_deploy": (
                "Do not deploy the GC-bunch-mingling rank adjustment yet: its exact historical "
                "eligibility population has only three prior stages and one positive case."
            ),
            "next_rank_change_to_test": (
                "Build a pre-2026 walk-forward replay for breakaway survival and candidate-tail "
                "diversity; adopt only if it raises top-20 capture with a positive bootstrap CI."
            ),
        },
    }


def _cell(value: object) -> str:
    return f"<td style='padding:6px 8px;border-top:1px solid #e4e8ed'>{html.escape(str(value))}</td>"


def render_html(report: dict[str, Any]) -> str:
    table_rows = "".join(
        "<tr>"
        + _cell(row["stage_no"])
        + _cell(row["profile_type"] + "/" + row["finish_type"])
        + _cell(f"{row['predictability_pct']:.2f}%")
        + _cell(f"{row['overlap_pct']:.1f}%")
        + _cell(f"{row['rank_accuracy_pct']:.2f}%")
        + _cell(f"{row['top9_capture']}/9")
        + _cell(row["captain_actual_finish"] or "outside top 20")
        + "</tr>"
        for row in report["stage_reports"]
    )
    regression = report["monte_carlo_regression"]
    effects = regression["effects"]
    hilly = effects["hilly_effect"]
    summit = effects["summit_effect"]
    overall = report["overall"]
    alert = regression["retrospective_uncertainty_alert"]
    return f"""<!doctype html><html><body style="background:#f6f8fa;padding:18px">
<div style="max-width:880px;margin:auto;background:#fff;border:1px solid #dfe4ea;padding:24px;font-family:Segoe UI,Arial,sans-serif;color:#20262d">
<h2 style="color:#201751;margin:0 0 6px">Vuelta 2026: stage-by-stage model performance</h2>
<p style="margin:0 0 16px;color:#58636f">Generated {html.escape(report['generated_at'])}. Only immutable pre-stage forecasts with completed Scorito evaluations are included.</p>
<p><b>Headline:</b> {overall['mean_predictability_pct']:.2f}% mean predictability, {overall['mean_overlap_pct']:.2f}% top-20 overlap, {overall['mean_rank_accuracy_pct']:.2f}% rank accuracy, and {overall['mean_top9_capture']:.2f}/9 actual top-nine riders captured. The model's rank-1 pick finished top-three on {overall['captain_top3_rate']:.0%} of evaluated stages.</p>
<table style="border-collapse:collapse;width:100%;font-size:13px"><tr style="background:#201751;color:white"><th style="padding:7px">Stage</th><th>Profile / finish</th><th>Predictability</th><th>Top-20 overlap</th><th>Rank accuracy</th><th>Top-9 capture</th><th>Rank-1 actual</th></tr>{table_rows}</table>
<h3 style="color:#201751;margin:22px 0 8px">Monte Carlo regression</h3>
<p>{html.escape(regression['method'])}</p>
<ul><li>Hilly-stage association: {hilly['estimate']:+.2f} percentage points, bootstrap 95% CI [{hilly['bootstrap_95ci']['p025']:+.2f}, {hilly['bootstrap_95ci']['p975']:+.2f}], P(effect &lt; 0) = {hilly['probability_below_zero']:.1%}.</li>
<li>Summit-finish association: {summit['estimate']:+.2f} percentage points, bootstrap 95% CI [{summit['bootstrap_95ci']['p025']:+.2f}, {summit['bootstrap_95ci']['p975']:+.2f}], P(effect &lt; 0) = {summit['probability_below_zero']:.1%}.</li></ul>
<p>The proposed uncertainty alert caught {alert['true_positive']} of the observed {alert['true_positive'] + alert['false_negative']} stages below 30% predictability (sensitivity {alert['sensitivity']:.0%}), with {alert['false_positive']} false alerts. This is retrospective, not a held-out deployment result.</p>
<h3 style="color:#201751;margin:22px 0 8px">Recommendation</h3>
<p><b>Deploy only the profile-aware uncertainty layer now.</b> It leaves ranking unchanged, flags hilly and summit finishes for same-day tactics/news review, and prevents false confidence. Do not deploy the GC-bunch-mingling rank adjustment: its exact historic eligibility group is only three stages, with one positive example.</p>
<p>The next scoring change should be a pre-2026 walk-forward replay of breakaway-survival and tail-candidate diversity. It should enter production only when top-20 capture improves with a positive bootstrap confidence interval.</p>
</div></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulations", type=int, default=DEFAULT_SIMULATIONS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--output", type=Path, default=OUTPUT_JSON)
    parser.add_argument("--html-output", type=Path, default=OUTPUT_HTML)
    args = parser.parse_args()
    report = analyse(simulations=args.simulations, seed=args.seed)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.html_output.parent.mkdir(parents=True, exist_ok=True)
    args.html_output.write_text(render_html(report), encoding="utf-8")
    print(f"JSON: {args.output}")
    print(f"HTML: {args.html_output}")
    print(json.dumps(report["overall"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())