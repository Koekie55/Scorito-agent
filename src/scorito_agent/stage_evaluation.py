"""Auditable comparison of immutable stage predictions with actual results."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence


TOP_N = 20
MAX_RANK_DISTANCE = TOP_N - 1


def calculate_predictability(
    predicted_rider_ids: Sequence[int],
    actual_results: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Score top-20 coverage and placement accuracy on a 0-100 scale."""
    predicted = [int(rider_id) for rider_id in predicted_rider_ids]
    if len(predicted) != TOP_N or len(set(predicted)) != TOP_N:
        raise ValueError("prediction must contain exactly 20 unique rider IDs")

    actual_by_id: dict[int, int] = {}
    for row in actual_results:
        rank = int(row["Rank"])
        if not 1 <= rank <= TOP_N:
            continue
        rider_id = int(row["RiderId"])
        if rider_id in actual_by_id:
            raise ValueError(f"actual top 20 contains duplicate rider ID {rider_id}")
        actual_by_id[rider_id] = rank
    if len(actual_by_id) != TOP_N:
        raise ValueError("actual result must contain exactly 20 unique top-20 riders")

    predicted_rank = {rider_id: rank for rank, rider_id in enumerate(predicted, start=1)}
    matched_ids = set(predicted_rank) & set(actual_by_id)
    overlap_pct = 100.0 * len(matched_ids) / TOP_N
    rank_accuracy_pct = (
        100.0
        * sum(
            1.0
            - abs(predicted_rank[rider_id] - actual_by_id[rider_id])
            / MAX_RANK_DISTANCE
            for rider_id in matched_ids
        )
        / TOP_N
    )
    predictability_pct = (overlap_pct + rank_accuracy_pct) / 2.0

    return {
        "formula_version": "top20-overlap-rank-v1",
        "top_n": TOP_N,
        "matched_riders": len(matched_ids),
        "overlap_pct": round(overlap_pct, 2),
        "rank_accuracy_pct": round(rank_accuracy_pct, 2),
        "predictability_pct": round(predictability_pct, 2),
        "mean_absolute_rank_error_matched": round(
            sum(
                abs(predicted_rank[rider_id] - actual_by_id[rider_id])
                for rider_id in matched_ids
            )
            / len(matched_ids),
            2,
        )
        if matched_ids
        else None,
    }

def _content(payload: Any) -> Any:
    return payload.get("Content", payload) if isinstance(payload, dict) else payload


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalise_name(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return " ".join(sorted(re.findall(r"[a-z0-9]+", ascii_value.lower())))


def load_stage_calendar(data_dir: Path) -> list[dict[str, Any]]:
    """Load exact stage dates from Scorito's saved stage records."""
    rounds = sorted(
        _content(_load_json(data_dir / "marketroundstage.json")),
        key=lambda row: int(row["StageOrder"]),
    )
    calendar = []
    for round_row in rounds:
        stage_id = int(round_row["StageId"])
        stage = _content(_load_json(data_dir / f"stage_{stage_id}.json"))
        start = datetime.fromisoformat(str(stage["StartDate"]))
        calendar.append(
            {
                "stage_no": int(round_row["StageOrder"]),
                "stage_id": stage_id,
                "market_round_id": int(round_row["MarketRoundId"]),
                "start_at_local": start,
                "stage_date": start.date(),
                "start_location": stage.get("StartLocation"),
                "finish_location": stage.get("FinishLocation"),
            }
        )
    return calendar


def stage_on_date(data_dir: Path, target_date: date) -> dict[str, Any] | None:
    return next(
        (stage for stage in load_stage_calendar(data_dir) if stage["stage_date"] == target_date),
        None,
    )


def _prediction_rider_ids(data_dir: Path, stage: dict[str, Any]) -> list[int]:
    market_riders = _content(_load_json(data_dir / "eventriderenriched.json"))
    ids_by_name: dict[str, list[int]] = {}
    for rider in market_riders:
        name = f"{rider.get('FirstName') or ''} {rider.get('LastName') or ''}".strip()
        key = _normalise_name(name or str(rider.get("NameShort") or ""))
        if key:
            ids_by_name.setdefault(key, []).append(int(rider["RiderId"]))

    rider_ids = []
    for row in stage["top_20"]:
        rider_name = str(row["rider"])
        matches = ids_by_name.get(_normalise_name(rider_name), [])
        if len(matches) != 1:
            raise ValueError(
                f"prediction rider {rider_name!r} resolves to {len(matches)} market riders"
            )
        rider_ids.append(matches[0])
    if len(rider_ids) != TOP_N or len(set(rider_ids)) != TOP_N:
        raise ValueError("prediction must resolve to exactly 20 unique market riders")
    return rider_ids


def _exclusive_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as destination:
        json.dump(payload, destination, ensure_ascii=False, indent=2)
        destination.write("\n")
        destination.flush()
        os.fsync(destination.fileno())


def archive_pre_stage_prediction(
    data_dir: Path,
    predictions_path: Path,
    stage_no: int,
    *,
    now: datetime | None = None,
) -> tuple[Path, dict[str, Any], bool]:
    """Persist one pre-start prediction; an existing archive is never replaced."""
    archive_path = data_dir / "stage_predictability" / "predictions" / f"stage_{stage_no:02d}.json"
    if archive_path.exists():
        archive = _load_json(archive_path)
        if int(archive.get("stage_no", 0)) != stage_no:
            raise ValueError(f"archive stage mismatch in {archive_path}")
        return archive_path, archive, False

    calendar_stage = next(
        stage for stage in load_stage_calendar(data_dir) if stage["stage_no"] == stage_no
    )
    current = now or datetime.now().astimezone()
    current_local = current.astimezone().replace(tzinfo=None) if current.tzinfo else current
    if current_local >= calendar_stage["start_at_local"]:
        raise RuntimeError(
            f"stage {stage_no} started at {calendar_stage['start_at_local'].isoformat()}; "
            "refusing to create a post-start prediction archive"
        )

    predictions = _load_json(predictions_path)
    prediction_stage = next(
        stage for stage in predictions["stages"] if int(stage["stage_no"]) == stage_no
    )
    rider_ids = _prediction_rider_ids(data_dir, prediction_stage)
    top_20 = []
    for rider_id, row in zip(rider_ids, prediction_stage["top_20"], strict=True):
        top_20.append({**row, "rider_id": rider_id})
    archive = {
        "schema_version": 1,
        "status": "immutable_pre_stage_prediction",
        "race": predictions.get("race") or data_dir.name,
        "race_slug": data_dir.name,
        "stage_no": stage_no,
        "stage_id": calendar_stage["stage_id"],
        "stage_date": calendar_stage["stage_date"].isoformat(),
        "stage_start_at_local": calendar_stage["start_at_local"].isoformat(),
        "archived_at": current.astimezone().isoformat() if current.tzinfo else current.isoformat(),
        "prediction_generated_at": predictions.get("generated_at"),
        "prediction_source_path": str(predictions_path),
        "prediction_source_sha256": _sha256(predictions_path),
        "formula_version": "top20-overlap-rank-v1",
        "top_20": top_20,
    }
    _exclusive_json_write(archive_path, archive)
    return archive_path, archive, True


def evaluate_stage_archive(
    archive_path: Path,
    result_path: Path,
    riders_path: Path,
    *,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    archive = _load_json(archive_path)
    actual_results = _content(_load_json(result_path))
    score = calculate_predictability(
        [int(row["rider_id"]) for row in archive["top_20"]], actual_results
    )
    riders = _content(_load_json(riders_path))
    names_by_id = {
        int(row["RiderId"]): f"{row.get('FirstName') or ''} {row.get('LastName') or ''}".strip()
        for row in riders
    }
    actual_top_20 = sorted(
        (
            {
                "actual_finish": int(row["Rank"]),
                "rider_id": int(row["RiderId"]),
                "rider": names_by_id.get(int(row["RiderId"]), str(row["RiderId"])),
            }
            for row in actual_results
            if 1 <= int(row["Rank"]) <= TOP_N
        ),
        key=lambda row: row["actual_finish"],
    )
    predicted_ids = {int(row["rider_id"]) for row in archive["top_20"]}
    actual_ids = {row["rider_id"] for row in actual_top_20}
    predicted_misses = sorted(
        (row for row in archive["top_20"] if int(row["rider_id"]) not in actual_ids),
        key=lambda row: row["predicted_finish"],
    )
    unpredicted_finishers = [
        row for row in actual_top_20 if row["rider_id"] not in predicted_ids
    ]
    timestamp = evaluated_at or datetime.now().astimezone()
    return {
        "schema_version": 1,
        "status": "completed_stage_evaluation",
        "race": archive["race"],
        "race_slug": archive["race_slug"],
        "stage_no": int(archive["stage_no"]),
        "stage_id": int(archive["stage_id"]),
        "stage_date": archive["stage_date"],
        "evaluated_at": timestamp.astimezone().isoformat() if timestamp.tzinfo else timestamp.isoformat(),
        "prediction_archive_path": str(archive_path),
        "prediction_archived_at": archive["archived_at"],
        "prediction_source_sha256": archive["prediction_source_sha256"],
        "result_source_path": str(result_path),
        "result_source_sha256": _sha256(result_path),
        "formula": (
            "predictability = 0.5 * top-20 overlap + 0.5 * rank accuracy; "
            "overlap = matches / 20; rank accuracy = sum(1 - |predicted-actual| / 19) / 20 "
            "over matched riders, with non-matches contributing zero"
        ),
        **score,
        "predicted_top_20": archive["top_20"],
        "actual_top_20": actual_top_20,
        "predicted_misses": predicted_misses,
        "unpredicted_finishers": unpredicted_finishers,
    }


def write_evaluation_once(path: Path, report: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if path.exists():
        return _load_json(path), False
    _exclusive_json_write(path, report)
    return report, True
