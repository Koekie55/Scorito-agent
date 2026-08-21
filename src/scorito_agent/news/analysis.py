"""Credibility-aware rider, form, ambition, and tactics analysis."""

from __future__ import annotations

import math
import re
import unicodedata
import urllib.parse
from collections import defaultdict
from datetime import UTC, datetime

from .models import FeedItem, Highlight, Rider


SIGNAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "availability": (
        "will not start",
        "wont start",
        "non starter",
        "dns",
        "abandons",
        "withdraws",
        "ruled out",
        "injury",
        "injured",
        "illness",
        "sick",
        "crash",
    "val",
    "valpartij",
    "ten val",
    "kopzorgen",
        "val",
        "valpartij",
        "ten val",
        "kopzorgen",
        "crashed",
        "pulls out",
        "pulled out",
        "in doubt",
        "50 50",
        "concussion",
        "blessure",
        "geblesseerd",
        "ziek",
        "start niet",
        "opgave",
        "styrt",
        "skadet",
        "syg",
        "abandon",
        "lesion",
    ),
    "role_selection": (
        "team leader",
        "co leader",
        "leadership",
        "domestique",
        "road captain",
        "lead out",
        "selected for",
        "startlist",
        "selection",
        "kopman",
        "kopmanschap",
        "knecht",
        "meesterknecht",
        "opstelling",
        "udtaget",
        "kaptajn",
        "hjaelperytter",
        "lider",
        "gregario",
    ),
    "tactics": (
        "breakaway",
        "attack",
        "attacking",
        "sprint train",
        "leadout",
        "pace setting",
        "marking",
        "stage plan",
        "race plan",
        "tactics",
        "tactical",
        "vlucht",
        "aanval",
        "aanvallen",
        "sprinttrein",
        "koersplan",
        "tactiek",
        "udbrud",
        "angribe",
        "taktik",
        "escapada",
        "ataque",
    ),
    "ambition": (
        "targeting",
        "targets",
        "aims for",
        "going for",
        "wants to win",
        "stage win",
        "overall victory",
        "podium",
        "red jersey",
        "points jersey",
        "mountains jersey",
        "ambition",
        "ambitions",
        "lowers ambitions",
        "doel",
        "gaat voor",
        "mikt op",
        "ritzege",
        "eindzege",
        "maal",
        "sigtet",
        "etapesejr",
        "objetivo",
    ),
    "form": (
        "wins",
        "victory",
        "overall victory",
        "strong form",
    "impressive performance",
    "confidence",
        "impressive performance",
        "confidence",
        "good form",
        "top form",
        "in form",
        "bad form",
        "out of form",
        "good legs",
        "heavy legs",
        "fatigue",
        "recovery",
        "altitude camp",
        "training camp",
        "vorm",
        "goede benen",
        "slechte benen",
        "vermoeid",
        "herstel",
        "hoogtestage",
        "formstark",
        "traethed",
        "recuperacion",
    ),
    "stage_conditions": (
        "crosswind",
        "headwind",
        "tailwind",
        "strong wind",
        "heat warning",
        "extreme heat",
        "rain",
        "wet roads",
        "route change",
        "weather",
        "zijwind",
        "waaiers",
        "hitte",
        "regen",
        "parcourswijziging",
        "sidevind",
        "varme",
        "lluvia",
    ),
    "interview": (
        "interview",
        "speaks ahead",
        "press conference",
        "reaction",
        "voorbeschouwing",
        "persconferentie",
        "interviewet",
        "entrevista",
    ),
}

NEGATIVE_KEYWORDS = (
    "will not start",
    "wont start",
    "non starter",
    "dns",
    "ruled out",
    "withdraws",
    "abandons",
    "injured",
    "injury",
    "illness",
    "sick",
    "concussion",
    "crash",
    "val",
    "valpartij",
    "ten val",
    "kopzorgen",
    "crashed",
    "pulls out",
    "pulled out",
    "in doubt",
    "not feeling well",
    "setback",
    "lowers ambitions",
    "bad form",
    "out of form",
    "heavy legs",
    "start niet",
    "opgave",
    "geblesseerd",
    "ziek",
    "skadet",
    "syg",
)

POSITIVE_KEYWORDS = (
    "wins",
    "victory",
    "overall victory",
    "strong form",
    "impressive performance",
    "confidence",
    "good form",
    "top form",
    "in form",
    "good legs",
    "going for",
    "wants to win",
    "targets",
    "kopman",
    "team leader",
    "leadership",
    "goede benen",
    "mikt op",
    "ritzege",
)

CATEGORY_WEIGHTS = {
    "availability": 30.0,
    "role_selection": 23.0,
    "tactics": 20.0,
    "ambition": 15.0,
    "form": 18.0,
    "stage_conditions": 16.0,
    "interview": 12.0,
    "general": 5.0,
}

TIER_CONFIDENCE = {1: 0.92, 2: 0.78, 3: 0.61, 4: 0.28}


def normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.lower()).strip()


def contains_phrase(normalized_text: str, phrase: str) -> bool:
    needle = normalize(phrase)
    return bool(needle) and f" {needle} " in f" {normalized_text} "


def matching_riders(text: str, riders: list[Rider]) -> list[Rider]:
    normalized_text = normalize(text)
    matches: list[Rider] = []
    for rider in riders:
        terms = (rider.name, *rider.aliases)
        if any(contains_phrase(normalized_text, term) for term in terms if len(normalize(term)) >= 4):
            matches.append(rider)
    return sorted(matches, key=lambda rider: (rider.priority, rider.name))


def _subject_riders(text: str, riders: list[Rider]) -> list[Rider]:
    """Return the leading rider subject(s), preserving conjunction lists."""
    normalized_text = normalize(text)
    positions: list[tuple[int, int, Rider]] = []
    for rider in riders:
        best: tuple[int, int] | None = None
        for raw_term in (rider.name, *rider.aliases):
            term = normalize(raw_term)
            if len(term) < 4:
                continue
            pattern = r"(?<![a-z0-9])" + r"\s+".join(
                re.escape(token) for token in term.split()
            ) + r"(?![a-z0-9])"
            match = re.search(pattern, normalized_text)
            if match and (best is None or match.start() < best[0]):
                best = (match.start(), match.end())
        if best:
            positions.append((best[0], best[1], rider))
    if not positions:
        return []
    positions.sort(key=lambda value: value[0])
    selected = [positions[0][2]]
    previous_end = positions[0][1]
    conjunctions = {"and", "en", "et", "y", "og"}
    for start, end, rider in positions[1:]:
        connector = normalized_text[previous_end:start].split()
        if connector and not set(connector) <= conjunctions:
            break
        selected.append(rider)
        previous_end = end
    return selected


def _categories(normalized_text: str) -> list[str]:
    categories = [
        category
        for category, phrases in SIGNAL_KEYWORDS.items()
        if any(contains_phrase(normalized_text, phrase) for phrase in phrases)
    ]
    return categories or ["general"]


def _impact_counts(normalized_text: str) -> tuple[int, int]:
    negative = sum(contains_phrase(normalized_text, phrase) for phrase in NEGATIVE_KEYWORDS)
    positive = sum(contains_phrase(normalized_text, phrase) for phrase in POSITIVE_KEYWORDS)
    return negative, positive


def _impact(normalized_text: str) -> str:
    negative, positive = _impact_counts(normalized_text)
    if negative > positive:
        return "negative"
    if positive > negative:
        return "positive"
    return "contextual"


def _excerpt(text: str, anchors: list[str], *, limit: int = 240) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return ""
    normalized_text = normalize(compact)
    anchor_index = -1
    for anchor in anchors:
        normalized_anchor = normalize(anchor)
        if not normalized_anchor:
            continue
        normalized_index = normalized_text.find(normalized_anchor)
        if normalized_index >= 0:
            ratio = normalized_index / max(len(normalized_text), 1)
            anchor_index = int(ratio * len(compact))
            break
    if anchor_index < 0:
        anchor_index = 0
    start = max(0, anchor_index - limit // 3)
    end = min(len(compact), start + limit)
    snippet = compact[start:end].strip()
    if start:
        snippet = "..." + snippet
    if end < len(compact):
        snippet += "..."
    return snippet


def is_potentially_relevant(item: FeedItem, riders: list[Rider], race_terms: tuple[str, ...]) -> bool:
    text = f"{item.title} {item.summary}"
    normalized_text = normalize(text)
    return bool(matching_riders(text, riders)) or any(
        contains_phrase(normalized_text, term) for term in race_terms
    )


def analyze_item(
    item: FeedItem,
    riders: list[Rider],
    *,
    race_terms: tuple[str, ...],
    article_text: str = "",
    now: datetime,
) -> Highlight | None:
    headline = f"{item.title}. {item.summary}".strip()
    combined = f"{headline}. {article_text}".strip()
    title_normalized = normalize(item.title)
    normalized_text = normalize(combined)
    title_matches = _subject_riders(item.title, riders)
    summary_matches = _subject_riders(item.summary, riders)
    matched = title_matches or summary_matches or _subject_riders(article_text, riders)[:5]
    race_match = any(contains_phrase(normalized_text, term) for term in race_terms)
    if not matched and not race_match:
        return None

    categories = _categories(title_normalized)
    if categories == ["general"]:
        categories = _categories(normalized_text)
    headline_normalized = normalize(headline)
    headline_race_match = any(
        contains_phrase(headline_normalized, term) for term in race_terms
    )
    if categories == ["general"] and not headline_race_match:
        return None
    title_negative, title_positive = _impact_counts(title_normalized)
    impact = _impact(title_normalized) if title_negative or title_positive else _impact(normalized_text)
    if impact == "negative" and matched and not title_matches:
        impact = "contextual"
    confidence = TIER_CONFIDENCE[item.source_tier]
    if item.source_official:
        confidence = max(confidence, 0.96)
    if item.source_kind == "youtube" and "interview" in categories:
        confidence = max(confidence, 0.84)
    if item.verification_required:
        confidence = min(confidence, 0.35)

    age_hours = max(0.0, (now.astimezone(UTC) - item.published_at).total_seconds() / 3600.0)
    freshness = 20.0 * math.exp(-age_hours / 36.0)
    rider_weight = 0.0
    if matched:
        rider_weight = {1: 34.0, 2: 25.0, 3: 15.0}.get(min(r.priority for r in matched), 10.0)
    category_weight = max(CATEGORY_WEIGHTS[category] for category in categories)
    score = min(
        100.0,
        rider_weight
        + category_weight
        + freshness
        + confidence * 10.0
        + (7.0 if item.same_day_tactics else 0.0)
        + (5.0 if item.source_official else 0.0),
    )

    if item.source_official:
        verification = "official_source"
    elif item.source_kind == "youtube" and "interview" in categories:
        verification = "direct_interview"
    elif item.verification_required:
        verification = "unverified_community"
    else:
        verification = "single_source"

    if "availability" in categories and impact == "negative":
        decision_hint = "verify_before_downgrading"
    elif any(category in categories for category in ("tactics", "role_selection", "stage_conditions")):
        decision_hint = "lineup_context_only"
    else:
        decision_hint = "monitor_only_no_automatic_upgrade"

    anchors = [rider.name for rider in matched]
    anchors.extend(phrase for category in categories for phrase in SIGNAL_KEYWORDS.get(category, ()))
    return Highlight(
        item=item,
        riders=matched,
        categories=categories,
        evidence=_excerpt(combined, anchors),
        score=score,
        confidence=confidence,
        impact=impact,
        verification=verification,
        decision_hint=decision_hint,
    )


def _source_identity(highlight: Highlight) -> str:
    host = (urllib.parse.urlsplit(highlight.item.url).hostname or "").lower()
    if host and host not in {"news.google.com", "youtube.com", "www.youtube.com"}:
        candidate = host
    else:
        candidate = highlight.item.publisher or highlight.item.source_name
    tokens = [token for token in normalize(candidate).split() if token != "www"]
    while tokens and tokens[-1] in {"com", "co", "uk", "nl", "dk", "fr", "es", "org", "net"}:
        tokens.pop()
    identity = "".join(tokens) or normalize(highlight.item.source_name).replace(" ", "")
    publisher_aliases = {
        "idlprocycling": "indeleiderstrui",
    }
    return publisher_aliases.get(identity, identity)


def apply_corroboration(highlights: list[Highlight]) -> None:
    groups: dict[tuple[str, str, str], list[Highlight]] = defaultdict(list)
    for highlight in highlights:
        for rider in highlight.riders:
            for category in highlight.categories:
                groups[(rider.slug, category, highlight.impact)].append(highlight)

    corroborated_riders: dict[int, set[str]] = defaultdict(set)
    corroborating_sources: dict[int, set[str]] = defaultdict(set)
    corroborated_confidence: dict[int, float] = defaultdict(float)
    for (rider_slug, _category, _impact), group in groups.items():
        independent: dict[str, Highlight] = {}
        for highlight in group:
            independent[_source_identity(highlight)] = highlight
        established = {
            key: value
            for key, value in independent.items()
            if value.item.source_tier <= 3 and not value.item.verification_required
        }
        if len(established) < 2:
            continue
        combined_confidence = 1.0
        for report in established.values():
            combined_confidence *= 1.0 - report.confidence
        group_confidence = min(0.93, 1.0 - combined_confidence)
        source_names = {
            report.item.publisher or report.item.source_name for report in established.values()
        }
        for highlight in group:
            identity = id(highlight)
            corroborated_riders[identity].add(rider_slug)
            corroborating_sources[identity].update(
                source
                for source in source_names
                if source != (highlight.item.publisher or highlight.item.source_name)
            )
            corroborated_confidence[identity] = max(
                corroborated_confidence[identity], group_confidence
            )

    for highlight in highlights:
        identity = id(highlight)
        highlight.corroborating_sources = sorted(corroborating_sources[identity])
        all_riders_corroborated = bool(highlight.riders) and all(
            rider.slug in corroborated_riders[identity] for rider in highlight.riders
        )
        if all_riders_corroborated and highlight.verification not in {
            "official_source",
            "direct_interview",
        }:
            highlight.verification = "corroborated_reports"
            highlight.confidence = max(
                highlight.confidence, corroborated_confidence[identity]
            )
        if (
            "availability" in highlight.categories
            and highlight.impact == "negative"
            and highlight.verification
            in {"official_source", "direct_interview", "corroborated_reports"}
        ):
            highlight.decision_hint = "review_selection_and_lineup"
