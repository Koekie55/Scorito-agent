# PCS notes

- Live PCS scraping was designed for cache/retry but must be run from a non-datacentre IP if Cloudflare returns 522.
- No new dependencies were added because root `requirements.txt` is owned by another agent.
- Optional future dependency request: `beautifulsoup4` for richer DOM tolerance on changing PCS pages.
- Optional structural reference: PyPI package `procyclingstats` (do not add automatically; useful to compare rider/race/stage field coverage).
- Dev/test dependency observed: `pytest` was missing from the new venv; installed locally in `.venv` to run `tests\pcs`.
- Live probe on 2026-08-01: `curl` to a PCS rider page returned Cloudflare-served HTTP 500, so no live HTML was usable here.
- Runtime dependency observed: live `fetcher.fetch_*` calls need the shared HTTP helper dependency `requests`; this venv currently lacks it, so URL builders/parsers/predictor remain importable and fetch calls raise a clear setup error until project requirements are installed.
