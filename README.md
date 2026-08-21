# Scorito Cycling Agent

A standalone system to reverse-engineer and win the **Scorito** fantasy cycling
game (grand tours + classics), by combining three data sources and a team
optimiser.

> ⚠️ This project is intentionally **separate** from any other repo. Credentials
> live only in a gitignored `.env`. Rotate the Scorito password if it was ever
> shared in plaintext.

## The three services

| # | Package | Source | Goal |
|---|---------|--------|------|
| 1 | `scorito_agent.scorito` | scorito.com public cycling API | Fetch my team, rider prices/points, apply the game rules (20 riders upfront under a budget cap, 9 enrolled per stage), and back-analyse *which rider I should have picked* per stage. |
| 2 | `scorito_agent.cyclingoracle` | cyclingoracle.com | Scrape stage predictions/stats and **replicate its prediction model** for future stages/riders. |
| 3 | `scorito_agent.pcs` | procyclingstats.com | Deep-scrape all races/stages, hold stage specifics in memory, and **predict a similar future stage** by profile + participating riders + announced tactics. |

**End goal:** learn the game, predict stages, and build a winning squad for the
upcoming **Vuelta**.

## Scorito rules (as applied)

* Pick a **shortlist of 20 riders** up front, total price must fit the **budget cap**.
* Each stage, **enrol 9** of those 20, and pick a **captain** (bonus points).
* Points come from stage results, classifications and jerseys.

## Layout

```
src/scorito_agent/
  common/         shared HTTP + cache + UTF-8 stdout helpers
  scorito/        part 1 (API client, rules, optimiser)
  cyclingoracle/  part 2 (scraper + model)
  pcs/            part 3 (scraper + stage-similarity model)
tests/            mirrors the package tree
data/             on-disk scrape cache (gitignored)
```

## Setup

```powershell
cd C:\Users\ac241\scorito-agent
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env   # then fill in real credentials
```

## Data-source notes

* **scorito.com** is a React SPA. The cycling API host is a runtime value
  (`cyclingApi`) read from `https://www.scorito.com/config.json`; the known-good
  base host is `https://cycling.scorito.com`. Public endpoints expose market
  prices/points; personal team/shortlist/stage-selection require login.
* **procyclingstats.com** returns HTTP 522 (Cloudflare) from datacentre IPs.
  The PCS scraper caches aggressively and is meant to be run from a **home /
  residential network**; tests run against saved HTML fixtures.

## Vuelta stage top 20

Refresh the live PCS startlist and regenerate all 21 stage predictions:

```powershell
.\.venv\Scripts\python.exe scripts\refresh_vuelta_stage_predictions.py
```

The command writes `data\scorito\vuelta2026\stage_top20_predictions.json` and
`stage_top20_predictions.csv`. It rebuilds the full PCS evidence projection only
when provisional participants change. The scheduled rider-news job runs the same
refresh after every successful news collection.

## WielerFlits forum opinion

Compile the curated PDF claims before refreshing predictions:

```powershell
.\.venv\Scripts\python.exe scripts\compile_wielerflits_forum.py data\forum_opinion\vuelta2026_wielerflits_claims.json
```

The forum digest contributes 30% of the combined opinion signal; evidence-aware
expert chat contributes 70%. The blend is applied once inside the existing 16%
consumer cap. Team-role, form, health, tactics, and stage-intent claims may move
close stage rankings. Relative-price/value opinions remain audit-only because
price cannot create race points. Unsupported guesses and humour receive zero
weight; repetition, likes, and consensus do not increase a claim.

## Running tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

