"""Discover the /api/account/login request shape WITHOUT spending a real
credential attempt: POST an empty body and inspect the validation error,
which normally names the expected fields.

Run:
    $env:NO_PROXY="*"; .\.venv\Scripts\python.exe scripts\probe_login_shape.py
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

CTX = ssl.create_default_context()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ScoritoAgent/0.1"
URL = "https://www.scorito.com/api/account/login"


def post(url: str, body: bytes, ctype: str) -> tuple[int, str, dict]:
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("Content-Type", ctype)
    req.add_header("Origin", "https://www.scorito.com")
    req.add_header("Referer", "https://www.scorito.com/inloggen")
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=20) as resp:
            return resp.status, resp.read(1200).decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read(1200).decode("utf-8", "replace")
        except Exception:
            pass
        return e.code, body_txt, dict(e.headers or {})
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}", {}


def main() -> None:
    print("== empty JSON body ==")
    s, b, h = post(URL, b"{}", "application/json")
    print(f"[{s}] ctype={h.get('Content-Type')}")
    print(b[:1000])

    print("\n== empty form body ==")
    s, b, h = post(URL, b"", "application/x-www-form-urlencoded")
    print(f"[{s}] ctype={h.get('Content-Type')}")
    print(b[:600])

    # Try a bogus-field JSON to see if it echoes model-binding errors
    print("\n== junk JSON body ==")
    s, b, h = post(URL, json.dumps({"foo": "bar"}).encode(), "application/json")
    print(f"[{s}]")
    print(b[:1000])


if __name__ == "__main__":
    main()
