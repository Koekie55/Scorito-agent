"""Fetch the signed-in user's personal Scorito squad (part 1).

Pulls the three *personal* cyclingmanager endpoints that require a Bearer token:

    cyclingmanager/v1.0/shortlist/{MARKET_ID}       -> your drafted 20 riders
    cyclingmanager/v1.0/teamselection/{MARKET_ID}    -> your saved team selection
    cyclingmanager/v1.0/stageselection/{MARKET_ID}   -> your per-stage 9+captain

The token comes from ``.env`` via :mod:`scorito_agent.scorito.auth` (paste your
browser oidc token — see ``auth.token_status()['instructions']``). Results are
saved to ``data/scorito/<slug>/personal/`` for ``compare_my_team.py`` to diff
against the enrolled-aware optimal-20 blueprint.

Corporate TLS interception => urllib + ssl.create_default_context (requests fails).

Usage:
    python scripts/fetch_my_team.py                 # defaults to market 309 / tdf2026
    python scripts/fetch_my_team.py 309 tdf2026
    python scripts/fetch_my_team.py 310 vuelta2026  # when the Vuelta market opens
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

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from scorito_agent.scorito import auth  # noqa: E402

SSL_CTX = ssl.create_default_context()
CYCLING = "cycling.scorito.com"

# Personal endpoints (401 without a Bearer token). {mid} = MARKET id (309 = TdF 2026).
PERSONAL_ENDPOINTS = {
    "shortlist": "cyclingmanager/v1.0/shortlist/{mid}",
    "teamselection": "cyclingmanager/v1.0/teamselection/{mid}",
    "stageselection": "cyclingmanager/v1.0/stageselection/{mid}",
}


def fetch(host: str, path: str, headers: dict[str, str]) -> tuple[int, bytes]:
    url = f"https://{host}/{path}"
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read() if hasattr(e, "read") else b""
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


def content_len(payload) -> int | str:
    return len(payload) if isinstance(payload, (list, dict)) else "?"


def main() -> int:
    market_id = int(sys.argv[1]) if len(sys.argv) > 1 else 309
    slug = sys.argv[2] if len(sys.argv) > 2 else "tdf2026"

    status = auth.token_status()
    print(f"Token status: have_token={status['have_token']} "
          f"expired={status['expired']} env={status['env_path']}")
    if not status["have_token"]:
        print("\n" + status["instructions"])
        print("\nNo token -> cannot fetch personal team. Paste your browser token "
              "into .env and re-run.")
        return 2
    if status["expired"]:
        print("WARNING: token is EXPIRED; the server will likely 401. "
              "Refresh it in the browser and re-paste.")

    headers = auth.bearer_headers(require=True)
    out = REPO / "data" / "scorito" / slug / "personal"
    out.mkdir(parents=True, exist_ok=True)

    print(f"\nFetching personal endpoints for market {market_id} ({slug}) "
          f"on {CYCLING} ...")
    any_ok = False
    unauthorized = False
    for name, tmpl in PERSONAL_ENDPOINTS.items():
        path = tmpl.format(mid=market_id)
        code, body = fetch(CYCLING, path, headers)
        payload = unwrap(body)
        cl = content_len(payload)
        ok = code == 200 and payload not in (None, [], {})
        if code == 200 and body:
            (out / f"{name}.json").write_bytes(body)
        if code in (401, 403):
            unauthorized = True
        any_ok = any_ok or ok
        flag = " <<<" if isinstance(cl, int) and cl > 0 else ""
        print(f"  [{code}] {name:<16} {len(body):>7}B content={cl}{flag}")

    if unauthorized:
        print("\n401/403 -> token rejected (expired or wrong scope). "
              "Re-copy a fresh access_token from the browser into .env.")
        print(auth.token_status()["instructions"])
        return 3
    if not any_ok:
        print("\nNo personal content returned. If you have not drafted a squad "
              f"in market {market_id}, there is nothing to fetch yet.")
        return 4

    print(f"\nSaved personal squad JSON to {out}")
    print("Next: python scripts/compare_my_team.py " + slug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
