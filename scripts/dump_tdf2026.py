"""Snapshot ALL public Tour de France 2026 data points from Scorito to local disk.

Tour de France 2026 identifiers (resolved from the public bootstrap endpoints):
    marketId      = 309   ("France 2026", Fantasy Cycling, SportType=2)
    eventId       = 802
    competitionId = 25

Hosts:
    platform.scorito.com  -> market/*, event/*
    cycling.scorito.com   -> cycling/*, cyclingmanager/*
    league.scorito.com    -> league/*

Public (unauthenticated) data is dumped as raw JSON envelopes into
    data/scorito/tdf2026/<name>.json

Personal data (shortlist / teamselection / stageselection / participation) needs
a Bearer token and is handled separately (see dump_personal.py).

Run:  .venv\\Scripts\\python.exe scripts\\dump_tdf2026.py
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

# The corporate proxy does TLS interception; only the Windows system cert store
# trusts its root CA. ssl.create_default_context() loads the Windows store on
# Windows, so urllib works where requests+certifi fails with SSLError.
SSL_CTX = ssl.create_default_context()

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "data" / "scorito" / "tdf2026"
OUT.mkdir(parents=True, exist_ok=True)

PLATFORM = "platform.scorito.com"
CYCLING = "cycling.scorito.com"
LEAGUE = "league.scorito.com"

MID = 309   # marketId  Tour de France 2026
EID = 802   # eventId
CID = 25    # competitionId

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Origin": "https://www.scorito.com",
    "Referer": "https://www.scorito.com/",
}

SESSION_HEADERS = dict(HEADERS)

MANIFEST: list[dict] = []


def fetch(host: str, path: str, name: str, *, save: bool = True, quiet: bool = False):
    """GET https://{host}/{path}; save raw body to <name>.json. Returns parsed JSON or None."""
    url = f"https://{host}/{path}"
    req = urllib.request.Request(url, headers=SESSION_HEADERS)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25, context=SSL_CTX) as resp:
                status = resp.status
                body = resp.read().decode("utf-8", "replace")
            if status == 200 and body.strip():
                if save:
                    (OUT / f"{name}.json").write_text(body, encoding="utf-8")
                try:
                    parsed = json.loads(body)
                except Exception:
                    parsed = None
                n = len(body)
                MANIFEST.append({"name": name, "path": path, "host": host, "status": status, "bytes": n})
                if not quiet:
                    print(f"[200] {name:<34} {path}  ({n}B)")
                return parsed
            if attempt == 2:
                MANIFEST.append({"name": name, "path": path, "host": host, "status": status, "bytes": len(body)})
                if not quiet:
                    print(f"[{status}] {name:<34} {path}")
                return None
        except urllib.error.HTTPError as e:
            if attempt == 2:
                MANIFEST.append({"name": name, "path": path, "host": host, "status": e.code})
                if not quiet:
                    print(f"[{e.code}] {name:<34} {path}")
                return None
        except Exception as e:
            if attempt == 2:
                MANIFEST.append({"name": name, "path": path, "host": host, "status": "ERR", "error": repr(e)[:120]})
                if not quiet:
                    print(f"[ERR] {name:<34} {path}  {repr(e)[:100]}")
                return None
        time.sleep(1.5)
    return None


def content(parsed):
    """Unwrap the {ResultCode, ErrorMessage, Content} envelope if present."""
    if isinstance(parsed, dict) and "Content" in parsed:
        return parsed["Content"]
    return parsed


def collect_ids(obj, id_keys):
    """Recursively pull integer values for the given key names out of nested JSON."""
    found = set()

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


def main() -> None:
    print(f"== Dumping Tour de France 2026  market={MID} event={EID} comp={CID} ==")
    print(f"== out: {OUT} ==\n")

    # ---- Market / event level (platform) ----
    fetch(PLATFORM, f"market/v1.0/gameInfo/{MID}/2026", "gameInfo")
    fetch(PLATFORM, f"event/v1.0/eventlist/bymarket/{MID}", "eventlist_bymarket")
    fetch(PLATFORM, f"event/v1.0/eventlist/id/{EID}", "eventlist_id")
    fetch(PLATFORM, f"market/v2.0/market/{MID}", "market_v2")
    fetch(PLATFORM, f"market/v2.0/market/{MID}/previous/3", "market_previous3")
    fetch(PLATFORM, f"market/v1.0/game", "market_game")

    # ---- Manager game (cycling host) ----
    riders_parsed = fetch(CYCLING, f"cyclingmanager/v1.0/eventriderenriched/{EID}", "eventriderenriched")
    fetch(CYCLING, f"cyclingmanager/v1.0/marketenriched/{MID}", "marketenriched")
    fetch(CYCLING, f"cyclingmanager/v1.0/marketpoints/{MID}", "marketpoints")
    fetch(CYCLING, f"cyclingmanager/v1.0/points/market/{MID}", "points_market")
    fetch(CYCLING, f"cyclingmanager/v1.0/points/totalpoints/{MID}", "points_totalpoints")

    # ---- Live results / structure (cycling host) ----
    fetch(CYCLING, "cycling/v2.0/team", "teams_all")
    fetch(CYCLING, f"cycling/v2.0/eventteam/{EID}", "eventteam")
    stage_parsed = fetch(CYCLING, f"cycling/v2.0/stage/{EID}", "stage_event")
    stage_mkt = fetch(CYCLING, f"cycling/v2.0/stage/market/{MID}", "stage_market")
    fetch(CYCLING, f"cycling/v2.0/marketroundstage/{MID}", "marketroundstage")
    fetch(CYCLING, f"cycling/v2.0/classification/{EID}", "classification")
    fetch(CYCLING, f"cycling/v2.0/eventrider/dropouts/{EID}", "dropouts")

    # ---- Discover stageIds and loop per-stage ----
    stage_src = content(stage_parsed) or content(stage_mkt) or []
    if isinstance(stage_src, list) and stage_src:
        print("\n  stage object sample keys:", list(stage_src[0].keys()) if isinstance(stage_src[0], dict) else stage_src[0])
    stage_ids = collect_ids(stage_src, {"StageId", "Id", "EventStageId"})
    print(f"\n  discovered {len(stage_ids)} stage ids: {stage_ids}\n")
    for sid in stage_ids:
        fetch(CYCLING, f"cycling/v2.0/stageresult/rider/{sid}", f"stageresult_rider_{sid}", quiet=True)
        fetch(CYCLING, f"cycling/v2.0/stageresult/team/{sid}", f"stageresult_team_{sid}", quiet=True)
        fetch(CYCLING, f"cycling/v2.0/stagereport/{sid}", f"stagereport_{sid}", quiet=True)
    print(f"  dumped per-stage results/reports for {len(stage_ids)} stages")

    # ---- Discover riderIds and dump descriptions ----
    rc = content(riders_parsed) or []
    rider_ids = collect_ids(rc, {"RiderId"})
    print(f"\n  discovered {len(rider_ids)} rider ids")
    for i, rid in enumerate(rider_ids):
        fetch(CYCLING, f"cyclingmanager/v1.0/riderdescription/{rid}", f"riderdescription_{rid}", quiet=True)
        if (i + 1) % 25 == 0:
            print(f"    ...riderdescription {i + 1}/{len(rider_ids)}")
    print(f"  dumped {len(rider_ids)} rider descriptions")

    # ---- Manifest ----
    (OUT / "_manifest.json").write_text(json.dumps(MANIFEST, indent=2), encoding="utf-8")
    ok = sum(1 for m in MANIFEST if m.get("status") == 200)
    print(f"\n== DONE: {ok}/{len(MANIFEST)} endpoints returned data. Manifest -> _manifest.json ==")


if __name__ == "__main__":
    main()
