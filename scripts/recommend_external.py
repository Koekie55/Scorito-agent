"""Externally-driven enrolled-aware recommender — does cyclingoracle beat the heuristic?

``recommend_enrolled.py`` grades a blind enrolled-aware squad built from two
*internal* scorers (``heuristic`` relevance-weighted qualities, ``fitted`` Ridge)
and shows the heuristic captures 6635 real pts = 94.7% of the season ceiling /
86.4% of the enrolled-aware ceiling on TdF 2026.

This script adds the now-*validated* **cyclingoracle** stage predictor as a third
scorer via the production seam (:class:`ExternalStageScorer` +
:class:`RankPointsCurve`) and asks the concrete proof-of-value question:

    Does the external model — proven to carry real per-stage signal
    (macro Spearman +0.4673, pooled +0.4644, positive on 19/21 stages) —
    build a *better* blind enrolled-aware 20-rider squad than the internal
    heuristic?

Two external variants are graded so the raw signal is isolated:

* ``cyclingoracle+fallback`` — matched riders get curve(rank) points, unmatched
  riders fall back to the fitted internal scorer. Necessary because cyclingoracle
  ranks only ~15 contenders per stage — far too thin to field a 20-rider squad
  across 21 stages on its own.
* ``cyclingoracle_pure`` — no fallback (unmatched -> 0). Isolates the raw external
  signal; the MILP can only build value from the riders cyclingoracle actually
  ranks.

The ``heuristic`` enrolled-aware pick is recomputed here as the reference so the
whole comparison is self-contained.

Grading is honest: :func:`back_analysis` re-derives the best real 9 + captain
from the chosen 20 using ACTUAL persisted points — the external ranks only drive
the *pick*, never the score.

Usage:
    python scripts/recommend_external.py [target_slug] [train_slug] [top_per_stage]
    python scripts/recommend_external.py tdf2026            # in-sample curve
    python scripts/recommend_external.py tdf2026 giro2026    # fallback trained OOS
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling scripts

from scorito_agent.scorito import (  # noqa: E402
    DATA_ROOT,
    StageScorer,
    back_analysis,
    expected_total_values,
    joint_enrolled_squad,
    load_snapshot,
    optimal_hindsight_squad,
    pick_squad,
)
from scorito_agent.scorito.external import (  # noqa: E402
    ExternalStageScorer,
    RankPointsCurve,
    name_key,
    predictions_from_cyclingoracle,
)

# reuse the verified helpers rather than duplicate them
from recommend_enrolled import _bounded_points_fn, _fmt_m, _grade  # noqa: E402

TOP_PER_STAGE_DEFAULT = 40
DEFAULT_PREDICTIONS = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "cyclingoracle"
    / "tdf2026_predictions.jsonl"
)


def _stage_id_by_number(snap) -> dict[int, int]:
    """Map cyclingoracle stage_number (1..N) -> Scorito stage_id.

    Robust to the exact id base: stages are taken in ascending stage_id order,
    which for the Scorito grand-tour snapshots is stage 1 .. stage 21.
    """
    stages_sorted = sorted(snap.stages, key=lambda s: s.stage_id)
    return {i + 1: st.stage_id for i, st in enumerate(stages_sorted)}


def load_cyclingoracle_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def build_external_predictions(snap, rows: list[dict]):
    """Harvested JSONL rows -> ``{stage_id -> {name_key -> predicted_rank}}``."""
    id_by_number = _stage_id_by_number(snap)
    rows_by_stage: dict[int, list[dict]] = {}
    for row in rows:
        try:
            n = int(row.get("stage_number"))
        except (TypeError, ValueError):
            continue
        stage_id = id_by_number.get(n)
        if stage_id is None:
            continue
        rows_by_stage.setdefault(stage_id, []).append(row)
    return predictions_from_cyclingoracle(rows_by_stage), rows_by_stage


def _coverage(snap, preds: dict[int, dict[str, int]]) -> dict:
    """How many predicted names match a snapshot rider, per stage."""
    rider_keys = {name_key(r.name) for r in snap.riders if r.price > 0}
    per_stage = []
    total_pred = 0
    total_matched = 0
    for st in sorted(snap.stages, key=lambda s: s.stage_id):
        stage_preds = preds.get(st.stage_id, {})
        matched = sum(1 for k in stage_preds if k in rider_keys)
        total_pred += len(stage_preds)
        total_matched += matched
        per_stage.append(
            {
                "stage_id": st.stage_id,
                "predicted": len(stage_preds),
                "matched": matched,
            }
        )
    return {
        "total_predicted": total_pred,
        "total_matched": total_matched,
        "match_pct": round(100.0 * total_matched / total_pred, 1) if total_pred else 0.0,
        "per_stage": per_stage,
    }


def _grade_scorer(snap, scorer, ceilings, top_per_stage) -> dict | None:
    """Blind enrolled-aware joint pick + season-total baseline for one scorer."""
    pf = _bounded_points_fn(snap, scorer, top_per_stage)
    joint_plan = joint_enrolled_squad(snap, pf)
    if joint_plan is None:  # scipy missing
        return None
    joint_grade = _grade(snap, joint_plan.rider_ids, joint_plan.value, ceilings)
    joint_grade["strategy"] = "enrolled-aware (joint MILP on predictions)"

    st_values = expected_total_values(snap, scorer)
    st_plan = pick_squad(snap, st_values)
    st_grade = _grade(snap, st_plan.rider_ids, st_plan.value, ceilings)
    st_grade["strategy"] = "season-total proxy (pick_squad)"
    return {"enrolled_aware": joint_grade, "season_total": st_grade}


def _squad_rows(snap, rider_ids) -> list[dict]:
    back = back_analysis(snap, rider_ids)
    contrib: dict[int, float] = {}
    for lu in (back.lineups or []):
        for rid in lu.rider_ids:
            factor = snap.captain_factor if rid == lu.captain_id else 1
            contrib[rid] = contrib.get(rid, 0.0) + factor * snap.actual_points(rid, lu.stage)
    rows = []
    for rid in rider_ids:
        r = snap.rider(rid)
        if r is None:
            continue
        rows.append(
            {
                "rider_id": rid,
                "name": r.name,
                "role": r.role_label,
                "price": r.price,
                "real_enrolled_contribution": round(contrib.get(rid, 0.0), 1),
            }
        )
    rows.sort(key=lambda x: x["real_enrolled_contribution"], reverse=True)
    return rows


def build_recommendation(
    target_slug: str,
    train_slug: str | None,
    predictions_path: Path = DEFAULT_PREDICTIONS,
    top_per_stage: int = TOP_PER_STAGE_DEFAULT,
) -> dict:
    snap = load_snapshot(target_slug)
    train_slug = train_slug or target_slug
    in_sample = train_slug == target_slug
    train_snap = snap if in_sample else load_snapshot(train_slug)

    # --- ceilings --------------------------------------------------------
    season_ceiling = optimal_hindsight_squad(snap)
    season_ceiling_total = season_ceiling.season_total or 0.0
    enrolled_ceiling = joint_enrolled_squad(
        snap, lambda rid, st: snap.actual_points(rid, st)
    )
    enrolled_ceiling_total = (enrolled_ceiling.value if enrolled_ceiling else 0.0) or 0.0
    ceilings = {"season": season_ceiling_total, "enrolled": enrolled_ceiling_total}

    # --- external predictor seam ----------------------------------------
    rows = load_cyclingoracle_rows(predictions_path)
    preds, _rows_by_stage = build_external_predictions(snap, rows)
    curve = RankPointsCurve.from_snapshot(snap)
    coverage = _coverage(snap, preds)

    fitted_fallback = StageScorer().fit(train_snap)

    scorers = {
        "heuristic": StageScorer(),  # reference (internal, unfitted)
        "cyclingoracle+fallback": ExternalStageScorer(
            preds, curve, fallback=fitted_fallback
        ),
        "cyclingoracle_pure": ExternalStageScorer(preds, curve, fallback=None),
    }

    models: dict[str, dict] = {}
    for name, scorer in scorers.items():
        graded = _grade_scorer(snap, scorer, ceilings, top_per_stage)
        if graded is not None:
            models[name] = graded

    # Recommended = best real_season_total across all (model, strategy) pairs.
    best_key = None
    best = None
    for name, m in models.items():
        for strat in ("enrolled_aware", "season_total"):
            g = m[strat]
            if best is None or g["real_season_total"] > best["real_season_total"]:
                best = g
                best_key = (name, strat)

    rec_ids = best["rider_ids"] if best else []

    return {
        "market_id": snap.market_id,
        "slug": snap.slug,
        "train_slug": train_slug,
        "out_of_sample": not in_sample,
        "budget": snap.budget,
        "captain_factor": snap.captain_factor,
        "top_per_stage": top_per_stage,
        "predictions_file": str(predictions_path),
        "predicted_rows": len(rows),
        "curve_source": snap.slug,
        "coverage": coverage,
        "ceilings": {
            "season_total": season_ceiling_total,
            "enrolled_aware": enrolled_ceiling_total,
        },
        "models": models,
        "recommended": {
            "model": best_key[0] if best_key else None,
            "strategy": best_key[1] if best_key else None,
            "price": best["price"] if best else 0,
            "real_season_total": best["real_season_total"] if best else 0.0,
            "pct_of_season_ceiling": best["pct_of_season_ceiling"] if best else 0.0,
            "pct_of_enrolled_ceiling": best["pct_of_enrolled_ceiling"] if best else 0.0,
        },
        "recommended_squad": _squad_rows(snap, rec_ids),
    }


def print_recommendation(rec: dict) -> None:
    line = "=" * 80
    print(line)
    print(
        f" EXTERNAL (cyclingoracle) ENROLLED-AWARE RECOMMENDER — {rec['slug']} "
        f"(market {rec['market_id']})"
    )
    print(line)
    sample = (
        f"fallback trained OUT-OF-SAMPLE on {rec['train_slug']}"
        if rec["out_of_sample"]
        else "in-sample curve + fallback"
    )
    print(f" budget {_fmt_m(rec['budget'])} | captain x{rec['captain_factor']} | {sample}")
    cov = rec["coverage"]
    print(
        f" predictor: {rec['predicted_rows']} rows | name match "
        f"{cov['total_matched']}/{cov['total_predicted']} ({cov['match_pct']}%) "
        f"| curve from {rec['curve_source']}"
    )
    print(
        f" ceilings:  season-total {rec['ceilings']['season_total']:.0f}"
        f"  |  ENROLLED-AWARE {rec['ceilings']['enrolled_aware']:.0f} pts"
    )
    print()

    print(" MODEL x STRATEGY GRADING — blind squad vs the real results")
    print(" " + "-" * 78)
    print(
        f" {'model / strategy':<40}{'price':>8}{'REAL':>8}"
        f"{'%season':>10}{'%enrol':>9}"
    )
    for name in ("heuristic", "cyclingoracle+fallback", "cyclingoracle_pure"):
        m = rec["models"].get(name)
        if not m:
            continue
        for strat, label in (
            ("enrolled_aware", "enrolled-aware (joint)"),
            ("season_total", "season-total (baseline)"),
        ):
            g = m[strat]
            tag = f"{name} / {label}"
            print(
                f" {tag:<40}{_fmt_m(g['price']):>8}"
                f"{g['real_season_total']:>8.0f}"
                f"{g['pct_of_season_ceiling']:>9.1f}%{g['pct_of_enrolled_ceiling']:>8.1f}%"
            )
    print(" " + "-" * 78)
    r = rec["recommended"]
    print(
        f" best: {r['model']} / {r['strategy']} -> {r['real_season_total']:.0f} pts "
        f"({r['pct_of_season_ceiling']:.1f}% season, {r['pct_of_enrolled_ceiling']:.1f}% enrolled)"
    )
    print()

    print(f" BEST 20-RIDER SQUAD ({r['model']} / {r['strategy']})")
    print(" " + "-" * 78)
    print(f" {'rider':<26}{'role':<22}{'price':>8}{'real pts':>10}")
    for row in rec["recommended_squad"]:
        print(
            f" {row['name']:<26}{row['role']:<22}{_fmt_m(row['price']):>8}"
            f"{row['real_enrolled_contribution']:>10.0f}"
        )
    print(" " + "-" * 78)
    print(f" squad price {_fmt_m(r['price'])} of {_fmt_m(rec['budget'])}")
    print(line)


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "tdf2026"
    train = sys.argv[2] if len(sys.argv) > 2 else None
    top = int(sys.argv[3]) if len(sys.argv) > 3 else TOP_PER_STAGE_DEFAULT
    rec = build_recommendation(target, train, top_per_stage=top)
    print_recommendation(rec)

    dest = DATA_ROOT / target / "recommendation_external.json"
    dest.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    print(f"\n wrote {dest}")


if __name__ == "__main__":
    main()
