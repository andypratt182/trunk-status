#!/usr/bin/env python3
"""
Build a static site showing roadworks/closures for user-defined routes.

A "route" (e.g. Axis, Omega) has a northbound and southbound direction,
and each direction is a chain of one or more "legs" -- road sections
travelled in order (e.g. M6 J45-26, then M58, then M57 J6-4).

Data can come from either:
  - the National Highways Road & Lane Closures API v2 (live, needs an
    API key), which returns a nested DATEX II JSON payload -- this is
    flattened by normalize_datex_response() into the same simple record
    shape the rest of this script works with, or
  - a pre-flattened JSON mirror (e.g. a GitHub-hosted snapshot) using
    the same field names already.

Reads routes.yaml for route definitions and source config, fetches
closures, filters records per leg, and renders static HTML into
./_site, ready to be published as a GitHub Pages artifact.

Usage:
    python build.py

Environment:
    NATIONAL_HIGHWAYS_API_KEY   required when routes.yaml site.source
                                 is "national_highways_api"
"""
from __future__ import annotations

import json
import os
import re
import shutil
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
OUTPUT_DIR = ROOT / "_site"

NATIONAL_HIGHWAYS_DEFAULT_BASE_URL = "https://api.data.nationalhighways.co.uk"

# Junction numbers show up in free text like "between J35 and J36" or
# "M62 eastbound Jct 36 to Jct 37" -- cover both "J35" and "Jct 35" styles.
JUNCTION_RE = re.compile(r'J(?:ct)?\.?\s*(\d+)', re.IGNORECASE)

# road_name is sometimes blank; fall back to parsing it off the front of
# the free-text comment, e.g. "M62 eastbound Jct 36...".
ROAD_RE = re.compile(r'^(M\d+[A-Z]?|A\d+\(M\)|A\d+)\b')


def load_routes() -> dict:
    with open(ROOT / "routes.yaml") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------
# Fetching + normalizing closures from either data source
# ---------------------------------------------------------------------

def fetch_json(url: str, headers: dict | None = None) -> dict:
    if url.startswith("file://"):
        return json.load(open(url[len("file://"):]))
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def normalize_datex_response(payload: dict) -> list[dict]:
    """
    Flatten a DATEX II JSON response from the National Highways API into
    a list of closure-location dicts using the same field names used
    throughout this script (road_name, direction, location_description,
    comment, start_datetime, end_datetime, validity_status, cause_type,
    lanes_restricted, lanes_operational, record_id) -- one dict per
    physical location segment. Multiple segments can share a record_id
    (they belong to the same real-world closure); rows_for_leg() collapses
    those back down to one row per closure when rendering.
    """
    flat: list[dict] = []
    situations = payload.get("D2Payload", {}).get("situation", [])

    for situation in situations:
        for sr in situation.get("situationRecord", []):
            # The v2 API wraps the actual record under a type-specific key
            # (e.g. "sitRoadOrCarriagewayOrLaneManagement"); unwrap it if
            # present. If a future response is already unwrapped, sr itself
            # already has the fields we need, so fall back to sr.
            record = sr
            for wrapper_key in (
                "sitRoadOrCarriagewayOrLaneManagement",
                "sitRoadOrCarriagewayOrLaneManagementExtensionG",
            ):
                if wrapper_key in sr and isinstance(sr[wrapper_key], dict):
                    record = sr[wrapper_key]
                    break

            record_id = record.get("idG") or record.get("id") or ""
            validity = record.get("validity", {})
            time_spec = validity.get("validityTimeSpecification", {})
            cause = record.get("cause", {})
            comments = record.get("generalPublicComment", [])
            comment = comments[0].get("comment", "") if comments else ""

            shared = {
                "record_id": record_id,
                "validity_status": validity.get("validityStatus", "unknown"),
                "start_datetime": time_spec.get("overallStartTime", ""),
                "end_datetime": time_spec.get("overallEndTime", ""),
                "cause_type": cause.get("causeType", ""),
                "comment": comment,
            }

            groups = (
                record.get("locationReference", {})
                      .get("locLocationGroupByList", {})
                      .get("locationContainedInGroup", [])
            )

            if not groups:
                # No location breakdown available -- still emit one row
                # so the closure isn't silently dropped from the feed.
                flat.append({
                    **shared,
                    "road_name": "", "direction": "", "location_description": "",
                    "lanes_restricted": None, "lanes_operational": None,
                })
                continue

            for entry in groups:
                lin_loc = entry.get("locLinearLocation", {})
                supp = lin_loc.get("supplementaryPositionalDescription", {})
                single = entry.get("locSingleRoadLinearLocation", {})
                within_list = single.get("linearWithinLinearElement", [])
                within = within_list[0] if within_list else {}
                linear_elem = within.get("linearElement", {}).get("locLinearElementByCode", {})

                carriageways = supp.get("carriageway", [])
                lanes_restricted = lanes_operational = None
                if carriageways:
                    impact = carriageways[0].get("carriagewayExtensionG", {}).get("impactOnCarriageway", {})
                    lanes_restricted = impact.get("numberOfLanesRestricted")
                    lanes_operational = impact.get("numberOfOperationalLanes")

                flat.append({
                    **shared,
                    "road_name": linear_elem.get("roadName", ""),
                    "direction": within.get("directionOnLinearSection", ""),
                    "location_description": supp.get("locationDescription", ""),
                    "lanes_restricted": lanes_restricted,
                    "lanes_operational": lanes_operational,
                })

    return flat


def fetch_from_national_highways_api(site_cfg: dict) -> list[dict]:
    api_key = os.environ.get("NATIONAL_HIGHWAYS_API_KEY")
    if not api_key:
        raise SystemExit(
            "NATIONAL_HIGHWAYS_API_KEY is not set.\n"
            "  - In GitHub Actions: add it as a repo secret "
            "(Settings -> Secrets and variables -> Actions) and pass it to\n"
            "    the build step's `env:` block -- see the README.\n"
            "  - Locally: export NATIONAL_HIGHWAYS_API_KEY=your-key-here "
            "before running build.py."
        )

    base_url = site_cfg.get("api_base_url", NATIONAL_HIGHWAYS_DEFAULT_BASE_URL)
    closure_type = site_cfg.get("closure_type")  # "planned" / "unplanned" / None (both)
    lookahead_days = site_cfg.get("lookahead_days", 60)

    now = datetime.now(timezone.utc)
    start = now.strftime("%Y-%m-%dT%H:%M:%S")
    end = (now + timedelta(days=lookahead_days)).strftime("%Y-%m-%dT%H:%M:%S")

    params = [f"startDateTime={start}", f"endDateTime={end}"]
    if closure_type:
        params.append(f"closureType={closure_type}")
    url = f"{base_url.rstrip('/')}/roads/v2.0/closures?{'&'.join(params)}"

    print(f"Fetching {url} ...")
    payload = fetch_json(url, headers={
        "Ocp-Apim-Subscription-Key": api_key,
        "X-Response-MediaType": "application/json",
        "X-Data-Format": "DATEXII",
        "Accept": "application/json",
        "User-Agent": "route-closures-build/1.0",
    })
    closures = normalize_datex_response(payload)
    print(f"Loaded {len(closures)} closure-location records from the live API")
    if not closures:
        print("Warning: 0 records parsed. If this is unexpected, the API's "
              "response shape may differ from what this script expects -- "
              "check the raw payload keys (top-level: "
              f"{list(payload.keys())}).")
    return closures


def fetch_from_flat_mirror(site_cfg: dict) -> list[dict]:
    url = site_cfg["data_url"]
    print(f"Fetching {url} ...")
    data = fetch_json(url, headers={"User-Agent": "route-closures-build/1.0"})
    closures = data["closures"]
    for c in closures:
        c.setdefault("record_id", c.get("idG") or c.get("id"))
    print(f"Loaded {len(closures)} closure records (feed updated {data.get('updated')})")
    return closures


def load_closures(site_cfg: dict) -> list[dict]:
    source = site_cfg.get("source", "flat_json")
    if source == "national_highways_api":
        return fetch_from_national_highways_api(site_cfg)
    return fetch_from_flat_mirror(site_cfg)


# ---------------------------------------------------------------------
# Route matching (unchanged regardless of data source -- everything is
# normalized to the same flat record shape by this point)
# ---------------------------------------------------------------------

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

    # A single closure can have multiple matching location segments (e.g.
    # spans several junctions within one leg's range) -- collapse those
    # back down to one row per underlying closure.
    deduped: dict[object, dict] = {}
    for c in matches:
        key = c.get("record_id") or id(c)
        if key not in deduped:
            deduped[key] = c
    matches = list(deduped.values())
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

    closures = load_closures(site_cfg)

    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    feed_updated = generated_at  # the live API has no single "feed updated" timestamp

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
