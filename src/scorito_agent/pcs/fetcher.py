"""Cache-first ProCyclingStats fetch helpers.

PCS is frequently Cloudflare-blocked from datacentre networks.  These helpers
only build known URL shapes and delegate HTTP/caching/retry behavior to the
shared ``scorito_agent.common.http`` module with ``namespace="pcs"``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from .slugs import slugify_rider

PCS_BASE_URL = "https://www.procyclingstats.com"
DEFAULT_TTL_SECONDS = 24 * 60 * 60

URL_TEMPLATES: dict[str, str] = {
    "rider": f"{PCS_BASE_URL}/rider/{{rider_slug}}",
    "search": f"{PCS_BASE_URL}/resources/search.php?searchfrom=&term={{query}}",
    "race": f"{PCS_BASE_URL}/race/{{race_slug}}/{{year}}",
    "race_startlist": f"{PCS_BASE_URL}/race/{{race_slug}}/{{year}}/startlist",
    "stage": f"{PCS_BASE_URL}/race/{{race_slug}}/{{year}}/{{stage_segment}}",
    "stage_result": f"{PCS_BASE_URL}/race/{{race_slug}}/{{year}}/{{stage_segment}}/result",
    "stage_startlist": f"{PCS_BASE_URL}/race/{{race_slug}}/{{year}}/{{stage_segment}}/startlist",
}


def _clean_path_part(value: str | int) -> str:
    return str(value).strip().strip("/")


def stage_segment(stage_no: str | int) -> str:
    """Return PCS's stage path segment (``stage-1`` unless already supplied)."""

    raw = _clean_path_part(stage_no).lower().replace("_", "-").replace(" ", "-")
    if raw.startswith("stage-") or raw in {"prologue", "gc"}:
        return raw
    return f"stage-{raw}"


def rider_page_url(
    first: str | Mapping[str, Any] | None = None,
    last: str | None = None,
    *,
    slug: str | None = None,
) -> str:
    rider_slug = slug or slugify_rider(first or "", last)
    return URL_TEMPLATES["rider"].format(rider_slug=_clean_path_part(rider_slug))


def rider_search_url(term: str) -> str:
    query = urlencode({"searchfrom": "", "term": term})
    return f"{PCS_BASE_URL}/resources/search.php?{query}"


def race_page_url(race_slug: str, year: int | str) -> str:
    return URL_TEMPLATES["race"].format(
        race_slug=_clean_path_part(race_slug), year=_clean_path_part(year)
    )


def race_startlist_url(race_slug: str, year: int | str) -> str:
    return URL_TEMPLATES["race_startlist"].format(
        race_slug=_clean_path_part(race_slug), year=_clean_path_part(year)
    )


def stage_page_url(race_slug: str, year: int | str, stage_no: int | str) -> str:
    return URL_TEMPLATES["stage"].format(
        race_slug=_clean_path_part(race_slug),
        year=_clean_path_part(year),
        stage_segment=stage_segment(stage_no),
    )


def stage_result_url(race_slug: str, year: int | str, stage_no: int | str) -> str:
    return URL_TEMPLATES["stage_result"].format(
        race_slug=_clean_path_part(race_slug),
        year=_clean_path_part(year),
        stage_segment=stage_segment(stage_no),
    )


def stage_startlist_url(race_slug: str, year: int | str, stage_no: int | str) -> str:
    return URL_TEMPLATES["stage_startlist"].format(
        race_slug=_clean_path_part(race_slug),
        year=_clean_path_part(year),
        stage_segment=stage_segment(stage_no),
    )


def _shared_get():
    try:
        from scorito_agent.common.http import get
    except ImportError as exc:
        raise RuntimeError(
            "PCS fetching requires the shared HTTP dependency stack "
            "(notably requests). Install the project requirements before live scraping."
        ) from exc
    return get


def _shared_get_json():
    try:
        from scorito_agent.common.http import get_json
    except ImportError as exc:
        raise RuntimeError(
            "PCS search requires the shared HTTP dependency stack "
            "(notably requests). Install the project requirements before live scraping."
        ) from exc
    return get_json


def fetch_url(
    url: str,
    *,
    cache: bool = True,
    ttl_seconds: float | None = DEFAULT_TTL_SECONDS,
    retries: int = 3,
    backoff: float = 2.0,
) -> str:
    return _shared_get()(
        url,
        namespace="pcs",
        cache=cache,
        ttl_seconds=ttl_seconds,
        retries=retries,
        backoff=backoff,
    )


def fetch_rider_page(
    first: str | Mapping[str, Any] | None = None,
    last: str | None = None,
    *,
    slug: str | None = None,
    **kwargs: Any,
) -> str:
    return fetch_url(rider_page_url(first, last, slug=slug), **kwargs)


def search_riders(term: str, **kwargs: Any) -> Any:
    return _shared_get_json()(rider_search_url(term), namespace="pcs", cache=True, **kwargs)


def fetch_race_page(race_slug: str, year: int | str, **kwargs: Any) -> str:
    return fetch_url(race_page_url(race_slug, year), **kwargs)


def fetch_race_startlist(race_slug: str, year: int | str, **kwargs: Any) -> str:
    return fetch_url(race_startlist_url(race_slug, year), **kwargs)


def fetch_stage_page(
    race_slug: str, year: int | str, stage_no: int | str, **kwargs: Any
) -> str:
    return fetch_url(stage_page_url(race_slug, year, stage_no), **kwargs)


def fetch_stage_result(
    race_slug: str, year: int | str, stage_no: int | str, **kwargs: Any
) -> str:
    return fetch_url(stage_result_url(race_slug, year, stage_no), **kwargs)


def fetch_stage_startlist(
    race_slug: str, year: int | str, stage_no: int | str, **kwargs: Any
) -> str:
    return fetch_url(stage_startlist_url(race_slug, year, stage_no), **kwargs)
