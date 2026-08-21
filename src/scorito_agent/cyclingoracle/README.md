# CyclingOracle scraper/model

Standalone package for scraping CyclingOracle/WielerOrakel prediction data and
ranking riders for future cycling stages.

## URL / endpoint map

- Race index: `https://www.cyclingoracle.com/nl/koersen`
- Blog/prediction index: `https://www.cyclingoracle.com/nl/blog`
- Race page example: `https://www.cyclingoracle.com/nl/koersen/tour-de-france-2`
- Stage prediction example:
  `https://www.cyclingoracle.com/nl/blog/tour-de-france-2026-voorspelling-etappe-21`
- Datalists example:
  `https://www.cyclingoracle.com/nl/blog/tour-de-france-2026-datalijsten`
- Rider pages:
  `https://www.cyclingoracle.com/nl/renners/<slug>-<id>`
- Public GraphQL hydration endpoint: `https://api.cyclingoracle.com/v1`

Stage pages include a `data-prediction-config` JSON attribute on the
`js-prediction-table`. It contains `apiUrl`, `apiKey`, `isTTT`, and compact
prediction rows. The browser posts those rows to the GraphQL
`riderPredictions` query to hydrate rider names, slugs, teams, and win
percentages.

## Normalized schema

`stage_predictions()` returns one row per predicted rider:

- `source_url`, `stage_slug`, `race_name`, `stage_title`, `stage_number`
- `stage_profile`
- `predicted_rank`
- `rider_id`, `rider_name`, `rider_slug`
- `team_id`, `team_name`, `team_slug`
- `win_probability_pct`, `win_probability`
- `raw`

`parse_rider_stats()` returns a rider record with `stats` / `skill_*` fields:
`ovr`, `cob`, `hll`, `mtn`, `gc`, `itt`, `spr`, plus bullet-derived features
such as `flat`, `leadout`, `one_day`, `prologue`, `short_itt`, and `long_itt`.

`parse_data_lists()` returns long-form feature rows:
`section`, `metric`, `rider_name`, `rider_id`, `rider_url`, `value`, `unit`.

## Model methodology

CyclingOracle does not publish its private model. `model.py` implements a
transparent approximation using the public rider-card/data-list features and
route profile text. Stage text is classified into profiles such as
`flat_sprint`, `hilly_sprint`, `mountain`, `time_trial`, and `cobble`.
Each profile applies documented linear weights, then converts scores to
probabilities with a softmax. The weights were inferred from CyclingOracle's
methodology text and cached live predictions.

## Usage

```python
from scorito_agent.cyclingoracle.scraper import rider_stats, stage_predictions
from scorito_agent.cyclingoracle.model import rank_riders

url = "https://www.cyclingoracle.com/nl/blog/tour-de-france-2026-voorspelling-etappe-21"
site_rows = stage_predictions(url)
riders = [rider_stats(row["rider_slug"] or row["rider_id"]) for row in site_rows]
ranked = rank_riders(riders, stage=site_rows[0])
```

Run deterministic tests:

```powershell
C:\Users\ac241\scorito-agent\.venv\Scripts\python.exe -m pytest tests\cyclingoracle -q
```

