"""Extract the live Scorito OIDC session token from Edge's on-disk Local Storage.

We cannot read Edge's *saved passwords*, but the Scorito website (an
oidc-client-ts SPA) persists its live session — including the Bearer
``access_token`` — into the browser's **Local Storage**, which Chromium/Edge
flushes to a small LevelDB under each profile:

    %LOCALAPPDATA%\\Microsoft\\Edge\\User Data\\<profile>\\Local Storage\\leveldb\\

The value we want lives under the Local-Storage key

    oidc.user:https://idsrv.scorito.com:Scorito.Website.Client

and is the oidc-client-ts ``User`` JSON:

    {"id_token":"...","access_token":"eyJ...","refresh_token":"...",
     "token_type":"Bearer","scope":"...","profile":{...},"expires_at":<unix>}

This script scans every ``*.ldb`` / ``*.log`` (SSTable + write-ahead log) in
all Edge profiles, pulls out any balanced-brace JSON object that contains an
``access_token`` (both the Latin-1 / one-byte and the UTF-16LE Local-Storage
encodings are handled), and picks the freshest non-expired one. It then writes
that blob into ``.env`` (as ``SCORITO_OIDC_USER``, exactly like
``paste_token.py``) and confirms via the real auth layer.

Files may be locked by a running Edge; we open them with a shared read handle
(``CreateFileW`` + ``FILE_SHARE_READ|WRITE|DELETE``) so a live browser doesn't
block us.

Run:
    python scripts/extract_edge_token.py
then, if it found a live token:
    python scripts/fetch_my_team.py 309 tdf2026
    python scripts/compare_my_team.py tdf2026
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

MARKER = "oidc.user:https://idsrv.scorito.com:Scorito.Website.Client"
NEEDLE = '"access_token"'


# --------------------------------------------------------------------------- #
# shared-read file access (Edge may hold the leveldb files open)              #
# --------------------------------------------------------------------------- #
def _read_shared(path: Path) -> bytes:
    """Read a possibly-locked file using a Windows shared read handle."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except PermissionError:
        pass
    except OSError:
        pass

    try:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x1
        FILE_SHARE_WRITE = 0x2
        FILE_SHARE_DELETE = 0x4
        OPEN_EXISTING = 3
        INVALID = ctypes.c_void_p(-1).value

        CreateFileW = ctypes.windll.kernel32.CreateFileW
        CreateFileW.restype = wintypes.HANDLE
        CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        handle = CreateFileW(
            str(path), GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None, OPEN_EXISTING, 0, None,
        )
        if not handle or handle == INVALID:
            return b""
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        with os.fdopen(fd, "rb") as fh:
            return fh.read()
    except Exception:
        return b""


# --------------------------------------------------------------------------- #
# balanced-brace JSON extraction around a needle                              #
# --------------------------------------------------------------------------- #
def _balanced_from(text: str, needle_pos: int) -> str | None:
    """Return the JSON object surrounding `needle_pos`, or None.

    Walks back to the nearest unmatched '{' then forward, counting braces while
    respecting JSON string quoting/escapes so braces inside strings don't
    unbalance the scan.
    """
    start = text.rfind("{", 0, needle_pos)
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, min(len(text), start + 200_000)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _candidates_from_text(text: str) -> list[dict]:
    """All parseable JSON objects containing access_token found in `text`."""
    out: list[dict] = []
    seen: set[str] = set()
    pos = 0
    while True:
        idx = text.find(NEEDLE, pos)
        if idx == -1:
            break
        pos = idx + len(NEEDLE)
        blob = _balanced_from(text, idx)
        if not blob or blob in seen:
            continue
        seen.add(blob)
        try:
            obj = json.loads(blob)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("access_token"):
            out.append(obj)
    return out


def _candidates_from_bytes(data: bytes) -> list[dict]:
    """Extract candidate blobs from a raw leveldb file (both encodings)."""
    found: list[dict] = []

    # 1-byte encoding (Chromium 0x01 prefix / ASCII values) -> latin-1 view
    try:
        found.extend(_candidates_from_text(data.decode("latin-1")))
    except Exception:
        pass

    # UTF-16LE encoding (Chromium 0x00 prefix). Search the raw UTF-16LE needle,
    # then decode a bounded, needle-aligned window so we don't misalign.
    needle16 = NEEDLE.encode("utf-16-le")
    pos = 0
    while True:
        idx = data.find(needle16, pos)
        if idx == -1:
            break
        pos = idx + len(needle16)
        lo = max(0, idx - 8192)
        hi = min(len(data), idx + 200_000)
        # re-align window start to the needle's byte parity
        if (idx - lo) % 2:
            lo += 1
        try:
            window = data[lo:hi].decode("utf-16-le", errors="replace")
        except Exception:
            continue
        # locate the needle inside the decoded window and extract
        for blob in _candidates_from_text(window):
            found.append(blob)
    return found


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #
def _leveldb_dirs() -> list[Path]:
    base = Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Edge" / "User Data"
    dirs: list[Path] = []
    if not base.exists():
        return dirs
    for prof in base.iterdir():
        if not prof.is_dir():
            continue
        ldb = prof / "Local Storage" / "leveldb"
        if ldb.is_dir():
            dirs.append(ldb)
    return dirs


def _preview(tok: str) -> str:
    return f"{tok[:14]}...{tok[-6:]} ({len(tok)} chars)" if tok else "(none)"


def main() -> int:
    now = time.time()
    dirs = _leveldb_dirs()
    if not dirs:
        print("No Edge Local Storage leveldb dirs found.", file=sys.stderr)
        return 1

    print(f"Scanning {len(dirs)} Edge profile(s) for a Scorito OIDC token...")
    all_cands: list[tuple[dict, Path]] = []
    marker_hits = 0
    files_scanned = 0

    for ldb in dirs:
        for path in sorted(ldb.iterdir()):
            if path.suffix.lower() not in (".ldb", ".log"):
                continue
            data = _read_shared(path)
            if not data:
                continue
            files_scanned += 1
            if MARKER.encode("latin-1") in data or MARKER.encode("utf-16-le") in data:
                marker_hits += 1
            for obj in _candidates_from_bytes(data):
                all_cands.append((obj, path))

    print(f"  files scanned : {files_scanned}")
    print(f"  marker hits   : {marker_hits}  (key '{MARKER[:24]}...')")
    print(f"  raw candidates: {len(all_cands)}")

    # de-dup by access_token, keep the record with the max expires_at
    best_by_tok: dict[str, tuple[dict, Path]] = {}
    for obj, path in all_cands:
        tok = str(obj.get("access_token"))
        exp = obj.get("expires_at") or 0
        cur = best_by_tok.get(tok)
        if cur is None or (obj.get("expires_at") or 0) > (cur[0].get("expires_at") or 0):
            best_by_tok[tok] = (obj, path)

    if not best_by_tok:
        print(
            "\nNo Scorito OIDC token found on disk.\n"
            "The session may only live in an unflushed memtable, or you're\n"
            "signed in on a different browser. Fallback: open the token in the\n"
            "browser DevTools and paste it — see AUTH.md, then:\n"
            "  python scripts/paste_token.py -",
            file=sys.stderr,
        )
        return 2

    ranked = sorted(
        best_by_tok.values(),
        key=lambda t: (t[0].get("expires_at") or 0),
        reverse=True,
    )
    print(f"\nDistinct tokens found: {len(ranked)}")
    for obj, path in ranked:
        exp = obj.get("expires_at")
        left = (exp - now) if isinstance(exp, (int, float)) else None
        state = "LIVE" if (left and left > 0) else "expired"
        left_s = f"{int(left)}s left" if left is not None else "no expiry"
        prof = path.parent.parent.parent.name
        print(f"  - {state:7} {left_s:>12}  from [{prof}]  {_preview(str(obj.get('access_token')))}")

    best_obj, best_path = ranked[0]
    exp = best_obj.get("expires_at")
    left = (exp - now) if isinstance(exp, (int, float)) else None

    if left is not None and left <= 0:
        print(
            "\nThe freshest token on disk is EXPIRED. Refresh it: open "
            "scorito.com in Edge, ensure you're logged in, reload once, then "
            "re-run this script.",
            file=sys.stderr,
        )
        # still write it — auth layer will report expired, but no harm
    # write it via paste_token's exact .env contract
    import paste_token as pt  # type: ignore

    compact = json.dumps(best_obj, separators=(",", ":"))
    pt._set_env_key(pt._ENV_PATH, "SCORITO_OIDC_USER", compact, clear=("SCORITO_ACCESS_TOKEN",))
    print(f"\nWrote SCORITO_OIDC_USER to {pt._ENV_PATH}")
    print("  (blanked SCORITO_ACCESS_TOKEN so it can't shadow the fresh blob)")

    claims = pt._decode_jwt(str(best_obj.get("access_token")))
    if claims is not None:
        print("\nDecoded access_token claims:")
        for f in ("iss", "aud", "sub", "name", "email", "preferred_username"):
            if claims.get(f):
                print(f"  {f:<18}: {claims.get(f)}")
        print(f"  exp               : {pt._fmt_epoch(claims.get('exp'))}")

    try:
        from scorito_agent.scorito import auth

        status = auth.token_status()
        print("\nauth.token_status():")
        for k in ("have_token", "expired", "seconds_left", "expires_at"):
            if k in status:
                print(f"  {k:<13}: {status[k]}")
        if status.get("have_token") and not status.get("expired"):
            print(
                "\nToken LIVE. Fetch your team NOW (it expires soon):\n"
                "  python scripts/fetch_my_team.py 309 tdf2026\n"
                "  python scripts/compare_my_team.py tdf2026"
            )
            return 0
        print("\nToken written but not live (expired?). See message above.", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"\n(could not load auth layer to confirm: {exc})", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
