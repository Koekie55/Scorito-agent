"""Shared HTTP + scraping helpers for the Scorito cycling agent.

Every sub-package (scorito, cyclingoracle, pcs) reuses these so we get the same
conventions everywhere:

* UTF-8 stdout (Windows consoles default to cp1252 and blow up on rider names
  with diacritics — call ``fix_stdout()`` at the top of any script).
* A polite, retrying HTTP GET with a browser User-Agent.
* A dead-simple on-disk cache so we never re-hammer a site while iterating and
  so tests can run against saved fixtures.

Design note: keep this dependency-light (stdlib + requests). It must import
cleanly even before the optional heavy deps (pandas/pulp/sklearn) are installed.
"""

from __future__ import annotations

import hashlib
import io
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import requests

DEFAULT_UA = os.environ.get(
    "HTTP_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ScoritoAgent/0.1",
)

# Repo root = three parents up from this file (src/scorito_agent/common/http.py).
REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"


def fix_stdout() -> None:
    """Force UTF-8 stdout/stderr so diacritics never raise UnicodeEncodeError."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            setattr(
                sys,
                stream_name,
                io.TextIOWrapper(buffer, encoding="utf-8", errors="replace"),
            )


def cache_path(namespace: str, key: str, suffix: str = ".html") -> Path:
    """Deterministic cache file path for ``key`` under ``data/<namespace>/``."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    directory = DATA_DIR / namespace
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{digest}{suffix}"


def resolve_proxies(
    namespace: str | None = None,
    explicit: dict[str, str] | str | None = None,
) -> dict[str, str] | None:
    """Work out which proxy (if any) to route a scrape through.

    Precedence:
      1. an ``explicit`` argument (dict of ``{"http":..,"https":..}`` or a bare
         URL string) always wins;
      2. otherwise env vars are consulted — PCS traffic prefers ``PCS_PROXY``
         then falls back to ``SCRAPER_PROXY``; everything else uses
         ``SCRAPER_PROXY``.

    Returns a ``requests``-style proxies dict, or ``None`` for a direct
    connection. This only affects the ``requests``-based scrapers
    (cyclingoracle / PCS); Scorito's own calls go through urllib elsewhere and
    are intentionally left direct (corporate TLS).
    """
    if isinstance(explicit, dict):
        return explicit or None
    if isinstance(explicit, str) and explicit.strip():
        url = explicit.strip()
        return {"http": url, "https": url}

    candidates: list[str] = []
    if namespace == "pcs":
        candidates = ["PCS_PROXY", "SCRAPER_PROXY"]
    else:
        candidates = ["SCRAPER_PROXY"]
    for name in candidates:
        url = os.environ.get(name, "").strip()
        if url:
            return {"http": url, "https": url}
    return None


def _urllib_get(
    url: str,
    *,
    params: dict[str, Any] | None,
    headers: dict[str, str],
    timeout: float,
    proxies: dict[str, str] | None,
) -> str:
    """Fetch ``url`` with stdlib urllib + the OS trust store.

    This is the corporate-network-safe transport. ``requests`` verifies TLS
    against certifi's bundle, which does NOT contain the corporate
    MITM/interception root CA installed in the Windows cert store — so every
    HTTPS GET from inside the corporate network raises
    ``SSLError(CERTIFICATE_VERIFY_FAILED)``. ``ssl.create_default_context()``
    loads the Windows system cert store (where that root IS trusted), exactly
    like Scorito's own urllib-based calls, so this path succeeds where
    ``requests`` cannot.
    """
    full_url = url
    if params:
        query = urllib.parse.urlencode(params)
        sep = "&" if "?" in url else "?"
        full_url = f"{url}{sep}{query}"

    # An explicit (possibly empty) ProxyHandler stops urllib from silently
    # picking up env proxies; empty dict == force a direct connection.
    proxy_handler = urllib.request.ProxyHandler(dict(proxies) if proxies else {})
    ctx = ssl.create_default_context()  # Windows system cert store on modern Python
    https_handler = urllib.request.HTTPSHandler(context=ctx)
    opener = urllib.request.build_opener(proxy_handler, https_handler)

    req = urllib.request.Request(full_url, headers=headers)
    with opener.open(req, timeout=timeout) as resp:  # raises HTTPError on 4xx/5xx
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace")


def get(
    url: str,
    *,
    namespace: str | None = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    cache: bool = True,
    ttl_seconds: float | None = None,
    retries: int = 3,
    backoff: float = 2.0,
    timeout: float = 30.0,
    proxies: dict[str, str] | str | None = None,
) -> str:
    """HTTP GET text with optional disk cache + retry/backoff.

    Args:
        namespace: cache sub-folder (e.g. "cyclingoracle", "pcs"). Required to
            enable caching.
        ttl_seconds: if set, ignore cache entries older than this.
        proxies: explicit proxy (dict or bare URL string). If omitted the proxy
            is resolved from env (``PCS_PROXY``/``SCRAPER_PROXY``) based on the
            namespace — see :func:`resolve_proxies`. Use this to reach
            Cloudflare-guarded sites (PCS/cyclingoracle) from a blocked IP.

    Transport: controlled by ``HTTP_TRANSPORT`` (default "auto"). "auto" tries
    ``requests`` first and, on ANY failure (notably the corporate-TLS
    ``CERTIFICATE_VERIFY_FAILED``), retries the same request via stdlib urllib +
    the OS trust store (:func:`_urllib_get`). Set "urllib" to skip ``requests``
    entirely, or "requests" to disable the fallback.
    """
    cache_key = url + ("?" + repr(sorted((params or {}).items())) if params else "")
    path = cache_path(namespace, cache_key) if (cache and namespace) else None

    if path and path.exists():
        fresh = ttl_seconds is None or (time.time() - path.stat().st_mtime) < ttl_seconds
        if fresh:
            return path.read_text(encoding="utf-8", errors="replace")

    merged_headers = {"User-Agent": DEFAULT_UA, "Accept-Language": "en,nl;q=0.8"}
    if headers:
        merged_headers.update(headers)

    resolved_proxies = resolve_proxies(namespace, proxies)
    transport = os.environ.get("HTTP_TRANSPORT", "auto").strip().lower() or "auto"

    def _store(text: str) -> str:
        if path:
            path.write_text(text, encoding="utf-8", errors="replace")
        return text

    last_exc: Exception | None = None
    for attempt in range(retries):
        # 1) requests transport (unless explicitly forced to urllib)
        if transport != "urllib":
            try:
                resp = requests.get(
                    url,
                    params=params,
                    headers=merged_headers,
                    timeout=timeout,
                    proxies=resolved_proxies,
                )
                resp.raise_for_status()
                return _store(resp.text)
            except Exception as exc:  # noqa: BLE001 — deliberately broad; retry any error
                last_exc = exc

        # 2) urllib + OS-trust-store fallback (or primary when forced)
        if transport != "requests":
            try:
                text = _urllib_get(
                    url,
                    params=params,
                    headers=merged_headers,
                    timeout=timeout,
                    proxies=resolved_proxies,
                )
                return _store(text)
            except Exception as exc:  # noqa: BLE001 — deliberately broad; retry any error
                last_exc = exc

        if attempt < retries - 1:
            time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url}") from last_exc


def get_json(url: str, **kwargs: Any) -> Any:
    """GET and parse JSON (uses the same cache/retry machinery)."""
    import json

    return json.loads(get(url, **kwargs))
