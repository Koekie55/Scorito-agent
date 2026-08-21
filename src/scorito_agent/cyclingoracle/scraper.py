"""Scraper/parsers for CyclingOracle (WielerOrakel) prediction pages.

The public site renders most prediction tables from data embedded in the
HTML.  A ``data-prediction-config`` attribute carries the public GraphQL
endpoint/key plus a compact list of ``riderId`` + Expected Win percentages;
the browser then POSTs that list to ``https://api.cyclingoracle.com/v1`` to
hydrate rider names/slugs/current teams.  This module keeps the parsers pure
and deterministic, while the network functions use the shared project HTTP
helper for GET requests and a tiny stdlib POST client for that GraphQL
hydration endpoint.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import shutil
import subprocess
import unicodedata
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

BASE_URL = "https://www.cyclingoracle.com"
DEFAULT_LANGUAGE = "nl"
NAMESPACE = "cyclingoracle"

RACES_URL = f"{BASE_URL}/{DEFAULT_LANGUAGE}/koersen"
BLOG_URL = f"{BASE_URL}/{DEFAULT_LANGUAGE}/blog"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DATA_DIR = _REPO_ROOT / "data" / NAMESPACE


def _http_get(
    url: str,
    *,
    cache: bool = True,
    ttl_seconds: float | None = None,
) -> str:
    """GET via the shared helper, namespaced into ``data/cyclingoracle``."""

    try:
        from scorito_agent.common.http import cache_path, get
    except Exception as exc:  # pragma: no cover - only hit in unprepared envs
        raise RuntimeError(
            "CyclingOracle live scraping requires the shared HTTP helper's "
            "dependencies (notably requests). Install project requirements or "
            "call the parse_* functions with saved HTML fixtures."
        ) from exc

    try:
        return get(url, namespace=NAMESPACE, cache=cache, ttl_seconds=ttl_seconds)
    except RuntimeError:
        path = cache_path(NAMESPACE, url) if cache else None
        if path and path.exists():
            return path.read_text(encoding="utf-8", errors="replace")

        curl = shutil.which("curl.exe") or shutil.which("curl")
        if not curl:
            raise
        completed = subprocess.run(
            [
                curl,
                "--location",
                "--silent",
                "--show-error",
                "--fail",
                "--user-agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ScoritoAgent/0.1",
                url,
            ],
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if path:
            path.write_text(completed.stdout, encoding="utf-8", errors="replace")
        return completed.stdout


def _absolute_url(url: str) -> str:
    return urllib.parse.urljoin(BASE_URL + "/", html_lib.unescape(url))


def _slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def _strip_tags(fragment: str) -> str:
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", fragment)
    text = re.sub(r"(?s)<[^>]+>", " ", fragment)
    return " ".join(html_lib.unescape(text).split())


def _parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\xa0", " ").replace("%", "").strip()
    if not text:
        return None
    text = re.sub(r"[^0-9,.\-]", "", text)
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._href: str | None = None
        self._classes: str = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attr = dict(attrs)
        href = attr.get("href")
        if href:
            self._href = _absolute_url(href)
            self._classes = attr.get("class") or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            self.links.append(
                {
                    "url": self._href,
                    "text": " ".join("".join(self._text).split()),
                    "class": self._classes,
                }
            )
            self._href = None
            self._classes = ""
            self._text = []


def list_races(*, html: str | None = None, cache: bool = True) -> list[dict[str, Any]]:
    """Return race links from the CyclingOracle race index."""

    html = html if html is not None else _http_get(RACES_URL, cache=cache)
    parser = _LinkCollector()
    parser.feed(html)
    races: dict[str, dict[str, Any]] = {}
    for link in parser.links:
        url = link["url"].split("?", 1)[0]
        if f"/{DEFAULT_LANGUAGE}/koersen/" not in url:
            continue
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        name = link["text"] or _slugify(slug).replace("-", " ").title()
        races[url] = {"name": name, "slug": slug, "url": url}
    return sorted(races.values(), key=lambda row: row["slug"])


def list_stages(
    race: str | dict[str, Any] | None = None,
    *,
    html: str | None = None,
    cache: bool = True,
) -> list[dict[str, Any]]:
    """Return prediction/datalist blog links, optionally filtered to a race."""

    html = html if html is not None else _http_get(BLOG_URL, cache=cache)
    parser = _LinkCollector()
    parser.feed(html)

    race_token = ""
    if isinstance(race, dict):
        race_token = _slugify(race.get("slug") or race.get("name") or "")
    elif race:
        race_token = _slugify(race)
    race_token = re.sub(r"-\d+$", "", race_token)

    stages: dict[str, dict[str, Any]] = {}
    for link in parser.links:
        url = link["url"].split("?", 1)[0]
        if f"/{DEFAULT_LANGUAGE}/blog/" not in url:
            continue
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        normalized_slug = _slugify(slug)
        if race_token and race_token not in normalized_slug:
            continue
        if not any(word in normalized_slug for word in ("voorspelling", "prediction", "datalijst", "datalist")):
            continue
        stage_number = _stage_number_from_text(normalized_slug)
        kind = "stage_prediction" if stage_number is not None else "race_prediction"
        if "datalijst" in normalized_slug or "datalist" in normalized_slug:
            kind = "data_list"
        stages[url] = {
            "url": url,
            "slug": slug,
            "title": link["text"] or slug.replace("-", " "),
            "race_filter": race_token or None,
            "stage_number": stage_number,
            "kind": kind,
        }
    return sorted(
        stages.values(),
        key=lambda row: (
            row["kind"] != "stage_prediction",
            row["stage_number"] if row["stage_number"] is not None else 999,
            row["slug"],
        ),
    )


def _stage_number_from_text(text: str) -> int | None:
    match = re.search(r"(?:etappe|stage)[-\s]?(\d{1,2})\b", text, flags=re.I)
    return int(match.group(1)) if match else None


def parse_prediction_configs(html: str) -> list[dict[str, Any]]:
    """Extract all ``data-prediction-config`` JSON blobs from a page."""

    configs: list[dict[str, Any]] = []
    pattern = re.compile(r"data-prediction-config=(['\"])(?P<value>.*?)(?<!\\)\1", re.S)
    for match in pattern.finditer(html):
        raw_value = html_lib.unescape(match.group("value"))
        try:
            config = json.loads(raw_value)
        except json.JSONDecodeError:
            continue
        if isinstance(config, dict):
            configs.append(config)
    return configs


def extract_stage_metadata(html: str, source_url: str = "") -> dict[str, Any]:
    """Extract stage/race title, route text and a coarse profile label."""

    h1s = [_strip_tags(match) for match in re.findall(r"(?is)<h1[^>]*>(.*?)</h1>", html)]
    page_title = h1s[0] if h1s else ""
    route_title = h1s[1] if len(h1s) > 1 else page_title
    title = route_title or page_title

    stage_number = _stage_number_from_text(" ".join([source_url, page_title, route_title]))
    race_name = re.split(r"\s+-\s+(?:voorspelling|prediction)", page_title, maxsplit=1, flags=re.I)[0]
    race_name = re.sub(r"\s+\d{4}$", "", race_name).strip() or None
    slug = source_url.rstrip("/").rsplit("/", 1)[-1] if source_url else ""

    # Keep public route/scenario text as model input but avoid related links/footer
    # because they can mention other classifications (e.g. "berg-klassement").
    prediction_start = html.lower().find("contentblock prediction")
    model_html = html[:prediction_start] if prediction_start > 0 else html
    body_match = re.search(r"(?is)<article\b.*?</article>", model_html)
    text = _strip_tags(body_match.group(0) if body_match else model_html)
    distance_match = re.search(r"(\d+(?:[,.]\d+)?)\s*km", text, flags=re.I)
    distance_km = _parse_number(distance_match.group(1)) if distance_match else None

    return {
        "source_url": source_url,
        "slug": slug,
        "page_title": page_title,
        "title": title,
        "race_name": race_name,
        "stage_number": stage_number,
        "distance_km": distance_km,
        "profile": classify_stage_text(text),
        "route_text": text,
    }


def classify_stage_text(text: str) -> str:
    """Heuristic route profile used by the transparent local model."""

    normalized = _slugify(text)
    if any(token in normalized for token in ("tijdrit", "time-trial", "itt", "proloog")):
        return "time_trial"
    if any(token in normalized for token in ("kassei", "cobble", "paris-roubaix")):
        return "cobble"
    has_mountain = any(token in normalized for token in ("berg", "bergetappe", "cols", "alpe-d-huez", "pyrenee", "alpen", "mountain"))
    has_hill = any(token in normalized for token in ("heuvel", "cote", "puncheur", "klassieker", "hilly", "montmartre"))
    has_sprint = any(token in normalized for token in ("sprint", "massasprint", "groepssprint", "vlak", "flat"))
    if has_mountain and not has_sprint:
        return "mountain"
    if has_mountain:
        return "hilly_mountain"
    if has_hill and has_sprint:
        return "hilly_sprint"
    if has_hill:
        return "hilly"
    if has_sprint:
        return "flat_sprint"
    return "mixed"


def stage_predictions(
    stage: str | dict[str, Any] | None = None,
    *,
    html: str | None = None,
    enrich: bool = False,
    cache: bool = True,
) -> list[dict[str, Any]]:
    """Return normalized Expected Win rows for one stage/prediction page.

    ``stage`` may be a URL, a stage dict returned by :func:`list_stages`, or
    omitted when ``html`` is supplied directly.
    """

    source_url = ""
    if isinstance(stage, dict):
        source_url = stage.get("url", "")
    elif isinstance(stage, str):
        source_url = stage
    if html is None:
        if not source_url:
            raise ValueError("stage_predictions requires a URL/stage dict or html=")
        html = _http_get(source_url, cache=cache)

    metadata = extract_stage_metadata(html, source_url)
    configs = parse_prediction_configs(html)
    prediction_config = next(
        (
            config
            for config in configs
            if any((row.get("col1") or row.get("searchQuery")) for row in config.get("predictions", []))
        ),
        configs[0] if configs else None,
    )
    if not prediction_config:
        return []

    details_by_id: dict[str, dict[str, Any]] = {}
    if enrich:
        details_by_id = {
            str(row.get("id")): row
            for row in fetch_rider_prediction_details(prediction_config, cache=cache)
            if row.get("id") is not None
        }

    rows: list[dict[str, Any]] = []
    for rank, raw in enumerate(prediction_config.get("predictions", []), start=1):
        rider_id = str(raw.get("riderId") or raw.get("col3") or "").strip()
        detail = details_by_id.get(rider_id, {})
        name = (
            detail.get("fullName")
            or raw.get("fullName")
            or raw.get("col1")
            or raw.get("searchQuery")
            or ""
        ).strip()
        percentage = _parse_number(raw.get("winPercentage") or raw.get("col2"))
        team = detail.get("currentTeam") or {}
        rows.append(
            {
                "source_url": source_url,
                "stage_slug": metadata["slug"],
                "race_name": metadata["race_name"],
                "stage_title": metadata["title"],
                "stage_number": metadata["stage_number"],
                "stage_profile": metadata["profile"],
                "predicted_rank": rank,
                "rider_id": rider_id or None,
                "rider_name": name or None,
                "rider_slug": detail.get("slug"),
                "team_id": team.get("id"),
                "team_name": team.get("name"),
                "team_slug": team.get("slug"),
                "win_probability_pct": percentage,
                "win_probability": percentage / 100.0 if percentage is not None else None,
                "raw": raw,
            }
        )
    return rows


def fetch_rider_prediction_details(
    prediction_config: dict[str, Any],
    *,
    cache: bool = True,
) -> list[dict[str, Any]]:
    """Hydrate embedded prediction rows through CyclingOracle's public GraphQL API."""

    api_url = prediction_config.get("apiUrl")
    api_key = prediction_config.get("apiKey")
    predictions = prediction_config.get("predictions") or []
    if not api_url or not api_key or not predictions:
        return []

    variables = {
        "riderPrediction": [
            {
                "riderId": str(row.get("riderId") or row.get("col3")),
                "winPercentage": row.get("winPercentage") or row.get("col2"),
            }
            for row in predictions
            if row.get("riderId") or row.get("col3")
        ]
    }
    payload = {
        "query": """
query RiderPredictions($riderPrediction: [GetRiderPredictionDto!]!) {
  riderPredictions(riderPrediction: $riderPrediction) {
    id
    fullName
    slug
    winPercentage
    currentTeam {
      id
      name
      slug
    }
  }
}
""",
        "variables": variables,
    }
    cache_key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    cache_file = _DATA_DIR / f"api_rider_predictions_{cache_key}.json"
    if cache and cache_file.exists():
        data = json.loads(cache_file.read_text(encoding="utf-8", errors="replace"))
    else:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            str(api_url),
            data=body,
            headers={
                "content-type": "application/json",
                "x-api-key": str(api_key),
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ScoritoAgent/0.1",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - public API URL from page config
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        if cache:
            cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return list((data.get("data") or {}).get("riderPredictions") or [])


def rider_stats(
    rider: str | dict[str, Any],
    *,
    html: str | None = None,
    cache: bool = True,
) -> dict[str, Any]:
    """Fetch/parse a rider page into normalized stats."""

    source_url = rider.get("url", "") if isinstance(rider, dict) else str(rider)
    if html is None:
        if not source_url.startswith("http"):
            source_url = f"{BASE_URL}/{DEFAULT_LANGUAGE}/renners/{source_url.strip('/')}"
        html = _http_get(source_url, cache=cache)
    return parse_rider_stats(html, source_url=source_url)


def parse_rider_stats(html: str, *, source_url: str = "") -> dict[str, Any]:
    """Parse the rider card and descriptive skill bullets from a rider page."""

    h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html)
    name = _strip_tags(h1.group(1)) if h1 else None
    slug_match = re.search(r'data-slug="([^"]+)"', html)
    rider_slug = html_lib.unescape(slug_match.group(1)) if slug_match else ""
    rider_id_match = re.search(r"-(\d+)$", rider_slug) or re.search(r"/renners/[^/]*-(\d+)", source_url)

    stats: dict[str, float] = {}
    average = re.search(r'(?is)<span class="stat stats-average">([^<]+)</span>', html)
    if average:
        value = _parse_number(average.group(1))
        if value is not None:
            stats["ovr"] = value

    stat_pattern = re.compile(
        r'(?is)<span class="stat stat-(?P<key>[^"]+)".*?'
        r'<span class="stat-text">(?P<label>[^<]+)</span>.*?'
        r'<span class="stat-value">(?P<value>[^<]+)</span>.*?</span>'
    )
    for match in stat_pattern.finditer(html):
        key = match.group("label").strip().lower()
        key = {"sp": "spr", "cob": "cob"}.get(key, key)
        value = _parse_number(match.group("value"))
        if value is not None:
            stats[key] = value

    for item in re.findall(r"(?is)<li[^>]*>(.*?)</li>", html):
        text = _strip_tags(item).lower()
        value_match = re.search(r"(\d+(?:[,.]\d+)?)\s+punten", text)
        value = _parse_number(value_match.group(1)) if value_match else None
        if value is None:
            continue
        skill = _skill_from_rider_bullet(text)
        if skill:
            stats[skill] = value

    team_match = re.search(r'(?is)<a class="[^"]*\bteam\b[^"]*"[^>]*href="(?P<url>[^"]+)"[^>]*>\s*<span>(?P<name>.*?)</span>', html)
    weight_match = re.search(r"(?is)<div class=\"dd\">Gewicht</div>.*?<div class=\"dt\">\s*(\d+(?:[,.]\d+)?)\s*kg", html)
    height_match = re.search(r"(?is)<div class=\"dd\">Lengte</div>.*?<div class=\"dt\">\s*(\d+(?:[,.]\d+)?)\s*cm", html)

    result: dict[str, Any] = {
        "source_url": source_url,
        "rider_id": rider_id_match.group(1) if rider_id_match else None,
        "rider_slug": rider_slug or (source_url.rstrip("/").rsplit("/", 1)[-1] if source_url else None),
        "rider_name": name,
        "team_name": _strip_tags(team_match.group("name")) if team_match else None,
        "team_url": _absolute_url(team_match.group("url")) if team_match else None,
        "height_cm": _parse_number(height_match.group(1)) if height_match else None,
        "weight_kg": _parse_number(weight_match.group(1)) if weight_match else None,
        "stats": stats,
    }
    result.update({f"skill_{key}": value for key, value in stats.items()})
    return result


def _skill_from_rider_bullet(text: str) -> str | None:
    mapping = [
        ("gemiddelde sterkte", "ovr"),
        ("kasseien", "cob"),
        ("heuvels", "hll"),
        ("bergen", "mtn"),
        ("algemeen klassement", "gc"),
        ("korte tijdrit", "short_itt"),
        ("lange tijdrit", "long_itt"),
        ("tijdrit", "itt"),
        ("sprint", "spr"),
        ("vlakke", "flat"),
        ("leadout", "leadout"),
        ("eendaagse wedstrijden", "one_day"),
        ("proloog", "prologue"),
    ]
    for needle, skill in mapping:
        if needle in text:
            return skill
    return None


class _DataListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.current_heading = ""
        self.heading_tag: str | None = None
        self.heading_text: list[str] = []
        self.in_li = False
        self.li_text: list[str] = []
        self.li_href = ""
        self.rows: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "h2":
            self.heading_tag = tag
            self.heading_text = []
        elif tag == "li":
            self.in_li = True
            self.li_text = []
            self.li_href = ""
        elif tag == "a" and self.in_li and attr.get("href"):
            self.li_href = _absolute_url(attr["href"] or "")

    def handle_data(self, data: str) -> None:
        if self.heading_tag:
            self.heading_text.append(data)
        if self.in_li:
            self.li_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self.heading_tag:
            self.current_heading = " ".join("".join(self.heading_text).split())
            self.heading_tag = None
            self.heading_text = []
        elif tag == "li" and self.in_li:
            text = " ".join("".join(self.li_text).split())
            row = _parse_data_list_item(self.current_heading, text, self.li_href)
            if row:
                self.rows.append(row)
            self.in_li = False
            self.li_text = []
            self.li_href = ""


def parse_data_lists(html: str, *, source_url: str = "") -> list[dict[str, Any]]:
    """Parse public "datalijsten" blog pages into long-form metric rows."""

    parser = _DataListParser()
    parser.feed(html)
    for row in parser.rows:
        row["source_url"] = source_url
    return parser.rows


def _parse_data_list_item(heading: str, text: str, href: str) -> dict[str, Any] | None:
    if not heading or not text:
        return None
    metric, unit = _metric_from_heading(heading)
    if not metric:
        return None
    skill_match = re.match(r"(?P<name>.+?)\s*\((?P<value>\d+(?:[,.]\d+)?)\)", text)
    physical_match = re.match(r"(?P<name>.+?)\s*[-–]\s*(?P<value>\d+(?:[,.]\d+)?)\s*(?P<unit>[a-zA-Z]+)?", text)
    match = skill_match or physical_match
    if not match:
        return None
    value = _parse_number(match.group("value"))
    if value is None:
        return None
    rider_id_match = re.search(r"-(\d+)(?:\D*$|%20)", href)
    return {
        "section": heading,
        "metric": metric,
        "rider_name": match.group("name").strip(),
        "rider_id": rider_id_match.group(1) if rider_id_match else None,
        "rider_url": href or None,
        "value": value,
        "unit": (match.groupdict().get("unit") or unit or "points").lower(),
    }


def _metric_from_heading(heading: str) -> tuple[str | None, str | None]:
    normalized = _slugify(heading)
    mapping = [
        ("ovr-skill", "ovr", "points"),
        ("gc-skill", "gc", "points"),
        ("itt-skill", "itt", "points"),
        ("mtn-skill", "mtn", "points"),
        ("spr-skill", "spr", "points"),
        ("hll-skill", "hll", "points"),
        ("meeste-vorm", "form", "points"),
        ("langst", "height_cm", "cm"),
        ("kortste", "height_cm", "cm"),
        ("zwaarste", "weight_kg", "kg"),
        ("lichtste", "weight_kg", "kg"),
    ]
    for needle, metric, unit in mapping:
        if needle in normalized:
            return metric, unit
    return None, None


def merge_feature_rows(*sources: Any) -> dict[str, dict[str, Any]]:
    """Merge datalist rows and rider-stat dicts into a rider feature table."""

    table: dict[str, dict[str, Any]] = {}

    def key_for(row: dict[str, Any]) -> str:
        return str(row.get("rider_id") or _slugify(row.get("rider_name") or "unknown"))

    for source in sources:
        items = source if isinstance(source, list) else [source]
        for item in items:
            if not isinstance(item, dict):
                continue
            key = key_for(item)
            entry = table.setdefault(
                key,
                {"rider_id": item.get("rider_id"), "rider_name": item.get("rider_name"), "features": {}},
            )
            if item.get("rider_name") and not entry.get("rider_name"):
                entry["rider_name"] = item["rider_name"]
            if item.get("metric"):
                entry["features"][item["metric"]] = item.get("value")
            for stat_key, stat_value in (item.get("stats") or {}).items():
                entry["features"][stat_key] = stat_value
            for field in ("height_cm", "weight_kg"):
                if item.get(field) is not None:
                    entry["features"][field] = item[field]
    return table


def write_jsonl(path: str | Path, rows: list[dict[str, Any]]) -> None:
    """Persist normalized rows as UTF-8 JSON Lines."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
