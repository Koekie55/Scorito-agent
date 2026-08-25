"""Evaluate today's Grand Tour prediction and optionally deliver it to Teams."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from scorito_agent.news.mailer import load_env_file  # noqa: E402
from scorito_agent.stage_evaluation import (  # noqa: E402
    archive_pre_stage_prediction,
    evaluate_stage_archive,
    load_stage_calendar,
    write_evaluation_once,
)
from scorito_agent.teams_graph import (  # noqa: E402
    TeamsGraphConfig,
    send_to_teams_self_chat,
)

DATA_ROOT = ROOT / "data" / "scorito"
PENDING_RESULTS_EXIT = 3


def _content(payload: Any) -> Any:
    return payload.get("Content", payload) if isinstance(payload, dict) else payload


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _discover_stage(target_date: date, slug: str | None) -> tuple[Path, dict[str, Any]] | None:
    directories = [DATA_ROOT / slug] if slug else sorted(DATA_ROOT.iterdir())
    matches = []
    for data_dir in directories:
        if not (data_dir / "marketroundstage.json").exists():
            continue
        calendar = load_stage_calendar(data_dir)
        if len(calendar) < 20:
            continue
        stage = next((row for row in calendar if row["stage_date"] == target_date), None)
        if stage:
            matches.append((data_dir, stage))
    if len(matches) > 1:
        raise RuntimeError(f"multiple Grand Tour stages found for {target_date}: {matches}")
    return matches[0] if matches else None


def _market_id(slug: str) -> int:
    registry = _load_json(DATA_ROOT / "markets_registry.json")
    for market_id, market in registry["markets"].items():
        if market.get("slug") == slug:
            return int(market_id)
    raise KeyError(f"market ID not found for {slug}")


def _refresh_results(slug: str) -> None:
    subprocess.run(
        [sys.executable, "scripts/snapshot_market.py", str(_market_id(slug)), slug],
        cwd=ROOT,
        check=True,
    )


def _archive_next_stage(data_dir: Path, current_stage_no: int) -> None:
    predictions_path = data_dir / "stage_top20_predictions.json"
    if not predictions_path.exists():
        print(f"NEXT ARCHIVE SKIPPED: no persisted predictions at {predictions_path}")
        return
    next_stage = next(
        (stage for stage in load_stage_calendar(data_dir) if stage["stage_no"] > current_stage_no),
        None,
    )
    if not next_stage:
        return
    try:
        archive_path, _, created = archive_pre_stage_prediction(
            data_dir, predictions_path, int(next_stage["stage_no"])
        )
    except RuntimeError as exc:
        print(f"NEXT ARCHIVE BLOCKED: {exc}", file=sys.stderr)
        return
    print(
        f"Next-stage archive: {archive_path} "
        f"({'created' if created else 'already immutable'})"
    )

def _render_message(report: dict[str, Any]) -> str:
    lines = [
        f"{report['race']} stage {report['stage_no']} predictability: "
        f"{report['predictability_pct']:.2f}%",
        f"Top-20 matches: {report['matched_riders']}/20 "
        f"({report['overlap_pct']:.2f}%).",
        f"Rank accuracy: {report['rank_accuracy_pct']:.2f}% "
        f"(matched-rider MAE {report['mean_absolute_rank_error_matched']}).",
        "Formula: 50% top-20 overlap + 50% rank accuracy; each matched rider's "
        "rank credit is 1 - |predicted rank - actual rank| / 19, and misses score zero.",
    ]
    misses = report.get("predicted_misses") or []
    surprises = report.get("unpredicted_finishers") or []
    if misses:
        names = ", ".join(
            f"{row['rider']} (pred #{row['predicted_finish']})" for row in misses
        )
        lines.append(f"Predicted but missed top 20 ({len(misses)}): {names}.")
    if surprises:
        names = ", ".join(
            f"{row['rider']} (actual #{row['actual_finish']})" for row in surprises
        )
        lines.append(f"Finished top 20 unpredicted ({len(surprises)}): {names}.")
    if report.get("model_improvement_analysis"):
        lines.append(f"Where the model fell short: {report['model_improvement_analysis']}")
    if report.get("pr_status"):
        lines.append(f"PR status: {report['pr_status']}")
    lines.append(f"Prediction archived: {report['prediction_archived_at']}")
    lines.append(f"Evaluation generated: {report['evaluated_at']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", help="Restrict evaluation to one saved Grand Tour slug")
    parser.add_argument("--date", type=date.fromisoformat, help="Local date override (YYYY-MM-DD)")
    parser.add_argument("--refresh-results", action="store_true", help="Refresh Scorito before evaluating")
    parser.add_argument(
        "--send-teams-if-configured",
        action="store_true",
        help="Send only when delegated Graph token and self-chat ID are configured",
    )
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args(argv)

    target_date = args.date or datetime.now().astimezone().date()
    discovered = _discover_stage(target_date, args.slug)
    if not discovered:
        print(f"SKIP: {target_date} is not a saved Grand Tour stage day.")
        return 0
    data_dir, stage = discovered
    stage_no = int(stage["stage_no"])
    stage_id = int(stage["stage_id"])
    if args.date is None:
        _archive_next_stage(data_dir, stage_no)
    archive_path = (
        data_dir / "stage_predictability" / "predictions" / f"stage_{stage_no:02d}.json"
    )
    if not archive_path.exists():
        print(
            f"BLOCKED: stage {stage_no} has no immutable pre-stage archive at {archive_path}; "
            "refusing a hindsight-contaminated evaluation.",
            file=sys.stderr,
        )
        return 2

    if args.refresh_results:
        _refresh_results(data_dir.name)
    result_path = data_dir / f"stageresult_rider_{stage_id}.json"
    actual_results = _content(_load_json(result_path)) if result_path.exists() else []
    actual_top_20 = [row for row in actual_results if 1 <= int(row.get("Rank") or 0) <= 20]
    if len(actual_top_20) != 20:
        print(
            f"PENDING: stage {stage_no} has {len(actual_top_20)}/20 actual top-20 rows; retry later.",
            file=sys.stderr,
        )
        return PENDING_RESULTS_EXIT

    report = evaluate_stage_archive(
        archive_path,
        result_path,
        data_dir / "eventriderenriched.json",
    )
    output_dir = data_dir / "stage_predictability"
    evaluation_path = output_dir / "evaluations" / f"stage_{stage_no:02d}.json"
    report, created = write_evaluation_once(evaluation_path, report)
    message = _render_message(report)
    outbox_path = output_dir / "teams_outbox" / f"stage_{stage_no:02d}.json"
    write_evaluation_once(
        outbox_path,
        {
            "schema_version": 1,
            "status": "pending_teams_delivery",
            "stage_no": stage_no,
            "created_at": datetime.now().astimezone().isoformat(),
            "evaluation_path": str(evaluation_path),
            "message": message,
        },
    )
    print(message)
    print(f"Evaluation: {evaluation_path} ({'created' if created else 'existing'})")

    if not args.send_teams_if_configured:
        return 0
    load_env_file(args.env_file)
    teams_config = TeamsGraphConfig.from_environment()
    if teams_config is None:
        print(
            "TEAMS NOT SENT: configure SCORITO_TEAMS_ACCESS_TOKEN and "
            "SCORITO_TEAMS_SELF_CHAT_ID; outbox retained.",
        )
        return 0
    delivery_path = output_dir / "teams_delivery" / f"stage_{stage_no:02d}.json"
    if delivery_path.exists():
        print(f"Teams already delivered: {delivery_path}")
        return 0
    message_id = send_to_teams_self_chat(teams_config, message)
    write_evaluation_once(
        delivery_path,
        {
            "schema_version": 1,
            "status": "delivered",
            "stage_no": stage_no,
            "delivered_at": datetime.now().astimezone().isoformat(),
            "graph_message_id": message_id,
            "expected_user": teams_config.expected_user,
        },
    )
    print(f"Teams delivered to {teams_config.expected_user}: message {message_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
