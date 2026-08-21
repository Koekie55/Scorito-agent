"""Scorito personal-team authentication (token-paste path).

The Scorito website logs in via OIDC (authority ``https://idsrv.scorito.com``,
client ``Scorito.Website.Client``). The auth-code + PKCE flow is not scriptable
head-less, and the ROPC (password) grant returns ``invalid_username_or_password``
for these credentials, so the reliable path is **token paste**:

  1. Log into https://www.scorito.com in a browser.
  2. Open DevTools -> Application -> Local Storage -> https://www.scorito.com.
  3. Copy the value of the key
        oidc.user:https://idsrv.scorito.com:Scorito.Website.Client
     (a JSON blob with ``access_token``/``refresh_token``/``expires_at``)
     into ``.env`` as  SCORITO_OIDC_USER='<paste>'
     -- OR just copy the bare ``access_token`` JWT into  SCORITO_ACCESS_TOKEN=.

This module hand-rolls a tiny ``.env`` reader (no python-dotenv dependency) and
exposes :func:`bearer_headers` for the personal cyclingmanager endpoints.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Project root = .../scorito-agent (this file is src/scorito_agent/scorito/auth.py)
_REPO = Path(__file__).resolve().parents[3]
_ENV_PATH = _REPO / ".env"

# Exact browser localStorage key holding the oidc-client `User` JSON blob.
OIDC_USER_KEY = "oidc.user:https://idsrv.scorito.com:Scorito.Website.Client"

_COPY_INSTRUCTIONS = (
    "No Scorito access token found.\n"
    "  1. Log into https://www.scorito.com in your browser.\n"
    "  2. DevTools (F12) -> Application -> Local Storage -> https://www.scorito.com\n"
    f"  3. Copy the value of the key:\n       {OIDC_USER_KEY}\n"
    "     into .env as  SCORITO_OIDC_USER='<paste the whole JSON>'\n"
    "     -- OR paste just the bare access_token JWT as  SCORITO_ACCESS_TOKEN=<jwt>\n"
    f"  .env expected at: {_ENV_PATH}"
)


def _parse_env_file(path: Path | None = None) -> dict[str, str]:
    """Minimal .env parser: ``KEY=VALUE`` per line, ``#`` comments, quote-stripping.

    Only splits on the *first* ``=`` so JSON blobs (full of ``=``? no, but full of
    ``:`` and ``,``) survive intact. Values may be wrapped in single or double
    quotes which are stripped.

    ``path`` is resolved *dynamically* against the module-level ``_ENV_PATH`` when
    not supplied, so tests (and any caller) can monkeypatch ``auth._ENV_PATH`` and
    have :func:`load_token` / :func:`token_status` honour it. (A default argument of
    ``_ENV_PATH`` would bind the value at import time and silently ignore the patch.)
    """
    if path is None:
        path = _ENV_PATH
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def _env(name: str, cache: dict[str, str]) -> str:
    """Prefer a real process env var, else the parsed .env file."""
    v = os.environ.get(name)
    if v:
        return v.strip()
    return cache.get(name, "").strip()


def _token_from_oidc_blob(blob: str) -> tuple[str, float | None]:
    """Extract ``access_token`` (+ optional ``expires_at``) from the oidc `User` JSON."""
    try:
        j = json.loads(blob)
    except Exception:
        return "", None
    if not isinstance(j, dict):
        return "", None
    tok = str(j.get("access_token") or "").strip()
    exp = j.get("expires_at")
    exp_f = float(exp) if isinstance(exp, (int, float)) else None
    return tok, exp_f


def load_token() -> tuple[str, float | None]:
    """Return ``(access_token, expires_at_epoch_or_None)`` from .env / environment.

    Order of precedence:
      1. ``SCORITO_ACCESS_TOKEN`` (bare JWT)
      2. ``access_token`` parsed out of ``SCORITO_OIDC_USER`` (the localStorage blob)
    """
    cache = _parse_env_file()

    bare = _env("SCORITO_ACCESS_TOKEN", cache)
    if bare:
        return bare, None

    blob = _env("SCORITO_OIDC_USER", cache)
    if blob:
        tok, exp = _token_from_oidc_blob(blob)
        if tok:
            return tok, exp

    return "", None


def token_status() -> dict:
    """Non-raising diagnostic used by scripts to explain what's missing."""
    tok, exp = load_token()
    now = time.time()
    status: dict = {
        "have_token": bool(tok),
        "expires_at": exp,
        "expired": (exp is not None and exp < now),
        "seconds_left": (int(exp - now) if exp else None),
        "env_path": str(_ENV_PATH),
        "instructions": _COPY_INSTRUCTIONS,
    }
    if tok:
        status["token_preview"] = tok[:12] + "..." + tok[-6:]
    return status


def bearer_headers(*, require: bool = True) -> dict[str, str]:
    """Browser-shaped headers incl. ``Authorization: Bearer <token>``.

    Raises ``RuntimeError`` with copy instructions when no token is present and
    ``require`` is True; otherwise returns the anonymous header set.
    """
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Origin": "https://www.scorito.com",
        "Referer": "https://www.scorito.com/",
    }
    tok, exp = load_token()
    if not tok:
        if require:
            raise RuntimeError(_COPY_INSTRUCTIONS)
        return headers
    if exp is not None and exp < time.time():
        # Token present but stale — still send it (server will 401), but warn.
        headers["X-Scorito-Token-Expired"] = "1"
    headers["Authorization"] = f"Bearer {tok}"
    return headers


__all__ = [
    "OIDC_USER_KEY",
    "bearer_headers",
    "load_token",
    "token_status",
]
