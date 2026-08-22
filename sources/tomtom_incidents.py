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

One request **per bounding box** per build — NOT one request per road,
unlike sources/national_highways_traffic_search.py. TomTom enforces a
hard 10,000km² limit per bbox (confirmed live: a single box spanning
the whole Central-Scotland-to-Manchester corridor was rejected with
HTTP 400 "Area of 'bbox' parameter is larger than 10,000km2" during
initial testing) -- so DEFAULT_BBOXES below is a small set of narrower
regional boxes covering the corridor between them, each comfortably
under that limit with margin, not one box for everything. Each
configured road_name is filtered from the merged, deduped results of
every box, same caching-then-filtering pattern as
sources/travel_alerts.py and sources/scotland_incidents.py -- so
adding more roads costs no extra requests, only adding a *new region*
not already covered by an existing box would.

categoryFilter is a **comma-separated list** of category values (numeric
IDs or descriptive strings -- TomTom accepts either), NOT a bitmask --
an earlier version of this module wrongly treated it as an OR'd bitmask
integer based on an unreliable third-party doc mirror, which TomTom's
API rejected live with HTTP 400 "Unsupported categoryFilter parameter
value". The values below are taken directly from TomTom's own official
Incident Details page (docs.tomtom.com), not a third-party mirror:
`0`=Unknown, `1`=Accident, `2`=Fog, `3`=DangerousConditions, `4`=Rain,
`5`=Ice, `6`=Jam, `7`=LaneClosed, `8`=RoadClosed, `9`=RoadWorks,
`10`=Wind, `11`=Flooding, `14`=BrokenDownVehicle (12 and 13 aren't
listed in TomTom's own table -- not a gap in this module, that's simply
how TomTom's own enumeration skips them). If TomTom's docs change this
list in the future, that's the first thing to re-check against a live
400 error mentioning categoryFilter.

The default (`Accident,DangerousConditions,RoadClosed`) deliberately
EXCLUDES Jam/LaneClosed/RoadWorks, since those overlap with what the
existing closure sources already cover well and this source's whole
purpose is filling the accidents/incidents gap specifically -- not
becoming a second, redundant roadworks feed. If categoryFilter is
omitted entirely, TomTom itself defaults to including every category
(`0,1,2,3,4,5,6,7,8,9,10,11,14`), which is why this module always sends
an explicit value rather than relying on TomTom's own default. Override
via routes.yaml's category_filter if you want a different mix (see the
module's fetch_from_tomtom_incidents() signature) -- consult TomTom's
own Incident Details docs page for the current, authoritative list
before changing this.

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
- **DEFAULT_BBOXES are approximate, not verified against a live map --**
  only their AREA (staying under TomTom's confirmed 10,000km² limit,
  with real margin) has been checked, via plain trigonometric estimate,
  not their actual geographic coverage of every junction this project's
  routes.yaml tracks. Tighten/reshape them (or override per-call) once
  you've confirmed real incidents near your actual junctions are being
  returned -- same "verify against live data" caveat every scraper in
  this project already carries for its own reason. The three default
  boxes are meant to sit edge-to-edge/slightly overlapping along the
  M74->M6->M57/M58/M62 corridor; overlap between adjacent boxes is
  handled by deduping on each incident's own record_id, so an incident
  returned by two boxes near a shared edge is only counted once.
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
  field names against. The 10,000km² bbox limit above IS confirmed live
  (from a real build's error log), but the actual incident-shaped JSON
  response body still isn't. If a live run logs "0 incidents in bbox"
  or errors on a field access, check the real response shape against
  this module's assumptions before assuming the source itself is broken.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from sources import status

INCIDENT_DETAILS_URL = "https://api.tomtom.com/traffic/services/5/incidentDetails"

# Confirmed (from TomTom's own official Incident Details docs page, not
# a third-party mirror -- see module docstring) categoryFilter values.
# categoryFilter itself is a comma-separated LIST, not a bitmask.
CATEGORY_ACCIDENT = "Accident"
CATEGORY_DANGEROUS_CONDITIONS = "DangerousConditions"
CATEGORY_ROAD_CLOSED = "RoadClosed"
DEFAULT_CATEGORY_FILTER = ",".join([CATEGORY_ACCIDENT, CATEGORY_DANGEROUS_CONDITIONS, CATEGORY_ROAD_CLOSED])

# Three regional boxes covering the M74/M6/M57/M58/M62 corridor, each
# kept comfortably under TomTom's confirmed 10,000km2-per-request limit
# (a single box spanning the whole corridor measured roughly 5x that
# limit and was rejected live -- see module docstring). Adjacent boxes
# overlap slightly rather than leaving a gap; the small resulting
# double-fetch near each shared edge is deduped by record_id in
# fetch_from_tomtom_incidents(). Areas below are plain trigonometric
# estimates (degrees -> km at each box's latitude), not authoritative --
# see "Known limitations" above.
# minLon,minLat,maxLon,maxLat
DEFAULT_BBOXES = [
    "-4.1,54.95,-3.0,55.85",   # Scotland: M74 J8-22 corridor (~6,900km2)
    "-3.2,54.0,-2.4,55.0",     # Cumbria/Lancashire: M6 J45 down to ~J30 (~5,700km2)
    "-3.2,53.3,-2.1,54.05",    # Gtr Manchester/Merseyside: M6 J30-21, M57, M58, M62 (~6,000km2)
]

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


def fetch_page(bbox: str, category_filter: str, api_key: str) -> dict:
    params = {
        "key": api_key,
        "bbox": bbox,
        "fields": FIELDS,
        "language": "en-GB",
        "categoryFilter": category_filter,
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
# normal case, since DEFAULT_BBOXES already covers every road this project
# tracks) fetch each box just once, same reasoning as
# sources/travel_alerts.py's _page_cache.
_response_cache: dict[tuple[str, str], dict | None] = {}


def fetch_incidents_in_bbox(bbox: str, category_filter: str, api_key: str) -> dict | None:
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
    bbox: str | list[str] | None = None,
    category_filter: str = DEFAULT_CATEGORY_FILTER,
) -> list[dict]:
    """Fetch current TomTom traffic incidents across one or more bboxes
    (DEFAULT_BBOXES if bbox isn't given; a single string is also
    accepted for a one-off override) and filter to ones whose own
    roadNumbers list mentions road_name. Each bbox is fetched/cached
    independently (see fetch_incidents_in_bbox), and results are deduped
    by record_id across boxes in case adjacent boxes' overlap returns
    the same incident twice. Returns closures in the standard flat
    record shape -- see module docstring for the direction caveat and
    known limitations."""
    global _warned_missing_key
    label = f"TomTom Incidents -- {road_name}"
    if not api_key:
        if not _warned_missing_key:
            print("Warning: TOMTOM_API_KEY is not set -- skipping the TomTom "
                  "Traffic Incidents source for all configured roads. Get a free "
                  "key at https://developer.tomtom.com/ and set it as a GitHub "
                  "Actions secret (or export it locally), same as "
                  "NATIONAL_HIGHWAYS_API_KEY.")
            _warned_missing_key = True
        # Treated as a failure (red), not "0 results" (amber): this
        # means the source never even attempted to run this build, which
        # is worth flagging distinctly from "ran fine, found nothing".
        status.record_status(label, ok=False, error="TOMTOM_API_KEY not set")
        return []

    if bbox is None:
        bboxes = DEFAULT_BBOXES
    elif isinstance(bbox, str):
        bboxes = [bbox]
    else:
        bboxes = bbox

    seen_ids: set[str] = set()
    results: list[dict] = []
    failed_boxes: list[str] = []
    for one_bbox in bboxes:
        payload = fetch_incidents_in_bbox(one_bbox, category_filter, api_key)
        if payload is None:
            failed_boxes.append(one_bbox)
            continue  # this box's fetch failed -- already warned, keep going with the rest

        features = payload.get("incidents", [])
        print(f"  {len(features)} total incident(s) in bbox {one_bbox} (all roads)")

        for feature in features:
            record = normalize_incident(feature, road_name)
            if record is None:
                continue
            if record["record_id"] in seen_ids:
                continue  # returned by more than one box's overlap -- count once
            seen_ids.add(record["record_id"])
            results.append(record)

    print(f"  {len(results)} match {road_name} across {len(bboxes)} bbox(es)")
    if failed_boxes:
        # At least one box's fetch failed -- red, even though the boxes
        # that DID succeed may still have produced usable results above.
        # Erring toward "flag it" rather than silently accepting partial
        # coverage as if nothing were wrong.
        status.record_status(
            label, ok=False,
            error=f"{len(failed_boxes)}/{len(bboxes)} bbox fetch(es) failed",
        )
    else:
        status.record_status(label, ok=True, count=len(results))
    return results
