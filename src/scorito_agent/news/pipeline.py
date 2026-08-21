"""End-to-end rider-news collection, analysis, state, and reporting."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .analysis import analyze_item, apply_corroboration, is_potentially_relevant, normalize
from .feeds import (
    FeedParseError,
    FetchError,
    HttpClient,
    article_url_allowed,
    extract_article_text,
    extract_youtube_transcript,
    parse_feed,
    source_urls,
    youtube_video_id,
)
from .mailer import SMTPConfig, send_digest
from .models import FeedItem, Highlight, Rider, Source


class PipelineError(RuntimeError):
    """Raised when the run cannot produce a trustworthy digest."""


@dataclass(slots=True)
class PipelineResult:
    report: dict[str, Any]
    highlights: list[Highlight]
    email_candidates: list[Highlight]
    emailed_count: int
    latest_json: Path
    latest_markdown: Path


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PipelineError(f"configuration not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PipelineError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"expected a JSON object in {path}")
    return value


def load_sources(path: Path) -> list[Source]:
    config = _load_json(path)
    raw_sources = config.get("sources")
    if not isinstance(raw_sources, list):
        raise PipelineError(f"sources must be a list in {path}")
    try:
        sources = [Source.from_dict(value) for value in raw_sources if isinstance(value, dict)]
    except (TypeError, ValueError) as exc:
        raise PipelineError(f"invalid source configuration in {path}: {exc}") from exc
    enabled = [source for source in sources if source.enabled]
    duplicate_ids = {source.id for source in enabled if sum(item.id == source.id for item in enabled) > 1}
    if duplicate_ids:
        raise PipelineError(f"duplicate source ids: {', '.join(sorted(duplicate_ids))}")
    if not enabled:
        raise PipelineError("no news sources are enabled")
    return enabled


def _slugify(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")


def _configured_riders(config: dict[str, Any]) -> dict[str, Rider]:
    raw_riders = config.get("riders")
    if not isinstance(raw_riders, list):
        raise PipelineError("watchlist riders must be a list")
    riders: dict[str, Rider] = {}
    for raw in raw_riders:
        if not isinstance(raw, dict) or not raw.get("name"):
            raise PipelineError("each watchlist rider must be an object with a name")
        name = str(raw["name"]).strip()
        slug = str(raw.get("slug") or _slugify(name))
        priority = int(raw.get("priority", 3))
        if priority not in (1, 2, 3):
            raise PipelineError(f"rider priority must be 1, 2, or 3: {name}")
        if slug in riders:
            raise PipelineError(f"duplicate rider slug in watchlist: {slug}")
        riders[slug] = Rider(
            slug=slug,
            name=name,
            aliases=tuple(str(alias) for alias in raw.get("aliases", [])),
            team=str(raw["team"]) if raw.get("team") else None,
            priority=priority,
            reasons=tuple(str(reason) for reason in raw.get("reasons", [])),
        )
    return riders


def _merge_live_market(riders: dict[str, Rider], race_id: str, external_root: Path) -> None:
    race_dir = external_root / "data" / "scorito" / race_id
    market_path = race_dir / "eventriderenriched.json"
    if market_path.exists():
        market = _load_json(market_path)
        content = market.get("Content")
        if isinstance(content, list):
            known_names = {normalize(rider.name): slug for slug, rider in riders.items()}
            for raw in content:
                if not isinstance(raw, dict):
                    continue
                name = f"{raw.get('FirstName', '')} {raw.get('LastName', '')}".strip()
                if not name:
                    continue
                normalized_name = normalize(name)
                slug = known_names.get(normalized_name, _slugify(name))
                if slug in riders:
                    continue
                riders[slug] = Rider(slug=slug, name=name, priority=3, reasons=("live_market",))

    recommendation_paths = (
        race_dir / "live_recommendation.json",
        race_dir / "projected_recommendation.json",
    )
    recommendation_path = next((path for path in recommendation_paths if path.exists()), None)
    if recommendation_path is None:
        return
    recommendation = _load_json(recommendation_path)
    selected = recommendation.get("recommendation", {}).get("squad", [])
    if not isinstance(selected, list):
        return
    selected_slugs = {
        str(item.get("rider_slug"))
        for item in selected
        if isinstance(item, dict) and item.get("rider_slug")
    }
    for slug in selected_slugs:
        rider = riders.get(slug)
        if rider and rider.priority > 1:
            riders[slug] = Rider(
                slug=rider.slug,
                name=rider.name,
                aliases=rider.aliases,
                team=rider.team,
                priority=1,
                reasons=tuple(sorted(set((*rider.reasons, "current_model_squad")))),
            )


def load_watchlist(path: Path, external_root: Path | None) -> tuple[dict[str, Any], list[Rider]]:
    config = _load_json(path)
    race = config.get("race")
    if not isinstance(race, dict) or not race.get("id") or not race.get("name"):
        raise PipelineError("watchlist race requires id and name")
    riders = _configured_riders(config)
    if external_root:
        _merge_live_market(riders, str(race["id"]), external_root)
    return race, sorted(riders.values(), key=lambda rider: (rider.priority, rider.name))


def _snapshot_metadata(race_id: str, external_root: Path | None) -> dict[str, Any] | None:
    if not external_root:
        return None
    race_dir = external_root / "data" / "scorito" / race_id
    candidates = [
        race_dir / "_manifest.json",
        race_dir / "eventriderenriched.json",
        race_dir / "projected_recommendation.json",
        race_dir / "live_recommendation.json",
    ]
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return None
    newest = max(existing, key=lambda path: path.stat().st_mtime)
    return {
        "directory": str(race_dir),
        "newest_file": newest.name,
        "snapshot_time": datetime.fromtimestamp(newest.stat().st_mtime, tz=UTC).isoformat(),
    }


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "seen": {}, "emailed": {}}
    state = _load_json(path)
    if not isinstance(state.get("seen", {}), dict) or not isinstance(state.get("emailed", {}), dict):
        raise PipelineError(f"invalid news state in {path}")
    state.setdefault("schema_version", 1)
    state.setdefault("seen", {})
    state.setdefault("emailed", {})
    return state


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# {report['race']['name']} rider-news digest",
        "",
        f"Generated: {report['generated_at']}",
        f"Watchlist: {report['watchlist_count']} riders; highlights: {report['highlight_count']}; new: {report['new_highlight_count']}",
    ]
    snapshot = report.get("market_snapshot")
    if snapshot:
        lines.append(f"Market snapshot: {snapshot['snapshot_time']} ({snapshot['newest_file']})")
    lines.extend(
        (
            "",
            "> Treat news as evidence, not a scoring guarantee. Reddit/community claims remain unverified until an independent source confirms them.",
            "",
            "## Selection impacts",
            "",
        )
    )
    impacts = report.get("selection_impacts", [])
    if impacts:
        for impact in impacts:
            lines.append(
                f"- **{impact['rider']}**: {impact['decision_hint']} "
                f"({impact['impact']}, {impact['verification']}) - [{impact['title']}]({impact['url']})"
            )
    else:
        lines.append("- No rider-specific selection impact found in the current freshness window.")
    lines.extend(("", "## Highlights", ""))
    for highlight in report["highlights"]:
        riders = ", ".join(rider["name"] for rider in highlight["riders"]) or "Race-wide"
        title = highlight["title"].replace("|", "\\|")
        lines.extend(
            (
                f"### [{title}]({highlight['url']})",
                f"- Riders: {riders}",
                f"- Source: {highlight['publisher'] or highlight['source']} (tier {highlight['source_tier']})",
                f"- Published: {highlight['published_at']}",
                f"- Signals: {', '.join(highlight['categories'])}; impact: {highlight['impact']}",
                f"- Verification: {highlight['verification']}; selection use: {highlight['decision_hint']}",
                f"- Evidence: {highlight['evidence']}",
                "",
            )
        )
    lines.extend(("## Source health", ""))
    for source in report["sources"]:
        suffix = f"; errors: {' | '.join(source['errors'])}" if source["errors"] else ""
        lines.append(f"- {source['name']}: {source['status']} ({source['items']} entries){suffix}")
    return "\n".join(lines) + "\n"


def _selection_impacts(highlights: list[Highlight]) -> list[dict[str, Any]]:
    def evidence_rank(highlight: Highlight) -> tuple[int, float]:
        if highlight.decision_hint == "review_selection_and_lineup":
            priority = 5
        elif highlight.impact == "negative":
            priority = 4
        elif (
            highlight.impact == "positive"
            and highlight.verification
            in {"official_source", "direct_interview", "corroborated_reports"}
        ):
            priority = 3
        elif highlight.decision_hint == "lineup_context_only":
            priority = 2
        else:
            priority = 1
        return priority, highlight.score

    strongest: dict[str, Highlight] = {}
    for highlight in highlights:
        for rider in highlight.riders:
            current = strongest.get(rider.slug)
            if current is None or evidence_rank(highlight) > evidence_rank(current):
                strongest[rider.slug] = highlight
    ordered = sorted(strongest.values(), key=lambda highlight: highlight.score, reverse=True)
    impacts = []
    for highlight in ordered:
        for rider in highlight.riders:
            if strongest.get(rider.slug) is not highlight:
                continue
            impacts.append(
                {
                    "rider": rider.name,
                    "rider_slug": rider.slug,
                    "priority": rider.priority,
                    "impact": highlight.impact,
                    "verification": highlight.verification,
                    "decision_hint": highlight.decision_hint,
                    "title": highlight.item.title,
                    "url": highlight.item.url,
                    "published_at": highlight.item.published_at.isoformat(),
                    "score": round(highlight.score, 2),
                }
            )
    return sorted(impacts, key=lambda item: (-item["score"], item["rider"]))


def _is_historical_title(title: str, current_year: int) -> bool:
    years = [int(value) for value in re.findall(r"\b20\d{2}\b", title)]
    return bool(years) and max(years) < current_year


def _trim_state_entries(values: dict[str, str], *, limit: int = 5_000) -> dict[str, str]:
    return dict(sorted(values.items(), key=lambda item: item[1], reverse=True)[:limit])


def _email_digest_items(highlights: list[Highlight], *, limit: int) -> list[Highlight]:
    selected: list[Highlight] = []
    seen_topics: set[tuple[tuple[str, ...], str, str]] = set()
    for highlight in highlights:
        rider_key = tuple(rider.slug for rider in highlight.riders) or ("race-wide",)
        primary_category = next(
            (category for category in highlight.categories if category not in {"interview", "general"}),
            highlight.categories[0],
        )
        topic = (rider_key, primary_category, highlight.impact)
        if topic in seen_topics:
            continue
        selected.append(highlight)
        seen_topics.add(topic)
        if len(selected) >= limit:
            break
    return selected


def run_pipeline(
    *,
    sources_path: Path,
    watchlist_path: Path,
    data_dir: Path,
    external_root: Path | None = None,
    now: datetime | None = None,
    max_age_hours: float | None = None,
    client: HttpClient | None = None,
    email_config: SMTPConfig | None = None,
    dry_run: bool = False,
) -> PipelineResult:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise PipelineError("run time must include a timezone")
    current = current.astimezone(UTC)
    sources = load_sources(sources_path)
    race, riders = load_watchlist(watchlist_path, external_root)
    race_terms = tuple(str(term) for term in race.get("terms", [race["name"]]))
    freshness_hours = float(max_age_hours or race.get("max_age_hours", 72))
    if freshness_hours <= 0:
        raise PipelineError("max_age_hours must be positive")
    minimum_successes = int(race.get("minimum_successful_sources", 4))
    minimum_score = float(race.get("minimum_highlight_score", 52))
    max_highlights = int(race.get("max_highlights", 50))
    max_email_highlights = int(race.get("max_email_highlights", 20))
    if not 0 <= minimum_score <= 100 or max_highlights < 1 or max_email_highlights < 1:
        raise PipelineError("highlight score and digest limits are invalid")
    http = client or HttpClient()

    source_reports: list[dict[str, Any]] = []
    feed_items: dict[str, tuple[FeedItem, Source]] = {}
    successful_sources = 0
    for source in sources:
        errors: list[str] = []
        parsed_count = 0
        successful_requests = 0
        urls = source_urls(source, riders)
        for batch_number, url in enumerate(urls, start=1):
            try:
                xml_text = http.get_text(url)
                parsed = parse_feed(xml_text, source, now=current)
            except (FetchError, FeedParseError) as exc:
                message = str(exc).replace(url, source.url)
                errors.append(f"batch {batch_number}: {message}")
                continue
            successful_requests += 1
            parsed_count += len(parsed)
            for item in parsed:
                feed_items[item.id] = (item, source)
        if successful_requests:
            successful_sources += 1
            status = "partial" if errors else "ok"
        else:
            status = "failed"
        source_reports.append(
            {
                "id": source.id,
                "name": source.name,
                "tier": source.tier,
                "status": status,
                "items": parsed_count,
                "requests": len(urls),
                "successful_requests": successful_requests,
                "errors": errors,
                "enrichment_errors": [],
                "discarded_historical": 0,
            }
        )

    if successful_sources < minimum_successes:
        failures = "; ".join(
            f"{source['name']}: {' | '.join(source['errors']) or 'no entries'}"
            for source in source_reports
            if source["status"] == "failed"
        )
        raise PipelineError(
            f"only {successful_sources} sources succeeded; minimum is {minimum_successes}. {failures}"
        )

    report_by_source = {source["id"]: source for source in source_reports}
    cutoff = current - timedelta(hours=freshness_hours)
    future_limit = current + timedelta(hours=6)
    highlights: list[Highlight] = []
    for item, source in feed_items.values():
        if item.published_at < cutoff or item.published_at > future_limit:
            continue
        if _is_historical_title(item.title, current.year):
            report_by_source[source.id]["discarded_historical"] += 1
            continue
        if not is_potentially_relevant(item, riders, race_terms):
            continue
        enrichment = ""
        if article_url_allowed(item.url, source):
            try:
                enrichment = extract_article_text(http.get_text(item.url))
            except (FetchError, FeedParseError) as exc:
                report_by_source[source.id]["enrichment_errors"].append(
                    f"{item.id}: article enrichment failed: {exc}"
                )
        if source.kind == "youtube":
            video_id = youtube_video_id(item.url)
            if video_id:
                watch_url = f"https://www.youtube.com/watch?v={video_id}"
                try:
                    watch_html = http.get_text(watch_url)
                    transcript = extract_youtube_transcript(watch_html, http)
                    if transcript:
                        enrichment = f"{enrichment} {transcript}".strip()
                except (FetchError, FeedParseError) as exc:
                    report_by_source[source.id]["enrichment_errors"].append(
                        f"{item.id}: transcript enrichment failed: {exc}"
                    )
        highlight = analyze_item(
            item,
            riders,
            race_terms=race_terms,
            article_text=enrichment,
            now=current,
        )
        if highlight:
            highlights.append(highlight)

    apply_corroboration(highlights)
    highlights = [highlight for highlight in highlights if highlight.score >= minimum_score]
    highlights.sort(key=lambda highlight: (highlight.score, highlight.item.published_at), reverse=True)
    highlights = highlights[:max_highlights]

    state_path = data_dir / "state.json"
    state = _load_state(state_path)
    seen: dict[str, str] = dict(state.get("seen", {}))
    emailed: dict[str, str] = dict(state.get("emailed", {}))
    for highlight in highlights:
        highlight.is_new = highlight.item.id not in seen
    pending_email = [highlight for highlight in highlights if highlight.item.id not in emailed]
    email_candidates = _email_digest_items(pending_email, limit=max_email_highlights)

    generated_at = current.isoformat()
    report = {
        "schema_version": 1,
        "race": {"id": race["id"], "name": race["name"]},
        "generated_at": generated_at,
        "market_snapshot": _snapshot_metadata(str(race["id"]), external_root),
        "watchlist_count": len(riders),
        "critical_rider_count": sum(rider.priority <= 2 for rider in riders),
        "source_success_count": successful_sources,
        "source_count": len(sources),
        "highlight_count": len(highlights),
        "new_highlight_count": sum(highlight.is_new for highlight in highlights),
        "pending_email_count": len(pending_email),
        "email_digest_count": len(email_candidates),
        "email_suppressed_duplicate_count": len(pending_email) - len(email_candidates),
        "selection_policy": (
            "Official/direct or independently corroborated negative availability news may trigger a selection review. "
            "Form, ambition, tactics, and community reports never cause an automatic model adjustment."
        ),
        "selection_impacts": _selection_impacts(highlights),
        "highlights": [highlight.to_dict() for highlight in highlights],
        "sources": source_reports,
    }

    latest_json = data_dir / "latest.json"
    latest_markdown = data_dir / "latest.md"
    if not dry_run:
        _atomic_json(latest_json, report)
        _atomic_text(latest_markdown, _markdown(report))
        history_path = data_dir / "history" / f"{current.strftime('%Y%m%dT%H%M%SZ')}.json"
        _atomic_json(history_path, report)
        history_files = sorted(history_path.parent.glob("*.json"), key=lambda path: path.stat().st_mtime)
        for old_path in history_files[:-120]:
            old_path.unlink()
        for highlight in highlights:
            seen[highlight.item.id] = generated_at
        state.update(
            {
                "schema_version": 1,
                "last_successful_run": generated_at,
                "seen": _trim_state_entries(seen),
                "emailed": _trim_state_entries(emailed),
            }
        )
        _atomic_json(state_path, state)

    emailed_count = 0
    if email_config and email_candidates and not dry_run:
        slot_label = current.strftime("%H:%M UTC")
        send_digest(
            email_config,
            race_name=str(race["name"]),
            generated_at=generated_at,
            slot_label=slot_label,
            highlights=email_candidates,
        )
        emailed_count = len(email_candidates)
        for highlight in pending_email:
            emailed[highlight.item.id] = generated_at
        state["emailed"] = _trim_state_entries(emailed)
        state["last_email_at"] = generated_at
        _atomic_json(state_path, state)

    return PipelineResult(
        report=report,
        highlights=highlights,
        email_candidates=email_candidates,
        emailed_count=emailed_count,
        latest_json=latest_json,
        latest_markdown=latest_markdown,
    )
