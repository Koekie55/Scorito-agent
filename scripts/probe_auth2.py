"""Map Scorito's /api/account/* auth family and personal cyclingmanager
endpoints — existence probes only (no credential POST here).

401/403 => endpoint exists but needs auth (what we want to find).
404 => wrong shape.

Run:
    $env:NO_PROXY="*"; .\.venv\Scripts\python.exe scripts\probe_auth2.py
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request

CTX = ssl.create_default_context()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ScoritoAgent/0.1"

ACCOUNT_PATHS = [
    "https://www.scorito.com/api/account/login",
    "https://www.scorito.com/api/account/me",
    "https://www.scorito.com/api/account/token",
    "https://www.scorito.com/api/account/refresh",
    "https://www.scorito.com/api/account/refreshtoken",
    "https://www.scorito.com/api/account/logout",
    "https://www.scorito.com/api/account",
    "https://www.scorito.com/api/user/me",
    "https://www.scorito.com/api/auth/login",
    "https://www.scorito.com/api/token",
]

# Personal cyclingmanager endpoints (need a Bearer/cookie). MID=309 (TdF 2026).
MID = 309
PERSONAL = [
    f"https://cycling.scorito.com/cyclingmanager/v1.0/shortlist/{MID}",
    f"https://cycling.scorito.com/cyclingmanager/v2.0/shortlist/{MID}",
    f"https://cycling.scorito.com/cyclingmanager/v1.0/teamselection/{MID}",
    f"https://cycling.scorito.com/cyclingmanager/v2.0/teamselection/{MID}",
    f"https://cycling.scorito.com/cyclingmanager/v1.0/stageselection/{MID}",
    f"https://cycling.scorito.com/cyclingmanager/v2.0/stageselection/{MID}",
    f"https://cycling.scorito.com/cyclingmanager/v1.0/myteam/{MID}",
    f"https://cycling.scorito.com/cyclingmanager/v1.0/team/{MID}",
    f"https://cycling.scorito.com/cyclingmanager/v1.0/participation/{MID}",
    f"https://cycling.scorito.com/cyclingmanager/v1.0/usershortlist/{MID}",
]


def _req(url: str, method: str = "GET") -> tuple[int, str, dict]:
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json, text/plain, */*")
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=15) as resp:
            return resp.status, resp.read(300).decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(300).decode("utf-8", "replace")
        except Exception:
            pass
        return e.code, body, dict(e.headers or {})
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}", {}


def main() -> None:
    print("== /api/account family (OPTIONS) ==")
    for url in ACCOUNT_PATHS:
        status, body, hdrs = _req(url, "OPTIONS")
        allow = hdrs.get("Allow") or hdrs.get("allow") or ""
        print(f"[OPTIONS {status}] {url}  Allow={allow!r}")

    print("\n== personal cyclingmanager endpoints (unauth GET; want 401/403) ==")
    for url in PERSONAL:
        status, body, _ = _req(url, "GET")
        snip = body.strip().replace("\n", " ")[:90]
        print(f"[GET {status}] {url}  {snip}")


if __name__ == "__main__":
    main()
