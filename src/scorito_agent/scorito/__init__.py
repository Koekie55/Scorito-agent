"""Part 1 — Scorito data model, scoring model and team optimiser."""

from .capabilities import (
    CAPABILITY_AUDIT_FIELDNAMES,
    CAPABILITY_OVERRIDES,
    CORROBORATED_SPRINT_SIGNAL_THRESHOLD,
    RECENT_FLAT_CORROBORATION_THRESHOLD,
    SPRINT_SIGNAL_THRESHOLD,
    CapabilityOverride,
    RawCapabilitySignals,
    SprintAssessment,
    apply_capability_overrides,
    assess_sprint_capability,
    capability_audit_fields,
    capability_set,
    raw_capability_signals,
    sprint_assessment_from_serialized,
    validated_sprint_assessment_from_serialized,
)
from .loader import DATA_ROOT, load_snapshot
from .models import (
    QUALITY_LABELS,
    ROLE_LABELS,
    STAGE_TYPE_LABELS,
    TERRAIN_LABELS,
    Rider,
    Snapshot,
    Stage,
)
from .optimizer import (
    SquadPlan,
    StageLineup,
    actual_total_values,
    back_analysis,
    best_stage_lineup,
    expected_total_values,
    joint_enrolled_squad,
    optimal_hindsight_squad,
    pick_squad,
    stage_regret,
)
from .scoring import StageScorer, heuristic_score, quality_relevance

__all__ = [
    # capabilities
    "RawCapabilitySignals",
    "CapabilityOverride",
    "SprintAssessment",
    "CAPABILITY_OVERRIDES",
    "CAPABILITY_AUDIT_FIELDNAMES",
    "SPRINT_SIGNAL_THRESHOLD",
    "CORROBORATED_SPRINT_SIGNAL_THRESHOLD",
    "RECENT_FLAT_CORROBORATION_THRESHOLD",
    "raw_capability_signals",
    "apply_capability_overrides",
    "assess_sprint_capability",
    "capability_audit_fields",
    "capability_set",
    "sprint_assessment_from_serialized",
    "validated_sprint_assessment_from_serialized",
    # loader
    "load_snapshot",
    "DATA_ROOT",
    # models
    "Rider",
    "Stage",
    "Snapshot",
    "ROLE_LABELS",
    "QUALITY_LABELS",
    "STAGE_TYPE_LABELS",
    "TERRAIN_LABELS",
    # scoring
    "StageScorer",
    "heuristic_score",
    "quality_relevance",
    # optimizer
    "SquadPlan",
    "StageLineup",
    "pick_squad",
    "best_stage_lineup",
    "back_analysis",
    "optimal_hindsight_squad",
    "joint_enrolled_squad",
    "actual_total_values",
    "expected_total_values",
    "stage_regret",
]
