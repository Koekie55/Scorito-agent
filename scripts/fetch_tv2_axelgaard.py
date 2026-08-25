"""Fetch and archive Emil Axelgaard's TV 2 stage previews for a race.

Runs daily so the next-stage preview is captured; TV 2 edits a preview after
first publication, so every distinct revision is kept for later audit.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from scorito_agent.common.http import fix_stdout, get  # noqa: E402
from scorito_agent.tv2_axelgaard import (  # noqa: E402
    archive_preview,
    discover_previews,
    is_usable_before_stage,
    parse_preview,
)

INDEX_URLS = (
    "https://sport.tv2.dk/profil/emil-axels",
    "https://sport.tv2.dk/cykling/vuelta-a-espana",
)
DATA_DIR = ROOT / "data" / "tv2_axelgaard"


def collect(
    index_urls: tuple[str, ...], race_slug: str, *, fetch=get
) -> list[dict[str, object]]:
    previews: dict[str, dict[str, object]] = {}
    errors: list[str] = []
    for url in index_urls:
        try:
            html = fetch(url, namespace=None, cache=False)
        except Exception as exc:  # noqa: BLE001 - one dead index must not stop the run
            errors.append(f"{url}: {exc}")
            continue
        for row in discover_previews(html):
            if row["race_slug"] == race_slug:
                previews[str(row["url"])] = row
    if not previews and errors:
        raise RuntimeError("no TV 2 index page could be read: " + "; ".join(errors))
    return sorted(previews.values(), key=lambda row: int(row["stage_number"]))


def main(argv: list[str] | None = None) -> int:
    fix_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--race-slug", default="vuelta-a-espana")
    parser.add_argument("--slug", default="vuelta2026", help="local storage slug")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args(argv)

    fetched_at = datetime.now(UTC).isoformat()
    discovered = collect(INDEX_URLS, args.race_slug)
    rows = []
    for row in discovered:
        url = str(row["url"])
        html = get(url, namespace=None, cache=False)
        preview = parse_preview(html, source_url=url)
        if preview.get("stage_number") != row["stage_number"]:
            print(f"  [skip] {url}: title stage does not match the link")
            continue
        result = archive_preview(
            args.data_dir, preview, race_slug=args.slug, fetched_at=fetched_at
        )
        rows.append(
            {
                "stage_number": preview["stage_number"],
                "stage_date": preview["stage_date"],
                "modified_at": preview["modified_at"],
                "usable_before_stage": is_usable_before_stage(preview),
                "rider_tiers": len(preview["rider_tiers"]),
                "breakaway_candidates": len(preview["breakaway_candidates"]),
                "scenarios": preview["scenario_probabilities_raw"],
                "revision_created": result["revision_created"],
                "source_url": preview["source_url"],
            }
        )
        state = "new revision" if result["revision_created"] else "unchanged"
        print(
            f"  stage {preview['stage_number']:>2} ({preview['stage_date']}): "
            f"{len(preview['rider_tiers'])} ranked riders, {state}"
        )

    index_path = args.data_dir / args.slug / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "tv2_axelgaard",
                "race_slug": args.race_slug,
                "fetched_at": fetched_at,
                "previews": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Archived {len(rows)} previews to {index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
