"""Tolerant stdlib HTML parsers for PCS rider, race, stage, and startlist pages."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from .slugs import slug_from_name

_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_PROFILE_TYPES = ("flat", "hilly", "mountain", "itt", "ttt")
_FINISH_TYPES = ("sprint", "flat", "uphill", "summit", "technical", "tt")
_PCS_PROFILE_TYPES = {
    "p1": "flat",
    "p2": "hilly",
    "p3": "hilly",
    "p4": "mountain",
    "p5": "mountain",
}
_PCS_FINISH_TYPES = {
    "p1": "sprint",
    "p2": "flat",
    "p3": "uphill",
    "p4": "flat",
    "p5": "summit",
}


@dataclass
class Element:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Element | str"] = field(default_factory=list)

    def get(self, name: str, default: str = "") -> str:
        return self.attrs.get(name, default)

    @property
    def classes(self) -> set[str]:
        return {part.strip().lower() for part in self.get("class").split() if part.strip()}

    def text(self, separator: str = " ") -> str:
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, Element):
                value = child.text(separator)
            else:
                value = unescape(child)
            if value:
                parts.append(value)
        return normalize_space(separator.join(parts))

    def iter(self, tag: str | None = None) -> list["Element"]:
        matches: list[Element] = []
        if tag is None or self.tag == tag.lower():
            matches.append(self)
        for child in self.children:
            if isinstance(child, Element):
                matches.extend(child.iter(tag))
        return matches

    def has_class_containing(self, needle: str) -> bool:
        needle = needle.lower()
        return any(needle in class_name for class_name in self.classes)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        elem = Element(tag.lower(), {key.lower(): (value or "") for key, value in attrs})
        self.stack[-1].children.append(elem)
        if elem.tag not in _VOID_TAGS:
            self.stack.append(elem)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        elem = Element(tag.lower(), {key.lower(): (value or "") for key, value in attrs})
        self.stack[-1].children.append(elem)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


def parse_html(html: str) -> Element:
    parser = _TreeBuilder()
    parser.feed(html or "")
    return parser.root


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _class_text(root: Element, *needles: str) -> str:
    for elem in root.iter():
        class_blob = " ".join(elem.classes)
        elem_id = elem.get("id").lower()
        if any(needle in class_blob or needle in elem_id for needle in needles):
            text = elem.text()
            if text:
                return text
    return ""


def _first_tag_text(root: Element, tag: str) -> str:
    for elem in root.iter(tag):
        text = elem.text()
        if text:
            return text
    return ""


def _meta_content(root: Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for elem in root.iter("meta"):
        name = (elem.get("name") or elem.get("property")).lower()
        if name in wanted and elem.get("content"):
            return elem.get("content")
    return ""


def _parse_float(text: str) -> float | None:
    cleaned = text.replace("\xa0", " ").strip()
    match = re.search(r"(\d+(?:[,.]\d+)?)", cleaned)
    if not match:
        return None
    return float(match.group(1).replace(",", "."))


def _parse_int(text: str) -> int | None:
    cleaned = re.sub(r"[^0-9]", "", text or "")
    return int(cleaned) if cleaned else None


def _parse_distance_km(root: Element) -> float | None:
    explicit = _class_text(root, "distance", "dist")
    if explicit and (value := _parse_float(explicit)) is not None:
        return value
    match = re.search(r"(\d+(?:[,.]\d+)?)\s*km\b", root.text(), re.IGNORECASE)
    return float(match.group(1).replace(",", ".")) if match else None


def _parse_vertical_meters(root: Element) -> int | None:
    detail = _detail_value(root, "vertical meters")
    if detail and (value := _parse_int(detail)) is not None:
        return value
    explicit = _class_text(root, "vertical", "elevation", "altitude")
    for source in (explicit, root.text()):
        if not source:
            continue
        match = re.search(
            r"(?:vertical|elevation|altitude|meters climbing|climbing)\D{0,30}([0-9][0-9\s.,]*)\s*m\b",
            source,
            re.IGNORECASE,
        ) or re.search(r"([0-9][0-9\s.,]*)\s*m\s*(?:vertical|elevation|climbing)", source, re.IGNORECASE)
        if match:
            return _parse_int(match.group(1))
    return None


def _detail_value(root: Element, label: str) -> str:
    """Return the value paired with a PCS race-detail label."""

    needle = label.strip().lower().rstrip(":")
    for item in root.iter("li"):
        title = next(
            (
                child
                for child in item.children
                if isinstance(child, Element) and "title" in child.classes
            ),
            None,
        )
        value = next(
            (
                child
                for child in item.children
                if isinstance(child, Element) and "value" in child.classes
            ),
            None,
        )
        if title is None or value is None:
            continue
        if title.text().strip().lower().rstrip(":") == needle:
            return value.text()
    return ""


def _pcs_profile_class(root: Element) -> str | None:
    for elem in root.iter():
        if "profile" not in elem.classes:
            continue
        for profile_class in _PCS_PROFILE_TYPES:
            if profile_class in elem.classes:
                return profile_class
    return None


def is_team_time_trial(text: str) -> bool:
    """Detect a team time trial from PCS wording or the ``(TTT)`` title marker.

    PCS writes the marker parenthesised (``S3 (TTT) Stage 3 - ...``), so a bare
    ``" ttt"`` substring test misses it.
    """
    lowered = text.lower()
    return "team time trial" in lowered or re.search(r"\bttt\b", lowered) is not None


def _infer_profile_type(root: Element) -> str:
    won_how = _detail_value(root, "won how").lower()
    # Order matters: "Team time trial" contains "time trial", so the team check
    # must run first or every TTT is misclassified as an individual time trial.
    if is_team_time_trial(won_how):
        return "ttt"
    if "time trial" in won_how:
        return "itt"
    explicit = _class_text(root, "profile-type", "profile")
    text = f"{explicit} {root.text()}".lower()
    if is_team_time_trial(text):
        return "ttt"
    profile_class = _pcs_profile_class(root)
    if profile_class:
        return _PCS_PROFILE_TYPES[profile_class]
    if "individual time trial" in text or "time trial" in text or " itt" in text:
        return "itt"
    if "mountain" in text or "high mountains" in text:
        return "mountain"
    if "hilly" in text or "hills" in text:
        return "hilly"
    if "flat" in text:
        return "flat"
    return "unknown"


def _infer_finish_type(root: Element, profile_type: str) -> str:
    if profile_type in {"itt", "ttt"}:
        return "tt"
    profile_class = _pcs_profile_class(root)
    if profile_class:
        return _PCS_FINISH_TYPES[profile_class]
    explicit = _class_text(root, "finish-type", "finish")
    text = f"{explicit} {root.text()}".lower()
    if "summit" in text or "mountain finish" in text:
        return "summit"
    if "uphill" in text or "hilltop" in text:
        return "uphill"
    if "sprint" in text or "bunch" in text:
        return "sprint"
    if "technical" in text:
        return "technical"
    if profile_type in {"itt", "ttt"}:
        return "tt"
    if "flat" in text:
        return "flat"
    return "unknown"


def _parse_date(root: Element) -> str | None:
    for elem in root.iter("time"):
        if elem.get("datetime"):
            return elem.get("datetime")[:10]
    explicit = _class_text(root, "date")
    text = f"{explicit} {root.text()}"
    iso = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if iso:
        return iso.group(1)
    named = re.search(r"\b(\d{1,2}\s+[A-Z][a-z]+\s+20\d{2})\b", text)
    return named.group(1) if named else None


def _parse_stage_no(root: Element) -> str | int | None:
    explicit = _class_text(root, "stage-no", "stage-number")
    match = re.search(r"stage\s*(\d+|[A-Za-z]+)", f"{explicit} {root.text()}", re.IGNORECASE)
    if not match:
        return None
    value = match.group(1)
    return int(value) if value.isdigit() else value.lower()


def _href_slug(href: str) -> str | None:
    # PCS anchor hrefs may be relative WITHOUT a leading slash
    # (e.g. "rider/jasper-philipsen") or absolute ("/rider/..." /
    # "https://.../rider/..."). Match the bare "rider/" needle so both forms
    # resolve; it is a substring of "/rider/" so full paths still match.
    if "rider/" not in href:
        return None
    path = urlparse(href).path or href
    if "rider/" not in path:
        path = href
    slug = path.split("rider/", 1)[-1].strip("/").split("/")[0]
    return slug or None


def _links(elem: Element) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for anchor in elem.iter("a"):
        href = anchor.get("href")
        text = anchor.text()
        if href or text:
            result.append({"href": href, "text": text})
    return result


def _table_rows(root: Element) -> list[list[dict[str, Any]]]:
    tables: list[list[dict[str, Any]]] = []
    for table in root.iter("table"):
        rows: list[dict[str, Any]] = []
        for tr in table.iter("tr"):
            cells = [child for child in tr.children if isinstance(child, Element) and child.tag in {"td", "th"}]
            if not cells:
                continue
            rows.append(
                {
                    "cells": cells,
                    "texts": [cell.text() for cell in cells],
                    "is_header": any(cell.tag == "th" for cell in cells),
                    "table_hint": " ".join([table.get("id"), table.get("class")]).lower(),
                }
            )
        if rows:
            tables.append(rows)
    return tables


def _header_indices(header: list[str]) -> dict[str, int]:
    cleaned = [re.sub(r"[^a-z#]+", "", h.lower()) for h in header]

    def find(*names: str) -> int | None:
        for name in names:
            for idx, value in enumerate(cleaned):
                if value == name or name in value:
                    return idx
        return None

    return {
        "rank": find("rank", "rnk", "pos", "#"),
        "rider": find("rider", "name"),
        "team": find("team"),
        "time": find("time"),
        "date": find("date"),
        "race": find("race"),
    }


def _rider_from_cell(cell: Element) -> tuple[str, str | None]:
    for link in _links(cell):
        slug = _href_slug(link.get("href", ""))
        if slug:
            return normalize_space(link.get("text")), slug
    name = cell.text()
    return name, slug_from_name(name) if name else None


def _cell_has_href(cell: Element, needle: str) -> bool:
    return any(needle in (anchor.get("href") or "") for anchor in cell.iter("a"))


def _team_from_cell(cell: Element) -> str | None:
    for anchor in cell.iter("a"):
        if "team/" in (anchor.get("href") or ""):
            text = normalize_space(anchor.text())
            if text:
                return text
    text = cell.text()
    return text or None


def _time_from_cell(cell: Element) -> str | None:
    """Collapse PCS' doubled time cell into a single value.

    PCS renders the finishing time twice (a visible fragment plus a hidden
    absolute copy), so the raw cell text looks like ``"3:53:11 3:53:11"`` for
    the winner and ``",, 0:00"`` for followers. Keep only digit-bearing tokens
    and dedupe them in order.
    """

    raw = normalize_space(cell.text())
    if not raw:
        return None
    tokens = [tok for tok in raw.split() if any(ch.isdigit() for ch in tok)]
    if not tokens:
        return raw
    seen: list[str] = []
    for tok in tokens:
        if tok not in seen:
            seen.append(tok)
    return " ".join(seen)


def _detect_result_columns(data_rows: list[dict[str, Any]]) -> dict[str, int | None]:
    """Infer column indices for icon-only PCS headers by inspecting cell content.

    PCS results tables use an icon-only header row (empty text, ``<td>`` not
    ``<th>``) so ``_header_indices`` finds nothing. Recover the true layout from
    the data cells: the rider column carries a ``rider/`` anchor, the team
    column a ``team/`` anchor, rank is the first integer cell, and time is the
    last ``:``-bearing cell (the finishing time, past intermediate gaps).

    Anchor hrefs on the live table are relative and slash-less
    (``rider/...`` / ``team/...``); matching the bare needle also matches the
    absolute ``/rider/`` / ``/team/`` forms used in test fixtures.
    """

    rank_idx: int | None = None
    rider_idx: int | None = None
    team_idx: int | None = None
    time_idx: int | None = None
    for row in data_rows:
        cells = row["cells"]
        if not cells:
            continue
        if rider_idx is None:
            for idx, cell in enumerate(cells):
                if _cell_has_href(cell, "rider/"):
                    rider_idx = idx
                    break
        if team_idx is None:
            for idx, cell in enumerate(cells):
                if _cell_has_href(cell, "team/") and not _cell_has_href(cell, "rider/"):
                    team_idx = idx
                    break
            if team_idx is None:
                for idx, cell in enumerate(cells):
                    if _cell_has_href(cell, "team/"):
                        team_idx = idx
                        break
        if rank_idx is None:
            for idx, cell in enumerate(cells):
                if _parse_int(cell.text()) is not None:
                    rank_idx = idx
                    break
        if time_idx is None:
            for idx in range(len(cells) - 1, -1, -1):
                if ":" in cells[idx].text():
                    time_idx = idx
                    break
        if None not in (rank_idx, rider_idx, team_idx, time_idx):
            break
    return {"rank": rank_idx, "rider": rider_idx, "team": team_idx, "time": time_idx}


def _parse_results_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    header: list[str] = []
    data_rows = rows
    if rows and (rows[0]["is_header"] or any("rider" in text.lower() for text in rows[0]["texts"])):
        header = rows[0]["texts"]
        data_rows = rows[1:]
    indices: dict[str, int | None] = dict(_header_indices(header)) if header else {}
    if indices.get("rider") is None:
        detected = _detect_result_columns(data_rows)
        for key, value in detected.items():
            if indices.get(key) is None and value is not None:
                indices[key] = value
    if indices.get("rider") is None:
        for key, value in {"rank": 0, "rider": 1, "team": 2, "time": 3}.items():
            if indices.get(key) is None:
                indices[key] = value
        if indices.get("rider") is None:
            indices["rider"] = 1 if any(len(row["cells"]) > 1 for row in data_rows) else 0
    results: list[dict[str, Any]] = []
    for row in data_rows:
        cells = row["cells"]
        rank_idx = indices.get("rank")
        rider_idx = indices.get("rider")
        if rider_idx is None or rider_idx >= len(cells):
            continue
        rank = _parse_int(cells[rank_idx].text()) if rank_idx is not None and rank_idx < len(cells) else None
        if rank is None:
            continue
        rider, slug = _rider_from_cell(cells[rider_idx])
        if not rider:
            continue
        team_idx = indices.get("team")
        time_idx = indices.get("time")
        results.append(
            {
                "rank": rank,
                "rider": rider,
                "rider_slug": slug,
                "team": _team_from_cell(cells[team_idx]) if team_idx is not None and team_idx < len(cells) else None,
                "time": _time_from_cell(cells[time_idx]) if time_idx is not None and time_idx < len(cells) else None,
            }
        )
    return results


def parse_results(html: str) -> list[dict[str, Any]]:
    """Extract stage result rows as rank/rider/team/time dictionaries."""

    root = parse_html(html)
    candidates: list[list[dict[str, Any]]] = []
    for rows in _table_rows(root):
        first_text = " ".join(rows[0]["texts"]).lower()
        table_hint = rows[0].get("table_hint", "")
        parsed = _parse_results_from_rows(rows)
        if parsed and ("result" in table_hint or "rank" in first_text or "rnk" in first_text or "pos" in first_text):
            candidates.append(parsed)
    if candidates:
        return max(candidates, key=len)
    all_candidates = [_parse_results_from_rows(rows) for rows in _table_rows(root)]
    all_candidates = [candidate for candidate in all_candidates if candidate]
    return max(all_candidates, key=len) if all_candidates else []


def parse_startlist(html: str) -> list[dict[str, Any]]:
    """Extract rider/team rows from a race or stage startlist page."""

    root = parse_html(html)
    # PCS's current race startlist is a nested list rather than a table.
    # Restrict parsing to this container so navigation/sidebar rider links are
    # never mistaken for entrants.
    for startlist in root.iter("ul"):
        if "startlist_v4" not in startlist.classes:
            continue
        riders: list[dict[str, Any]] = []
        seen: set[str] = set()
        team_items = [
            child
            for child in startlist.children
            if isinstance(child, Element) and child.tag == "li"
        ]
        for team_item in team_items:
            rider_container = next(
                (
                    elem
                    for elem in team_item.iter()
                    if "riderscont" in {class_name.lower() for class_name in elem.classes}
                ),
                None,
            )
            if rider_container is None:
                continue
            team = next(
                (
                    normalize_space(anchor.text())
                    for anchor in rider_container.iter("a")
                    if "team" in anchor.classes and anchor.text()
                ),
                None,
            )
            for anchor in rider_container.iter("a"):
                slug = _href_slug(anchor.get("href", ""))
                name = normalize_space(anchor.text())
                if not slug or not name or slug in seen:
                    continue
                seen.add(slug)
                riders.append({"rider": name, "rider_slug": slug, "team": team})
        return riders

    page_text = root.text().lower()
    allow_generic = "startlist" in page_text or "start list" in page_text
    riders: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rows in _table_rows(root):
        table_hint = rows[0].get("table_hint", "")
        header = rows[0]["texts"] if rows and rows[0]["is_header"] else []
        indices = _header_indices(header)
        result_like = indices.get("rank") is not None
        startlist_like = "start" in table_hint or (allow_generic and indices.get("rider") is not None and not result_like)
        if not startlist_like:
            continue
        current_team = None
        data_rows = rows[1:] if header else rows
        for row in data_rows:
            cells = row["cells"]
            row_links = [link for cell in cells for link in _links(cell) if _href_slug(link.get("href", ""))]
            if not row_links and len(cells) == 1 and cells[0].text():
                current_team = cells[0].text()
                continue
            team_idx = indices.get("team")
            team = cells[team_idx].text() if team_idx is not None and team_idx < len(cells) else current_team
            for link in row_links:
                slug = _href_slug(link.get("href", ""))
                name = normalize_space(link.get("text"))
                if not slug or slug in seen:
                    continue
                seen.add(slug)
                riders.append({"rider": name, "rider_slug": slug, "team": team})
    if riders:
        return riders
    if allow_generic:
        for link in _links(root):
            slug = _href_slug(link.get("href", ""))
            name = normalize_space(link.get("text"))
            if slug and slug not in seen:
                seen.add(slug)
                riders.append({"rider": name, "rider_slug": slug, "team": None})
    return riders


def _parse_startlist_quality(text: str) -> tuple[int | None, int | None]:
    values = [int(value) for value in re.findall(r"\d+", text or "")]
    if not values:
        return None, None
    return values[0], values[1] if len(values) > 1 else None


def parse_stage_page(html: str, *, source_url: str | None = None) -> dict[str, Any]:
    """Parse a PCS stage page into the normalized Stage schema."""

    root = parse_html(html)
    title = _first_tag_text(root, "title")
    h1 = _first_tag_text(root, "h1")
    race = _class_text(root, "race-name") or _meta_content(root, "og:title") or h1 or title
    stage_no = _parse_stage_no(root)
    profile_type = _infer_profile_type(root)
    startlist = parse_startlist(html)
    results = parse_results(html)
    startlist_quality, startlist_quality_finish = _parse_startlist_quality(
        _detail_value(root, "startlist quality score")
    )
    stage = {
        "race": normalize_space(race),
        "stage_no": stage_no,
        "date": _parse_date(root),
        "profile_type": profile_type,
        "distance_km": _parse_distance_km(root),
        "vertical_meters": _parse_vertical_meters(root),
        "finish_type": _infer_finish_type(root, profile_type),
        "profile_score": _parse_int(_detail_value(root, "profilescore")),
        "gradient_final_km": _parse_float(_detail_value(root, "gradient final km")),
        "race_ranking": _parse_int(_detail_value(root, "race ranking")),
        "startlist_quality_score": startlist_quality,
        "startlist_quality_finish_score": startlist_quality_finish,
        "startlist_count": len(startlist),
        "result_count": len(results),
        "departure": _detail_value(root, "departure") or None,
        "arrival": _detail_value(root, "arrival") or None,
        "startlist": startlist,
        "results": results,
    }
    if source_url:
        stage["source_url"] = source_url
    return stage


def parse_race_page(html: str, *, source_url: str | None = None) -> dict[str, Any]:
    """Parse high-level race metadata and links to stage pages."""

    root = parse_html(html)
    race = _class_text(root, "race-name") or _first_tag_text(root, "h1") or _first_tag_text(root, "title")
    stages: list[dict[str, str]] = []
    for link in _links(root):
        href = link.get("href", "")
        if "/stage-" in href:
            stages.append({"label": link.get("text", ""), "url": href})
    data = {"race": normalize_space(race), "stages": stages}
    if source_url:
        data["source_url"] = source_url
    return data


def parse_rider_page(html: str, *, source_url: str | None = None) -> dict[str, Any]:
    """Parse a PCS rider page into the normalized Rider schema."""

    root = parse_html(html)
    name = _class_text(root, "rider-name") or _first_tag_text(root, "h1") or _first_tag_text(root, "title")
    team = _class_text(root, "current-team", "team") or None
    specialties: dict[str, int] = {}
    text = root.text()
    for label, points in re.findall(r"([A-Za-z][A-Za-z /-]{2,})\s+(\d{1,4})\s*(?:pts|points)?", text):
        if label.strip().lower() in {"points per specialty", "results"}:
            continue
        if any(word in label.lower() for word in ("sprint", "climb", "gc", "time", "one day", "hills")):
            specialties[normalize_space(label)] = int(points)
    recent_results: list[dict[str, Any]] = []
    source_year_match = re.search(r"/(\d{4})/?$", source_url or "")
    source_year = int(source_year_match.group(1)) if source_year_match else None
    for rows in _table_rows(root):
        table_hint = rows[0].get("table_hint", "")
        if "result" not in table_hint:
            continue
        current_event: str | None = None
        current_race_class: str | None = None
        for row in rows[1:] if rows and rows[0]["is_header"] else rows:
            texts = row["texts"]
            if len(texts) < 5 or texts[0].lower() == "date":
                continue
            rank = _parse_int(texts[1])
            race = normalize_space(texts[4])
            links = [link for cell in row["cells"] for link in _links(cell)]
            race_link = next(
                (link for link in links if "race/" in (link.get("href") or "")),
                None,
            )
            # PCS inserts a race-header row before its stage/classification rows.
            # Preserve that context so evidence can be weighted by race quality.
            if rank is None and race:
                current_event = race
                class_match = re.search(r"\(([^()]*(?:UWT|Pro|\.1|\.2|NC)[^()]*)\)", race)
                current_race_class = class_match.group(1) if class_match else None
                continue
            if rank is None or not race:
                continue
            distance = _parse_float(texts[6]) if len(texts) > 6 else None
            pcs_points = _parse_int(texts[7]) if len(texts) > 7 else None
            recent_results.append(
                {
                    "year": source_year,
                    "date": normalize_space(texts[0]) or None,
                    "rank": rank,
                    "race": race,
                    "event": current_event,
                    "race_class": current_race_class,
                    "result_url": (race_link or {}).get("href"),
                    "distance_km": distance,
                    "pcs_points": pcs_points,
                }
            )
    birth_date = None
    age = None
    birth_match = re.search(
        r"Date of birth:\s*(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})"
        r"\s*\(\s*(\d+)\s*\)",
        text,
        re.IGNORECASE,
    )
    if birth_match:
        month_names = {
            name.lower(): number
            for number, name in enumerate(
                (
                    "",
                    "January",
                    "February",
                    "March",
                    "April",
                    "May",
                    "June",
                    "July",
                    "August",
                    "September",
                    "October",
                    "November",
                    "December",
                )
            )
            if name
        }
        month = month_names.get(birth_match.group(2).lower())
        if month:
            birth_date = (
                f"{int(birth_match.group(3)):04d}-{month:02d}-"
                f"{int(birth_match.group(1)):02d}"
            )
        age = int(birth_match.group(4))

    rider = {
        "name": normalize_space(name),
        "rider_slug": slug_from_name(name) if name else None,
        "team": team,
        "birth_date": birth_date,
        "age": age,
        "specialties": specialties,
        "recent_results": recent_results,
    }
    if source_url:
        rider["source_url"] = source_url
    return rider
