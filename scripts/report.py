"""Scorito back-analysis report — the "how to win" blueprint for a market.

Answers the core part-1 question directly from the reconciled ground truth:

* Which **20 riders** should I have bought (<= budget) to maximise points?
* For **each stage**, which **9 + captain** should I have enrolled, and how
  many points would that have scored?
* For each stage, **which single rider I should have had** (best owned vs the
  best in the entire market) and the points gap.
* Budget spent vs the cap, and the season point ceiling.

Per-stage points use the unambiguous 878-basis stage points (enrolling 9 +
captain earns exactly their summed stage points, captain doubled). Final
classification/jersey bonuses accrue once at race end and are reported
separately for the owned squad (they are not attributable to a single stage).

Usage:
    python scripts/report.py [slug]          # default: tdf2026
    python scripts/report.py giro2026
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
    actual_total_values,
    load_snapshot,
    optimal_hindsight_squad,
    stage_regret,
)


def _fmt_m(price: int) -> str:
    return f"{price / 1_000_000:.2f}M"


def build_report(slug: str) -> dict:
    snap = load_snapshot(slug)
    totals = actual_total_values(snap)  # leaderboard-basis season totals

    plan = optimal_hindsight_squad(snap)  # 20 riders <= budget, real points
    squad = [snap.rider(rid) for rid in plan.rider_ids]
    squad = [r for r in squad if r is not None]
    squad.sort(key=lambda r: totals.get(r.rider_id, 0.0), reverse=True)

    # Per-stage lineups (9 + captain) from back-analysis
    lineups = plan.lineups or []
    stage_rows = []
    for lu in lineups:
        cap = snap.rider(lu.captain_id)
        stage_rows.append(
            {
                "order": lu.stage.order,
                "label": lu.stage.label,
                "enrolled": lu.rider_ids,
                "captain_id": lu.captain_id,
                "captain": cap.name if cap else "?",
                "captain_points": lu.captain_points,
                "stage_total": lu.total,
            }
        )

    # Per-stage regret: best owned vs best in the whole market
    regret_rows = []
    total_regret = 0.0
    for stage in snap.stages:
        owned, market, gap = stage_regret(snap, plan.rider_ids, stage)
        total_regret += gap
        regret_rows.append(
            {
                "order": stage.order,
                "label": stage.label,
                "best_owned": owned.name if owned else "?",
                "best_owned_points": snap.actual_points(owned.rider_id, stage)
                if owned
                else 0.0,
                "best_market": market.name if market else "?",
                "best_market_points": snap.actual_points(market.rider_id, stage)
                if market
                else 0.0,
                "gap": gap,
            }
        )

    classification = {
        r.rider_id: snap.classification_bonus(r.rider_id) for r in squad
    }
    classification_total = sum(classification.values())

    return {
        "market_id": snap.market_id,
        "slug": snap.slug,
        "budget": snap.budget,
        "captain_factor": snap.captain_factor,
        "n_riders": len(snap.riders),
        "n_stages": len(snap.stages),
        "squad": [
            {
                "rider_id": r.rider_id,
                "name": r.name,
                "role": r.role_label,
                "price": r.price,
                "season_points": totals.get(r.rider_id, 0.0),
                "classification_bonus": classification.get(r.rider_id, 0.0),
            }
            for r in squad
        ],
        "squad_price": plan.total_price,
        "budget_left": snap.budget - plan.total_price,
        "enrolled_season_total": plan.season_total,
        "classification_total": classification_total,
        "regret_total": total_regret,
        "stages": stage_rows,
        "regret": regret_rows,
    }


def print_report(rep: dict) -> None:
    line = "=" * 72
    print(line)
    print(f" SCORITO BACK-ANALYSIS — {rep['slug']} (market {rep['market_id']})")
    print(line)
    print(
        f" budget {_fmt_m(rep['budget'])} | captain x{rep['captain_factor']} | "
        f"{rep['n_riders']} riders | {rep['n_stages']} stages"
    )
    print()

    print(" OPTIMAL 20-RIDER SQUAD (hindsight, <= budget)")
    print(" " + "-" * 70)
    print(f" {'rider':<26}{'role':<20}{'price':>8}{'season':>9}{'classif':>8}")
    for r in rep["squad"]:
        print(
            f" {r['name']:<26}{r['role']:<20}{_fmt_m(r['price']):>8}"
            f"{r['season_points']:>9.0f}{r['classification_bonus']:>8.0f}"
        )
    print(" " + "-" * 70)
    print(
        f" squad price {_fmt_m(rep['squad_price'])} of {_fmt_m(rep['budget'])}"
        f"  (left {_fmt_m(rep['budget_left'])})"
    )
    print(
        f" enrolled season total (9/stage + captain): "
        f"{rep['enrolled_season_total']:.0f} pts"
    )
    print(
        f" + end-of-race classification bonuses (owned): "
        f"{rep['classification_total']:.0f} pts"
    )
    print()

    print(" PER-STAGE BEST LINEUP (9 enrolled, captain doubled)")
    print(" " + "-" * 70)
    print(f" {'#':>3} {'stage':<34}{'captain':<22}{'pts':>7}")
    for s in rep["stages"]:
        print(
            f" {s['order']:>3} {s['label'][:33]:<34}{s['captain'][:21]:<22}"
            f"{s['stage_total']:>7.0f}"
        )
    print()

    print(" PER-STAGE REGRET — the rider you should have had")
    print(" " + "-" * 70)
    print(f" {'#':>3} {'best owned':<24}{'best in market':<24}{'gap':>6}")
    for s in rep["regret"]:
        flag = " <=" if s["gap"] > 0 else ""
        print(
            f" {s['order']:>3} {s['best_owned'][:23]:<24}"
            f"{s['best_market'][:23]:<24}{s['gap']:>6.0f}{flag}"
        )
    print(" " + "-" * 70)
    print(
        f" total stage-regret vs perfect market picks: "
        f"{rep['regret_total']:.0f} pts"
    )
    print(line)


def main() -> None:
    slug = sys.argv[1] if len(sys.argv) > 1 else "tdf2026"
    rep = build_report(slug)
    print_report(rep)

    dest = DATA_ROOT / slug / "analysis_report.json"
    dest.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print(f"\n wrote {dest}")


if __name__ == "__main__":
    main()
