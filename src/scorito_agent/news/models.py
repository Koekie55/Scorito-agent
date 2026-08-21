"""Typed data contracts used by the rider-news pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


VALID_SOURCE_KINDS = {
    "rss",
    "atom",
    "news_sitemap",
    "youtube",
    "reddit",
    "google_news",
    "google_news_watchlist",
}


@dataclass(frozen=True, slots=True)
class Source:
    id: str
    name: str
    url: str
    kind: str
    tier: int
    language: str = "en"
    enabled: bool = True
    official: bool = False
    same_day_tactics: bool = False
    verification_required: bool = False
    fetch_articles: bool = False
    max_items: int = 40
    allowed_hosts: tuple[str, ...] = ()
    site_filter: str | None = None
    query_terms: tuple[str, ...] = ()
    batch_size: int = 6

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Source:
        required = ("id", "name", "url", "kind", "tier")
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"source is missing fields: {', '.join(missing)}")
        kind = str(value["kind"])
        if kind not in VALID_SOURCE_KINDS:
            raise ValueError(f"unsupported source kind {kind!r}")
        tier = int(value["tier"])
        if tier not in (1, 2, 3, 4):
            raise ValueError("source tier must be 1, 2, 3, or 4")
        url = str(value["url"])
        if not url.startswith("https://"):
            raise ValueError(f"source URL must use HTTPS: {url}")
        source_id = str(value["id"]).strip()
        if not source_id or any(char.isspace() for char in source_id):
            raise ValueError(f"invalid source id {source_id!r}")
        max_items = int(value.get("max_items", 40))
        batch_size = int(value.get("batch_size", 6))
        if max_items < 1 or batch_size < 1:
            raise ValueError("max_items and batch_size must be positive")
        return cls(
            id=source_id,
            name=str(value["name"]).strip(),
            url=url,
            kind=kind,
            tier=tier,
            language=str(value.get("language", "en")),
            enabled=bool(value.get("enabled", True)),
            official=bool(value.get("official", False)),
            same_day_tactics=bool(value.get("same_day_tactics", False)),
            verification_required=bool(value.get("verification_required", False)),
            fetch_articles=bool(value.get("fetch_articles", False)),
            max_items=max_items,
            allowed_hosts=tuple(str(host).lower() for host in value.get("allowed_hosts", [])),
            site_filter=str(value["site_filter"]) if value.get("site_filter") else None,
            query_terms=tuple(str(term) for term in value.get("query_terms", [])),
            batch_size=batch_size,
        )


@dataclass(frozen=True, slots=True)
class Rider:
    slug: str
    name: str
    aliases: tuple[str, ...] = ()
    team: str | None = None
    priority: int = 3
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FeedItem:
    id: str
    source_id: str
    source_name: str
    source_kind: str
    source_tier: int
    source_official: bool
    same_day_tactics: bool
    verification_required: bool
    title: str
    url: str
    summary: str
    published_at: datetime
    publisher: str | None = None
    author: str | None = None


@dataclass(slots=True)
class Highlight:
    item: FeedItem
    riders: list[Rider]
    categories: list[str]
    evidence: str
    score: float
    confidence: float
    impact: str
    verification: str
    decision_hint: str
    corroborating_sources: list[str] = field(default_factory=list)
    is_new: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.item.id,
            "title": self.item.title,
            "url": self.item.url,
            "published_at": self.item.published_at.isoformat(),
            "source": self.item.source_name,
            "publisher": self.item.publisher,
            "source_tier": self.item.source_tier,
            "source_kind": self.item.source_kind,
            "riders": [
                {
                    "slug": rider.slug,
                    "name": rider.name,
                    "team": rider.team,
                    "priority": rider.priority,
                }
                for rider in self.riders
            ],
            "categories": self.categories,
            "evidence": self.evidence,
            "score": round(self.score, 2),
            "confidence": round(self.confidence, 2),
            "impact": self.impact,
            "verification": self.verification,
            "decision_hint": self.decision_hint,
            "corroborating_sources": self.corroborating_sources,
            "is_new": self.is_new,
        }
