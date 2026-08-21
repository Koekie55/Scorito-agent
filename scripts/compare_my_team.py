"""Diff your real drafted 20 vs the enrolled-aware optimal-20 blueprint.

Part 1 of the "how to improve my team" analysis. Reads the personal squad
saved by ``fetch_my_team.py`` (``data/scorito/<slug>/personal/shortlist.json``)
and compares it, rider-by-rider, against the enrolled-aware optimal 20 stored
in ``data/scorito/<slug>/stage_study.json`` under ``enrolled_aware.squad_ids``
(the 7675-ceiling blueprint — the 20 that maximise the sum of the per-stage
best 9 + captain, not the sum of season totals).

It prints three buckets:

  * KEPT               — riders in both your squad and the blueprint.
  * MISSING (add)      — blueprint riders you did NOT draft (your biggest wins).
  * REDUNDANT (drop)   — riders you drafted that the blueprint would drop.

...plus a budget delta and a hindsight season-total delta, so you can see what
swapping toward the blueprint would have cost and earned.

Degrades gracefully: if ``personal/shortlist.json`` is absent it tells you to
run ``fetch_my_team.py`` first (which needs a browser token pasted into .env),
and if it can't recognise the shortlist JSON shape it dumps the structure so
the parser can be adjusted against the first real payload.

Usage:
    python scripts/compare_my_team.py                 # defaults to tdf2026
    python scripts/compare_my_team.py tdf2026
    python scripts/compare_my_team.py vuelta2026      # once you've drafted the Vuelta
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from scorito_agent.scorito.loader import load_snapshot  # noqa: E402

# Rider-id keys we probe for, most-specific first. Scorito's own snapshot uses
# ``RiderId`` / ``EventRiderId``; the personal shortlist shape is unconfirmed
# until the first authenticated fetch, so accept the common casings too.
ID_KEYS = ("RiderId", "riderId", "EventRiderId", "eventRiderId", "Id", "id")
# List-valued wrappers the drafted riders might be nested under.
LIST_KEYS = ("Content", "Riders", "Shortlist", "Items", "Data", "Result")


def _iter_candidate_dicts(obj: object):
    """Yield every dict anywhere in a nested JSON structure."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_candidate_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_candidate_dicts(v)


def _extract_rider_ids(payload: object) -> list[int]:
    """Best-effort pull of rider ids from an unknown-shape shortlist payload.

    Strategy: unwrap the ``{ResultCode, Content}`` envelope and any known list
    wrapper, then walk every nested dict and collect the first id-like key we
    find on each. RiderId is preferred over EventRiderId so the ids line up
    with ``Snapshot.rider(...)`` / the blueprint ``squad_ids``.
    """
    # Unwrap the standard Scorito envelope if present.
    if isinstance(payload, dict) and "Content" in payload:
        payload = payload["Content"]
    # Unwrap a known list wrapper (e.g. {"Riders": [...]}).
    if isinstance(payload, dict):
        for key in LIST_KEYS:
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break

    # teamselection returns a BARE LIST OF RIDER IDS (ints), not dicts.
    if isinstance(payload, list) and payload and all(
        isinstance(v, int) for v in payload
    ):
        seen_int: set[int] = set()
        out_ids: list[int] = []
        for rid in payload:
            if rid not in seen_int:
                seen_int.add(rid)
                out_ids.append(rid)
        return out_ids

    seen: set[int] = set()
    ids: list[int] = []
    for d in _iter_candidate_dicts(payload):
        for key in ID_KEYS:
            if key in d and isinstance(d[key], int):
                rid = d[key]
                if rid not in seen:
                    seen.add(rid)
                    ids.append(rid)
                break
    return ids


def _rider_line(snap, rid: int) -> str:
    r = snap.rider(rid)
    if r is None:
        return f"  - id {rid} (not in snapshot)"
    return (
        f"  - {r.name:<26} {r.price_m:>4.1f}M  {r.role_label:<9} "
        f"season_total={snap.season_total(rid):>6.0f}"
    )


def main(argv: list[str]) -> int:
    slug = argv[1] if len(argv) > 1 else "tdf2026"

    snap = load_snapshot(slug)

    study_path = REPO / "data" / "scorito" / slug / "stage_study.json"
    if not study_path.exists():
        print(f"[!] No blueprint at {study_path}.")
        print("    Run:  python scripts/stage_study.py " + slug)
        return 2
    study = json.loads(study_path.read_text(encoding="utf-8"))
    blueprint_ids = list(study.get("enrolled_aware", {}).get("squad_ids", []))
    if not blueprint_ids:
        print(f"[!] Blueprint {study_path} has no enrolled_aware.squad_ids.")
        return 2

    personal_dir = REPO / "data" / "scorito" / slug / "personal"
    # The real drafted-20 lives in teamselection.json (a bare list of RiderIds).
    # Fall back to shortlist.json only if teamselection was never fetched.
    personal_path = personal_dir / "teamselection.json"
    if not personal_path.exists():
        personal_path = personal_dir / "shortlist.json"
    if not personal_path.exists():
        print(f"[!] Your drafted squad is not saved yet ({personal_dir}\\teamselection.json missing).")
        print("    Fetch it first (needs a browser token in .env):")
        print("        python scripts/fetch_my_team.py "
              + ("309 tdf2026" if slug == "tdf2026" else f"<market_id> {slug}"))
        print("    Then re-run this script.")
        return 3

    try:
        payload = json.loads(personal_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[!] Could not parse {personal_path}: {e}")
        return 4

    my_ids = _extract_rider_ids(payload)
    if not my_ids:
        print(f"[!] Recognised no rider ids in {personal_path}.")
        print("    Tried keys:", ", ".join(ID_KEYS))
        print("    Raw top-level structure so the parser can be adjusted:")
        if isinstance(payload, dict):
            for k, v in payload.items():
                print(f"      {k}: {type(v).__name__}"
                      + (f" (len {len(v)})" if isinstance(v, (list, dict)) else ""))
        else:
            print(f"      <{type(payload).__name__}>")
        return 4

    my_set = set(my_ids)
    bp_set = set(blueprint_ids)

    kept = [r for r in blueprint_ids if r in my_set]
    missing = [r for r in blueprint_ids if r not in my_set]  # blueprint says add
    redundant = [r for r in my_ids if r not in bp_set]       # blueprint says drop

    def _budget(ids: list[int]) -> float:
        return sum((snap.rider(i).price for i in ids if snap.rider(i))) / 1_000_000

    def _season(ids: list[int]) -> float:
        return sum(snap.season_total(i) for i in ids)

    print(f"=== compare_my_team: {slug} ===")
    print(f"Your drafted squad: {len(my_ids)} riders   "
          f"budget {_budget(my_ids):.2f}M   season_total {_season(my_ids):.0f}")
    print(f"Blueprint (enrolled-aware optimal 20): {len(blueprint_ids)} riders   "
          f"budget {_budget(blueprint_ids):.2f}M   season_total {_season(blueprint_ids):.0f}")
    ea = study.get("enrolled_aware", {})
    if "enrolled_total" in ea:
        print(f"Blueprint enrolled-aware ceiling (best 9+captain/stage summed): "
              f"{ea['enrolled_total']:.0f}")
    print()

    print(f"KEPT ({len(kept)}) — already in your squad and the blueprint:")
    for rid in kept:
        print(_rider_line(snap, rid))
    print()

    print(f"MISSING ({len(missing)}) — blueprint riders you should have drafted:")
    for rid in missing:
        print(_rider_line(snap, rid))
    print()

    print(f"REDUNDANT ({len(redundant)}) — riders the blueprint would drop:")
    for rid in redundant:
        print(_rider_line(snap, rid))
    print()

    dbudget = _budget(blueprint_ids) - _budget(my_ids)
    dseason = _season(blueprint_ids) - _season(my_ids)
    print("--- if you moved to the blueprint ---")
    print(f"  budget change:        {dbudget:+.2f}M "
          f"(cap {snap.budget_m:.2f}M)")
    print(f"  season-total change:  {dseason:+.0f} pts (hindsight, full leaderboard)")
    print(f"  overlap:              {len(kept)}/20 riders shared")

    out = {
        "slug": slug,
        "my_ids": my_ids,
        "blueprint_ids": blueprint_ids,
        "kept": kept,
        "missing_add": missing,
        "redundant_drop": redundant,
        "my_budget_m": round(_budget(my_ids), 3),
        "blueprint_budget_m": round(_budget(blueprint_ids), 3),
        "my_season_total": round(_season(my_ids), 1),
        "blueprint_season_total": round(_season(blueprint_ids), 1),
        "budget_delta_m": round(dbudget, 3),
        "season_total_delta": round(dseason, 1),
    }
    out_path = REPO / "data" / "scorito" / slug / "compare_my_team.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
