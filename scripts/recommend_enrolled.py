"""Enrolled-aware Scorito forward recommender — the fixed "build a winning team".

``recommend.py`` picks the 20 by maximising a **season-total** proxy
(:func:`pick_squad`). That silently over-buys redundant GC/climbers who cannot
all enrol on the same mountain stage and leaves the ~7 flat stages thin — its
blind heuristic squad buys ZERO sprinters and captures only ~80% of the true
*enrolled-aware* ceiling.

This script instead maximises the **real game objective** up front: pick the 20
AND the best 9 + doubled captain per stage *jointly* via
:func:`joint_enrolled_squad`, driven by a forward **prediction** model (no race
results leak into the pick). It then grades itself on the real persisted points
against BOTH ceilings:

* season-total ceiling  (:func:`optimal_hindsight_squad`, e.g. 7010 on tdf2026)
* enrolled-aware ceiling(:func:`joint_enrolled_squad` fed ACTUAL points, 7675)

Two scorers are compared (``heuristic`` un-trained, ``fitted`` Ridge). Pass a
different ``train_slug`` for a genuine out-of-sample test (train Giro -> pick
Tour) — exactly how a Vuelta squad would be built.

To keep the joint MILP tractable the predicted candidate pool is bounded to the
top ``--top-per-stage`` riders per stage (predictions are almost never exactly
0, unlike sparse actual points). Grading is unaffected: :func:`back_analysis`
re-derives the best real 9 + captain from the chosen 20 using ACTUAL points.

Usage:
    python scripts/recommend_enrolled.py [target_slug] [train_slug] [top_per_stage]
    python scripts/recommend_enrolled.py tdf2026             # in-sample
    python scripts/recommend_enrolled.py tdf2026 giro2026     # OUT-OF-SAMPLE
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
    joint_enrolled_squad,
    load_snapshot,
    optimal_hindsight_squad,
    pick_squad,
    expected_total_values,
)

TOP_PER_STAGE_DEFAULT = 40


def _fmt_m(price: int) -> str:
    return f"{price / 1_000_000:.2f}M"


def _bounded_points_fn(snap, scorer, top_per_stage: int):
    """Predicted per-(rider, stage) points, restricted to the top-N per stage.

    Returns a ``points_fn(rider_id, stage) -> float`` whose support is the
    top ``top_per_stage`` predicted riders on each stage (0 elsewhere). This
    bounds the joint MILP candidate set — heuristic predictions are dense, so
    an unbounded ``>0`` filter would make the model explode.
    """
    allowed: dict[int, dict[int, float]] = {}
    for st in snap.stages:
        scored = [
            (r.rider_id, float(scorer.expected(r, st)))
            for r in snap.riders
            if r.price > 0
        ]
        scored = [(rid, p) for rid, p in scored if p > 0]
        scored.sort(key=lambda t: t[1], reverse=True)
        allowed[st.stage_id] = {rid: p for rid, p in scored[:top_per_stage]}

    def points_fn(rider_id: int, stage) -> float:
        return allowed.get(stage.stage_id, {}).get(rider_id, 0.0)

    return points_fn


def _grade(snap, rider_ids, predicted_value, ceilings) -> dict:
    back = back_analysis(snap, rider_ids)
    real = back.season_total or 0.0
    return {
        "rider_ids": list(rider_ids),
        "price": back.total_price,
        "predicted_enrolled_value": round(predicted_value, 1),
        "real_season_total": real,
        "real_leaderboard_value": back.value,
        "pct_of_season_ceiling": (
            100.0 * real / ceilings["season"] if ceilings["season"] else 0.0
        ),
        "pct_of_enrolled_ceiling": (
            100.0 * real / ceilings["enrolled"] if ceilings["enrolled"] else 0.0
        ),
    }


def build_recommendation(
    target_slug: str,
    train_slug: str | None,
    top_per_stage: int = TOP_PER_STAGE_DEFAULT,
) -> dict:
    snap = load_snapshot(target_slug)
    train_slug = train_slug or target_slug
    in_sample = train_slug == target_slug

    # --- two ceilings ----------------------------------------------------
    season_ceiling = optimal_hindsight_squad(snap)
    season_ceiling_total = season_ceiling.season_total or 0.0
    enrolled_ceiling = joint_enrolled_squad(snap, lambda rid, st: snap.actual_points(rid, st))
    enrolled_ceiling_total = (enrolled_ceiling.value if enrolled_ceiling else 0.0) or 0.0
    ceilings = {"season": season_ceiling_total, "enrolled": enrolled_ceiling_total}

    # --- scorers ---------------------------------------------------------
    heur = StageScorer()  # unfitted -> heuristic_score
    train_snap = snap if in_sample else load_snapshot(train_slug)
    fitted = StageScorer().fit(train_snap)

    models: dict[str, dict] = {}
    for name, scorer in (("heuristic", heur), ("fitted", fitted)):
        # (a) enrolled-aware joint pick on PREDICTED points (the fix)
        pf = _bounded_points_fn(snap, scorer, top_per_stage)
        joint_plan = joint_enrolled_squad(snap, pf)
        if joint_plan is None:  # scipy missing -> skip enrolled pick
            continue
        joint_grade = _grade(snap, joint_plan.rider_ids, joint_plan.value, ceilings)
        joint_grade["strategy"] = "enrolled-aware (joint MILP on predictions)"

        # (b) baseline season-total pick (what recommend.py does)
        st_values = expected_total_values(snap, scorer)
        st_plan = pick_squad(snap, st_values)
        st_grade = _grade(snap, st_plan.rider_ids, st_plan.value, ceilings)
        st_grade["strategy"] = "season-total proxy (pick_squad)"

        models[name] = {"enrolled_aware": joint_grade, "season_total": st_grade}

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
    # order squad rows by real per-rider contribution for readability
    back = back_analysis(snap, rec_ids)
    contrib: dict[int, float] = {}
    for lu in (back.lineups or []):
        for rid in lu.rider_ids:
            factor = snap.captain_factor if rid == lu.captain_id else 1
            contrib[rid] = contrib.get(rid, 0.0) + factor * snap.actual_points(rid, lu.stage)
    squad_rows = []
    for rid in rec_ids:
        r = snap.rider(rid)
        if r is None:
            continue
        squad_rows.append(
            {
                "rider_id": rid,
                "name": r.name,
                "role": r.role_label,
                "price": r.price,
                "real_enrolled_contribution": round(contrib.get(rid, 0.0), 1),
            }
        )
    squad_rows.sort(key=lambda x: x["real_enrolled_contribution"], reverse=True)

    return {
        "market_id": snap.market_id,
        "slug": snap.slug,
        "train_slug": train_slug,
        "out_of_sample": not in_sample,
        "budget": snap.budget,
        "captain_factor": snap.captain_factor,
        "top_per_stage": top_per_stage,
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
        "recommended_squad": squad_rows,
    }


def print_recommendation(rec: dict) -> None:
    line = "=" * 76
    print(line)
    print(f" ENROLLED-AWARE FORWARD RECOMMENDER — {rec['slug']} (market {rec['market_id']})")
    print(line)
    sample = (
        f"OUT-OF-SAMPLE (trained on {rec['train_slug']})"
        if rec["out_of_sample"]
        else "in-sample calibration"
    )
    print(f" budget {_fmt_m(rec['budget'])} | captain x{rec['captain_factor']} | {sample}")
    print(f" top-{rec['top_per_stage']} predicted riders/stage form the candidate pool")
    print(
        f" ceilings:  season-total {rec['ceilings']['season_total']:.0f}"
        f"  |  ENROLLED-AWARE {rec['ceilings']['enrolled_aware']:.0f} pts"
    )
    print()

    print(" MODEL x STRATEGY GRADING — blind squad vs the real results")
    print(" " + "-" * 74)
    print(
        f" {'model / strategy':<34}{'price':>8}{'REAL':>8}"
        f"{'%season':>10}{'%enrol':>9}"
    )
    for name in ("heuristic", "fitted"):
        m = rec["models"].get(name)
        if not m:
            continue
        for strat, label in (
            ("enrolled_aware", "enrolled-aware (joint)"),
            ("season_total", "season-total (baseline)"),
        ):
            g = m[strat]
            print(
                f" {name + ' / ' + label:<34}{_fmt_m(g['price']):>8}"
                f"{g['real_season_total']:>8.0f}"
                f"{g['pct_of_season_ceiling']:>9.1f}%{g['pct_of_enrolled_ceiling']:>8.1f}%"
            )
    print(" " + "-" * 74)
    r = rec["recommended"]
    print(
        f" recommended: {r['model']} / {r['strategy']} -> {r['real_season_total']:.0f} pts "
        f"({r['pct_of_season_ceiling']:.1f}% season, {r['pct_of_enrolled_ceiling']:.1f}% enrolled)"
    )
    print()

    print(f" RECOMMENDED 20-RIDER SQUAD ({r['model']} / {r['strategy']})")
    print(" " + "-" * 74)
    print(f" {'rider':<26}{'role':<22}{'price':>8}{'real pts':>10}")
    for row in rec["recommended_squad"]:
        print(
            f" {row['name']:<26}{row['role']:<22}{_fmt_m(row['price']):>8}"
            f"{row['real_enrolled_contribution']:>10.0f}"
        )
    print(" " + "-" * 74)
    print(f" squad price {_fmt_m(r['price'])} of {_fmt_m(rec['budget'])}")
    print(line)


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "tdf2026"
    train = sys.argv[2] if len(sys.argv) > 2 else None
    top = int(sys.argv[3]) if len(sys.argv) > 3 else TOP_PER_STAGE_DEFAULT
    rec = build_recommendation(target, train, top)
    print_recommendation(rec)

    dest = DATA_ROOT / target / "recommendation_enrolled.json"
    dest.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    print(f"\n wrote {dest}")


if __name__ == "__main__":
    main()
