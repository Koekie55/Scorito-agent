"""Stage-similarity predictor for future cycling stages.

Feature encoding
----------------
Each stage becomes a numeric vector with:

* profile one-hot: flat, hilly, mountain, ITT, TTT, unknown
* finish one-hot: sprint, flat, uphill, summit, technical, time-trial, unknown
* scaled distance (km / 250), vertical meters (/ 5000), and startlist size (/ 200)

Similarity metric
-----------------
The predictor computes Euclidean distance over those normalized vectors and
converts it to ``similarity = 1 / (1 + distance)``.  For a target stage, the
K most similar past stages contribute their finishers.  A rider's contribution
is ``similarity * position_score`` where ``position_score`` rewards higher
placing.  Predictions are filtered to the announced target startlist when one
is provided.  Optional tactics notes or dictionaries lightly boost riders or
teams marked as protected, leaders, aggressive, or lead-out options.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from .slugs import slug_from_name
from .store import StageStore, stage_key

PROFILE_TYPES = ("flat", "hilly", "mountain", "itt", "ttt", "unknown")
FINISH_TYPES = ("sprint", "flat", "uphill", "summit", "technical", "tt", "unknown")


def _norm_token(value: Any, allowed: tuple[str, ...]) -> str:
    token = str(value or "unknown").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "time_trial": "itt",
        "individual_time_trial": "itt",
        "team_time_trial": "ttt",
        "mountains": "mountain",
        "hills": "hilly",
        "time_trial_finish": "tt",
        "summit_finish": "summit",
        "uphill_finish": "uphill",
        "flat_finish": "flat",
    }
    token = aliases.get(token, token)
    return token if token in allowed else "unknown"


def _scaled_float(value: Any, divisor: float) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return max(0.0, float(value)) / divisor
    except (TypeError, ValueError):
        return 0.0


def _one_hot(value: str, allowed: tuple[str, ...]) -> list[float]:
    return [1.0 if value == option else 0.0 for option in allowed]


def feature_vector(stage: dict[str, Any]) -> list[float]:
    """Encode a stage into the predictor's normalized feature vector."""

    profile = _norm_token(stage.get("profile_type"), PROFILE_TYPES)
    finish = _norm_token(stage.get("finish_type"), FINISH_TYPES)
    startlist_size = len(stage.get("startlist") or stage.get("participants") or [])
    return [
        *_one_hot(profile, PROFILE_TYPES),
        *_one_hot(finish, FINISH_TYPES),
        _scaled_float(stage.get("distance_km"), 250.0),
        _scaled_float(stage.get("vertical_meters"), 5000.0),
        min(startlist_size, 200) / 200.0,
    ]


def stage_similarity(target: dict[str, Any], past: dict[str, Any]) -> float:
    """Return a 0..1-ish similarity score between target and past stages."""

    left = feature_vector(target)
    right = feature_vector(past)
    distance = math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))
    return 1.0 / (1.0 + distance)


def _iter_stages(stages_or_store: Iterable[dict[str, Any]] | StageStore | None) -> list[dict[str, Any]]:
    if stages_or_store is None:
        return StageStore().all_stages()
    if isinstance(stages_or_store, StageStore):
        return stages_or_store.all_stages()
    return list(stages_or_store)


def find_similar_stages(
    target_stage: dict[str, Any],
    stages_or_store: Iterable[dict[str, Any]] | StageStore | None = None,
    *,
    k: int = 10,
) -> list[dict[str, Any]]:
    """Return the K most similar stored stages with similarity metadata."""

    scored = []
    for stage in _iter_stages(stages_or_store):
        if not stage.get("results"):
            continue
        similarity = stage_similarity(target_stage, stage)
        enriched = dict(stage)
        enriched["similarity"] = similarity
        enriched["id"] = stage_key(stage)
        scored.append(enriched)
    return sorted(scored, key=lambda item: item["similarity"], reverse=True)[:k]


def _participant_maps(target_stage: dict[str, Any]) -> tuple[set[str], dict[str, dict[str, Any]]]:
    participants = target_stage.get("participants") or target_stage.get("startlist") or []
    allowed: set[str] = set()
    by_slug: dict[str, dict[str, Any]] = {}
    for participant in participants:
        if isinstance(participant, str):
            name = participant
            slug = slug_from_name(name)
            record = {"rider": name, "rider_slug": slug, "team": None}
        else:
            name = participant.get("rider") or participant.get("name") or ""
            slug = participant.get("rider_slug") or participant.get("slug") or (slug_from_name(name) if name else "")
            record = {"rider": name, "rider_slug": slug, "team": participant.get("team")}
        if slug:
            allowed.add(slug)
            by_slug[slug] = record
    return allowed, by_slug


def _rank_points(rank: Any) -> float:
    try:
        rank_int = int(rank)
    except (TypeError, ValueError):
        return 0.0
    if rank_int <= 0:
        return 0.0
    return 1.0 / math.sqrt(rank_int)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _tactics_boost(slug: str, name: str, team: str | None, tactics: Any) -> float:
    if not tactics:
        return 1.0
    boost = 1.0
    if isinstance(tactics, str):
        text = tactics.lower()
        name_bits = [name.lower(), slug.replace("-", " ").lower()]
        if any(bit and bit in text for bit in name_bits):
            boost *= 1.08
            if any(word in text for word in ("protected", "leader", "gc", "sprinter", "aggressive", "attack")):
                boost *= 1.08
        if team and team.lower() in text and any(word in text for word in ("leadout", "leader", "protected", "aggressive")):
            boost *= 1.05
        return boost
    if not isinstance(tactics, dict):
        return boost
    protected = {slug_from_name(str(item)) for item in _as_list(tactics.get("protected_riders") or tactics.get("leaders"))}
    aggressive = {slug_from_name(str(item)) for item in _as_list(tactics.get("aggressive_riders") or tactics.get("attackers"))}
    leadout = {slug_from_name(str(item)) for item in _as_list(tactics.get("leadout_riders") or tactics.get("sprinters"))}
    if slug in protected:
        boost *= 1.20
    if slug in aggressive:
        boost *= 1.12
    if slug in leadout:
        boost *= 1.10
    team_tactics = tactics.get("teams") or {}
    if team and isinstance(team_tactics, dict):
        note = str(team_tactics.get(team) or team_tactics.get(team.lower()) or "").lower()
        if any(word in note for word in ("protected", "leader", "leadout", "aggressive", "attack")):
            boost *= 1.08
    return boost


def predict_finishers(
    target_stage: dict[str, Any],
    stages_or_store: Iterable[dict[str, Any]] | StageStore | None = None,
    *,
    k: int = 10,
    top_n: int = 20,
    tactics: Any | None = None,
) -> dict[str, Any]:
    """Predict likely top finishers for a target stage from similar past stages."""

    similar_stages = find_similar_stages(target_stage, stages_or_store, k=k)
    allowed_slugs, participant_by_slug = _participant_maps(target_stage)
    effective_tactics = tactics if tactics is not None else target_stage.get("tactics") or target_stage.get("tactics_notes")
    scores: dict[str, float] = defaultdict(float)
    riders: dict[str, dict[str, Any]] = {}
    support: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for stage in similar_stages:
        similarity = float(stage.get("similarity") or 0.0)
        for result in stage.get("results", []):
            name = result.get("rider") or result.get("name") or ""
            slug = result.get("rider_slug") or result.get("slug") or (slug_from_name(name) if name else "")
            if not slug or (allowed_slugs and slug not in allowed_slugs):
                continue
            participant = participant_by_slug.get(slug, {})
            team = participant.get("team") or result.get("team")
            boost = _tactics_boost(slug, name, team, effective_tactics)
            contribution = similarity * _rank_points(result.get("rank")) * boost
            if contribution <= 0:
                continue
            scores[slug] += contribution
            riders[slug] = {
                "rider": participant.get("rider") or name,
                "rider_slug": slug,
                "team": team,
            }
            support[slug].append(
                {
                    "stage_id": stage.get("id"),
                    "race": stage.get("race"),
                    "stage_no": stage.get("stage_no"),
                    "rank": result.get("rank"),
                    "similarity": similarity,
                    "contribution": contribution,
                }
            )

    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_n]
    predictions = []
    for index, (slug, score) in enumerate(ordered, start=1):
        record = dict(riders[slug])
        record.update({"predicted_rank": index, "score": score, "supporting_stages": support[slug]})
        predictions.append(record)
    return {"predictions": predictions, "similar_stages": similar_stages, "feature_vector": feature_vector(target_stage)}
