"""ILP squad optimiser and stage back-analysis for Scorito.

Game rules (from ``markets_registry.json`` ``global``):

* Pick **20 riders** upfront, total price <= the market budget (45M for TdF).
* Each stage, enrol **9** of those 20, and nominate **1 captain** who scores
  ``CaptainFactor`` x (2x) points.

This module provides:

* :func:`pick_squad` - ILP: choose the 20-rider squad maximising a value
  function subject to the budget (uses PuLP; greedy fallback if unavailable).
* :func:`best_stage_lineup` - given a squad and per-rider points for a stage,
  choose the best 9 + captain.
* :func:`back_analysis` - for a squad, the best achievable lineup every stage
  using the *real* points ("which of my 20 I should have enrolled").
* :func:`optimal_hindsight_squad` - the theoretical best 20-rider squad and
  season total under budget, using real points (an upper-bound benchmark).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable

from .models import Rider, Snapshot, Stage
from .scoring import StageScorer


@dataclass
class StageLineup:
    stage: Stage
    rider_ids: list[int]
    captain_id: int
    captain_points: float
    total: float


@dataclass
class SquadPlan:
    rider_ids: list[int]
    total_price: int
    value: float
    lineups: list[StageLineup] | None = None
    season_total: float | None = None


# --------------------------------------------------------------------------- #
# Squad selection (20 riders under budget)
# --------------------------------------------------------------------------- #


def pick_squad(
    snapshot: Snapshot,
    values: dict[int, float],
    *,
    budget: int | None = None,
    squad_size: int = 20,
    max_riders_per_team: int | None = None,
    coverage_constraints: Iterable[tuple[set[int], int]] | None = None,
    excluded_rider_ids: set[int] | None = None,
) -> SquadPlan:
    """Choose a legal squad maximising ``values`` under budget.

    ``coverage_constraints`` contains ``(eligible_rider_ids, minimum)`` pairs.
    Team caps and exclusions are hard constraints in every solver path; an
    illegal fallback is never returned.
    """
    budget = snapshot.budget if budget is None else budget
    excluded = excluded_rider_ids or set()
    coverage = list(coverage_constraints or [])
    riders = [r for r in snapshot.riders if r.price > 0 and r.rider_id not in excluded]
    if squad_size <= 0:
        raise ValueError("squad_size must be positive")
    if len(riders) < squad_size:
        raise ValueError(
            f"cannot select {squad_size} riders from {len(riders)} eligible riders"
        )
    target_size = squad_size

    chosen = _solve_scipy(
        riders, values, budget, target_size, max_riders_per_team, coverage
    )
    if chosen is None:
        chosen = _solve_ilp(
            riders, values, budget, target_size, max_riders_per_team, coverage
        )
    if chosen is None:
        chosen = _solve_greedy(
            riders, values, budget, target_size, max_riders_per_team, coverage
        )
    if not _is_legal_squad(
        riders, chosen, budget, target_size, max_riders_per_team, coverage
    ):
        raise RuntimeError("unable to produce a squad satisfying all hard constraints")

    total_price = sum(snapshot.rider(rid).price for rid in chosen)
    total_value = sum(values.get(rid, 0.0) for rid in chosen)
    return SquadPlan(rider_ids=chosen, total_price=total_price, value=total_value)


def _solve_scipy(
    riders: list[Rider],
    values: dict[int, float],
    budget: int,
    squad_size: int,
    max_riders_per_team: int | None,
    coverage_constraints: list[tuple[set[int], int]],
) -> list[int] | None:
    """In-process MILP via scipy/HiGHS (no external subprocess)."""
    try:
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp
    except Exception:  # pragma: no cover - scipy missing
        return None

    n = len(riders)
    if n < squad_size:
        return None

    c = np.array([-values.get(r.rider_id, 0.0) for r in riders], dtype=float)
    price = np.array([r.price for r in riders], dtype=float)
    ones = np.ones(n)

    constraints = [
        LinearConstraint(price, -np.inf, float(budget)),
        LinearConstraint(ones, squad_size, squad_size),
    ]
    if max_riders_per_team is not None:
        for team_id in {r.team_id for r in riders}:
            constraints.append(
                LinearConstraint(
                    np.array([float(r.team_id == team_id) for r in riders]),
                    -np.inf,
                    float(max_riders_per_team),
                )
            )
    for eligible_ids, minimum in coverage_constraints:
        constraints.append(
            LinearConstraint(
                np.array([float(r.rider_id in eligible_ids) for r in riders]),
                float(minimum),
                np.inf,
            )
        )
    try:
        res = milp(
            c=c,
            constraints=constraints,
            integrality=np.ones(n),
            bounds=Bounds(0, 1),
        )
    except Exception:  # pragma: no cover - solver error
        return None
    if not res.success or res.x is None:
        return None
    return [riders[i].rider_id for i in range(n) if res.x[i] > 0.5]


def _solve_ilp(
    riders: list[Rider],
    values: dict[int, float],
    budget: int,
    squad_size: int,
    max_riders_per_team: int | None,
    coverage_constraints: list[tuple[set[int], int]],
) -> list[int] | None:
    try:
        import pulp
    except Exception:  # pragma: no cover - pulp missing
        return None

    prob = pulp.LpProblem("scorito_squad", pulp.LpMaximize)
    x = {r.rider_id: pulp.LpVariable(f"x_{r.rider_id}", cat="Binary") for r in riders}

    prob += pulp.lpSum(values.get(r.rider_id, 0.0) * x[r.rider_id] for r in riders)
    prob += pulp.lpSum(r.price * x[r.rider_id] for r in riders) <= budget
    prob += pulp.lpSum(x.values()) == squad_size
    if max_riders_per_team is not None:
        for team_id in {r.team_id for r in riders}:
            prob += pulp.lpSum(
                x[r.rider_id] for r in riders if r.team_id == team_id
            ) <= max_riders_per_team
    for eligible_ids, minimum in coverage_constraints:
        prob += pulp.lpSum(
            x[r.rider_id] for r in riders if r.rider_id in eligible_ids
        ) >= minimum

    try:
        status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
    except Exception:  # pragma: no cover - CBC subprocess blocked
        return None
    if pulp.LpStatus[status] != "Optimal":
        return None
    return [rid for rid, var in x.items() if var.value() and var.value() > 0.5]


def _solve_greedy(
    riders: list[Rider],
    values: dict[int, float],
    budget: int,
    squad_size: int,
    max_riders_per_team: int | None,
    coverage_constraints: list[tuple[set[int], int]],
) -> list[int]:
    """Value-density greedy fallback (not guaranteed optimal)."""
    ranked = sorted(
        riders,
        key=lambda r: values.get(r.rider_id, 0.0) / max(r.price, 1),
        reverse=True,
    )
    chosen: list[int] = []
    spent = 0
    team_counts: dict[int, int] = defaultdict(int)

    def can_add(rider: Rider) -> bool:
        return (
            rider.rider_id not in chosen
            and spent + rider.price <= budget
            and (
                max_riders_per_team is None
                or team_counts[rider.team_id] < max_riders_per_team
            )
        )

    def add(rider: Rider) -> None:
        nonlocal spent
        chosen.append(rider.rider_id)
        spent += rider.price
        team_counts[rider.team_id] += 1

    for eligible_ids, minimum in coverage_constraints:
        candidates = [r for r in ranked if r.rider_id in eligible_ids]
        while sum(rid in eligible_ids for rid in chosen) < minimum:
            rider = next((r for r in candidates if can_add(r)), None)
            if rider is None:
                return []
            add(rider)
    for r in ranked:
        if len(chosen) >= squad_size:
            break
        if can_add(r):
            add(r)
    if len(chosen) < squad_size:
        rest = sorted(
            (r for r in riders if r.rider_id not in chosen), key=lambda r: r.price
        )
        for r in rest:
            if len(chosen) >= squad_size or not can_add(r):
                continue
            add(r)
    return chosen


def _is_legal_squad(
    riders: list[Rider],
    chosen: list[int],
    budget: int,
    squad_size: int,
    max_riders_per_team: int | None,
    coverage_constraints: list[tuple[set[int], int]],
) -> bool:
    by_id = {r.rider_id: r for r in riders}
    if len(chosen) != squad_size or len(set(chosen)) != squad_size:
        return False
    if any(rid not in by_id for rid in chosen):
        return False
    if sum(by_id[rid].price for rid in chosen) > budget:
        return False
    counts: dict[int, int] = defaultdict(int)
    for rid in chosen:
        counts[by_id[rid].team_id] += 1
    if max_riders_per_team is not None and any(
        count > max_riders_per_team for count in counts.values()
    ):
        return False
    return all(
        sum(rid in eligible for rid in chosen) >= minimum
        for eligible, minimum in coverage_constraints
    )


# --------------------------------------------------------------------------- #
# Value functions
# --------------------------------------------------------------------------- #


def actual_total_values(snapshot: Snapshot) -> dict[int, float]:
    """Real season totals per rider (hindsight). Prefers the leaderboard."""
    if snapshot.market_totals:
        return dict(snapshot.market_totals)
    totals: dict[int, float] = {}
    for (_, rid), pts in snapshot.stage_points.items():
        totals[rid] = totals.get(rid, 0.0) + pts
    return totals


def expected_total_values(snapshot: Snapshot, scorer: StageScorer) -> dict[int, float]:
    """Forward-looking season value per rider from the scoring model."""
    return {r.rider_id: scorer.season_value(r, snapshot) for r in snapshot.riders}


# --------------------------------------------------------------------------- #
# Per-stage lineup (9 + captain)
# --------------------------------------------------------------------------- #


def best_stage_lineup(
    stage: Stage,
    squad_ids: list[int],
    points_by_rider: dict[int, float],
    *,
    lineup_size: int = 9,
    captain_factor: int = 2,
) -> StageLineup:
    """Pick the best ``lineup_size`` of the squad + best captain for a stage."""
    if lineup_size <= 0:
        raise ValueError("lineup_size must be positive")
    if len(set(squad_ids)) != len(squad_ids):
        raise ValueError("squad_ids must contain unique riders")
    if len(squad_ids) < lineup_size:
        raise ValueError(
            f"cannot select a {lineup_size}-rider lineup from {len(squad_ids)} squad riders"
        )
    ranked = sorted(
        squad_ids, key=lambda rid: points_by_rider.get(rid, 0.0), reverse=True
    )
    chosen = ranked[:lineup_size]
    captain_id = chosen[0] if chosen else -1
    cap_pts = points_by_rider.get(captain_id, 0.0) if chosen else 0.0
    base = sum(points_by_rider.get(rid, 0.0) for rid in chosen)
    total = base + (captain_factor - 1) * cap_pts
    return StageLineup(
        stage=stage,
        rider_ids=chosen,
        captain_id=captain_id,
        captain_points=cap_pts,
        total=total,
    )


def _stage_points_map(snapshot: Snapshot, stage: Stage, squad_ids: list[int]) -> dict[int, float]:
    return {rid: snapshot.actual_points(rid, stage) for rid in squad_ids}


def back_analysis(snapshot: Snapshot, squad_ids: list[int]) -> SquadPlan:
    """Best achievable per-stage lineups for a squad using the *real* points."""
    lineups: list[StageLineup] = []
    season_total = 0.0
    for stage in snapshot.stages:
        pts = _stage_points_map(snapshot, stage, squad_ids)
        lu = best_stage_lineup(
            stage, squad_ids, pts, captain_factor=snapshot.captain_factor
        )
        lineups.append(lu)
        season_total += lu.total
    total_price = sum(snapshot.rider(rid).price for rid in squad_ids if snapshot.rider(rid))
    value = sum(actual_total_values(snapshot).get(rid, 0.0) for rid in squad_ids)
    return SquadPlan(
        rider_ids=squad_ids,
        total_price=total_price,
        value=value,
        lineups=lineups,
        season_total=season_total,
    )


def optimal_hindsight_squad(
    snapshot: Snapshot, *, budget: int | None = None, squad_size: int = 20
) -> SquadPlan:
    """Best 20-rider squad + season total under budget, using real points."""
    values = actual_total_values(snapshot)
    plan = pick_squad(snapshot, values, budget=budget, squad_size=squad_size)
    back = back_analysis(snapshot, plan.rider_ids)
    back.value = plan.value
    return back


# --------------------------------------------------------------------------- #
# Enrolled-aware joint optimiser (squad + per-stage lineups together)
# --------------------------------------------------------------------------- #


def joint_enrolled_squad(
    snapshot: Snapshot,
    points_fn: Callable[[int, Stage], float],
    *,
    budget: int | None = None,
    squad_size: int = 20,
    lineup_size: int = 9,
    selection_values: dict[int, float] | None = None,
    max_riders_per_team: int | None = None,
    coverage_constraints: Iterable[tuple[set[int], int]] | None = None,
    excluded_rider_ids: set[int] | None = None,
) -> SquadPlan | None:
    """Joint MILP: choose the squad AND the best lineup + captain per stage.

    Unlike :func:`pick_squad` (which maximises a *season-total* proxy) this
    maximises the summed **enrolled** score — best ``lineup_size`` + a doubled
    captain every stage — jointly deciding the ``squad_size`` riders and which
    of them to enrol on each stage. That is the true game objective.

    ``points_fn(rider_id, stage) -> float`` supplies the per-(rider, stage)
    points and may be:

    * **actual** points (``snapshot.actual_points``) → the enrolled-aware
      hindsight *ceiling*; or
    * **predicted** points (a :class:`StageScorer`) → the forward,
      enrolment-aware recommender that buys sprinters for the flat stages the
      season-total optimiser leaves thin.

    ``selection_values`` adds rider-level points that do not depend on stage
    enrolment, such as projected final-classification and jersey bonuses.

    Every eligible rider enters every stage so zero or negative projections do
    not relax the exact lineup and captain cardinalities. Returns a
    :class:`SquadPlan` whose ``value`` is the summed enrolled score under the
    supplied points, or ``None`` if scipy is unavailable / the model is
    infeasible.
    """
    try:
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import coo_matrix
    except Exception:  # pragma: no cover - scipy missing
        return None

    budget = snapshot.budget if budget is None else budget
    excluded = excluded_rider_ids or set()
    coverage = list(coverage_constraints or [])
    riders = [r for r in snapshot.riders if r.price > 0 and r.rider_id not in excluded]
    n = len(riders)
    if squad_size <= 0 or lineup_size <= 0 or lineup_size > squad_size:
        raise ValueError("require 0 < lineup_size <= squad_size")
    if n < squad_size:
        return None
    ridx = {r.rider_id: i for i, r in enumerate(riders)}
    cap_factor = snapshot.captain_factor

    # Variable layout: [ y_0..y_{n-1} | x_0..x_{nx-1} | c_0..c_{nx-1} ]
    #   y_i = rider i in squad
    #   x_k = candidate (rider, stage) enrolled
    #   c_k = candidate (rider, stage) is captain
    y_off = 0
    x_meta: list[tuple[int, int, float]] = []  # (rider_index, stage_index, points)
    for si, st in enumerate(snapshot.stages):
        for r in riders:
            p = float(points_fn(r.rider_id, st))
            x_meta.append((ridx[r.rider_id], si, p))
    x_off = n
    nx = len(x_meta)
    c_off = n + nx
    total_vars = n + nx + nx
    # Objective (maximise -> minimise the negative): sum p*x + (cap-1)*p*c
    c_obj = np.zeros(total_vars)
    for i, rider in enumerate(riders):
        c_obj[y_off + i] = -float((selection_values or {}).get(rider.rider_id, 0.0))
    for k, (_, _, p) in enumerate(x_meta):
        c_obj[x_off + k] = -p
        c_obj[c_off + k] = -(cap_factor - 1) * p

    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    lb: list[float] = []
    ub: list[float] = []
    row = 0

    def add_row(entries: list[tuple[int, float]], lo: float, hi: float) -> None:
        nonlocal row
        for col, v in entries:
            rows.append(row)
            cols.append(col)
            vals.append(v)
        lb.append(lo)
        ub.append(hi)
        row += 1

    # squad size == squad_size
    add_row([(y_off + i, 1.0) for i in range(n)], squad_size, squad_size)
    # budget
    add_row(
        [(y_off + i, float(riders[i].price)) for i in range(n)],
        -np.inf,
        float(budget),
    )
    if max_riders_per_team is not None:
        for team_id in {r.team_id for r in riders}:
            add_row(
                [
                    (y_off + i, 1.0)
                    for i, rider in enumerate(riders)
                    if rider.team_id == team_id
                ],
                -np.inf,
                float(max_riders_per_team),
            )
    for eligible_ids, minimum in coverage:
        add_row(
            [
                (y_off + i, 1.0)
                for i, rider in enumerate(riders)
                if rider.rider_id in eligible_ids
            ],
            float(minimum),
            np.inf,
        )

    # per-stage: exactly lineup_size enrolled and exactly one captain
    stage_x: dict[int, list[int]] = defaultdict(list)
    for k, (_, si, _) in enumerate(x_meta):
        stage_x[si].append(k)
    for si in range(len(snapshot.stages)):
        ks = stage_x[si]
        add_row([(x_off + k, 1.0) for k in ks], float(lineup_size), float(lineup_size))
        add_row([(c_off + k, 1.0) for k in ks], 1.0, 1.0)

    # link x_k <= y_rider and c_k <= x_k
    for k, (ri, _, _) in enumerate(x_meta):
        add_row([(x_off + k, 1.0), (y_off + ri, -1.0)], -np.inf, 0.0)
        add_row([(c_off + k, 1.0), (x_off + k, -1.0)], -np.inf, 0.0)

    A = coo_matrix((vals, (rows, cols)), shape=(row, total_vars)).tocsr()
    constraints = LinearConstraint(A, np.array(lb), np.array(ub))

    try:
        res = milp(
            c=c_obj,
            constraints=constraints,
            integrality=np.ones(total_vars),
            bounds=Bounds(0, 1),
        )
    except Exception:  # pragma: no cover - solver error
        return None
    if not res.success or res.x is None:
        return None

    x = res.x
    squad_ids = [riders[i].rider_id for i in range(n) if x[y_off + i] > 0.5]
    if not _is_legal_squad(
        riders, squad_ids, budget, squad_size, max_riders_per_team, coverage
    ):
        return None
    enrolled_total = -float(res.fun)
    total_price = sum(snapshot.rider(rid).price for rid in squad_ids)
    return SquadPlan(
        rider_ids=squad_ids,
        total_price=total_price,
        value=enrolled_total,
    )


def stage_regret(
    snapshot: Snapshot, squad_ids: list[int], stage: Stage
) -> tuple[Rider | None, Rider | None, float]:
    """Best rider you had vs best rider in the whole market for a stage.

    Returns (best_owned, best_in_market, points_gap).
    """
    def pts(rid: int) -> float:
        return snapshot.actual_points(rid, stage)

    best_owned = max(
        (snapshot.rider(r) for r in squad_ids if snapshot.rider(r)),
        key=lambda r: pts(r.rider_id),
        default=None,
    )
    best_market = max(
        snapshot.riders, key=lambda r: pts(r.rider_id), default=None
    )
    gap = 0.0
    if best_owned and best_market:
        gap = pts(best_market.rider_id) - pts(best_owned.rider_id)
    return best_owned, best_market, gap
