# Vuelta 2026 public verification snapshot

This directory is a credential-free export of the model state used on
2026-08-23T19:24:12.133780+00:00. All values are forward projections, not race results.

## Files

- `top20_per_stage.json` and `.csv`: all 20 predicted finishers for all 21 stages.
- `objective_optimal_teams.json`: complete base and conditional 20-rider squads,
  their 21 nine-rider lineups, captains, live prices, and score components.
  The objective solver uses the populated Scorito market snapshot; the full
  projection separately labels its internal price field as synthetic fallback.
- `riders.json`: raw model inputs and capabilities for all 184 riders.
- `rider_analytics.csv`: one query-friendly aggregate row per rider.
- `stages.json` and `stage_analytics.csv`: complete route/profile metadata.
- `stage_rider_analytics.csv`: all 3864 rider-stage scores and evidence rows.
- `projection_full.json`: the unabridged model snapshot behind those tables.
- `market_*_raw.json`: raw public Scorito market rider, quality, and stage inputs.
- `manifest.json`: source freshness, record counts, sizes, and SHA-256 checksums.

The objective optimizer output excludes the local `personal_base` and
`personal_conditional` comparison objects. Authenticated team selections,
credentials, tokens, and cookies are never exported.

Regenerate from the repository root with:

```powershell
.\.venv\Scripts\python.exe scripts\export_public_vuelta_data.py
```
