#!/usr/bin/env python3
"""
Build a static site showing roadworks/closures for user-defined routes.

A "route" (e.g. Axis, Omega) has a northbound and southbound direction,
and each direction is a chain of one or more "legs" -- road sections
travelled in order (e.g. M6 J45-26, then M58, then M57 J6-4).

This file is orchestration only: it loads routes.yaml, fetches closures
from whichever sources are configured, matches them to each route's legs,
and renders the Jinja templates. The actual data-source logic lives in
sources/ (one module per source, each exposing a fetch function that
returns closures in the common flat record shape), and the shared
matching/sorting logic lives in matching.py.

Data sources (site.source in routes.yaml):
  - "national_highways_api" -- the live National Highways Road & Lane
    Closures API v2 (needs an API key), or
  - "flat_json" -- a pre-flattened JSON mirror (e.g. a GitHub-hosted
    snapshot) using the same field names already.

Additional sources (site.additional_sources), layered on top of
whichever primary source is active:
  - "xlsx_advance_notice" -- National Highways' public 7-day closure
    report spreadsheet.
  - "traffic_scotland_scraper" -- a scraper for Traffic Scotland's
    roadworks pages, filtered to a specific road.

Usage:
    python build.py

Environment:
    NATIONAL_HIGHWAYS_API_KEY   required when routes.yaml site.source
                                 is "national_highways_api"
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from jinja2 import Environment, FileSystemLoader

from matching import build_direction
from sources.national_highways import fetch_from_flat_mirror, fetch_from_national_highways_api
from sources.traffic_scotland import fetch_from_traffic_scotland
from sources.xlsx_advance_notice import fetch_from_xlsx_advance_notice

ROOT = Path(__file__).parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
OUTPUT_DIR = ROOT / "_site"


def load_routes() -> dict:
    with open(ROOT / "routes.yaml") as f:
        return yaml.safe_load(f)


def load_closures(site_cfg: dict) -> tuple[list[dict], str]:
    """Returns (closures, feed_updated_label). feed_updated_label is "" when
    the source has no separate "last updated" timestamp of its own (e.g.
    the live API, which is fetched fresh on every build)."""
    source = site_cfg.get("source", "flat_json")
    if source == "national_highways_api":
        closures = fetch_from_national_highways_api(site_cfg)
        for c in closures:
            c.setdefault("source_label", "Live API")
        return closures, ""
    closures, feed_updated = fetch_from_flat_mirror(site_cfg)
    for c in closures:
        c.setdefault("source_label", "Feed")
    return closures, feed_updated


def load_additional_closures(site_cfg: dict) -> list[dict]:
    extra: list[dict] = []
    for source in site_cfg.get("additional_sources", []) or []:
        source_type = source.get("type")
        if source_type == "xlsx_advance_notice":
            try:
                extra.extend(fetch_from_xlsx_advance_notice(source["url"]))
            except Exception as e:  # noqa: BLE001 -- additional sources are best-effort
                print(f"Warning: additional source '{source_type}' failed ({e}) -- "
                      f"continuing without it.")
        elif source_type == "traffic_scotland_scraper":
            try:
                extra.extend(fetch_from_traffic_scotland(
                    road_name=source.get("road_name", "M74"),
                ))
            except Exception as e:  # noqa: BLE001 -- additional sources are best-effort
                print(f"Warning: additional source '{source_type}' failed ({e}) -- "
                      f"continuing without it.")
        else:
            print(f"Warning: unknown additional source type '{source_type}' -- skipping.")
    return extra


def main() -> None:
    config = load_routes()
    site_cfg = config["site"]

    closures, feed_updated = load_closures(site_cfg)
    closures.extend(load_additional_closures(site_cfg))
    print(f"Total closures across all sources: {len(closures)}")

    generated_at = datetime.now(ZoneInfo("Europe/London")).strftime("%d %b %Y, %H:%M %Z")
    if not feed_updated:
        feed_updated = generated_at

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True)
    if STATIC_DIR.exists():
        shutil.copytree(STATIC_DIR, OUTPUT_DIR / "static")

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    route_cards = []  # summary data for the index page

    for route in config["routes"]:
        directions_for_index = []

        for dir_key, dir_cfg in route["directions"].items():
            built = build_direction(closures, dir_cfg)
            page_id = f"{route['id']}-{dir_key}"

            html = env.get_template("route.html").render(
                site_title=site_cfg["title"],
                route_name=route["name"],
                direction_label=built["label"],
                leg_groups=built["leg_groups"],
                generated_at=generated_at,
                feed_updated=feed_updated,
            )
            (OUTPUT_DIR / f"{page_id}.html").write_text(html, encoding="utf-8")

            directions_for_index.append({
                "page": f"{page_id}.html",
                "page_id": page_id,
                "label": built["label"],
                "count": built["total"],
                "active_count": built["active_total"],
                "date_summary": [
                    {"start": r["start_iso"], "end": r["end_iso"], "status": r["status"]}
                    for lg in built["leg_groups"] for r in lg["rows"]
                ],
                "leg_summary": [
                    {
                        "road_name": lg["road_name"],
                        "badge_class": lg["badge_class"],
                        "junction_from": lg["junction_from"],
                        "junction_to": lg["junction_to"],
                    }
                    for lg in built["leg_groups"]
                ],
            })

        route_cards.append({
            "name": route["name"],
            "directions": directions_for_index,
        })

    index_html = env.get_template("index.html").render(
        site_title=site_cfg["title"],
        route_cards=route_cards,
        generated_at=generated_at,
        feed_updated=feed_updated,
    )
    (OUTPUT_DIR / "index.html").write_text(index_html, encoding="utf-8")

    total_pages = sum(len(r["directions"]) for r in route_cards) + 1
    print(f"Built {total_pages} pages into {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
