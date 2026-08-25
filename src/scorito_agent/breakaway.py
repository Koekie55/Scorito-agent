"""Stage-level breakaway priors and rider-level permission scenarios."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

EARLY_STAGE_MAX = 7
UNIPUERTO_VERTICAL_METERS_MAX = 3_000
PRIOR_ALPHA = 2.0
PRIOR_BETA = 2.0
UNIPUERTO_PRIOR_WEIGHT = 0.50


def is_early_stage(stage_no: int) -> bool:
    return stage_no <= EARLY_STAGE_MAX


def is_unipuerto_like(stage: dict[str, Any]) -> bool:
    """Return the reproducible low-total-climbing summit proxy used in history."""
    return (
        str(stage.get("profile_type") or "").lower() == "mountain"
        and str(stage.get("finish_type") or "").lower() == "summit"
        and 0 < float(stage.get("vertical_meters") or 0) <= UNIPUERTO_VERTICAL_METERS_MAX
    )


def _smoothed_rate(
    records: Sequence[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
) -> tuple[float, int, int]:
    selected = [record for record in records if predicate(record)]
    wins = sum(int(record["breakaway_win"]) for record in selected)
    total = len(selected)
    rate = (wins + PRIOR_ALPHA) / (total + PRIOR_ALPHA + PRIOR_BETA)
    return rate, wins, total


def historical_breakaway_prior(
    records: Sequence[dict[str, Any]], stage: dict[str, Any]
) -> dict[str, float]:
    """Return the conservative stage prior used by the production projection."""
    stage_no = int(stage.get("stage_no") or 1)
    early = is_early_stage(stage_no)
    unipuerto = is_unipuerto_like(stage)
    global_rate, global_wins, global_total = _smoothed_rate(records, lambda _: True)
    early_rate, early_wins, early_total = _smoothed_rate(
        records, lambda record: is_early_stage(int(record["stage_no"]))
    )
    unipuerto_rate, unipuerto_wins, unipuerto_total = _smoothed_rate(
        records,
        lambda record: 0 < float(record["vertical_meters"]) <= UNIPUERTO_VERTICAL_METERS_MAX,
    )
    probability = global_rate
    if unipuerto:
        probability += UNIPUERTO_PRIOR_WEIGHT * (unipuerto_rate - global_rate)
    return {
        "probability": max(0.05, min(0.95, probability)),
        "global_rate": global_rate,
        "early_rate": early_rate,
        "unipuerto_rate": unipuerto_rate,
        "global_wins": float(global_wins),
        "global_total": float(global_total),
        "early_wins": float(early_wins),
        "early_total": float(early_total),
        "unipuerto_wins": float(unipuerto_wins),
        "unipuerto_total": float(unipuerto_total),
        "early": float(early),
        "unipuerto_like": float(unipuerto),
    }



# --- Rider-level breakaway permission ---------------------------------------
# KOM/polka-dot ambition raises how often a climber is *in* the move; a
# compressed early GC field grants that marked rider *less* space to stay clear.
ENTRY_ATTEMPT_GAIN = 0.06
KOM_MARKING_PENALTY = 0.40


def climber_break_dependence(gc_strength: float, climb_strength: float) -> float:
    """Share of a rider's summit-stage upside that relies on a surviving break.

    A GC-calibre climber wins from the front group; a pure climber or KOM hunter
    who sits well down on GC needs the breakaway to stay clear to score.
    """
    return max(0.0, min(1.0, float(climb_strength) - float(gc_strength)))


def summit_breakaway_rider_factor(
    prior: dict[str, float],
    gc_strength: float,
    climb_strength: float,
) -> dict[str, float]:
    """Rider-level breakaway permission multiplier for a mountain summit stage.

    Two opposing mechanisms are modelled and reported separately: KOM ambition
    raises break-entry frequency (``entry_attempt_factor`` > 1), while a
    compressed-GC stage lowers the marked rider''s permission on top of the lower
    baseline survival (``marking_factor`` < 1, ``space_ratio`` < 1). Only the
    break-dependent share of value is affected, and the net factor is capped at
    1.0 so a summit prior can never inflate a rider.
    """
    survival = float(prior.get("probability") or prior.get("survival_probability") or 0.0)
    global_rate = float(prior.get("global_rate") or 0.0)
    space_ratio = survival / global_rate if global_rate > 0 else 1.0
    break_dependence = climber_break_dependence(gc_strength, climb_strength)
    kom_ambition = break_dependence
    entry_attempt = 1.0 + ENTRY_ATTEMPT_GAIN * kom_ambition
    marking = 1.0 - KOM_MARKING_PENALTY * kom_ambition * max(0.0, 1.0 - space_ratio)
    permission = space_ratio * entry_attempt * marking
    factor = min(1.0, (1.0 - break_dependence) + break_dependence * permission)
    return {
        "factor": factor,
        "space_ratio": space_ratio,
        "break_dependence": break_dependence,
        "kom_ambition": kom_ambition,
        "entry_attempt_factor": entry_attempt,
        "marking_factor": marking,
        "permission_factor": permission,
    }
