"""Scorito forward squad recommender — the "build a winning team" engine.

Where ``report.py`` answers *hindsight* ("which 20 riders SHOULD I have bought"),
this script answers the **forward** question the game actually asks you before a
race starts:

* Using only rider qualities x stage profiles (NO race results), which **20
  riders** should I buy under the budget, and which **9 + captain** would the
  model enrol each stage?

It then *grades itself* against the real results that we have persisted:

* run :func:`back_analysis` on the recommended squad with the REAL points to get
  the season total it would actually have scored, and
* compare that to the hindsight ceiling (:func:`optimal_hindsight_squad`) — the
  best any squad could have done — as a ``% of ceiling`` efficiency score.

Two scorers are compared so the value of calibration is visible:

* ``heuristic`` — un-trained relevance-weighted quality sum (always available).
* ``fitted``    — :class:`StageScorer` Ridge model trained on a snapshot's real
  points. Pass a *different* ``train_slug`` for a genuine **out-of-sample** test
  (train on the Giro, pick the Tour), which is exactly how you would build a
  squad for a brand-new race such as the Vuelta.

Usage:
    python scripts/recommend.py [target_slug] [train_slug]
    python scripts/recommend.py tdf2026            # in-sample calibration
    python scripts/recommend.py tdf2026 giro2026    # OUT-OF-SAMPLE (train Giro)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scorito_agent.scorito import (  # noqa: E402
    DATA_ROOT,
    StageScorer,
    back_analysis,
    expected_total_values,
    load_snapshot,
    optimal_hindsight_squad,
    pick_squad,
)


def _fmt_m(price: int) -> str:
    return f"{price / 1_000_000:.2f}M"


def _grade_squad(snap, rider_ids: list[int]) -> dict:
    """Real per-stage achievable season total for a blindly-picked squad."""
    back = back_analysis(snap, rider_ids)
    return {
        "rider_ids": list(rider_ids),
        "price": back.total_price,
        "real_season_total": back.season_total,
        "real_leaderboard_value": back.value,
    }


def build_recommendation(target_slug: str, train_slug: str | None) -> dict:
    snap = load_snapshot(target_slug)
    train_slug = train_slug or target_slug
    in_sample = train_slug == target_slug

    # Hindsight ceiling — best any squad could have scored (upper bound).
    ceiling = optimal_hindsight_squad(snap)
    ceiling_total = ceiling.season_total or 0.0

    # --- heuristic model (no training) -----------------------------------
    heur = StageScorer()  # unfitted -> falls back to heuristic_score
    heur_values = expected_total_values(snap, heur)
    heur_plan = pick_squad(snap, heur_values)
    heur_grade = _grade_squad(snap, heur_plan.rider_ids)
    heur_grade["predicted_value"] = heur_plan.value

    # --- fitted Ridge model ----------------------------------------------
    train_snap = snap if in_sample else load_snapshot(train_slug)
    fitted = StageScorer().fit(train_snap)
    fit_values = expected_total_values(snap, fitted)
    fit_plan = pick_squad(snap, fit_values)
    fit_grade = _grade_squad(snap, fit_plan.rider_ids)
    fit_grade["predicted_value"] = fit_plan.value

    def _eff(total: float) -> float:
        return 100.0 * total / ceiling_total if ceiling_total else 0.0

    heur_grade["pct_of_ceiling"] = _eff(heur_grade["real_season_total"])
    fit_grade["pct_of_ceiling"] = _eff(fit_grade["real_season_total"])

    # Recommended squad = the better-grading model.
    best_key = (
        "fitted"
        if fit_grade["real_season_total"] >= heur_grade["real_season_total"]
        else "heuristic"
    )
    best_plan = fit_plan if best_key == "fitted" else heur_plan
    best_values = fit_values if best_key == "fitted" else heur_values

    squad = [snap.rider(r) for r in best_plan.rider_ids]
    squad = [r for r in squad if r is not None]
    squad.sort(key=lambda r: best_values.get(r.rider_id, 0.0), reverse=True)
    squad_rows = [
        {
            "rider_id": r.rider_id,
            "name": r.name,
            "role": r.role_label,
            "price": r.price,
            "predicted_value": round(best_values.get(r.rider_id, 0.0), 1),
        }
        for r in squad
    ]

    return {
        "market_id": snap.market_id,
        "slug": snap.slug,
        "train_slug": train_slug,
        "out_of_sample": not in_sample,
        "budget": snap.budget,
        "captain_factor": snap.captain_factor,
        "ceiling_season_total": ceiling_total,
        "models": {"heuristic": heur_grade, "fitted": fit_grade},
        "recommended_model": best_key,
        "recommended_squad": squad_rows,
        "recommended_price": best_plan.total_price,
        "recommended_real_season_total": (
            fit_grade if best_key == "fitted" else heur_grade
        )["real_season_total"],
    }


def print_recommendation(rec: dict) -> None:
    line = "=" * 72
    print(line)
    print(f" SCORITO FORWARD RECOMMENDER — {rec['slug']} (market {rec['market_id']})")
    print(line)
    sample = (
        f"OUT-OF-SAMPLE (trained on {rec['train_slug']})"
        if rec["out_of_sample"]
        else "in-sample calibration"
    )
    print(
        f" budget {_fmt_m(rec['budget'])} | captain x{rec['captain_factor']} | {sample}"
    )
    print(f" hindsight ceiling (best possible): {rec['ceiling_season_total']:.0f} pts")
    print()

    print(" MODEL GRADING — blind squad vs the real results")
    print(" " + "-" * 70)
    print(f" {'model':<12}{'price':>9}{'pred.val':>11}{'REAL total':>12}{'% ceiling':>11}")
    for name in ("heuristic", "fitted"):
        m = rec["models"][name]
        print(
            f" {name:<12}{_fmt_m(m['price']):>9}{m['predicted_value']:>11.0f}"
            f"{m['real_season_total']:>12.0f}{m['pct_of_ceiling']:>10.1f}%"
        )
    print(" " + "-" * 70)
    print(f" recommended model: {rec['recommended_model']}")
    print()

    print(f" RECOMMENDED 20-RIDER SQUAD ({rec['recommended_model']} model)")
    print(" " + "-" * 70)
    print(f" {'rider':<26}{'role':<20}{'price':>8}{'pred.val':>10}")
    for r in rec["recommended_squad"]:
        print(
            f" {r['name']:<26}{r['role']:<20}{_fmt_m(r['price']):>8}"
            f"{r['predicted_value']:>10.0f}"
        )
    print(" " + "-" * 70)
    print(
        f" squad price {_fmt_m(rec['recommended_price'])} of {_fmt_m(rec['budget'])} | "
        f"would score {rec['recommended_real_season_total']:.0f} pts "
        f"({100.0 * rec['recommended_real_season_total'] / rec['ceiling_season_total']:.1f}% of ceiling)"
        if rec["ceiling_season_total"]
        else ""
    )
    print(line)


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "tdf2026"
    train = sys.argv[2] if len(sys.argv) > 2 else None
    rec = build_recommendation(target, train)
    print_recommendation(rec)

    dest = DATA_ROOT / target / "recommendation.json"
    dest.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    print(f"\n wrote {dest}")


if __name__ == "__main__":
    main()
