"""
Route/leg matching logic -- the one thing every data source shares.

Once a source has normalized its data into the common flat record shape
(road_name, direction, location_description, comment, start_datetime,
end_datetime, validity_status, cause_type, lanes_restricted,
lanes_operational, source_label, record_id), everything here operates on
it identically regardless of where it came from.
"""
from __future__ import annotations

import re
from datetime import datetime

# Junction numbers show up in free text like "between J35 and J36", "M62
# eastbound Jct 36 to Jct 37", or spelled out as "Junction 40" / "Junctions
# 40 to 39" (seen in the National Highways advance-notice XLSX) -- the
# second number in a spelled-out range often has no "J"/"Junction" prefix
# of its own, so an optional trailing "to/and/-<number>" is captured too.
JUNCTION_RE = re.compile(
    r'\b(?:Junction|Junc|Jct|J)s?\.?\s*(\d+)(?:\s*(?:to|and|-|\u2013)\s*(\d+))?',
    re.IGNORECASE,
)

# road_name is sometimes blank; fall back to parsing it off the front of
# the free-text comment, e.g. "M62 eastbound Jct 36...".
ROAD_RE = re.compile(r'^(M\d+[A-Z]?|A\d+\(M\)|A\d+)\b')

# Matches a lowercase/digit immediately followed by an uppercase letter --
# a true camelCase word boundary (e.g. the "e"->"N" in "advanceNotice").
# Deliberately does NOT match an uppercase letter that already has a space
# (or any other non-alnum) before it, so already-human text is untouched.
_CAMEL_CASE_BOUNDARY_RE = re.compile(r'(?<=[a-z0-9])(?=[A-Z])')


def humanize_cause(text: str) -> str:
    """cause_type values come in two shapes depending on the source:
    camelCase machine identifiers (National Highways' DATEX causeType,
    e.g. "roadMaintenance"; this project's own XLSX-source placeholder,
    "advanceNoticeFullClosure") and already-human text (Traffic
    Scotland's "Works:" field, e.g. "Barrier Repair, Filter Drain").
    Splitting the first kind into words and leaving the second kind's
    casing alone avoids two failure modes: showing a machine identifier
    as one long run-together word (a bare `capitalize()` turns
    "advanceNoticeFullClosure" into "Advancenoticefullclosure"), and
    forcibly lowercasing already-well-cased human text."""
    if not text:
        return ""
    spaced = _CAMEL_CASE_BOUNDARY_RE.sub(" ", text)
    if spaced == text:
        return text  # no camelCase boundary found -- already human-readable
    return spaced[0].upper() + spaced[1:].lower()


def resolve_road_name(closure: dict) -> str:
    if closure.get("road_name"):
        return closure["road_name"]
    comment = closure.get("comment") or ""
    m = ROAD_RE.match(comment.strip())
    return m.group(1) if m else ""


def _junctions_in_text(text: str) -> list[int]:
    nums = []
    for m in JUNCTION_RE.finditer(text):
        nums.append(int(m.group(1)))
        if m.group(2):
            nums.append(int(m.group(2)))
    return nums


def extract_junctions(closure: dict) -> list[int]:
    """
    Junction numbers for route matching/sorting come from the structured
    location text only, not the free-text comment -- the comment field can
    contain diversion route instructions (e.g. "diversion via A50, rejoin
    at J24") that mention junctions with no relation to where the closure
    actually is, which would otherwise contaminate matching and sort order.
    Only fall back to the comment when location text has no junction info.
    """
    junctions = _junctions_in_text(closure.get("location_description") or "")
    if junctions:
        return junctions
    return _junctions_in_text(closure.get("comment") or "")


# Direction values meaning "affects every direction of travel" -- a
# closure/alert reporting one of these should match a leg regardless of
# that leg's own configured data_direction, rather than requiring an
# exact (and therefore never-matching) string comparison. Real case:
# National Highways' Travel Alerts frequently report "Both directions"
# for major incidents (2 of 3 alerts checked while building this were),
# which would otherwise match neither a route's northbound nor
# southbound leg and silently vanish from both.
BOTH_DIRECTIONS_VALUES = {"both directions", "both", "both ways"}


def closure_matches_leg(closure: dict, road_name: str, data_direction: str,
                         j_from: int | None, j_to: int | None) -> bool:
    if resolve_road_name(closure).upper() != road_name.upper():
        return False
    closure_direction = (closure.get("direction") or "").lower()
    if closure_direction not in BOTH_DIRECTIONS_VALUES and closure_direction != data_direction.lower():
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


def leg_direction_sort_key(closure: dict, j_from: int | None, j_to: int | None):
    """
    Order closures along the direction of travel for a leg -- e.g. for
    M6 J45 -> J26 southbound, a closure near J45 should come before one
    near J30. Falls back to start time for closures with no junction
    number in their text (which also covers "entire road" legs, where
    junction ordering isn't meaningful).
    """
    if j_from is not None and j_to is not None:
        junctions = extract_junctions(closure)
        if junctions:
            avg = sum(junctions) / len(junctions)
            # travelling from a higher to a lower junction number -> reverse
            return (0, -avg if j_from > j_to else avg)
    return (1, closure.get("start_datetime") or "")


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
    matches.sort(key=lambda c: leg_direction_sort_key(c, j_from, j_to))

    rows = []
    for c in matches:
        location_text = c.get("location_description") or c.get("comment") or "\u2014"

        # If the displayed location has no junction number of its own but
        # matching/sorting still found one (via the comment fallback in
        # extract_junctions() -- e.g. a diversion instruction like "leave
        # the motorway at J22"), surface it so there's never a gap between
        # why a row is positioned where it is and what's visible about it.
        # Worded as "near" since a diversion-derived number is an inferred
        # proxy for the closure's location, not a stated fact about it.
        if not _junctions_in_text(c.get("location_description") or ""):
            fallback_junctions = extract_junctions(c)
            if fallback_junctions:
                uniq = sorted(set(fallback_junctions))
                note = "/".join(f"J{j}" for j in uniq)
                location_text = f"{location_text} \u2014 near {note}"

        rows.append({
            "location": location_text,
            "comment": c.get("comment") or "",
            "start": format_dt(c.get("start_datetime", "")),
            "end": format_dt(c.get("end_datetime", "")),
            "start_iso": c.get("start_datetime") or "",
            "end_iso": c.get("end_datetime") or "",
            "status": (c.get("validity_status") or "unknown").lower(),
            "lanes_restricted": c.get("lanes_restricted"),
            "lanes_operational": c.get("lanes_operational"),
            "lane_info": c.get("lane_info") or "",
            "cause": humanize_cause(c.get("cause_type") or ""),
            "source_label": c.get("source_label") or "",
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
