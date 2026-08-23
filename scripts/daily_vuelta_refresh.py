"""Refresh Vuelta evidence and email the next-stage recommendations."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from scorito_agent.news.mailer import SMTPConfig, load_env_file, send_message  # noqa: E402
from scorito_agent.stage_evaluation import archive_pre_stage_prediction  # noqa: E402
from scorito_agent.scorito import load_snapshot  # noqa: E402
from scripts.score_saved_vuelta_teams import name_token_key, score_saved_squads  # noqa: E402
from scripts.refresh_vuelta_stage_predictions import SCORITO_STAGE_POINTS  # noqa: E402

DATA_DIR = ROOT / "data" / "scorito" / "vuelta2026"
NEWS_PATH = ROOT / "data" / "rider_news" / "vuelta2026" / "latest.json"
PREDICTIONS_PATH = DATA_DIR / "stage_top20_predictions.json"
PROJECTION_PATH = DATA_DIR / "projected_recommendation.json"
PERSONAL_PATH = DATA_DIR / "personal" / "teamselection.json"
HAWKTUAH_PATH = DATA_DIR / "hawktuah_candidate_stage_plan.json"
CYCLINGORACLE_PATH = ROOT / "data" / "cyclingoracle" / "vuelta2026_predictions.jsonl"
OUTPUT_JSON = DATA_DIR / "daily_stage_recommendation.json"
OUTPUT_MARKDOWN = DATA_DIR / "daily_stage_recommendation.md"
DEFAULT_RECIPIENTS = ("quintenkoe@hotmail.com", "wouterjanson@hotmail.com")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _content(payload: Any) -> Any:
    return payload.get("Content", payload) if isinstance(payload, dict) else payload


def _completed_and_next_stage(data_dir: Path = DATA_DIR) -> tuple[list[int], int | None]:
    rounds = sorted(
        _content(_load_json(data_dir / "marketroundstage.json")),
        key=lambda row: int(row["StageOrder"]),
    )
    completed: list[int] = []
    target: int | None = None
    for stage in rounds:
        stage_no = int(stage["StageOrder"])
        result_path = data_dir / f"stageresult_rider_{int(stage['StageId'])}.json"
        results = _content(_load_json(result_path)) if result_path.exists() else []
        if results and target is None:
            completed.append(stage_no)
        elif target is None:
            target = stage_no
    return completed, target


def _run_refresh_commands() -> None:
    commands = (
        [sys.executable, "scripts/snapshot_market.py", "310", "vuelta2026"],
        [
            sys.executable,
            "scripts/harvest_cyclingoracle.py",
            "--race-slug",
            "vuelta-a-espana-2026",
            "--output",
            str(CYCLINGORACLE_PATH),
        ],
        [sys.executable, "scripts/rider_news.py", "--force", "--external-data-root", str(ROOT)],
        [sys.executable, "scripts/refresh_vuelta_stage_predictions.py"],
        [sys.executable, "scripts/analyze_my_vuelta_team.py"],
        [sys.executable, "scripts/export_vuelta_race_book.py"],
    )
    for command in commands:
        print(f"Running: {' '.join(command[1:])}")
        subprocess.run(command, cwd=ROOT, check=True)


def _apply_cyclingoracle_stage(
    stage: dict[str, Any], path: Path = CYCLINGORACLE_PATH
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if int(row.get("stage_number") or 0) == int(stage["stage_no"]):
            rows.append(row)
    if not rows:
        return None
    rows.sort(key=lambda row: int(row.get("model_rank") or row.get("predicted_rank") or 999))

    pcs_by_key = {
        name_token_key(str(row["rider"])): row for row in stage["top_20"]
    }
    ranked = []
    seen = set()
    for oracle_row in rows:
        rider_name = str(oracle_row.get("rider_name") or "").strip()
        key = name_token_key(rider_name)
        if not key or key in seen:
            continue
        seen.add(key)
        prediction = dict(pcs_by_key.get(key, {}))
        prediction.update(
            {
                "rider": prediction.get("rider", rider_name),
                "rider_slug": prediction.get("rider_slug") or oracle_row.get("rider_slug"),
                "evidence": (
                    f"CyclingOracle stage {stage['stage_no']} rank; "
                    f"win probability {float(oracle_row.get('win_probability_pct') or 0):.2f}%."
                ),
                "confidence": "external_stage_prediction",
                "uncertainty": "CyclingOracle win probability is a forecast, not a result guarantee.",
                "prediction_source": "cyclingoracle",
                "cyclingoracle_win_probability_pct": oracle_row.get("win_probability_pct"),
            }
        )
        ranked.append(prediction)
    for pcs_row in stage["top_20"]:
        key = name_token_key(str(pcs_row["rider"]))
        if key in seen:
            continue
        seen.add(key)
        prediction = dict(pcs_row)
        prediction["prediction_source"] = "pcs_fallback"
        ranked.append(prediction)
        if len(ranked) >= 20:
            break
    for rank, prediction in enumerate(ranked[:20], start=1):
        prediction["predicted_finish"] = rank
        prediction["scorito_stage_points"] = SCORITO_STAGE_POINTS[rank]
    stage["top_20"] = ranked[:20]
    return {
        "source_url": rows[0].get("source_url"),
        "prediction_count": len(rows),
        "file_updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(),
    }


def _personal_squad(snapshot: Any) -> list[str]:
    rider_ids = _content(_load_json(PERSONAL_PATH))
    if not isinstance(rider_ids, list) or len(rider_ids) != 20 or len(set(rider_ids)) != 20:
        raise RuntimeError("personal teamselection must contain exactly 20 unique riders")
    riders = [snapshot.rider(int(rider_id)) for rider_id in rider_ids]
    if any(rider is None for rider in riders):
        raise RuntimeError("personal team contains a rider absent from the current market")
    return [rider.name for rider in riders]


def _saved_squads(snapshot: Any) -> list[dict[str, Any]]:
    hawktuah_squad = _load_json(HAWKTUAH_PATH).get("squad")
    if not isinstance(hawktuah_squad, list) or len(hawktuah_squad) != 20:
        raise RuntimeError("Hawktuah plan must contain a 20-rider squad")
    return [
        {
            "team": "Personal",
            "sources": [str(PERSONAL_PATH.relative_to(ROOT))],
            "riders": _personal_squad(snapshot),
        },
        {
            "team": "Hawktuah comparison (different squad)",
            "sources": [str(HAWKTUAH_PATH.relative_to(ROOT))],
            "riders": [str(name) for name in hawktuah_squad],
        },
    ]


def _short(value: Any, limit: int = 320) -> str:
    text = " ".join(str(value or "No comparable-result detail available.").split())
    return text if len(text) <= limit else f"{text[: limit - 3].rstrip()}..."


def _relevant_news(news: dict[str, Any], rider_names: set[str]) -> list[dict[str, Any]]:
    keys = {name_token_key(name) for name in rider_names}
    rows, seen = [], set()
    for item in news.get("selection_impacts", []):
        identity = (item.get("title"), item.get("url"))
        if name_token_key(str(item.get("rider") or "")) not in keys or identity in seen:
            continue
        seen.add(identity)
        rows.append(
            {key: item.get(key) for key in (
                "rider", "impact", "verification", "decision_hint", "title", "url", "published_at"
            )}
        )
    return rows[:8]


def _team_stage_report(
    team: dict[str, Any], stage: dict[str, Any], lineup: dict[str, Any]
) -> dict[str, Any]:
    predictions = {name_token_key(str(row["rider"])): row for row in stage["top_20"]}
    selected_keys = {name_token_key(name) for name in lineup["lineup"]}
    riders = []
    for name in lineup["lineup"]:
        row = predictions.get(name_token_key(name))
        riders.append(
            {
                "rider": name,
                "predicted_finish": int(row["predicted_finish"]) if row else None,
                "projected_points": int(row["scorito_stage_points"]) if row else 0,
                "point_route": (
                    f"Projected #{row['predicted_finish']} stage finish for "
                    f"{row['scorito_stage_points']} Scorito points."
                    if row else "Outside the projected top 20; selected after stronger squad options were exhausted."
                ),
                "evidence": _short(row.get("evidence")) if row else "No projected stage placing.",
                "confidence": row.get("confidence") if row else None,
                "uncertainty": row.get("uncertainty") if row else None,
                "news": row.get("news") if row else None,
            }
        )

    squad_keys = {name_token_key(name): name for name in team["riders"]}
    excluded = [
        {
            "rider": squad_keys[name_token_key(str(row["rider"]))],
            "predicted_finish": int(row["predicted_finish"]),
            "projected_points": int(row["scorito_stage_points"]),
        }
        for row in stage["top_20"]
        if name_token_key(str(row["rider"])) in squad_keys
        and name_token_key(str(row["rider"])) not in selected_keys
    ]
    return {
        "team": team["team"],
        "squad_sources": team["sources"],
        "legal_current_market": team["legal_current_market"],
        "current_price": team["current_price"],
        "budget_remaining": team["budget_remaining"],
        "max_trade_team_count": team["max_trade_team_count"],
        "lineup": lineup["lineup"],
        "captain": lineup["captain"],
        "projected_stage_points": lineup["projected_stage_points"],
        "riders": riders,
        "captain_rationale": "Highest objective projected stage score among the selected nine.",
        "main_excluded_alternative": excluded[0] if excluded else None,
    }


def build_report(*, archive_prediction: bool = True) -> dict[str, Any]:
    snapshot = load_snapshot("vuelta2026")
    predictions = _load_json(PREDICTIONS_PATH)
    projection = _load_json(PROJECTION_PATH)
    news = _load_json(NEWS_PATH)
    completed, target_stage_no = _completed_and_next_stage()
    if target_stage_no is None:
        return {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "race_complete",
            "completed_stages": completed,
        }

    stage = next(row for row in predictions["stages"] if int(row["stage_no"]) == target_stage_no)
    cyclingoracle = _apply_cyclingoracle_stage(stage)
    prediction_archive = None
    if archive_prediction:
        archive_path, archive, created = archive_pre_stage_prediction(
            DATA_DIR, PREDICTIONS_PATH, target_stage_no
        )
        prediction_archive = {
            "path": str(archive_path.relative_to(ROOT)),
            "archived_at": archive["archived_at"],
            "created": created,
        }
    scored = score_saved_squads(predictions, projection, _saved_squads(snapshot), snapshot)
    team_reports = []
    for team in scored["teams"]:
        lineup = next(row for row in team["lineups"] if int(row["stage_no"]) == target_stage_no)
        team_reports.append(_team_stage_report(team, stage, lineup))
    team_reports.sort(key=lambda team: (team["team"] != "Personal", team["team"]))
    selected_names = {name for team in team_reports for name in team["lineup"]}
    source_data = predictions.get("sources", {})
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "forward_recommendation",
        "market_status": "live",
        "completed_stages": completed,
        "target_stage": {key: stage.get(key) for key in (
            "stage_no", "date", "departure", "arrival", "distance_km", "profile_type",
            "finish_type", "vertical_meters", "gradient_final_km", "source_url"
        )},
        "teams": team_reports,
        "sources": {
            "market_snapshot_time": source_data.get("market_snapshot_time"),
            "prediction_generated_at": predictions.get("generated_at"),
            "projection_generated_at": source_data.get("projection_generated_at"),
            "projection_model_version": source_data.get("projection_model_version"),
            "pcs_startlist": source_data.get("pcs_startlist"),
            "rider_news_generated_at": news.get("generated_at"),
            "rider_news_health": f"{news.get('source_success_count')}/{news.get('source_count')} sources healthy",
            "forum_generated_at": source_data.get("forum_generated_at"),
            "stage_profile": stage.get("source_url"),
            "cyclingoracle": cyclingoracle,
            "pre_stage_prediction_archive": prediction_archive,
            "relevant_news": _relevant_news(news, selected_names),
        },
        "method": predictions.get("method"),
        "uncertainty": predictions.get("uncertainty"),
        "data_gaps": [
            "Breakaway composition, late weather, crashes and tactical changes remain uncertain.",
            "Team bonuses are conditional upside and do not determine the selected nine or captain.",
            "Hawktuah ownership remains based on the saved plan until authenticated league access is restored.",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    if report["status"] == "race_complete":
        return "# Vuelta 2026 daily recommendation\n\nAll stages have completed.\n"
    stage = report["target_stage"]
    lines = [
        f"# Vuelta 2026 stage {stage['stage_no']} recommendation", "",
        f"{stage.get('departure')} to {stage.get('arrival')}, {stage.get('distance_km')} km, "
        f"{stage.get('profile_type')} / {stage.get('finish_type')}.",
        f"Completed stages: {', '.join(map(str, report['completed_stages'])) or 'none'}.", "",
    ]
    for team in report["teams"]:
        lines.extend([
            f"## {team['team']}", "",
            f"Captain: **{team['captain']}**. Projected enrolled points: {team['projected_stage_points']:.0f}.",
            f"Legality: {'PASS' if team['legal_current_market'] else 'FAIL'}; price {team['current_price']:,}; "
            f"budget left {team['budget_remaining']:,}; team cap {team['max_trade_team_count']}/4.", "",
        ])
        for index, rider in enumerate(team["riders"], start=1):
            captain = " (C)" if rider["rider"] == team["captain"] else ""
            lines.append(f"{index}. **{rider['rider']}**{captain}: {rider['point_route']} {rider['evidence']}")
        alternative = team["main_excluded_alternative"]
        alternative_text = (
            f"{alternative['rider']} (projected #{alternative['predicted_finish']}, "
            f"{alternative['projected_points']} points)."
            if alternative else "No squad rider outside the nine has a projected top-20 finish."
        )
        lines.extend(["", f"Main excluded alternative: {alternative_text}", ""])
    sources = report["sources"]
    lines.extend([
        "## Sources and uncertainty", "",
        f"- Market snapshot: {sources['market_snapshot_time']}",
        f"- Prediction: {sources['prediction_generated_at']} ({sources['projection_model_version']})",
        f"- Rider news: {sources['rider_news_generated_at']}; {sources['rider_news_health']}",
        f"- PCS start list: {sources['pcs_startlist']}",
        f"- Stage profile: {sources['stage_profile']}",
        f"- CyclingOracle: {sources['cyclingoracle'] or 'not yet published; PCS fallback used'}",
        f"- Uncertainty: {report['uncertainty']}",
    ])
    for item in sources["relevant_news"]:
        lines.append(f"- News: {item['rider']}: {item['title']} ({item['verification']}; {item['url']})")
    return "\n".join(lines).rstrip() + "\n"


def _require_cyclingoracle_for_email(report: dict[str, Any]) -> None:
    if report.get("status") != "forward_recommendation":
        return
    target_stage = report.get("target_stage", {}).get("stage_no")
    cyclingoracle = report.get("sources", {}).get("cyclingoracle")
    if not cyclingoracle:
        raise RuntimeError(
            f"Stage {target_stage} WielerOrakel/CyclingOracle prediction is not published; "
            "email was not sent."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Render saved inputs without refresh, writes or email")
    parser.add_argument("--send-email", action="store_true", help="Send through configured SMTP")
    parser.add_argument(
        "--require-cyclingoracle",
        action="store_true",
        help="Fail unless the target-stage CyclingOracle prediction is published",
    )
    parser.add_argument("--recipient", action="append", help="Override recipient; repeat for multiple addresses")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args(argv)
    if args.dry_run and args.send_email:
        parser.error("--dry-run cannot be combined with --send-email")
    if not args.dry_run:
        _run_refresh_commands()
    report = build_report(archive_prediction=not args.dry_run)
    markdown = render_markdown(report)
    print(markdown)
    if not args.dry_run:
        OUTPUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        OUTPUT_MARKDOWN.write_text(markdown, encoding="utf-8")
        print(f"JSON: {OUTPUT_JSON}")
        print(f"Markdown: {OUTPUT_MARKDOWN}")
    if args.send_email or args.require_cyclingoracle:
        _require_cyclingoracle_for_email(report)
    if args.send_email:
        load_env_file(args.env_file)
        config = replace(
            SMTPConfig.from_environment(),
            recipients=tuple(args.recipient or DEFAULT_RECIPIENTS),
        )
        stage_no = report.get("target_stage", {}).get("stage_no", "complete")
        send_message(
            config,
            subject=f"[Scorito Vuelta] Stage {stage_no} personal and Hawktuah recommendations",
            plain_body=markdown,
        )
        print(f"Email sent to: {', '.join(config.recipients)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
