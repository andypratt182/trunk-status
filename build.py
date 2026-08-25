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

import hashlib
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from jinja2 import Environment, FileSystemLoader

import investigation_log
from matching import build_direction
from sources import status
from sources.national_highways import fetch_from_flat_mirror, fetch_from_national_highways_api
from sources.national_highways_traffic_search import fetch_from_national_highways_traffic_search
from sources.scotland_incidents import fetch_from_scotland_incidents
from sources.traffic_scotland import fetch_from_traffic_scotland
from sources.travel_alerts import fetch_from_travel_alerts
from sources.xlsx_advance_notice import fetch_from_xlsx_advance_notice

ROOT = Path(__file__).parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
OUTPUT_DIR = ROOT / "_site"


def load_routes() -> dict:
    with open(ROOT / "routes.yaml") as f:
        return yaml.safe_load(f)


def content_hash(path: Path) -> str:
    """Short hash of a static file's own content, used as a cache-busting
    query string on its <link>/<script> tag. The HTML is guaranteed fresh
    on every build (it has a new "Page built" timestamp baked in every
    time), but style.css/day-filter.js are referenced by the exact same
    URL on every build -- so a browser or CDN can keep serving a stale
    cached copy of THOSE files indefinitely, even once the HTML on the
    page is visibly fresh. Since the query string only changes when the
    file's actual content changes, this doesn't force a refetch on every
    rebuild -- only when something in the file genuinely changed."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


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
                extra.extend(fetch_from_xlsx_advance_notice(
                    report_page_url=source.get("report_page_url"),
                    fallback_xlsx_url=source.get("fallback_xlsx_url"),
                    url=source.get("url"),  # deprecated -- see xlsx_advance_notice.py docstring
                ))
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
        elif source_type == "travel_alerts_scraper":
            try:
                extra.extend(fetch_from_travel_alerts(
                    road_name=source["road_name"],
                ))
            except Exception as e:  # noqa: BLE001 -- additional sources are best-effort
                print(f"Warning: additional source '{source_type}' failed ({e}) -- "
                      f"continuing without it.")
        elif source_type == "scotland_incidents_scraper":
            try:
                extra.extend(fetch_from_scotland_incidents(
                    road_name=source.get("road_name", "M74"),
                ))
            except Exception as e:  # noqa: BLE001 -- additional sources are best-effort
                print(f"Warning: additional source '{source_type}' failed ({e}) -- "
                      f"continuing without it.")
        elif source_type == "national_highways_traffic_search":
            try:
                extra.extend(fetch_from_national_highways_traffic_search(
                    road_name=source["road_name"],
                ))
            except Exception as e:  # noqa: BLE001 -- additional sources are best-effort
                print(f"Warning: additional source '{source_type}' failed ({e}) -- "
                      f"continuing without it.")
        else:
            print(f"Warning: unknown additional source type '{source_type}' -- skipping.")
    return extra


def main() -> None:
    status.reset()  # last-build-only status registry (see sources/status.py) --
                     # cleared so re-running main() in the same process (e.g. tests)
                     # doesn't accumulate stale entries from a previous run.
    config = load_routes()
    site_cfg = config["site"]

    closures, feed_updated = load_closures(site_cfg)
    closures.extend(load_additional_closures(site_cfg))
    print(f"Total closures across all sources: {len(closures)}")

    # TEMPORARY investigation aid -- see investigation_log.py's module
    # docstring for what this is for and when to remove it.
    investigation_log.log_new_entries(closures)

    # Whether the PRIMARY source (site.source) failed this build -- checked
    # by label prefix rather than threaded through as a separate return
    # value, since sources/national_highways.py already records this via
    # the same shared status registry every other source uses (see its
    # module docstring). Triggers a prominent, page-level warning banner
    # on EVERY page (not just the collapsed status panel on index.html,
    # and not just a log line) -- unlike an additional source going quiet,
    # a failed PRIMARY source means the page is very likely under-reporting
    # real closures, which is materially different from "no disruptions
    # right now" and worth surfacing loudly rather than silently. See
    # sources/national_highways.py's PrimarySourceError docstring for the
    # full reasoning behind not hard-failing the whole build over this
    # anymore.
    primary_source_failed = any(
        s["label"].startswith("Primary Source") and s["state"] == "failed"
        for s in status.get_statuses()
    )

    generated_at = datetime.now(ZoneInfo("Europe/London")).strftime("%d %b %Y, %H:%M %Z")
    if not feed_updated:
        feed_updated = generated_at

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True)
    if STATIC_DIR.exists():
        shutil.copytree(STATIC_DIR, OUTPUT_DIR / "static")

    # Cache-busting query strings for style.css/day-filter.js -- see
    # content_hash()'s docstring for why this is needed even though the
    # HTML itself is always fresh.
    style_hash = content_hash(STATIC_DIR / "style.css") if (STATIC_DIR / "style.css").exists() else ""
    script_hash = content_hash(STATIC_DIR / "day-filter.js") if (STATIC_DIR / "day-filter.js").exists() else ""

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
                style_hash=style_hash,
                script_hash=script_hash,
                primary_source_failed=primary_source_failed,
            )
            (OUTPUT_DIR / f"{page_id}.html").write_text(html, encoding="utf-8")

            directions_for_index.append({
                "page": f"{page_id}.html",
                "page_id": page_id,
                "label": built["label"],
                "count": built["total"],
                "active_count": built["active_total"],
                "date_summary": [
                    {"start": r["start_iso"], "end": r["end_iso"], "status": r["status"],
                     "source": r["source_label"]}
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
        style_hash=style_hash,
        script_hash=script_hash,
        source_statuses=status.get_statuses(),
        primary_source_failed=primary_source_failed,
    )
    (OUTPUT_DIR / "index.html").write_text(index_html, encoding="utf-8")

    total_pages = sum(len(r["directions"]) for r in route_cards) + 1
    print(f"Built {total_pages} pages into {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
