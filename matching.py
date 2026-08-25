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
# Direction values meaning "affects every direction of travel" -- a
# closure/alert reporting one of these should match a leg regardless of
# that leg's own configured data_direction, rather than requiring an
# exact (and therefore never-matching) string comparison. Real cases,
# not hypothetical: National Highways' Travel Alerts frequently report
# "Both directions" (2 of 3 alerts checked while building that source
# were), and Traffic Scotland's incidents page uses a DIFFERENT phrasing
# for the same thing -- "Northbound & Southbound" (seen on a real A9
# closure) -- confirming this needs more than one exact string to catch
# every real-world variant, not just the one first observed.
BOTH_DIRECTIONS_VALUES = {
    "both directions", "both", "both ways",
    "northbound & southbound", "southbound & northbound",
}


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


# The M61 terminates into the M6 northbound at J30 (a real, well-known
# interchange, not a general cross-road ambiguity). National Highways'
# own data describes an M61-origin closure reaching this merge point as
# e.g. "M61 Northbound Jct 9 to M6 Jct 30 carriageway closure." in the
# comment, while the API's own location_description field then
# approximates this as an M6 "J30-J31" range -- even though the closure
# is fundamentally on the M61, not the M6 mainline. Deliberately scoped
# narrow (M61->M6 specifically, northbound only, matching the exact real
# reported case) rather than a general "any cross-road mention" rule --
# a broad rule risks hiding genuinely relevant M6 closures that just
# happen to mention another road as part of a diversion route, the same
# false-positive class already fixed twice elsewhere in this project
# (Traffic Scotland's M8/M74, Travel Alerts' A31/M27). Kept as a
# separate, clearly-named exclusion rather than baked into
# closure_matches_leg() itself, so it's easy to find, adjust, or remove
# later without touching the general-purpose matcher every road relies on.
_M61_MERGE_RE = re.compile(r'\bM61\b.*?\bto\b.*?\bM6\b', re.IGNORECASE | re.DOTALL)


def is_m61_m6_merge_closure(closure: dict, road_name: str, data_direction: str) -> bool:
    """True if this is actually an M61-origin closure reaching the
    M61/M6 merge point, not a genuine M6 mainline closure -- see the
    comment above _M61_MERGE_RE for why this needs its own narrow rule."""
    if road_name.upper() != "M6" or "north" not in (data_direction or "").lower():
        return False
    haystack = f"{closure.get('location_description', '')} {closure.get('comment', '')}"
    return bool(_M61_MERGE_RE.search(haystack))


# National Highways uses "link road" as their own term for the physical
# slip roads directly connecting two motorways at a shared interchange --
# here, the M6/M62 interchange (Croft Interchange, M6 ~J21 / M62 ~J10).
# Real confirmed example (an EXCLUDED case): "M62 Westbound to M6
# Southbound link road closure" in location_description. There are 8
# possible direction combinations at this interchange (2 M6 directions x
# 2 M62 directions, each as source or destination), but only 2
# correspond to the actual path this project's own Omega route takes
# through it: M6 South links to M62 West (the southbound leg continues
# this way), and M62 East links to M6 North (the northbound leg comes
# from this way). The other 6 combinations describe link roads serving a
# completely different journey through the SAME interchange and have
# nothing to do with Omega's own path, even though they mention both M6
# and M62 by name and would otherwise match either leg. This is
# necessarily an explicit allow-list, not a derived rule -- there's no
# way to derive "does this link serve Omega's route" from the text
# alone; it has to be stated explicitly, confirmed against one real
# example, the same way the M74->M6 continuation elsewhere in this file
# does. The other 7 combinations (including both allowed ones) are
# untested against real text -- they're inferred to follow the same
# phrasing convention as the one confirmed example, on the assumption
# this is a templated/auto-generated description, not independently
# confirmed for each one. The regex matches "M6"/"M62" literally rather
# than a generic road pattern, since that alone guarantees this can
# never fire for any other road pair -- no separate scope-check needed.
_LINK_ROAD_RE = re.compile(
    r'\b(M6|M62)\s+(North|South|East|West)(?:bound)?\s+to\s+'
    r'(M6|M62)\s+(North|South|East|West)(?:bound)?\s+link\s+road\b',
    re.IGNORECASE,
)
_ALLOWED_M6_M62_LINKS = {
    ("M6", "south", "M62", "west"),
    ("M62", "east", "M6", "north"),
}


def is_excluded_m6_m62_link_road(closure: dict) -> bool:
    """True if this is a National Highways "link road" closure at the
    M6/M62 interchange describing a from/to combination that isn't part
    of this project's own configured routes -- see the comment on
    _ALLOWED_M6_M62_LINKS for the exact two combinations that ARE kept.
    Deliberately independent of which leg/direction is currently being
    built (unlike is_m61_m6_merge_closure) -- the allow-list is a fixed,
    universal rule about the closure's own stated from/to combination,
    not something that varies by which leg happens to be asking, since
    the same closure could otherwise match either the M6 leg or the M62
    leg and should be excluded from both if it isn't one of the two
    allowed combinations."""
    haystack = f"{closure.get('location_description', '')} {closure.get('comment', '')}"
    m = _LINK_ROAD_RE.search(haystack)
    if not m:
        return False
    from_road, from_dir, to_road, to_dir = m.groups()
    key = (from_road.upper(), from_dir.lower(), to_road.upper(), to_dir.lower())
    return key not in _ALLOWED_M6_M62_LINKS


def format_dt(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    # 12-hour time, no leading zero on the hour, period separator,
    # lowercase am/pm (e.g. "9.30 am", "12.00 pm") -- computed manually
    # rather than via a %-I / %#I strftime extension, since those aren't
    # portable across platforms (GNU vs Windows use different flags) and
    # this needs to behave identically regardless of where it runs.
    hour12 = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{dt.strftime('%d %b %Y')}, {hour12}.{dt.minute:02d} {ampm}"


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


# Small icon per row, picked from static/logos/ based on what kind of
# entry it actually is. Checked in priority order -- an entry can
# genuinely match more than one category (a slip-road closure IS also a
# full closure), so this is deliberately "most specific/actionable
# impact first, falls back to a generic roadworks icon only when nothing
# more specific is available" rather than a strict cause-vs-impact split.
ICON_ACCIDENT = "accident.png"
ICON_SLIP_ROAD = "slip_road_closed.png"
ICON_ROAD_CLOSED = "road_closed.png"
ICON_LANE_CLOSURE = "lane_closure.png"
ICON_ROADWORKS = "roadworks.png"  # fallback -- most common case in practice

_ACCIDENT_KEYWORDS = (
    "collision", "accident", "police led incident", "vehicle fire",
    "congestion", "queue", "breakdown", "off strategic network incident",
    "road traffic incident",
)
_FULL_CLOSURE_KEYWORDS = ("total closure", "road closure", "carriageway closure", "full closure")
_LANE_CLOSURE_KEYWORDS = (
    "lane closure", "lanes restricted", "single lane running", "single-lane running",
)


def choose_icon(row: dict) -> str:
    """Pick an icon filename (served from static/logos/) representing
    what kind of entry this row is, from its cause/location/comment/lane
    text -- checked in priority order, most specific first:
    accident/incident > slip road > full closure > lane restriction >
    generic roadworks (the fallback when nothing more specific matched).
    """
    location = (row.get("location") or "").lower()
    haystack = " ".join(str(x).lower() for x in (
        row.get("cause") or "",
        row.get("comment") or "",
        row.get("lane_info") or "",
    ))

    if any(k in haystack for k in _ACCIDENT_KEYWORDS):
        return ICON_ACCIDENT
    # Checks the already-normalized location qualifier (e.g. "(Exit Slip
    # Road)") rather than independently re-scanning comment/diversion
    # text with a separate keyword list -- diversion instructions often
    # mention rejoining the motorway "on slip" as an incidental routing
    # detail unrelated to whether the closure itself is slip-road-
    # specific (a real bug this guards against: a mainline Jct 7 to Jct 8
    # closure was misclassified because its diversion said "...rejoin
    # M74 south jct 8 on slip"). Checking the qualifier instead of raw
    # text also guarantees the icon and the visible location text can
    # never disagree with each other.
    if "slip road" in location:
        return ICON_SLIP_ROAD
    if any(k in haystack for k in _FULL_CLOSURE_KEYWORDS) or row.get("lanes_operational") == 0:
        return ICON_ROAD_CLOSED
    if row.get("lanes_restricted") is not None or any(k in haystack for k in _LANE_CLOSURE_KEYWORDS):
        return ICON_LANE_CLOSURE
    return ICON_ROADWORKS


# "on"/"off" can appear either side of "slip" depending on the source --
# "Offslip" (no separator), "off-slip", "off slip", or reversed as
# "slip off" -- all seen in real Traffic Scotland data. National Highways
# uses the official UK terminology instead -- "entry slip road"/"exit
# slip road" -- which "on"/"off" alone never matched, so those fell
# through to the generic "Slip road" label with no direction. Whichever
# group matches gives the direction regardless of which side of "slip"
# it's on.
_SLIP_DIRECTION_RE = re.compile(
    r'\b(?:(on|off|entry|exit)[\s-]?slip|slip[\s-]?(on|off|entry|exit))\b', re.IGNORECASE,
)
_SLIP_ROAD_RE = re.compile(r'\bslip\s*road\b', re.IGNORECASE)

# "on"/"entry" both mean the same thing (joining the motorway); "off"/
# "exit" both mean leaving it.
_ENTRY_WORDS = {"on", "entry"}


def detect_slip_road(text: str) -> str:
    """Detect and normalize slip-road terminology, which varies wildly
    across sources -- real examples seen: "Offslip", "slip off",
    "Onslip", "on-slip", "Slip Off", "slip road" (Traffic Scotland);
    "entry slip road", "exit slip road" (National Highways' official UK
    terminology). Returns "Entry Slip Road", "Exit Slip Road", "Slip
    road" (direction unspecified), or "" if no slip-road mention is
    found at all."""
    m = _SLIP_DIRECTION_RE.search(text)
    if m:
        direction = (m.group(1) or m.group(2)).lower()
        return "Entry Slip Road" if direction in _ENTRY_WORDS else "Exit Slip Road"
    if _SLIP_ROAD_RE.search(text):
        return "Slip road"
    return ""


# "Gretna Services", "Todhills Services", etc. -- named motorway service
# stations don't have their own junction number, they sit BETWEEN two
# junctions. Real bug this guards against: "M74 SB Gretna Services
# Offslip - Slip Road Closure" with a diversion routing traffic via
# "J21 SB Offslip" (since Gretna's own slip road is closed) was showing
# as "M74(S) J21 (near)" -- implying the closure IS at/near J21, when
# it's actually at Gretna Services, which sits between J21 and J22. The
# diversion's junction is a real, useful routing detail, but it's an
# inferred proxy for location, not a stated fact the way the service
# station's own name is.
# Requires each word to be proper Title Case (capital + lowercase, e.g.
# "Gretna") rather than any capitalized word -- otherwise this matches
# direction codes too, since "SB"/"NB" also start with a capital letter.
# Confirmed as a real problem while building this: "M74 SB Gretna
# Services" was extracting "SB Gretna Services" instead of just "Gretna
# Services" until this was tightened.
_SERVICES_RE = re.compile(r"\b((?:[A-Z][a-z][A-Za-z'-]*\s+)*[A-Z][a-z][A-Za-z'-]*\s+Services)\b")


def extract_services_name(text: str) -> str:
    """Extract a named motorway service station from location text, if
    present -- see the module comment on _SERVICES_RE for why this is
    preferred over a fallback-derived junction number when both are
    available."""
    m = _SERVICES_RE.search(text)
    return m.group(1) if m else ""


# Named interchanges/termini that aren't a numbered junction on the road
# itself -- e.g. "Switch Island" (Merseyside), where the M58 physically
# ends and meets the M57/A5036. Detects "J<N> to <Place>" in raw
# location text (e.g. "jct 1 to Switch Island"), so a closure genuinely
# reaching a named terminus can show that instead of just the bare
# junction number, which alone doesn't convey where the closure actually
# reaches. The "j(?:ct)?" part is matched case-insensitively (the real
# text uses lowercase "jct"), scoped with an inline (?i:...) flag rather
# than the whole pattern, since the place-name part deliberately needs
# to stay case-SENSITIVE (requires Title Case) to distinguish it from
# ordinary lowercase text following "to".
_JUNCTION_TO_PLACE_RE = re.compile(
    r"(?i:j(?:ct)?\.?)\s*(\d+[A-Z]?)\s+to\s+([A-Z][a-z][A-Za-z'-]*(?:\s+[A-Z][a-z][A-Za-z'-]*)*)"
)


def extract_junction_to_place(text: str) -> tuple[str, str] | None:
    """Detect a "J<N> to <Place>" pattern in raw location text -- see
    the comment on _JUNCTION_TO_PLACE_RE. Returns (junction_number_str,
    place_name), or None if no such pattern is found."""
    m = _JUNCTION_TO_PLACE_RE.search(text)
    if not m:
        return None
    return m.group(1), m.group(2).strip()


# Known road continuations within THIS PROJECT's own configured routes
# -- M74 Southbound becomes M6 Southbound (both Axis and Omega configure
# this same M74->M6 sequence). A junction number ABOVE this leg's own
# configured upper bound likely belongs to the continuing road, not this
# one -- real case: Traffic Scotland's raw M74 text "J22 - J45" is
# really describing a closure spanning from M74's own J22 all the way
# onto the M6's J45, which doesn't exist as an M74 junction at all (M74
# tops out around J22 in this project's own configuration). Deliberately
# ONE-DIRECTIONAL (too-high only, not too-low too -- see
# label_junction_for_display()'s docstring for the confirmed-live bug
# this fixed): M74's real numbering starts at J1, so a junction number
# BELOW this leg's configured lower bound is still almost certainly a
# legitimate M74 junction the route's own configured slice just doesn't
# happen to cover -- not a sign it belongs to a different road. This is
# deliberately hardcoded to this ONE specific continuation this
# project's own routes actually use -- there's no way to derive "M74
# continues into M6" from the text itself; it's real-world road
# topology knowledge that has to come from somewhere, and routes.yaml's
# own configured leg ranges are the only source of that knowledge
# already available here. If the route configuration ever adds a
# different road that also connects to M74, or removes this M74->M6
# sequence, this mapping needs updating by hand to match -- it will not
# automatically infer a different continuation from routes.yaml, only
# use the leg's own j_from/j_to bounds to decide whether a junction is
# above that upper bound.
_KNOWN_ROAD_CONTINUATIONS = {"M74": "M6"}


def label_junction_for_display(road_name: str, junction: int,
                                j_from: int | None, j_to: int | None) -> str:
    """Format one junction number for display, prefixing it with a
    known continuing road's name if it's ABOVE this leg's own configured
    upper bound -- see the comment on _KNOWN_ROAD_CONTINUATIONS for why,
    and why this is deliberately narrow rather than general.

    CONFIRMED LIVE BUG, now fixed: this used to trigger on ANY
    out-of-range junction, in either direction -- a real M74 closure at
    J6-J8 (both real, legitimate M74 junctions) was showing as
    "M74(S) M6 J6-J8" on the live site, because this route's own M74 leg
    happens to be configured starting at J8 (that's just where THIS
    route joins the M74, not where the real motorway starts), so J6 fell
    "below range" and got wrongly labeled as if it belonged to the M6
    continuation instead. But M74's real numbering starts at J1 -- a
    too-LOW junction is still almost certainly a legitimate M74 junction
    the route's own configured slice just doesn't happen to cover, while
    a too-HIGH junction (e.g. J45, which exceeds M74's real ~22-junction
    span entirely) genuinely can't be an M74 junction at all and DOES
    need the continuation label. Only the "too high" direction should
    ever trigger this, which is what junction > hi (rather than the
    previous not (lo <= junction <= hi)) now checks."""
    if j_from is not None and j_to is not None:
        lo, hi = sorted((j_from, j_to))
        if junction > hi:
            continuation = _KNOWN_ROAD_CONTINUATIONS.get(road_name.upper())
            if continuation:
                return f"{continuation} J{junction}"
    return f"J{junction}"


def format_junction_display(road_name: str, junctions: list[int], j_from: int | None,
                             j_to: int | None, place_name: str = "", range_end_place: str = "") -> str:
    """Compute just the junction/place portion of a location summary
    (everything after "RoadName(Direction)"). Handles three cases: a
    named service station replacing a bare junction number entirely
    (place_name); a known road continuation labeling an out-of-range
    junction with its real road, e.g. "M6 J45" when M74 doesn't have a
    J45 (via label_junction_for_display); and a junction paired with a
    named terminus/interchange, e.g. "J1 - Switch Island"
    (range_end_place). When a continuation label is used, the separator
    between a plain "J22" and a road-prefixed "M6 J45" gets surrounding
    spaces (" - ") rather than the usual bare hyphen, since "J22-M6 J45"
    reads worse than "J8-J22" does -- detected by checking whether
    either label contains a space (a plain "J<N>" never does, a
    continuation label like "M6 J45" always does)."""
    if place_name:
        return place_name
    if not junctions:
        return ""
    uniq = sorted(set(junctions))
    if range_end_place and len(uniq) == 1:
        return f"J{uniq[0]} - {range_end_place}"
    if len(uniq) == 1:
        return label_junction_for_display(road_name, uniq[0], j_from, j_to)
    lo_label = label_junction_for_display(road_name, uniq[0], j_from, j_to)
    hi_label = label_junction_for_display(road_name, uniq[-1], j_from, j_to)
    separator = " - " if (" " in lo_label or " " in hi_label) else "-"
    return f"{lo_label}{separator}{hi_label}"


def format_location(road_name: str, direction_letter: str, junctions: list[int],
                     qualifiers: list[str] | None = None, place_name: str = "",
                     j_from: int | None = None, j_to: int | None = None,
                     range_end_place: str = "") -> str:
    """Build a consistent 'M74(S) J9' style location summary from
    already-extracted structured fields, rather than displaying each
    source's own free-text location description verbatim -- which
    varies wildly in format across this project's sources: direction
    sometimes appears right after the road name, sometimes at the very
    end of the string (Traffic Scotland's raw text, e.g. "M74 J8 - J9
    SB"); junctions are sometimes "J9", sometimes "Jct 9"; some sources
    spell "northbound" out in a full descriptive sentence instead of an
    abbreviation at all (National Highways' traffic-search API).
    qualifiers (e.g. "Exit Slip Road", "near") are appended as one
    combined parenthetical, e.g. "M74(S) J9 (Exit Slip Road)" --
    normalizing slip-road terminology specifically was a deliberate
    second pass after the first version of this function dropped it
    entirely; it's real, useful information (a slip-road closure behaves
    very differently from a mainline one), not just descriptive noise to
    strip out. See format_junction_display() for what place_name,
    j_from/j_to, and range_end_place each do."""
    parts = [f"{road_name}({direction_letter})" if direction_letter else road_name]
    junction_part = format_junction_display(road_name, junctions, j_from, j_to, place_name, range_end_place)
    if junction_part:
        parts.append(junction_part)
    text = " ".join(parts)
    if qualifiers:
        text += f" ({', '.join(qualifiers)})"
    return text


# National Highways' own API appears to count the hard shoulder as a
# regular running lane in its lane totals on TRADITIONAL motorway
# sections (a real hard shoulder that isn't normally driven on) --
# confirmed directly by the person maintaining this project, who has
# first-hand knowledge of which stretches are genuine All Lane Running
# (ALR, where the hard shoulder IS a permanent running lane and the raw
# count is accurate) versus traditional sections (where it isn't, so a
# real 1-lane closure on a 3-lane-plus-hard-shoulder stretch shows as
# "1 restricted / 3 open" when only 2 of those "open" lanes are lanes a
# driver would actually use). Deliberately road-specific and manually
# curated -- there's no way to derive this from the closure data itself,
# it's real-world road engineering knowledge that has to come from
# somewhere. M74 deliberately excluded per explicit instruction --
# Traffic Scotland's own sources are different and this hasn't been
# confirmed to apply there; don't add Scotland roads here without that
# same direct confirmation.
ALR_SECTIONS: dict[str, list[tuple[int, int]]] = {
    "M6": [(21, 26)],
}


def is_within_alr_section(road_name: str, junctions: list[int]) -> bool:
    """True only if every extracted junction genuinely falls within a
    known All Lane Running section for this road (see ALR_SECTIONS).
    No extractable junctions at all defaults to False (apply the hard-
    shoulder correction downstream) -- the ALR sections defined above
    are a narrow minority of these roads' overall length, so an
    unlocated closure is more likely to be on a traditional stretch
    than not."""
    ranges = ALR_SECTIONS.get((road_name or "").upper())
    if not ranges or not junctions:
        return False
    return all(any(lo <= j <= hi for lo, hi in ranges) for j in junctions)


def has_hard_shoulder_lane_count_quirk(road_name: str) -> bool:
    """True only for roads EXPLICITLY confirmed to have this quirk in
    National Highways' own lane-count data -- currently just M6 (the
    same set of roads that have an ALR_SECTIONS entry, though this is
    checked separately from is_within_alr_section() itself: a road with
    NO known quirk at all -- M57/M58/M62, or M74, explicitly excluded
    per instruction since Traffic Scotland's own sources are different
    and unconfirmed -- must NEVER get the lanes_operational correction,
    not even a fallback "no junctions -> assume not ALR" case. Without
    this separate check, is_within_alr_section() alone would return
    False for every one of those roads too (they have no ALR_SECTIONS
    entry), which would have wrongly applied the correction to roads
    this was never confirmed for -- a real bug caught by this project's
    own M74 test case."""
    return (road_name or "").upper() in ALR_SECTIONS


def rows_for_leg(closures: list[dict], road_name: str, data_direction: str,
                  j_from: int | None, j_to: int | None) -> list[dict]:
    matches = [
        c for c in closures
        if closure_matches_leg(c, road_name, data_direction, j_from, j_to)
        and not is_m61_m6_merge_closure(c, road_name, data_direction)
        and not is_excluded_m6_m62_link_road(c)
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
        raw_location = c.get("location_description") or ""
        raw_comment = c.get("comment") or ""
        resolved_road = resolve_road_name(c) or road_name
        direction_display = data_direction[0].upper() if data_direction else ""

        all_junctions = extract_junctions(c)  # location first, falls back to comment
        junctions_from_location_text = _junctions_in_text(raw_location)
        used_fallback = bool(all_junctions) and not junctions_from_location_text
        # Deliberately scans ONLY the raw location text, not raw_comment
        # -- diversion instructions frequently mention rejoining the
        # motorway "on slip" as an incidental routing detail, which has
        # nothing to do with whether the CLOSURE ITSELF is a slip-road
        # closure. Real bug this guards against: a mainline "M74 SB Jct 7
        # to Jct 8 - Road closure" whose diversion said "...rejoin M74
        # south jct 8 on slip" was being misclassified as a slip-road
        # closure entirely because of that incidental mention.
        slip_road = detect_slip_road(raw_location)
        # Prefer a named service station over a fallback-derived junction
        # number, when both are available -- a diversion routing traffic
        # via a nearby junction (since the service station's own slip
        # road is closed) is a real, useful routing detail, but it's an
        # inferred proxy for location, not a stated fact the way the
        # station's own name is. Scoped to the fallback case specifically
        # -- a junction stated directly in the location text is reliable
        # and shouldn't be overridden by this.
        services_name = extract_services_name(raw_location) if used_fallback else ""
        # A junction paired with a named terminus/interchange (e.g. "jct
        # 1 to Switch Island", where the M58 physically ends), checked
        # against the location text specifically -- not scoped to the
        # fallback case, since the junction here IS stated directly, it's
        # just accompanied by a place name for where the closure's other
        # end actually reaches. Confirmed the extracted junction number
        # actually matches one we already found, before trusting it.
        junction_to_place = extract_junction_to_place(raw_location)
        range_end_place = ""
        if junction_to_place:
            place_junction_str, place_name_text = junction_to_place
            if place_junction_str.isdigit() and int(place_junction_str) in all_junctions:
                range_end_place = place_name_text

        if resolved_road:
            qualifiers = []
            if slip_road:
                qualifiers.append(slip_road)
            # Worded as "near" since a comment-derived junction is an
            # inferred proxy for the closure's location (e.g. a diversion
            # instruction like "leave the motorway at J22"), not a stated
            # fact about where the closure itself actually is.
            if used_fallback and not services_name:
                qualifiers.append("near")
            junctions_to_show = [] if services_name else all_junctions
            location_text = format_location(
                resolved_road, direction_display, junctions_to_show, qualifiers, services_name,
                j_from, j_to, range_end_place,
            )
        else:
            # Nothing structured enough to build a clean summary from --
            # fall back to whatever raw text is available rather than
            # showing just a bare direction letter with no road name.
            location_text = raw_location or raw_comment or "\u2014"

        # The normalized summary above is deliberately terser than each
        # source's own free text -- preserve that original detail (place
        # names, closure type, lane specifics) in the comment instead of
        # discarding it, so nothing is actually lost, just moved out of
        # the at-a-glance label into the collapsible More Info section.
        extra_detail = raw_location if raw_location and raw_location != raw_comment else ""
        combined_comment = " \u2014 ".join(p for p in (extra_detail, raw_comment) if p)

        # Hard-shoulder correction -- see ALR_SECTIONS/is_within_alr_section()
        # above. Only ever touches lanes_operational (the "still open"
        # count) -- lanes_restricted is left exactly as reported, since
        # the hard shoulder isn't something National Highways would ever
        # report as "restricted"; it's just not a real running lane to
        # begin with outside a genuine ALR section.
        lanes_operational_display = c.get("lanes_operational")
        if (
            resolved_road
            and lanes_operational_display is not None
            and has_hard_shoulder_lane_count_quirk(resolved_road)
            and not is_within_alr_section(resolved_road, all_junctions)
        ):
            lanes_operational_display = max(0, lanes_operational_display - 1)

        row = {
            "location": location_text,
            "comment": combined_comment,
            "start": format_dt(c.get("start_datetime", "")),
            "end": format_dt(c.get("end_datetime", "")),
            "start_iso": c.get("start_datetime") or "",
            "end_iso": c.get("end_datetime") or "",
            "status": (c.get("validity_status") or "unknown").lower(),
            "lanes_restricted": c.get("lanes_restricted"),
            "lanes_operational": lanes_operational_display,
            "lane_info": c.get("lane_info") or "",
            "cause": humanize_cause(c.get("cause_type") or ""),
            "source_label": c.get("source_label") or "",
        }
        row["icon"] = choose_icon(row)
        rows.append(row)
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
