# Scorito personal-team auth (token paste)

To pull **your real drafted-20 squad** (and per-stage enrolments) we need a
Scorito bearer token. The OIDC **ROPC / password grant is dead** for this
account: `quintenkoe@hotmail.com` is **federated** ("Sign in with Microsoft"),
so the identity server returns `invalid_grant / invalid_username_or_password`
even with correct credentials. Do **not** retry ROPC — repeated attempts risk a
lockout. The only working path is to **paste a browser token**.

Tokens live ~**1 hour**, so run the fetch commands **immediately** after pasting.

---

## Step 1 — copy a token from the browser

Log into <https://www.scorito.com> in a normal browser, then open DevTools (F12).
Use **either** form below — **B (the OIDC blob) is preferred** because it carries
the expiry timestamp, so the tool can tell you how long the token is still valid.

### Form B (preferred) — the whole OIDC blob

1. DevTools → **Application** → **Local Storage** → `https://www.scorito.com`
2. Find the key:
   ```
   oidc.user:https://idsrv.scorito.com:Scorito.Website.Client
   ```
3. Copy its **entire JSON value** (starts with `{"id_token":...`).

### Form A (fallback) — a bare access_token JWT

1. DevTools → **Network** tab, reload, click any request to
   `cycling.scorito.com` (e.g. `shortlist/309`).
2. In **Request Headers** find `Authorization: Bearer <JWT>`.
3. Copy just the `<JWT>` (three dot-separated parts, starts `eyJ...`).

---

## Step 2 — paste it (the tool writes `.env` for you)

Pass whatever you copied (blob **or** bare JWT) as a single argument. Wrap it in
**single quotes** in PowerShell so the JSON / dots survive:

```powershell
cd C:\Users\ac241\scorito-agent
.\.venv\Scripts\python.exe scripts\paste_token.py '<PASTE THE TOKEN OR BLOB HERE>'
```

`paste_token.py`:
- auto-detects blob vs bare JWT,
- decodes/validates it (3-part JWT; blob → extracts `access_token` + `expires_at`),
- writes the correct key to `.env`
  (`SCORITO_OIDC_USER=` for a blob, `SCORITO_ACCESS_TOKEN=` for a bare JWT,
  clearing the other), and
- prints `auth.token_status()` so you can confirm `have_token: True`
  (and, for a blob, `seconds_left`).

Expected tail on success:
```
have_token: True
expired: False
seconds_left: 35xx        # (blob only; bare JWT shows None — that's fine)
```

---

## Step 3 — fetch your team and get the improvement diff

Run these back-to-back (TdF 2026 = market **309**, slug **tdf2026**):

```powershell
.\.venv\Scripts\python.exe scripts\fetch_my_team.py 309 tdf2026
.\.venv\Scripts\python.exe scripts\compare_my_team.py tdf2026
```

- `fetch_my_team.py` saves `data/scorito/tdf2026/personal/{shortlist,teamselection,stageselection}.json`.
- `compare_my_team.py` buckets your drafted riders **KEPT / MISSING / REDUNDANT**
  against the enrolled-aware **7675-point blueprint**
  (`data/scorito/tdf2026/stage_study.json → enrolled_aware.squad_ids`) and writes
  `data/scorito/tdf2026/compare_my_team.json` — i.e. exactly *which riders you
  should have drafted differently*.

If the token has expired by the time you fetch (401), just repeat Steps 1–2.

---

## Vuelta (when market 310 opens, ~5 days)

Same token flow, then reuse the offline engine out-of-sample (train on the Tour):

```powershell
.\.venv\Scripts\python.exe scripts\snapshot_market.py 310 vuelta2026
.\.venv\Scripts\python.exe scripts\recommend_enrolled.py vuelta2026 tdf2026
```

(Pogačar is expected ~8.5M; the budget cap comes from the live market snapshot.)
