"""Build field- and course-adjusted Vuelta 2026 rider values from live PCS data.

The output is an evidence projection, not a live-price recommendation. The separate
``recommend_vuelta_live.py`` command applies these values to market 310 prices,
official Scorito qualities, team bonuses, squad constraints, and stage lineups.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scorito_agent.breakaway import (  # noqa: E402
    historical_breakaway_prior,
    summit_breakaway_rider_factor,
)
from scorito_agent.pcs.fetcher import (  # noqa: E402
    PCS_BASE_URL,
    fetch_race_startlist,
    fetch_stage_page,
    fetch_url,
    race_startlist_url,
    rider_page_url,
    stage_page_url,
)
from scorito_agent.pcs.parse import (
    is_team_time_trial,
    parse_results,
    parse_rider_page,
    parse_stage_page,
    parse_startlist,
)  # noqa: E402
from scorito_agent.pcs.predict import predict_finishers  # noqa: E402
from scorito_agent.pcs.store import StageStore  # noqa: E402
from scorito_agent.scorito import (  # noqa: E402
    CAPABILITY_AUDIT_FIELDNAMES,
    Rider,
    Snapshot,
    Stage,
    best_stage_lineup,
    capability_audit_fields,
    capability_set,
    joint_enrolled_squad,
    load_snapshot,
    raw_capability_signals,
)
from scorito_agent.scorito.external import RankPointsCurve  # noqa: E402

RACE_SLUG = "vuelta-a-espana"
YEAR = 2026
MARKET_ID = 310
BUDGET = 45_000_000
SQUAD_SIZE = 20
LINEUP_SIZE = 9
CAPTAIN_FACTOR = 2
MAX_RIDERS_PER_TEAM = 4
MIN_SPRINT_OPTIONS = 5
RECENT_YEARS = (2026, 2025, 2024)
RECENCY_WEIGHTS = {2026: 0.60, 2025: 0.27, 2024: 0.13}

RANKING_VIEWS = {
    "overall": "top-competitors",
    "form": "form",
    "gc": "top-gc-riders",
    "climb": "climbers",
    "sprint": "sprinters",
    "tt": "tt-specialists",
    "prologue": "prologue-specialists",
    "classic": "classic-riders",
    "previous_vuelta": "previous-performance",
}

PRICE_OVERRIDES = {
    "tadej-pogacar": 8_500_000,
    "joao-almeida": 5_500_000,
    "primoz-roglic": 6_000_000,
    "mattias-skjelmose-jensen": 5_000_000,
    "mads-pedersen": 4_500_000,
    "wout-van-aert": 4_500_000,
    "felix-gall": 4_000_000,
    "carlos-rodriguez-cano": 3_000_000,
    "enric-mas": 3_000_000,
    "mikel-landa": 2_500_000,
    "matthew-brennan": 3_000_000,
    "jay-vine": 3_000_000,
    "matthew-riccitello": 2_750_000,
    "ben-tulett": 2_750_000,
    "harold-tejada": 2_500_000,
    "cian-uijtdebroeks": 2_500_000,
    "luke-plapp": 2_500_000,
    "filippo-zana": 2_250_000,
    "raul-garcia-pierna": 1_500_000,
    "ivan-romeo": 1_500_000,
    "pablo-castrillo-zapater": 1_500_000,
    "pau-miquel": 1_500_000,
    "jesus-herrada-lopez": 1_000_000,
    "david-de-la-cruz": 750_000,
}

# A 4-rider trade-team cap is enforced as the likely Grand Tour rule.  Saved
# cycling market metadata does not expose this field and jvdlaar/scorito does
# not implement selection, so this remains an explicit assumption until market
# 310 opens and its rules can be checked directly.
TEAM_LIMIT_EVIDENCE = (
    "likely cap of 4: user-confirmed that 5 is illegal; no numeric cycling cap "
    "exists in saved market metadata or jvdlaar/scorito prior art"
)

UAE_TEAM = "UAE Team Emirates - XRG (WT)"
UAE_PRICE_FLOORS = {
    "jay-vine": 2_500_000,
    "pavel-sivakov": 2_500_000,
    "kevin-vermaerke": 2_500_000,
    "pablo-torres-arias": 2_000_000,
    "ivo-emanuel-alves": 2_000_000,
    "domen-novak": 2_000_000,
}

# The provisional list is incomplete, so every
# entrant carries explicit participation uncertainty.  No rider is excluded by
# name: only objective non-starter/injury evidence may make a rider unavailable.
MODEL_VERSION = "pcs-scorito-evidence-v5"
EVIDENCE_SCHEMA_VERSION = 2
STARTLIST_CERTAINTY = 0.72
MIN_PROVISIONAL_STARTERS = 70
UNAVAILABLE_RIDERS: dict[str, dict[str, str]] = {}

def _startlist_status(count: int, *, reused: bool = False) -> str:
    if count == 184:
        status = "PCS full-sized 184-rider start list; final availability still monitored"
    else:
        status = f"PCS provisional {count}-rider start list; field may be incomplete"
    if reused:
        status += "; recent-result evidence reused from the preceding projection"
    return status
PROFILE_TYPES = ("flat", "hilly", "mountain", "itt")
# A TTT may be a valid *source* profile for evidence even though the Vuelta has
# no TTT stage to predict, so it is not a target in PROFILE_TYPES.
SOURCE_PROFILE_TYPES = PROFILE_TYPES + ("ttt",)
# A team time trial is a team-paced effort, so it carries only weak evidence of
# individual time-trial ability. Use the generic cross-profile floor until the
# transfer can be calibrated against subsequent individual time trials.
TTT_TO_ITT_TRANSFER = 0.08
RACE_QUALITY_WEIGHTS = {
    "uwt": 1.00,
    "pro": 0.82,
    "2.1": 0.68,
    "1.1": 0.68,
    "nc": 0.66,
    "2.2": 0.52,
    "1.2": 0.52,
}
RESULT_CONTEXT_LIMIT = 6
RESULT_CONTEXT_CACHE = ROOT / "data" / "pcs" / "vuelta2026_result_contexts_v2.json"
FIELD_QUALITY_REFERENCE = 1_600.0
RESULT_CONTEXT_FIELDS = (
    "profile_type",
    "finish_type",
    "distance_km",
    "vertical_meters",
    "profile_score",
    "gradient_final_km",
    "race_ranking",
    "startlist_quality_score",
    "startlist_quality_finish_score",
    "startlist_count",
    "result_count",
)
MODEL_QUALITY_NAMES = {
    0: "gc",
    1: "climb",
    2: "time_trial",
    3: "sprint",
    4: "punch",
    5: "hill",
    6: "cobbles",
}


def _ranking_url(view: str) -> str:
    return f"{PCS_BASE_URL}/race/{RACE_SLUG}/{YEAR}/startlist/{view}"




def _signal(rows: list[dict[str, Any]], *, scale: float, floor: float = 0.0) -> dict[str, float]:
    values: dict[str, float] = {}
    for row in rows:
        slug = str(row.get("rider_slug") or "")
        rank = row.get("rank")
        if not slug or not isinstance(rank, int) or rank <= 0:
            continue
        values[slug] = max(floor, math.exp(-(rank - 1) / scale))
    return values


def _load_rankings() -> tuple[dict[str, dict[str, float]], dict[str, str]]:
    rankings: dict[str, dict[str, float]] = {}
    urls: dict[str, str] = {}
    scales = {
        "overall": 28.0,
        "form": 20.0,
        # These pages rank the provisional starters relative to one another.
        # A short decay prevents the bottom of a provisional specialty list
        # from retaining most of the winner's signal.
        "gc": 18.0,
        "climb": 22.0,
        "sprint": 22.0,
        "tt": 22.0,
        "prologue": 20.0,
        "classic": 24.0,
        "previous_vuelta": 18.0,
    }
    for key, view in RANKING_VIEWS.items():
        url = _ranking_url(view)
        rows = parse_results(fetch_url(url, cache=True))
        rankings[key] = _signal(rows, scale=scales[key])
        urls[key] = url
    return rankings, urls


def _age_factor(age: int | None) -> float:
    """Modest age trajectory prior; never an availability/exclusion rule."""
    if age is None or age <= 33:
        return 1.0
    return {34: 0.99, 35: 0.97, 36: 0.94, 37: 0.90, 38: 0.85}.get(age, 0.79)


def _race_quality(result: dict[str, Any]) -> float:
    text = f"{result.get('race_class') or ''} {result.get('event') or ''}".lower()
    return next((weight for token, weight in RACE_QUALITY_WEIGHTS.items() if token in text), 0.58)


def _placing_strength(rank: int) -> float:
    return math.exp(-(max(rank, 1) - 1) / 11.5)


def _absolute_result_url(value: Any) -> str | None:
    url = str(value or "").strip()
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"{PCS_BASE_URL}/{url.lstrip('/')}"


def _context_candidate_score(result: dict[str, Any]) -> float:
    placing = _placing_strength(int(result["rank"]))
    pcs_points = max(0, int(result.get("pcs_points") or 0))
    pcs_signal = min(1.0, math.log1p(pcs_points) / math.log(201.0))
    return 0.78 * _race_quality(result) * placing + 0.22 * pcs_signal


def _load_result_contexts(
    results_by_slug: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    urls: set[str] = set()
    for results in results_by_slug.values():
        candidates = sorted(results, key=_context_candidate_score, reverse=True)
        for result in candidates[:RESULT_CONTEXT_LIMIT]:
            if url := _absolute_result_url(result.get("result_url")):
                urls.add(url)

    contexts: dict[str, dict[str, Any]] = {}
    if RESULT_CONTEXT_CACHE.exists():
        cached = json.loads(RESULT_CONTEXT_CACHE.read_text(encoding="utf-8"))
        contexts = {
            url: row
            for url, row in cached.items()
            if isinstance(row, dict) and isinstance(row.get("_results"), list)
        }
    ordered_urls = sorted(urls)
    pending_urls = [url for url in ordered_urls if url not in contexts]
    print(
        f"PCS result context cache: {len(ordered_urls) - len(pending_urls)} retained, "
        f"{len(pending_urls)} pending"
    )
    def parse_context(url: str) -> tuple[str, dict[str, Any]]:
        parsed = parse_stage_page(fetch_url(url, cache=True), source_url=url)
        context = {key: parsed.get(key) for key in RESULT_CONTEXT_FIELDS}
        context["_results"] = parsed.get("results", [])
        return url, context

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(parse_context, url): url for url in pending_urls}
        for index, future in enumerate(as_completed(futures), start=1):
            url = futures[future]
            try:
                parsed_url, context = future.result()
            except RuntimeError as exc:
                print(
                    f"PCS result context unavailable {index}/{len(pending_urls)}: "
                    f"{url} ({exc})"
                )
                continue
            contexts[parsed_url] = context
            if index == 1 or index % 5 == 0 or index == len(pending_urls):
                RESULT_CONTEXT_CACHE.parent.mkdir(parents=True, exist_ok=True)
                temporary = RESULT_CONTEXT_CACHE.with_name(
                    f"{RESULT_CONTEXT_CACHE.stem}.{os.getpid()}.tmp"
                )
                temporary.write_text(
                    json.dumps(contexts, ensure_ascii=False), encoding="utf-8"
                )
                temporary.replace(RESULT_CONTEXT_CACHE)
                print(f"PCS result context {index:>3}/{len(pending_urls)}")
    return {url: contexts[url] for url in ordered_urls if url in contexts}


def _gap_seconds(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if not all(part.isdigit() for part in parts):
        return None
    values = [int(part) for part in parts]
    if len(values) == 2:
        return values[0] * 60 + values[1]
    if len(values) == 3:
        return values[0] * 3600 + values[1] * 60 + values[2]
    return None


def _finish_group_context(
    results: list[dict[str, Any]], rider_slug: str
) -> dict[str, Any]:
    rider = next(
        (row for row in results if row.get("rider_slug") == rider_slug), None
    )
    if rider is None:
        return {}
    rank = int(rider["rank"])
    rider_time = str(rider.get("time") or "")
    if rank == 1:
        gap_to_winner = 0
        group_size = 1 + sum(
            row.get("rank") != 1 and str(row.get("time") or "") == "0:00"
            for row in results
        )
        second_gap = next(
            (
                _gap_seconds(row.get("time"))
                for row in results
                if row.get("rank") == 2
            ),
            None,
        )
        winning_margin = second_gap
    else:
        gap_to_winner = _gap_seconds(rider_time)
        group_size = sum(
            str(row.get("time") or "") == rider_time for row in results
        )
        if rider_time == "0:00":
            group_size += 1
        winning_margin = None
    group_type = (
        "bunch"
        if group_size >= 8
        else "reduced"
        if group_size >= 3
        else "small"
        if group_size == 2
        else "solo"
    )
    return {
        "gap_to_winner_seconds": gap_to_winner,
        "finish_group_size": group_size,
        "finish_group_type": group_type,
        "winning_margin_seconds": winning_margin,
    }


def _field_strength(result: dict[str, Any]) -> float:
    context = result.get("course_context") or {}
    class_strength = _race_quality(result)
    quality_score = context.get("startlist_quality_score")
    if not isinstance(quality_score, (int, float)) or quality_score <= 0:
        return 0.88 * class_strength

    quality_strength = min(1.0, math.sqrt(float(quality_score) / FIELD_QUALITY_REFERENCE))
    startlist_count = context.get("startlist_count")
    size_strength = (
        min(1.0, math.sqrt(float(startlist_count) / 160.0))
        if isinstance(startlist_count, (int, float)) and startlist_count > 0
        else 0.72
    )
    race_ranking = context.get("race_ranking")
    ranking_strength = (
        math.exp(-(float(race_ranking) - 1.0) / 90.0)
        if isinstance(race_ranking, (int, float)) and race_ranking > 0
        else 0.55
    )
    return min(
        1.0,
        0.70 * quality_strength
        + 0.15 * class_strength
        + 0.10 * size_strength
        + 0.05 * ranking_strength,
    )


def _history_profile_lookup(history: list[dict[str, Any]]) -> dict[tuple[str, int, int], tuple[str, str]]:
    aliases = {"tdf2026": "tour-de-france", "giro2026": "giro-d-italia"}
    out: dict[tuple[str, int, int], tuple[str, str]] = {}
    for stage in history:
        race = aliases.get(str(stage.get("race")), str(stage.get("race") or ""))
        year = int(stage.get("year") or 0)
        number = stage.get("stage_no")
        if race and year and isinstance(number, int):
            out[(race, year, number)] = (
                str(stage.get("profile_type") or "unknown"),
                str(stage.get("finish_type") or "unknown"),
            )
    return out


def _result_profile(
    result: dict[str, Any],
    lookup: dict[tuple[str, int, int], tuple[str, str]],
) -> tuple[str, str, str]:
    race = str(result.get("race") or "")
    if "classification" in race.lower():
        return "gc", "classification", "PCS classification result"

    # Guard ahead of the cached course profile: stores built before the TTT
    # classifier fix label team time trials as individual ones.
    if is_team_time_trial(race):
        return "ttt", "tt", "TTT marker on PCS result"

    context = result.get("course_context") or {}
    profile = str(context.get("profile_type") or "")
    if profile in SOURCE_PROFILE_TYPES:
        return (
            profile,
            str(context.get("finish_type") or "unknown"),
            "exact PCS result-page course profile",
        )

    url = str(result.get("result_url") or "")
    match = re.search(r"race/([^/]+)/(20\d{2})/stage-(\d+)", url)
    if match:
        key = (match.group(1), int(match.group(2)), int(match.group(3)))
        if key in lookup:
            saved_profile, finish = lookup[key]
            return saved_profile, finish, "matched saved PCS/Scorito stage profile"
    if "/ttt" in url.lower():
        return "ttt", "tt", "TTT marker on PCS result"
    if "(itt)" in race.lower() or "/itt" in url.lower():
        return "itt", "tt", "ITT marker on PCS result"
    event = str(result.get("event") or "").lower()
    if "championship" in race.lower() or re.search(r"\(1\.(?:uwt|pro|1|2)\)", event):
        return "hilly", "one-day", "one-day race proxy; exact profile unavailable"
    return "unknown", "unknown", "result profile unavailable"


def _profile_transfer(source: str, target: str) -> float:
    if source == target:
        return 1.0
    if {source, target} == {"ttt", "itt"}:
        return TTT_TO_ITT_TRANSFER
    if source == "gc" and target == "mountain":
        return 0.72
    if {source, target} == {"hilly", "mountain"}:
        return 0.55
    if {source, target} == {"flat", "hilly"}:
        return 0.40
    if source == "unknown":
        return 0.22
    return 0.08


def _metric_similarity(source: Any, target: Any, *, offset: float) -> float | None:
    if not isinstance(source, (int, float)) or not isinstance(target, (int, float)):
        return None
    if source < 0 or target < 0:
        return None
    return math.exp(-abs(math.log((float(source) + offset) / (float(target) + offset))))


def _course_similarity(result: dict[str, Any], target_stage: dict[str, Any]) -> float:
    source_profile = str(result.get("profile_type") or "unknown")
    target_profile = str(target_stage.get("profile_type") or "unknown")
    transfer = _profile_transfer(source_profile, target_profile)
    context = result.get("course_context") or {}
    metric_similarities = [
        _metric_similarity(context.get("distance_km"), target_stage.get("distance_km"), offset=35.0),
        _metric_similarity(context.get("vertical_meters"), target_stage.get("vertical_meters"), offset=600.0),
        _metric_similarity(context.get("profile_score"), target_stage.get("profile_score"), offset=45.0),
        _metric_similarity(
            context.get("gradient_final_km"),
            target_stage.get("gradient_final_km"),
            offset=2.0,
        ),
    ]
    available = [value for value in metric_similarities if value is not None]
    metric_score = sum(available) / len(available) if available else 0.72
    source_finish = str(result.get("finish_type") or "unknown")
    target_finish = str(target_stage.get("finish_type") or "unknown")
    finish_score = 1.0 if source_finish == target_finish else 0.82
    return transfer * (0.52 + 0.38 * metric_score + 0.10 * finish_score)


def _profile_course_factor(result: dict[str, Any], target_profile: str) -> float:
    transfer = _profile_transfer(str(result.get("profile_type") or "unknown"), target_profile)
    context = result.get("course_context") or {}
    if target_profile == "mountain":
        difficulty = max(
            min(1.0, float(context.get("profile_score") or 0) / 380.0),
            min(1.0, float(context.get("vertical_meters") or 0) / 4800.0),
        )
    elif target_profile == "hilly":
        difficulty = max(
            min(1.0, float(context.get("profile_score") or 0) / 160.0),
            min(1.0, float(context.get("vertical_meters") or 0) / 3200.0),
        )
    else:
        difficulty = 0.85 if context else 0.55
    return transfer * (0.78 + 0.22 * difficulty)


def _season_result_strength(results: list[dict[str, Any]]) -> float:
    values = [
        float(row["field_strength"]) * float(row["placing_strength"])
        for row in results
        if row.get("date") and isinstance(row.get("rank"), int)
    ]
    return sum(sorted(values, reverse=True)[:8]) / 3.6


def _recent_course_strength(evidence: dict[str, Any], stage: dict[str, Any]) -> float:
    values = [
        RECENCY_WEIGHTS[int(result["year"])]
        * float(result["field_strength"])
        * float(result["placing_strength"])
        * _course_similarity(result, stage)
        for result in evidence.get("contextual_results", [])
    ]
    return sum(sorted(values, reverse=True)[:8]) / 2.1


def _load_rider_evidence(
    startlist: list[dict[str, Any]],
    history: list[dict[str, Any]] | None = None,
    existing_evidence: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    profile_lookup = _history_profile_lookup(history or [])
    raw_by_slug: dict[str, list[dict[str, Any]]] = {}
    rider_meta: dict[str, dict[str, Any]] = {}
    current_slugs = {str(rider["rider_slug"]) for rider in startlist}
    evidence = {
        slug: dict(row)
        for slug, row in (existing_evidence or {}).items()
        if slug in current_slugs
    }
    for row in evidence.values():
        row["startlist_certainty"] = STARTLIST_CERTAINTY
        row["startlist_status"] = (
            _startlist_status(len(startlist), reused=True)
        )
    pending = [rider for rider in startlist if rider["rider_slug"] not in evidence]
    print(
        f"PCS rider evidence reuse: {len(evidence)} retained, "
        f"{len(pending)} new or uncached"
    )

    for index, rider in enumerate(pending, start=1):
        slug = rider["rider_slug"]
        all_results: list[dict[str, Any]] = []
        age: int | None = None
        birth_date: str | None = None
        for year in RECENT_YEARS:
            url = f"{rider_page_url(slug=slug)}/{year}"
            page = parse_rider_page(fetch_url(url, cache=True), source_url=url)
            age = age if age is not None else page.get("age")
            birth_date = birth_date or page.get("birth_date")
            for result in page["recent_results"]:
                if result.get("date") and isinstance(result.get("rank"), int):
                    all_results.append(dict(result))
        raw_by_slug[slug] = all_results
        rider_meta[slug] = {"age": age, "birth_date": birth_date}
        print(f"PCS rider results {index:>2}/{len(pending)}: {rider['rider']:<28} rows={len(all_results):>3}")

    contexts = _load_result_contexts(raw_by_slug)
    for index, rider in enumerate(pending, start=1):
        slug = rider["rider_slug"]
        enriched_results: list[dict[str, Any]] = []
        for result in raw_by_slug[slug]:
            enriched = dict(result)
            result_url = _absolute_result_url(enriched.get("result_url"))
            if result_url:
                enriched["source_url"] = result_url
                if result_url in contexts:
                    context = contexts[result_url]
                    enriched["course_context"] = {
                        key: value
                        for key, value in context.items()
                        if key != "_results"
                    }
                    enriched.update(
                        _finish_group_context(context.get("_results", []), slug)
                    )
            profile, finish, basis = _result_profile(enriched, profile_lookup)
            enriched.update(
                {
                    "profile_type": profile,
                    "finish_type": finish,
                    "profile_basis": basis,
                    "race_quality": round(_race_quality(enriched), 4),
                    "field_strength": round(_field_strength(enriched), 4),
                    "placing_strength": round(_placing_strength(int(enriched["rank"])), 4),
                }
            )
            enriched_results.append(enriched)

        yearly: dict[int, dict[str, Any]] = {}
        for year in RECENT_YEARS:
            rows = [row for row in enriched_results if int(row.get("year") or 0) == year]
            ranks = [int(row["rank"]) for row in rows]
            exact_context = sum(bool(row.get("course_context")) for row in rows)
            yearly[year] = {
                "starts": len(ranks),
                "wins": sum(rank == 1 for rank in ranks),
                "top_10": sum(rank <= 10 for rank in ranks),
                "top_20": sum(rank <= 20 for rank in ranks),
                "best_result": min(ranks, default=None),
                "strength": round(_season_result_strength(rows), 4),
                "exact_field_course_context": exact_context,
                "source_url": f"{rider_page_url(slug=slug)}/{year}",
            }
        recency_score = sum(
            RECENCY_WEIGHTS[year] * float(yearly[year]["strength"])
            for year in RECENT_YEARS
        )

        profile_strength: dict[str, float] = {}
        profile_confidence: dict[str, float] = {}
        strongest_by_profile: dict[str, list[dict[str, Any]]] = {}
        for target in PROFILE_TYPES:
            candidates_by_result: dict[str, tuple[float, float, dict[str, Any]]] = {}
            for result in enriched_results:
                year = int(result["year"])
                transfer = _profile_course_factor(result, target)
                contribution = (
                    RECENCY_WEIGHTS[year]
                    * float(result["field_strength"])
                    * float(result["placing_strength"])
                    * transfer
                )
                key = str(
                    result.get("source_url")
                    or (result.get("date"), result.get("race"), result.get("rank"))
                )
                existing = candidates_by_result.get(key)
                if existing is None or contribution > existing[0]:
                    candidates_by_result[key] = (contribution, transfer, result)
            candidates = sorted(candidates_by_result.values(), key=lambda item: item[0], reverse=True)
            top = candidates[:8]
            profile_strength[target] = round(sum(item[0] for item in top) / 2.1, 4)
            direct = [item for item in top if item[1] >= 0.7]
            exact = [item for item in direct if item[2].get("course_context")]
            profile_confidence[target] = round(
                min(
                    0.94,
                    0.18
                    + 0.065 * len(direct)
                    + 0.035 * len(exact)
                    + 0.16 * sum(item[0] for item in top),
                ),
                3,
            )
            strongest_by_profile[target] = [
                {
                    "year": int(item[2]["year"]),
                    "rank": int(item[2]["rank"]),
                    "race": item[2]["race"],
                    "event": item[2].get("event"),
                    "profile_type": item[2]["profile_type"],
                    "finish_type": item[2]["finish_type"],
                    "profile_basis": item[2]["profile_basis"],
                    "field_strength": item[2]["field_strength"],
                    "startlist_quality_score": (item[2].get("course_context") or {}).get("startlist_quality_score"),
                    "startlist_count": (item[2].get("course_context") or {}).get("startlist_count"),
                    "profile_score": (item[2].get("course_context") or {}).get("profile_score"),
                    "vertical_meters": (item[2].get("course_context") or {}).get("vertical_meters"),
                    "distance_km": (item[2].get("course_context") or {}).get("distance_km"),
                    "transfer": round(item[1], 3),
                    "contribution": round(item[0], 4),
                    "source_url": item[2].get("source_url"),
                }
                for item in top[:3]
            ]

        contextual_results = sorted(
            enriched_results,
            key=lambda row: float(row["field_strength"]) * float(row["placing_strength"]),
            reverse=True,
        )[:RESULT_CONTEXT_LIMIT]
        strength_2026 = float(yearly[2026]["strength"])
        strength_2025 = float(yearly[2025]["strength"])
        trend_factor = max(0.90, min(1.08, 0.98 + 0.10 * (strength_2026 - strength_2025)))
        age = rider_meta[slug]["age"]
        age_factor = _age_factor(age)
        trajectory = age_factor * trend_factor
        evidence[slug] = {
            "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
            "age": age,
            "birth_date": rider_meta[slug]["birth_date"],
            "age_factor": round(age_factor, 3),
            "trend_factor": round(trend_factor, 3),
            "trajectory_factor": round(trajectory, 3),
            "startlist_certainty": STARTLIST_CERTAINTY,
            "startlist_status": _startlist_status(len(startlist)),
            "recency_score": round(recency_score, 4),
            "field_course_context_results": sum(bool(row.get("course_context")) for row in enriched_results),
            "profile_strength": profile_strength,
            "profile_confidence": profile_confidence,
            "strongest_by_profile": strongest_by_profile,
            "contextual_results": contextual_results,
            "years": yearly,
        }
        print(
            f"PCS rider evidence {index:>2}/{len(pending)}: "
            f"{rider['rider']:<28} age={str(age):>2} recent={recency_score:.3f} "
            f"context={evidence[slug]['field_course_context_results']}"
        )
    return evidence


def _recent_signal(evidence: dict[str, Any], profile: str) -> float:
    strength = max(0.0, float(evidence.get("profile_strength", {}).get(profile, 0.0)))
    return 1.0 - math.exp(-1.15 * strength)


def _gradual_quality_ratings(
    signals: dict[str, dict[str, float]],
    slug: str,
    evidence: dict[str, Any],
) -> dict[str, float]:
    overall = _value(signals, "overall", slug)
    gc = _value(signals, "gc", slug)
    climb = _value(signals, "climb", slug)
    sprint = _value(signals, "sprint", slug)
    tt = max(_value(signals, "tt", slug), _value(signals, "prologue", slug))
    classic = _value(signals, "classic", slug)
    previous = _value(signals, "previous_vuelta", slug)
    recent = {profile: _recent_signal(evidence, profile) for profile in PROFILE_TYPES}
    raw = {
        "gc": 0.55 * gc + 0.20 * recent["mountain"] + 0.15 * previous + 0.10 * overall,
        "climb": 0.52 * climb + 0.34 * recent["mountain"] + 0.09 * gc + 0.05 * overall,
        "time_trial": 0.58 * tt + 0.32 * recent["itt"] + 0.10 * gc,
        "sprint": 0.55 * sprint + 0.30 * recent["flat"] + 0.10 * overall + 0.05 * classic,
        "punch": 0.52 * classic + 0.32 * recent["hilly"] + 0.10 * overall + 0.06 * climb,
        "hill": 0.39 * classic + 0.29 * climb + 0.27 * recent["hilly"] + 0.05 * overall,
        "cobbles": 0.72 * classic + 0.18 * overall + 0.10 * recent["hilly"],
    }
    return {key: round(10.0 * max(0.0, min(1.0, value)), 1) for key, value in raw.items()}


def _qualities_from_ratings(ratings: dict[str, float]) -> dict[int, int]:
    return {
        quality_type: int(round(ratings[name]))
        for quality_type, name in MODEL_QUALITY_NAMES.items()
        if ratings[name] >= 0.5
    }

def _value(signals: dict[str, dict[str, float]], key: str, slug: str, default: float = 0.0) -> float:
    return signals.get(key, {}).get(slug, default)


def _field_percentile(signals: dict[str, dict[str, float]], key: str, slug: str) -> float:
    """Normalise a rider's raw capability signal to a 0..1 within-field strength.

    The breakaway permission model needs GC and climb strengths on a comparable
    0..1 scale; the raw capability signals sit near ~0.01, so their bare
    difference would produce no effect.  Percentile rank against the current
    field yields the calibrated strength the rider-level factor expects.
    """
    values = signals.get(key)
    if not values:
        return 0.0
    own = values.get(slug)
    if own is None:
        return 0.0
    population = list(values.values())
    total = len(population)
    if total <= 1:
        return 1.0
    below = sum(1 for value in population if value < own)
    equal = sum(1 for value in population if value == own)
    return (below + 0.5 * equal) / total


def _effective_rankings(
    rankings: dict[str, dict[str, float]],
    capabilities: dict[str, dict[str, Any]],
) -> dict[str, dict[str, float]]:
    effective = {key: dict(values) for key, values in rankings.items()}
    field_map = {
        "overall": "overall",
        "form": "form",
        "gc": "gc",
        "climb": "climb",
        "sprint": "sprint",
        "tt": "time_trial",
        "prologue": "prologue",
        "classic": "classic",
        "previous_vuelta": "previous_vuelta",
    }
    for slug, capability in capabilities.items():
        effective_signals = capability["effective"]
        for ranking_name, capability_name in field_map.items():
            effective.setdefault(ranking_name, {})[slug] = float(
                effective_signals[capability_name]
            )
    return effective


def _infer_role(signals: dict[str, dict[str, float]], slug: str) -> int:
    scores = {
        1: 0.60 * _value(signals, "gc", slug) + 0.40 * _value(signals, "climb", slug),
        2: _value(signals, "climb", slug),
        3: max(_value(signals, "tt", slug), _value(signals, "prologue", slug)),
        4: _value(signals, "sprint", slug),
        5: _value(signals, "classic", slug),
    }
    role, score = max(scores.items(), key=lambda item: item[1])
    return role if score >= 0.20 else 6



def _project_price(signals: dict[str, dict[str, float]], slug: str) -> int:
    if slug in PRICE_OVERRIDES:
        return PRICE_OVERRIDES[slug]
    overall = _value(signals, "overall", slug)
    form = _value(signals, "form", slug)
    specialty = max(
        _value(signals, key, slug)
        for key in ("gc", "climb", "sprint", "tt", "prologue", "classic")
    )
    rating = 0.62 * overall + 0.18 * form + 0.20 * specialty
    price_m = 0.5 + 6.0 * rating**2
    return int(max(500_000, min(6_500_000, round(price_m * 4) / 4 * 1_000_000)))


def _stage_signal_components(
    stage: dict[str, Any],
    slug: str,
    signals: dict[str, dict[str, float]],
    historical: float,
    evidence: dict[str, Any],
) -> dict[str, float]:
    overall = _value(signals, "overall", slug, 0.015)
    form = _value(signals, "form", slug, 0.015)
    gc = _value(signals, "gc", slug, 0.01)
    climb = _value(signals, "climb", slug, 0.01)
    sprint = _value(signals, "sprint", slug, 0.01)
    tt = _value(signals, "tt", slug, 0.01)
    prologue = _value(signals, "prologue", slug, 0.01)
    classic = _value(signals, "classic", slug, 0.01)
    previous = _value(signals, "previous_vuelta", slug, 0.01)
    profile = stage["profile_type"]
    finish = stage["finish_type"]
    if profile == "itt":
        specialty = (0.57 * prologue + 0.28 * tt + 0.15 * gc) if float(stage.get("distance_km") or 0) <= 12 else (0.65 * tt + 0.23 * gc + 0.12 * prologue)
    elif profile == "mountain":
        specialty = 0.52 * climb + 0.31 * gc + 0.11 * previous + 0.06 * classic
    elif profile == "hilly" and (finish == "uphill" or float(stage.get("gradient_final_km") or 0) >= 5):
        specialty = 0.39 * climb + 0.34 * classic + 0.19 * gc + 0.08 * sprint
    elif profile == "hilly":
        specialty = 0.39 * classic + 0.23 * climb + 0.25 * sprint + 0.13 * previous
    else:
        specialty = 0.68 * sprint + 0.20 * classic + 0.08 * tt + 0.04 * previous
    recent_profile = float(evidence.get("profile_strength", {}).get(profile, 0.0))
    recent_course = _recent_course_strength(evidence, stage)
    ranking = 0.64 * specialty + 0.10 * overall + 0.08 * form
    raw = ranking + 0.16 * historical + 0.10 * recent_profile + 0.08 * recent_course
    base_score = raw * float(evidence.get("trajectory_factor", 1.0))
    score = base_score
    breakaway: dict[str, float] = {}
    if profile == "mountain" and finish == "summit":
        breakaway_history = stage.get("breakaway_history") or {}
        baseline_probability = float(breakaway_history.get("global_rate") or 0.0)
        survival_probability = float(
            stage.get("breakaway_survival_probability") or 0.0
        )
        rider_factor = summit_breakaway_rider_factor(
            {"probability": survival_probability, "global_rate": baseline_probability},
            gc_strength=gc,
            climb_strength=climb,
        gc_percentile = _field_percentile(signals, "gc", slug)
        tt_percentile = _field_percentile(signals, "tt", slug)
        # A rider is only a GC-defence threat teams must control if they can both
        # climb AND time-trial; a marked pure climber (high climb, weak TT, e.g. a
        # KOM/mountain-jersey hunter) is not protected in the front group and must
        # score from the break, so gate the GC signal by the weaker of GC/TT.
        gc_strength = min(gc_percentile, tt_percentile)
        climb_strength = _field_percentile(signals, "climb", slug)
        rider_factor = summit_breakaway_rider_factor(
            {
                "probability": survival_probability,
                "global_rate": baseline_probability,
            },
            gc_strength=gc_strength,
            climb_strength=climb_strength,
        )
        score = base_score * rider_factor["factor"]
        breakaway = {
            "breakaway_survival_baseline_probability": baseline_probability,
            "breakaway_survival_probability": survival_probability,
            "breakaway_survival_probability_delta": (
                survival_probability - baseline_probability
            ),
            "breakaway_rider_factor": rider_factor["factor"],
            "breakaway_break_dependence": rider_factor["break_dependence"],
            "breakaway_kom_entry_factor": rider_factor["entry_attempt_factor"],
            "breakaway_kom_marking_factor": rider_factor["marking_factor"],
            "breakaway_gc_percentile": gc_percentile,
            "breakaway_tt_percentile": tt_percentile,
            "breakaway_gc_strength": gc_strength,
            "breakaway_climb_strength": climb_strength,
            "breakaway_space_ratio": rider_factor["space_ratio"],
            "breakaway_break_dependence": rider_factor["break_dependence"],
            "breakaway_kom_entry_factor": rider_factor["entry_attempt_factor"],
            "breakaway_kom_marking_factor": rider_factor["marking_factor"],
            "breakaway_rider_factor": rider_factor["factor"],
        }
    confidence = min(
        0.92,
        0.30
        + 0.34 * float(evidence.get("profile_confidence", {}).get(profile, 0.0))
        + 0.14 * float(historical > 0)
        + 0.14 * form,
    )
    return {
        "score": score,
        "specialty": specialty,
        "ranking": ranking,
        "historical_similarity": historical,
        "recent_profile_evidence": recent_profile,
        "recent_course_evidence": recent_course,
        "confidence": confidence,
        **breakaway,
    }


def _stage_signal(
    stage: dict[str, Any],
    slug: str,
    signals: dict[str, dict[str, float]],
    historical: float,
    evidence: dict[str, Any],
) -> float:
    return _stage_signal_components(stage, slug, signals, historical, evidence)["score"]


def _average_curve() -> dict[int, dict[int, float]]:
    source_curves = [
        RankPointsCurve.from_snapshot(load_snapshot("tdf2026")).curves,
        RankPointsCurve.from_snapshot(load_snapshot("giro2026")).curves,
    ]
    averaged: dict[int, dict[int, float]] = {}
    for stage_type in {key for curves in source_curves for key in curves}:
        ranks = {rank for curves in source_curves for rank in curves.get(stage_type, {})}
        averaged[stage_type] = {
            rank: sum(
                curves[stage_type][rank]
                for curves in source_curves
                if rank in curves.get(stage_type, {})
            )
            / sum(rank in curves.get(stage_type, {}) for curves in source_curves)
            for rank in ranks
        }
    return averaged


def _curve_points(curves: dict[int, dict[int, float]], stage_type: int, rank: int) -> float:
    curve = curves.get(stage_type) or curves.get(1) or {}
    return float(curve.get(rank, 0.0))


def _expected_curve_points(
    curves: dict[int, dict[int, float]],
    stage_type: int,
    predicted_rank: int,
    confidence: float,
) -> float:
    """Expected Scorito points over an uncertainty band around predicted rank."""
    curve = curves.get(stage_type) or curves.get(1) or {}
    if not curve:
        return 0.0
    sigma = 2.4 + 8.5 * (1.0 - confidence)
    max_rank = max(max(curve), predicted_rank + math.ceil(4 * sigma))
    weights = [math.exp(-0.5 * ((rank - predicted_rank) / sigma) ** 2) for rank in range(1, max_rank + 1)]
    denominator = sum(weights) or 1.0
    return sum(weight * _curve_points(curves, stage_type, rank) for rank, weight in enumerate(weights, 1)) / denominator


def _evidence_argument(evidence: dict[str, Any], profile: str) -> str:
    rows = evidence.get("strongest_by_profile", {}).get(profile, [])
    if not rows:
        return "No profile-matched recent result parsed; ranking priors dominate."
    parts = []
    for row in rows[:2]:
        event = row.get("event") or row.get("race")
        field = (
            f"field {row['startlist_quality_score']}/{row['startlist_count']}"
            if row.get("startlist_quality_score")
            else f"field-class proxy {row['field_strength']:.2f}"
        )
        course = ", ".join(
            value
            for value in (
                f"ProfileScore {row['profile_score']}" if row.get("profile_score") is not None else "",
                f"{row['vertical_meters']}vm" if row.get("vertical_meters") is not None else "",
                f"{row['distance_km']}km" if row.get("distance_km") is not None else "",
            )
            if value
        )
        parts.append(
            f"{row['year']} #{row['rank']} {event} "
            f"({row['profile_type']}; {field}; {course or row['profile_basis']}; "
            f"adjusted {row['contribution']:.3f})"
        )
    return "; ".join(parts)


def _uncertainty_label(confidence: float) -> str:
    if confidence >= 0.72:
        return "medium"
    if confidence >= 0.52:
        return "medium-high"
    return "high"


def _stage_description(stage: dict[str, Any]) -> str:
    profile = stage["profile_type"]
    finish = stage["finish_type"]
    if profile == "itt":
        return "Individual time trial; prioritize GC leaders and TT specialists."
    if profile == "flat":
        return "Likely bunch sprint; prioritize five-sprinter depth and lead-team points."
    if profile == "mountain":
        ending = "summit finish" if finish == "summit" else "major climbing day"
        return f"High-mountain {ending}; prioritize GC leaders and elite climbers."
    if finish == "uphill":
        return "Hilly stage with uphill finish; prioritize puncheurs and climbing sprinters."
    return "Hilly transition/breakaway stage; prioritize durable fast finishers and attackers."


def _validation_summary() -> str:
    validation_path = ROOT / "data" / "pcs" / "pcs_validation.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if (
        validation.get("race_filter") is not None
        or validation.get("protocol_version") != 2
        or validation.get("evaluation_mode") != "pre-race cross-race holdout"
    ):
        raise RuntimeError(
            "PCS validation artifact is not the canonical pre-race cross-race holdout"
        )
    protocol = str(validation.get("protocol") or "unspecified protocol")
    stages = int(validation["stages_with_rho"])
    macro = float(validation["macro_spearman"])
    top10 = float(validation["mean_top10_hit_rate"])
    return (
        f"{stages}-stage PCS model ({protocol}): "
        f"macro Spearman {macro:.4f}, top-10 hit rate {top10:.4f}"
    )


def _historical_breakaway_records() -> list[dict[str, Any]]:
    path = ROOT / "data" / "historical" / "gt_summit_breakaway_labels.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))



def build_projection() -> dict[str, Any]:
    fetched_at = datetime.now(UTC).isoformat()
    history = StageStore().all_stages()
    startlist_url = race_startlist_url(RACE_SLUG, YEAR)
    startlist = parse_startlist(fetch_race_startlist(RACE_SLUG, YEAR, cache=True))
    if len(startlist) < MIN_PROVISIONAL_STARTERS:
        raise RuntimeError(
            f"expected at least {MIN_PROVISIONAL_STARTERS} PCS Vuelta starters, got {len(startlist)}"
        )
    previous_projection_path = ROOT / "data" / "scorito" / "vuelta2026" / "projected_recommendation.json"
    previous_evidence: dict[str, dict[str, Any]] = {}
    if previous_projection_path.exists():
        previous_projection = json.loads(previous_projection_path.read_text(encoding="utf-8"))
        previous_evidence = {
            str(row["rider_slug"]): row["recent_evidence"]
            for row in previous_projection.get("riders", [])
            if (
                row.get("rider_slug")
                and row.get("recent_evidence", {}).get("evidence_schema_version")
                == EVIDENCE_SCHEMA_VERSION
            )
        }
    rider_evidence = _load_rider_evidence(
        startlist, history, existing_evidence=previous_evidence
    )

    stages: list[dict[str, Any]] = []
    for stage_no in range(1, 22):
        url = stage_page_url(RACE_SLUG, YEAR, stage_no)
        stage = parse_stage_page(
            fetch_stage_page(RACE_SLUG, YEAR, stage_no, cache=True), source_url=url
        )
        stage.update(
            {
                "race": "Vuelta a España",
                "race_slug": RACE_SLUG,
                "year": YEAR,
                "stage_no": stage_no,
                "startlist": startlist,
            }
        )
        stages.append(stage)

    breakaway_records = _historical_breakaway_records()
    for stage in stages:
        if stage["profile_type"] != "mountain" or stage["finish_type"] != "summit":
            continue
        prior = historical_breakaway_prior(breakaway_records, stage)
        stage["breakaway_survival_probability"] = prior["probability"]
        stage["breakaway_history"] = prior
    rankings, ranking_urls = _load_rankings()
    capabilities = {
        row["rider_slug"]: capability_set(
            row["rider_slug"],
            raw_capability_signals(
                rankings,
                row["rider_slug"],
                recent_flat=float(
                    rider_evidence[row["rider_slug"]]["profile_strength"]["flat"]
                ),
            ),
        )
        for row in startlist
    }
    effective_rankings = _effective_rankings(rankings, capabilities)
    historical_by_stage: dict[int, dict[str, float]] = {}
    similar_by_stage: dict[int, list[dict[str, Any]]] = {}
    for stage in stages:
        result = predict_finishers(stage, history, k=12, top_n=len(startlist))
        raw_scores = {row["rider_slug"]: float(row["score"]) for row in result["predictions"]}
        maximum = max(raw_scores.values(), default=1.0)
        historical_by_stage[stage["stage_no"]] = {
            slug: score / maximum for slug, score in raw_scores.items()
        }
        similar_by_stage[stage["stage_no"]] = [
            {
                "id": row["id"],
                "race": row.get("race"),
                "stage_no": row.get("stage_no"),
                "profile_type": row.get("profile_type"),
                "finish_type": row.get("finish_type"),
                "similarity": round(float(row["similarity"]), 4),
            }
            for row in result["similar_stages"][:5]
        ]

    riders: list[Rider] = []
    rider_rows: list[dict[str, Any]] = []
    team_ids = {
        team: team_id
        for team_id, team in enumerate(sorted({row.get("team") or "" for row in startlist}), start=1)
    }
    for rider_id, row in enumerate(startlist, start=1):
        slug = row["rider_slug"]
        price = _project_price(effective_rankings, slug)
        if row.get("team") == UAE_TEAM:
            price = max(price, UAE_PRICE_FLOORS.get(slug, 2_000_000))
        evidence = rider_evidence[slug]
        model_qualities = _gradual_quality_ratings(effective_rankings, slug, evidence)
        rider = Rider(
            rider_id=rider_id,
            event_rider_id=rider_id,
            name=row["rider"],
            team_id=team_ids[row.get("team") or ""],
            price=price,
            role=_infer_role(effective_rankings, slug),
            nationality="",
            age=evidence["age"],
            qualities=_qualities_from_ratings(model_qualities),
        )
        riders.append(rider)
        rider_rows.append(
            {
                **row,
                "projected_price": price,
                "role": rider.role_label,
                "age": rider.age,
                "availability": "provisional starter",
                "recent_evidence": evidence,
                "model_qualities": model_qualities,
                "signals": {
                    key: round(_value(rankings, key, slug), 4)
                    for key in RANKING_VIEWS
                },
                "capabilities": capabilities[slug],
            }
        )

    model_stages = [
        Stage(
            market_round_id=10_000 + stage["stage_no"],
            stage_id=stage["stage_no"],
            order=stage["stage_no"],
            stage_type=2 if stage["profile_type"] == "itt" else 1,
            terrain_type={"flat": 1, "hilly": 2, "mountain": 3, "itt": 1}[stage["profile_type"]],
        )
        for stage in stages
    ]
    snapshot = Snapshot(
        market_id=MARKET_ID,
        slug="vuelta2026-projected",
        budget=BUDGET,
        captain_factor=CAPTAIN_FACTOR,
        riders=riders,
        stages=model_stages,
    )
    source_by_rider_id = {
        rider.rider_id: source
        for rider, source in zip(riders, startlist, strict=True)
    }
    rider_id_by_slug = {
        source["rider_slug"]: rider.rider_id
        for rider, source in zip(riders, startlist, strict=True)
    }

    curves = _average_curve()
    projected_points: dict[tuple[int, int], float] = {}
    stage_rankings: dict[int, list[dict[str, Any]]] = {}
    stage_components: dict[tuple[int, int], dict[str, float]] = {}
    for stage, model_stage in zip(stages, model_stages, strict=True):
        candidates: list[dict[str, Any]] = []
        historical = historical_by_stage[stage["stage_no"]]
        for rider, source in zip(riders, startlist, strict=True):
            slug = source["rider_slug"]
            components = _stage_signal_components(
                stage,
                slug,
                effective_rankings,
                historical.get(slug, 0.0),
                rider_evidence[slug],
            )
            candidates.append({"rider": rider, "source": source, "components": components})
        # Infer tactical hierarchy only from this provisional team: leader/co-leader/
        # helper.  This is deliberately modest because no declarations are available.
        by_team: dict[str, list[dict[str, Any]]] = {}
        for item in candidates:
            by_team.setdefault(item["source"].get("team") or "", []).append(item)
        for team_rows in by_team.values():
            team_rows.sort(key=lambda item: item["components"]["score"], reverse=True)
            for team_rank, item in enumerate(team_rows, start=1):
                role_factor = 1.045 if team_rank == 1 else (1.0 if team_rank == 2 else 0.94)
                item["components"]["role_factor"] = role_factor
                item["components"]["score"] *= role_factor
                item["role_assumption"] = (
                    "inferred protected option" if team_rank == 1 else
                    "inferred co-option" if team_rank == 2 else
                    "inferred helper; tactics unconfirmed"
                )
        candidates.sort(key=lambda item: item["components"]["score"], reverse=True)
        rows = []
        for rank, item in enumerate(candidates, start=1):
            rider = item["rider"]
            source = item["source"]
            components = item["components"]
            confidence = float(components["confidence"])
            points = _expected_curve_points(curves, model_stage.stage_type, rank, confidence)
            points *= STARTLIST_CERTAINTY
            projected_points[(rider.rider_id, model_stage.stage_id)] = points
            stage_components[(rider.rider_id, model_stage.stage_id)] = components
            sigma = 2.4 + 8.5 * (1.0 - confidence)
            rows.append(
                {
                    "rank": rank,
                    "rider": rider.name,
                    "rider_slug": source["rider_slug"],
                    "score": round(float(components["score"]), 5),
                    "projected_scorito_points": round(points, 2),
                    "expected_finish_band": [max(1, round(rank - sigma)), round(rank + sigma)],
                    "confidence": round(confidence, 3),
                    "uncertainty": _uncertainty_label(confidence),
                    "role_assumption": item["role_assumption"],
                    "score_components": {key: round(float(value), 4) for key, value in components.items()},
                    "evidence": _evidence_argument(rider_evidence[source["rider_slug"]], stage["profile_type"]),
                }
            )
        stage_rankings[stage["stage_no"]] = rows

    classification_values: dict[int, float] = {}
    stage_potential: dict[int, float] = {}
    for rider, source in zip(riders, startlist, strict=True):
        slug = source["rider_slug"]
        evidence = rider_evidence[slug]
        gc = _value(effective_rankings, "gc", slug)
        sprint = _value(effective_rankings, "sprint", slug)
        climb = _value(effective_rankings, "climb", slug)
        previous = _value(effective_rankings, "previous_vuelta", slug)
        youth = 12.0 * gc**1.8 if rider.age is not None and rider.age <= 25 else 0.0
        classification = (
            142.0 * gc**2.25
            + 52.0 * sprint**2.15
            + 27.0 * climb**2.1
            + 18.0 * previous**2.0
            + youth
        ) * float(evidence["trajectory_factor"]) * STARTLIST_CERTAINTY
        classification_values[rider.rider_id] = classification
        stage_potential[rider.rider_id] = sum(
            projected_points[(rider.rider_id, model_stage.stage_id)] for model_stage in model_stages
        )

    def points_fn(rider_id: int, stage: Stage) -> float:
        return projected_points.get((rider_id, stage.stage_id), 0.0)

    sprint_pool = sorted(
        (
            (
                float(
                    capabilities[source["rider_slug"]]["sprint_assessment"][
                        "effective_sprint"
                    ]
                ),
                rider.rider_id,
            )
            for rider, source in zip(riders, startlist, strict=True)
            if capabilities[source["rider_slug"]]["sprint_assessment"]["eligible"]
        ),
        reverse=True,
    )
    sprint_ids = {rider_id for _effective_sprint, rider_id in sprint_pool}
    unavailable_ids = {
        rider.rider_id
        for rider, source in zip(riders, startlist, strict=True)
        if source["rider_slug"] in UNAVAILABLE_RIDERS
    }
    plan = joint_enrolled_squad(
        snapshot,
        points_fn,
        budget=BUDGET,
        squad_size=SQUAD_SIZE,
        lineup_size=LINEUP_SIZE,
        selection_values=classification_values,
        max_riders_per_team=MAX_RIDERS_PER_TEAM,
        coverage_constraints=[(sprint_ids, MIN_SPRINT_OPTIONS)],
        excluded_rider_ids=unavailable_ids,
    )
    if plan is None:
        raise RuntimeError("Vuelta projected squad MILP did not produce a solution")

    selected_ids = set(plan.rider_ids)
    selected_team_counts = Counter(snapshot.rider(rider_id).team_id for rider_id in selected_ids)
    selected_sprint_ids = selected_ids & sprint_ids
    if len(selected_ids) != SQUAD_SIZE or len(set(selected_ids)) != SQUAD_SIZE:
        raise RuntimeError("optimizer returned a non-unique or wrong-sized squad")
    if plan.total_price > BUDGET:
        raise RuntimeError("optimizer returned an over-budget squad")
    if max(selected_team_counts.values(), default=0) > MAX_RIDERS_PER_TEAM:
        raise RuntimeError("optimizer returned a squad above the trade-team cap")
    if len(selected_sprint_ids) < MIN_SPRINT_OPTIONS:
        raise RuntimeError("optimizer returned too few credible sprint options")
    if selected_ids & unavailable_ids:
        raise RuntimeError("optimizer returned an objectively unavailable rider")

    season_proxy = {
        rider.rider_id: stage_potential[rider.rider_id] + classification_values[rider.rider_id]
        for rider in riders
    }
    proxy_rank = {
        rider_id: rank
        for rank, rider_id in enumerate(sorted(season_proxy, key=season_proxy.get, reverse=True), start=1)
    }
    value_rank = {
        rider_id: rank
        for rank, rider_id in enumerate(
            sorted(season_proxy, key=lambda rid: season_proxy[rid] / snapshot.rider(rid).price, reverse=True),
            start=1,
        )
    }
    squad = []
    decision_review = []
    for rider, source in zip(riders, startlist, strict=True):
        rider_id = rider.rider_id
        slug = source["rider_slug"]
        profile = max(PROFILE_TYPES, key=lambda key: rider_evidence[slug]["profile_strength"][key])
        argument = _evidence_argument(rider_evidence[slug], profile)
        selected = rider_id in selected_ids
        rationale = (
            f"Selected by joint 21-stage lineup MILP: season proxy {season_proxy[rider_id]:.1f}, "
            f"value rank {value_rank[rider_id]}/{len(startlist)}; strongest transfer is {profile}."
            if selected else
            f"Omitted on marginal value/coverage: season proxy {season_proxy[rider_id]:.1f}, "
            f"value rank {value_rank[rider_id]}/{len(startlist)}; a legal higher-value combination filled 20 places."
        )
        review = {
            "rider": rider.name,
            "rider_slug": slug,
            "selected": selected,
            "projected_price": rider.price,
            "season_proxy_points": round(season_proxy[rider_id], 2),
            "stage_potential_points": round(stage_potential[rider_id], 2),
            "classification_jersey_points": round(classification_values[rider_id], 2),
            "overall_proxy_rank": proxy_rank[rider_id],
            "value_rank": value_rank[rider_id],
            "best_evidence_profile": profile,
            "strongest_evidence": argument,
            "decision_rationale": rationale,
            "uncertainty": f"high: provisional {len(startlist)}-rider list and synthetic price",
            "capabilities": capabilities[slug],
        }
        decision_review.append(review)
        if selected:
            squad.append(
                {
                    "rider_id": rider_id,
                    "rider": rider.name,
                    "rider_slug": slug,
                    "team": source.get("team"),
                    "role": rider.role_label,
                    "age": rider.age,
                    "projected_price": rider.price,
                    "projected_stage_potential": round(stage_potential[rider_id], 2),
                    "classification_jersey_value": round(classification_values[rider_id], 2),
                    "season_proxy_points": round(season_proxy[rider_id], 2),
                    "value_points_per_m": round(season_proxy[rider_id] / (rider.price / 1_000_000), 2),
                    "recent_evidence": rider_evidence[slug],
                    "sprint_option": rider_id in sprint_ids,
                    "sprint_reason": capabilities[slug]["sprint_assessment"]["reason"],
                    "capabilities": capabilities[slug],
                    "evidence_argument": argument,
                    "decision_rationale": rationale,
                    "uncertainty": review["uncertainty"],
                }
            )
    squad.sort(key=lambda row: row["projected_price"], reverse=True)
    decision_review.sort(key=lambda row: row["overall_proxy_rank"])

    lineups = []
    for stage, model_stage in zip(stages, model_stages, strict=True):
        lineup = best_stage_lineup(
            model_stage,
            plan.rider_ids,
            {rider_id: points_fn(rider_id, model_stage) for rider_id in plan.rider_ids},
            lineup_size=LINEUP_SIZE,
            captain_factor=CAPTAIN_FACTOR,
        )
        if len(lineup.rider_ids) != LINEUP_SIZE or len(set(lineup.rider_ids)) != LINEUP_SIZE:
            raise RuntimeError(f"stage {stage['stage_no']} lineup is not nine unique riders")
        if lineup.captain_id not in lineup.rider_ids:
            raise RuntimeError(f"stage {stage['stage_no']} captain is not enrolled")
        details = []
        ranking_by_id = {
            rider_id_by_slug[row["rider_slug"]]: row
            for row in stage_rankings[stage["stage_no"]]
        }
        for rider_id in lineup.rider_ids:
            rider = snapshot.rider(rider_id)
            source = source_by_rider_id[rider_id]
            ranking_row = ranking_by_id[rider_id]
            details.append(
                {
                    "rider": rider.name,
                    "rider_slug": source["rider_slug"],
                    "projected_scorito_points": round(points_fn(rider_id, model_stage), 2),
                    "model_rank": ranking_row["rank"],
                    "expected_finish_band": ranking_row["expected_finish_band"],
                    "role_assumption": ranking_row["role_assumption"],
                    "evidence": ranking_row["evidence"],
                    "uncertainty": ranking_row["uncertainty"],
                }
            )
        captain = snapshot.rider(lineup.captain_id)
        captain_detail = next(row for row in details if row["rider"] == captain.name)
        lineups.append(
            {
                "stage_no": stage["stage_no"],
                "date": stage.get("date"),
                "route": f"{stage.get('departure')} -> {stage.get('arrival')}",
                "profile_type": stage["profile_type"],
                "finish_type": stage["finish_type"],
                "description": _stage_description(stage),
                "distance_km": stage["distance_km"],
                "vertical_meters": stage["vertical_meters"],
                "gradient_final_km": stage.get("gradient_final_km"),
                "captain": captain.name,
                "captain_rationale": (
                    f"Highest expected Scorito return ({captain_detail['projected_scorito_points']:.2f}) "
                    f"in enrolled nine; {captain_detail['evidence']}"
                ),
                "lineup": [snapshot.rider(rider_id).name for rider_id in lineup.rider_ids],
                "lineup_details": details,
                "projected_points": round(lineup.total, 2),
                "ideal_riders": [row["rider"] for row in stage_rankings[stage["stage_no"]][:9]],
                "uncertainty": "Expected-points proxy, not a finishing-order guarantee; provisional field.",
            }
        )

    terrain_distribution = dict(Counter(stage["profile_type"] for stage in stages))
    selected_classification = sum(classification_values[rid] for rid in selected_ids)
    selected_lineup_points = sum(row["projected_points"] for row in lineups)
    return {
        "schema_version": 4,
        "model_version": MODEL_VERSION,
        "status": "evidence_projection",
        "generated_at": fetched_at,
        "market_id": MARKET_ID,
        "budget": BUDGET,
        "squad_size": SQUAD_SIZE,
        "lineup_size": LINEUP_SIZE,
        "captain_factor": CAPTAIN_FACTOR,
        "price_status": "synthetic fallback only; live market 310 prices are applied separately",
        "price_method": (
            "Synthetic prices only support the standalone evidence projection; recommend_vuelta_live.py uses live Scorito prices."
        ),
        "model_method": {
            "target": "expected Scorito points proxy",
            "stage_points": "PCS specialty/form + field-strength-adjusted and exact-course-matched 2026/2025/2024 results + validated stage similarity, mapped through empirical TdF/Giro Scorito rank-points curves with finish uncertainty",
            "classification_jersey": "separate calibrated proxy using GC, points, KOM/climb and youth signals; not enrolled-stage points",
            "field_strength": "PCS startlist-quality score, field size and race ranking; race class is an explicit fallback",
            "course_matching": "exact PCS profile, distance, vertical metres, ProfileScore, final-km gradient and finish type",
            "quality_ratings": "continuous 0.0-10.0 ratings in 0.1 steps from effective specialty capabilities plus field/course-adjusted evidence",
            "capabilities": "immutable raw PCS/recent-result signals plus explicit, provenance-bearing effective overrides; absolute eligibility precedes relative ranking",
            "race_quality_fallback": RACE_QUALITY_WEIGHTS,
            "recency_weights": RECENCY_WEIGHTS,
            "age_trajectory": "modest continuous modifier; never a name or age exclusion",
            "tactics": "provisional-team profile hierarchy only; no declarations available",
            "availability": "no named exclusion; objective unavailable riders require cited evidence",
            "validation": _validation_summary(),
        },
        "price_overrides": PRICE_OVERRIDES,
        "uae_price_floors": UAE_PRICE_FLOORS,
        "sources": {
            "startlist": startlist_url,
            "stages": [stage_page_url(RACE_SLUG, YEAR, number) for number in range(1, 22)],
            "rankings": ranking_urls,
            "rider_results": [f"{PCS_BASE_URL}/rider/<rider-slug>/<year>" for year in RECENT_YEARS],
            "result_context": "PCS result pages for each rider's top recent evidence candidates",
            "historical_memory": str(StageStore().path),
            "historical_validation": str(ROOT / "data" / "pcs" / "pcs_validation.json"),
            "scorito_point_curves": ["tdf2026", "giro2026"],
        },
        "startlist_count": len(startlist),
        "startlist_status": _startlist_status(len(startlist)),
        "stage_count": len(stages),
        "terrain_distribution": terrain_distribution,
        "riders": rider_rows,
        "stages": [
            {key: stage.get(key) for key in (
                "stage_no", "date", "distance_km", "profile_type", "finish_type",
                "profile_score", "vertical_meters", "gradient_final_km", "departure",
                "arrival", "source_url",
            )}
            for stage in stages
        ],
        "similar_stages": similar_by_stage,
        "stage_rankings": stage_rankings,
        "constraints": {
            "max_riders_per_trade_team": MAX_RIDERS_PER_TEAM,
            "team_limit_evidence": TEAM_LIMIT_EVIDENCE,
            "minimum_credible_sprint_options": MIN_SPRINT_OPTIONS,
            "credible_sprint_pool": [
                {
                    "rider": snapshot.rider(rid).name,
                    "rider_id": rid,
                    "effective_sprint": round(effective_sprint, 4),
                    "assessment": capabilities[source_by_rider_id[rid]["rider_slug"]][
                        "sprint_assessment"
                    ],
                }
                for effective_sprint, rid in sprint_pool
            ],
            "unavailable_riders": UNAVAILABLE_RIDERS,
            "arbitrary_named_exclusions": [],
            "recent_years": list(RECENT_YEARS),
            "recency_weights": RECENCY_WEIGHTS,
        },
        "decision_review": decision_review,
        "recommendation": {
            "total_price": plan.total_price,
            "budget_remaining": BUDGET - plan.total_price,
            "projected_objective": round(plan.value, 2),
            "projected_enrolled_stage_points": round(selected_lineup_points, 2),
            "projected_classification_jersey_points": round(selected_classification, 2),
            "credible_sprint_options": len(selected_sprint_ids),
            "trade_team_counts": {
                team: sum(row["team"] == team for row in squad)
                for team in sorted({row["team"] for row in squad})
            },
            "squad": squad,
            "lineups": lineups,
        },
    }


def _write_squad_csv(projection: dict[str, Any], path: Path) -> None:
    fieldnames = [
        "rider", "team", "role", "projected_price_m", "age", "sprint_option",
        "sprint_reason", "projected_stage_potential", "classification_jersey_value",
        "season_proxy_points", "value_points_per_m", "recency_score", "2026_top_10",
        "2026_top_20", "2025_top_10", "2024_top_10", "evidence_argument",
        "decision_rationale", "uncertainty", *CAPABILITY_AUDIT_FIELDNAMES,
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in projection["recommendation"]["squad"]:
            evidence = row["recent_evidence"]
            yearly = evidence["years"]
            year = lambda value: yearly.get(value, yearly.get(str(value), {}))
            writer.writerow(
                {
                    "rider": row["rider"],
                    "team": row["team"],
                    "role": row["role"],
                    "projected_price_m": f"{row['projected_price'] / 1_000_000:.2f}",
                    "age": row["age"],
                    "sprint_option": "yes" if row["sprint_option"] else "no",
                    "sprint_reason": row["sprint_reason"] or "",
                    "projected_stage_potential": row["projected_stage_potential"],
                    "classification_jersey_value": row["classification_jersey_value"],
                    "season_proxy_points": row["season_proxy_points"],
                    "value_points_per_m": row["value_points_per_m"],
                    "recency_score": evidence["recency_score"],
                    "2026_top_10": year(2026)["top_10"],
                    "2026_top_20": year(2026)["top_20"],
                    "2025_top_10": year(2025)["top_10"],
                    "2024_top_10": year(2024)["top_10"],
                    "evidence_argument": row["evidence_argument"],
                    "decision_rationale": row["decision_rationale"],
                    "uncertainty": row["uncertainty"],
                    **capability_audit_fields(row["capabilities"]),
                }
            )


def _write_stage_csv(projection: dict[str, Any], path: Path) -> None:
    fieldnames = [
        "stage_no", "date", "route", "profile_type", "finish_type", "distance_km",
        "vertical_meters", "gradient_final_km", "description", "captain",
        "captain_rationale", "lineup", "lineup_projected_points", "lineup_evidence",
        "projected_points", "ideal_riders", "uncertainty",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in projection["recommendation"]["lineups"]:
            writer.writerow(
                {
                    **{key: row.get(key) for key in fieldnames},
                    "lineup": "; ".join(row["lineup"]),
                    "lineup_projected_points": "; ".join(
                        f"{item['rider']}={item['projected_scorito_points']:.2f}"
                        for item in row["lineup_details"]
                    ),
                    "lineup_evidence": " | ".join(
                        f"{item['rider']}: {item['evidence']}" for item in row["lineup_details"]
                    ),
                    "ideal_riders": "; ".join(row["ideal_riders"]),
                }
            )


def _write_decision_csv(projection: dict[str, Any], path: Path) -> None:
    fieldnames = [
        "rider", "rider_slug", "selected", "projected_price_m", "season_proxy_points",
        "stage_potential_points", "classification_jersey_points", "overall_proxy_rank",
        "value_rank", "best_evidence_profile", "strongest_evidence", "decision_rationale",
        "uncertainty",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in projection["decision_review"]:
            output = dict(row)
            output["projected_price_m"] = f"{output.pop('projected_price') / 1_000_000:.2f}"
            writer.writerow({key: output.get(key) for key in fieldnames})


def _write_csv_with_lock_fallback(
    writer: Callable[[dict[str, Any], Path], None],
    projection: dict[str, Any],
    preferred_path: Path,
) -> Path:
    try:
        writer(projection, preferred_path)
        return preferred_path
    except PermissionError:
        fallback = preferred_path.with_stem(f"{preferred_path.stem}_recalibrated")
        writer(projection, fallback)
        return fallback


def main() -> None:
    projection = build_projection()
    pcs_out = ROOT / "data" / "pcs" / "vuelta2026_projection.json"
    out_dir = ROOT / "data" / "scorito" / "vuelta2026"
    recommendation_out = out_dir / "projected_recommendation.json"
    versioned_out = out_dir / "projected_recommendation_evidence_v5.json"
    pcs_out.parent.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    squad_csv_out = out_dir / "vuelta2026_projected_squad_evidence_v5.csv"
    stage_csv_out = out_dir / "vuelta2026_stage_teams_evidence_v5.csv"
    decision_csv_out = out_dir / "vuelta2026_rider_decisions_evidence_v5.csv"
    text = json.dumps(projection, ensure_ascii=False, indent=2)
    pcs_out.write_text(text, encoding="utf-8")
    recommendation_out.write_text(text, encoding="utf-8")
    versioned_out.write_text(text, encoding="utf-8")
    squad_csv_out = _write_csv_with_lock_fallback(_write_squad_csv, projection, squad_csv_out)
    stage_csv_out = _write_csv_with_lock_fallback(_write_stage_csv, projection, stage_csv_out)
    decision_csv_out = _write_csv_with_lock_fallback(_write_decision_csv, projection, decision_csv_out)

    recommendation = projection["recommendation"]
    print(f"PCS: {projection['stage_count']} stages, {projection['startlist_count']} provisional starters")
    print(
        f"Projected squad: {recommendation['total_price'] / 1_000_000:.2f}M / "
        f"{projection['budget'] / 1_000_000:.2f}M"
    )
    for row in recommendation["squad"]:
        print(
            f"  {row['rider']:<28} {row['role']:<10} "
            f"{row['projected_price'] / 1_000_000:>4.2f}M "
            f"proxy={row['season_proxy_points']:>6.1f}"
        )
    for path in (pcs_out, recommendation_out, versioned_out, squad_csv_out, stage_csv_out, decision_csv_out):
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
