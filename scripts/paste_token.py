"""Paste a Scorito browser access-token into the local .env.

The Scorito hotmail account is federated via "Sign in with Microsoft", so the
ROPC (username/password) grant can never mint a token (it fails at credential
validation). The supported path is therefore: log in through the browser and
copy the short-lived access_token out of Local Storage.

This helper accepts EITHER form and writes the right key into `.env`:

  1. A bare JWT access_token           -> written as  SCORITO_ACCESS_TOKEN=<jwt>
  2. The whole `oidc.user:...` JSON    -> written as  SCORITO_OIDC_USER=<compact json>
     blob (has an "access_token" field)   (keeps expires_at so the CLI can warn
                                            you before it dies)

Usage (any of):
    # paste the token/blob as the argument
    python scripts/paste_token.py eyJhbGciOi...   (bare JWT)

    # read from a file you pasted it into
    python scripts/paste_token.py C:\\path\\token.txt

    # pipe it in (best for the big JSON blob — no shell-quoting headaches)
    Get-Content token.txt -Raw | python scripts/paste_token.py -
    python scripts/paste_token.py            (then paste, Enter, Ctrl-Z, Enter)

After it writes the token it re-parses `.env` and prints the decoded expiry so
you know how long you have (typically ~1 hour). Run the fetch immediately:

    python scripts/fetch_my_team.py 309 tdf2026
    python scripts/compare_my_team.py tdf2026

See AUTH.md for the exact DevTools copy steps.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_ENV_PATH = _REPO / ".env"

# make `import scorito_agent...` work without installing the package
sys.path.insert(0, str(_REPO / "src"))


# --------------------------------------------------------------------------- #
# input                                                                       #
# --------------------------------------------------------------------------- #
def _read_input() -> str:
    """Return the pasted token/blob text from argv, a file, or stdin."""
    args = [a for a in sys.argv[1:] if a.strip()]
    if args and args[0] != "-":
        candidate = args[0]
        # a path to a file we should read?
        try:
            p = Path(candidate)
            if p.exists() and p.is_file():
                return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        # otherwise the argument itself is the token (join in case a raw JSON
        # blob got split on spaces by the shell)
        return " ".join(args)
    # stdin
    data = sys.stdin.read()
    return data


def _clean(text: str) -> str:
    """Strip whitespace and one layer of wrapping quotes/backticks."""
    t = text.strip()
    # tolerate a trailing comma or semicolon from a sloppy copy
    t = t.rstrip(";, \t\r\n")
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"', "`"):
        t = t[1:-1].strip()
    return t


# --------------------------------------------------------------------------- #
# JWT decode                                                                   #
# --------------------------------------------------------------------------- #
def _b64url_json(segment: str) -> dict | None:
    """Base64url-decode a JWT segment into a dict, or None if not decodable."""
    pad = "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(segment + pad)
    except (binascii.Error, ValueError):
        return None
    try:
        obj = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _decode_jwt(token: str) -> dict | None:
    """Return the JWT payload claims, or None if `token` is not a JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    return _b64url_json(parts[1])


def _fmt_epoch(exp: object) -> str:
    try:
        ts = int(exp)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "unknown"
    when = dt.datetime.fromtimestamp(ts, dt.timezone.utc).astimezone()
    left = ts - dt.datetime.now(dt.timezone.utc).timestamp()
    mins = int(left // 60)
    sign = "in" if left >= 0 else "EXPIRED"
    if left >= 0:
        return f"{when:%Y-%m-%d %H:%M:%S %Z} ({sign} ~{mins} min)"
    return f"{when:%Y-%m-%d %H:%M:%S %Z} ({sign} {abs(mins)} min ago)"


# --------------------------------------------------------------------------- #
# .env writer                                                                 #
# --------------------------------------------------------------------------- #
def _set_env_key(path: Path, key: str, value: str, *, clear: tuple[str, ...] = ()) -> None:
    """Replace-or-append `key=value` in `.env`; blank out any `clear` keys.

    Preserves all other lines. `clear` keys have their value emptied (not
    removed) so a stale bare token can't shadow a freshly pasted blob.
    """
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    def _matches(line: str, k: str) -> bool:
        stripped = line.lstrip()
        if stripped.startswith("export "):
            stripped = stripped[len("export "):]
        return stripped.split("=", 1)[0].strip() == k

    seen_key = False
    out: list[str] = []
    for line in lines:
        if _matches(line, key):
            if not seen_key:
                out.append(f"{key}={value}")
                seen_key = True
            # drop any duplicate definitions of the same key
            continue
        if any(_matches(line, c) for c in clear):
            k = line.lstrip()
            if k.startswith("export "):
                k = k[len("export "):]
            out.append(f"{k.split('=', 1)[0].strip()}=")
            continue
        out.append(line)

    if not seen_key:
        out.append(f"{key}={value}")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #
def main() -> int:
    text = _clean(_read_input())
    if not text:
        print(
            "No token provided.\n"
            "  Paste a bare access_token JWT, or the whole oidc.user JSON blob.\n"
            "  python scripts/paste_token.py <token>\n"
            "  Get-Content token.txt -Raw | python scripts/paste_token.py -\n"
            "See AUTH.md for the DevTools copy steps.",
            file=sys.stderr,
        )
        return 1

    access_token: str
    key: str
    value: str
    clear: tuple[str, ...] = ()
    blob_expires_at: object = None

    if text.lstrip().startswith("{"):
        # full oidc.user JSON blob
        try:
            blob = json.loads(text)
        except json.JSONDecodeError as exc:
            print(f"Input looks like JSON but did not parse: {exc}", file=sys.stderr)
            return 1
        access_token = str(blob.get("access_token") or "").strip()
        if not access_token:
            print(
                "That JSON blob has no `access_token` field.\n"
                "Copy the value of the Local-Storage key\n"
                "  oidc.user:https://idsrv.scorito.com:Scorito.Website.Client\n"
                "which contains access_token / id_token / expires_at.",
                file=sys.stderr,
            )
            return 1
        blob_expires_at = blob.get("expires_at")
        # store the whole blob compact (single line, no spaces) so expires_at
        # survives; blank any bare token so it can't take precedence.
        compact = json.dumps(blob, separators=(",", ":"))
        key, value = "SCORITO_OIDC_USER", compact
        clear = ("SCORITO_ACCESS_TOKEN",)
    else:
        # bare token (expected: a JWT access_token)
        access_token = text
        key, value = "SCORITO_ACCESS_TOKEN", access_token

    claims = _decode_jwt(access_token)
    parts = access_token.count(".") + 1
    if claims is None:
        print(
            f"WARNING: the token is not a decodable 3-part JWT (found {parts} "
            "segment(s)). Writing it anyway — Scorito access_tokens are normally "
            "JWTs, so double-check you copied `access_token` and not `id_token` "
            "or a truncated value.",
            file=sys.stderr,
        )

    _set_env_key(_ENV_PATH, key, value, clear=clear)

    print(f"Wrote {key} to {_ENV_PATH}")
    if clear:
        print(f"  (blanked {', '.join(clear)} so it can't shadow the fresh blob)")

    # decoded claim summary
    if claims is not None:
        exp = claims.get("exp")
        scope = claims.get("scope")
        if isinstance(scope, list):
            scope = " ".join(str(s) for s in scope)
        print("\nDecoded access_token claims:")
        print(f"  iss   : {claims.get('iss')}")
        print(f"  aud   : {claims.get('aud')}")
        print(f"  sub   : {claims.get('sub')}")
        for f in ("name", "email", "preferred_username"):
            if claims.get(f):
                print(f"  {f:<6}: {claims.get(f)}")
        print(f"  scope : {scope}")
        print(f"  exp   : {_fmt_epoch(exp)}")
    elif blob_expires_at is not None:
        print(f"\nblob expires_at: {_fmt_epoch(blob_expires_at)}")

    # confirm via the real auth layer (re-parses .env from disk)
    try:
        from scorito_agent.scorito import auth

        status = auth.token_status()
        print("\nauth.token_status():")
        for k in ("have_token", "expired", "seconds_left", "expires_at", "token_preview"):
            if k in status:
                print(f"  {k:<13}: {status[k]}")
        if status.get("have_token") and not status.get("expired"):
            print(
                "\nToken live. Fetch your team NOW (it expires soon):\n"
                "  python scripts/fetch_my_team.py 309 tdf2026\n"
                "  python scripts/compare_my_team.py tdf2026"
            )
        else:
            print(
                "\nNo live token detected after write — re-check the pasted value.",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        print(f"\n(could not load auth layer to confirm: {exc})", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
