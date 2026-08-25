"""Parse Emil Axelgaard's TV 2 cycling stage previews into structured predictions."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

STAGE_TITLE_RE = re.compile(r"optakt til\s+(\d+)\.\s*etape", re.IGNORECASE)
STAR_RE = re.compile("[\u2605\u2b50]")
LEADING_STARS_RE = re.compile("^([\u2605\u2b50]+)\\s*(.*)$", re.DOTALL)
BREAKAWAY_RE = re.compile(r"^Kandidater til et udbrud\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)
NOTE_RE = re.compile(r"^(BEM\u00c6RK[^:]*|OPDATERING[^:]*)\s*:\s*(.+)$", re.DOTALL)
DATE_RE = re.compile(r"(\d{1,2})\.\s*([a-z\u00e6\u00f8\u00e5]+)\s*(\d{4})", re.IGNORECASE)
START_TIME_RE = re.compile(r"Start/i m\u00e5l\s*:\s*(\d{1,2})[.:](\d{2})", re.IGNORECASE)

DANISH_MONTHS = {
    "januar": 1, "februar": 2, "marts": 3, "april": 4, "maj": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11,
    "december": 12,
}

# Ordered so that a longer phrase is never masked by a shorter substring match.
SCENARIO_PHRASES = {
    "bunch_sprint": ("massespurt", "feltspurt", "samlet spurt", "spurt fra et samlet felt"),
    "reduced_sprint": ("puncheurspurt", "reduceret spurt", "spurt p\u00e5 toppen", "lille gruppe"),
    "gc_selection": ("klassementsrytterne", "favoritgruppen", "klassementsbrag", "bjergafslutning"),
    "breakaway": ("udbrudssejr", "udbruddet holder", "udbrud kan holde", "udbrud"),
    "time_trial": ("enkeltstart", "tempoetape"),
    "echelons": ("sidevind", "vindsplittelse", "vaaierne"),
}


def _news_article(soup: BeautifulSoup) -> dict[str, Any]:
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(node.get_text(strip=True))
        except (TypeError, json.JSONDecodeError):
            continue
        for candidate in payload if isinstance(payload, list) else [payload]:
            if isinstance(candidate, dict) and candidate.get("@type") == "NewsArticle":
                return candidate
    return {}


def _timestamp(value: Any) -> str | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()


def _text(node: Any) -> str:
    return " ".join(node.stripped_strings)


def name_key(name: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", str(name))
    ascii_name = "".join(c for c in normalized if not unicodedata.combining(c))
    return tuple(sorted(re.findall(r"[a-z0-9]+", ascii_name.lower())))


def _split_names(text: str) -> list[str]:
    parts = [part.strip(" .:-\u2013") for part in re.split(r",|\bog\b|/", text)]
    return [part for part in parts if len(name_key(part)) >= 2]


def _stage_schedule(text: str) -> dict[str, Any]:
    schedule: dict[str, Any] = {"stage_date": None, "start_time_local": None}
    date_match = DATE_RE.search(text)
    if date_match:
        month = DANISH_MONTHS.get(date_match.group(2).lower())
        if month:
            schedule["stage_date"] = (
                f"{int(date_match.group(3)):04d}-{month:02d}-{int(date_match.group(1)):02d}"
            )
    time_match = START_TIME_RE.search(text)
    if time_match:
        schedule["start_time_local"] = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
    return schedule


def _scenarios(text: str) -> dict[str, float]:
    normalized = " ".join(text.lower().split())
    hits = {
        key: 1.0
        for key, terms in SCENARIO_PHRASES.items()
        if any(term in normalized for term in terms)
    }
    total = sum(hits.values())
    if not total:
        return {}
    return {key: round(value / total, 6) for key, value in hits.items()}


def parse_preview(html: str, *, source_url: str) -> dict[str, Any]:
    """Parse one preview. Danish source text is preserved, never translated."""
    soup = BeautifulSoup(html, "lxml")
    article = _news_article(soup)
    title = str(article.get("headline") or (soup.title.string if soup.title else "")).strip()
    stage_match = STAGE_TITLE_RE.search(title)
    description = str(article.get("description") or "").strip()

    body = soup.select_one(".tc_richcontent") or soup.select_one("article")
    blocks = body.find_all(recursive=False) if body else []

    section = ""
    sections: dict[str, list[str]] = {}
    rider_tiers: list[dict[str, Any]] = []
    breakaway_candidates: list[str] = []
    notes: list[dict[str, str]] = []
    schedule: dict[str, Any] = {"stage_date": None, "start_time_local": None}

    for block in blocks:
        text = _text(block)
        if not text:
            continue
        if block.name in {"h2", "h3"}:
            section = text
            continue
        if block.name == "ul" and schedule["stage_date"] is None:
            schedule = _stage_schedule(text)
        sections.setdefault(section, []).append(text)

        stars_match = LEADING_STARS_RE.match(text)
        if stars_match:
            stars = len(STAR_RE.findall(stars_match.group(1)))
            for rider in _split_names(stars_match.group(2)):
                rider_tiers.append({"rider": rider, "stars": stars})
            continue
        breakaway_match = BREAKAWAY_RE.match(text)
        if breakaway_match:
            breakaway_candidates.extend(_split_names(breakaway_match.group(1)))
            continue
        note_match = NOTE_RE.match(text)
        if note_match:
            notes.append({"kind": note_match.group(1).strip(), "text": note_match.group(2).strip()})

    section_text = {name: " ".join(parts) for name, parts in sections.items()}
    analysis = " ".join(
        value for name, value in section_text.items() if "analyse af etapen" in name.lower()
    )
    scenario_text = " ".join(part for part in (description, analysis) if part)

    canonical = soup.select_one('link[rel="canonical"]')
    resolved_url = str(article.get("url") or (canonical.get("href") if canonical else source_url))
    return {
        "schema_version": 2,
        "source": "tv2_axelgaard",
        "source_url": resolved_url,
        "author": "Emil Axelgaard",
        "title": title,
        "stage_number": int(stage_match.group(1)) if stage_match else None,
        "published_at": _timestamp(article.get("datePublished")),
        "modified_at": _timestamp(article.get("dateModified")),
        "stage_date": schedule["stage_date"],
        "start_time_local": schedule["start_time_local"],
        "description_da": description,
        "scenario_text_da": scenario_text,
        "scenario_probabilities_raw": _scenarios(scenario_text),
        "rider_tiers": rider_tiers,
        "breakaway_candidates": breakaway_candidates,
        "notes": notes,
        "sections_da": section_text,
        "content_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
    }


PREVIEW_URL_RE = re.compile(
    r"^https://sport\.tv2\.dk/cykling/(\d{4}-\d{2}-\d{2})-axelgaards-optakt-til-(\d+)-etape-af-([a-z0-9-]+)$"
)


def discover_previews(html: str) -> list[dict[str, Any]]:
    """Return preview links from a TV 2 index page (author profile or race section)."""
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, dict[str, Any]] = {}
    for anchor in soup.select("a[href]"):
        match = PREVIEW_URL_RE.match(str(anchor.get("href") or "").split("?")[0])
        if not match:
            continue
        found[match.group(0)] = {
            "url": match.group(0),
            "url_date": match.group(1),
            "stage_number": int(match.group(2)),
            "race_slug": match.group(3),
        }
    return sorted(found.values(), key=lambda row: (row["race_slug"], row["stage_number"]))


def archive_preview(
    root: Path, preview: dict[str, Any], *, race_slug: str, fetched_at: str
) -> dict[str, Any]:
    """Persist a preview, keeping one immutable revision per distinct content hash."""
    stage_number = preview.get("stage_number")
    if not stage_number:
        raise ValueError("preview has no stage number")
    directory = root / race_slug
    revisions_directory = directory / "revisions"
    revisions_directory.mkdir(parents=True, exist_ok=True)
    digest = str(preview["content_sha256"])
    record = {**preview, "race_slug": race_slug, "fetched_at": fetched_at}

    revision_path = revisions_directory / f"stage-{int(stage_number):02d}-{digest[:12]}.json"
    created = not revision_path.exists()
    if created:
        revision_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    latest_path = directory / f"stage-{int(stage_number):02d}.json"
    previous_digest = None
    if latest_path.exists():
        previous_digest = json.loads(latest_path.read_text(encoding="utf-8")).get("content_sha256")
    latest_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "latest_path": latest_path,
        "revision_path": revision_path,
        "revision_created": created,
        "content_changed": previous_digest != digest,
        "previous_content_sha256": previous_digest,
    }


def is_usable_before_stage(preview: dict[str, Any]) -> bool:
    """Reject a preview whose last edit is not provably earlier than the stage start."""
    stage_date = preview.get("stage_date")
    modified = preview.get("modified_at")
    if not stage_date or not modified:
        return False
    start = preview.get("start_time_local") or "00:00"
    stage_start = datetime.fromisoformat(f"{stage_date}T{start}:00+02:00")
    return datetime.fromisoformat(str(modified)) < stage_start


def stage_star_signals(directory: Path) -> dict[int, dict[tuple[str, ...], float]]:
    """Return {stage_no: {rider name key: stars}} for previews fixed before the start."""
    signals: dict[int, dict[tuple[str, ...], float]] = {}
    if not directory.exists():
        return signals
    for path in sorted(directory.glob("stage-*.json")):
        preview = json.loads(path.read_text(encoding="utf-8"))
        stage_number = preview.get("stage_number")
        if not stage_number or not is_usable_before_stage(preview):
            continue
        tiers: dict[tuple[str, ...], float] = {}
        for row in preview.get("rider_tiers", []):
            key = name_key(str(row.get("rider") or ""))
            if key:
                tiers[key] = max(tiers.get(key, 0.0), float(row.get("stars") or 0.0))
        if tiers:
            signals[int(stage_number)] = tiers
    return signals


def validated_weight(path: Path) -> tuple[float, str]:
    """Read the weight earned by out-of-sample validation; absent evidence means none."""
    if not path.exists():
        return 0.0, "not validated"
    report = json.loads(path.read_text(encoding="utf-8"))
    signal = report.get("recommended_signal", {})
    weight = float(signal.get("weight") or 0.0)
    return weight, (
        f"{report.get('status', 'unknown')}; stages {report.get('validated_stages')}; "
        f"weight {weight}"
    )
