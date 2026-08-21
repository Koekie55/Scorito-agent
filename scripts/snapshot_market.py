"""Parameterized Scorito market snapshotter.

Snapshots ALL public data points for a given cycling market (Tour / Giro /
Vuelta / classics) to data/scorito/<slug>/. Reuses the endpoint surface proven
on TdF 2026 (marketId 309). urllib + Windows system cert store because the
corporate proxy does TLS interception (requests+certifi fails with SSLError).

The one non-obvious rule learned the hard way:
    riders+prices come from  cyclingmanager/v1.0/eventriderenriched/{MARKET_ID}
    (the MARKET id, NOT the eventId) -- passing the eventId returns empty.

Usage:
    python scripts/snapshot_market.py <marketId> <slug>
    python scripts/snapshot_market.py 306 giro2026
    python scripts/snapshot_market.py 310 vuelta2026
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

PLATFORM = "platform.scorito.com"
CYCLING = "cycling.scorito.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Origin": "https://www.scorito.com",
    "Referer": "https://www.scorito.com/",
}

MANIFEST: list[dict] = []


def fetch(host: str, path: str) -> tuple[int, bytes]:
    url = f"https://{host}/{path}"
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                print("   ERR", repr(e)[:80])
                return 0, b""
            time.sleep(1.2)
    return 0, b""


def unwrap(body: bytes):
    try:
        j = json.loads(body)
    except Exception:
        return None
    return j.get("Content") if isinstance(j, dict) and "Content" in j else j


def content_len(body: bytes) -> int | str:
    c = unwrap(body)
    return len(c) if isinstance(c, (list, dict)) else "?"


def save(out: Path, name: str, host: str, path: str, *, quiet: bool = False) -> bytes:
    status, body = fetch(host, path)
    cl = content_len(body)
    ok = status == 200 and body and cl not in (0, "?")
    if status == 200 and body:
        (out / name).write_bytes(body)
    MANIFEST.append({"name": name, "host": host, "path": path, "status": status,
                     "bytes": len(body), "content": cl})
    if not quiet:
        flag = " <<<" if isinstance(cl, int) and cl > 0 else ""
        print(f"  [{status}] {name:<34} {len(body):>7}B content={cl}{flag}")
    return body if ok else b""


def collect_ids(obj, id_keys) -> list[int]:
    found: set[int] = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in id_keys and isinstance(v, int):
                    found.add(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for it in o:
                walk(it)

    walk(obj)
    return sorted(found)


def main(mid: int, slug: str) -> None:
    out = REPO / "data" / "scorito" / slug
    out.mkdir(parents=True, exist_ok=True)
    print(f"== Snapshot market={mid} slug={slug} -> {out} ==\n")

    # ---- CRITICAL: riders + prices (market id!) ----
    riders_body = save(out, "eventriderenriched.json", CYCLING,
                       f"cyclingmanager/v1.0/eventriderenriched/{mid}")
    riders = unwrap(riders_body) or []
    if not riders:
        print("\n  !! no riders for this market yet (not open). Stopping early.\n")
        (out / "_manifest.json").write_text(json.dumps(MANIFEST, indent=2), encoding="utf-8")
        return

    # ---- Market metadata / rules / budget ----
    save(out, "marketenriched.json", CYCLING, f"cyclingmanager/v1.0/marketenriched/{mid}")
    save(out, "marketpoints.json", CYCLING, f"cyclingmanager/v1.0/marketpoints/{mid}")
    save(out, "points_totalpoints.json", CYCLING, f"cyclingmanager/v1.0/points/totalpoints/{mid}")
    save(out, "market_v2.json", PLATFORM, f"market/v2.0/market/{mid}")
    save(out, "gameInfo.json", PLATFORM, f"market/v1.0/gameInfo/{mid}/2026")

    # ---- Discover eventId ----
    elist = unwrap(save(out, "eventlist_bymarket.json", PLATFORM,
                        f"event/v1.0/eventlist/bymarket/{mid}"))
    eids = collect_ids(elist, {"EventId", "Id"})
    eid = eids[0] if eids else None
    print(f"\n  discovered eventIds={eids} -> using {eid}")

    if eid:
        save(out, "eventrider_qualities.json", CYCLING, f"cycling/v2.0/eventrider/{eid}")
        save(out, "eventteam.json", CYCLING, f"cycling/v2.0/eventteam/{eid}")
        save(out, "classification.json", CYCLING, f"cycling/v2.0/classification/{eid}")
        save(out, "dropouts.json", CYCLING, f"cycling/v2.0/eventrider/dropouts/{eid}")
    save(out, "teams_all.json", CYCLING, "cycling/v2.0/team")

    # ---- Stages ----
    mrs_body = save(out, "marketroundstage.json", CYCLING, f"cycling/v2.0/marketroundstage/{mid}")
    save(out, "stage_market.json", CYCLING, f"cycling/v2.0/stage/market/{mid}")
    rows = unwrap(mrs_body) or []
    stages = []
    for r in rows if isinstance(rows, list) else []:
        sid = r.get("StageId") or r.get("StageEventId") or r.get("EventId")
        mrid = r.get("MarketRoundId")
        order = r.get("StageOrder") or r.get("Order")
        if sid:
            stages.append((int(order or 0), int(sid), int(mrid or 0)))
    stages.sort()
    print(f"\n  {len(stages)} stages: {[s[1] for s in stages]}\n")
    for order, sid, mrid in stages:
        save(out, f"stageresult_rider_{sid}.json", CYCLING, f"cycling/v2.0/stageresult/rider/{sid}", quiet=True)
        save(out, f"stageresult_team_{sid}.json", CYCLING, f"cycling/v2.0/stageresult/team/{sid}", quiet=True)
        save(out, f"stagereport_{sid}.json", CYCLING, f"cycling/v2.0/stagereport/{sid}", quiet=True)
        save(out, f"stage_{sid}.json", CYCLING, f"cycling/v2.0/stage/{sid}", quiet=True)
        time.sleep(0.12)
    print(f"  dumped per-stage files for {len(stages)} stages")

    (out / "_manifest.json").write_text(json.dumps(MANIFEST, indent=2), encoding="utf-8")
    ok = sum(1 for m in MANIFEST if isinstance(m.get("content"), int) and m["content"] > 0)
    print(f"\n== DONE {slug}: {ok}/{len(MANIFEST)} endpoints returned data ==")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python scripts/snapshot_market.py <marketId> <slug>")
        raise SystemExit(2)
    main(int(sys.argv[1]), sys.argv[2])
