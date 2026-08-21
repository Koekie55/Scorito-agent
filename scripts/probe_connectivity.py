"""Connectivity probe: what can this host actually reach?

Tests each target with a short timeout and prints status/latency/first-bytes.
Uses urllib + system SSL context (corporate TLS interception friendly).
Honours an optional proxy via the PROBE_PROXY env var (http://host:port).
"""
from __future__ import annotations

import io
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SSL_CTX = ssl.create_default_context()

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"

TARGETS = [
    ("scorito-cycling-public", "https://cycling.scorito.com/cyclingmanager/v1.0/eventriderenriched/309"),
    ("scorito-platform-root", "https://platform.scorito.com/"),
    ("scorito-www", "https://www.scorito.com/"),
    ("pcs", "https://www.procyclingstats.com/"),
    ("pcs-stage", "https://www.procyclingstats.com/race/tour-de-france/2025/stage-1"),
    ("cyclingoracle", "https://www.cyclingoracle.com/"),
    ("github-raw", "https://raw.githubusercontent.com/jvdlaar/scorito/master/README.md"),
    ("github-api", "https://api.github.com/repos/jvdlaar/scorito"),
    ("proxy-list-source", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
    ("httpbin-ip", "https://httpbin.org/ip"),
]


def probe(name: str, url: str, proxy: str | None = None) -> None:
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        # explicitly bypass any system proxy
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    t0 = time.time()
    try:
        with opener.open(req, timeout=12) as r:
            body = r.read(200)
            dt = (time.time() - t0) * 1000
            print(f"  [{name:24}] {r.status}  {dt:6.0f}ms  {len(body)}B  {body[:60]!r}")
    except urllib.error.HTTPError as e:
        dt = (time.time() - t0) * 1000
        print(f"  [{name:24}] HTTP {e.code}  {dt:6.0f}ms  (server responded)")
    except Exception as e:  # noqa: BLE001
        dt = (time.time() - t0) * 1000
        print(f"  [{name:24}] ERR  {dt:6.0f}ms  {type(e).__name__}: {str(e)[:70]}")


if __name__ == "__main__":
    proxy = os.environ.get("PROBE_PROXY") or None
    print(f"Direct connectivity probe (proxy={proxy!r}):")
    for name, url in TARGETS:
        probe(name, url, proxy=proxy)
