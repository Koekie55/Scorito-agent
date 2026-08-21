"""Discover Scorito's auth/token endpoint (GET-only, no credential POST).

Corporate TLS interception on this machine breaks requests+certifi for
scorito hosts, so we use urllib + ssl.create_default_context() (Windows system
cert store), exactly like scripts/probe_connectivity.py.

Run:
    $env:NO_PROXY="*"; .\.venv\Scripts\python.exe scripts\probe_auth.py
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.request

CTX = ssl.create_default_context()

# Candidate identity hosts + discovery paths.
DISCOVERY_URLS = [
    "https://platform.scorito.com/.well-known/openid-configuration",
    "https://auth.scorito.com/.well-known/openid-configuration",
    "https://identity.scorito.com/.well-known/openid-configuration",
    "https://login.scorito.com/.well-known/openid-configuration",
    "https://www.scorito.com/.well-known/openid-configuration",
    "https://cycling.scorito.com/.well-known/openid-configuration",
    "https://account.scorito.com/.well-known/openid-configuration",
]

# Candidate custom-login API shapes (probe existence with OPTIONS/GET only).
PROBE_PATHS = [
    "https://platform.scorito.com/account/v1.0/login",
    "https://platform.scorito.com/account/v2.0/login",
    "https://platform.scorito.com/user/v1.0/login",
    "https://platform.scorito.com/authentication/v1.0/login",
    "https://platform.scorito.com/auth/v1.0/login",
    "https://cycling.scorito.com/account/v1.0/login",
    "https://www.scorito.com/api/account/login",
]


def _req(url: str, method: str = "GET") -> tuple[int, str, dict]:
    req = urllib.request.Request(url, method=method)
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ScoritoAgent/0.1",
    )
    req.add_header("Accept", "application/json, text/plain, */*")
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=15) as resp:
            body = resp.read(4000).decode("utf-8", "replace")
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(2000).decode("utf-8", "replace")
        except Exception:
            pass
        return e.code, body, dict(e.headers or {})
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}", {}


def main() -> None:
    print("== OIDC discovery ==")
    for url in DISCOVERY_URLS:
        status, body, _ = _req(url)
        line = f"[{status}] {url}"
        if status == 200 and body.lstrip().startswith("{"):
            try:
                doc = json.loads(body)
                print(line + "  <-- FOUND")
                for k in (
                    "issuer",
                    "token_endpoint",
                    "authorization_endpoint",
                    "userinfo_endpoint",
                ):
                    if k in doc:
                        print(f"      {k}: {doc[k]}")
                gts = doc.get("grant_types_supported")
                if gts:
                    print(f"      grant_types_supported: {gts}")
            except Exception:
                print(line + "  (200 but not JSON)")
        else:
            print(line)

    print("\n== Candidate login endpoints (existence probe, no creds) ==")
    for url in PROBE_PATHS:
        status, body, hdrs = _req(url, method="OPTIONS")
        allow = hdrs.get("Allow") or hdrs.get("allow") or ""
        snippet = body.strip().replace("\n", " ")[:120]
        print(f"[OPTIONS {status}] {url}  Allow={allow!r}  {snippet}")


if __name__ == "__main__":
    main()
