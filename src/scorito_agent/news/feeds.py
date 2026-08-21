"""HTTP, feed, article, and YouTube transcript parsing."""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Iterable

from .models import FeedItem, Rider, Source


class FetchError(RuntimeError):
    """Raised when a configured public source cannot be fetched safely."""


class FeedParseError(ValueError):
    """Raised when a configured feed does not contain parseable news entries."""


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float = 25.0,
        retries: int = 2,
        max_bytes: int = 6_000_000,
        user_agent: str = "Mozilla/5.0 (compatible; ScoritoRiderNews/1.0; +https://github.com/jvdlaar/scorito)",
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.max_bytes = max_bytes
        self.user_agent = user_agent

    def get_text(self, url: str) -> str:
        if not url.startswith("https://"):
            raise FetchError(f"refusing non-HTTPS URL: {url}")
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/html;q=0.9, */*;q=0.5",
                "Accept-Encoding": "gzip",
                "Accept-Language": "en,nl;q=0.9,da;q=0.7,fr;q=0.6,es;q=0.6",
            },
        )
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(self.max_bytes + 1)
                    if len(raw) > self.max_bytes:
                        raise FetchError(f"response exceeds {self.max_bytes} bytes: {url}")
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        raw = gzip.decompress(raw)
                    charset = response.headers.get_content_charset() or "utf-8"
                    declaration = re.match(br"\s*<\?xml[^>]+encoding=[\"']([^\"']+)", raw[:200], re.I)
                    if declaration:
                        charset = declaration.group(1).decode("ascii", errors="replace")
                    return raw.decode(charset, errors="replace")
            except FetchError:
                raise
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
        raise FetchError(f"GET failed after {self.retries + 1} attempts: {url}: {last_error}") from last_error


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.ignored_depth += 1
        elif tag in {"p", "br", "li", "div"} and not self.ignored_depth:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif tag in {"p", "li", "div"} and not self.ignored_depth:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def clean_text(value: str | None, *, limit: int | None = None) -> str:
    if not value:
        return ""
    parser = _PlainTextParser()
    parser.feed(value)
    text = re.sub(r"\s+", " ", html.unescape("".join(parser.parts))).strip()
    if limit and len(text) > limit:
        return text[: limit - 1].rstrip() + "..."
    return text


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(node: ET.Element, names: Iterable[str]) -> str:
    wanted = {name.lower() for name in names}
    for child in node:
        if _local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def _descendant_text(node: ET.Element, names: Iterable[str]) -> str:
    wanted = {name.lower() for name in names}
    for child in node.iter():
        if _local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def parse_datetime(value: str | None, *, default: datetime) -> datetime:
    if not value:
        return default.astimezone(UTC)
    candidate = value.strip()
    try:
        parsed = parsedate_to_datetime(candidate)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError as exc:
            raise FeedParseError(f"unsupported publication date {candidate!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def canonical_url(url: str) -> str:
    value = html.unescape(url.strip())
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return value
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [
        (key, val)
        for key, val in query
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    ]
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, urllib.parse.urlencode(query), "")
    )


def _item_id(source_id: str, url: str, title: str) -> str:
    identity = canonical_url(url) or re.sub(r"\s+", " ", title.lower()).strip()
    return hashlib.sha256(f"{source_id}|{identity}".encode("utf-8")).hexdigest()[:24]


def _atom_link(entry: ET.Element) -> str:
    fallback = ""
    for child in entry:
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href", "").strip()
        if not href:
            continue
        if child.attrib.get("rel", "alternate") == "alternate":
            return href
        fallback = fallback or href
    return fallback


def _make_item(
    source: Source,
    *,
    title: str,
    url: str,
    summary: str,
    published: str | None,
    now: datetime,
    publisher: str | None = None,
    author: str | None = None,
) -> FeedItem | None:
    clean_title = clean_text(title, limit=500)
    clean_url = canonical_url(url)
    if not clean_title or not clean_url:
        return None
    return FeedItem(
        id=_item_id(source.id, clean_url, clean_title),
        source_id=source.id,
        source_name=source.name,
        source_kind=source.kind,
        source_tier=source.tier,
        source_official=source.official,
        same_day_tactics=source.same_day_tactics,
        verification_required=source.verification_required,
        title=clean_title,
        url=clean_url,
        summary=clean_text(summary, limit=2_000),
        published_at=parse_datetime(published, default=now),
        publisher=(
            None
            if clean_text(publisher, limit=120).startswith(("http://", "https://"))
            else clean_text(publisher, limit=120) or None
        ),
        author=clean_text(author, limit=120) or None,
    )


def parse_feed(xml_text: str, source: Source, *, now: datetime) -> list[FeedItem]:
    try:
        root = ET.fromstring(xml_text.lstrip("\ufeff \t\r\n"))
    except ET.ParseError as exc:
        raise FeedParseError(f"invalid XML from {source.name}: {exc}") from exc

    items: list[FeedItem] = []
    root_name = _local_name(root.tag)
    if root_name == "urlset":
        for node in root:
            if _local_name(node.tag) != "url":
                continue
            item = _make_item(
                source,
                title=_descendant_text(node, ("title",)),
                url=_child_text(node, ("loc",)),
                summary="",
                published=_descendant_text(node, ("publication_date", "lastmod")),
                now=now,
            )
            if item:
                items.append(item)
    elif root_name in {"feed"}:
        for entry in root:
            if _local_name(entry.tag) != "entry":
                continue
            item = _make_item(
                source,
                title=_child_text(entry, ("title",)),
                url=_atom_link(entry),
                summary=_child_text(entry, ("summary", "content", "description")),
                published=_child_text(entry, ("published", "updated", "date")),
                now=now,
                author=_descendant_text(entry, ("name", "author")),
            )
            if item:
                items.append(item)
    else:
        for entry in root.iter():
            if _local_name(entry.tag) != "item":
                continue
            item = _make_item(
                source,
                title=_child_text(entry, ("title",)),
                url=_child_text(entry, ("link", "guid")),
                summary=_child_text(entry, ("description", "encoded", "summary", "content")),
                published=_child_text(entry, ("pubdate", "published", "updated", "date")),
                now=now,
                publisher=_child_text(entry, ("source",)),
                author=_child_text(entry, ("author", "creator")),
            )
            if item:
                items.append(item)

    if not items:
        if source.kind in {"google_news", "google_news_watchlist"}:
            return []
        raise FeedParseError(f"no news entries found in {source.name}")
    unique = {item.id: item for item in items}
    return sorted(unique.values(), key=lambda item: item.published_at, reverse=True)[: source.max_items]


def source_urls(source: Source, riders: list[Rider]) -> list[str]:
    if source.kind != "google_news_watchlist":
        return [source.url]
    critical = [rider for rider in riders if rider.priority <= 2]
    if not critical:
        return []
    locale = {
        "nl": ("nl", "NL", "NL:nl"),
        "da": ("da", "DK", "DK:da"),
        "fr": ("fr", "FR", "FR:fr"),
        "es": ("es", "ES", "ES:es"),
    }.get(source.language, ("en", "US", "US:en"))
    context = source.query_terms or ("Vuelta", "La Vuelta", "injury", "interview", "tactics", "form")
    urls: list[str] = []
    for start in range(0, len(critical), source.batch_size):
        batch = critical[start : start + source.batch_size]
        rider_clause = " OR ".join(f'"{rider.name}"' for rider in batch)
        context_clause = " OR ".join(f'"{term}"' if " " in term else term for term in context)
        parts = []
        if source.site_filter:
            parts.append(f"site:{source.site_filter}")
        parts.extend((f"({rider_clause})", f"({context_clause})"))
        query = " ".join(parts)
        params = urllib.parse.urlencode(
            {"q": query, "hl": locale[0], "gl": locale[1], "ceid": locale[2]}
        )
        urls.append(f"{source.url}?{params}")
    return urls


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.article_depth = 0
        self.paragraph_depth = 0
        self.script_depth = 0
        self.script_is_json = False
        self.meta_descriptions: list[str] = []
        self.paragraphs: list[str] = []
        self.current: list[str] = []
        self.json_scripts: list[str] = []
        self.current_script: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag == "meta":
            name = (attributes.get("property") or attributes.get("name") or "").lower()
            if name in {"description", "og:description", "twitter:description"}:
                self.meta_descriptions.append(attributes.get("content", ""))
        if tag == "article":
            self.article_depth += 1
        elif tag == "p" and self.article_depth:
            self.paragraph_depth += 1
            self.current = []
        elif tag == "script":
            self.script_depth += 1
            self.script_is_json = attributes.get("type", "").lower() == "application/ld+json"
            self.current_script = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self.paragraph_depth:
            text = clean_text("".join(self.current))
            if text:
                self.paragraphs.append(text)
            self.paragraph_depth -= 1
            self.current = []
        elif tag == "article" and self.article_depth:
            self.article_depth -= 1
        elif tag == "script" and self.script_depth:
            if self.script_is_json and self.current_script:
                self.json_scripts.append("".join(self.current_script))
            self.script_depth -= 1
            self.script_is_json = False
            self.current_script = []

    def handle_data(self, data: str) -> None:
        if self.paragraph_depth:
            self.current.append(data)
        if self.script_depth and self.script_is_json:
            self.current_script.append(data)


def _article_bodies(value: object) -> list[str]:
    if isinstance(value, dict):
        bodies = []
        body = value.get("articleBody")
        if isinstance(body, str):
            bodies.append(body)
        for nested in value.values():
            bodies.extend(_article_bodies(nested))
        return bodies
    if isinstance(value, list):
        bodies: list[str] = []
        for nested in value:
            bodies.extend(_article_bodies(nested))
        return bodies
    return []


def extract_article_text(html_text: str, *, limit: int = 20_000) -> str:
    parser = _ArticleParser()
    parser.feed(html_text)
    candidates = list(parser.paragraphs)
    for raw in parser.json_scripts:
        try:
            candidates.extend(_article_bodies(json.loads(raw)))
        except json.JSONDecodeError:
            continue
    if not candidates:
        candidates.extend(parser.meta_descriptions)
    return clean_text(" ".join(candidates), limit=limit)


def article_url_allowed(url: str, source: Source) -> bool:
    if not source.fetch_articles or not source.allowed_hosts:
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(host == allowed or host.endswith(f".{allowed}") for allowed in source.allowed_hosts)


def youtube_video_id(url: str) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0] or None
    if host.endswith("youtube.com"):
        query_id = urllib.parse.parse_qs(parsed.query).get("v", [None])[0]
        if query_id:
            return query_id
        match = re.search(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{6,})", parsed.path)
        if match:
            return match.group(1)
    return None


def extract_youtube_transcript(watch_html: str, client: HttpClient) -> str:
    marker = '"captionTracks":'
    index = watch_html.find(marker)
    if index < 0:
        return ""
    decoder = json.JSONDecoder()
    try:
        tracks, _ = decoder.raw_decode(watch_html[index + len(marker) :])
    except json.JSONDecodeError as exc:
        raise FeedParseError(f"invalid YouTube caption metadata: {exc}") from exc
    if not isinstance(tracks, list):
        return ""
    language_order = {code: rank for rank, code in enumerate(("en", "nl", "fr", "es", "da"))}
    valid_tracks = [track for track in tracks if isinstance(track, dict) and track.get("baseUrl")]
    valid_tracks.sort(
        key=lambda track: (
            language_order.get(str(track.get("languageCode", "")), 99),
            1 if str(track.get("kind", "")) == "asr" else 0,
        )
    )
    if not valid_tracks:
        return ""
    transcript_xml = client.get_text(html.unescape(str(valid_tracks[0]["baseUrl"])))
    try:
        root = ET.fromstring(transcript_xml)
    except ET.ParseError as exc:
        raise FeedParseError(f"invalid YouTube transcript XML: {exc}") from exc
    text = " ".join("".join(node.itertext()) for node in root.iter() if _local_name(node.tag) == "text")
    return clean_text(text, limit=30_000)
