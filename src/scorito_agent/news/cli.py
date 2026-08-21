"""Command-line interface for scheduled and manual rider-news runs."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .mailer import SMTPConfig, load_env_file
from .pipeline import PipelineError, run_pipeline


ROOT = Path(__file__).resolve().parents[3]
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--now must include a timezone offset")
    return parsed


def scheduled_run_due(race: dict[str, object], now: datetime, *, tolerance_minutes: int = 45) -> tuple[bool, str]:
    timezone_name = str(race.get("timezone", "Europe/Amsterdam"))
    try:
        local_now = now.astimezone(ZoneInfo(timezone_name))
    except ZoneInfoNotFoundError as exc:
        raise PipelineError(f"unknown race timezone {timezone_name!r}") from exc
    start = datetime.fromisoformat(str(race["monitoring_start"])).date()
    end = datetime.fromisoformat(str(race["monitoring_end"])).date()
    if not start <= local_now.date() <= end:
        return False, f"outside monitoring season {start.isoformat()}..{end.isoformat()}"
    slots = race.get("schedule_times", [])
    if not isinstance(slots, list) or not slots:
        raise PipelineError("race schedule_times must be a non-empty list")
    differences = []
    for raw_slot in slots:
        slot_time = datetime.strptime(str(raw_slot), "%H:%M").time()
        slot = datetime.combine(local_now.date(), slot_time, tzinfo=local_now.tzinfo)
        differences.append((abs((local_now - slot).total_seconds()) / 60.0, str(raw_slot)))
    difference, nearest = min(differences)
    if difference > tolerance_minutes:
        return False, f"local time {local_now.strftime('%H:%M')} is not due; nearest slot is {nearest}"
    return True, f"due for {nearest} {timezone_name}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect and analyze current Vuelta rider news, then optionally email new highlights."
    )
    parser.add_argument("--sources", type=Path, default=ROOT / "config" / "rider_news_sources.json")
    parser.add_argument("--watchlist", type=Path, default=ROOT / "config" / "vuelta2026_watchlist.json")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "rider_news" / "vuelta2026")
    parser.add_argument("--external-data-root", type=Path, default=None)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--max-age-hours", type=float, default=None)
    parser.add_argument("--now", type=_aware_datetime, default=None, help="ISO timestamp for deterministic runs/tests")
    parser.add_argument("--email", action="store_true", help="Email all fresh highlights not successfully emailed before")
    parser.add_argument(
        "--email-if-configured",
        action="store_true",
        help="Email when SMTP settings exist; otherwise warn and still write the local digest",
    )
    parser.add_argument("--scheduled", action="store_true", help="Run only in the configured season and near a schedule slot")
    parser.add_argument("--force", action="store_true", help="Bypass the race-season/schedule gate")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and analyze without writing state or sending email")
    parser.add_argument("--json", action="store_true", help="Print the complete report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run and (args.email or args.email_if_configured):
        raise SystemExit("--dry-run cannot be combined with an email option")
    now = args.now or datetime.now().astimezone()
    try:
        watchlist_config = json.loads(args.watchlist.read_text(encoding="utf-8"))
        race = watchlist_config["race"]
        if args.scheduled and not args.force:
            due, reason = scheduled_run_due(race, now)
            if not due:
                print(f"Rider-news run skipped: {reason}")
                return 0
            print(f"Rider-news schedule gate: {reason}")

        external_root = args.external_data_root
        if external_root is None and (ROOT / "data" / "scorito").exists():
            external_root = ROOT
        email_config = None
        email_requested = args.email or args.email_if_configured
        if email_requested:
            load_env_file(args.env_file)
            try:
                email_config = SMTPConfig.from_environment()
            except ValueError as exc:
                if args.email:
                    raise
                print(f"Email delivery disabled: {exc}", file=sys.stderr)
        result = run_pipeline(
            sources_path=args.sources,
            watchlist_path=args.watchlist,
            data_dir=args.data_dir,
            external_root=external_root,
            now=now,
            max_age_hours=args.max_age_hours,
            email_config=email_config,
            dry_run=args.dry_run,
        )
    except (PipelineError, ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"Rider-news run failed: {exc}", file=sys.stderr)
        return 2

    report = result.report
    print(
        f"{report['race']['name']}: {report['highlight_count']} fresh highlights "
        f"({report['new_highlight_count']} new), {report['source_success_count']}/{report['source_count']} sources healthy"
    )
    if not args.dry_run:
        print(f"JSON: {result.latest_json}")
        print(f"Markdown: {result.latest_markdown}")
    if email_requested:
        if email_config:
            print(f"Email: {result.emailed_count} highlights sent")
        else:
            print("Email: disabled until SCORITO_NEWS_* settings are configured")
    for source in report["sources"]:
        if source["status"] != "ok" or source["enrichment_errors"]:
            print(
                f"Source {source['name']}: {source['status']}; "
                f"errors={len(source['errors'])}; enrichment_errors={len(source['enrichment_errors'])}"
            )
    for highlight in result.highlights[:10]:
        riders = ", ".join(rider.name for rider in highlight.riders) or "race-wide"
        print(
            f"- [{highlight.score:.0f}] {riders}: {highlight.item.title} "
            f"({highlight.verification}, {highlight.decision_hint})"
        )
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
