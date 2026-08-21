"""Deep per-stage study — "learn from the best scoring per stage and overall".

This goes beyond ``report.py`` (which picks the 20 that maximise *season-total*
leaderboard points) and answers the sharper strategy questions:

1. **Best per stage** — for every stage, the top market scorers, their role,
   price and points, plus which rider *archetype* actually won that profile.
2. **Best overall** — the season's top scorers and, crucially, the top
   *value* riders (points per million), which is what actually wins the draft.
3. **Archetype lesson** — how points and stage wins split across rider roles
   and stage terrain, so the day-1 budget can be allocated on purpose.
4. **Enrolled-aware ceiling** — a joint MILP that picks the 20 *and* the 9+captain
   per stage together to maximise the summed enrolled score. The season-total
   optimum (report.py) over-buys redundant GC/climbers who can't all be enrolled
   on the same mountain stage and leaves flat/ITT stages thin; this recovers the
   points left on the table.

Usage:
    python scripts/stage_study.py [slug]     # default tdf2026
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scorito_agent.scorito import (  # noqa: E402
    DATA_ROOT,
    Snapshot,
    actual_total_values,
    back_analysis,
    load_snapshot,
)


def _fmt_m(price: float) -> str:
    return f"{price / 1_000_000:.2f}M"


# --------------------------------------------------------------------------- #
# 1-3. Descriptive study
# --------------------------------------------------------------------------- #

def descriptive_study(snap: Snapshot) -> dict:
    totals = actual_total_values(snap)

    # Per-stage top-5 scorers ------------------------------------------------
    per_stage = []
    for st in snap.stages:
        scored = [
            (snap.actual_points(r.rider_id, st), r) for r in snap.riders
        ]
        scored = [(p, r) for p, r in scored if p > 0]
        scored.sort(key=lambda t: (t[0], -t[1].price), reverse=True)
        per_stage.append(
            {
                "order": st.order,
                "label": st.label,
                "n_scorers": len(scored),
                "top": [
                    {
                        "name": r.name,
                        "role": r.role_label,
                        "price": r.price,
                        "points": p,
                    }
                    for p, r in scored[:5]
                ],
            }
        )

    # Overall best (season leaderboard) --------------------------------------
    overall = sorted(
        snap.riders, key=lambda r: totals.get(r.rider_id, 0.0), reverse=True
    )[:15]
    overall_rows = [
        {
            "name": r.name,
            "role": r.role_label,
            "price": r.price,
            "season": totals.get(r.rider_id, 0.0),
            "ppm": totals.get(r.rider_id, 0.0) / max(r.price_m, 0.01),
        }
        for r in overall
    ]

    # Value leaders (points per million) — what actually wins the draft ------
    priced = [r for r in snap.riders if r.price >= 500_000]
    priced.sort(
        key=lambda r: totals.get(r.rider_id, 0.0) / max(r.price_m, 0.01),
        reverse=True,
    )
    value_rows = [
        {
            "name": r.name,
            "role": r.role_label,
            "price": r.price,
            "season": totals.get(r.rider_id, 0.0),
            "ppm": totals.get(r.rider_id, 0.0) / max(r.price_m, 0.01),
        }
        for r in priced[:15]
    ]

    # Archetype x terrain — where do the points live? ------------------------
    # points_by[terrain][role] = summed real stage points
    points_by: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    wins_by: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    stages_by_terrain: dict[str, int] = defaultdict(int)
    for st in snap.stages:
        stages_by_terrain[st.terrain_label] += 1
        best_p, best_r = 0.0, None
        for r in snap.riders:
            p = snap.actual_points(r.rider_id, st)
            if p > 0:
                points_by[st.terrain_label][r.role_label] += p
            if p > best_p:
                best_p, best_r = p, r
        if best_r is not None:
            wins_by[st.terrain_label][best_r.role_label] += 1

    archetype = {
        terrain: {
            "n_stages": stages_by_terrain[terrain],
            "points_by_role": dict(sorted(roles.items(), key=lambda kv: kv[1], reverse=True)),
            "wins_by_role": dict(wins_by[terrain]),
        }
        for terrain, roles in points_by.items()
    }

    return {
        "per_stage": per_stage,
        "overall": overall_rows,
        "value_leaders": value_rows,
        "archetype": archetype,
    }


# --------------------------------------------------------------------------- #
# 4. Enrolled-aware joint MILP (pick 20 + 9/stage + captain together)
# --------------------------------------------------------------------------- #

def enrolled_aware_squad(snap: Snapshot) -> dict | None:
    """Maximise summed enrolled score (best 9 + captain doubled) over stages,
    jointly choosing the 20-rider squad and each stage lineup.

    This is the *true* game objective; ``optimal_hindsight_squad`` maximises
    the looser season-total proxy. Returns None if scipy is unavailable.
    """
    try:
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import coo_matrix
    except Exception:  # pragma: no cover - scipy missing
        return None

    riders = [r for r in snap.riders if r.price > 0]
    n = len(riders)
    ridx = {r.rider_id: i for i, r in enumerate(riders)}
    cap_factor = snap.captain_factor

    # Variable layout: [ y_0..y_{n-1} | x vars | c vars ]
    # y_i  = rider i in squad
    # x_k  = rider enrolled on a stage   (only candidates with points>0)
    # c_k  = rider captain on a stage    (only candidates with points>0)
    y_off = 0
    x_meta: list[tuple[int, int, float]] = []  # (rider_index, stage_index, points)
    x_index: dict[tuple[int, int], int] = {}
    for si, st in enumerate(snap.stages):
        for r in riders:
            p = snap.actual_points(r.rider_id, st)
            if p > 0:
                x_index[(ridx[r.rider_id], si)] = n + len(x_meta)
                x_meta.append((ridx[r.rider_id], si, p))
    x_off = n
    nx = len(x_meta)
    c_off = n + nx
    # captain candidate for every enrolled candidate
    nc = nx
    total_vars = n + nx + nc

    # Objective (maximise): sum p * x + sum (cap_factor-1) * p * c
    c_obj = np.zeros(total_vars)
    for k, (_, _, p) in enumerate(x_meta):
        c_obj[x_off + k] = -p
        c_obj[c_off + k] = -(cap_factor - 1) * p

    rows, cols, vals = [], [], []
    lb, ub = [], []
    row = 0

    def add_row(entries, lo, hi):
        nonlocal row
        for col, v in entries:
            rows.append(row)
            cols.append(col)
            vals.append(v)
        lb.append(lo)
        ub.append(hi)
        row += 1

    # squad size == 20
    add_row([(y_off + i, 1.0) for i in range(n)], 20, 20)
    # budget
    add_row([(y_off + i, float(riders[i].price)) for i in range(n)], -np.inf, float(snap.budget))

    # per-stage: sum x <= 9 ; sum c <= 1
    stage_x: dict[int, list[int]] = defaultdict(list)
    for k, (_, si, _) in enumerate(x_meta):
        stage_x[si].append(k)
    for si, ks in stage_x.items():
        add_row([(x_off + k, 1.0) for k in ks], -np.inf, 9)
        add_row([(c_off + k, 1.0) for k in ks], -np.inf, 1)

    # link x_k <= y_{rider}, and c_k <= x_k
    for k, (ri, _, _) in enumerate(x_meta):
        add_row([(x_off + k, 1.0), (y_off + ri, -1.0)], -np.inf, 0.0)
        add_row([(c_off + k, 1.0), (x_off + k, -1.0)], -np.inf, 0.0)

    A = coo_matrix((vals, (rows, cols)), shape=(row, total_vars)).tocsr()
    constraints = LinearConstraint(A, np.array(lb), np.array(ub))

    res = milp(
        c=c_obj,
        constraints=constraints,
        integrality=np.ones(total_vars),
        bounds=Bounds(0, 1),
    )
    if not res.success or res.x is None:
        return None

    x = res.x
    squad_ids = [riders[i].rider_id for i in range(n) if x[y_off + i] > 0.5]
    enrolled_total = -float(res.fun)

    # Rebuild per-stage lineups from the solution for reporting
    plan = back_analysis(snap, squad_ids)  # optimal enrollment for that squad
    return {
        "squad_ids": squad_ids,
        "enrolled_total": enrolled_total,
        "recomputed_enrolled_total": plan.season_total,
        "total_vars": total_vars,
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def print_study(snap: Snapshot, desc: dict, enrolled: dict | None) -> None:
    line = "=" * 74
    totals = actual_total_values(snap)
    print(line)
    print(f" SCORITO STAGE STUDY — {snap.slug} (market {snap.market_id})")
    print(line)

    print("\n BEST SCORER PER STAGE (whole market)")
    print(" " + "-" * 72)
    print(f" {'#':>3} {'stage':<20}{'winner':<24}{'role':<16}{'price':>7}{'pts':>5}")
    for s in desc["per_stage"]:
        w = s["top"][0] if s["top"] else None
        if w:
            print(
                f" {s['order']:>3} {s['label']:<20}{w['name'][:23]:<24}"
                f"{w['role'][:15]:<16}{_fmt_m(w['price']):>7}{w['points']:>5.0f}"
            )

    print("\n OVERALL TOP SEASON SCORERS")
    print(" " + "-" * 72)
    print(f" {'rider':<24}{'role':<18}{'price':>7}{'season':>8}{'pt/M':>7}")
    for r in desc["overall"]:
        print(
            f" {r['name'][:23]:<24}{r['role'][:17]:<18}{_fmt_m(r['price']):>7}"
            f"{r['season']:>8.0f}{r['ppm']:>7.0f}"
        )

    print("\n BEST VALUE (points per million) — the draft-winning picks")
    print(" " + "-" * 72)
    print(f" {'rider':<24}{'role':<18}{'price':>7}{'season':>8}{'pt/M':>7}")
    for r in desc["value_leaders"]:
        print(
            f" {r['name'][:23]:<24}{r['role'][:17]:<18}{_fmt_m(r['price']):>7}"
            f"{r['season']:>8.0f}{r['ppm']:>7.0f}"
        )

    print("\n WHERE THE POINTS LIVE — terrain x rider archetype")
    print(" " + "-" * 72)
    for terrain, info in sorted(
        desc["archetype"].items(), key=lambda kv: kv[1]["n_stages"], reverse=True
    ):
        roles = info["points_by_role"]
        top_roles = list(roles.items())[:3]
        role_str = ", ".join(f"{k} {v:.0f}" for k, v in top_roles)
        wins = ", ".join(f"{k}x{v}" for k, v in info["wins_by_role"].items())
        print(f" {terrain:<10} ({info['n_stages']} stages)  top: {role_str}")
        print(f"            stage wins: {wins}")

    if enrolled:
        season_opt = back_analysis(
            snap, [r for r in _season_total_squad(snap)]
        ).season_total
        print("\n ENROLLED-AWARE OPTIMUM vs season-total optimum")
        print(" " + "-" * 72)
        print(f"   season-total squad enrolled ceiling : {season_opt:.0f} pts")
        print(f"   enrolled-aware squad ceiling        : {enrolled['enrolled_total']:.0f} pts")
        gain = enrolled["enrolled_total"] - season_opt
        print(f"   points recovered by picking for enrolment: {gain:+.0f}")
        print("\n   enrolled-aware optimal 20 (season pts | price):")
        squad = [snap.rider(rid) for rid in enrolled["squad_ids"]]
        squad = [r for r in squad if r]
        squad.sort(key=lambda r: totals.get(r.rider_id, 0.0), reverse=True)
        for r in squad:
            print(
                f"     {r.name[:26]:<27}{r.role_label[:20]:<21}"
                f"{_fmt_m(r.price):>7}{totals.get(r.rider_id,0.0):>7.0f}"
            )
    print(line)


def _season_total_squad(snap: Snapshot) -> list[int]:
    from scorito_agent.scorito import optimal_hindsight_squad

    return optimal_hindsight_squad(snap).rider_ids


def main() -> None:
    slug = sys.argv[1] if len(sys.argv) > 1 else "tdf2026"
    snap = load_snapshot(slug)
    desc = descriptive_study(snap)
    enrolled = enrolled_aware_squad(snap)
    print_study(snap, desc, enrolled)

    out = {
        "market_id": snap.market_id,
        "slug": snap.slug,
        "descriptive": desc,
        "enrolled_aware": enrolled,
    }
    dest = DATA_ROOT / slug / "stage_study.json"
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\n wrote {dest}")


if __name__ == "__main__":
    main()
