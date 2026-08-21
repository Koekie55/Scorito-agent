"""On-disk structured memory for parsed ProCyclingStats stages."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STORE_PATH = REPO_ROOT / "data" / "pcs" / "stages.json"


def stage_key(stage: dict[str, Any]) -> str:
    """Build a stable identifier for a parsed stage."""

    for key in ("id", "stage_id", "key"):
        if stage.get(key):
            return str(stage[key])
    parts = [
        stage.get("race_slug") or stage.get("race") or "race",
        stage.get("year") or (str(stage.get("date"))[:4] if stage.get("date") else "year"),
        stage.get("stage_no") or stage.get("stage") or "stage",
    ]
    return "::".join(str(part).strip().lower().replace(" ", "-") for part in parts)


class StageStore:
    """Small JSON-backed stage memory stored under ``data/pcs`` by default."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_STORE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "stages": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, payload: dict[str, Any]) -> None:
        payload.setdefault("schema_version", 1)
        payload.setdefault("stages", [])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def all_stages(self) -> list[dict[str, Any]]:
        return list(self.load().get("stages", []))

    def clear(self) -> None:
        self.save({"schema_version": 1, "stages": []})

    def upsert_stage(self, stage: dict[str, Any]) -> dict[str, Any]:
        payload = self.load()
        stages = list(payload.get("stages", []))
        key = stage_key(stage)
        stored = dict(stage)
        stored["id"] = key
        for index, existing in enumerate(stages):
            if stage_key(existing) == key:
                stages[index] = stored
                break
        else:
            stages.append(stored)
        payload["stages"] = stages
        self.save(payload)
        return stored

    add_stage = upsert_stage

    def extend(self, stages: Iterable[dict[str, Any]]) -> None:
        for stage in stages:
            self.upsert_stage(stage)

    def get(self, key: str) -> dict[str, Any] | None:
        for stage in self.all_stages():
            if stage_key(stage) == key:
                return stage
        return None

    def query(
        self,
        *,
        profile_type: str | None = None,
        finish_type: str | None = None,
        race: str | None = None,
    ) -> list[dict[str, Any]]:
        results = self.all_stages()
        if profile_type:
            results = [stage for stage in results if str(stage.get("profile_type", "")).lower() == profile_type.lower()]
        if finish_type:
            results = [stage for stage in results if str(stage.get("finish_type", "")).lower() == finish_type.lower()]
        if race:
            needle = race.lower()
            results = [stage for stage in results if needle in str(stage.get("race", "")).lower()]
        return results
