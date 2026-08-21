# CyclingOracle notes

- No new package dependency is required by this package. The scraper uses only
  the Python standard library plus the shared `scorito_agent.common.http` helper.
- Live `GET` scraping depends on the existing shared helper dependencies
  (`requests` in the project environment). Parser/model tests use saved fixtures
  and do not require network access.
- If `requests` cannot verify the local corporate CA chain, `scraper._http_get`
  falls back to `curl.exe`/`curl` and writes the same shared cache namespace.
- Validation environment had `requests 2.34.2` and `pytest 9.1.1`; the
  cyclingoracle fixture tests passed.
- `ruff` was attempted but its executable was blocked by Windows group policy
  (`WinError 1260`), so lint validation could not complete in this environment.
