#!/usr/bin/env python3
"""
Build a static site showing roadworks/closures for user-defined routes.

A "route" (e.g. Axis, Omega) has a northbound and southbound direction,
and each direction is a chain of one or more "legs" -- road sections
travelled in order (e.g. M6 J45-26, then M58, then M57 J6-4).

Reads routes.yaml for route definitions, downloads the live closures
feed, filters records per leg, and renders static HTML into ./_site,
ready to be published as a GitHub Pages artifact.

Usage:
    python build.py
"""
from __future__ import annotations

import json
import re
import shutil
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
OUTPUT_DIR = ROOT / "_site"

# Junction numbers show up in free text like "between J35 and J36" or
# "M62 eastbound Jct 36 to Jct 37" -- cover both "J35" and "Jct 35" styles.
JUNCTION_RE = re.compile(r'J(?:ct)?\.?\s*(\d+)', re.IGNORECASE)

# road_name is blank on ~27% of records in this feed; fall back to parsing
# it off the front of the free-text comment, e.g. "M62 eastbound Jct 36...".
ROAD_RE = re.compile(r'^(M\d+[A-Z]?|A\d+\(M\)|A\d+)\b')


def load_routes() -> dict:
    with open(ROOT / "routes.yaml") as f:
        return yaml.safe_load(f)


def fetch_data(url: str) -> dict:
    if url.startswith("file://"):
        return json.load(open(url[len("file://"):]))
    req = urllib.request.Request(url, headers={"User-Agent": "route-closures-build/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def resolve_road_name(closure: dict) -> str:
    if closure.get("road_name"):
        return closure["road_name"]
    comment = closure.get("comment") or ""
    m = ROAD_RE.match(comment.strip())
    return m.group(1) if m else ""


def extract_junctions(closure: dict) -> list[int]:
    text = " ".join(filter(None, [
        closure.get("location_description", ""),
        closure.get("comment", ""),
    ]))
    return [int(n) for n in JUNCTION_RE.findall(text)]


def closure_matches_leg(closure: dict, road_name: str, data_direction: str,
                         j_from: int | None, j_to: int | None) -> bool:
    if resolve_road_name(closure).upper() != road_name.upper():
        return False
    if (closure.get("direction") or "").lower() != data_direction.lower():
        return False
    if j_from is None or j_to is None:
        return True  # "entire road" leg -- no junction filter
    junctions = extract_junctions(closure)
    if not junctions:
        return False
    lo, hi = sorted((j_from, j_to))
    return any(lo <= j <= hi for j in junctions)


def format_dt(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        return datetime.fromisoformat(iso_str).strftime("%d %b %Y, %H:%M")
    except ValueError:
        return iso_str


def road_badge_class(road_name: str) -> str:
    """UK signage convention: motorways are blue, primary A-roads green."""
    if road_name.upper().startswith("M") or "(M)" in road_name.upper():
        return "badge-motorway"
    return "badge-primary"


def rows_for_leg(closures: list[dict], road_name: str, data_direction: str,
                  j_from: int | None, j_to: int | None) -> list[dict]:
    matches = [
        c for c in closures
        if closure_matches_leg(c, road_name, data_direction, j_from, j_to)
    ]
    matches.sort(key=lambda c: c.get("start_datetime") or "")

    rows = []
    for c in matches:
        rows.append({
            "location": c.get("location_description") or c.get("comment") or "\u2014",
            "comment": c.get("comment") or "",
            "start": format_dt(c.get("start_datetime", "")),
            "end": format_dt(c.get("end_datetime", "")),
            "status": (c.get("validity_status") or "unknown").lower(),
            "lanes_restricted": c.get("lanes_restricted"),
            "lanes_operational": c.get("lanes_operational"),
            "cause": (c.get("cause_type") or "").replace("Work", " work").strip(),
        })
    return rows


def build_direction(closures: list[dict], dir_cfg: dict) -> dict:
    """Build the leg groups (each with its own rows) for one direction."""
    leg_groups = []
    total = 0
    active_total = 0
    for leg in dir_cfg["legs"]:
        rows = rows_for_leg(
            closures,
            leg["road_name"],
            leg["data_direction"],
            leg.get("junction_from"),
            leg.get("junction_to"),
        )
        leg_groups.append({
            "road_name": leg["road_name"],
            "badge_class": road_badge_class(leg["road_name"]),
            "junction_from": leg.get("junction_from"),
            "junction_to": leg.get("junction_to"),
            "rows": rows,
            "count": len(rows),
        })
        total += len(rows)
        active_total += sum(1 for r in rows if r["status"] == "active")
    return {
        "label": dir_cfg["label"],
        "leg_groups": leg_groups,
        "total": total,
        "active_total": active_total,
    }


def main() -> None:
    config = load_routes()
    site_cfg = config["site"]

    print(f"Fetching {site_cfg['data_url']} ...")
    data = fetch_data(site_cfg["data_url"])
    closures = data["closures"]
    print(f"Loaded {len(closures)} closure records (feed updated {data.get('updated')})")

    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    feed_updated = data.get("updated", "")

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
                "label": built["label"],
                "count": built["total"],
                "active_count": built["active_total"],
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
