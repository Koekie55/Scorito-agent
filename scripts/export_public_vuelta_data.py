"""Export a reproducible, credential-free Vuelta analysis snapshot."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "data" / "scorito" / "vuelta2026"
PUBLIC_DIR = ROOT / "data" / "public" / "vuelta2026"


def load_json(name: str) -> dict[str, Any]:
    return json.loads((SOURCE_DIR / name).read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def json_cell(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_inputs(projection: dict[str, Any], top20: dict[str, Any], optimization: dict[str, Any]) -> None:
    riders = projection["riders"]
    stages = projection["stages"]
    rankings = projection["stage_rankings"]
    rider_slugs = {row["rider_slug"] for row in riders}
    if len(stages) != 21 or len(rankings) != 21:
        raise ValueError("public export requires exactly 21 stages")
    if len(rider_slugs) != len(riders):
        raise ValueError("projection contains duplicate rider slugs")
    for stage_no in range(1, 22):
        stage_rows = rankings[str(stage_no)]
        if len(stage_rows) != len(riders):
            raise ValueError(f"stage {stage_no} does not rank every rider")
        if {row["rider_slug"] for row in stage_rows} != rider_slugs:
            raise ValueError(f"stage {stage_no} rider set differs from projection")

    if len(top20["stages"]) != 21:
        raise ValueError("top-20 export requires exactly 21 stages")
    for stage in top20["stages"]:
        rows = stage["top_20"]
        if len(rows) != 20:
            raise ValueError(f"stage {stage['stage_no']} does not have 20 predictions")
        if [row["predicted_finish"] for row in rows] != list(range(1, 21)):
            raise ValueError(f"stage {stage['stage_no']} finish ranks are incomplete")

    constraints = optimization["constraints"]
    for key in ("base_optimal", "conditional_optimal"):
        result = optimization[key]
        if len(result["rider_ids"]) != constraints["squad_size"]:
            raise ValueError(f"{key} does not contain 20 riders")
        if len(set(result["rider_ids"])) != constraints["squad_size"]:
            raise ValueError(f"{key} contains duplicate riders")
        if result["price"] > constraints["budget"]:
            raise ValueError(f"{key} exceeds the budget")
        if len(result["stages"]) != 21:
            raise ValueError(f"{key} does not contain 21 stage lineups")
        for stage in result["stages"]:
            if len(stage["lineup"]) != constraints["lineup_size"]:
                raise ValueError(f"{key} stage {stage['stage_no']} lineup is not nine")
            if stage["captain"] not in stage["lineup"]:
                raise ValueError(f"{key} stage {stage['stage_no']} captain is not enrolled")


def build_rider_rows(projection: dict[str, Any]) -> list[dict[str, Any]]:
    rankings_by_rider: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for stage_no, rankings in projection["stage_rankings"].items():
        for row in rankings:
            rankings_by_rider[row["rider_slug"]].append((int(stage_no), row))

    rows = []
    for rider in projection["riders"]:
        stage_rows = rankings_by_rider[rider["rider_slug"]]
        best_rank = min(row["rank"] for _, row in stage_rows)
        best_stages = [stage_no for stage_no, row in stage_rows if row["rank"] == best_rank]
        rows.append({
            "rider": rider["rider"],
            "rider_slug": rider["rider_slug"],
            "team": rider["team"],
            "projected_price": rider["projected_price"],
            "role": rider["role"],
            "age": rider["age"],
            "availability": json_cell(rider["availability"]),
            "best_stage_rank": best_rank,
            "best_stage_numbers": json_cell(best_stages),
            "top_10_appearances": sum(row["rank"] <= 10 for _, row in stage_rows),
            "top_20_appearances": sum(row["rank"] <= 20 for _, row in stage_rows),
            "projected_stage_points": round(sum(row["projected_scorito_points"] for _, row in stage_rows), 4),
            "mean_model_score": round(sum(row["score"] for _, row in stage_rows) / len(stage_rows), 6),
            "mean_confidence": round(sum(row["confidence"] for _, row in stage_rows) / len(stage_rows), 6),
            "recent_evidence": json_cell(rider["recent_evidence"]),
            "model_qualities": json_cell(rider["model_qualities"]),
            "signals": json_cell(rider["signals"]),
            "capabilities": json_cell(rider["capabilities"]),
        })
    return rows


def build_stage_rider_rows(projection: dict[str, Any]) -> list[dict[str, Any]]:
    stages = {int(row["stage_no"]): row for row in projection["stages"]}
    rows = []
    for stage_no_text, rankings in projection["stage_rankings"].items():
        stage_no = int(stage_no_text)
        stage = stages[stage_no]
        for ranking in rankings:
            components = ranking["score_components"]
            finish_band = ranking["expected_finish_band"]
            rows.append({
                "stage_no": stage_no,
                "date": stage["date"],
                "departure": stage["departure"],
                "arrival": stage["arrival"],
                "profile_type": stage["profile_type"],
                "finish_type": stage["finish_type"],
                "rider_rank": ranking["rank"],
                "rider": ranking["rider"],
                "rider_slug": ranking["rider_slug"],
                "model_score": ranking["score"],
                "projected_scorito_points": ranking["projected_scorito_points"],
                "expected_finish_low": finish_band[0],
                "expected_finish_high": finish_band[1],
                "confidence": ranking["confidence"],
                "uncertainty": ranking["uncertainty"],
                "role_assumption": ranking["role_assumption"],
                "specialty_score": components["specialty"],
                "ranking_score": components["ranking"],
                "historical_similarity_score": components["historical_similarity"],
                "recent_profile_evidence_score": components["recent_profile_evidence"],
                "recent_course_evidence_score": components["recent_course_evidence"],
                "role_factor": components["role_factor"],
                "evidence": ranking["evidence"],
            })
    return rows


def build_public_snapshot() -> Path:
    projection = load_json("projected_recommendation.json")
    top20 = load_json("stage_top20_predictions.json")
    optimization = load_json("optimal_team_exact_analysis.json")
    validate_inputs(projection, top20, optimization)
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    for old_file in PUBLIC_DIR.iterdir():
        if old_file.is_file():
            old_file.unlink()

    raw_copies = {
        "projection_full.json": "projected_recommendation.json",
        "top20_per_stage.json": "stage_top20_predictions.json",
        "top20_per_stage.csv": "stage_top20_predictions.csv",
        "market_riders_raw.json": "eventriderenriched.json",
        "market_rider_qualities_raw.json": "eventrider_qualities.json",
        "market_stages_raw.json": "marketroundstage.json",
    }
    for destination, source in raw_copies.items():
        shutil.copyfile(SOURCE_DIR / source, PUBLIC_DIR / destination)

    objective_optimization = {key: optimization[key] for key in (
        "schema_version", "generated_at", "prediction_generated_at",
        "projection_generated_at", "scoring_method", "constraints",
        "base_optimal", "conditional_optimal",
    )}
    objective_optimization["privacy_note"] = "Personal-team comparison fields were intentionally omitted."
    write_json(PUBLIC_DIR / "objective_optimal_teams.json", objective_optimization)
    write_json(PUBLIC_DIR / "riders.json", {
        "schema_version": projection["schema_version"],
        "model_version": projection["model_version"],
        "generated_at": projection["generated_at"],
        "projection_price_status": projection["price_status"],
        "optimization_price_status": "live Scorito market snapshot",
        "startlist_status": projection["startlist_status"],
        "riders": projection["riders"],
    })
    write_json(PUBLIC_DIR / "stages.json", {
        "schema_version": projection["schema_version"],
        "model_version": projection["model_version"],
        "generated_at": projection["generated_at"],
        "stages": projection["stages"],
    })

    rider_rows = build_rider_rows(projection)
    write_csv(PUBLIC_DIR / "rider_analytics.csv", list(rider_rows[0]), rider_rows)
    stage_rows = projection["stages"]
    write_csv(PUBLIC_DIR / "stage_analytics.csv", list(stage_rows[0]), stage_rows)
    stage_rider_rows = build_stage_rider_rows(projection)
    write_csv(PUBLIC_DIR / "stage_rider_analytics.csv", list(stage_rider_rows[0]), stage_rider_rows)

    readme = f"""# Vuelta 2026 public verification snapshot

+This directory is a credential-free export of the model state used on
+{top20['generated_at']}. All values are forward projections, not race results.
+
+## Files
+
+- `top20_per_stage.json` and `.csv`: all 20 predicted finishers for all 21 stages.
+- `objective_optimal_teams.json`: complete base and conditional 20-rider squads,
+  their 21 nine-rider lineups, captains, live prices, and score components.
+  The objective solver uses the populated Scorito market snapshot; the full
+  projection separately labels its internal price field as synthetic fallback.
+- `riders.json`: raw model inputs and capabilities for all {len(projection['riders'])} riders.
+- `rider_analytics.csv`: one query-friendly aggregate row per rider.
+- `stages.json` and `stage_analytics.csv`: complete route/profile metadata.
+- `stage_rider_analytics.csv`: all {len(stage_rider_rows)} rider-stage scores and evidence rows.
+- `projection_full.json`: the unabridged model snapshot behind those tables.
+- `market_*_raw.json`: raw public Scorito market rider, quality, and stage inputs.
+- `manifest.json`: source freshness, record counts, sizes, and SHA-256 checksums.
+
+The objective optimizer output excludes the local `personal_base` and
+`personal_conditional` comparison objects. Authenticated team selections,
+credentials, tokens, and cookies are never exported.
+
+Regenerate from the repository root with:
+
+```powershell
+.\\.venv\\Scripts\\python.exe scripts\\export_public_vuelta_data.py
+```
+""".replace("\n+", "\n")
    (PUBLIC_DIR / "README.md").write_text(readme, encoding="utf-8")

    record_counts = {
        "market_riders_raw.json": len(load_json("eventriderenriched.json")["Content"]),
        "market_rider_qualities_raw.json": len(load_json("eventrider_qualities.json")["Content"]),
        "market_stages_raw.json": len(load_json("marketroundstage.json")["Content"]),
        "riders.json": len(projection["riders"]),
        "rider_analytics.csv": len(rider_rows),
        "stages.json": len(projection["stages"]),
        "stage_analytics.csv": len(stage_rows),
        "stage_rider_analytics.csv": len(stage_rider_rows),
        "top20_per_stage.json": sum(len(row["top_20"]) for row in top20["stages"]),
        "top20_per_stage.csv": sum(len(row["top_20"]) for row in top20["stages"]),
        "objective_optimal_teams.json": 2,
    }
    manifest_files = []
    for path in sorted(PUBLIC_DIR.iterdir()):
        if path.name == "manifest.json" or not path.is_file():
            continue
        manifest_files.append({
            "name": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
            "records": record_counts.get(path.name),
        })
    manifest = {
        "schema_version": 1,
        "race": "Vuelta a Espana 2026",
        "exported_at": datetime.now(UTC).isoformat(),
        "status": top20["status"],
        "projection_price_status": projection["price_status"],
        "optimization_price_status": "live Scorito market snapshot",
        "projection_generated_at": projection["generated_at"],
        "top20_generated_at": top20["generated_at"],
        "optimization_generated_at": optimization["generated_at"],
        "market_snapshot_time": top20["sources"]["market_snapshot_time"],
        "source_input_hashes": top20["sources"]["input_hashes"],
        "counts": {
            "model_riders": len(projection["riders"]),
            "market_riders": record_counts["market_riders_raw.json"],
            "stages": len(projection["stages"]),
            "rider_stage_rows": len(stage_rider_rows),
            "top20_rows": record_counts["top20_per_stage.json"],
            "objective_optimal_teams": 2,
        },
        "privacy": {
            "personal_team_data_included": False,
            "authenticated_payloads_included": False,
            "credentials_included": False,
        },
        "files": manifest_files,
    }
    write_json(PUBLIC_DIR / "manifest.json", manifest)
    return PUBLIC_DIR


if __name__ == "__main__":
    output = build_public_snapshot()
    print(f"Exported public Vuelta snapshot to {output}")