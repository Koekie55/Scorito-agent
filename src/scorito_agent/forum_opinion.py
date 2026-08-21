"""Bounded WielerFlits forum-opinion intelligence."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

FORUM_OPINION_SHARE = 0.30
CHAT_OPINION_SHARE = 0.70
OPINION_MAX_ADJUSTMENT = 0.16
CLAIM_KIND_WEIGHTS = {"reported_fact": 0.72, "interpretation": 0.38, "guess": 0.0, "humour": 0.0}
PERFORMANCE_CATEGORIES = frozenset({"team_role", "form", "health", "stage_intent", "tactics", "availability"})
VALUE_CATEGORIES = frozenset({"price", "value"})


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def _effect(claim: dict[str, Any]) -> float:
    weight = CLAIM_KIND_WEIGHTS.get(str(claim.get("kind") or "guess"), 0.0)
    confidence = max(0.0, min(1.0, float(claim.get("confidence") or 0.0)))
    return _clamp(float(claim.get("signal") or 0.0)) * weight * confidence


def _aggregate(claims: list[dict[str, Any]], categories: frozenset[str]) -> float:
    grouped: dict[str, list[float]] = defaultdict(list)
    for claim in claims:
        category = str(claim.get("category") or "other")
        if category in categories:
            grouped[category].append(_effect(claim))
    return _clamp(sum(_clamp(sum(values)) for values in grouped.values()))


def _uncertainty_assessment(
    claims: list[dict[str, Any]], performance_signal: float, value_signal: float
) -> dict[str, Any]:
    negative_claims = [
        claim for claim in claims
        if _effect(claim) <= -0.05
        and str(claim.get("category"))
        in PERFORMANCE_CATEGORIES | VALUE_CATEGORIES
    ]
    worst_stage_signal = min(
        (
            _aggregate(
                [claim for claim in claims if stage in claim.get("stages", [])],
                PERFORMANCE_CATEGORIES,
            )
            for stage in range(1, 22)
        ),
        default=performance_signal,
    )
    if worst_stage_signal <= -0.20 and value_signal <= -0.05:
        level = "high"
    elif worst_stage_signal <= -0.12 or value_signal <= -0.08:
        level = "medium"
    else:
        level = "low"
    return {
        "level": level,
        "worst_stage_signal": round(worst_stage_signal, 6),
        "value_signal": round(value_signal, 6),
        "reasons": [str(claim.get("text") or "") for claim in negative_claims],
    }


def compile_forum_opinion(payload: dict[str, Any]) -> dict[str, Any]:
    claims = [row for row in payload.get("claims", []) if isinstance(row, dict)]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for claim in claims:
        if slug := str(claim.get("rider_slug") or ""):
            grouped[slug].append(claim)
    riders: dict[str, dict[str, Any]] = {}
    for slug, rider_claims in sorted(grouped.items()):
        stages = sorted({int(stage) for claim in rider_claims for stage in claim.get("stages", []) if 1 <= int(stage) <= 21})
        performance_signal = _aggregate(
            [claim for claim in rider_claims if not claim.get("stages")],
            PERFORMANCE_CATEGORIES,
        )
        value_signal = _aggregate(rider_claims, VALUE_CATEGORIES)
        riders[slug] = {
            "rider": rider_claims[-1].get("rider") or slug,
            "performance_signal": _aggregate(
                [claim for claim in rider_claims if not claim.get("stages")],
                PERFORMANCE_CATEGORIES,
            ),
            "value_signal": value_signal,
            "stage_signals": {
                str(stage): _aggregate(
                    [claim for claim in rider_claims if not claim.get("stages") or stage in claim.get("stages", [])],
                    PERFORMANCE_CATEGORIES,
                )
                for stage in stages
            },
            "claim_count": len(rider_claims),
            "model_claim_count": sum(CLAIM_KIND_WEIGHTS.get(str(claim.get("kind")), 0.0) > 0 for claim in rider_claims),
            "uncertainty": _uncertainty_assessment(
                rider_claims, performance_signal, value_signal
            ),
            "claims": rider_claims,
        }
    return {
        "schema_version": 1,
        "source": payload.get("source", {}),
        "generated_at": payload.get("generated_at"),
        "forum_opinion_share": FORUM_OPINION_SHARE,
        "chat_opinion_share": CHAT_OPINION_SHARE,
        "policy": "Performance facts/interpretations may affect stages; price/value is audit-only; guesses/humour are zero-weight.",
        "riders": riders,
        "summary": {
            "claims": len(claims), "riders": len(riders),
            "reported_facts": sum(row.get("kind") == "reported_fact" for row in claims),
            "interpretations": sum(row.get("kind") == "interpretation" for row in claims),
            "zero_weight": sum(CLAIM_KIND_WEIGHTS.get(str(row.get("kind")), 0.0) == 0.0 for row in claims),
        },
    }


def load_forum_opinion(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"riders": {}}


def forum_signal_for_stage(rider: dict[str, Any], stage: int) -> float:
    return _clamp(float(rider.get("stage_signals", {}).get(str(stage), rider.get("performance_signal", 0.0)) or 0.0))


def blend_opinion_signals(chat_signal: float, forum_signal: float) -> float:
    return _clamp(CHAT_OPINION_SHARE * _clamp(chat_signal) + FORUM_OPINION_SHARE * _clamp(forum_signal))