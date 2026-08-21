"""Locate the riders+prices endpoint by (a) grepping the SPA JS bundles for URL
fragments and (b) probing candidate endpoints live via urllib+system cert store.
"""
from __future__ import annotations

import io
import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SSL_CTX = ssl.create_default_context()
REPO = Path(__file__).resolve().parents[1]
BUNDLE_DIR = REPO / "data" / "scorito" / "_bundle"

MID, EID, CID = 309, 802, 25
STAGE0 = 2799  # first real TdF2026 stage id
MR0 = 7298     # first MarketRoundId

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Origin": "https://www.scorito.com",
    "Referer": "https://www.scorito.com/",
}

# ---------------------------------------------------------------- grep bundle
print("=== 1. Grepping JS bundles for rider/value/enriched URL fragments ===")
pat = re.compile(
    r'["\']([a-zA-Z0-9]+/v[0-9.]+/[a-zA-Z0-9/_.-]*'
    r'(?:rider|enrich|value|price|market|budget|team|shortlist|selection)'
    r'[a-zA-Z0-9/_.-]*)["\']',
    re.IGNORECASE,
)
frags: set[str] = set()
for f in sorted(BUNDLE_DIR.glob("*.js")):
    txt = f.read_text(encoding="utf-8", errors="replace")
    for m in pat.finditer(txt):
        frags.add(m.group(1))
# also catch concat-style fragments like "cyclingmanager/v1.0/eventriderenriched/"
pat2 = re.compile(r'["\']((?:cyclingmanager|cycling|market|event)/v[0-9.]+/[a-zA-Z0-9/_-]+)["\']')
for f in sorted(BUNDLE_DIR.glob("*.js")):
    txt = f.read_text(encoding="utf-8", errors="replace")
    for m in pat2.finditer(txt):
        frags.add(m.group(1))

rider_frags = sorted(x for x in frags if re.search(r"rider|enrich|value|budget|shortlist|selection", x, re.I))
print(f"  {len(rider_frags)} rider/value/selection-related fragments:")
for fr in rider_frags:
    print("   ", fr)

# --------------------------------------------------------------- live probes
print("\n=== 2. Probing candidate rider/price endpoints ===")
CANDIDATES = [
    ("cycling.scorito.com", f"cyclingmanager/v1.0/eventriderenriched/{EID}"),
    ("cycling.scorito.com", f"cyclingmanager/v1.0/eventriderenriched/{MID}"),
    ("cycling.scorito.com", f"cyclingmanager/v1.0/eventriderenriched/{STAGE0}"),
    ("cycling.scorito.com", f"cyclingmanager/v2.0/eventriderenriched/{EID}"),
    ("cycling.scorito.com", f"cyclingmanager/v2.0/eventriderenriched/{MID}"),
    ("cycling.scorito.com", f"cyclingmanager/v1.0/marketrider/{MID}"),
    ("cycling.scorito.com", f"cyclingmanager/v1.0/marketriderenriched/{MID}"),
    ("cycling.scorito.com", f"cyclingmanager/v1.0/riderenriched/{MID}"),
    ("cycling.scorito.com", f"cyclingmanager/v1.0/ridervalue/{MID}"),
    ("cycling.scorito.com", f"cyclingmanager/v2.0/ridermarketvalue/{MID}"),
    ("cycling.scorito.com", f"cyclingmanager/v1.0/marketvalue/{MID}"),
    ("cycling.scorito.com", f"cyclingmanager/v1.0/budget/{MID}"),
    ("cycling.scorito.com", f"cycling/v2.0/eventrider/{EID}"),
    ("cycling.scorito.com", f"cycling/v2.0/eventrider/event/{EID}"),
    ("cycling.scorito.com", f"cycling/v2.0/rider/event/{EID}"),
    ("cycling.scorito.com", f"cycling/v2.0/rider/market/{MID}"),
    ("cycling.scorito.com", f"cycling/v2.0/eventrider/market/{MID}"),
    ("cycling.scorito.com", f"cyclingmanager/v1.0/eventrider/{EID}"),
    ("cycling.scorito.com", f"cyclingmanager/v1.0/rider/market/{MID}"),
    ("platform.scorito.com", f"market/v1.0/marketrider/{MID}"),
]


def probe(host: str, path: str) -> None:
    url = f"https://{host}/{path}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            body = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        print(f"  [{e.code}] {path}")
        return
    except Exception as e:  # noqa: BLE001
        print(f"  [ERR] {path}  {repr(e)[:80]}")
        return
    n = len(body)
    try:
        j = json.loads(body)
        c = j.get("Content") if isinstance(j, dict) else j
        cnt = len(c) if isinstance(c, (list, dict)) else "?"
    except Exception:
        cnt = "?"
    flag = "  <<< HAS DATA" if isinstance(cnt, int) and cnt > 0 else ""
    print(f"  [{status}] {path:<52} {n}B content={cnt}{flag}")
    if isinstance(cnt, int) and cnt > 0 and n > 200:
        sample = c[0] if isinstance(c, list) else c
        print("        sample keys:", list(sample.keys())[:20] if isinstance(sample, dict) else str(sample)[:150])


for host, path in CANDIDATES:
    probe(host, path)

print("\n=== done ===")
