"""Deterministic extraction helpers for evidence-aware cycling chat claims."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from urllib.parse import urlparse

from .models import ClaimAction, ClaimCategory, EvidenceTier

_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_STAGE_RE = re.compile(
    r"\b(?:rit|etappe|stage)\s*(\d{1,2})"
    r"(?:\s*(?:t/m|tm|tot|[-–])\s*(?:rit|etappe|stage)?\s*(\d{1,2}))?",
    re.IGNORECASE,
)
_EXPLICIT_LIST_RE = re.compile(
    r"\b(?:rit|etappe|stage)s?\s*((?:\d{1,2}\s*(?:,|/|&|en)\s*)+\d{1,2})",
    re.IGNORECASE,
)

_POSITIVE = {
    "topvorm": 1.0,
    "sterk": 0.55,
    "favoriet": 0.75,
    "ritwinst": 0.7,
    "winnaar": 0.75,
    "wint": 0.75,
    "kanshebber": 0.55,
    "interessant": 0.4,
    "goede vorm": 0.65,
    "fit": 0.45,
    "kopman": 0.5,
    "sprint": 0.25,
    "tijdrit": 0.25,
    "gaat voor": 0.5,
    "mikt op": 0.5,
    "focus op": 0.45,
    "must have": 0.65,
    "meenemen": 0.4,
    "nemen": 0.3,
    "strong": 0.55,
    "favourite": 0.75,
    "favorite": 0.75,
    "good form": 0.65,
    "will start": 0.65,
    "is starting": 0.65,
}
_NEGATIVE = {
    "ziek": -0.9,
    "blessure": -0.85,
    "geblesseerd": -0.9,
    "infectie": -0.8,
    "crash": -0.65,
    "gevallen": -0.55,
    "opgave": -1.0,
    "opgegeven": -1.0,
    "niet aan de start": -1.0,
    "start niet": -1.0,
    "doet niet mee": -1.0,
    "uit vorm": -0.7,
    "slechte vorm": -0.7,
    "slecht": -0.35,
    "knecht": -0.25,
    "te duur": -0.45,
    "overslaan": -0.45,
    "niet nemen": -0.6,
    "sick": -0.9,
    "injury": -0.85,
    "injured": -0.9,
    "will not start": -1.0,
    "won't start": -1.0,
    "not starting": -1.0,
    "out of form": -0.7,
    "bad form": -0.7,
    "too expensive": -0.45,
}

_OFFICIAL_DOMAINS = {
    "scorito.com",
    "lavuelta.es",
    "letour.fr",
    "giroditalia.it",
    "uci.org",
    "greenedgecycling.com",
    "teamvismaleaseabike.com",
}
_CREDIBLE_DOMAINS = {
    "procyclingstats.com",
    "cyclingoracle.com",
    "wielerflits.nl",
    "indeleiderstrui.nl",
    "domestiquecycling.com",
    "hln.be",
    "marca.com",
    "tuttobiciweb.it",
}

_REASONING_MARKERS = (
    "ik denk",
    "volgens mij",
    "waarschijnlijk",
    "verwacht",
    "lijkt",
    "omdat",
    "want",
    "gezien zijn",
    "op basis van",
    "i think",
    "in my view",
    "probably",
    "because",
    "based on",
    "results",
    "form",
    "tactics",
    "profiel",
    "uitslagen",
    "resultaten",
    "vorm",
    "tactiek",
)
_HUMOUR_MARKERS = (
    "😂",
    "🤣",
    "😁",
    "😛",
    "grap",
    "haha",
    "hahaha",
    "lol",
    "sidequest",
    "goat",
    "goud halen",
    "de app heeft humor",
)

_TRANSLITERATION = str.maketrans(
    {"ø": "o", "Ø": "O", "æ": "ae", "Æ": "Ae", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ß": "ss"}
)


def name_key(value: str) -> str:
    """Return an accent-insensitive rider key shared with recommendation code."""

    normalised = unicodedata.normalize("NFKD", str(value).translate(_TRANSLITERATION))
    ascii_value = normalised.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", " ", ascii_value).strip()


def build_rider_aliases(rider_names: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Map safe full-name and unique-surname aliases to canonical rider names."""

    names = [str(name).strip() for name in rider_names if str(name).strip()]
    aliases: dict[str, set[str]] = {}
    surname_counts = Counter(name_key(name).split()[-1] for name in names if name_key(name))
    for name in names:
        key = name_key(name)
        if not key:
            continue
        aliases.setdefault(key, set()).add(name)
        parts = key.split()
        if len(parts) > 1 and surname_counts[parts[-1]] == 1:
            aliases.setdefault(parts[-1], set()).add(name)
    return {alias: tuple(sorted(matches)) for alias, matches in aliases.items()}


def find_riders(
    text: str,
    aliases: Mapping[str, tuple[str, ...] | list[str] | str],
) -> list[str]:
    """Find canonical riders once each, preferring longest aliases."""

    haystack = f" {name_key(text)} "
    found: list[str] = []
    occupied: list[tuple[int, int]] = []
    for alias in sorted(aliases, key=lambda value: (-len(value), value)):
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])")
        for match in pattern.finditer(haystack):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in occupied):
                continue
            raw_matches = aliases[alias]
            candidates = (raw_matches,) if isinstance(raw_matches, str) else tuple(raw_matches)
            if len(candidates) == 1 and candidates[0] not in found:
                found.append(candidates[0])
                occupied.append(span)
            break
    return found


def _is_scorito_share_url(url: str) -> bool:
    parsed = urlparse(url)
    domain = parsed.netloc.lower().split(":", 1)[0]
    if domain.startswith("www."):
        domain = domain[4:]
    if domain != "scorito.com" and not domain.endswith(".scorito.com"):
        return False
    return (
        "voucherid=" in parsed.query.lower()
        or "/subleague/" in parsed.path.lower()
    )


def extract_urls(text: str) -> tuple[str, ...]:
    urls = (url.rstrip(".,;") for url in _URL_RE.findall(str(text)))
    return tuple(
        dict.fromkeys(url for url in urls if not _is_scorito_share_url(url))
    )


def is_url_only_message(text: str) -> bool:
    stripped = _URL_RE.sub("", str(text))
    stripped = re.sub(
        r"(?:image|afbeelding|link|video|gif|omitted|weggelaten|‎)",
        "",
        stripped,
        flags=re.IGNORECASE,
    )
    return not re.sub(r"[\s:;,.!?()\[\]{}\-–—]+", "", stripped)


def is_humour(text: str) -> bool:
    lowered = str(text).lower()
    if any(marker in lowered for marker in _HUMOUR_MARKERS):
        return True
    if "elosegui" not in lowered:
        return False
    return any(
        marker in lowered
        for marker in (
            "hoef niet uit te leggen",
            "er vanuitgaand dat je",
            "allemaal nemen",
            "alle 3",
            "refresh mijn scorito",
            "more like",
            "en dan elosegui",
        )
    )


def extract_stages(text: str, maximum: int = 21) -> tuple[int, ...]:
    stages: set[int] = set()
    for match in _STAGE_RE.finditer(str(text)):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start > end:
            start, end = end, start
        stages.update(range(max(1, start), min(maximum, end) + 1))
    for match in _EXPLICIT_LIST_RE.finditer(str(text)):
        stages.update(
            value
            for value in (int(raw) for raw in re.findall(r"\d{1,2}", match.group(1)))
            if 1 <= value <= maximum
        )
    return tuple(sorted(stages))


def classify_action(text: str) -> ClaimAction:
    lowered = str(text).lower()
    if any(
        marker in lowered
        for marker in (
            "trek ik terug",
            "ik trek terug",
            "negeer",
            "vergeet dat",
            "klopt niet meer",
            "i retract",
            "ignore that",
            "disregard",
        )
    ):
        return ClaimAction.RETRACT
    if any(
        marker in lowered
        for marker in (
            "correctie",
            "ik bedoelde",
            "herstel",
            "toch niet",
            "niet ziek",
            "was fout",
            "klopt niet",
            "correction",
            "i meant",
            "not sick",
            "was wrong",
            "is wrong",
        )
    ):
        return ClaimAction.CORRECT
    if any(
        marker in lowered
        for marker in (
            "ter verduidelijking",
            "precieser",
            "aanvulling",
            "to clarify",
            "more precisely",
        )
    ):
        return ClaimAction.CLARIFY
    return ClaimAction.ASSERT


def classify_category(text: str, stages: tuple[int, ...] = ()) -> ClaimCategory:
    lowered = str(text).lower()
    if is_humour(text):
        return ClaimCategory.HUMOUR
    if is_url_only_message(text):
        return ClaimCategory.SOURCE_REFERENCE
    if any(
        marker in lowered
        for marker in (
            "niet aan de start",
            "start niet",
            "doet niet mee",
            "startlijst",
            "selectie",
            "will not start",
            "won't start",
            "not starting",
            "startlist",
            "selection",
        )
    ):
        return ClaimCategory.AVAILABILITY
    if any(
        marker in lowered
        for marker in (
            "ziek",
            "blessure",
            "geblesseerd",
            "infectie",
            "crash",
            "gevallen",
            "fit",
            "sick",
            "injury",
            "injured",
        )
    ):
        return ClaimCategory.HEALTH
    if any(marker in lowered for marker in ("€", "miljoen", "mio", "prijs", "budget", "te duur", "koopje")):
        return ClaimCategory.PRICE
    if any(marker in lowered for marker in ("uitslag", "resultaat", "werd ", "eindigde", "top 10", "podium")):
        return ClaimCategory.RESULT
    if any(marker in lowered for marker in ("kopman", "knecht", "lead-out", "leadout", "helper", "vrije rol", "sprintkopman")):
        return ClaimCategory.TEAM_ROLE
    if stages or any(marker in lowered for marker in ("gaat voor", "mikt op", "focus op", "ritzege", "etappezege")):
        return ClaimCategory.STAGE_INTENT
    if any(marker in lowered for marker in ("tactiek", "aanval", "vlucht", "ontsnapping", "controleren", "trein")):
        return ClaimCategory.TACTICS
    if any(marker in lowered for marker in ("vorm", "benen", "sterk", "zwak", "fris", "vermoeid")):
        return ClaimCategory.FORM
    if any(marker in lowered for marker in ("punten", "rendement", "waarde", "pickrate", "scorito")):
        return ClaimCategory.VALUE
    if any(marker in lowered for marker in ("nemen", "meenemen", "overslaan", "must have", "mijn team")):
        return ClaimCategory.PREFERENCE
    return ClaimCategory.OTHER


def _source_domains(text: str, sources: Iterable[str]) -> set[str]:
    urls = (*extract_urls(text), *(str(source) for source in sources))
    domains: set[str] = set()
    for url in urls:
        domain = urlparse(url).netloc.lower().split(":", 1)[0]
        if domain.startswith("www."):
            domain = domain[4:]
        if domain:
            domains.add(domain)
    return domains


def classify_evidence_tier(
    text: str,
    *,
    sources: Iterable[str] = (),
    category: ClaimCategory | None = None,
) -> EvidenceTier:
    """Assign evidence from provenance, never from confidence wording."""

    if is_humour(text):
        return EvidenceTier.HUMOUR
    domains = _source_domains(text, sources)
    if any(domain in _OFFICIAL_DOMAINS or domain.endswith(tuple(f".{d}" for d in _OFFICIAL_DOMAINS)) for domain in domains):
        return EvidenceTier.T1
    if any(domain in _CREDIBLE_DOMAINS or domain.endswith(tuple(f".{d}" for d in _CREDIBLE_DOMAINS)) for domain in domains):
        return EvidenceTier.T2
    lowered = str(text).lower()
    if any(marker in lowered for marker in _REASONING_MARKERS):
        return EvidenceTier.T3
    if category == ClaimCategory.TACTICS and len(lowered.split()) >= 8:
        return EvidenceTier.T3
    return EvidenceTier.T4


def sentiment_score(text: str) -> float:
    lowered = str(text).lower()
    score = sum(weight for marker, weight in _POSITIVE.items() if marker in lowered)
    score += sum(weight for marker, weight in _NEGATIVE.items() if marker in lowered)
    if any(
        marker in lowered
        for marker in (
            "niet ziek",
            "geen blessure",
            "toch aan de start",
            "not sick",
            "no injury",
            "not injured",
            "will start after all",
        )
    ):
        score = max(score, 0.65)
    if classify_action(text) == ClaimAction.RETRACT:
        return 0.0
    return max(-1.0, min(1.0, score))


def extraction_confidence(
    *,
    rider_count: int,
    category: ClaimCategory,
    stages: tuple[int, ...],
    sentiment: float,
) -> float:
    confidence = 0.55
    if category != ClaimCategory.OTHER:
        confidence += 0.12
    if stages:
        confidence += 0.08
    if sentiment != 0:
        confidence += 0.08
    if rider_count == 1:
        confidence += 0.1
    elif rider_count > 4:
        confidence -= min(0.2, (rider_count - 4) * 0.025)
    return max(0.25, min(1.0, confidence))


def classify_kind(text: str) -> tuple[str, float]:
    """Compatibility view: source-backed claims are facts; others opinions."""

    category = classify_category(text, extract_stages(text))
    tier = classify_evidence_tier(text, category=category)
    kind = "fact" if tier in {EvidenceTier.T1, EvidenceTier.T2} else "opinion"
    return kind, extraction_confidence(
        rider_count=1,
        category=category,
        stages=extract_stages(text),
        sentiment=sentiment_score(text),
    )


def claim_fingerprint(
    rider_key: str,
    category: ClaimCategory,
    stages: tuple[int, ...],
) -> tuple[str, ClaimCategory, tuple[int, ...]]:
    return rider_key, category, stages


# Backward-compatible spelling for older callers.
is_url_only = is_url_only_message
