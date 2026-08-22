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
from collections import Counter
from datetime import datetime, timedelta, timezone

from matching import format_dt
from sources import status

NATIONAL_HIGHWAYS_DEFAULT_BASE_URL = "https://api.data.nationalhighways.co.uk"


class PrimarySourceError(Exception):
    """Raised internally within this module for a genuine fetch/auth
    failure -- never escapes fetch_from_national_highways_api() or
    fetch_from_flat_mirror() themselves, both of which catch this (or
    let it signal a partial per-closureType failure -- see
    fetch_from_national_highways_api()'s docstring) and report it via
    sources/status.py instead, the same best-effort pattern every
    additional source in this project already follows. Previously this
    module used `raise SystemExit(...)` here instead, which crashed the
    whole build outright on any primary-source failure -- deliberately
    changed so a broken primary source no longer means "no site update
    at all", now that the additional sources can still provide useful
    information on their own (see build.py's main() for the prominent,
    page-level warning banner this triggers instead, since silently
    publishing a much sparser page without it could read as "the road
    is clear" when the real reason is just that the main data source
    broke -- a materially different, worth-flagging-loudly situation)."""


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
        raise PrimarySourceError(
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
    label = "Primary Source (National Highways Live API)"
    api_key = os.environ.get("NATIONAL_HIGHWAYS_API_KEY", "").strip()
    if not api_key:
        print("Warning: NATIONAL_HIGHWAYS_API_KEY is not set.\n"
              "  - In GitHub Actions: add it as a repo secret "
              "(Settings -> Secrets and variables -> Actions) and pass it to\n"
              "    the build step's `env:` block -- see the README.\n"
              "  - Locally: export NATIONAL_HIGHWAYS_API_KEY=your-key-here "
              "before running build.py.")
        status.record_status(label, ok=False, error="NATIONAL_HIGHWAYS_API_KEY not set")
        return []

    base_url = site_cfg.get("api_base_url", NATIONAL_HIGHWAYS_DEFAULT_BASE_URL)
    closure_type_cfg = site_cfg.get("closure_type")  # "planned" / "unplanned" / None (both)
    lookahead_days = site_cfg.get("lookahead_days", 29)
    if lookahead_days > 29:
        print(f"Note: lookahead_days ({lookahead_days}) is at or above the API's "
              f"30-day maximum window; clamping to 29 to leave a safety margin.")
        lookahead_days = 29

    now = datetime.now(timezone.utc)
    start = now.strftime("%Y-%m-%dT%H:%M:%S")
    end = (now + timedelta(days=lookahead_days)).strftime("%Y-%m-%dT%H:%M:%S")

    headers = {
        "Ocp-Apim-Subscription-Key": api_key,
        "X-Response-MediaType": "application/json",
        "X-Data-Format": "DATEXII",
        "Accept": "application/json",
        "User-Agent": "route-closures-build/1.0",
    }

    # IMPORTANT: omitting closureType was originally assumed to mean "both"
    # (per this API's apparent design), but that was never actually
    # verified against a live response -- and it turned out to be wrong.
    # A real run with closure_type left unset returned ONLY roadMaintenance/
    # constructionWork/authorityOperation causes across 3,201 records, with
    # zero incident-flavoured ones -- extremely unlikely if "both" were
    # genuinely being returned across a live network that size over a
    # 29-day window. So "both" is now handled explicitly, as two separate
    # requests, rather than trusting an unverified default.
    closure_types_to_fetch = [closure_type_cfg] if closure_type_cfg else ["planned", "unplanned"]

    closures: list[dict] = []
    failed_closure_types: list[str] = []
    for closure_type in closure_types_to_fetch:
        params = [f"startDateTime={start}", f"endDateTime={end}", f"closureType={closure_type}"]
        url = f"{base_url.rstrip('/')}/roads/v2.0/closures?{'&'.join(params)}"

        page_num = 1
        max_pages = 50  # safety cap so a pagination bug can't loop forever
        type_closures: list[dict] = []
        try:
            while url and page_num <= max_pages:
                print(f"Fetching {url} ...")
                payload, response_headers = fetch_json(url, headers=headers)
                page_closures = normalize_datex_response(payload)
                type_closures.extend(page_closures)
                print(f"  page {page_num}: {len(page_closures)} closure-location records")

                next_url = find_next_page_url(payload, response_headers)
                if next_url and next_url != url:
                    url = next_url
                    page_num += 1
                else:
                    url = None
        except Exception as e:  # noqa: BLE001 -- best-effort source now, same pattern as
                                 # every other source in this project; not just
                                 # PrimarySourceError, since a raw network timeout or
                                 # JSON decode error should be caught here too, not
                                 # left to crash the build the way the old
                                 # `raise SystemExit` behavior did.
            # A genuine fetch failure partway through THIS closureType --
            # keep whatever closures this closureType already gathered
            # across earlier pages (better than discarding good partial
            # data), but flag the closureType as failed so the overall
            # source status is honestly 'failed', not silently 'ok' with
            # an incomplete count. Other closureType requests still run
            # (see the enclosing for-loop) -- one bad request shouldn't
            # take out the other, still-working one.
            print(f"Warning: {e} -- closureType={closure_type} may be incomplete.")
            failed_closure_types.append(closure_type)

        if page_num > 1:
            print(f"  closureType={closure_type}: followed {page_num} page(s), "
                  f"{len(type_closures)} total closure-location records")
        else:
            print(f"  closureType={closure_type}: {len(type_closures)} closure-location "
                  f"records (single page -- no pagination link found)")

        # Tag each closure with which query actually returned it. This is
        # the reliable signal for "was this unplanned" -- cause_type text
        # turned out NOT to be: real unplanned closures have shown up
        # with a generic cause_type like "roadOrCarriagewayOrLaneManagement",
        # not anything that a keyword guess would flag as incident-like.
        for c in type_closures:
            c["closure_category"] = closure_type
        closures.extend(type_closures)

    print(f"Loaded {len(closures)} closure-location records from the live API "
          f"across {len(closure_types_to_fetch)} closureType request(s) "
          f"({', '.join(closure_types_to_fetch)})")

    if closures:
        # Diagnostic: real cause_type distribution, broken down by which
        # closureType query actually returned each record -- this is the
        # reliable signal for "is this unplanned", not cause_type text
        # (see the tagging comment above). Counts are per location-segment,
        # not per unique closure, since one closure can have multiple
        # location segments -- fine for a rough breakdown, not meant to be
        # an exact incident count.
        for category in closure_types_to_fetch:
            category_closures = [c for c in closures if c.get("closure_category") == category]
            if not category_closures:
                continue
            cause_counts = Counter(c.get("cause_type") or "(none)" for c in category_closures)
            print(f"  cause_type breakdown for closureType={category} "
                  f"({len(category_closures)} records):")
            for cause_type, count in cause_counts.most_common():
                print(f"    {count:5d}x  {cause_type!r}")

    if not closures:
        print("Warning: 0 records parsed. If this is unexpected, the API's "
              "response shape may differ from what this script expects -- "
              "check the raw payload keys.")

    if failed_closure_types:
        status.record_status(
            label, ok=False,
            error=f"{len(failed_closure_types)}/{len(closure_types_to_fetch)} "
                  f"closureType request(s) failed ({', '.join(failed_closure_types)})",
        )
    else:
        status.record_status(label, ok=True, count=len(closures))
    return closures


def fetch_from_flat_mirror(site_cfg: dict) -> tuple[list[dict], str]:
    label = "Primary Source (Flat JSON Mirror)"
    url = site_cfg["data_url"]
    print(f"Fetching {url} ...")
    try:
        data, _headers = fetch_json(url, headers={"User-Agent": "route-closures-build/1.0"})
    except Exception as e:  # noqa: BLE001 -- best-effort source now, same pattern as
                             # every other source in this project.
        print(f"Warning: {e} -- continuing with 0 closures from the primary source. "
              f"Other configured sources (if any) are still included.")
        status.record_status(label, ok=False, error=str(e).splitlines()[0])
        return [], ""

    closures = data["closures"]
    for c in closures:
        c.setdefault("record_id", c.get("idG") or c.get("id"))
    print(f"Loaded {len(closures)} closure records (feed updated {data.get('updated')})")
    feed_updated = format_dt(data.get("updated", ""))
    status.record_status(label, ok=True, count=len(closures))
    return closures, feed_updated
