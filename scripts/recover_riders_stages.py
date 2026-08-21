"""Recover + persist the CRITICAL riders+prices data and per-stage results for
TdF 2026 using the CORRECT ids (marketId 309 for riders; real stage ids
2799-2819 for stage results). urllib + system cert store (corporate TLS).
"""
from __future__ import annotations

import io
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SSL_CTX = ssl.create_default_context()
REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "data" / "scorito" / "tdf2026"
OUT.mkdir(parents=True, exist_ok=True)

MID, EID, CID = 309, 802, 25
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Origin": "https://www.scorito.com",
    "Referer": "https://www.scorito.com/",
}


def fetch(host: str, path: str) -> tuple[int, bytes]:
    url = f"https://{host}/{path}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:  # noqa: BLE001
        print("   ERR", repr(e)[:80])
        return 0, b""


def content_len(body: bytes) -> int | str:
    try:
        j = json.loads(body)
        c = j.get("Content") if isinstance(j, dict) else j
        return len(c) if isinstance(c, (list, dict)) else "?"
    except Exception:
        return "?"


def save(name: str, host: str, path: str) -> int | str:
    status, body = fetch(host, path)
    cl = content_len(body)
    if status == 200 and body:
        (OUT / name).write_bytes(body)
    flag = " <<<" if isinstance(cl, int) and cl > 0 else ""
    print(f"  [{status}] {name:<34} {len(body):>7}B content={cl}{flag}")
    return cl


print("=== CRITICAL: riders + prices (marketId 309) ===")
save("eventriderenriched.json", "cycling.scorito.com", f"cyclingmanager/v1.0/eventriderenriched/{MID}")
save("eventrider_qualities.json", "cycling.scorito.com", f"cycling/v2.0/eventrider/{EID}")

# read real stage ids from marketroundstage.json
mrs = json.loads((OUT / "marketroundstage.json").read_text(encoding="utf-8"))
rows = mrs.get("Content", mrs) if isinstance(mrs, dict) else mrs
stages = []
for r in rows:
    sid = r.get("StageId") or r.get("StageEventId") or r.get("EventId")
    mrid = r.get("MarketRoundId")
    order = r.get("StageOrder") or r.get("Order")
    if sid:
        stages.append((int(order or 0), int(sid), int(mrid or 0)))
stages.sort()
print(f"\n=== per-stage results for {len(stages)} real stages ===")
for order, sid, mrid in stages:
    save(f"stageresult_rider_{sid}.json", "cycling.scorito.com", f"cycling/v2.0/stageresult/rider/{sid}")
    save(f"stageresult_team_{sid}.json", "cycling.scorito.com", f"cycling/v2.0/stageresult/team/{sid}")
    save(f"stagereport_{sid}.json", "cycling.scorito.com", f"cycling/v2.0/stagereport/{sid}")
    save(f"stage_{sid}.json", "cycling.scorito.com", f"cycling/v2.0/stage/{sid}")
    if mrid:
        save(f"marketround_{mrid}.json", "cycling.scorito.com", f"cyclingmanager/v1.0/marketround/{mrid}")
    time.sleep(0.15)

print("\n=== done ===")
