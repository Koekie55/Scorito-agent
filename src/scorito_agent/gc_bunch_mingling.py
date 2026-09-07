"""Grounded GC-versus-sprinter mingling gate for hilly, non-summit finishes.

The sprint-survival mechanism in refresh_vuelta_stage_predictions.py measures
whether a rider can survive a selective climb using pure-sprinter evidence
(profile_strength.hilly, classic results). It has no path for a GC/climb
favourite who is not a sprinter but still contests a reduced bunch for bonus
seconds -- exactly what happened on Vuelta a Espana 2026 stage 2 (Monaco ->
Manosque, hilly/uphill, 3028 vertical metres, 3.6%/km final climb), where the
top 25 finishers all recorded the same time and Tadej Pogacar took 3rd, yet the
production top-20 prediction never considered him.

data/historical/gt_hilly_bunch_mingling_labels.csv and
data/pcs/gt_hilly_bunch_mingling_analysis.json ground the gate in 53 real
2024-2026 Grand Tour stages (rebuild both with
scripts/analyze_gt_hilly_bunch_mingling.py): a hilly-profile stage with a
final-km gradient at or above 6.0% never kept a front group of 10+ finishers in
the sample (0/6), while stages below 6.0% did about a third of the time
(10/29, Wilson 95% CI 19.9-52.7%). Below 6.0% is therefore a *possible* reduced
bunch, never a guaranteed one, so this only unlocks GC credibility as an
alternate survival path -- it never adds a standalone score bonus.
"""

from __future__ import annotations

from typing import Any

GRADIENT_CEILING_PCT = 6.0
VERTICAL_METERS_MIN = 1_200.0
VERTICAL_METERS_MAX = 3_500.0


def is_bunch_mingling_eligible(stage: dict[str, Any]) -> bool:
    """Return whether this stage profile can plausibly finish in a reduced bunch.

    Restricted to hilly, uphill finishes below the grounded gradient ceiling and
    within the observed vertical-metres band; mountain summits and flat sprints
    are handled by their own existing factors and are never eligible here.
    """
    if str(stage.get("profile_type", "")).lower() != "hilly":
        return False
    if str(stage.get("finish_type", "")).lower() != "uphill":
        return False
    gradient = float(stage.get("gradient_final_km") or 0.0)
    if gradient >= GRADIENT_CEILING_PCT:
        return False
    vertical = float(stage.get("vertical_meters") or 0.0)
    return VERTICAL_METERS_MIN <= vertical <= VERTICAL_METERS_MAX


def gc_bunch_credibility(stage: dict[str, Any], rider: dict[str, Any]) -> float:
    """Return 0 unless eligible, else the rider's GC/climb credibility in [0, 1].

    Mirrors the credibility calculation already used for mountain finishes
    (max of the PCS GC/climb ranking signal and recent hilly-profile strength)
    so a rider only benefits here if the same evidence would already have
    supported them on a genuine mountain stage.
    """
    if not is_bunch_mingling_eligible(stage):
        return 0.0
    signals = rider.get("signals", {})
    ranking_credibility = max(
        float(signals.get("gc", 0.0)),
        float(signals.get("climb", 0.0)),
    )
    hilly_strength = min(
        1.0,
        float(rider.get("recent_evidence", {}).get("profile_strength", {}).get("hilly", 0.0)),
    )
    return round(max(ranking_credibility, hilly_strength), 4)