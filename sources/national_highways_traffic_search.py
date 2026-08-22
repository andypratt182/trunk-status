"""
National Highways live traffic search API (unofficial, beta) --
https://nationalhighways.co.uk/trafficsearchapi/events?road=<ROAD>

This is NOT the documented Road & Lane Closures API v2 this project
already uses (see sources/national_highways.py) -- it's the internal
endpoint powering a NEW, still-in-beta page, "Check current incidents,
disruptions and delays"
(https://nationalhighways.co.uk/roads-and-travel/live-travel-updates/current-incidents-disruptions-and-delays/).
That page shows placeholder/Lorem-Ipsum content on initial load and only
fetches real data once you search -- this endpoint was found by
inspecting the browser's network requests while testing it live.

Kept as a fully separate, best-effort additional source, same pattern as
every other one in this project -- a parsing failure here never breaks
the main build. UNOFFICIAL AND UNDOCUMENTED: unlike the versioned Road &
Lane Closures API v2, this endpoint could change shape or be withdrawn
without notice at any time, since it isn't a published, supported
product on National Highways' developer portal. Treat it as more fragile
than every other source in this project, not less.

This fills a genuinely different gap from every other source here: it's
real-time traffic-FLOW data (congestion, abnormal traffic), distinct
from scheduled roadworks (the main API / XLSX report) and from the
hand-curated, highest-severity-only incidents on the Travel Alerts page.
A situation here can have a real machine-readable end time
("returnToNormal") when known -- most other incident-style sources in
this project have no end time at all.

England only (M6/M57/M58/M62 in this project's own route config).
Scotland's equivalent live-incidents data is a completely separate
source, sources/scotland_incidents.py, for a completely separate
country's road network -- no relation between the two.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from sources import status

TRAFFIC_SEARCH_BASE_URL = "https://nationalhighways.co.uk/trafficsearchapi/events"

# The API's single-letter direction codes -> this project's own
# data_direction convention (matching the documented DATEX API's
# camelCase style already used throughout routes.yaml).
DIRECTION_MAP = {
    "N": "northBound", "S": "southBound", "E": "eastBound", "W": "westBound",
}


def clean_iso(value) -> str:
    """Strip sub-second precision from an ISO datetime string (e.g.
    "2026-08-16T16:17:46.683" -> "2026-08-16T16:17:46"), for consistency
    with the plain-second ISO strings every other source in this project
    uses. Returns "" for None/empty/missing input."""
    if not value:
        return ""
    return re.sub(r'\.\d+$', '', value)


def fetch_page(road_name: str, page: int, page_size: int) -> dict:
    url = f"{TRAFFIC_SEARCH_BASE_URL}?page={page}&limit={page_size}&road={road_name}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "route-closures-build/1.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def normalize_record(record: dict, road_name: str) -> dict:
    """Flatten one raw API record into the common flat record shape used
    throughout this project."""
    direction_code = (record.get("direction") or "").upper()
    direction = DIRECTION_MAP.get(direction_code, record.get("direction") or "")

    end_datetime = clean_iso(record.get("returnToNormal")) or clean_iso(record.get("timeToClear"))

    return {
        "record_id": f"nh-traffic-{record.get('id', '')}",
        "road_name": road_name,
        "direction": direction,
        "location_description": record.get("location") or record.get("title") or "",
        "comment": record.get("delayText") or "",
        "start_datetime": clean_iso(record.get("createdDate")),
        "end_datetime": end_datetime,
        "validity_status": (record.get("status") or "active").lower(),
        "cause_type": record.get("reason") or "",
        "lanes_restricted": None,
        "lanes_operational": None,
        "lane_info": record.get("lanesClosedText") or "",
        "source_label": "National Highways Traffic Search (beta)",
    }


def fetch_from_national_highways_traffic_search(road_name: str, page_size: int = 50) -> list[dict]:
    """Fetch current live traffic situations for road_name from the
    unofficial trafficsearchapi endpoint (see module docstring). Returns
    closures in the standard flat record shape."""
    results = []
    page = 1
    max_pages = 20  # safety cap so a pagination bug can't loop forever
    label = f"NH Traffic Search (beta) -- {road_name}"

    while page <= max_pages:
        print(f"Fetching {TRAFFIC_SEARCH_BASE_URL}?page={page}&limit={page_size}&road={road_name} ...")
        try:
            payload = fetch_page(road_name, page, page_size)
        except urllib.error.HTTPError as e:
            print(f"Warning: HTTP {e.code} {e.reason} fetching National Highways traffic "
                  f"search data -- skipping this source.")
            status.record_status(label, ok=False, error=f"HTTP {e.code} {e.reason}")
            return []
        except Exception as e:  # noqa: BLE001 -- this source is best-effort, never fatal
            print(f"Warning: failed to fetch National Highways traffic search data ({e}) "
                  f"-- skipping this source.")
            status.record_status(label, ok=False, error=str(e))
            return []

        records = payload.get("data", [])
        print(f"  page {page}: {len(records)} record(s)")

        for record in records:
            # Defensive -- the API is already server-filtered by road via
            # the query parameter, but don't blindly trust that.
            if (record.get("road") or "").upper() != road_name.upper():
                continue
            results.append(normalize_record(record, road_name))

        pagination = payload.get("pagination", {})
        total_pages = pagination.get("totalPages", 1)
        if page >= total_pages:
            break
        page += 1

    print(f"  {len(results)} match {road_name}")
    status.record_status(label, ok=True, count=len(results))
    return results
