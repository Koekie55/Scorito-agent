"""Build a live-price Vuelta squad and all stage lineups in one CSV.

Prices, riders, teams, stages, and Scorito quality ratings come from the public
market 310 snapshot. Expected stage and classification values come from the
separately labelled PCS projection; they remain indicative while the startlist
is provisional. The active plan is the legal unconstrained live-price optimum;
the forced-four-UAE optimum is retained only as a comparator.

An optional qualitative "QK" expert-opinion signal (sourced from the user's own
Teams self-chat, when it can be retrieved and transcribed) can be blended into
the individual stage score, capped at a small maximum weight so it augments
rather than overrides the objective PCS field/course model and live market
qualities. Incompatible stage numbers, distances, or profiles are rejected per
stage rather than blended. The completed rider-news digest is attached as
selection evidence but follows its own no-automatic-upgrade policy.

Uncertain teammate bonuses do not drive lineup selection. The CSV reports only
conditional red-jersey and projected-stage-winner upside, separately from the
base individual score.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import unicodedata
from collections.abc import Mapping
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scorito_agent.scorito import (  # noqa: E402
    CAPABILITY_AUDIT_FIELDNAMES,
    SprintAssessment,
    capability_audit_fields,
    joint_enrolled_squad,
    load_snapshot,
    quality_relevance,
    validated_sprint_assessment_from_serialized,
)
from scorito_agent.scorito.optimizer import SquadPlan, StageLineup  # noqa: E402
from scorito_agent.forum_opinion import (  # noqa: E402
    OPINION_MAX_ADJUSTMENT,
    load_forum_opinion,
)

from scorito_agent.expert_chat import (  # noqa: E402
    CONSUMER_MAX_ADJUSTMENT,
    apply_signal,
    name_key as intel_name_key,
    signals_by_rider_stage,
)

MARKET_ID = 310
SLUG = "vuelta2026"
SQUAD_SIZE = 20
LINEUP_SIZE = 9
MAX_RIDERS_PER_TEAM = 4
UAE_TEAM_NAME = "UAE Team Emirates - XRG"
GC_TEAM_BONUS = 8.0
STAGE_WIN_TEAM_BONUS = 10.0
QUALITY_STAGE_WEIGHT = 0.0
CYCLINGORACLE_CLASSIFICATION_WEIGHT = 0.12
SCORITO_QUALITY_WEIGHT = 0.45
MODEL_QUALITY_NAMES = {
    0: "gc",
    1: "climb",
    2: "time_trial",
    3: "sprint",
    4: "punch",
    5: "hill",
    6: "cobbles",
}
DATA_DIR = ROOT / "data" / "scorito" / SLUG
PROJECTION_PATH = DATA_DIR / "projected_recommendation.json"
NEWS_PATH = ROOT / "data" / "rider_news" / SLUG / "latest.json"
EXPERT_CHAT_PATH = DATA_DIR / "expert_chat_intel.json"
FORUM_OPINION_PATH = DATA_DIR / "wielerflits_forum_opinion.json"
CYCLINGORACLE_PATH = ROOT / "data" / "cyclingoracle" / "vuelta2026_predictions.jsonl"
STAGE_TOP20_PATH = DATA_DIR / "stage_top20_predictions.json"
OUTPUT_PATH = DATA_DIR / "vuelta2026_live_prices_team_lineups.csv"

_TRANSLITERATION = str.maketrans(
    {
        "ø": "o",
        "Ø": "O",
        "æ": "ae",
        "Æ": "Ae",
        "ł": "l",
        "Ł": "L",
        "đ": "d",
        "Đ": "D",
        "ß": "ss",
    }
)

CSV_FIELDS = [
    "record_type",
    "scenario",
    "selected_scenario",
    "scenario_squad",
    "scenario_total_price_m",
    "scenario_projected_objective",
    "scenario_objective_gap",
    "scenario_uae_count",
    "snapshot_at_utc",
    "projection_at_utc",
    "market_id",
    "market_name",
    "price_source",
    "plan_source",
    "value_source",
    "cyclingoracle_source",
    "stage_analysis_source",
    "news_source",
    "expert_chat_source",
    "expert_chat_signal_avg",
    "forum_uncertainty_level",
    "forum_value_signal",
    "forum_worst_stage_signal",
    "forum_uncertainty_reasons",
    "budget",
    "budget_m",
    "squad_total_price",
    "squad_total_price_m",
    "budget_remaining",
    "budget_remaining_m",
    "uae_riders",
    "uae_team_bonus_assumption",
    "conditional_team_bonus_upside_points",
    "projected_individual_stage_points",
    "projected_team_bonus_points",
    "projected_enrolled_stage_points",
    "projected_classification_jersey_points",
    "projected_objective",
    "stage_no",
    "stage_date",
    "route",
    "profile_type",
    "finish_type",
    "distance_km",
    "projected_stage_winner_team",
    "uae_team_bonus_per_rider",
    "captain",
    "captain_projected_points",
    "lineup",
    "lineup_live_prices_m",
    "lineup_model_projected_points",
    "lineup_expert_chat_signals",
    "lineup_individual_projected_points",
    "lineup_team_bonus_points",
    "lineup_projected_points",
    "ideal_market_lineup",
    "projected_lineup_total",
    "stage_analysis_weight",
    "stage_analysis_status",
    "rider_id",
    "event_rider_id",
    "rider",
    "team",
    "role",
    "live_price",
    "live_price_m",
    "selected_squad",
    "quality_gc",
    "quality_climb",
    "quality_time_trial",
    "quality_sprint",
    "quality_punch",
    "quality_hill",
    "quality_cobbles",
    "rating_gc",
    "rating_climb",
    "rating_time_trial",
    "rating_sprint",
    "rating_punch",
    "rating_hill",
    "rating_cobbles",
    "projected_model_stage_potential",
    "projected_individual_stage_potential",
    "projected_team_bonus_potential",
    "conditional_team_bonus_upside_potential",
    "projected_stage_potential",
    "classification_jersey_value_raw",
    "cyclingoracle_classification_probability",
    "classification_jersey_value",
    "season_proxy_points",
    "value_points_per_m_live_price",
    "projected_overall_rank",
    "live_value_rank",
    "sprint_option",
    *CAPABILITY_AUDIT_FIELDNAMES,
    "news_impact",
    "news_verification",
    "news_decision",
    "news_evidence",
    "field_course_recency_score",
    "field_course_context_results",
    "evidence",
    "uncertainty",
]


def _read_content(path: Path) -> Any:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and "Content" in data:
        return data["Content"]
    return data


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.translate(_TRANSLITERATION))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return "-".join(re.findall(r"[a-z0-9]+", ascii_value))


def _tokens(value: str) -> frozenset[str]:
    return frozenset(_slug(value).split("-"))


def _load_cyclingoracle_classifications(snapshot) -> tuple[dict[int, dict[str, float]], str]:
    if not CYCLINGORACLE_PATH.exists():
        return {}, "not available"

    ids_by_tokens: dict[frozenset[str], list[int]] = {}
    for rider in snapshot.riders:
        ids_by_tokens.setdefault(_tokens(rider.name), []).append(rider.rider_id)

    probabilities: dict[int, dict[str, float]] = {}
    rows = 0
    unmatched = 0
    for line in CYCLINGORACLE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows += 1
        prediction = json.loads(line)
        source_url = str(prediction.get("source_url") or "")
        classification = "youth" if "jongeren" in source_url else "points" if "punten" in source_url else ""
        rider_ids = ids_by_tokens.get(_tokens(str(prediction.get("rider_name") or "")), [])
        if not classification or len(rider_ids) != 1:
            unmatched += 1
            continue
        probability = max(0.0, min(1.0, float(prediction.get("win_probability") or 0.0)))
        rider_probabilities = probabilities.setdefault(rider_ids[0], {})
        rider_probabilities[classification] = max(
            probability, rider_probabilities.get(classification, 0.0)
        )

    timestamp = datetime.fromtimestamp(CYCLINGORACLE_PATH.stat().st_mtime, tz=UTC).isoformat()
    source = f"{CYCLINGORACLE_PATH.name} ({timestamp}; {rows} rows; {unmatched} unmatched)"
    return probabilities, source

def _match_projection_riders(snapshot, projected_rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    by_slug = {row["rider_slug"]: row for row in projected_rows}
    if len(by_slug) != len(projected_rows):
        raise RuntimeError("projection contains duplicate rider slugs")

    matched: dict[int, dict[str, Any]] = {}
    used: set[str] = set()
    for rider in snapshot.riders:
        direct = by_slug.get(_slug(rider.name))
        if direct is not None and direct["rider_slug"] not in used:
            row = direct
        else:
            live_tokens = _tokens(rider.name)
            candidates: list[tuple[int, dict[str, Any]]] = []
            for candidate in projected_rows:
                candidate_slug = candidate["rider_slug"]
                if candidate_slug in used:
                    continue
                projected_tokens = _tokens(candidate["rider"])
                if projected_tokens == live_tokens:
                    candidates.append((3, candidate))
                elif projected_tokens <= live_tokens or live_tokens <= projected_tokens:
                    candidates.append((2, candidate))
                elif _slug(rider.name).endswith(candidate_slug):
                    candidates.append((1, candidate))
            if not candidates:
                continue  # priced rider absent from the PCS provisional startlist
            best_score = max(score for score, _candidate in candidates)
            best = [candidate for score, candidate in candidates if score == best_score]
            if len(best) != 1:
                names = ", ".join(candidate["rider"] for candidate in best)
                raise RuntimeError(f"ambiguous PCS projection match for {rider.name!r}: {names}")
            row = best[0]
        matched[rider.rider_id] = row
        used.add(row["rider_slug"])

    # PCS lists every provisional starter; Scorito prices only part of them.
    return matched


def _validated_sprint_assessments(
    projected_by_id: Mapping[int, Mapping[str, Any]],
) -> dict[int, SprintAssessment]:
    assessments: dict[int, SprintAssessment] = {}
    for rider_id, projected in projected_by_id.items():
        serialized = projected.get("capabilities")
        if not isinstance(serialized, Mapping):
            raise RuntimeError(f"projected rider {rider_id} has no serialized capabilities")
        try:
            reconstructed = validated_sprint_assessment_from_serialized(serialized)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"projected rider {rider_id} has invalid capability evidence"
            ) from exc
        assessments[rider_id] = reconstructed
    return assessments


def _snapshot_timestamp() -> str:
    timestamp = (DATA_DIR / "_manifest.json").stat().st_mtime
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _blended_quality_ratings(
    projected: dict[str, Any], raw: dict[str, Any]
) -> dict[str, float]:
    model = projected.get("model_qualities") or {}
    live = {
        int(quality["Type"]): float(quality["Value"])
        for quality in raw.get("Qualities", [])
    }
    has_live_ratings = bool(live)
    ratings: dict[str, float] = {}
    for quality_type, name in MODEL_QUALITY_NAMES.items():
        model_value = float(model.get(name, 0.0))
        value = (
            SCORITO_QUALITY_WEIGHT * live.get(quality_type, 0.0)
            + (1.0 - SCORITO_QUALITY_WEIGHT) * model_value
            if has_live_ratings
            else model_value
        )
        ratings[name] = round(max(0.0, min(10.0, value)), 1)
    return ratings


def _conditional_team_bonus_upside(
    *, rider_team_id: int, winner_team_id: int, red_jersey_team_id: int | None
) -> float:
    """Conditional upside only; never part of the base lineup objective."""
    bonus = GC_TEAM_BONUS if rider_team_id == red_jersey_team_id else 0.0
    if rider_team_id == winner_team_id:
        bonus += STAGE_WIN_TEAM_BONUS
    return bonus


def _stage_analysis_weight(
    stage_projection: dict[str, Any], stage_notes: dict[str, Any], weight_cap: float
) -> tuple[float, str]:
    if not stage_notes:
        return 0.0, "not_available"

    profile = str(stage_projection.get("profile_type", "")).lower()
    finish = str(stage_projection.get("finish_type", "")).lower()
    note_type = str(stage_notes.get("type", "")).lower()
    target_distance = float(stage_projection.get("distance_km") or 0.0)
    note_distance = float(stage_notes.get("distance_km") or 0.0)
    if target_distance and note_distance:
        tolerance = max(15.0, target_distance * 0.20)
        if abs(target_distance - note_distance) > tolerance:
            return 0.0, "ignored_distance_mismatch"

    if profile == "itt" and "itt" not in note_type:
        return 0.0, "ignored_profile_mismatch"
    if profile != "itt" and "itt" in note_type:
        return 0.0, "ignored_profile_mismatch"
    if profile == "mountain" and finish == "summit" and not any(
        marker in note_type for marker in ("gc", "mountain", "hill", "punch")
    ):
        return 0.0, "ignored_profile_mismatch"
    if profile == "flat" and finish == "sprint" and all(
        marker not in note_type
        for marker in ("sprint", "flat", "transition", "breakaway", "punch")
    ):
        return 0.0, "ignored_profile_mismatch"
    return max(0.0, min(0.15, weight_cap)), "applied"


def _captain_eligible_ids(
    stage_projection: dict[str, Any],
    stage_notes: dict[str, Any],
    analysis_status: str,
    squad_ids: set[int],
    sprint_ids: set[int],
) -> set[int]:
    profile = str(stage_projection.get("profile_type", "")).lower()
    finish = str(stage_projection.get("finish_type", "")).lower()
    note_type = str(stage_notes.get("type", "")).lower()
    flat_finish = profile in {"flat", "hilly"} and finish in {"flat", "sprint"}
    expert_supports_punchers = analysis_status == "applied" and any(
        marker in note_type for marker in ("gc", "mountain", "hill", "punch")
    )
    if flat_finish and not expert_supports_punchers:
        sprint_pool = squad_ids & sprint_ids
        if sprint_pool:
            return sprint_pool
    return set(squad_ids)


def _objective_stage_lineup(
    stage: Any,
    squad_ids: list[int],
    points_by_rider: dict[int, float],
    *,
    captain_eligible_ids: set[int] | None = None,
    captain_rank_scores: Mapping[int, float] | None = None,
    lineup_size: int = LINEUP_SIZE,
    captain_factor: int = 2,
) -> StageLineup:
    if len(squad_ids) < lineup_size:
        raise RuntimeError("fewer squad riders than lineup places")
    missing_points = sorted(set(squad_ids) - set(points_by_rider))
    if missing_points:
        raise RuntimeError(f"stage points are missing for squad riders: {missing_points}")

    chosen = sorted(
        squad_ids,
        key=lambda rider_id: (-points_by_rider[rider_id], rider_id),
    )[:lineup_size]
    eligible_captains = [
        rider_id
        for rider_id in chosen
        if captain_eligible_ids is None or rider_id in captain_eligible_ids
    ]
    if not eligible_captains:
        raise RuntimeError("stage lineup has no eligible captain")
    ranked_captains = (
        [rider_id for rider_id in eligible_captains if rider_id in captain_rank_scores]
        if captain_rank_scores
        else []
    )
    captain_candidates = ranked_captains or eligible_captains
    ranking_scores = captain_rank_scores if ranked_captains else points_by_rider
    captain_id = min(
        captain_candidates,
        key=lambda rider_id: (-ranking_scores[rider_id], rider_id),
    )
    captain_points = points_by_rider[captain_id]
    total = sum(points_by_rider[rider_id] for rider_id in chosen)
    total += (captain_factor - 1) * captain_points
    return StageLineup(
        stage=stage,
        rider_ids=chosen,
        captain_id=captain_id,
        captain_points=captain_points,
        total=total,
    )


def build_live_recommendation() -> dict[str, Any]:
    projection = json.loads(PROJECTION_PATH.read_text(encoding="utf-8"))
    snapshot = load_snapshot(SLUG)
    raw_riders = _read_content(DATA_DIR / "eventriderenriched.json")
    raw_by_id = {int(row["RiderId"]): row for row in raw_riders}
    team_rows = _read_content(DATA_DIR / "teams_all.json")
    team_names = {int(row["Id"]): row["Name"] for row in team_rows}
    live_stages = {
        int(row["Id"]): row for row in _read_content(DATA_DIR / "stage_market.json")
    }
    game_info = _read_content(DATA_DIR / "gameInfo.json")

    if snapshot.market_id != MARKET_ID:
        raise RuntimeError(f"expected market {MARKET_ID}, loaded {snapshot.market_id}")
    if projection.get("market_id") != MARKET_ID:
        raise RuntimeError("projection belongs to a different market")
    if (
        projection.get("schema_version") != 4
        or projection.get("model_version") != "pcs-scorito-evidence-v5"
    ):
        raise RuntimeError("refresh project_vuelta.py before building the live recommendation")
    if len(snapshot.stages) != 21 or projection.get("stage_count") != 21:
        raise RuntimeError("expected all 21 Vuelta stages")
    if not NEWS_PATH.exists():
        raise RuntimeError("completed Vuelta rider-news digest is missing")

    uae_team_ids = [team_id for team_id, name in team_names.items() if name == UAE_TEAM_NAME]
    if len(uae_team_ids) != 1:
        raise RuntimeError(f"expected one live {UAE_TEAM_NAME!r} team, found {len(uae_team_ids)}")
    uae_team_id = uae_team_ids[0]
    uae_ids = {rider.rider_id for rider in snapshot.riders if rider.team_id == uae_team_id}
    if len(uae_ids) < MAX_RIDERS_PER_TEAM:
        raise RuntimeError("live market exposes fewer than four UAE riders")

    projected_by_id = _match_projection_riders(snapshot, projection["riders"])
    uncovered_riders = sorted(
        rider.name for rider in snapshot.riders if rider.rider_id not in projected_by_id
    )
    if uncovered_riders:
        # Riders Scorito prices but PCS has no evidence row for cannot be valued.
        snapshot.riders = [
            rider for rider in snapshot.riders if rider.rider_id in projected_by_id
        ]
        snapshot.__post_init__()
    sprint_assessments = _validated_sprint_assessments(projected_by_id)
    slug_to_id = {
        row["rider_slug"]: rider_id for rider_id, row in projected_by_id.items()
    }
    stage_top20 = json.loads(STAGE_TOP20_PATH.read_text(encoding="utf-8"))
    if (
        stage_top20.get("sources", {}).get("projection_generated_at")
        != projection.get("generated_at")
    ):
        raise RuntimeError("stage top-20 predictions do not match the active projection")
    captain_rank_scores_by_stage = {
        int(stage["stage_no"]): {
            slug_to_id[row["rider_slug"]]: float(row["combined_score"])
            for row in stage["top_20"]
            if row["rider_slug"] in slug_to_id
        }
        for stage in stage_top20["stages"]
    }
    decision_by_slug = {
        row["rider_slug"]: row for row in projection["decision_review"]
    }
    if not set(slug_to_id) <= set(decision_by_slug):
        raise RuntimeError("projection decision rows do not cover the live rider field")
    decisions = {
        rider_id: decision_by_slug[row["rider_slug"]]
        for rider_id, row in projected_by_id.items()
    }
    gradual_ratings = {
        rider_id: _blended_quality_ratings(projected_by_id[rider_id], raw_by_id[rider_id])
        for rider_id in projected_by_id
    }

    projection_stages = {int(row["stage_no"]): row for row in projection["stages"]}
    stage_top20_by_no = {
        int(row["stage_no"]): row for row in stage_top20["stages"]
    }
    base_projected_points: dict[tuple[int, int], float] = {}
    stage_rows: dict[int, list[dict[str, Any]]] = {}
    for stage in snapshot.stages:
        stage_report = stage_top20_by_no.get(stage.order)
        if stage_report is None:
            raise RuntimeError(f"stage top-20 is missing stage {stage.order}")
        rows = [
            row for row in stage_report["top_20"]
            if row["rider_slug"] in slug_to_id
        ]
        if len(rows) != 20:
            raise RuntimeError(f"stage {stage.order} does not contain 20 live riders")
        stage_rows[stage.order] = rows
        for rider in snapshot.riders:
            base_projected_points[(rider.rider_id, stage.stage_id)] = 0.0
        for row in rows:
            rider_id = slug_to_id[row["rider_slug"]]
            base_projected_points[(rider_id, stage.stage_id)] = float(
                row["scorito_stage_points"]
            )

    qk_path = DATA_DIR / "qk_expert_opinion.json"
    qk_weight_cap = 0.0
    qk_breakdown: dict[str, Any] = {}
    qk_unavailable_slugs: set[str] = set()
    qk_source = "not available"
    if qk_path.exists():
        qk_data = json.loads(qk_path.read_text(encoding="utf-8"))
        qk_source = f"{qk_path.name} ({qk_data.get('generated_at', 'unknown time')})"
        if qk_data.get("retrieval_status") == "retrieved":
            qk_weight_cap = min(0.15, float(qk_data.get("weight_applied", 0.15)))
            qk_breakdown = qk_data.get("stage_breakdown", {})
            qk_unavailable_slugs = {
                str(row.get("rider_slug") or "")
                for row in qk_data.get("selection_assumptions", {}).get(
                    "unavailable_riders", []
                )
                if row.get("rider_slug")
            }

    expert_chat_source = "not available"
    expert_chat_signals: dict[int, dict[int, float]] = {}
    if EXPERT_CHAT_PATH.exists():
        intel = json.loads(EXPERT_CHAT_PATH.read_text(encoding="utf-8"))
        summary = intel.get("summary", {})
        expert_chat_source = (
            f"{EXPERT_CHAT_PATH.name} ({intel.get('generated_at', 'unknown time')}; "
            f"{summary.get('messages', 0)} messages)"
        )
        signals_by_key = signals_by_rider_stage(intel, stage_max=len(snapshot.stages))
        for rider in snapshot.riders:
            per_stage = signals_by_key.get(intel_name_key(rider.name))
            if per_stage:
                expert_chat_signals[rider.rider_id] = per_stage

    forum_opinion = load_forum_opinion(FORUM_OPINION_PATH)
    forum_by_id = {
        slug_to_id[slug]: row
        for slug, row in forum_opinion.get("riders", {}).items()
        if slug in slug_to_id
    }

    news_data = json.loads(NEWS_PATH.read_text(encoding="utf-8"))
    if news_data.get("race", {}).get("id") != SLUG:
        raise RuntimeError("rider-news digest belongs to a different race")
    news_by_slug = {
        row["rider_slug"]: row for row in news_data.get("selection_impacts", [])
    }
    news_impacts = {
        slug_to_id[rider_slug]: row
        for rider_slug, row in news_by_slug.items()
        if rider_slug in slug_to_id
    }

    model_projected_points: dict[tuple[int, int], float] = {}
    individual_projected_points: dict[tuple[int, int], float] = {}
    team_bonus_points: dict[tuple[int, int], float] = {}
    conditional_team_bonus_points: dict[tuple[int, int], float] = {}
    projected_points: dict[tuple[int, int], float] = {}
    stage_winner_team_ids: dict[int, int] = {}
    uae_team_bonus_by_stage: dict[int, float] = {}
    stage_analysis_weights: dict[int, float] = {}
    stage_analysis_statuses: dict[int, str] = {}
    for stage in snapshot.stages:
        relevance = quality_relevance(stage)
        quality_scores = {
            rider.rider_id: sum(
                weight * gradual_ratings[rider.rider_id][MODEL_QUALITY_NAMES[quality_type]]
                for quality_type, weight in relevance.items()
            )
            for rider in snapshot.riders
        }
        maximum_quality = max(quality_scores.values(), default=1.0) or 1.0
        maximum_base = max(
            base_projected_points[(rider.rider_id, stage.stage_id)]
            for rider in snapshot.riders
        )
        winner_id = slug_to_id[stage_rows[stage.order][0]["rider_slug"]]
        winner_team_id = snapshot.rider(winner_id).team_id
        stage_winner_team_ids[stage.stage_id] = winner_team_id
        uae_team_bonus_by_stage[stage.stage_id] = _conditional_team_bonus_upside(
            rider_team_id=uae_team_id,
            winner_team_id=winner_team_id,
            red_jersey_team_id=uae_team_id,
        )

        stage_notes = qk_breakdown.get(str(stage.order), {})
        _stage_weight, analysis_status = _stage_analysis_weight(
            projection_stages[stage.order], stage_notes, qk_weight_cap
        )
        expert_weight = 0.0
        stage_analysis_weights[stage.order] = expert_weight
        stage_analysis_statuses[stage.order] = analysis_status
        base_weight = 1.0 - QUALITY_STAGE_WEIGHT - expert_weight
        stype = str(stage_notes.get("type", "")).lower()

        expert_scores = {}
        for rider in snapshot.riders:
            rider_ratings = gradual_ratings[rider.rider_id]
            if "gc" in stype or "mountain" in stype:
                expert_score = rider_ratings["gc"] * 0.6 + rider_ratings["climb"] * 0.4
            elif "itt" in stype:
                expert_score = rider_ratings["time_trial"]
            elif "sprint" in stype:
                expert_score = rider_ratings["sprint"]
            elif "punch" in stype or "hill" in stype:
                expert_score = rider_ratings["punch"] * 0.5 + rider_ratings["hill"] * 0.5
            else:
                expert_score = sum(
                    weight * rider_ratings[MODEL_QUALITY_NAMES[quality_type]]
                    for quality_type, weight in relevance.items()
                )
            expert_scores[rider.rider_id] = expert_score

        maximum_expert = max(expert_scores.values(), default=1.0) or 1.0
        for rider in snapshot.riders:
            rider_id = rider.rider_id
            base = base_projected_points[(rider_id, stage.stage_id)]
            quality_proxy = maximum_base * quality_scores[rider_id] / maximum_quality
            expert_proxy = maximum_base * expert_scores[rider_id] / maximum_expert
            individual = (
                base_weight * base
                + QUALITY_STAGE_WEIGHT * quality_proxy
                + expert_weight * expert_proxy
            )
            model_projected_points[(rider_id, stage.stage_id)] = individual
            # Expert chat is already bounded and applied in stage_top20_predictions.json.
            conditional_bonus = _conditional_team_bonus_upside(
                rider_team_id=rider.team_id,
                winner_team_id=winner_team_id,
                red_jersey_team_id=uae_team_id,
            )
            individual_projected_points[(rider_id, stage.stage_id)] = individual
            team_bonus_points[(rider_id, stage.stage_id)] = 0.0
            conditional_team_bonus_points[(rider_id, stage.stage_id)] = conditional_bonus
            projected_points[(rider_id, stage.stage_id)] = individual

    classification_values_raw = {
        rider_id: float(row["classification_jersey_points"])
        for rider_id, row in decisions.items()
    }
    cyclingoracle_probabilities, cyclingoracle_source = (
        _load_cyclingoracle_classifications(snapshot)
    )
    classification_values = {
        rider_id: value
        * (
            1.0
            + CYCLINGORACLE_CLASSIFICATION_WEIGHT
            * max(cyclingoracle_probabilities.get(rider_id, {}).values(), default=0.0)
        )
        for rider_id, value in classification_values_raw.items()
    }
    sprint_ids = {
        rider_id
        for rider_id, assessment in sprint_assessments.items()
        if assessment.eligible
    }
    minimum_sprint_options = int(
        projection["constraints"]["minimum_credible_sprint_options"]
    )
    if len(sprint_ids) < minimum_sprint_options:
        raise RuntimeError("live market exposes too few rated sprint options")
    unavailable_slugs = (
        set(projection["constraints"]["unavailable_riders"])
        | qk_unavailable_slugs
    )
    excluded_ids = {
        rider_id
        for rider_id, raw in raw_by_id.items()
        if int(raw.get("Status", 0)) != 1
    } | {
        slug_to_id[rider_slug]
        for rider_slug in unavailable_slugs
        if rider_slug in slug_to_id
    }

    live_ids = {rider.rider_id for rider in snapshot.riders}

    def points_fn(rider_id: int, stage) -> float:
        return individual_projected_points[(rider_id, stage.stage_id)]

    captain_eligible_by_stage = {
        stage.order: _captain_eligible_ids(
            projection_stages[stage.order],
            qk_breakdown.get(str(stage.order), {}),
            stage_analysis_statuses[stage.order],
            live_ids,
            sprint_ids,
        )
        for stage in snapshot.stages
    }

    comparator_coverage = [(sprint_ids, minimum_sprint_options)]
    optimized_plan = joint_enrolled_squad(
        snapshot,
        points_fn,
        budget=snapshot.budget,
        squad_size=SQUAD_SIZE,
        lineup_size=LINEUP_SIZE,
        selection_values=classification_values,
        max_riders_per_team=MAX_RIDERS_PER_TEAM,
        coverage_constraints=comparator_coverage,
        excluded_rider_ids=excluded_ids,
    )
    forced_four_uae_plan = joint_enrolled_squad(
        snapshot,
        points_fn,
        budget=snapshot.budget,
        squad_size=SQUAD_SIZE,
        lineup_size=LINEUP_SIZE,
        selection_values=classification_values,
        max_riders_per_team=MAX_RIDERS_PER_TEAM,
        coverage_constraints=[*comparator_coverage, (uae_ids, MAX_RIDERS_PER_TEAM)],
        excluded_rider_ids=excluded_ids,
    )
    if optimized_plan is None or forced_four_uae_plan is None:
        raise RuntimeError("live-price Vuelta comparator MILP failed")

    def reevaluate_comparator(candidate: SquadPlan) -> SquadPlan:
        comparator_lineups: list[StageLineup] = []
        comparator_stage_total = 0.0
        candidate_ids = set(candidate.rider_ids)
        for stage in snapshot.stages:
            stage_points = {
                rider_id: individual_projected_points[(rider_id, stage.stage_id)]
                for rider_id in candidate.rider_ids
            }
            lineup = _objective_stage_lineup(
                stage,
                candidate.rider_ids,
                stage_points,
                captain_eligible_ids=captain_eligible_by_stage[stage.order] & candidate_ids,
                captain_rank_scores=captain_rank_scores_by_stage[stage.order],
                lineup_size=LINEUP_SIZE,
                captain_factor=snapshot.captain_factor,
            )
            comparator_lineups.append(lineup)
            comparator_stage_total += lineup.total
        classification_total = sum(
            classification_values[rider_id] for rider_id in candidate.rider_ids
        )
        return SquadPlan(
            rider_ids=candidate.rider_ids,
            total_price=candidate.total_price,
            value=comparator_stage_total + classification_total,
            lineups=comparator_lineups,
            season_total=comparator_stage_total,
        )

    optimized_plan = reevaluate_comparator(optimized_plan)
    forced_four_uae_plan = reevaluate_comparator(forced_four_uae_plan)
    personal_ids = list(optimized_plan.rider_ids)
    selected_ids = set(personal_ids)
    total_price = optimized_plan.total_price
    team_counts = Counter(snapshot.rider(rider_id).team_id for rider_id in personal_ids)

    lineups = []
    projected_individual_stage_total = 0.0
    projected_team_bonus_total = 0.0
    conditional_team_bonus_total = 0.0
    for stage in snapshot.stages:
        stage_points = {
            rider_id: individual_projected_points[(rider_id, stage.stage_id)]
            for rider_id in personal_ids
        }
        lineup = _objective_stage_lineup(
            stage,
            personal_ids,
            stage_points,
            captain_eligible_ids=captain_eligible_by_stage[stage.order] & selected_ids,
            captain_rank_scores=captain_rank_scores_by_stage[stage.order],
            lineup_size=LINEUP_SIZE,
            captain_factor=snapshot.captain_factor,
        )
        if len(lineup.rider_ids) != LINEUP_SIZE or len(set(lineup.rider_ids)) != LINEUP_SIZE:
            raise RuntimeError(f"stage {stage.order} lineup is not nine unique riders")
        if lineup.captain_id not in lineup.rider_ids:
            raise RuntimeError(f"stage {stage.order} captain is not enrolled")
        if not set(lineup.rider_ids) <= selected_ids:
            raise RuntimeError(f"stage {stage.order} includes a rider outside the selected squad")

        individual_total = sum(
            individual_projected_points[(rider_id, stage.stage_id)]
            for rider_id in lineup.rider_ids
        ) + (snapshot.captain_factor - 1) * individual_projected_points[
            (lineup.captain_id, stage.stage_id)
        ]
        if abs(lineup.total - individual_total) > 1e-6:
            raise RuntimeError(f"stage {stage.order} lineup total does not reconcile")
        conditional_bonus_total = sum(
            conditional_team_bonus_points[(rider_id, stage.stage_id)]
            for rider_id in lineup.rider_ids
        )
        projected_individual_stage_total += individual_total
        conditional_team_bonus_total += conditional_bonus_total
        ideal_ids = sorted(
            (rider.rider_id for rider in snapshot.riders),
            key=lambda rider_id: individual_projected_points[(rider_id, stage.stage_id)],
            reverse=True,
        )[:LINEUP_SIZE]
        lineups.append(
            {
                "stage": stage,
                "lineup": lineup,
                "live": live_stages[stage.stage_id],
                "projection": projection_stages[stage.order],
                "ideal_ids": ideal_ids,
                "individual_total": individual_total,
                "team_bonus_total": 0.0,
                "conditional_team_bonus_total": conditional_bonus_total,
                "winner_team_id": stage_winner_team_ids[stage.stage_id],
                "uae_team_bonus_per_rider": uae_team_bonus_by_stage[stage.stage_id],
            }
        )

    projected_stage_total = projected_individual_stage_total
    projected_classification_total = sum(
        classification_values[rider_id] for rider_id in selected_ids
    )
    plan = SquadPlan(
        rider_ids=personal_ids,
        total_price=total_price,
        value=projected_stage_total + projected_classification_total,
        lineups=[item["lineup"] for item in lineups],
        season_total=projected_stage_total,
    )
    if abs(plan.value - projected_stage_total - projected_classification_total) > 0.1:
        raise RuntimeError("optimized plan objective does not reconcile")

    scenario_candidates = [
        ("optimized_unconstrained_uae", plan, True),
        ("optimized_forced_four_uae", forced_four_uae_plan, False),
    ]
    benchmark_value = max(candidate.value for _name, candidate, _selected in scenario_candidates)
    scenarios = [
        {
            "scenario": name,
            "required_ids": [],
            "plan": candidate,
            "objective_gap": benchmark_value - candidate.value,
            "selected": selected,
            "uae_count": len(set(candidate.rider_ids) & uae_ids),
        }
        for name, candidate, selected in scenario_candidates
    ]

    model_stage_potential = {
        rider.rider_id: sum(
            model_projected_points[(rider.rider_id, stage.stage_id)]
            for stage in snapshot.stages
        )
        for rider in snapshot.riders
    }
    individual_stage_potential = {
        rider.rider_id: sum(
            individual_projected_points[(rider.rider_id, stage.stage_id)]
            for stage in snapshot.stages
        )
        for rider in snapshot.riders
    }
    team_bonus_potential = {rider.rider_id: 0.0 for rider in snapshot.riders}
    conditional_team_bonus_potential = {
        rider.rider_id: sum(
            conditional_team_bonus_points[(rider.rider_id, stage.stage_id)]
            for stage in snapshot.stages
        )
        for rider in snapshot.riders
    }
    stage_potential = dict(individual_stage_potential)
    season_values = {
        rider.rider_id: stage_potential[rider.rider_id]
        + classification_values[rider.rider_id]
        for rider in snapshot.riders
    }
    overall_ranks = {
        rider_id: rank
        for rank, rider_id in enumerate(
            sorted(season_values, key=season_values.get, reverse=True), start=1
        )
    }
    value_ranks = {
        rider_id: rank
        for rank, rider_id in enumerate(
            sorted(
                season_values,
                key=lambda rider_id: season_values[rider_id] / snapshot.rider(rider_id).price,
                reverse=True,
            ),
            start=1,
        )
    }

    return {
        "snapshot": snapshot,
        "projection": projection,
        "snapshot_at": _snapshot_timestamp(),
        "game_info": game_info,
        "raw_by_id": raw_by_id,
        "team_names": team_names,
        "projected_by_id": projected_by_id,
        "sprint_assessments": sprint_assessments,
        "decisions": decisions,
        "gradual_ratings": gradual_ratings,
        "classification_values_raw": classification_values_raw,
        "classification_values": classification_values,
        "cyclingoracle_probabilities": cyclingoracle_probabilities,
        "cyclingoracle_source": cyclingoracle_source,
        "base_projected_points": base_projected_points,
        "model_projected_points": model_projected_points,
        "individual_projected_points": individual_projected_points,
        "team_bonus_points": team_bonus_points,
        "conditional_team_bonus_points": conditional_team_bonus_points,
        "projected_points": projected_points,
        "plan": plan,
        "plan_source": "joint live-price top-20-points optimizer",
        "selected_scenario": "optimized_unconstrained_uae",
        "scenarios": scenarios,
        "selected_ids": selected_ids,
        "sprint_ids": sprint_ids,
        "uae_ids": uae_ids,
        "uae_team_id": uae_team_id,
        "lineups": lineups,
        "projected_individual_stage_total": projected_individual_stage_total,
        "projected_team_bonus_total": projected_team_bonus_total,
        "conditional_team_bonus_total": conditional_team_bonus_total,
        "projected_stage_total": projected_stage_total,
        "projected_classification_total": projected_classification_total,
        "model_stage_potential": model_stage_potential,
        "individual_stage_potential": individual_stage_potential,
        "team_bonus_potential": team_bonus_potential,
        "conditional_team_bonus_potential": conditional_team_bonus_potential,
        "stage_potential": stage_potential,
        "season_values": season_values,
        "overall_ranks": overall_ranks,
        "value_ranks": value_ranks,
        "team_counts": team_counts,
        "minimum_sprint_options": minimum_sprint_options,
        "expert_weight": qk_weight_cap,
        "stage_analysis_source": qk_source,
        "uncovered_riders": uncovered_riders,
        "expert_chat_source": expert_chat_source,
        "expert_chat_signals": expert_chat_signals,
        "forum_opinion": forum_opinion,
        "forum_by_id": forum_by_id,
        "stage_analysis_weights": stage_analysis_weights,
        "captain_rank_scores_by_stage": captain_rank_scores_by_stage,
        "stage_analysis_statuses": stage_analysis_statuses,
        "news_data": news_data,
        "news_impacts": news_impacts,
    }

def _common_columns(result: dict[str, Any]) -> dict[str, Any]:
    snapshot = result["snapshot"]
    plan = result["plan"]
    news_data = result["news_data"]
    uae_names = sorted(
        snapshot.rider(rider_id).name for rider_id in result["selected_ids"] & result["uae_ids"]
    )
    return {
        "snapshot_at_utc": result["snapshot_at"],
        "projection_at_utc": result["projection"]["generated_at"],
        "market_id": MARKET_ID,
        "market_name": result["game_info"]["MarketName"],
        "price_source": "live public Scorito market 310",
        "plan_source": result["plan_source"],
        "cyclingoracle_source": result["cyclingoracle_source"],
        "value_source": (
            "indicative PCS evidence-v5 plus gradual live/model qualities; "
            f"CyclingOracle classification uplift capped at {CYCLINGORACLE_CLASSIFICATION_WEIGHT * 100:.0f}%; "
            "captains ranked by course-specific stage top-20 combined score; "
            f"compatible QK stage notes capped at {result['expert_weight'] * 100:.0f}%; "
            f"combined expert-chat/forum opinion capped at {OPINION_MAX_ADJUSTMENT * 100:.0f}%; "
            f"{len(result['uncovered_riders'])} priced riders lack a PCS row and were dropped; "
            "news is selection evidence only; uncertain teammate bonuses are excluded"
        ),
        "stage_analysis_source": result["stage_analysis_source"],
        "expert_chat_source": result["expert_chat_source"],
        "news_source": (
            f"latest.json ({news_data.get('generated_at', 'unknown time')}; "
            f"{news_data.get('source_success_count', 0)}/{news_data.get('source_count', 0)} sources)"
        ),
        "budget": snapshot.budget,
        "budget_m": f"{snapshot.budget / 1_000_000:.2f}",
        "squad_total_price": plan.total_price,
        "squad_total_price_m": f"{plan.total_price / 1_000_000:.2f}",
        "budget_remaining": snapshot.budget - plan.total_price,
        "budget_remaining_m": f"{(snapshot.budget - plan.total_price) / 1_000_000:.2f}",
        "uae_riders": "; ".join(uae_names),
        "uae_team_bonus_assumption": (
            "Excluded from base lineup ranking. Conditional upside only: +8 if UAE holds red; "
            "no automatic points/KOM jersey credit; +10 only if the projected teammate wins."
        ),
        "conditional_team_bonus_upside_points": f"{result['conditional_team_bonus_total']:.2f}",
        "projected_individual_stage_points": f"{result['projected_individual_stage_total']:.2f}",
        "projected_team_bonus_points": "0.00",
        "projected_enrolled_stage_points": f"{result['projected_stage_total']:.2f}",
        "projected_classification_jersey_points": f"{result['projected_classification_total']:.2f}",
        "projected_objective": f"{plan.value:.2f}",
    }


def _mean_signal(result: dict[str, Any], rider_id: int) -> float:
    per_stage = result["expert_chat_signals"].get(rider_id, {})
    return sum(per_stage.values()) / len(per_stage) if per_stage else 0.0


def _rider_columns(result: dict[str, Any], rider_id: int) -> dict[str, Any]:
    snapshot = result["snapshot"]
    rider = snapshot.rider(rider_id)
    raw = result["raw_by_id"][rider_id]
    projected = result["projected_by_id"][rider_id]
    evidence = projected["recent_evidence"]
    capabilities = projected["capabilities"]
    assessment = result["sprint_assessments"][rider_id]
    audit_fields = capability_audit_fields(capabilities, assessment=assessment)
    qualities = {int(row["Type"]): int(row["Value"]) for row in raw.get("Qualities", [])}
    ratings = result["gradual_ratings"][rider_id]
    news = result["news_impacts"].get(rider_id, {})
    forum = result["forum_by_id"].get(rider_id, {})
    forum_uncertainty = forum.get("uncertainty", {})
    return {
        "rider_id": rider.rider_id,
        "event_rider_id": rider.event_rider_id,
        "rider": rider.name,
        "team": result["team_names"][rider.team_id],
        "role": rider.role_label,
        "live_price": rider.price,
        "live_price_m": f"{rider.price / 1_000_000:.2f}",
        "selected_squad": "yes" if rider_id in result["selected_ids"] else "no",
        "quality_gc": qualities.get(0, 0),
        "quality_climb": qualities.get(1, 0),
        "quality_time_trial": qualities.get(2, 0),
        "quality_sprint": qualities.get(3, 0),
        "quality_punch": qualities.get(4, 0),
        "quality_hill": qualities.get(5, 0),
        "quality_cobbles": qualities.get(6, 0),
        "rating_gc": ratings["gc"],
        "rating_climb": ratings["climb"],
        "rating_time_trial": ratings["time_trial"],
        "rating_sprint": ratings["sprint"],
        "rating_punch": ratings["punch"],
        "rating_hill": ratings["hill"],
        "rating_cobbles": ratings["cobbles"],
        "projected_model_stage_potential": f"{result['model_stage_potential'][rider_id]:.2f}",
        "projected_individual_stage_potential": f"{result['individual_stage_potential'][rider_id]:.2f}",
        "projected_team_bonus_potential": "0.00",
        "conditional_team_bonus_upside_potential": (
            f"{result['conditional_team_bonus_potential'][rider_id]:.2f}"
        ),
        "projected_stage_potential": f"{result['stage_potential'][rider_id]:.2f}",
        "classification_jersey_value_raw": f"{result['classification_values_raw'][rider_id]:.2f}",
        "cyclingoracle_classification_probability": (
            f"{max(result['cyclingoracle_probabilities'].get(rider_id, {}).values(), default=0.0):.4f}"
        ),
        "classification_jersey_value": f"{result['classification_values'][rider_id]:.2f}",
        "season_proxy_points": f"{result['season_values'][rider_id]:.2f}",
        "value_points_per_m_live_price": (
            f"{result['season_values'][rider_id] / rider.price * 1_000_000:.2f}"
        ),
        "projected_overall_rank": result["overall_ranks"][rider_id],
        "live_value_rank": result["value_ranks"][rider_id],
        "sprint_option": "yes" if rider_id in result["sprint_ids"] else "no",
        **audit_fields,
        "expert_chat_signal_avg": f"{_mean_signal(result, rider_id):.3f}",
        "forum_uncertainty_level": forum_uncertainty.get("level", ""),
        "forum_value_signal": f"{float(forum.get('value_signal', 0.0)):.3f}",
        "forum_worst_stage_signal": (
            f"{float(forum_uncertainty.get('worst_stage_signal', 0.0)):.3f}"
        ),
        "forum_uncertainty_reasons": "; ".join(
            str(reason) for reason in forum_uncertainty.get("reasons", [])
        ),
        "news_impact": news.get("impact", ""),
        "news_verification": news.get("verification", ""),
        "news_decision": news.get("decision_hint", ""),
        "news_evidence": news.get("title", ""),
        "field_course_recency_score": evidence["recency_score"],
        "field_course_context_results": evidence["field_course_context_results"],
        "evidence": result["decisions"][rider_id]["strongest_evidence"],
        "uncertainty": result["projection"]["startlist_status"],
    }


def write_combined_csv(result: dict[str, Any], path: Path = OUTPUT_PATH) -> Path:
    common = _common_columns(result)
    snapshot = result["snapshot"]
    rows: list[dict[str, Any]] = []

    for scenario in result["scenarios"]:
        plan = scenario["plan"]
        squad_ids = sorted(
            plan.rider_ids,
            key=lambda rider_id: result["season_values"][rider_id],
            reverse=True,
        )
        rows.append(
            {
                **common,
                "record_type": "scenario",
                "scenario": scenario["scenario"],
                "required_riders": "; ".join(
                    snapshot.rider(rider_id).name for rider_id in scenario["required_ids"]
                ),
                "selected_scenario": "yes" if scenario["selected"] else "no",
                "scenario_squad": "; ".join(snapshot.rider(rider_id).name for rider_id in squad_ids),
                "scenario_total_price_m": f"{plan.total_price / 1_000_000:.2f}",
                "scenario_projected_objective": f"{plan.value:.2f}",
                "scenario_objective_gap": f"{scenario['objective_gap']:.2f}",
                "scenario_uae_count": scenario["uae_count"],
            }
        )

    squad_order = sorted(
        result["plan"].rider_ids,
        key=lambda rider_id: result["season_values"][rider_id],
        reverse=True,
    )
    for rider_id in squad_order:
        rows.append({**common, "record_type": "squad_member", **_rider_columns(result, rider_id)})

    for item in result["lineups"]:
        stage = item["stage"]
        lineup = item["lineup"]
        live = item["live"]
        stage_projection = item["projection"]
        rider_names = [snapshot.rider(rider_id).name for rider_id in lineup.rider_ids]
        rows.append(
            {
                **common,
                "record_type": "stage_lineup",
                "stage_no": stage.order,
                "stage_date": live["StartDate"],
                "route": f"{live['StartLocation']} -> {live['FinishLocation']}",
                "profile_type": stage_projection["profile_type"],
                "finish_type": stage_projection["finish_type"],
                "distance_km": live["Distance"],
                "projected_stage_winner_team": result["team_names"][item["winner_team_id"]],
                "uae_team_bonus_per_rider": f"{item['uae_team_bonus_per_rider']:.2f}",
                "captain": snapshot.rider(lineup.captain_id).name,
                "captain_projected_points": f"{lineup.captain_points:.2f}",
                "lineup": "; ".join(rider_names),
                "lineup_live_prices_m": "; ".join(
                    f"{snapshot.rider(rider_id).name}={snapshot.rider(rider_id).price / 1_000_000:.2f}"
                    for rider_id in lineup.rider_ids
                ),
                "lineup_model_projected_points": "; ".join(
                    f"{snapshot.rider(rider_id).name}="
                    f"{result['model_projected_points'][(rider_id, stage.stage_id)]:.2f}"
                    for rider_id in lineup.rider_ids
                ),
                "lineup_expert_chat_signals": "; ".join(
                    f"{snapshot.rider(rider_id).name}="
                    f"{result['expert_chat_signals'].get(rider_id, {}).get(stage.order, 0.0):.3f}"
                    for rider_id in lineup.rider_ids
                ),
                "lineup_individual_projected_points": "; ".join(
                    f"{snapshot.rider(rider_id).name}="
                    f"{result['individual_projected_points'][(rider_id, stage.stage_id)]:.2f}"
                    for rider_id in lineup.rider_ids
                ),
                "lineup_team_bonus_points": "; ".join(
                    f"{snapshot.rider(rider_id).name}=0.00" for rider_id in lineup.rider_ids
                ),
                "lineup_projected_points": "; ".join(
                    f"{snapshot.rider(rider_id).name}="
                    f"{result['projected_points'][(rider_id, stage.stage_id)]:.2f}"
                    for rider_id in lineup.rider_ids
                ),
                "ideal_market_lineup": "; ".join(
                    snapshot.rider(rider_id).name for rider_id in item["ideal_ids"]
                ),
                "projected_individual_stage_points": f"{item['individual_total']:.2f}",
                "projected_team_bonus_points": "0.00",
                "conditional_team_bonus_upside_points": (
                    f"{item['conditional_team_bonus_total']:.2f}"
                ),
                "projected_lineup_total": f"{lineup.total:.2f}",
                "stage_analysis_weight": f"{result['stage_analysis_weights'][stage.order]:.2f}",
                "stage_analysis_status": result["stage_analysis_statuses"][stage.order],
                "uncertainty": (
                    "Indicative field/course model. Conditional teammate bonus is excluded; "
                    "refresh for startlist, jersey, health, and tactical changes."
                ),
            }
        )

    market_order = sorted(
        (rider.rider_id for rider in snapshot.riders),
        key=lambda rider_id: (
            -snapshot.rider(rider_id).price,
            -result["season_values"][rider_id],
            snapshot.rider(rider_id).name,
        ),
    )
    for rider_id in market_order:
        rows.append({**common, "record_type": "market_rider", **_rider_columns(result, rider_id)})

    target_paths = [path]
    for target in dict.fromkeys(target_paths):
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})
    return path


def print_summary(result: dict[str, Any], output_path: Path = OUTPUT_PATH) -> None:
    snapshot = result["snapshot"]
    plan = result["plan"]
    print(
        f"Live Vuelta snapshot {result['snapshot_at']} | {len(snapshot.riders)} riders | "
        f"budget {snapshot.budget / 1_000_000:.2f}M"
    )
    print(
        f"Optimized squad {plan.total_price / 1_000_000:.2f}M / "
        f"{snapshot.budget / 1_000_000:.2f}M | base objective {plan.value:.2f} | "
        f"conditional team upside {result['conditional_team_bonus_total']:.2f} | "
        f"UAE {len(result['selected_ids'] & result['uae_ids'])} | "
        f"rated sprinters {len(result['selected_ids'] & result['sprint_ids'])}"
    )
    for rider_id in sorted(
        plan.rider_ids,
        key=lambda rider_id: result["season_values"][rider_id],
        reverse=True,
    ):
        rider = snapshot.rider(rider_id)
        news = result["news_impacts"].get(rider_id, {})
        forum = result["forum_by_id"].get(rider_id, {})
        forum_uncertainty = forum.get("uncertainty", {})
        news_flag = news.get("decision_hint", "none")
        print(
            f"  {rider.name:<28} {rider.price / 1_000_000:>4.2f}M "
            f"value={result['season_values'][rider_id]:>6.2f} "
            f"conditional={result['conditional_team_bonus_potential'][rider_id]:>5.1f} "
            f"news={news_flag} forum_risk={forum_uncertainty.get('level', 'none')}"
        )
    print("Scenarios (uncertain teammate bonuses excluded):")
    for scenario in result["scenarios"]:
        print(
            f"  {scenario['scenario']:<32} UAE={scenario['uae_count']} "
            f"objective={scenario['plan'].value:>7.2f} gap={scenario['objective_gap']:>6.2f}"
        )
    print("Captain plan:")
    for item in result["lineups"]:
        lineup = item["lineup"]
        captain = snapshot.rider(lineup.captain_id)
        print(
            f"  S{item['stage'].order:02d} {captain.name:<28} {lineup.total:>6.2f}"
        )
    print(f"Wrote {output_path}")

def main() -> None:
    result = build_live_recommendation()
    output_path = write_combined_csv(result)
    print_summary(result, output_path)


if __name__ == "__main__":
    main()
