"""Load a Scorito market snapshot directory into normalized objects.

A snapshot dir (e.g. ``data/scorito/tdf2026/``) is produced by
``scripts/snapshot_market.py`` and contains the raw Scorito API JSON files.
This module turns that raw JSON into a :class:`Snapshot` of typed
:class:`Rider` / :class:`Stage` objects plus the per-stage points ground truth.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Rider, Snapshot, Stage

# Default location of the snapshot data, relative to the repo root.
DATA_ROOT = Path(__file__).resolve().parents[3] / "data" / "scorito"


def _content(obj: Any) -> Any:
    """Unwrap the Scorito ``{ResultCode, Content: ...}`` envelope."""
    if isinstance(obj, dict) and "Content" in obj:
        return obj["Content"]
    return obj


def _read(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return _content(json.load(f))


def _resolve_dir(dir_or_slug: str | Path) -> Path:
    p = Path(dir_or_slug)
    if p.is_dir():
        return p
    candidate = DATA_ROOT / str(dir_or_slug)
    if candidate.is_dir():
        return candidate
    raise FileNotFoundError(
        f"Snapshot directory not found: {dir_or_slug!r} "
        f"(looked at {p} and {candidate})"
    )


def _parse_riders(raw: list[dict]) -> list[Rider]:
    riders: list[Rider] = []
    for r in raw:
        qualities = {
            int(q["Type"]): int(q["Value"]) for q in (r.get("Qualities") or [])
        }
        first = (r.get("FirstName") or "").strip()
        last = (r.get("LastName") or "").strip()
        name = (f"{first} {last}").strip() or r.get("NameShort") or str(r.get("RiderId"))
        riders.append(
            Rider(
                rider_id=int(r["RiderId"]),
                event_rider_id=int(r.get("EventRiderId", 0)),
                name=name,
                team_id=int(r.get("TeamId", 0)),
                price=int(r.get("Price", 0)),
                role=int(r.get("Type", 0)),
                nationality=r.get("NationalityCode", "") or "",
                age=r.get("Age"),
                qualities=qualities,
            )
        )
    return riders


def _parse_stages(raw: list[dict]) -> list[Stage]:
    stages = [
        Stage(
            market_round_id=int(s["MarketRoundId"]),
            stage_id=int(s["StageId"]),
            order=int(s["StageOrder"]),
            stage_type=int(s["StageType"]),
            terrain_type=int(s["TerrainType"]),
        )
        for s in raw
    ]
    stages.sort(key=lambda s: s.order)
    return stages


def _parse_stage_points(raw: list[dict]) -> dict[tuple[int, int], float]:
    """(market_round_id, rider_id) -> summed points over all PointsTypes."""
    out: dict[tuple[int, int], float] = {}
    for elem in raw:
        mr = int(elem["MarketRoundId"])
        rpc = elem.get("RiderPointsCollection") or {}
        inner = rpc.get("RiderPointsCollection") or []
        for entry in inner:
            rid = int(entry["RiderId"])
            total = sum(float(pc.get("Points", 0)) for pc in entry.get("PointsCollection") or [])
            out[(mr, rid)] = out.get((mr, rid), 0.0) + total
    return out


def _parse_market_totals(raw: list[dict]) -> dict[int, float]:
    return {int(e["RiderId"]): float(e.get("Points", 0)) for e in raw}


def _parse_classification_points(raw: dict) -> dict[int, float]:
    """rider_id -> summed end-of-race classification/jersey bonus points.

    ``points_market.json`` holds market-wide bonuses awarded once at the end of
    the race (final GC podium, points/KOM/youth jerseys, etc.), keyed by
    ``PointsType`` (e.g. 101 = GC winner). These are separate from the per-stage
    ``points_totalpoints.json`` and together reconcile to the leaderboard.
    """
    out: dict[int, float] = {}
    for entry in raw.get("RiderPointsCollection") or []:
        rid = int(entry["RiderId"])
        total = sum(
            float(pc.get("Points", 0)) for pc in entry.get("PointsCollection") or []
        )
        out[rid] = out.get(rid, 0.0) + total
    return out


def load_snapshot(dir_or_slug: str | Path) -> Snapshot:
    """Load a Scorito snapshot directory (or a slug under ``data/scorito/``)."""
    d = _resolve_dir(dir_or_slug)

    riders = _parse_riders(_read(d / "eventriderenriched.json"))
    stages = _parse_stages(_read(d / "marketroundstage.json"))

    market = _read(d / "marketenriched.json")
    if isinstance(market, list):  # some envelopes wrap the object in a list
        market = market[0] if market else {}
    budget = int(market.get("Budget", 0))
    captain_factor = int(market.get("CaptainFactor", 2))

    stage_points: dict[tuple[int, int], float] = {}
    tp_path = d / "points_totalpoints.json"
    if tp_path.exists():
        stage_points = _parse_stage_points(_read(tp_path))

    market_totals: dict[int, float] = {}
    mp_path = d / "marketpoints.json"
    if mp_path.exists():
        market_totals = _parse_market_totals(_read(mp_path))

    classification_points: dict[int, float] = {}
    pm_path = d / "points_market.json"
    if pm_path.exists():
        classification_points = _parse_classification_points(_read(pm_path))

    # market_id / slug from the directory name where possible.
    slug = d.name
    market_id = 0
    reg = DATA_ROOT / "markets_registry.json"
    if reg.exists():
        try:
            markets = json.loads(reg.read_text(encoding="utf-8")).get("markets", {})
            for mid, m in markets.items():
                if m.get("slug") == slug:
                    market_id = int(mid)
                    break
        except (ValueError, KeyError):  # pragma: no cover - registry is best-effort
            pass

    return Snapshot(
        market_id=market_id,
        slug=slug,
        budget=budget,
        captain_factor=captain_factor,
        riders=riders,
        stages=stages,
        stage_points=stage_points,
        market_totals=market_totals,
        classification_points=classification_points,
    )
