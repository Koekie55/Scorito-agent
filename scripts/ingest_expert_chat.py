"""Ingest exported cycling chat into the schema-v2 evidence store.

Messages are deduplicated by stable ID. Re-importing the same export therefore
does not amplify a claim or create duplicate evidence. Use ``--rebuild`` after
parser identity changes to replace an older store with the canonical export.

Examples::

    python scripts/ingest_expert_chat.py data/expert_chat/vuelta2026/export-01.txt
    Get-Content export.txt | python scripts/ingest_expert_chat.py - --slug vuelta2026
"""

from __future__ import annotations

from datetime import date
import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scorito_agent.expert_chat import ExpertChatStore, parse_export
from scorito_agent.scorito import load_snapshot

DEFAULT_ROOT = ROOT / "data" / "expert_chat"


def _read_export(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8-sig")

def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected an ISO date (YYYY-MM-DD), got {value!r}"
        ) from exc


def _within_dates(messages, *, from_date: date | None, through_date: date | None):
    return [
        message
        for message in messages
        if (from_date is None or message.timestamp.date() >= from_date)
        and (through_date is None or message.timestamp.date() <= through_date)
    ]


def _rider_names(slug: str) -> list[str]:
    rider_path = ROOT / "data" / "scorito" / slug / "eventriderenriched.json"
    if not rider_path.exists():
        return []
    return [rider.name for rider in load_snapshot(slug).riders]


def _print_speaker_profiles(digest: dict[str, Any]) -> None:
    speakers = digest.get("speakers", {})
    if not speakers:
        return
    print("Speaker calibration (T3 only):")
    for speaker in speakers.values():
        calibration = speaker["calibration"]
        print(
            f"  {calibration['label']}: T3={calibration['t3_factor']:.2f} "
            f"messages={speaker['messages']} "
            f"active_claims={speaker['active_model_claims']}"
        )


def _print_top_riders(digest: dict[str, Any]) -> None:
    riders = list(digest.get("riders", {}).values())
    if not riders:
        return

    riders.sort(
        key=lambda rider: (
            -abs(float(rider.get("signal", 0.0))),
            str(rider.get("name", "")).casefold(),
        )
    )
    print("Highest bounded rider signals:")
    for rider in riders[:15]:
        stages = sorted(
            (
                f"S{stage}:{float(signal):+.2f}"
                for stage, signal in rider.get("stage_signals", {}).items()
                if abs(float(signal)) > 1e-9
            )
        )
        stage_text = f" stages={','.join(stages)}" if stages else ""
        print(
            f"  {rider['name']}: {float(rider['signal']):+.2f} "
            f"active_model_claims={rider['active_model_claim_count']}"
            f"{stage_text}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", help="Path to export text, or '-' for stdin")
    parser.add_argument("--slug", default="vuelta2026")
    parser.add_argument(
        "--store-dir",
        "--store",
        dest="store_dir",
        type=Path,
        help="Schema-v2 store directory (default: data/expert_chat/<slug>)",
    )
    parser.add_argument(
        "--digest",
        type=Path,
        help="Digest output path (default: data/scorito/<slug>/expert_chat_intel.json)",
    )
    parser.add_argument(
        "--export-index",
        type=int,
        default=1,
        help="Monotonic source-export number recorded on newly seen messages",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Replace the existing store from this export instead of merging it",
    )
    parser.add_argument(
        "--from-date",
        type=_iso_date,
        help="Include messages on or after this ISO date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--through-date",
        type=_iso_date,
        help="Include messages on or before this ISO date (YYYY-MM-DD)",
    )
    args = parser.parse_args()
    if args.from_date and args.through_date and args.from_date > args.through_date:
        parser.error("--from-date cannot be later than --through-date")


    store_dir = args.store_dir or (DEFAULT_ROOT / args.slug)
    digest_path = args.digest or (
        ROOT / "data" / "scorito" / args.slug / "expert_chat_intel.json"
    )

    parsed_messages = parse_export(
        _read_export(args.export),
        export_index=args.export_index,
    )
    messages = _within_dates(
        parsed_messages, from_date=args.from_date, through_date=args.through_date
    )
    store = ExpertChatStore(store_dir)
    stats = store.import_messages(
        messages,
        _rider_names(args.slug),
        replace_existing=args.rebuild,
    )
    digest = store.write_digest(digest_path)
    summary = digest["summary"]
    active_model_claims = sum(
        rider["active_model_claim_count"] for rider in digest["riders"].values()
    )

    print(
        f"Parsed {len(parsed_messages)} messages; selected {len(messages)} and "
        f"skipped {len(parsed_messages) - len(messages)} outside the date window; "
        f"{stats['messages_added']} new, "
        f"{stats['messages_seen'] - stats['messages_added']} duplicate."
    )
    print(
        f"Store now contains {summary['messages']} messages, "
        f"{summary['claims']} notes, and {active_model_claims} "
        "active T1-T3 model claims."
    )
    print(f"Store directory: {store_dir}")
    print(f"Digest: {digest_path}")
    _print_speaker_profiles(digest)
    _print_top_riders(digest)


if __name__ == "__main__":
    main()
