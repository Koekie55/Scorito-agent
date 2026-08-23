"""Refresh and export Vuelta 2026 per-stage top-20 predictions.

PCS rankings drive finishing order. The handwritten stage analysis is a capped
qualitative signal, while rider news is attached as evidence and never changes
a rank automatically. A changed PCS startlist triggers the full evidence model.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from scorito_agent.pcs.fetcher import fetch_race_startlist  # noqa: E402
from scorito_agent.pcs.parse import parse_startlist  # noqa: E402
from scorito_agent.forum_opinion import (  # noqa: E402
    FORUM_OPINION_SHARE,
    OPINION_MAX_ADJUSTMENT,
    blend_opinion_signals,
    forum_signal_for_stage,
    load_forum_opinion,
)
from scripts.project_vuelta import _course_similarity  # noqa: E402

DATA_DIR = ROOT / "data" / "scorito" / "vuelta2026"
PROJECTION_PATH = DATA_DIR / "projected_recommendation.json"
EXPERT_PATH = DATA_DIR / "qk_expert_opinion.json"
NEWS_PATH = ROOT / "data" / "rider_news" / "vuelta2026" / "latest.json"
EXPERT_CHAT_PATH = DATA_DIR / "expert_chat_intel.json"
FORUM_OPINION_PATH = DATA_DIR / "wielerflits_forum_opinion.json"
OUTPUT_JSON = DATA_DIR / "stage_top20_predictions.json"
OUTPUT_CSV = DATA_DIR / "stage_top20_predictions.csv"
TOP_N = 20
EXPERT_WEIGHT_CAP = 0.15
OPINION_MAX_ADJUSTMENT = 0.16
SCORITO_STAGE_POINTS = {
    1: 50,
    2: 44,
    3: 40,
    4: 36,
    5: 32,
    6: 30,
    7: 28,
    8: 26,
    9: 24,
    10: 22,
    11: 20,
    12: 18,
    13: 16,
    14: 14,
    15: 12,
    16: 10,
    17: 8,
    18: 6,
    19: 4,
    20: 1,
}


def _name_key(name: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", str(name))
    ascii_name = "".join(char for char in normalized if not unicodedata.combining(char))
    return tuple(sorted(re.findall(r"[a-z0-9]+", ascii_name.lower())))


def _load_json(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise RuntimeError(f"required input is missing: {path}")
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _projection_slugs(projection: dict[str, Any]) -> set[str]:
    return {str(row["rider_slug"]) for row in projection.get("riders", [])}


def _startlist_change(
    projection: dict[str, Any], live_startlist: list[dict[str, Any]]
) -> tuple[set[str], set[str]]:
    saved = _projection_slugs(projection)
    live = {str(row["rider_slug"]) for row in live_startlist}
    return live - saved, saved - live


def _stage_analysis_weight(
    stage: dict[str, Any], notes: dict[str, Any], cap: float
) -> tuple[float, str]:
    if not notes:
        return 0.0, "not_available"
    profile = str(stage.get("profile_type", "")).lower()
    finish = str(stage.get("finish_type", "")).lower()
    note_type = str(notes.get("type", "")).lower()
    stage_distance = float(stage.get("distance_km") or 0.0)
    note_distance = float(notes.get("distance_km") or 0.0)
    if stage_distance and note_distance:
        tolerance = max(15.0, stage_distance * 0.20)
        if abs(stage_distance - note_distance) > tolerance:
            return 0.0, "ignored_distance_mismatch"
    if (profile == "itt") != ("itt" in note_type):
        return 0.0, "ignored_profile_mismatch"
    if profile == "mountain" and finish == "summit" and not any(
        token in note_type for token in ("gc", "mountain", "hill", "punch")
    ):
        return 0.0, "ignored_profile_mismatch"
    return max(0.0, min(EXPERT_WEIGHT_CAP, cap)), "applied"


def _stage_selectivity(stage: dict[str, Any], notes: dict[str, Any]) -> float:
    if str(stage.get("profile_type", "")).lower() != "hilly":
        return 0.0
    vertical = float(stage.get("vertical_meters") or 0.0)
    gradient = float(stage.get("gradient_final_km") or 0.0)
    finish = str(stage.get("finish_type", "")).lower()
    note_type = str(notes.get("type", "")).lower()
    note_text = " ".join(
        str(notes.get(key) or "").lower()
        for key in ("type", "climbs", "finish", "notes")
    )
    final_50km = notes.get("final_50km") or {}
    if bool(final_50km.get("sprinter_drop_climb_last_25km")):
        selectivity = 0.78
    elif bool(final_50km.get("weak_sprinters_dropped")):
        selectivity = 0.58
    elif (
        _is_reduced_sprint_stage(stage, notes)
        and bool(final_50km.get("sprinters_retained"))
    ):
        selectivity = 0.28
    else:
        selectivity = max(0.0, min(0.75, (vertical - 900.0) / 3_000.0))
    if finish == "uphill":
        selectivity = max(selectivity, 0.52)
    if gradient >= 3.0:
        selectivity = max(selectivity, 0.55)
    if "mountain" in note_type:
        selectivity = max(selectivity, 0.78)
    elif "breakaway" in note_type:
        selectivity = max(selectivity, 0.65 if vertical >= 2_500 else 0.52)
    if "punch" in note_type:
        selectivity = max(selectivity, 0.55)
    if "gc finish" in note_type:
        selectivity = max(selectivity, 0.72)
    if "4km @ 6%" in note_text:
        selectivity = max(selectivity, 0.68)
    elif "above 10%" in note_text:
        selectivity = max(selectivity, 0.58)
    elif "climb" in note_text:
        selectivity = max(selectivity, 0.48)
    return round(min(0.82, selectivity), 3)


def _result_rows(
    rider: dict[str, Any], profile: str, notes: dict[str, Any]
) -> list[dict[str, Any]]:
    evidence = rider.get("recent_evidence", {})
    rows = list(evidence.get("contextual_results", []))
    for row in evidence.get("strongest_by_profile", {}).get(profile, []):
        enriched = dict(row)
        enriched["course_context"] = {
            "distance_km": row.get("distance_km"),
            "vertical_meters": row.get("vertical_meters"),
            "profile_score": row.get("profile_score"),
            "gradient_final_km": row.get("gradient_final_km"),
        }
        rows.append(enriched)
    for row in notes.get("pcs_result_evidence", {}).get(
        str(rider.get("rider_slug") or ""), []
    ):
        enriched = dict(row)
        enriched["course_context"] = {
            "distance_km": row.get("distance_km"),
            "vertical_meters": row.get("vertical_meters"),
            "profile_score": row.get("profile_score"),
            "gradient_final_km": row.get("gradient_final_km"),
        }
        rows.append(enriched)
    deduplicated = {}
    for row in rows:
        key = str(
            row.get("source_url")
            or (row.get("year"), row.get("race"), row.get("rank"))
        )
        deduplicated.setdefault(key, row)
    return list(deduplicated.values())


def _comparable_performance(
    stage: dict[str, Any], rider: dict[str, Any], notes: dict[str, Any]
) -> float:
    profile = str(stage.get("profile_type") or "unknown")
    values = []
    for result in _result_rows(rider, profile, notes):
        rank = result.get("rank")
        year = int(result.get("year") or 0)
        if not isinstance(rank, int) or rank <= 0 or year not in {2024, 2025, 2026}:
            continue
        similarity = _course_similarity(result, stage)
        final_50km = notes.get("final_50km") or {}
        if _is_reduced_sprint_stage(stage, notes) and bool(
            final_50km.get("sprinters_retained")
        ):
            context = result.get("course_context") or {}
            source_profile = str(result.get("profile_type") or "unknown")
            profile_similarity = {
                "flat": 1.0,
                "hilly": 0.92,
                "mountain": 0.20,
                "unknown": 0.45,
            }.get(source_profile, 0.35)
            source_finish = str(result.get("finish_type") or "unknown")
            finish_similarity = {
                "sprint": 1.0,
                "flat": 0.95,
                "uphill": 0.82,
                "technical": 0.72,
                "unknown": 0.60,
                "summit": 0.12,
            }.get(source_finish, 0.45)
            source_gradient = context.get("gradient_final_km")
            target_gradient = float(stage.get("gradient_final_km") or 0.0)
            gradient_similarity = (
                2.0 ** (-abs(float(source_gradient) - target_gradient) / 2.5)
                if isinstance(source_gradient, (int, float))
                else 0.65
            )
            total_vertical_similarity = 0.65
            source_vertical = context.get("vertical_meters")
            target_vertical = stage.get("vertical_meters")
            if isinstance(source_vertical, (int, float)) and isinstance(
                target_vertical, (int, float)
            ):
                total_vertical_similarity = (
                    min(float(source_vertical), float(target_vertical)) + 700.0
                ) / (max(float(source_vertical), float(target_vertical)) + 700.0)
            similarity = (
                0.30 * profile_similarity
                + 0.30 * finish_similarity
                + 0.25 * gradient_similarity
                + 0.10 * total_vertical_similarity
                + 0.05 * similarity
            )
        if profile == "itt":
            context = result.get("course_context") or {}
            source_vertical = context.get("vertical_meters")
            target_vertical = stage.get("vertical_meters")
            source_gradient = context.get("gradient_final_km")
            target_gradient = stage.get("gradient_final_km") or 0.0
            if isinstance(source_vertical, (int, float)) and isinstance(
                target_vertical, (int, float)
            ):
                vertical_ratio = (min(source_vertical, target_vertical) + 80.0) / (
                    max(source_vertical, target_vertical) + 80.0
                )
                similarity *= vertical_ratio
            if isinstance(source_gradient, (int, float)):
                similarity *= 2.0 ** (-abs(float(source_gradient) - float(target_gradient)) / 2.0)
        recency = {2026: 1.0, 2025: 0.58, 2024: 0.32}[year]
        placing = 1.0 / rank**0.55
        field = result.get("field_strength")
        if not isinstance(field, (int, float)):
            quality = result.get("startlist_quality_score")
            field = (
                min(1.0, (float(quality) / 1_600.0) ** 0.5)
                if isinstance(quality, (int, float)) and quality > 0
                else 0.30
            )
        field = max(0.30, float(field))
        values.append(recency * placing * field * similarity)
    return round(min(1.0, sum(sorted(values, reverse=True)[:6]) / 1.35), 4)


def _selective_result_score(rider: dict[str, Any]) -> float:
    values = []
    for result in rider.get("recent_evidence", {}).get("contextual_results", []):
        if result.get("profile_type") not in {"hilly", "mountain"}:
            continue
        context = result.get("course_context") or {}
        vertical = context.get("vertical_meters")
        rank = result.get("rank")
        year = int(result.get("year") or 0)
        if not isinstance(vertical, (int, float)) or vertical < 1_400:
            continue
        if not isinstance(rank, int) or rank > 20 or year not in {2024, 2025, 2026}:
            continue
        recency = {2026: 0.60, 2025: 0.27, 2024: 0.13}[year]
        placing = 1.0 / rank**0.35
        field = max(0.25, float(result.get("field_strength") or 0.25))
        difficulty = min(1.15, float(vertical) / 2_600.0)
        values.append(recency * placing * field * difficulty)
    return round(min(1.0, sum(sorted(values, reverse=True)[:8]) / 0.90), 4)


def _fast_finish_score(stage: dict[str, Any], rider: dict[str, Any]) -> float:
    values = []
    target_gradient = float(stage.get("gradient_final_km") or 0.0)
    for result in rider.get("recent_evidence", {}).get("contextual_results", []):
        rank = result.get("rank")
        year = int(result.get("year") or 0)
        if not isinstance(rank, int) or rank > 20 or year not in {2024, 2025, 2026}:
            continue
        if result.get("profile_type") not in {"flat", "hilly"}:
            continue
        finish = str(result.get("finish_type") or "unknown")
        context = result.get("course_context") or {}
        gradient = context.get("gradient_final_km")
        if finish not in {"flat", "sprint", "uphill"}:
            continue
        if finish == "uphill" and isinstance(gradient, (int, float)) and gradient > 5.0:
            continue
        finish_weight = {"sprint": 1.0, "flat": 0.95, "uphill": 0.90}[finish]
        gradient_weight = (
            2.0 ** (-abs(float(gradient) - target_gradient) / 3.0)
            if isinstance(gradient, (int, float))
            else 0.70
        )
        recency = {2026: 1.0, 2025: 0.58, 2024: 0.32}[year]
        placing = 1.0 / rank**0.45
        field = max(0.30, float(result.get("field_strength") or 0.30))
        values.append(recency * placing * field * finish_weight * gradient_weight)
    return round(min(1.0, sum(sorted(values, reverse=True)[:8]) / 1.20), 4)


def _sprint_survival_score(rider: dict[str, Any]) -> float:
    signals = rider.get("signals", {})
    recent = rider.get("recent_evidence", {}).get("profile_strength", {})
    score = (
        0.50 * _selective_result_score(rider)
        + 0.22 * min(1.0, float(recent.get("hilly", 0.0)) / 1.10)
        + 0.15 * min(1.0, float(recent.get("mountain", 0.0)) / 0.75)
        + 0.08 * float(signals.get("classic", 0.0))
        + 0.05 * float(signals.get("previous_vuelta", 0.0))
    )
    return round(max(0.0, min(1.0, score)), 4)


def _survival_factor(selectivity: float, survival: float) -> float:
    factor = (1.0 - selectivity) + selectivity * survival**2
    if selectivity >= 0.70 and survival < 0.50:
        factor *= 0.55
    return factor


def _mountain_finish_factor(
    stage: dict[str, Any], rider: dict[str, Any]
) -> tuple[float, float]:
    if str(stage.get("profile_type", "")).lower() != "mountain":
        return 1.0, 1.0

    signals = rider.get("signals", {})
    ranking_credibility = max(
        float(signals.get("gc", 0.0)),
        float(signals.get("climb", 0.0)),
    )
    mountain_results = {
        str(
            result.get("source_url")
            or (result.get("year"), result.get("race"), result.get("rank"))
        )
        for result in rider.get("recent_evidence", {}).get("contextual_results", [])
        if result.get("profile_type") == "mountain"
        and int(result.get("year") or 0) in {2024, 2025, 2026}
        and isinstance(result.get("rank"), int)
        and int(result["rank"]) <= 20
    }
    result_credibility = min(1.0, len(mountain_results) / 3.0)
    credibility = max(ranking_credibility, result_credibility)
    factor = 0.08 + 0.92 * credibility**0.55
    if (
        str(stage.get("finish_type", "")).lower() == "summit"
        or float(stage.get("vertical_meters") or 0.0) >= 4_000
    ):
        factor **= 1.15
    return round(factor, 4), round(credibility, 4)


def _hilly_attrition_factor(
    stage: dict[str, Any], notes: dict[str, Any], survival: float
) -> float:
    if str(stage.get("profile_type", "")).lower() != "hilly":
        return 1.0

    final_50km = notes.get("final_50km") or {}
    note_type = str(notes.get("type", "")).lower()
    if bool(final_50km.get("sprinter_drop_climb_last_25km")):
        return 0.08 if survival < 0.40 else (0.65 if survival < 0.60 else 1.0)
    if bool(final_50km.get("weak_sprinters_dropped")) and survival < 0.40:
        return 0.35
    if "gc finish" in note_type and survival < 0.40:
        return 0.25
    if (
        str(stage.get("finish_type", "")).lower() == "uphill"
        and float(stage.get("vertical_meters") or 0.0) >= 2_500
        and survival < 0.35
    ):
        return 0.55
    return 1.0


def _is_reduced_sprint_stage(stage: dict[str, Any], notes: dict[str, Any]) -> bool:
    note_type = str(notes.get("type", "")).lower()
    gradient = float(stage.get("gradient_final_km") or 0.0)
    final_50km = notes.get("final_50km") or {}
    return (
        str(stage.get("profile_type", "")).lower() == "hilly"
        and ("sprint" in note_type or bool(final_50km.get("sprinters_retained")))
        and gradient <= 4.5
    )


def _conversion_factor(
    stage: dict[str, Any],
    notes: dict[str, Any],
    rider: dict[str, Any],
    stage_expert_signal: float,
) -> tuple[float, str]:
    """Return the finish-conversion multiplier and the rule that produced it.

    This multiplier is applied to the combined stage score and is frequently the
    single largest term in it: on a reduced bunch sprint a GC rider without a
    sprint signal is cut by 92%, which is enough on its own to move a rider from
    the top of the field to outside the published top 20.

    The returned reason string is a stable identifier for the branch taken, so
    the published output records *why* a rider was up- or down-weighted rather
    than only the final score. Behaviour is unchanged from the inline version
    this replaced; only the reason label is new.
    """
    profile = str(stage.get("profile_type", "")).lower()
    signals = rider.get("signals", {})
    pcs_sprint = float(signals.get("sprint", 0.0))

    if profile == "flat":
        flat_strength = float(
            rider.get("recent_evidence", {})
            .get("profile_strength", {})
            .get("flat", 0.0)
        )
        if pcs_sprint <= 0.0 and flat_strength < 0.35:
            return 0.18, "flat_no_sprint_signal_weak_flat_evidence"
        if pcs_sprint < 0.02 and flat_strength < 0.55:
            return 0.55, "flat_marginal_sprint_signal"
        return 1.0, "none"

    if _is_reduced_sprint_stage(stage, notes):
        pcs_gc = float(signals.get("gc", 0.0))
        pcs_classic = float(signals.get("classic", 0.0))
        fast_finish = _fast_finish_score(stage, rider)
        supported_fast_finisher = (
            pcs_sprint >= 0.02 or pcs_classic >= 0.05 or stage_expert_signal >= 0.50
        )
        if pcs_gc >= 0.20 and pcs_sprint < 0.02:
            return 0.08, "reduced_sprint_gc_rider_without_sprint_signal"
        if not supported_fast_finisher:
            if pcs_gc >= 0.08 or fast_finish < 0.70:
                return 0.25, "reduced_sprint_unsupported_finisher"
            return 0.55, "reduced_sprint_unsupported_but_fast_finish_evidence"
        if (
            pcs_sprint <= 0.0
            and pcs_classic < 0.05
            and _selective_result_score(rider) < 0.35
        ):
            return 0.25, "reduced_sprint_thin_selective_results"
        if pcs_sprint < 0.02 and _selective_result_score(rider) < 0.50:
            return 0.55, "reduced_sprint_marginal_selective_results"
        return 1.0, "none"

    return 1.0, "none"


def _pcs_outcome_score(
    stage: dict[str, Any],
    rider: dict[str, Any],
    base_score: float,
    notes: dict[str, Any],
) -> float:
    signals = rider.get("signals", {})
    recent = rider.get("recent_evidence", {}).get("profile_strength", {})
    signal = lambda name: float(signals.get(name, 0.0))
    recency = lambda name: min(1.0, float(recent.get(name, 0.0)))
    profile = str(stage.get("profile_type", "")).lower()
    finish = str(stage.get("finish_type", "")).lower()
    comparable = _comparable_performance(stage, rider, notes)

    if profile == "itt":
        return (
            0.72 * comparable
            + 0.10 * max(signal("tt"), signal("prologue"))
            + 0.10 * min(1.0, recency("itt") / 0.85)
            + 0.05 * signal("form")
            + 0.03 * base_score
        )
    if profile == "mountain":
        return (
            0.42 * comparable
            + 0.20 * signal("climb")
            + 0.14 * signal("gc")
            + 0.12 * min(1.0, recency("mountain"))
            + 0.07 * signal("form")
            + 0.05 * base_score
        )
    if profile == "flat":
        return (
            0.46 * comparable
            + 0.24 * signal("sprint")
            + 0.16 * min(1.0, recency("flat"))
            + 0.08 * signal("form")
            + 0.06 * base_score
        )
    if _is_reduced_sprint_stage(stage, notes):
        fast_finish = _fast_finish_score(stage, rider)
        return (
            0.42 * fast_finish
            + 0.18 * signal("sprint")
            + 0.12 * comparable
            + 0.10 * _selective_result_score(rider)
            + 0.08 * min(1.0, recency("hilly"))
            + 0.05 * signal("classic")
            + 0.03 * signal("form")
            + 0.02 * base_score
        )
    if finish == "uphill" or float(stage.get("gradient_final_km") or 0.0) >= 3.0:
        return (
            0.40 * comparable
            + 0.20 * signal("classic")
            + 0.14 * signal("climb")
            + 0.12 * min(1.0, recency("hilly"))
            + 0.08 * signal("form")
            + 0.06 * base_score
        )
    return (
        0.38 * comparable
        + 0.20 * signal("sprint")
        + 0.14 * _selective_result_score(rider)
        + 0.10 * signal("classic")
        + 0.10 * min(1.0, recency("hilly"))
        + 0.05 * signal("form")
        + 0.03 * base_score
    )


def _expert_score(
    note_type: str,
    rider: dict[str, Any],
    stage: dict[str, Any],
    notes: dict[str, Any],
) -> float:
    kind = note_type.lower()
    signals = rider.get("signals", {})
    recent = rider.get("recent_evidence", {}).get("profile_strength", {})
    value = lambda name: float(signals.get(name, 0.0))
    if "itt" in kind:
        return 0.72 * _comparable_performance(stage, rider, notes) + 0.28 * min(
            1.0, float(recent.get("itt", 0.0)) / 0.85
        )
    if "gc" in kind or "mountain" in kind:
        return 0.55 * value("gc") + 0.35 * value("climb") + 0.10 * min(
            1.0, float(recent.get("mountain", 0.0))
        )
    if "sprint" in kind:
        sprint = 0.65 * value("sprint") + 0.35 * min(
            1.0, float(recent.get("flat", 0.0))
        )
        if "punch" in kind or "hill" in kind:
            return 0.60 * sprint + 0.25 * value("classic") + 0.15 * min(
                1.0, float(recent.get("hilly", 0.0))
            )
        return sprint
    if "punch" in kind or "hill" in kind:
        return 0.55 * value("classic") + 0.25 * value("climb") + 0.20 * min(
            1.0, float(recent.get("hilly", 0.0))
        )
    return 0.50 * value("overall") + 0.50 * value("form")


def _news_by_slug(news: dict[str, Any]) -> dict[str, dict[str, Any]]:
    priority = {
        "review_selection_and_lineup": 3,
        "verify_before_downgrading": 2,
        "lineup_context_only": 1,
    }
    selected: dict[str, dict[str, Any]] = {}
    for row in news.get("selection_impacts", []):
        slug = str(row.get("rider_slug") or "")
        if not slug:
            continue
        current = selected.get(slug)
        key = (priority.get(str(row.get("decision_hint")), 0), float(row.get("score") or 0.0))
        current_key = (
            priority.get(str(current.get("decision_hint")), 0),
            float(current.get("score") or 0.0),
        ) if current else (-1, -1.0)
        if key > current_key:
            selected[slug] = row
    return selected


def _expert_chat_by_key() -> dict[tuple[str, ...], dict[str, Any]]:
    if not EXPERT_CHAT_PATH.exists():
        return {}
    payload = json.loads(EXPERT_CHAT_PATH.read_text(encoding="utf-8"))
    return {
        _name_key(str(row.get("name") or key)): row
        for key, row in payload.get("riders", {}).items()
    }


def build_stage_top20(
    projection: dict[str, Any], expert: dict[str, Any], news: dict[str, Any]
) -> dict[str, Any]:
    participants = projection.get("riders", [])
    if len(participants) < TOP_N:
        raise RuntimeError(f"projection has only {len(participants)} participants")
    riders = {str(row["rider_slug"]): row for row in participants}
    expert_chat = _expert_chat_by_key()
    forum_opinion = load_forum_opinion(FORUM_OPINION_PATH)
    forum_riders = forum_opinion.get("riders", {})
    for slug, rider in riders.items():
        rider["_expert_chat"] = expert_chat.get(_name_key(rider["rider"]), {})
        rider["_forum_opinion"] = forum_riders.get(slug, {})
    if len(riders) != len(participants):
        raise RuntimeError("projection contains duplicate rider slugs")

    stages = {int(row["stage_no"]): row for row in projection.get("stages", [])}
    rankings = projection.get("stage_rankings", {})
    notes_by_stage = expert.get("stage_breakdown", {})
    news_index = _news_by_slug(news)
    expert_cap = min(EXPERT_WEIGHT_CAP, float(expert.get("weight_applied", 0.0)))
    output_stages: list[dict[str, Any]] = []

    for stage_no in range(1, 22):
        stage = stages.get(stage_no)
        rows = rankings.get(str(stage_no), rankings.get(stage_no))
        if stage is None or not isinstance(rows, list) or len(rows) != len(riders):
            raise RuntimeError(f"stage {stage_no} projection is incomplete")
        row_slugs = {str(row["rider_slug"]) for row in rows}
        if row_slugs != set(riders):
            raise RuntimeError(f"stage {stage_no} participant set differs from PCS projection")

        notes = notes_by_stage.get(str(stage_no), {})
        expert_weight, analysis_status = _stage_analysis_weight(stage, notes, expert_cap)
        selectivity = _stage_selectivity(stage, notes)
        survival_scores = {
            slug: _sprint_survival_score(riders[slug]) for slug in riders
        }
        maximum_base = max(float(row.get("score") or 0.0) for row in rows) or 1.0
        normalized_base = {
            str(row["rider_slug"]): float(row.get("score") or 0.0) / maximum_base
            for row in rows
        }
        outcome_scores = {
            slug: _pcs_outcome_score(
                stage, riders[slug], normalized_base[slug], notes
            )
            for slug in riders
        }
        maximum_outcome = max(outcome_scores.values(), default=0.0) or 1.0
        expert_scores = {
            slug: _expert_score(
                str(notes.get("type", "")), riders[slug], stage, notes
            )
            for slug in riders
        }
        maximum_expert = max(expert_scores.values(), default=0.0) or 1.0
        scored = []
        for row in rows:
            slug = str(row["rider_slug"])
            pcs_score = normalized_base[slug]
            outcome_score = outcome_scores[slug] / maximum_outcome
            expert_score = expert_scores[slug] / maximum_expert
            profile = str(stage.get("profile_type", "")).lower()
            base_weight = 0.05 if profile == "flat" else (0.15 if profile == "hilly" else 0.25)
            pre_survival_score = (
                base_weight * pcs_score
                + (1.0 - base_weight - expert_weight) * outcome_score
                + expert_weight * expert_score
            )
            survival = survival_scores[slug]
            survival_factor = _survival_factor(selectivity, survival)
            mountain_factor, mountain_credibility = _mountain_finish_factor(
                stage, riders[slug]
            )
            hilly_attrition_factor = _hilly_attrition_factor(stage, notes, survival)
            stage_expert_signal = float(
                notes.get("rider_signals", {}).get(slug, 0.0) or 0.0
            )
            conversion_factor, conversion_reason = _conversion_factor(
                stage, notes, riders[slug], stage_expert_signal
            )
            chat = riders[slug].get("_expert_chat", {})
            chat_signal = float(
                chat.get("stage_signals", {}).get(
                    str(stage_no), chat.get("signal", chat.get("bias", 0.0))
                )
                or 0.0
            )
            forum_signal = forum_signal_for_stage(
                riders[slug].get("_forum_opinion", {}), stage_no
            )
            opinion_signal = blend_opinion_signals(chat_signal, forum_signal)
            chat_multiplier = 1.0 + OPINION_MAX_ADJUSTMENT * opinion_signal
            stage_expert_multiplier = 1.0 + expert_weight * max(
                -1.0, min(1.0, stage_expert_signal)
            )
            news_row = news_index.get(slug)
            news_multiplier = 1.0
            if news_row and (
                news_row.get("decision_hint") == "review_selection_and_lineup"
                and news_row.get("impact") == "negative"
            ):
                news_multiplier = 0.10
            combined = (
                pre_survival_score
                * survival_factor
                * mountain_factor
                * hilly_attrition_factor
                * conversion_factor
                * chat_multiplier
                * stage_expert_multiplier
                * news_multiplier
            )
            scored.append(
                (
                    combined,
                    row,
                    news_row,
                    survival,
                    survival_factor,
                    mountain_credibility,
                    mountain_factor,
                    hilly_attrition_factor,
                    conversion_factor,
                    conversion_reason,
                    chat_signal,
                    forum_signal,
                    opinion_signal,
                    stage_expert_signal,
                    news_multiplier,
                )
            )
        scored.sort(key=lambda item: (-item[0], str(item[1].get("rider", ""))))

        predictions = []
        for rank, (
            combined,
            row,
            news_row,
            survival,
            survival_factor,
            mountain_credibility,
            mountain_factor,
            hilly_attrition_factor,
            conversion_factor,
            conversion_reason,
            chat_signal,
            forum_signal,
            opinion_signal,
            stage_expert_signal,
            news_multiplier,
        ) in enumerate(scored[:TOP_N], start=1):
            slug = str(row["rider_slug"])
            predictions.append(
                {
                    "predicted_finish": rank,
                    "scorito_stage_points": SCORITO_STAGE_POINTS[rank],
                    "rider": row["rider"],
                    "rider_slug": row["rider_slug"],
                    "team": riders[str(row["rider_slug"])].get("team"),
                    "combined_score": round(combined, 6),
                    "pcs_model_rank": int(row["rank"]),
                    "stage_selectivity": selectivity,
                    "sprint_survival_score": survival,
                    "selective_result_score": _selective_result_score(riders[slug]),
                    "fast_finish_score": _fast_finish_score(stage, riders[slug]),
                    "sprint_survival_factor": round(survival_factor, 4),
                    "mountain_credibility": mountain_credibility,
                    "mountain_finish_factor": mountain_factor,
                    "hilly_attrition_factor": hilly_attrition_factor,
                    "conversion_factor": round(conversion_factor, 4),
                    "conversion_reason": conversion_reason,
                    "expert_chat_signal": round(chat_signal, 4),
                    "wielerflits_forum_signal": round(forum_signal, 4),
                    "forum_opinion_share": FORUM_OPINION_SHARE,
            "opinion_max_adjustment": OPINION_MAX_ADJUSTMENT,
                    "blended_opinion_signal": round(opinion_signal, 4),
                    "expert_chat_multiplier": round(
                        1.0 + OPINION_MAX_ADJUSTMENT * opinion_signal, 4
                    ),
                    "opinion_max_adjustment": OPINION_MAX_ADJUSTMENT,
                    "stage_expert_signal": round(stage_expert_signal, 4),
                    "stage_expert_multiplier": round(
                        1.0 + expert_weight * stage_expert_signal, 4
                    ),
                    "news_multiplier": news_multiplier,
                    "pcs_model_score": row.get("score"),
                    "expected_finish_band": row.get("expected_finish_band"),
                    "confidence": row.get("confidence"),
                    "uncertainty": row.get("uncertainty"),
                    "role_assumption": row.get("role_assumption"),
                    "evidence": row.get("evidence"),
                    "news": {
                        key: news_row.get(key)
                        for key in ("impact", "verification", "decision_hint", "title", "url", "published_at")
                    } if news_row else None,
                    "news_rank_adjustment": 0,
                }
            )
        if len(predictions) != TOP_N or len({row["rider_slug"] for row in predictions}) != TOP_N:
            raise RuntimeError(f"stage {stage_no} does not contain {TOP_N} unique predictions")
        output_stages.append(
            {
                **stage,
                "expert_analysis": notes,
                "expert_weight": expert_weight,
                "expert_status": analysis_status,
                "stage_selectivity": selectivity,
                "top_20": predictions,
            }
        )

    return {
        "schema_version": 1,
        "status": "forward_projection",
        "generated_at": datetime.now(UTC).isoformat(),
        "race": "Vuelta a Espana 2026",
        "known_pcs_participants": len(riders),
        "prediction_count_per_stage": TOP_N,
        "stage_points_by_finish": SCORITO_STAGE_POINTS,
        "method": (
            "PCS comparable performances, specialty rankings, form and exact course evidence; "
            "mountain stages require PCS GC/climb signals or recent mountain top-20 evidence; "
            "corrected handwritten analysis is bounded; expert chat and WielerFlits forum opinion are blended 70/30 inside one 16% cap; verified negative news "
            "can reduce availability. Scorito rider ratings are excluded from ordering."
        ),
        "uncertainty": (
            "The PCS startlist is provisional and incomplete. Predicted places are model estimates, "
            "not guarantees; breakaways, tactics, weather, crashes and final selections can change them."
        ),
        "sources": {
            "projection_generated_at": projection.get("generated_at"),
            "projection_model_version": projection.get("model_version"),
            "pcs_startlist": projection.get("sources", {}).get("startlist"),
            "expert_generated_at": expert.get("generated_at"),
            "news_generated_at": news.get("generated_at"),
            "forum_generated_at": forum_opinion.get("generated_at"),
            "forum_opinion_share": FORUM_OPINION_SHARE,
            "opinion_max_adjustment": OPINION_MAX_ADJUSTMENT,
            "market_snapshot_time": (news.get("market_snapshot") or {}).get(
                "snapshot_time"
            ),
            "input_hashes": {
                "projection": _sha256(PROJECTION_PATH),
                "expert": _sha256(EXPERT_PATH),
                "news": _sha256(NEWS_PATH),
                "forum_opinion": _sha256(FORUM_OPINION_PATH),
            },
        },
        "stages": output_stages,
    }


def _write_csv(report: dict[str, Any], path: Path) -> None:
    fieldnames = [
        "stage_no", "date", "route", "profile_type", "finish_type", "distance_km",
        "vertical_meters", "expert_type", "expert_status", "expert_weight",
        "predicted_finish", "scorito_stage_points", "rider", "rider_slug", "team", "combined_score",
        "pcs_model_rank", "mountain_credibility", "mountain_finish_factor",
        "hilly_attrition_factor", "conversion_factor", "conversion_reason",
        "expert_chat_signal", "wielerflits_forum_signal",
        "forum_opinion_share", "blended_opinion_signal", "expected_finish_band", "confidence", "uncertainty",
        "role_assumption", "news_decision", "news_verification", "news_title", "evidence",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for stage in report["stages"]:
            route = f"{stage.get('departure') or ''} -> {stage.get('arrival') or ''}"
            for prediction in stage["top_20"]:
                news = prediction.get("news") or {}
                writer.writerow(
                    {
                        "stage_no": stage["stage_no"],
                        "date": stage.get("date"),
                        "route": route,
                        "profile_type": stage.get("profile_type"),
                        "finish_type": stage.get("finish_type"),
                        "distance_km": stage.get("distance_km"),
                        "vertical_meters": stage.get("vertical_meters"),
                        "expert_type": stage.get("expert_analysis", {}).get("type"),
                        "expert_status": stage["expert_status"],
                        "expert_weight": stage["expert_weight"],
                        "predicted_finish": prediction["predicted_finish"],
                        "scorito_stage_points": prediction["scorito_stage_points"],
                        "rider": prediction["rider"],
                        "rider_slug": prediction["rider_slug"],
                        "team": prediction["team"],
                        "combined_score": prediction["combined_score"],
                        "pcs_model_rank": prediction["pcs_model_rank"],
                        "mountain_credibility": prediction["mountain_credibility"],
                        "mountain_finish_factor": prediction["mountain_finish_factor"],
                        "hilly_attrition_factor": prediction["hilly_attrition_factor"],
                        "conversion_factor": prediction["conversion_factor"],
                        "conversion_reason": prediction["conversion_reason"],
                        "expert_chat_signal": prediction["expert_chat_signal"],
                        "wielerflits_forum_signal": prediction["wielerflits_forum_signal"],
                        "forum_opinion_share": prediction["forum_opinion_share"],
                        "blended_opinion_signal": prediction["blended_opinion_signal"],
                        "expected_finish_band": "-".join(map(str, prediction.get("expected_finish_band") or [])),
                        "confidence": prediction.get("confidence"),
                        "uncertainty": prediction.get("uncertainty"),
                        "role_assumption": prediction.get("role_assumption"),
                        "news_decision": news.get("decision_hint"),
                        "news_verification": news.get("verification"),
                        "news_title": news.get("title"),
                        "evidence": prediction.get("evidence"),
                    }
                )


def refresh(*, check_pcs: bool, force_model_refresh: bool) -> dict[str, Any]:
    projection = _load_json(PROJECTION_PATH)
    if check_pcs or force_model_refresh:
        live_startlist = parse_startlist(
            fetch_race_startlist("vuelta-a-espana", 2026, cache=False)
        )
        added, removed = _startlist_change(projection, live_startlist)
        if added or removed or force_model_refresh:
            print(
                f"PCS startlist changed: {len(added)} added, {len(removed)} removed; "
                "rebuilding the full projection"
            )
            from scripts.project_vuelta import main as rebuild_projection

            rebuild_projection()
            projection = _load_json(PROJECTION_PATH)
            remaining_added, remaining_removed = _startlist_change(projection, live_startlist)
            if remaining_added or remaining_removed:
                raise RuntimeError("rebuilt projection still differs from the checked PCS startlist")
        else:
            print(f"PCS startlist unchanged: {len(live_startlist)} provisional starters")

    expert = _load_json(EXPERT_PATH)
    news = _load_json(NEWS_PATH, required=False)
    report = build_stage_top20(projection, expert, news)
    OUTPUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(report, OUTPUT_CSV)
    print(
        f"Wrote {len(report['stages'])} stages x {TOP_N} predictions for "
        f"{report['known_pcs_participants']} PCS participants"
    )
    print(f"JSON: {OUTPUT_JSON}")
    print(f"CSV: {OUTPUT_CSV}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-pcs-check",
        action="store_true",
        help="Regenerate from saved inputs without checking for PCS startlist changes.",
    )
    parser.add_argument(
        "--force-model-refresh",
        action="store_true",
        help="Rebuild the full PCS evidence model even when the startlist is unchanged.",
    )
    args = parser.parse_args()
    refresh(check_pcs=not args.skip_pcs_check, force_model_refresh=args.force_model_refresh)


if __name__ == "__main__":
    main()












