"""
National Highways Road & Lane Closures API v2 (live, needs an API key),
plus the flat-JSON-mirror alternative (a pre-flattened snapshot, e.g. a
GitHub-hosted JSON file using the same field names already).

The API returns a nested DATEX II JSON payload; normalize_datex_response()
flattens it into the common record shape the rest of the project uses
(see matching.py's module docstring for the field list).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from matching import format_dt

NATIONAL_HIGHWAYS_DEFAULT_BASE_URL = "https://api.data.nationalhighways.co.uk"


def fetch_json(url: str, headers: dict | None = None) -> tuple[dict, dict]:
    """Returns (payload, response_headers) so callers can inspect pagination
    signals like a Link header, which json.load() alone would discard."""
    if url.startswith("file://"):
        return json.load(open(url[len("file://"):])), {}
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.load(resp)
            return payload, dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(
            f"HTTP {e.code} {e.reason} calling:\n  {url}\n\n"
            f"Response body from the API:\n{body}\n"
        ) from None


def find_next_page_url(payload: dict, response_headers: dict) -> str | None:
    """
    Best-effort detection of a "next page" link, since National Highways'
    docs don't publish the exact pagination mechanics for this API (their
    changelog only notes a "pagination refinement" patch in May 2025).
    Checks the two most common conventions: an RFC 5988 Link header, and a
    handful of common JSON field names for a continuation URL. If neither
    is present, this returns None and the caller assumes a single page.
    """
    link_header = response_headers.get("Link") or response_headers.get("link")
    if link_header:
        for part in link_header.split(","):
            segments = part.split(";")
            if len(segments) >= 2 and 'rel="next"' in segments[1]:
                return segments[0].strip().strip("<>")

    for key in ("nextLink", "@odata.nextLink", "next", "nextPageLink", "nextPage"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value

    return None


def normalize_datex_response(payload: dict) -> list[dict]:
    """
    Flatten a DATEX II JSON response from the National Highways API into
    a list of closure-location dicts using the same field names used
    throughout this project (road_name, direction, location_description,
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
    api_key = os.environ.get("NATIONAL_HIGHWAYS_API_KEY", "").strip()
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
    lookahead_days = site_cfg.get("lookahead_days", 29)
    if lookahead_days > 29:
        print(f"Note: lookahead_days ({lookahead_days}) is at or above the API's "
              f"30-day maximum window; clamping to 29 to leave a safety margin.")
        lookahead_days = 29

    now = datetime.now(timezone.utc)
    start = now.strftime("%Y-%m-%dT%H:%M:%S")
    end = (now + timedelta(days=lookahead_days)).strftime("%Y-%m-%dT%H:%M:%S")

    params = [f"startDateTime={start}", f"endDateTime={end}"]
    if closure_type:
        params.append(f"closureType={closure_type}")
    url = f"{base_url.rstrip('/')}/roads/v2.0/closures?{'&'.join(params)}"

    headers = {
        "Ocp-Apim-Subscription-Key": api_key,
        "X-Response-MediaType": "application/json",
        "X-Data-Format": "DATEXII",
        "Accept": "application/json",
        "User-Agent": "route-closures-build/1.0",
    }

    closures: list[dict] = []
    page_num = 1
    max_pages = 50  # safety cap so a pagination bug can't loop forever
    while url and page_num <= max_pages:
        print(f"Fetching {url} ...")
        payload, response_headers = fetch_json(url, headers=headers)
        page_closures = normalize_datex_response(payload)
        closures.extend(page_closures)
        print(f"  page {page_num}: {len(page_closures)} closure-location records")

        next_url = find_next_page_url(payload, response_headers)
        if next_url and next_url != url:
            url = next_url
            page_num += 1
        else:
            url = None

    if page_num > 1:
        print(f"Followed {page_num} page(s), {len(closures)} total closure-location records")
    else:
        print(f"Loaded {len(closures)} closure-location records from the live API "
              f"(single page -- no pagination link found in the response)")

    if not closures:
        print("Warning: 0 records parsed. If this is unexpected, the API's "
              "response shape may differ from what this script expects -- "
              "check the raw payload keys.")
    return closures


def fetch_from_flat_mirror(site_cfg: dict) -> tuple[list[dict], str]:
    url = site_cfg["data_url"]
    print(f"Fetching {url} ...")
    data, _headers = fetch_json(url, headers={"User-Agent": "route-closures-build/1.0"})
    closures = data["closures"]
    for c in closures:
        c.setdefault("record_id", c.get("idG") or c.get("id"))
    print(f"Loaded {len(closures)} closure records (feed updated {data.get('updated')})")
    feed_updated = format_dt(data.get("updated", ""))
    return closures, feed_updated
