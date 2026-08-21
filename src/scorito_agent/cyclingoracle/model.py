"""Transparent approximation of CyclingOracle's stage prediction model.

CyclingOracle does not publish its learned model, but its rider pages explain
the inputs: three years of UCI results, recency/form bonuses, field strength,
race class, stage profile and 13 rider skills (sprint, flat, mountain, hill,
time-trial variants, cobble, lead-out, GC, one-day and stage-race ability).

This reimplementation uses the exposed rider-card skills and data-list scores
as features, then applies profile-specific linear weights.  The weights below
were inferred from the public methodology text plus several live prediction
pages cached in ``data/cyclingoracle``:

* flat sprint: SPR/flat/lead-out dominate, with OVR and form as tie-breakers.
* hilly sprint/classic: HLL, one-day, sprint, flat and cobble all matter.
* mountain stage: MTN + GC + stage-race skill dominate, with form important.
* time trial: ITT plus prologue/short/long TT variants dominate.
* cobbled one-day: COB + one-day + HLL/flat.

The result is not a clone of the private model; it is a documented, portable
proxy that ranks future starters when only rider skills/profile text are
available.  When a known CyclingOracle prediction page is supplied, callers can
use :func:`best_weight_profile` to choose the profile whose weighted score best
matches the published Expected Win ordering.
"""

from __future__ import annotations

import math
import unicodedata
from typing import Any

PROFILE_WEIGHTS: dict[str, dict[str, float]] = {
    "flat_sprint": {
        "spr": 0.38,
        "flat": 0.18,
        "leadout": 0.08,
        "ovr": 0.16,
        "one_day": 0.05,
        "hll": 0.05,
        "form": 0.10,
    },
    "hilly_sprint": {
        "hll": 0.32,
        "one_day": 0.22,
        "ovr": 0.18,
        "leadout": 0.09,
        "spr": 0.08,
        "mtn": 0.06,
        "flat": 0.03,
        "form": 0.02,
    },
    "hilly": {
        "hll": 0.34,
        "one_day": 0.20,
        "ovr": 0.16,
        "mtn": 0.08,
        "itt": 0.05,
        "spr": 0.07,
        "form": 0.10,
    },
    "mountain": {
        "mtn": 0.36,
        "gc": 0.22,
        "stage_race": 0.09,
        "ovr": 0.13,
        "hll": 0.08,
        "spr": 0.03,
        "form": 0.09,
    },
    "hilly_mountain": {
        "mtn": 0.28,
        "hll": 0.20,
        "gc": 0.18,
        "ovr": 0.13,
        "stage_race": 0.08,
        "spr": 0.04,
        "form": 0.09,
    },
    "time_trial": {
        "itt": 0.46,
        "short_itt": 0.12,
        "long_itt": 0.12,
        "prologue": 0.08,
        "ovr": 0.12,
        "flat": 0.05,
        "form": 0.05,
    },
    "cobble": {
        "cob": 0.36,
        "one_day": 0.20,
        "hll": 0.14,
        "flat": 0.10,
        "spr": 0.08,
        "ovr": 0.07,
        "form": 0.05,
    },
    "mixed": {
        "ovr": 0.24,
        "hll": 0.16,
        "mtn": 0.14,
        "spr": 0.14,
        "gc": 0.12,
        "one_day": 0.10,
        "form": 0.10,
    },
}

FALLBACK_FEATURES: dict[str, tuple[str, ...]] = {
    "flat": ("ovr",),
    "leadout": ("spr", "flat"),
    "one_day": ("hll", "cob", "ovr"),
    "stage_race": ("gc", "mtn", "ovr"),
    "short_itt": ("itt",),
    "long_itt": ("itt",),
    "prologue": ("itt", "spr"),
    "form": (),
}


def score_rider(
    rider: dict[str, Any],
    *,
    profile: str = "mixed",
    weights: dict[str, float] | None = None,
) -> float:
    """Return a 0-100-ish model score for a rider on a given profile."""

    features = extract_features(rider)
    if not features and rider.get("win_probability_pct") is not None:
        return float(rider["win_probability_pct"])
    selected = weights or PROFILE_WEIGHTS.get(profile) or PROFILE_WEIGHTS["mixed"]
    score = 0.0
    total_weight = 0.0
    for feature, weight in selected.items():
        value = feature_value(features, feature)
        score += value * weight
        total_weight += weight
    return score / total_weight if total_weight else 0.0


def rank_riders(
    riders: list[dict[str, Any]] | dict[str, dict[str, Any]],
    *,
    stage: dict[str, Any] | str | None = None,
    profile: str | None = None,
    weights: dict[str, float] | None = None,
    temperature: float = 7.5,
) -> list[dict[str, Any]]:
    """Rank riders and attach model probabilities.

    ``riders`` may be a list of rider dictionaries or the feature table
    returned by ``scraper.merge_feature_rows``.
    """

    rider_list = list(riders.values()) if isinstance(riders, dict) else list(riders)
    profile_name = profile or profile_from_stage(stage)
    scored: list[dict[str, Any]] = []
    for rider in rider_list:
        model_score = score_rider(rider, profile=profile_name, weights=weights)
        row = {
            **{key: value for key, value in rider.items() if key != "raw"},
            "stage_profile": profile_name,
            "model_score": model_score,
        }
        scored.append(row)

    scored.sort(key=lambda row: row["model_score"], reverse=True)
    probabilities = _softmax([row["model_score"] for row in scored], temperature=temperature)
    for rank, (row, probability) in enumerate(zip(scored, probabilities, strict=True), start=1):
        row["model_rank"] = rank
        row["model_probability"] = probability
        row["model_probability_pct"] = probability * 100.0
    return scored


def extract_features(rider: dict[str, Any]) -> dict[str, float]:
    """Accept either scraper rider_stats rows or feature-table entries."""

    raw_features: dict[str, Any] = {}
    raw_features.update(rider.get("features") or {})
    raw_features.update(rider.get("stats") or {})
    for key, value in rider.items():
        if key.startswith("skill_"):
            raw_features[key.removeprefix("skill_")] = value
        elif key in {
            "ovr",
            "cob",
            "hll",
            "mtn",
            "gc",
            "itt",
            "spr",
            "flat",
            "leadout",
            "one_day",
            "stage_race",
            "prologue",
            "short_itt",
            "long_itt",
            "form",
        }:
            raw_features[key] = value
    return {
        key: float(value)
        for key, value in raw_features.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def feature_value(features: dict[str, float], feature: str, neutral: float = 50.0) -> float:
    """Feature lookup with transparent fallbacks for unpublished skills."""

    if feature in features:
        return features[feature]
    fallback_keys = FALLBACK_FEATURES.get(feature, ())
    fallback_values = [features[key] for key in fallback_keys if key in features]
    if fallback_values:
        return sum(fallback_values) / len(fallback_values)
    if "ovr" in features and feature != "form":
        return features["ovr"]
    return neutral


def profile_from_stage(stage: dict[str, Any] | str | None) -> str:
    """Return the model profile from parser metadata or free text."""

    if isinstance(stage, dict):
        profile = stage.get("profile") or stage.get("stage_profile")
        if profile:
            return str(profile)
        text = " ".join(str(stage.get(key) or "") for key in ("title", "stage_title", "route_text"))
    else:
        text = str(stage or "")
    if not text:
        return "mixed"

    # Import lazily to keep model.py independent and avoid circular imports.
    try:
        from scorito_agent.cyclingoracle.scraper import classify_stage_text

        return classify_stage_text(text)
    except Exception:  # pragma: no cover
        return "mixed"


def best_weight_profile(
    site_predictions: list[dict[str, Any]],
    feature_table: dict[str, dict[str, Any]] | list[dict[str, Any]],
) -> tuple[str, float]:
    """Pick the predefined profile with best Spearman match to site xW ranks."""

    features_by_key = _feature_index(feature_table)
    comparable: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for prediction in site_predictions:
        key = str(prediction.get("rider_id") or "")
        feature_row = features_by_key.get(key) or features_by_key.get(_name_key(prediction.get("rider_name")))
        if feature_row:
            comparable.append((prediction, feature_row))
    if len(comparable) < 3:
        return "mixed", 0.0

    observed = [
        float(prediction.get("win_probability_pct") or 0.0)
        for prediction, _feature_row in comparable
    ]
    best_profile = "mixed"
    best_corr = -2.0
    for profile, weights in PROFILE_WEIGHTS.items():
        modeled = [score_rider(feature_row, profile=profile, weights=weights) for _prediction, feature_row in comparable]
        corr = spearman_correlation(observed, modeled)
        if corr > best_corr:
            best_profile = profile
            best_corr = corr
    return best_profile, best_corr


def _feature_index(
    feature_table: dict[str, dict[str, Any]] | list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    rows = list(feature_table.values()) if isinstance(feature_table, dict) else feature_table
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("rider_id"):
            index[str(row["rider_id"])] = row
        if row.get("rider_name"):
            index[_name_key(row["rider_name"])] = row
    return index


def _name_key(name: Any) -> str:
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return "".join(ch for ch in text if ch.isalnum())


def spearman_correlation(a: list[float], b: list[float]) -> float:
    """Small dependency-free Spearman rank correlation."""

    if len(a) != len(b) or len(a) < 2:
        return 0.0
    ra = _ranks(a)
    rb = _ranks(b)
    mean_a = sum(ra) / len(ra)
    mean_b = sum(rb) / len(rb)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb, strict=True))
    var_a = sum((x - mean_a) ** 2 for x in ra)
    var_b = sum((y - mean_b) ** 2 for y in rb)
    if not var_a or not var_b:
        return 0.0
    return cov / math.sqrt(var_a * var_b)


def _ranks(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda pair: pair[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        rank = (i + j + 2) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = rank
        i = j + 1
    return ranks


def _softmax(values: list[float], *, temperature: float) -> list[float]:
    if not values:
        return []
    scale = max(temperature, 0.001)
    max_value = max(values)
    exps = [math.exp((value - max_value) / scale) for value in values]
    total = sum(exps)
    return [value / total for value in exps]
