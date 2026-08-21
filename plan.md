# Build plan — Scorito Cycling Agent

## Phase 0 — Scaffold (DONE)
- [x] Standalone project tree at `C:\Users\ac241\scorito-agent\`
- [x] `.gitignore`, `.env.example`, `requirements.txt`
- [x] `common/http.py` (retry + on-disk cache + UTF-8 stdout)
- [x] Package `__init__.py` stubs (common, scorito, cyclingoracle, pcs)
- [x] `conftest.py`, `README.md`, `plan.md`

## Phase 1 — Scorito API client (part 1) — DONE
- [x] Fetch `https://www.scorito.com/config.json` → resolve `cyclingApi` host
- [x] Sweep `cyclingmanager/v1.0/eventriderenriched/{id}` to find the current
      **TdF 2026** and **Vuelta** event IDs (cross-check a known rider's age)
- [x] Model rider records (price, qualities, type, status) as typed rows
- [x] Probe the POINTS surface: `marketpoints`, `points/totalpoints`,
      `points/market`, `stageresult/rider/{id}`, `classification`
- [x] Login flow (or documented manual-paste fallback) for the personal
      shortlist / stage-selection / captain endpoints

## Phase 2 — Optimiser (part 1)
- [x] MILP (scipy HiGHS): choose 20-rider shortlist maximising expected points ≤ budget
- [x] Per-stage: pick best legal 9 + captain from the shortlist
- [x] Back-analysis: given realised stage points, report the optimal picks I
      *should* have made, and my efficiency vs. that optimum

## Phase 3 — CyclingOracle model (part 2) — delegated to Agent A
- [x] Live-scrape cyclingoracle.com stage predictions/stats
- [x] Normalise into tables under `data/cyclingoracle/`
- [x] Replicate and validate its prediction model

## Phase 4 — ProCyclingStats model (part 3) — delegated to Agent B
- [x] Deep scraper (races → stages → results/startlists/profiles), cached
- [x] Stage feature model (profile, distance, elevation, participants, tactics)
- [x] Stage-similarity predictor for a future stage

## Phase 5 — Ensemble + Vuelta squad
- [x] Combine Scorito point curves + CyclingOracle + PCS into expected-points estimates
- [x] Build a projected **Vuelta** squad from 70 PCS starters and all 21 stages
      while live market 310 remains empty
- [x] Validate by back-testing on completed TdF 2026 stages
- [x] Replace projected Vuelta prices with live market 310 prices once it opens
- [x] Anchor the active rider plan to the signed-in user's completed 20-rider team
- [x] Blend compatible stage-analysis notes and attach the completed rider-news digest
- [x] Keep uncertain teammate/jersey upside separate from lineup scoring and compare
      the personal team against unconstrained and forced-four-UAE scenarios

## Ownership while parallelised
- **Me:** `scorito/`, shared root files (`common/`, requirements, README, plan).
- **Agent A:** owns `cyclingoracle/`, `tests/cyclingoracle/`, `data/cyclingoracle/`.
- **Agent B:** owns `pcs/`, `tests/pcs/`, `data/pcs/`.
- Agents reuse `common/http.py` and must **not** edit shared root files.
