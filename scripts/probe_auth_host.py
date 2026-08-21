"""Find the REAL Scorito auth/token host.

`www.scorito.com` is a static SPA nginx (405 to any non-GET). The real API is
`cycling.scorito.com` (proper 401s). This script:
  1. Dumps ALL headers of the 401 on a protected personal endpoint
     (WWW-Authenticate reveals the scheme + often the OIDC authority).
  2. DNS-resolves + probes candidate identity/auth hosts and paths, keeping
     only responses that look like a REAL API (JSON / proper Allow header /
     401 / 400) vs nginx-static 405 or DNS failure.

Run:
    $env:NO_PROXY="*"; .\.venv\Scripts\python.exe scripts\probe_auth_host.py
"""

from __future__ import annotations

import socket
import ssl
import urllib.error
import urllib.request

CTX = ssl.create_default_context()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ScoritoAgent/0.1"


def req(url: str, method: str) -> tuple[int, dict, str]:
    r = urllib.request.Request(url, method=method)
    r.add_header("User-Agent", UA)
    r.add_header("Accept", "application/json, text/plain, */*")
    r.add_header("Origin", "https://www.scorito.com")
    try:
        with urllib.request.urlopen(r, context=CTX, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(400).decode("utf-8", "replace")
        except Exception:
            pass
        return e.code, dict(e.headers or {}), body
    except Exception as e:  # noqa: BLE001
        return -1, {}, f"{type(e).__name__}: {e}"


def resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except Exception:
        return False


def main() -> None:
    print("== 401 header dump (auth scheme / authority) ==")
    prot = "https://cycling.scorito.com/cyclingmanager/v1.0/shortlist/309"
    s, h, _ = req(prot, "GET")
    print(f"[GET {s}] {prot}")
    for k, v in h.items():
        print(f"   {k}: {v}")

    print("\n== candidate auth hosts (DNS + probe) ==")
    hosts = [
        "cycling.scorito.com",
        "api.scorito.com",
        "identity.scorito.com",
        "auth.scorito.com",
        "login.scorito.com",
        "account.scorito.com",
        "accounts.scorito.com",
        "sso.scorito.com",
        "id.scorito.com",
        "platform.scorito.com",
    ]
    paths = [
        "/account/login",
        "/api/account/login",
        "/login",
        "/token",
        "/connect/token",
        "/oauth/token",
        "/cyclingmanager/v1.0/account/login",
        "/v1.0/account/login",
        "/.well-known/openid-configuration",
    ]
    for host in hosts:
        if not resolves(host):
            print(f"[dns-fail] {host}")
            continue
        print(f"[dns-ok]   {host}")
        for p in paths:
            url = f"https://{host}{p}"
            # OPTIONS first (cheap, no body); note the Allow header
            s, h, _ = req(url, "OPTIONS")
            allow = h.get("Allow", "")
            server = h.get("Server", "")
            ctype = h.get("Content-Type", "")
            # only surface interesting ones
            if s in (-1, 404):
                continue
            print(f"    [OPT {s}] {p}  Allow={allow!r} Server={server!r} ctype={ctype!r}")


if __name__ == "__main__":
    main()
