# ProCyclingStats (PCS) service

This package is Agent B's standalone PCS scraper/memory/predictor for the Scorito cycling agent.

## Network caveat

`procyclingstats.com` is Cloudflare-protected and returned Cloudflare errors (documented 522 here; live probe returned 500) from this datacentre environment during development. The code is cache-first and retrying, but deep scraping should be run by the user from a normal residential/home network. Cached pages are stored by the shared HTTP helper under `data/pcs/`.

## Reference URL templates

The PHP reference `jvdlaar/scorito` (`ProCyclingStatsFetcher.php`) hits:

- Rider page: `https://www.procyclingstats.com/rider/{rider_slug}`
- Autocomplete fallback: `https://www.procyclingstats.com/resources/search.php?searchfrom=&term={query}`

The PCS fetcher here also supports the stage/race URL shapes used by PCS pages:

- Race page: `https://www.procyclingstats.com/race/{race_slug}/{year}`
- Race startlist: `https://www.procyclingstats.com/race/{race_slug}/{year}/startlist`
- Stage page: `https://www.procyclingstats.com/race/{race_slug}/{year}/stage-{stage_no}`
- Stage result: `https://www.procyclingstats.com/race/{race_slug}/{year}/stage-{stage_no}/result`
- Stage startlist: `https://www.procyclingstats.com/race/{race_slug}/{year}/stage-{stage_no}/startlist`

## Rider slug exceptions

`slugs.py` reproduces the PHP `formatRiderName` exceptions: `negasi-haylu-abreha -> negasi-abreha`, `mikkel-frolich-honore -> mikkel-honore`, `daniel-martin -> dan-martin`, `omer-goldshtein -> omer-goldstein`, `chris-froome -> christopher-froome`, `alexey-lutsenko -> aleksey-lutsenko`, `soren-kragh -> soren-kragh-andersen`, `fred-wright -> alfred-wright`, `magnus-cort -> magnus-cort-nielsen`, `ivan-garcia -> ivan-garcia-cortina`, `georg-zimmerman -> georg-zimmermann`, `brandon-rivera -> brandon-smith-rivera-vargas`, `einer-rubio -> einer-augusto-rubio-reyes`, `diego-camargo -> diego-andres-camargo`.

## Data schema

Stage dicts contain: `id`, `race`, `race_slug`, `year`, `stage_no`, `date`, `profile_type` (`flat|hilly|mountain|itt|ttt|unknown`), `distance_km`, `vertical_meters`, `finish_type` (`sprint|flat|uphill|summit|technical|tt|unknown`), `startlist` (`rider`, `rider_slug`, `team`), and `results` (`rank`, `rider`, `rider_slug`, `team`, `time`).

Rider dicts contain: `name`, `rider_slug`, `team`, `specialties`, and `recent_results`.

`store.StageStore` persists parsed stages as JSON in `data/pcs/stages.json` by default.

## Similarity model

`predict.py` encodes each stage as profile one-hot, finish one-hot, scaled distance, scaled elevation, and startlist size. It ranks past stages by Euclidean similarity (`1 / (1 + distance)`), aggregates weighted finishing positions from the K nearest stages, filters to announced participants, and lightly boosts riders/teams named in protected/aggressive/leadout tactics.

## Usage

```python
from scorito_agent.common.http import fix_stdout
from scorito_agent.pcs.fetcher import fetch_stage_page
from scorito_agent.pcs.parse import parse_stage_page
from scorito_agent.pcs.store import StageStore
from scorito_agent.pcs.predict import predict_finishers

fix_stdout()
html = fetch_stage_page("vuelta-a-espana", 2025, 1)
stage = parse_stage_page(html)
store = StageStore()
store.upsert_stage(stage)
result = predict_finishers(target_stage, store, k=10, top_n=20)
```

Run tests:

```powershell
C:\Users\ac241\scorito-agent\.venv\Scripts\python.exe -m pytest tests\pcs -q
```

