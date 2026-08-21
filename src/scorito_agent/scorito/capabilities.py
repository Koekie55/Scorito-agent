"""Shared rider-capability semantics for projected and live recommendations."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping


@dataclass(frozen=True)
class RawCapabilitySignals:
    """Immutable capability inputs as observed from the source models."""

    overall: float = 0.0
    form: float = 0.0
    gc: float = 0.0
    climb: float = 0.0
    sprint: float = 0.0
    time_trial: float = 0.0
    prologue: float = 0.0
    classic: float = 0.0
    previous_vuelta: float = 0.0
    recent_flat: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class CapabilityOverride:
    override_id: str
    rider_slug: str
    capability: str
    floor: float
    evidence_source: str
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SprintAssessment:
    eligible: bool
    reason: str
    merit: float
    raw_sprint: float
    effective_sprint: float
    recent_flat: float
    applied_override_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["applied_override_ids"] = list(self.applied_override_ids)
        return data


SPRINT_SIGNAL_THRESHOLD = 0.20
CORROBORATED_SPRINT_SIGNAL_THRESHOLD = 0.03
RECENT_FLAT_CORROBORATION_THRESHOLD = 0.10
CAPABILITY_AUDIT_FIELDNAMES = (
    "sprint_eligible",
    "sprint_eligibility_reason",
    "sprint_merit",
    "sprint_raw_signal",
    "sprint_effective_signal",
    "sprint_recent_flat_signal",
    "sprint_applied_override_ids",
    "capability_override_ids",
    "capability_override_evidence",
    "capability_override_rationale",
    "raw_capabilities",
    "effective_capabilities",
)


CAPABILITY_OVERRIDES: tuple[CapabilityOverride, ...] = (
    CapabilityOverride(
        override_id="wout-van-aert-sprint-2024-2025",
        rider_slug="wout-van-aert",
        capability="sprint",
        floor=0.52,
        evidence_source=(
            "PCS: 2025 Tour stage 21 win and stage 8 second; "
            "2024 Tour stages 12 and 13 second"
        ),
        rationale=(
            "Recent WorldTour bunch and reduced-bunch results establish Van Aert "
            "as a genuine sprint option despite sparse PCS sprint-ranking coverage."
        ),
    ),
    CapabilityOverride(
        override_id="wout-van-aert-time-trial-2024",
        rider_slug="wout-van-aert",
        capability="time_trial",
        floor=0.75,
        evidence_source="PCS/Olympics: bronze in the 2024 Olympic individual time trial",
        rationale="Recent championship evidence establishes elite time-trial capability.",
    ),
    CapabilityOverride(
        override_id="wout-van-aert-prologue-2024",
        rider_slug="wout-van-aert",
        capability="prologue",
        floor=0.65,
        evidence_source=(
            "PCS/Olympics: 2024 Olympic ITT bronze plus repeated elite short-TT results"
        ),
        rationale="Short time trials are a documented scoring route for Van Aert.",
    ),
    CapabilityOverride(
        override_id="kaden-groves-sprint-vuelta-2024",
        rider_slug="kaden-groves",
        capability="sprint",
        floor=0.78,
        evidence_source=(
            "PCS: three 2024 Vuelta stage wins, another stage second, and strong "
            "previous-Vuelta performance"
        ),
        rationale=(
            "Recent repeated Grand Tour bunch-sprint results establish Groves as "
            "an elite sprint option despite a missing current PCS ranking signal."
        ),
    ),
)


def raw_capability_signals(
    rankings: Mapping[str, Mapping[str, float]],
    rider_slug: str,
    *,
    recent_flat: float = 0.0,
) -> RawCapabilitySignals:
    def value(key: str) -> float:
        return max(0.0, float(rankings.get(key, {}).get(rider_slug, 0.0)))

    return RawCapabilitySignals(
        overall=value("overall"),
        form=value("form"),
        gc=value("gc"),
        climb=value("climb"),
        sprint=value("sprint"),
        time_trial=value("tt"),
        prologue=value("prologue"),
        classic=value("classic"),
        previous_vuelta=value("previous_vuelta"),
        recent_flat=max(0.0, float(recent_flat)),
    )


def apply_capability_overrides(
    rider_slug: str,
    raw: RawCapabilitySignals,
) -> tuple[RawCapabilitySignals, tuple[CapabilityOverride, ...]]:
    overrides = tuple(
        override
        for override in CAPABILITY_OVERRIDES
        if override.rider_slug == rider_slug
    )
    return _apply_overrides(raw, overrides)


def _apply_overrides(
    raw: RawCapabilitySignals,
    overrides: tuple[CapabilityOverride, ...],
) -> tuple[RawCapabilitySignals, tuple[CapabilityOverride, ...]]:
    effective = raw
    applied: list[CapabilityOverride] = []
    for override in overrides:
        current = float(getattr(effective, override.capability))
        if current >= override.floor:
            continue
        effective = replace(effective, **{override.capability: override.floor})
        applied.append(override)
    return effective, tuple(applied)


def assess_sprint_capability(
    raw: RawCapabilitySignals,
    effective: RawCapabilitySignals,
    overrides: tuple[CapabilityOverride, ...] = (),
) -> SprintAssessment:
    sprint_overrides = tuple(
        override.override_id for override in overrides if override.capability == "sprint"
    )
    if sprint_overrides:
        eligible = True
        reason = "documented sprint capability override"
    elif effective.sprint >= SPRINT_SIGNAL_THRESHOLD:
        eligible = True
        reason = (
            f"absolute sprint signal {effective.sprint:.3f} "
            f">= {SPRINT_SIGNAL_THRESHOLD:.2f}"
        )
    elif (
        raw.sprint >= CORROBORATED_SPRINT_SIGNAL_THRESHOLD
        and raw.recent_flat >= RECENT_FLAT_CORROBORATION_THRESHOLD
    ):
        eligible = True
        reason = (
            f"raw sprint signal {raw.sprint:.3f} with recent flat-result "
            f"corroboration {raw.recent_flat:.3f}"
        )
    else:
        eligible = False
        reason = (
            "insufficient absolute sprint evidence; form and generic flat "
            "strength cannot establish sprint identity"
        )
    merit = (
        0.66 * effective.sprint
        + 0.18 * raw.form
        + 0.16 * raw.recent_flat
    )
    return SprintAssessment(
        eligible=eligible,
        reason=reason,
        merit=round(merit, 6),
        raw_sprint=round(raw.sprint, 6),
        effective_sprint=round(effective.sprint, 6),
        recent_flat=round(raw.recent_flat, 6),
        applied_override_ids=sprint_overrides,
    )


def capability_set(
    rider_slug: str,
    raw: RawCapabilitySignals,
) -> dict[str, Any]:
    effective, overrides = apply_capability_overrides(rider_slug, raw)
    sprint = assess_sprint_capability(raw, effective, overrides)
    return {
        "raw": raw.as_dict(),
        "effective": effective.as_dict(),
        "overrides": [override.as_dict() for override in overrides],
        "sprint_assessment": sprint.as_dict(),
    }


def sprint_assessment_from_serialized(
    capability: Mapping[str, Any],
) -> SprintAssessment:
    raw_data = capability.get("raw")
    effective_data = capability.get("effective")
    override_data = capability.get("overrides", [])
    if not isinstance(raw_data, Mapping) or not isinstance(effective_data, Mapping):
        raise ValueError("serialized capability is missing raw/effective signals")
    signal_fields = set(RawCapabilitySignals.__dataclass_fields__)
    if missing := signal_fields - set(raw_data):
        raise ValueError(f"serialized raw capability is missing signals: {sorted(missing)}")
    if missing := signal_fields - set(effective_data):
        raise ValueError(
            f"serialized effective capability is missing signals: {sorted(missing)}"
        )
    if not isinstance(override_data, Sequence) or isinstance(
        override_data, (str, bytes)
    ):
        raise TypeError("serialized capability overrides must be a sequence")
    if any(not isinstance(row, Mapping) for row in override_data):
        raise TypeError("each serialized capability override must be a mapping")
    raw = RawCapabilitySignals(
        **{field: float(raw_data[field]) for field in signal_fields}
    )
    overrides = tuple(
        CapabilityOverride(
            override_id=str(row["override_id"]),
            rider_slug=str(row["rider_slug"]),
            capability=str(row["capability"]),
            floor=float(row["floor"]),
            evidence_source=str(row["evidence_source"]),
            rationale=str(row["rationale"]),
        )
        for row in override_data
    )
    known_overrides = {
        override.override_id: override for override in CAPABILITY_OVERRIDES
    }
    for override in overrides:
        if known_overrides.get(override.override_id) != override:
            raise ValueError(
                f"unknown or altered capability override {override.override_id!r}"
            )
    reconstructed, applied = _apply_overrides(raw, overrides)
    effective = RawCapabilitySignals(
        **{field: float(effective_data[field]) for field in signal_fields}
    )
    if effective != reconstructed:
        raise ValueError(
            "serialized effective capabilities disagree with raw signals and overrides"
        )
    return assess_sprint_capability(raw, effective, applied)


def validated_sprint_assessment_from_serialized(
    capability: Mapping[str, Any],
) -> SprintAssessment:
    """Rebuild and verify the sprint verdict stored with capability evidence."""
    stored = capability.get("sprint_assessment")
    if not isinstance(stored, Mapping):
        raise TypeError("serialized sprint_assessment must be a mapping")
    reconstructed = sprint_assessment_from_serialized(capability)
    if dict(stored) != reconstructed.as_dict():
        raise ValueError(
            "stored sprint assessment disagrees with serialized capability evidence"
        )
    return reconstructed


def capability_audit_fields(
    capability: Mapping[str, Any],
    *,
    assessment: SprintAssessment | None = None,
) -> dict[str, Any]:
    """Return stable CSV fields for validated serialized capabilities."""
    reconstructed = validated_sprint_assessment_from_serialized(capability)
    if assessment is not None and assessment != reconstructed:
        raise ValueError("provided sprint assessment disagrees with capability evidence")

    override_data = capability.get("overrides", [])
    if not isinstance(override_data, Sequence) or isinstance(
        override_data, (str, bytes)
    ):
        raise TypeError("serialized capability overrides must be a sequence")
    overrides = [row for row in override_data if isinstance(row, Mapping)]
    raw = capability["raw"]
    effective = capability["effective"]
    if not isinstance(raw, Mapping) or not isinstance(effective, Mapping):
        raise TypeError("serialized raw and effective capabilities must be mappings")

    return {
        "sprint_eligible": "yes" if reconstructed.eligible else "no",
        "sprint_eligibility_reason": reconstructed.reason,
        "sprint_merit": f"{reconstructed.merit:.6f}",
        "sprint_raw_signal": f"{reconstructed.raw_sprint:.6f}",
        "sprint_effective_signal": f"{reconstructed.effective_sprint:.6f}",
        "sprint_recent_flat_signal": f"{reconstructed.recent_flat:.6f}",
        "sprint_applied_override_ids": "; ".join(
            reconstructed.applied_override_ids
        ),
        "capability_override_ids": "; ".join(
            str(row.get("override_id", "")) for row in overrides
        ),
        "capability_override_evidence": " | ".join(
            f"{row.get('override_id', '')}: {row.get('evidence_source', '')}"
            for row in overrides
        ),
        "capability_override_rationale": " | ".join(
            f"{row.get('override_id', '')}: {row.get('rationale', '')}"
            for row in overrides
        ),
        "raw_capabilities": json.dumps(
            dict(raw), sort_keys=True, separators=(",", ":")
        ),
        "effective_capabilities": json.dumps(
            dict(effective), sort_keys=True, separators=(",", ":")
        ),
    }
