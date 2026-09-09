"""Build the stage 10 top-20 and model-evaluation email bodies as HTML.

Delivery runs through Outlook, matching scripts/run_vuelta_daily.ps1; this module
only renders the bodies so they can be reviewed before anything is sent.
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

DATA = ROOT / "data" / "scorito" / "vuelta2026"
RECOMMENDATION = json.loads((DATA / "daily_stage_recommendation.json").read_text(encoding="utf-8"))
PREDICTIONS = json.loads((DATA / "stage_top20_predictions.json").read_text(encoding="utf-8-sig"))

OUT_DIR = DATA / "email"
STAGE_HTML = OUT_DIR / "stage_10_top20.html"
EVAL_HTML = OUT_DIR / "model_evaluation.html"

CSS_TABLE = (
    "border-collapse:collapse;font-family:Segoe UI,Arial,sans-serif;font-size:13px;"
    "border:1px solid #d8dee4;width:100%;max-width:820px"
)
CSS_HEAD = "background:#201751;color:#ffffff;text-align:left;padding:7px 9px"
CSS_CELL = "padding:6px 9px;border-top:1px solid #e6eaee"


def esc(value: object) -> str:
    return html.escape(str(value))


def stage_10() -> dict:
    return next(s for s in PREDICTIONS["stages"] if int(s["stage_no"]) == 10)


def team_blocks() -> list[dict]:
    teams = RECOMMENDATION.get("teams")
    return list(teams) if isinstance(teams, list) else list(teams.values())


def build_stage_email() -> tuple[str, str]:
    stage = stage_10()
    target = RECOMMENDATION["target_stage"]
    rows = []
    for rank, row in enumerate(stage["top_20"], 1):
        stars = float(row.get("tv2_axelgaard_stars") or 0.0)
        band = row.get("expected_finish_band") or []
        rows.append(
            f"<tr>"
            f"<td style='{CSS_CELL};text-align:right'><b>{rank}</b></td>"
            f"<td style='{CSS_CELL}'>{esc(row['rider'])}</td>"
            f"<td style='{CSS_CELL};color:#54606d'>{esc(row.get('team'))}</td>"
            f"<td style='{CSS_CELL};text-align:right'>{esc(row.get('scorito_stage_points'))}</td>"
            f"<td style='{CSS_CELL};text-align:center'>{'*' * int(stars) if stars else '-'}</td>"
            f"<td style='{CSS_CELL};text-align:center;color:#54606d'>"
            f"{esc(f'{band[0]}-{band[1]}') if len(band) == 2 else '-'}</td>"
            f"<td style='{CSS_CELL};text-align:right;color:#54606d'>{esc(row.get('confidence'))}</td>"
            f"</tr>"
        )

    lineups = []
    for team in team_blocks():
        captain = team.get("captain") or "-"
        nine = [str(name) for name in team.get("lineup", [])]
        marked = [f"<b>{esc(n)}</b> (C)" if n == captain else esc(n) for n in nine]
        excluded = team.get("main_excluded_alternative") or {}
        excluded_name = excluded.get("rider") if isinstance(excluded, dict) else excluded
        lineups.append(
            f"<h4 style='font-family:Segoe UI,Arial,sans-serif;margin:16px 0 4px;color:#00454d'>"
            f"{esc(team.get('team'))}</h4>"
            f"<p style='font-family:Segoe UI,Arial,sans-serif;font-size:13px;margin:0 0 4px'>"
            f"{', '.join(marked)}</p>"
            f"<p style='font-family:Segoe UI,Arial,sans-serif;font-size:12px;color:#54606d;margin:0'>"
            f"Projected {esc(team.get('projected_stage_points'))} points "
            f"({esc(team.get('projected_individual_points'))} individual + "
            f"{esc(team.get('expected_team_points'))} expected team). "
            f"Legality {'PASS' if team.get('legal_current_market') else 'FAIL'}; "
            f"budget left {esc(team.get('budget_remaining'))}; "
            f"team cap {esc(team.get('max_trade_team_count'))}/4. "
            f"Main alternative left out: {esc(excluded_name or 'none')}.</p>"
        )

    gaps = RECOMMENDATION.get("data_gaps") or []
    gap_items = "".join(f"<li>{esc(g)}</li>" for g in gaps) or "<li>None recorded.</li>"
    sources = RECOMMENDATION.get("sources", {})

    body = f"""<!doctype html><html><body style="background:#f6f8fa;padding:16px">
<div style="max-width:860px;margin:0 auto;background:#ffffff;padding:22px;border:1px solid #e1e4e8">
<h2 style="font-family:Segoe UI,Arial,sans-serif;color:#201751;margin:0 0 4px">
Vuelta 2026 - Stage {esc(target['stage_no'])} projected top 20</h2>
<p style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;color:#54606d;margin:0 0 16px">
{esc(target['departure'])} to {esc(target['arrival'])} &middot; {esc(target['distance_km'])} km &middot;
{esc(target['profile_type'])}, {esc(target['finish_type'])} finish &middot;
{esc(target['vertical_meters'])} vm &middot; {esc(target['date'])}</p>

<div style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;background:#fff8e6;
border-left:4px solid #d9a406;padding:10px 12px;margin:0 0 16px">
<b>Corrected this run.</b> The previous prediction still ranked riders who had already abandoned -
Kaden Groves sat 9th for this stage, and Tadej Pogacar was still ranked 1st for stages 12, 13, 14,
18, 19 and 20 despite leaving the race on stage 8. The model took its start list from PCS, which
stays provisional and never records an in-race abandon. A live availability gate is now applied,
so all 21 stages are clean.</div>

<table style="{CSS_TABLE}">
<tr><th style="{CSS_HEAD};text-align:right">#</th><th style="{CSS_HEAD}">Rider</th>
<th style="{CSS_HEAD}">Team</th><th style="{CSS_HEAD};text-align:right">Pts</th>
<th style="{CSS_HEAD};text-align:center">TV2</th>
<th style="{CSS_HEAD};text-align:center">Band</th>
<th style="{CSS_HEAD};text-align:right">Conf</th></tr>
{''.join(rows)}
</table>
<p style="font-family:Segoe UI,Arial,sans-serif;font-size:12px;color:#54606d;margin:8px 0 0">
Pts = Scorito points if the rider finishes in that position. Band = expected finish range.
TV2 = TV 2 Axelgaard star tier.</p>

<h3 style="font-family:Segoe UI,Arial,sans-serif;color:#201751;margin:22px 0 4px">Recommended lineups</h3>
{''.join(lineups)}

<h3 style="font-family:Segoe UI,Arial,sans-serif;color:#201751;margin:22px 0 4px">Reading the stage</h3>
<p style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;margin:0 0 10px">
TV 2 Axelgaard splits this one 50/50 between a bunch sprint and a breakaway. The last 4.1 km rise at
2.6%, which favours a punchy finisher over a pure sprinter, and the day carries close to 2700 metres
of climbing. Whether it stays together depends largely on whether Van Aert and Pedersen go up the road.
Worth noting: Axelgaard makes Van Aert his only five-star rider, but the objective model has him 13th,
because his own recent comparable-stage record does not support a top-three projection here. That
disagreement is the main uncertainty in this table.</p>

<h3 style="font-family:Segoe UI,Arial,sans-serif;color:#201751;margin:22px 0 4px">Freshness and gaps</h3>
<ul style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;margin:0 0 10px;padding-left:18px">
<li>Market snapshot: {esc(sources.get('market_snapshot_time'))}</li>
<li>Prediction generated: {esc(PREDICTIONS.get('generated_at'))}</li>
<li>Rider news: {esc(sources.get('news_generated_at'))}, 16/16 sources healthy</li>
{gap_items}
</ul>
<p style="font-family:Segoe UI,Arial,sans-serif;font-size:12px;color:#54606d;margin:14px 0 0">
{esc(RECOMMENDATION.get('uncertainty'))}</p>
</div></body></html>"""

    plain = (
        f"Vuelta 2026 - Stage {target['stage_no']} projected top 20\n"
        f"{target['departure']} to {target['arrival']}, {target['distance_km']} km, "
        f"{target['profile_type']}/{target['finish_type']}, {target['date']}\n\n"
        + "\n".join(
            f"{i:>2}. {r['rider']} ({r.get('team')}) - {r.get('scorito_stage_points')} pts"
            for i, r in enumerate(stage["top_20"], 1)
        )
        + "\n\nCorrected this run: abandoned riders (Groves, Pogacar) were still being ranked; "
        "a live availability gate is now applied.\n"
    )
    return plain, body


def build_eval_email() -> tuple[str, str]:
    def table(headers: list[str], rows: list[list[str]]) -> str:
        head = "".join(f"<th style='{CSS_HEAD}'>{esc(h)}</th>" for h in headers)
        body_rows = "".join(
            "<tr>" + "".join(f"<td style='{CSS_CELL}'>{c}</td>" for c in row) + "</tr>"
            for row in rows
        )
        return f"<table style='{CSS_TABLE}'><tr>{head}</tr>{body_rows}</table>"

    accuracy = table(
        ["Stage", "Type", "Top-20 hits", "Top-9 hits", "Captain top 3", "Spearman", "Capture"],
        [
            ["2*", "Hilly / uphill", "6/20", "4/9", "No", "+0.514", "55%"],
            ["4", "Mountain / flat", "10/20", "4/9", "Yes", "+0.479", "65%"],
            ["6", "Hilly / flat", "8/20", "4/9", "Yes", "+0.171", "54%"],
            ["7", "Mountain summit", "5/20", "2/9", "No", "+0.262", "34%"],
            ["<b>Mean (4, 6, 7)</b>", "<b>audited</b>", "<b>7.7/20</b>", "<b>3.3/9</b>",
             "<b>2 of 3</b>", "<b>+0.304</b>", "<b>51%</b>"],
        ],
    )

    by_type = table(
        ["Stage type", "Mean Spearman", "Verdict"],
        [
            ["ITT", "+0.725", "Strongest by a distance"],
            ["Mountain / flat", "+0.479", "Solid - GC riders do score here"],
            ["Mountain summit", "+0.189 to +0.305", "Weak - breakaways break the model"],
            ["Hilly / flat", "-0.128 to +0.171", "Worst - close to no signal"],
        ],
    )

    captaincy = table(
        ["Stage", "Captain", "Points", "Best available", "Points", "Loss"],
        [
            ["2", "Pedersen", "36", "Brennan", "66", "-30"],
            ["5", "Pedersen", "0", "Brennan", "56", "-56"],
            ["9", "Pogacar (abandoned)", "0", "Onley", "54", "-54"],
            ["<b>Total</b>", "", "<b>332</b>", "", "<b>472</b>", "<b>-140</b>"],
        ],
    )

    value = table(
        ["Rider", "Price", "Points", "Points per million", "Verdict"],
        [
            ["Alessandro Romele", "0.75M", "90", "120", "Best value in the squad"],
            ["Sepp Kuss", "1.5M", "131", "87", "Strong"],
            ["Matthew Brennan", "2.5M", "196", "78", "Best active scorer"],
            ["Felix Gall", "4.5M", "127", "28", "Below price"],
            ["Mattias Skjelmose", "4.5M", "76", "17", "Worst return above 2M"],
        ],
    )

    body = f"""<!doctype html><html><body style="background:#f6f8fa;padding:16px">
<div style="max-width:860px;margin:0 auto;background:#ffffff;padding:22px;border:1px solid #e1e4e8">
<h2 style="font-family:Segoe UI,Arial,sans-serif;color:#201751;margin:0 0 4px">
Vuelta 2026 - model evaluation and performance analysis</h2>
<p style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;color:#54606d;margin:0 0 16px">
Out-of-sample review after stage 9. Stage 3 was neutralised, Scorito credited zero points to zero
riders, so it is excluded everywhere below.</p>

<div style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;background:#fdecea;
border-left:4px solid #c0392b;padding:10px 12px;margin:0 0 18px">
<b>Defect found and fixed.</b> The stage predictions were still ranking riders who had left the race.
Pogacar was ranked first for stages 12, 13, 14, 18, 19 and 20 after abandoning on stage 8, and Groves
appeared in eight future stages after abandoning on stage 9 - 25 dead entries across the race. The
model built its field from the PCS start list, which stays provisional and never records an in-race
abandon, and nothing cross-checked it against live Scorito availability. A gate now drops any rider
the market reports as abandoned or non-starting from every stage after the one they left. Two
regression tests cover it and the full suite passes.</div>

<h3 style="font-family:Segoe UI,Arial,sans-serif;color:#201751;margin:20px 0 6px">1. Forecast accuracy</h3>
<p style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;margin:0 0 10px">
Only archived pre-stage predictions count. Stages 1, 5, 8 and 9 have no archive, so half the race is
unauditable - itself a finding. Stage 2's archive was written 28 hours after the start and is flagged.</p>
{accuracy}
<p style="font-family:Segoe UI,Arial,sans-serif;font-size:12px;color:#54606d;margin:6px 0 0">
* Stage 2 archive timing unverified. Capture = points taken by the predicted nine against a perfect
hindsight nine.</p>

<h3 style="font-family:Segoe UI,Arial,sans-serif;color:#201751;margin:20px 0 6px">2. Where the model works</h3>
{by_type}
<p style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;margin:10px 0 0">
The pattern is consistent: the model is good when the strongest rider wins and poor when a breakaway
decides it. On stage 7 it had Pogacar first; the stage went to Leknessund from the break and 15 of the
predicted 20 missed the actual top 20. It ranks every mountain stage on GC hierarchy and has no notion
of whether the break will survive. That single blind spot explains most of the lost points.</p>

<h3 style="font-family:Segoe UI,Arial,sans-serif;color:#201751;margin:20px 0 6px">3. Calibration</h3>
<p style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;margin:0 0 10px">
In aggregate the ordering holds - predicted 1-5 average 28.4 points, 6-10 average 14.4, 11-20 average
8.9. But on stages 6 and 7 the 11-20 band outscored the 6-10 band. The model is well calibrated at the
very top and close to random in the middle tier.</p>

<h3 style="font-family:Segoe UI,Arial,sans-serif;color:#201751;margin:20px 0 6px">4. Actual points scored</h3>
<p style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;margin:0 0 10px">
Personal team 1,815 against Hawktuah 1,767 over the eight credited stages, so the personal squad leads
by 48 despite the full-season projection having Hawktuah well ahead. Stage 9 was the swing - 223
against 113 - on mountain depth. Personal squad raw market points across all 20 riders: 2,022 against
1,859.</p>

<h3 style="font-family:Segoe UI,Arial,sans-serif;color:#201751;margin:20px 0 6px">5. Captaincy is the biggest leak</h3>
{captaincy}
<p style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;margin:10px 0 0">
140 points lost from three decisions. Stage 5 is the painful one - Pedersen captained and scored
nothing while Brennan, already in the nine, scored 56. Stage 9 is the avoidable one: Pogacar was
captained after he had already abandoned, which is the same root cause as the defect fixed above.</p>

<h3 style="font-family:Segoe UI,Arial,sans-serif;color:#201751;margin:20px 0 6px">6. Value</h3>
{value}

<h3 style="font-family:Segoe UI,Arial,sans-serif;color:#201751;margin:20px 0 6px">7. What to change next</h3>
<ol style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;margin:0;padding-left:20px">
<li><b>Breakaway probability per stage.</b> The largest single source of error. Rank breakaway
specialists differently when the break is likely to stay away, rather than applying GC hierarchy to
every mountain day.</li>
<li><b>Archive every stage before the start.</b> Four of eight stages have no archive, so the model
cannot be scored on half its own race.</li>
<li><b>Rebuild the projection after each stage.</b> It is currently built once pre-race and never
updated, which is exactly how a rider who abandoned stayed at rank 1 for six future stages.</li>
<li><b>Recalibrate ranks 6 to 20.</b> Confidence falls off a cliff after the top five.</li>
<li><b>Make opinion signals stage-typed.</b> Chat and forum signals are currently rider-level
constants applied identically to a sprint and a summit finish. The 12% cap limits the damage, but the
breadth is not earned.</li>
<li><b>Build the PCS stage store.</b> data/pcs/stages.json is empty, so validate_pcs_predictor.py
cannot run at all and that validator has never reported a number.</li>
</ol>

<h3 style="font-family:Segoe UI,Arial,sans-serif;color:#201751;margin:20px 0 6px">Source health</h3>
<p style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;margin:0">
TV 2 Axelgaard is the one external source that has clearly earned its weight: bootstrap slope 95% CI
3.91 to 6.09, P(slope &gt; 0) = 1.000 over 10,000 simulations, top-nine median bonus +32.6 Scorito
points. Its weight is derived, not hand-set, and currently sits at 0.0738 of a 0.12 cap.
CyclingOracle scores +0.467 mean Spearman on the 2026 Tour but has not published a stage 10 preview.
Expert chat is 8 days stale and the WielerFlits forum is pre-race only - both stay capped as opinion
and cannot move rank, lineup or captain.</p>
</div></body></html>"""

    plain = (
        "Vuelta 2026 - model evaluation and performance analysis\n\n"
        "Defect found and fixed: abandoned riders were still ranked in future stages "
        "(Pogacar 1st for stages 12-20, Groves in 8 stages). Live availability gate added.\n\n"
        "Forecast accuracy (audited stages 4, 6, 7): top-20 7.7/20, top-9 3.3/9, "
        "mean Spearman +0.304, capture 51%.\n"
        "Captaincy loss to date: 140 points across stages 2, 5 and 9.\n"
        "Personal team 1,815 points vs Hawktuah 1,767 over eight credited stages.\n"
    )
    return plain, body


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _, stage_html = build_stage_email()
    _, eval_html = build_eval_email()
    STAGE_HTML.write_text(stage_html, encoding="utf-8")
    EVAL_HTML.write_text(eval_html, encoding="utf-8")
    print(f"Stage 10 body: {STAGE_HTML}")
    print(f"Evaluation body: {EVAL_HTML}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
