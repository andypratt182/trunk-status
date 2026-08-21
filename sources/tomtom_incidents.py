"""
TomTom Traffic Incidents API ("Incident Details" v5) --
https://api.tomtom.com/traffic/services/5/incidentDetails

Kept as a fully separate, best-effort additional source -- same pattern
as every other source in this project (see load_additional_closures() in
build.py): a fetch/parse failure here never breaks the main build, and a
missing TOMTOM_API_KEY is a warning + skip, not a hard failure, since
this is an *additional* source layered on top of whichever primary
source (site.source) is configured -- unlike national_highways.py's
NATIONAL_HIGHWAYS_API_KEY, which the primary source genuinely can't run
without.

Added specifically to cover LIVE accidents/incidents, a gap this project
already documented twice over: National Highways' own "unplanned"
closureType turned out to carry no genuinely incident-flavoured records
(see sources/national_highways.py's comments), and both Travel Alerts
and the beta traffic-search endpoint are known to be thin (Travel Alerts
by design -- only 2-3 nationwide entries at a time; the beta endpoint
because National Highways' own new incidents page is still in beta and
showing placeholder content as of Aug 2026). TomTom's feed is a
genuinely different, third-party data source (floating car / probe data
+ TomTom's own incident aggregation), not a re-scrape of anything
National Highways or Traffic Scotland already publish -- see the
"Known limitations" section below for how this could overlap with the
existing sources, since neither has a stable ID to dedupe against.

## Request shape

One request per build, with a single bounding box covering the whole
route network (Central Scotland down to NW England) -- NOT one request
per road, unlike sources/national_highways_traffic_search.py. TomTom's
bbox query returns every incident in the box in one response; each
configured road_name is then filtered from that single cached response,
same caching pattern as sources/travel_alerts.py and
sources/scotland_incidents.py (fetch the shared listing/area once,
filter per road afterwards) -- so adding more roads costs no extra
requests, same reasoning as those two.

categoryFilter is a bitmask limiting which incident categories TomTom
returns. Only a handful of category values were confirmed against
TomTom's own docs while building this (0=Unknown, 1=Accident, 2=Fog,
4=Dangerous conditions, 8=Rain, 16=Ice, 32=Jam, 64=Lane closed,
128=Road closed, 256=Road works) -- TomTom's docs mention further
categories exist beyond this but the exact bit values for those weren't
confirmed, so they're deliberately not included in DEFAULT_CATEGORY_FILTER.
The default (Accident | Dangerous conditions | Road closed = 133)
deliberately EXCLUDES Jam/Lane closed/Road works, since those overlap
with what the existing closure sources already cover well and this
source's whole purpose is filling the accidents/incidents gap
specifically -- not becoming a second, redundant roadworks feed.
Override via routes.yaml's category_filter if you want a different mix
(see the module's fetch_from_tomtom_incidents() signature) -- consult
TomTom's own Incident Details docs for the full, current bit values
before adding categories beyond the ones confirmed above.

## Known limitations

- **No reliable direction field.** Unlike every other source in this
  project, TomTom's incident properties don't expose a clean cardinal
  direction (N/S/E/W) for this endpoint. detect_direction() tries to
  find an explicit "northbound"/"southbound"/etc. word in the incident's
  own free text (from/to/event descriptions) first, but when that isn't
  present, this deliberately defaults to "Both directions" rather than
  guessing -- matching.py's BOTH_DIRECTIONS_VALUES then shows the
  incident on every leg for that road regardless of its own configured
  direction. This is a deliberate over-show, not a bug: showing a
  genuinely relevant nearby accident on both the northbound and
  southbound page is a far smaller cost than silently guessing the
  wrong direction (which could tell someone their direction is clear
  when it isn't) or silently dropping it entirely.
- **DEFAULT_BBOX is approximate, not verified against a live map.** It's
  a generous box intended to cover the M74/M6/M57/M58/M62 corridor this
  project's routes.yaml already uses, padded to be safe rather than
  tight. Tighten it (or override per-call) once you've confirmed real
  incidents are being returned/filtered correctly -- same "verify
  against live data" caveat every scraper in this project already
  carries for its own reason.
- **Possible overlap with existing sources, not yet resolved.** TomTom
  could report the same physical accident that National Highways'
  Travel Alerts or the beta traffic-search endpoint also reports, with
  no shared ID between them to deduplicate on -- same known limitation
  already documented for sources/scotland_incidents.py vs.
  sources/traffic_scotland.py (a real possible double-count, deferred
  rather than guessed at with an unverified dedup rule).
- **UNTESTED AGAINST A LIVE RESPONSE.** This was built from TomTom's
  published request/response documentation, not a real fetched payload
  (no network access while writing this) -- unlike some of this
  project's other sources, which had at least one real payload to check
  field names against. If a live run logs "0 incidents in bbox" or
  errors on a field access, check the real response shape against this
  module's assumptions before assuming the source itself is broken.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

INCIDENT_DETAILS_URL = "https://api.tomtom.com/traffic/services/5/incidentDetails"

# Confirmed subset of TomTom's categoryFilter bitmask (see module
# docstring -- further categories exist per TomTom's own docs but their
# bit values weren't confirmed while building this).
CATEGORY_ACCIDENT = 1
CATEGORY_DANGEROUS_CONDITIONS = 4
CATEGORY_ROAD_CLOSED = 128
DEFAULT_CATEGORY_FILTER = CATEGORY_ACCIDENT | CATEGORY_DANGEROUS_CONDITIONS | CATEGORY_ROAD_CLOSED  # 133

# Generous box covering the M74 (Glasgow) down through the M6/M57/M58/M62
# corridor (NW England) -- see module docstring's "Known limitations".
# minLon,minLat,maxLon,maxLat
DEFAULT_BBOX = "-4.6,53.15,-2.0,55.95"

FIELDS = (
    "{incidents{type,geometry{type,coordinates},"
    "properties{id,iconCategory,events{description,code},"
    "startTime,endTime,from,to,roadNumbers}}}"
)

_DIRECTION_RE = re.compile(r'\b(north|south|east|west)bound\b', re.IGNORECASE)


def clean_iso(value) -> str:
    """Strip sub-second precision (and a trailing "Z" UTC marker, which
    TomTom's timestamps use but sources/national_highways_traffic_search.py's
    original didn't need to handle) from an ISO datetime string, for
    consistency with the plain-second ISO strings every other source in
    this project uses, e.g. "2026-08-20T14:05:11.123Z" ->
    "2026-08-20T14:05:11". Returns "" for None/empty/missing input."""
    if not value:
        return ""
    if value.endswith("Z"):
        value = value[:-1]  # strip trailing UTC "Z" marker
    return re.sub(r'\.\d+$', '', value)


def detect_direction(text: str) -> str:
    """Best-effort direction from free text (from/to/event descriptions).
    Returns "Both directions" (matching.py's BOTH_DIRECTIONS_VALUES) when
    no explicit direction word is found -- see module docstring for why
    this is a deliberate over-show, not a guess."""
    m = _DIRECTION_RE.search(text or "")
    if not m:
        return "Both directions"
    return f"{m.group(1).capitalize()}bound"


def fetch_page(bbox: str, category_filter: int, api_key: str) -> dict:
    params = {
        "key": api_key,
        "bbox": bbox,
        "fields": FIELDS,
        "language": "en-GB",
        "categoryFilter": str(category_filter),
        "timeValidityFilter": "present",
    }
    url = f"{INCIDENT_DETAILS_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "route-closures-build/1.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def normalize_incident(feature: dict, target_road: str) -> dict | None:
    """Flatten one raw TomTom incident feature into the common flat
    record shape, or None if it doesn't mention target_road in its own
    roadNumbers list."""
    props = feature.get("properties", {}) or {}
    road_numbers = {(r or "").upper() for r in (props.get("roadNumbers") or [])}
    if target_road.upper() not in road_numbers:
        return None

    events = props.get("events") or []
    # dict.fromkeys() dedupes while preserving order -- TomTom sometimes
    # repeats the same event description across multiple event entries.
    descriptions = list(dict.fromkeys(
        e.get("description", "").strip() for e in events if e.get("description")
    ))
    cause_type = ", ".join(descriptions)

    from_text = (props.get("from") or "").strip()
    to_text = (props.get("to") or "").strip()
    location_bits = [b for b in (from_text, to_text) if b]
    location_description = (
        f"{target_road} " + " to ".join(location_bits) if location_bits else target_road
    )

    direction = detect_direction(f"{from_text} {to_text} {cause_type}")

    incident_id = props.get("id")
    if not incident_id:
        # No stable ID in this response -- fall back to a value derived
        # from the incident's own geometry + start time, stable across
        # builds as long as neither changes (matches this project's
        # general preference for a real fallback over silently dropping
        # a record that has no obvious record_id).
        coords = feature.get("geometry", {}).get("coordinates")
        incident_id = f"{coords}-{props.get('startTime', '')}"

    return {
        "record_id": f"tomtom-{incident_id}",
        "road_name": target_road,
        "direction": direction,
        "location_description": location_description,
        "comment": cause_type,
        "start_datetime": clean_iso(props.get("startTime")),
        "end_datetime": clean_iso(props.get("endTime")),
        "validity_status": "active",
        "cause_type": cause_type,
        "lanes_restricted": None,
        "lanes_operational": None,
        "source_label": "TomTom Traffic Incident",
    }


# Cached per (bbox, category_filter) for the lifetime of the process (one
# build run) -- so multiple road_name entries sharing the same bbox (the
# normal case, since DEFAULT_BBOX already covers every road this project
# tracks) fetch the area just once, same reasoning as
# sources/travel_alerts.py's _page_cache.
_response_cache: dict[tuple[str, int], dict | None] = {}


def fetch_incidents_in_bbox(bbox: str, category_filter: int, api_key: str) -> dict | None:
    cache_key = (bbox, category_filter)
    if cache_key in _response_cache:
        return _response_cache[cache_key]

    print(f"Fetching TomTom incidents for bbox={bbox} categoryFilter={category_filter} ...")
    try:
        payload = fetch_page(bbox, category_filter, api_key)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"Warning: HTTP {e.code} {e.reason} fetching TomTom incidents -- "
              f"skipping this source. Response body: {body[:500]}")
        payload = None
    except Exception as e:  # noqa: BLE001 -- this source is best-effort, never fatal
        print(f"Warning: failed to fetch TomTom incidents ({e}) -- skipping this source.")
        payload = None

    _response_cache[cache_key] = payload
    return payload


_warned_missing_key = False


def fetch_from_tomtom_incidents(
    road_name: str,
    api_key: str | None = None,
    bbox: str = DEFAULT_BBOX,
    category_filter: int = DEFAULT_CATEGORY_FILTER,
) -> list[dict]:
    """Fetch current TomTom traffic incidents in bbox (shared/cached
    across every road_name using the same bbox) and filter to ones whose
    own roadNumbers list mentions road_name. Returns closures in the
    standard flat record shape -- see module docstring for the direction
    caveat and known limitations."""
    global _warned_missing_key
    if not api_key:
        if not _warned_missing_key:
            print("Warning: TOMTOM_API_KEY is not set -- skipping the TomTom "
                  "Traffic Incidents source for all configured roads. Get a free "
                  "key at https://developer.tomtom.com/ and set it as a GitHub "
                  "Actions secret (or export it locally), same as "
                  "NATIONAL_HIGHWAYS_API_KEY.")
            _warned_missing_key = True
        return []

    payload = fetch_incidents_in_bbox(bbox, category_filter, api_key)
    if payload is None:
        return []

    features = payload.get("incidents", [])
    print(f"  {len(features)} total incident(s) in bbox (all roads)")

    results = []
    for feature in features:
        record = normalize_incident(feature, road_name)
        if record is not None:
            results.append(record)

    print(f"  {len(results)} match {road_name}")
    return results
