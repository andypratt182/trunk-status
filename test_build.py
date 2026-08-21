#!/usr/bin/env python3
"""
Tests for matching.py and sources/*.py.

Run with: python3 test_build.py

No test framework dependency (just plain asserts via check()) -- keeps
this runnable in the same environment as build.py itself, with no extra
requirements.txt entries. Exits non-zero on the first failure.

Several of these use real captured content (from live fetches made while
building each source) rather than synthetic data, noted per test.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error

import matching
import build
from sources import national_highways as nh
from sources import national_highways_traffic_search as nhts
from sources import scotland_incidents as si
from sources import tomtom_incidents as tti
from sources import traffic_scotland as scot
from sources import travel_alerts as ta
from sources import xlsx_advance_notice as xlsx

FAILURES = 0


def check(label: str, cond: bool) -> None:
    global FAILURES
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        FAILURES += 1


def section(title: str) -> None:
    print(f"\n--- {title} ---")


# =======================================================================
# matching.py
# =======================================================================

section("matching: extract_junctions (location vs. comment fallback)")

check(
    "junction from location_description",
    matching.extract_junctions({"location_description": "M6 southbound between J40 and J39", "comment": ""})
    == [40, 39],
)
check(
    "falls back to comment when location has none",
    matching.extract_junctions({"location_description": "", "comment": "Closure near Junction 12"}) == [12],
)
check(
    "diversion text in comment does NOT contaminate when location already has junctions",
    matching.extract_junctions({
        "location_description": "M6 southbound Junction 31 to Junction 30",
        "comment": "Diversion via A50, rejoin motorway at Junction 24",
    }) == [31, 30],
)
check(
    "spelled-out 'Junction 40 to 39' (second number has no prefix)",
    matching._junctions_in_text("Junctions 40 to 39") == [40, 39],
)
check(
    "'conjunction' does not false-positive match",
    matching._junctions_in_text("Roadworks in conjunction with utility company") == [],
)

section("matching: closure_matches_leg")

m6_closure = {
    "road_name": "M6", "direction": "southBound",
    "location_description": "M6 southbound between J40 and J39",
}
check("matches correct road/direction/range", matching.closure_matches_leg(m6_closure, "M6", "southBound", 45, 26))
check("direction comparison is case-insensitive", matching.closure_matches_leg(m6_closure, "M6", "SOUTHBOUND", 45, 26))
check("rejects wrong road", not matching.closure_matches_leg(m6_closure, "M1", "southBound", 45, 26))
check("rejects out-of-range junction", not matching.closure_matches_leg(m6_closure, "M6", "southBound", 10, 20))
check(
    "entire-road leg (no junction filter) matches regardless of junction",
    matching.closure_matches_leg(m6_closure, "M6", "southBound", None, None),
)

section("matching: leg_direction_sort_key ordering (real M6 data pattern)")

closures = [
    {"road_name": "M6", "direction": "southBound", "location_description": "M6 southbound between J30 and J29", "start_datetime": "2026-08-25T00:00:00"},
    {"road_name": "M6", "direction": "southBound", "location_description": "M6 southbound between J45 and J44", "start_datetime": "2026-08-30T00:00:00"},
    {"road_name": "M6", "direction": "southBound", "location_description": "M6 southbound between J27 and J26", "start_datetime": "2026-08-15T00:00:00"},
]
rows = matching.rows_for_leg(closures, "M6", "southBound", 45, 26)
check(
    "descending order (45 -> 26) regardless of start_datetime",
    [r["location"] for r in rows] == [
        "M6(S) J44-J45",
        "M6(S) J29-J30",
        "M6(S) J26-J27",
    ],
)

section("matching: choose_icon (real examples from every source built in this project)")

check(
    "Travel Alert collision -> accident icon",
    matching.choose_icon({
        "cause": "Road traffic collision", "location": "M1 - Between J16 and J18 - Carriageway Closure",
        "comment": "Northamptonshire - Road traffic collision - Expect Delays - Northbound",
        "lane_info": "", "lanes_restricted": None, "lanes_operational": None,
    }) == matching.ICON_ACCIDENT,
)
check(
    "NH Traffic Search congestion -> accident icon",
    matching.choose_icon({
        "cause": "Congestion", "location": "The M6 northbound between junctions J9 and J11",
        "comment": "There are currently delays of 12 minutes against expected traffic",
        "lane_info": "", "lanes_restricted": None, "lanes_operational": None,
    }) == matching.ICON_ACCIDENT,
)
check(
    "real M74 J9 Offslip total closure -> slip road icon "
    "(checks the already-normalized location qualifier, which is what choose_icon() "
    "actually receives in real usage -- rows_for_leg always normalizes location first)",
    matching.choose_icon({
        "cause": "Barrier Repair, Filter Drain, Inspections, Sign Installation/Repairs",
        "location": "M74(S) J9 (Exit Slip Road)",
        "comment": "Diversion: Follow mainline closure", "lane_info": "Road Closure.",
        "lanes_restricted": None, "lanes_operational": None,
    }) == matching.ICON_SLIP_ROAD,
)
check(
    "real bug this guards against: a diversion mentioning 'rejoin ... on slip' as an "
    "incidental routing detail must NOT make a genuine mainline closure look like a "
    "slip-road one -- confirmed by checking a normalized location WITHOUT the slip "
    "qualifier (since detect_slip_road only scans raw location text, not diversions)",
    matching.choose_icon({
        "cause": "Grass Cutting", "location": "M74(S) J7-J8",
        "comment": "Diversion: leave M74 Jct 7- at jct turn left onto A72 Lanark Rd- at "
                   "jct turn left onto onto B7808 Ayr Rd- at rbt take 1st exit onto A71- "
                   "rejoin M74 south jct 8 on slip.",
        "lane_info": "Road Closure.", "lanes_restricted": None, "lanes_operational": None,
    }) == matching.ICON_ROAD_CLOSED,
)
check(
    "National Highways roadMaintenance WITH a real lane-restriction number -> lane closure icon (impact-based)",
    matching.choose_icon({
        "cause": "Road maintenance", "location": "M6 southbound between J40 and J39",
        "comment": "", "lane_info": "", "lanes_restricted": 1, "lanes_operational": 2,
    }) == matching.ICON_LANE_CLOSURE,
)
check(
    "'single lane running' text (in lane_info) correctly gets the lane closure icon",
    matching.choose_icon({
        "cause": "Road maintenance", "location": "M6(S) J40", "comment": "",
        "lane_info": "Single lane running", "lanes_restricted": None, "lanes_operational": None,
    }) == matching.ICON_LANE_CLOSURE,
)
check(
    "'single lane running' text appearing in the comment/description instead is also caught",
    matching.choose_icon({
        "cause": "Road maintenance", "location": "M6(S) J15-J16",
        "comment": "M6 Southbound Jct 16 to 15 - single lane running in operation for resurfacing works",
        "lane_info": "", "lanes_restricted": None, "lanes_operational": None,
    }) == matching.ICON_LANE_CLOSURE,
)
check(
    "generic roadworks with no lane/slip/accident detail at all -> roadworks fallback",
    matching.choose_icon({
        "cause": "Road maintenance", "location": "M57 J4 to J6", "comment": "",
        "lane_info": "", "lanes_restricted": None, "lanes_operational": None,
    }) == matching.ICON_ROADWORKS,
)
check(
    "rows_for_leg attaches a real icon filename to every row",
    all(
        r["icon"] in {matching.ICON_ACCIDENT, matching.ICON_SLIP_ROAD, matching.ICON_ROAD_CLOSED,
                      matching.ICON_LANE_CLOSURE, matching.ICON_ROADWORKS}
        for r in matching.rows_for_leg(
            [{"road_name": "M6", "direction": "southBound", "location_description": "M6 J40",
              "comment": "", "cause_type": "roadMaintenance", "lanes_restricted": 1,
              "lanes_operational": 2, "start_datetime": "", "end_datetime": "",
              "validity_status": "planned", "source_label": "test"}],
            "M6", "southBound", 39, 41,
        )
    ),
)

section("matching: humanize_cause (camelCase identifiers vs. already-human text)")

check(
    "the real reported bug: XLSX placeholder no longer shows as a mangled run-together word",
    matching.humanize_cause("advanceNoticeFullClosure") == "Advance notice full closure",
)
check(
    "National Highways DATEX causeType values split correctly",
    matching.humanize_cause("roadMaintenance") == "Road maintenance"
    and matching.humanize_cause("constructionWork") == "Construction work"
    and matching.humanize_cause("roadOrCarriagewayOrLaneManagement")
    == "Road or carriageway or lane management",
)
check(
    "Traffic Scotland's already-human Works text is left completely untouched, casing and all",
    matching.humanize_cause("Barrier Repair, Filter Drain, Inspections, Sign Installation/Repairs")
    == "Barrier Repair, Filter Drain, Inspections, Sign Installation/Repairs",
)
check("empty input -> empty output", matching.humanize_cause("") == "")

section("matching: detect_slip_road (real and plausible terminology variants)")

check(
    "real terminology variants all normalize correctly",
    matching.detect_slip_road("M74 J9 Offslip SB -Total Closure") == "Exit Slip Road"
    and matching.detect_slip_road("M74 J5 (Raith) North - slip off") == "Exit Slip Road"
    and matching.detect_slip_road("A737 M8-J29 North - Slip Off") == "Exit Slip Road"
    and matching.detect_slip_road("M74 J8 Onslip NB") == "Entry Slip Road"
    and matching.detect_slip_road("M6 on-slip closure at J40") == "Entry Slip Road"
    and matching.detect_slip_road("M6 slip on closure") == "Entry Slip Road"
    and matching.detect_slip_road("M74 slip road closure at J10") == "Slip road",
)
check(
    "no slip mention -> empty string, and 'on' elsewhere doesn't false-positive",
    matching.detect_slip_road("M6 southbound between J40 and J39, Lane 3 closure") == ""
    and matching.detect_slip_road("Road maintenance on the hard shoulder") == "",
)
check(
    "real reported bug: National Highways' official 'entry slip road'/'exit slip road' "
    "terminology now correctly gives a direction, instead of falling through to the "
    "generic 'Slip road' label -- the original regex only recognized 'on'/'off' next "
    "to 'slip', which Traffic Scotland uses but National Highways doesn't",
    matching.detect_slip_road("M6 southbound entry slip road closure at J40") == "Entry Slip Road"
    and matching.detect_slip_road("M6 northbound exit slip road closed J20") == "Exit Slip Road"
    and matching.detect_slip_road("The M6 entry slip road at junction 15 is closed") == "Entry Slip Road",
)

section("matching: rows_for_leg -- real reported bug, diversion text falsely triggering slip-road detection")

_real_m74_j7j8_closure = {
    "road_name": "M74", "direction": "Southbound",
    "location_description": "M74 SB Jct 7 to Jct 8 - Road closure",
    "comment": "Diversion: leave M74 Jct 7- at jct turn left onto A72 Lanark Rd- at jct "
               "turn left onto onto B7808 Ayr Rd- at rbt take 1st exit onto A71- rejoin "
               "M74 south jct 8 on slip.",
    "cause_type": "Grass Cutting", "lane_info": "Road Closure.",
    "lanes_restricted": None, "lanes_operational": None, "source_label": "Traffic Scotland (scraped)",
    "start_datetime": "2026-09-21T20:00:00", "end_datetime": "2026-09-22T06:00:00",
    "validity_status": "planned",
}
_real_rows = matching.rows_for_leg([_real_m74_j7j8_closure], "M74", "southBound", 7, 8)
check("one row produced", len(_real_rows) == 1)
check(
    "location correctly shows a plain mainline closure -- the diversion's incidental "
    "'...rejoin M74 south jct 8 on slip' must NOT add a false '(Exit Slip Road)' qualifier",
    _real_rows[0]["location"] == "M74(S) J7-J8",
)
check(
    "icon correctly shows a full road closure, not a slip-road closure",
    _real_rows[0]["icon"] == matching.ICON_ROAD_CLOSED,
)

section("matching: extract_services_name (real reported bug -- Gretna Services falsely shown as J21)")

check(
    "'SB'/'NB' direction codes are NOT mistaken for part of the place name "
    "(a real bug caught while building this: the regex first matched "
    "'SB Gretna Services' before being tightened to require proper Title Case)",
    matching.extract_services_name("M74 SB Gretna Services Offslip - Slip Road Closure") == "Gretna Services",
)
check(
    "no services station mentioned -> empty string",
    matching.extract_services_name("M74 SB Jct 7 to Jct 8 - Road closure") == "",
)

_real_gretna_services_closure = {
    "road_name": "M74", "direction": "Southbound",
    "location_description": "M74 SB Gretna Services Offslip - Slip Road Closure",
    "comment": "Diversion: J21 SB Offslip, B6357, B7076",
    "cause_type": "Lining Works", "lane_info": "Road Closure.",
    "lanes_restricted": None, "lanes_operational": None, "source_label": "Traffic Scotland (scraped)",
    "start_datetime": "2026-08-24T20:00:00", "end_datetime": "2026-08-25T06:00:00",
    "validity_status": "planned",
}
_gretna_rows = matching.rows_for_leg([_real_gretna_services_closure], "M74", "southBound", 8, 22)
check("one row produced", len(_gretna_rows) == 1)
check(
    "shows the real place name, not a misleading fallback junction -- Gretna Services "
    "sits BETWEEN J21 and J22, it isn't AT J21 the way the diversion text alone implies",
    _gretna_rows[0]["location"] == "M74(S) Gretna Services (Exit Slip Road)",
)
check(
    "the fallback-derived junction (J21) is still used correctly for leg MATCHING "
    "internally -- only the DISPLAYED text changes, not which leg this belongs to",
    len(matching.rows_for_leg([_real_gretna_services_closure], "M74", "southBound", 1, 5)) == 0,
)

section("matching: is_m61_m6_merge_closure (real reported bug -- M61/M6 merge shown as an M6 mainline closure)")

_m61_merge_closure = {
    "road_name": "M6", "direction": "northBound",
    "location_description": "M6 northbound between J30 and J31",
    "comment": "M61 Northbound Jct 9 to M6 Jct 30 carriageway closure.",
    "cause_type": "roadMaintenance", "lanes_restricted": 5, "lanes_operational": 0,
    "start_datetime": "2026-08-26T20:00:00", "end_datetime": "2026-08-27T05:00:00",
    "validity_status": "planned", "source_label": "Live API",
}
check(
    "correctly detected as an M61-origin closure reaching the M6, not a genuine M6 closure",
    matching.is_m61_m6_merge_closure(_m61_merge_closure, "M6", "northBound"),
)
check(
    "correctly excluded from the M6 Northbound leg end-to-end",
    len(matching.rows_for_leg([_m61_merge_closure], "M6", "northBound", 26, 45)) == 0,
)

_ordinary_m6_closure = {
    "road_name": "M6", "direction": "northBound",
    "location_description": "M6 northbound between J40 and J39", "comment": "",
    "cause_type": "roadMaintenance", "lanes_restricted": 1, "lanes_operational": 2,
    "start_datetime": "2026-08-26T20:00:00", "end_datetime": "2026-08-27T05:00:00",
    "validity_status": "planned", "source_label": "Live API",
}
check(
    "a genuine M6 northbound closure (no M61 mention at all) is completely unaffected",
    len(matching.rows_for_leg([_ordinary_m6_closure], "M6", "northBound", 26, 45)) == 1,
)

_m61_mentioned_as_diversion = dict(_ordinary_m6_closure,
                                    comment="Diversion via M61 available if required")
check(
    "an M6 closure that merely MENTIONS the M61 (e.g. as an alternative diversion "
    "route) is NOT excluded -- only the specific 'M61 ... to M6' merge pattern is",
    len(matching.rows_for_leg([_m61_mentioned_as_diversion], "M6", "northBound", 26, 45)) == 1,
)

_m61_pattern_on_different_road = dict(_m61_merge_closure, road_name="M57", location_description="M57 J4 to J5")
check(
    "the exact same merge-pattern text on a DIFFERENT road (M57) is NOT excluded -- "
    "this rule is deliberately scoped to road_name == 'M6' only, not general text matching",
    len(matching.rows_for_leg([_m61_pattern_on_different_road], "M57", "northBound", 4, 6)) == 1,
)

section("matching: extract_junction_to_place (real reported case -- M58 ending at Switch Island)")

check(
    "'jct 1 to Switch Island' correctly extracted",
    matching.extract_junction_to_place("M58 westbound jct 1 to Switch Island carriageway closure")
    == ("1", "Switch Island"),
)
check(
    "no such pattern -> None",
    matching.extract_junction_to_place("M6 southbound between J40 and J39, Lane 3 closure") is None,
)

_real_m58_switch_island = {
    "road_name": "M58", "direction": "Westbound",
    "location_description": "M58 westbound jct 1 to Switch Island carriageway closure",
    "comment": "Overall Scheme Details: M58 westbound M58 to Switch Island - carriageway closure "
               "for carriageway - reconstruction/renewal on behalf of National Highways",
    "cause_type": "advanceNoticeFullClosure", "lanes_operational": 0,
    "start_datetime": "2026-08-20T21:00:00", "end_datetime": "2026-08-21T05:00:00",
    "validity_status": "planned", "source_label": "Advance notice (full closure)",
}
_m58_rows = matching.rows_for_leg([_real_m58_switch_island], "M58", "Westbound", None, None)
check("one row produced", len(_m58_rows) == 1)
check(
    "shows the real terminus name paired with the junction, not just a bare 'J1' -- "
    "Switch Island is where the M58 physically ends, meeting the M57/A5036",
    _m58_rows[0]["location"] == "M58(W) J1 - Switch Island",
)

section("matching: label_junction_for_display / M74->M6 continuation (real reported case)")

check(
    "an out-of-range M74 junction is labeled with the continuing road (M6)",
    matching.label_junction_for_display("M74", 45, 8, 22) == "M6 J45",
)
check(
    "an in-range M74 junction is shown plainly, no continuation label",
    matching.label_junction_for_display("M74", 15, 8, 22) == "J15",
)
check(
    "M6 itself has no configured continuation -- an out-of-range M6 junction is shown plainly",
    matching.label_junction_for_display("M6", 100, 26, 45) == "J100",
)

_real_m74_switch_to_m6 = {
    "road_name": "M74", "direction": "Southbound",
    "location_description": "M74 SB J22 - J45 - Road Closure",
    "comment": "Diversion: J22 offslip, Kirkstyle, A6071, A7, J44",
    "cause_type": "Third Party Works", "lane_info": "Road Closure.",
    "start_datetime": "2026-09-07T20:00:00", "end_datetime": "2026-09-08T06:00:00",
    "validity_status": "planned", "source_label": "Traffic Scotland (scraped)",
}
_m74_continuation_rows = matching.rows_for_leg([_real_m74_switch_to_m6], "M74", "southBound", 8, 22)
check("one row produced", len(_m74_continuation_rows) == 1)
check(
    "shows 'J22 - M6 J45', not the misleading 'J22-J45' -- M74 doesn't have a J45 at all, "
    "the M74 southbound leg becomes the M6 southbound leg at exactly this point",
    _m74_continuation_rows[0]["location"] == "M74(S) J22 - M6 J45",
)
check(
    "regression: a normal M74 range with BOTH junctions in range is completely unaffected, "
    "still uses the plain no-space hyphen",
    matching.rows_for_leg(
        [{"road_name": "M74", "direction": "Southbound",
          "location_description": "M74 J8 - J10 SB - Total Closure", "comment": ""}],
        "M74", "southBound", 8, 22,
    )[0]["location"] == "M74(S) J8-J10",
)

section("matching: is_excluded_m6_m62_link_road (real reported case -- M6/M62 interchange 'link roads')")

_real_m62_link_closure = {
    "road_name": "M62", "direction": "Westbound",
    "location_description": "M62 Westbound to M6 Southbound link road closure",
    "comment": "Overall Scheme Details: M62 both directions Jct 9 to Jct 12 - carriageway "
               "closure for horticulture (cutting and planting) on behalf of National Highways",
    "cause_type": "advanceNoticeFullClosure", "lanes_operational": 0,
    "start_datetime": "2026-08-17T21:00:00", "end_datetime": "2026-08-18T05:00:00",
    "validity_status": "planned", "source_label": "Advance notice (full closure)",
}
check(
    "real reported entry correctly detected as an excluded link road "
    "(M62 West -> M6 South isn't part of Omega's own path through this interchange)",
    matching.is_excluded_m6_m62_link_road(_real_m62_link_closure),
)
check(
    "correctly excluded from the M62 Westbound leg end-to-end",
    len(matching.rows_for_leg([_real_m62_link_closure], "M62", "Westbound", 8, 10)) == 0,
)

check(
    "all 8 possible direction combinations at this interchange are classified correctly per "
    "the full reported rule table -- only 1 of the 8 (M62 West -> M6 South) was independently "
    "confirmed against real text; the other 7 are inferred to follow the same phrasing",
    all(
        (not matching.is_excluded_m6_m62_link_road({
            "location_description": f"{from_road} {from_dir}bound to {to_road} {to_dir}bound link road closure",
            "comment": "",
        })) == should_include
        for from_road, from_dir, to_road, to_dir, should_include in [
            ("M6", "South", "M62", "West", True),
            ("M6", "South", "M62", "East", False),
            ("M6", "North", "M62", "East", False),
            ("M6", "North", "M62", "West", False),
            ("M62", "East", "M6", "North", True),
            ("M62", "East", "M6", "South", False),
            ("M62", "West", "M6", "North", False),
            ("M62", "West", "M6", "South", False),  # matches the real confirmed example
        ]
    ),
)

check(
    "ordinary M6/M62 closures with no link-road phrasing at all are completely unaffected",
    len(matching.rows_for_leg(
        [{"road_name": "M6", "direction": "northBound",
          "location_description": "M6 northbound between J40 and J39", "comment": "",
          "cause_type": "roadMaintenance", "lanes_restricted": 1, "lanes_operational": 2,
          "start_datetime": "2026-08-26T20:00:00", "end_datetime": "2026-08-27T05:00:00",
          "validity_status": "planned", "source_label": "Live API"}],
        "M6", "northBound", 26, 45,
    )) == 1,
)
check(
    "the same 'link road' phrasing for a DIFFERENT interchange entirely (M56/M6) is NOT "
    "excluded -- the regex matches 'M6'/'M62' literally, deliberately scoped to this one "
    "interchange, not a general 'any link road' rule",
    not matching.is_excluded_m6_m62_link_road({
        "location_description": "M56 Eastbound to M6 Northbound link road closure", "comment": "",
    }),
)

gretna_style = {
    "road_name": "M74", "direction": "Northbound",
    "location_description": "M74 (Gretna Nth Slip) to M74 (Off Slip), Northbound \u2014 Road Closure.",
    "comment": "Works: Lining | Traffic Management: Road Closure. | Diversion: Leave the motorway at J22",
    "start_datetime": "2026-08-25T20:00:00", "end_datetime": "",
    "validity_status": "planned", "cause_type": "Lining Works",
    "lanes_restricted": None, "lanes_operational": None, "source_label": "test",
}
rows = matching.rows_for_leg([gretna_style], "M74", "Northbound", 22, 8)
check("matched via comment fallback", len(rows) == 1)
check(
    "'(near)' qualifier added since location itself has no junction "
    "(junction 22 only came from the comment fallback) -- and 'Off-slip' "
    "correctly detected too, since this real fixture's location text "
    "genuinely mentions '(Off Slip)'",
    rows[0]["location"] == "M74(N) J22 (Exit Slip Road, near)",
)

section("matching: cross-check dedup by record_id")

dup_a = {"record_id": "X", "road_name": "M6", "direction": "southBound", "location_description": "M6 southbound J10", "start_datetime": "2026-01-01T00:00:00"}
dup_b = {"record_id": "X", "road_name": "M6", "direction": "southBound", "location_description": "M6 southbound J10 to J11", "start_datetime": "2026-01-01T00:00:00"}
rows = matching.rows_for_leg([dup_a, dup_b], "M6", "southBound", 1, 20)
check("multiple segments sharing record_id collapse to one row", len(rows) == 1)


# =======================================================================
# sources/national_highways.py
# =======================================================================

section("national_highways: normalize_datex_response (synthetic DATEX II payload)")

synthetic_payload = {
    "D2Payload": {
        "situation": [{
            "situationRecord": [{
                "sitRoadOrCarriagewayOrLaneManagement": {
                    "idG": "test-001",
                    "validity": {
                        "validityStatus": "active",
                        "validityTimeSpecification": {
                            "overallStartTime": "2026-08-20T08:00:00.00Z",
                            "overallEndTime": "2026-08-21T09:00:00.00Z",
                        },
                    },
                    "cause": {"causeType": "roadMaintenance"},
                    "generalPublicComment": [{"comment": "M6 J40 to J39 lane closure"}],
                    "locationReference": {"locLocationGroupByList": {"locationContainedInGroup": [{
                        "locLinearLocation": {"supplementaryPositionalDescription": {
                            "locationDescription": "M6 southbound between J40 and J39",
                            "carriageway": [{"carriagewayExtensionG": {"impactOnCarriageway": {
                                "numberOfLanesRestricted": 1, "numberOfOperationalLanes": 2,
                            }}}],
                        }},
                        "locSingleRoadLinearLocation": {"linearWithinLinearElement": [{
                            "directionOnLinearSection": "southBound",
                            "linearElement": {"locLinearElementByCode": {"roadName": "M6"}},
                        }]},
                    }]}},
                },
            }],
        }],
    },
}
flat = nh.normalize_datex_response(synthetic_payload)
check("one flat record produced", len(flat) == 1)
check("road_name extracted", flat[0]["road_name"] == "M6")
check("direction extracted", flat[0]["direction"] == "southBound")
check("lanes extracted", flat[0]["lanes_restricted"] == 1 and flat[0]["lanes_operational"] == 2)
check("record_id from idG", flat[0]["record_id"] == "test-001")

section("national_highways: fetch_from_national_highways_api fetches BOTH closureTypes explicitly")

_nh_test_calls = []


def _make_nh_record(idx, cause_type):
    return {"sitRoadOrCarriagewayOrLaneManagement": {
        "idG": f"test-{idx}", "validity": {"validityStatus": "active", "validityTimeSpecification": {
            "overallStartTime": "2026-08-20T08:00:00Z", "overallEndTime": "2026-08-21T08:00:00Z"}},
        "cause": {"causeType": cause_type}, "generalPublicComment": [{"comment": "test"}],
        "locationReference": {"locLocationGroupByList": {"locationContainedInGroup": [{
            "locLinearLocation": {"supplementaryPositionalDescription": {"locationDescription": "M6", "carriageway": []}},
            "locSingleRoadLinearLocation": {"linearWithinLinearElement": [{
                "directionOnLinearSection": "southBound",
                "linearElement": {"locLinearElementByCode": {"roadName": "M6"}}}]},
        }]}},
    }}


_nh_planned_payload = {"D2Payload": {"situation": [{"situationRecord": [
    _make_nh_record(1, "roadMaintenance"), _make_nh_record(2, "constructionWork"),
]}]}}
_nh_unplanned_payload = {"D2Payload": {"situation": [{"situationRecord": [
    _make_nh_record(3, "vehicleAccident"),
]}]}}


def _fake_nh_fetch_json(url, headers=None):
    _nh_test_calls.append(url)
    if "closureType=unplanned" in url:
        return _nh_unplanned_payload, {}
    if "closureType=planned" in url:
        return _nh_planned_payload, {}
    raise AssertionError(f"unexpected URL with no closureType: {url}")


os.environ["NATIONAL_HIGHWAYS_API_KEY"] = "fake-test-key-for-tests"
_original_nh_fetch_json = nh.fetch_json
nh.fetch_json = _fake_nh_fetch_json
try:
    nh_closures = nh.fetch_from_national_highways_api({"lookahead_days": 7})
finally:
    nh.fetch_json = _original_nh_fetch_json

check("closure_type left unset -> exactly 2 calls made (planned + unplanned explicitly)",
      len(_nh_test_calls) == 2)
check("one call used closureType=planned, the other closureType=unplanned",
      any("closureType=planned" in u for u in _nh_test_calls)
      and any("closureType=unplanned" in u for u in _nh_test_calls))
check("results from both calls are merged, including the incident record",
      len(nh_closures) == 3 and any(c["cause_type"] == "vehicleAccident" for c in nh_closures))
check(
    "each closure is reliably tagged with which closureType query returned it -- "
    "the signal actually worth trusting, since a real unplanned closure turned "
    "out to have a generic cause_type ('roadOrCarriagewayOrLaneManagement') that "
    "no keyword guess would have flagged",
    all(c["closure_category"] in ("planned", "unplanned") for c in nh_closures)
    and sum(1 for c in nh_closures if c["closure_category"] == "planned") == 2
    and sum(1 for c in nh_closures if c["closure_category"] == "unplanned") == 1,
)

_nh_test_calls.clear()
nh.fetch_json = _fake_nh_fetch_json
try:
    nh_closures_explicit = nh.fetch_from_national_highways_api(
        {"lookahead_days": 7, "closure_type": "planned"}
    )
finally:
    nh.fetch_json = _original_nh_fetch_json

check("closure_type explicitly set to 'planned' -> only 1 call made (unchanged behavior)",
      len(_nh_test_calls) == 1 and "closureType=planned" in _nh_test_calls[0])
check("only the planned records are returned", len(nh_closures_explicit) == 2)
check("those records are also correctly tagged closure_category='planned'",
      all(c["closure_category"] == "planned" for c in nh_closures_explicit))


section("national_highways: find_next_page_url")

check("finds Link header rel=next", nh.find_next_page_url(
    {}, {"Link": '<https://api.example.com/closures?page=2>; rel="next"'}
) == "https://api.example.com/closures?page=2")
check("finds JSON nextLink field", nh.find_next_page_url({"nextLink": "https://x/2"}, {}) == "https://x/2")
check("no pagination signal -> None", nh.find_next_page_url({"D2Payload": {}}, {}) is None)


# =======================================================================
# sources/xlsx_advance_notice.py
# =======================================================================

section("xlsx_advance_notice: header row detection with a title row above it")

rows_with_title = [
    ("7 Day Closure Report -- Saturday",),  # title row
    ("Road Number", "Direction", "Location", "Scheduled Start Time", "Scheduled End Time", "Closure Details Including Diversions"),
    ("M6", "Southbound", "J40 to J39", "2026-08-20", "2026-08-21", "Full closure"),
]
header_idx, col_map = xlsx.find_header_row(rows_with_title)
check("finds header on row 2 (index 1), not row 1", header_idx == 1)
check("maps 'Road Number' -> road_name", "road_name" in col_map)
check("maps 'Scheduled Start Time' -> start_datetime", "start_datetime" in col_map)

section("xlsx_advance_notice: MAX_COLUMNS_TO_READ caps how many columns are actually read")

check(
    "cap is generous enough to cover all 6 known real columns",
    xlsx.MAX_COLUMNS_TO_READ >= 6,
)

section("xlsx_advance_notice: trim_trailing_empty (real Tuesday-sheet-style padding)")

padded_row = (
    "Road number", "Direction", "Location", "Scheduled\nstart time",
    "Scheduled\nend time", "Closure details, including diversions"
) + (None,) * 300
trimmed = xlsx.trim_trailing_empty(padded_row)
check("hundreds of trailing Nones removed (logging cleanup only)", len(trimmed) == 6)
check(
    "real headers preserved exactly",
    trimmed == ("Road number", "Direction", "Location", "Scheduled\nstart time",
                "Scheduled\nend time", "Closure details, including diversions"),
)
check(
    "a None in the MIDDLE of a row is preserved, not stripped",
    xlsx.trim_trailing_empty(("Road", None, "Location")) == ("Road", None, "Location"),
)
check(
    "header detection still works correctly against heavily-padded rows (fix is logging-only)",
    xlsx.find_header_row([
        ("title row",) + (None,) * 5,
        ("Road Number", "Direction", "Location", "Start", "End", "Comment") + (None,) * 300,
    ])[0] == 1,
)

section("xlsx_advance_notice: unrecognized schema -> graceful no-match")

header_idx, col_map = xlsx.find_header_row([("Foo", "Bar", "Baz")] * 8)
check("no header found for unrelated columns", header_idx is None)


# =======================================================================
# sources/traffic_scotland.py
# =======================================================================

section("traffic_scotland: date parsing (real formats from live fetches)")

for text, expected in [
    ("20th of July 2026, 8:00pm", "2026-07-20T20:00:00"),
    ("18th of September 2026, 6:00am", "2026-09-18T06:00:00"),
    ("1st of January 2025, 6:00am", "2025-01-01T06:00:00"),
]:
    check(f"{text!r} -> {expected}", scot.parse_scottish_datetime(text) == expected)

section("traffic_scotland: M74/A74(M) alias matching survives \\b-after-paren bug")

# \b fails right after "A74(M)"'s closing paren since both ")" and the
# following space are non-word chars -- lookarounds fix this
check(
    "A74(M) title matches via lookaround (not \\b)",
    bool(scot.LISTING_ROAD_TOKEN_RE.findall("A74(M) (Jct 15 to Jct 16), Northbound")),
)

section("traffic_scotland: stage 1 entry discovery (real listing page content)")

real_listing_html = """
<html><body>
<div class="views-row">
<h2>A701 SB from A74M Road Part of A Diversion Route</h2>
<p>Location:A701 (Start Beattock East Rbt) to M74 (A701 Moffat Rbt), Southbound</p>
<p>Start time:14th of August 2026, 7:00pm</p>
<p>Description:Works:<br>Road Part of a Diversion Route</p>
<a href="https://www.traffic.gov.scot/more-details?sid=cSW202669539&type=roadworks">More details</a>
</div>
<div class="views-row">
<h2>M74 J8 - J9 SB - Lane Closures</h2>
<p>Location:M74 (J8 Off Slip to J9 On Slip), Southbound</p>
<p>Start time:20th of July 2026, 8:00pm</p>
<p>Description:Works:<br>Barrier Repair</p>
<a href="https://www.traffic.gov.scot/more-details?sid=cSW202669759&type=roadworks">More details</a>
</div>
<div class="views-row">
<h2>A75 Gatehouse of Fleet Bypass EB - Lane closure</h2>
<p>Location:A75 (Climbing Lane End to B796), Eastbound</p>
<p>Start time:20th of July 2026, 9:00am</p>
<p>Description:Works:<br>Drainage Works</p>
<a href="https://www.traffic.gov.scot/more-details?sid=cSW202669373&type=roadworks">More details</a>
</div>
</body></html>
"""
entries = scot.find_road_entries(real_listing_html, scot.ROAD_ALIASES["M74"])
check("finds 2 (A701/M74 diversion mention + real M74 entry)", len(entries) == 2)
check("A75 correctly excluded", not any("A75" in e["location_text"] for e in entries))

section("traffic_scotland: stage 2 detail page parsing (real captured data)")

real_detail_html = """
<html><body><main>
<h2>Roadwork details</h2>
Location
M74 J8 - J9 SB - Lane Closures
Direction
Southbound
Starting
20th of July 2026, 8:00pm
Ending
18th of September 2026, 6:00am
Roadwork description
Works:
Barrier Repair, Filter Drain
Traffic Management:
Lane Closure (40mph)
</main>
Did you find what you were looking for?
</body></html>
"""
entries = scot.parse_detail_page(
    real_detail_html, "https://www.traffic.gov.scot/more-details?sid=cSW202669759&type=roadworks",
)
check("single fallback row (no Activity Periods)", len(entries) == 1)
check("location matches real page", entries[0]["location_description"] == "M74 J8 - J9 SB - Lane Closures")
check("real end date captured", entries[0]["end_datetime"] == "2026-09-18T06:00:00")

section("traffic_scotland: Activity Periods parsing + midnight merge (real data)")

days_times_text = """
Week commencing 17th Aug
Activity PeriodsExpand
- Thu 20th Aug - 22:00 to 23:59
- Fri 21st Aug - 00:00 to 06:00
"""
raw = scot.parse_activity_periods(days_times_text, "2026-08-02T22:00:00", "2026-09-04T06:00:00")
check("finds 2 raw periods", len(raw) == 2)
merged = scot.merge_adjacent_periods(raw)
check("merges into one period across the midnight boundary", len(merged) == 1)
check("merged span is Thu 22:00 -> Fri 06:00", merged[0] == ("2026-08-20T22:00:00", "2026-08-21T06:00:00"))

section("traffic_scotland: parse_calendar_grid_periods (real M74 J9 Offslip case -- empty Activity Periods list)")

_grid_j9_text = """
Week commencing 17th Aug
MonTueWedThuFriSatSun
MTWTFSS
Early Morning (00:00 - 06:00)
\u25cf
Monday.
Activity PeriodsExpand
"""
check(
    "the real J9 entry's Activity Periods list is genuinely empty (confirms this needs the fallback)",
    scot.parse_activity_periods(_grid_j9_text, "2026-08-02T22:00:00", "2026-09-04T06:00:00") == [],
)
_grid_periods = scot.parse_calendar_grid_periods(_grid_j9_text, "2026-08-02T22:00:00", "2026-09-04T06:00:00")
check("calendar grid fallback finds exactly 1 period", len(_grid_periods) == 1)
check(
    "correctly computed as Monday 17 Aug 00:00-06:00 (17 Aug 2026 is a Monday)",
    _grid_periods[0] == ("2026-08-17T00:00:00", "2026-08-17T06:00:00"),
)

_real_j9_detail_html = """
<html><body><main>
<h2>Roadwork details</h2>
Location
M74 J9 Offslip SB -Total Closure
Direction
Southbound
Starting
2nd of August 2026, 10:00pm
Ending
4th of September 2026, 6:00am
Days & times affected
Week commencing 17th Aug
MonTueWedThuFriSatSun
MTWTFSS
Early Morning (00:00 - 06:00)
\u25cf
Monday.
Activity PeriodsExpand
Roadwork description
Works:
Barrier Repair, Filter Drain, Inspections, Sign Installation/Repairs
Traffic Management:
Road Closure.
Diversion Information:
Follow mainline closure
</main>
Did you find what you were looking for?
</body></html>
"""
_j9_entries = scot.parse_detail_page(
    _real_j9_detail_html, "https://www.traffic.gov.scot/more-details?sid=cSW202669763&type=roadworks",
)
check("exactly ONE row produced (not the misleading 5-week span)", len(_j9_entries) == 1)
check(
    "shows the real answer -- Monday 17 Aug 00:00-06:00, not '02 Aug -> 04 Sep'",
    _j9_entries[0]["start_datetime"] == "2026-08-17T00:00:00"
    and _j9_entries[0]["end_datetime"] == "2026-08-17T06:00:00",
)

section("traffic_scotland: REGRESSION -- calendar grid fallback never overrides a populated Activity Periods list")

_grid_j8j10_text = """
Week commencing 17th Aug
MonTueWedThuFriSatSun
MTWTFSS
Early Morning (00:00 - 06:00)
    \u25cf
Friday.
Evening (18:00 - 00:00)
   \u25cf
Thursday.
Activity PeriodsExpand
- Thu 20th Aug - 22:00 to 23:59
- Fri 21st Aug - 00:00 to 06:00
"""
_raw_j8j10 = scot.parse_activity_periods(_grid_j8j10_text, "2026-08-02T22:00:00", "2026-09-04T06:00:00")
check("the precise Activity Periods list is found first and used, unaffected by the new fallback",
      len(_raw_j8j10) == 2)
check(
    "precise times preserved (22:00-23:59) rather than the coarser Evening band (18:00-00:00)",
    _raw_j8j10[0] == ("2026-08-20T22:00:00", "2026-08-20T23:59:00"),
)

section("traffic_scotland: clean_field_text (paren spacing + share-widget boilerplate)")

check(
    "'Lane Closure( 40mph)' -> 'Lane Closure (40mph)' (source-data spacing quirk)",
    scot.clean_field_text("Lane Closure( 40mph)") == "Lane Closure (40mph)",
)
check(
    "already-correct spacing is left unchanged (idempotent)",
    scot.clean_field_text("Lane Closure (40mph)") == "Lane Closure (40mph)",
)
check(
    "share-widget boilerplate stripped from trailing text",
    scot.clean_field_text(
        "Leave the motorway at J22\nShare\nLink for sharing\nCopy link"
    ) == "Leave the motorway at J22",
)
check(
    "a legitimate word like 'shared' is NOT falsely stripped",
    scot.clean_field_text("Diversion via the shared path to the north")
    == "Diversion via the shared path to the north",
)

section("traffic_scotland: extract_tm_for_date (per-date Traffic Management lists)")

tm_list = (
    "15/10/2024 - Portable Traffic Lights (TTLS), "
    "16/10/2024 - Portable Traffic Lights (TTLS), "
    "05/11/2025 - No Obstruction on Carriageway or Footway( 40mph)"
)
check(
    "picks the entry matching the target date, date prefix stripped and paren spacing fixed",
    scot.extract_tm_for_date(tm_list, "2025-11-05T00:00:00")
    == "No Obstruction on Carriageway or Footway (40mph)",
)
check(
    "unmatched date falls back to the first entry, not the whole raw list",
    scot.extract_tm_for_date(tm_list, "2099-01-01T00:00:00") == "Portable Traffic Lights (TTLS)",
)
check(
    "plain single-value TM text (the common case) passed through unchanged",
    scot.extract_tm_for_date("Lane Closure (40mph)", "2026-08-20T22:00:00") == "Lane Closure (40mph)",
)

section("traffic_scotland: parse_detail_page routes TM to lane_info, keeps only Diversion in comment")

decluttered_html = """
<html><body><main>
<h2>Roadwork details</h2>
Location
M74 J8 - J10 SB - Total Closure
Direction
Southbound
Starting
2nd of August 2026, 10:00pm
Ending
4th of September 2026, 6:00am
Days & times affected
Week commencing 17th Aug
Activity PeriodsExpand
- Thu 20th Aug - 22:00 to 23:59
- Fri 21st Aug - 00:00 to 06:00
Roadwork description
Works:
Barrier Repair, Filter Drain
Traffic Management:
Road Closure.
Diversion Information:
S/B traffic to exit M74 at junction 8, re-join the M74 at J10 S/B
</main>
Did you find what you were looking for?
</body></html>
"""
entries = scot.parse_detail_page(
    decluttered_html, "https://www.traffic.gov.scot/more-details?sid=cSW202669760&type=roadworks",
)
check("one merged row produced", len(entries) == 1)
e = entries[0]
check("location_description stays clean", e["location_description"] == "M74 J8 - J10 SB - Total Closure")
check("comment holds ONLY diversion, not Works/TM", e["comment"] == (
    "Diversion: S/B traffic to exit M74 at junction 8, re-join the M74 at J10 S/B"
))
check("lane_info holds the Traffic Management text", e["lane_info"] == "Road Closure.")
check("cause_type still holds Works text for the Cause column",
      e["cause_type"] == "Barrier Repair, Filter Drain")

section("traffic_scotland: parse_detail_page strips share-widget boilerplate that leaks in as trailing text")

boilerplate_leak_html = """
<html><body><main>
<h2>Roadwork details</h2>
Location
M74 (Gretna Nth Slip to Gretna Int O'Bridge), Northbound
Direction
Northbound
Starting
20th of August 2026, 8:00pm
Ending
21st of August 2026, 6:00am
Roadwork description
Works:
Lining Works
Traffic Management:
Road Closure( immediate)
Diversion Information:
Leave the motorway at J22 - A75 - turn right on to Glasgow Road
Share
Link for sharing
Copy link
</main>
Did you find what you were looking for?
</body></html>
"""
entries = scot.parse_detail_page(
    boilerplate_leak_html, "https://www.traffic.gov.scot/more-details?sid=cSWTEST&type=roadworks",
)
check("one entry parsed", len(entries) == 1)
check("share-widget boilerplate NOT in comment",
      "Share" not in entries[0]["comment"] and "Copy link" not in entries[0]["comment"])
check("diversion text stays intact", "Leave the motorway at J22" in entries[0]["comment"])
check("Traffic Management paren spacing fixed", entries[0]["lane_info"] == "Road Closure (immediate)")


section("traffic_scotland: isolate_road_segment (cross-road junction contamination guard)")

_m74_aliases = scot.ROAD_ALIASES["M74"]

check(
    "real rogue example: M8's 'Jct 22' stripped out, M74's own 'Jct 3a' kept",
    scot.isolate_road_segment(
        "M8 (Sec C/Way Jct 22) to M74 SB (Sec C/Way Jct 3a), Eastbound", _m74_aliases
    ) == "M74 SB (Sec C/Way Jct 3a), Eastbound",
)
check(
    "single-road text is left completely unchanged",
    scot.isolate_road_segment("M74 J8 - J9 SB - Lane Closures", _m74_aliases)
    == "M74 J8 - J9 SB - Lane Closures",
)
check(
    "legitimate cross-road entry with M74's OWN real junctions still isolates correctly",
    scot.isolate_road_segment("M8 (Some Slip) to M74 (Jct 15 to Jct 16), Southbound", _m74_aliases)
    == "M74 (Jct 15 to Jct 16), Southbound",
)
check(
    "target road absent from text -> unchanged (safe fallback, nothing to isolate)",
    scot.isolate_road_segment("M8 (Jct 10) to A80 (Jct 5), Northbound", _m74_aliases)
    == "M8 (Jct 10) to A80 (Jct 5), Northbound",
)

section("traffic_scotland: fetch_from_traffic_scotland excludes an M8/M74 entry whose only "
        "in-range 'junction' actually belongs to the M8, not M74 (real reported case)")

rogue_listing_html = """
<html><body><div class="views-row">
<h2>M8 EB Sec c/way Jct 22 to M74 SB Jct 3a - Mobile Lane Closures</h2>
<p>Location:M8 (Sec C/Way Jct 22) to M74 (Sec C/Way Jct 3a), Eastbound</p>
<p>Start time:11th of May 2026, 8:00pm</p>
<p>Description:Works:<br>Cyclic Maintenance, Pothole Repairs</p>
<a href="https://www.traffic.gov.scot/more-details?sid=cSWROGUE2&type=roadworks">More details</a>
</div></body></html>
"""
rogue_detail_html = """
<html><body><main>
<h2>Roadwork details</h2>
Location
M8 (Sec C/Way Jct 22) to M74 SB (Sec C/Way Jct 3a)
Direction
Eastbound
Starting
11th of May 2026, 8:00pm
Ending
12th of May 2026, 6:00am
Roadwork description
Works:
Cyclic Maintenance, Pothole Repairs
Traffic Management:
Mobile Lane Closures.
</main>
Did you find what you were looking for?
</body></html>
"""


def fake_fetch_text_rogue2(url, headers=None):
    if "planned-roadworks" in url:
        return "<html><body>none</body></html>"
    if "more-details" in url:
        return rogue_detail_html
    return rogue_listing_html


_original_fetch_text2 = scot.fetch_text
scot.fetch_text = fake_fetch_text_rogue2
try:
    rogue2_results = scot.fetch_from_traffic_scotland("M74")
finally:
    scot.fetch_text = _original_fetch_text2

check("entry passes through with location isolated to M74's own segment",
      len(rogue2_results) == 1 and "M8" not in rogue2_results[0]["location_description"])
rogue2_rows = matching.rows_for_leg(rogue2_results, "M74", "Eastbound", 8, 22)
check(
    "correctly EXCLUDED from the M74 J8-22 leg -- M8's Jct 22 no longer "
    "masquerades as an in-range M74 junction; the real M74 junction (3A) is out of range",
    len(rogue2_rows) == 0,
)


section("traffic_scotland: compute_validity_status (real-time active/planned)")

from datetime import datetime as _dt
_now = _dt(2026, 8, 20, 12, 0, 0)

check(
    "now within [start, end] -> active",
    scot.compute_validity_status("2026-08-20T08:00:00", "2026-08-20T18:00:00", _now, fallback="planned") == "active",
)
check(
    "now before the window -> planned",
    scot.compute_validity_status("2026-08-25T08:00:00", "2026-08-25T18:00:00", _now, fallback="active") == "planned",
)
check(
    "now after the window -> planned (per spec: active only during, planned if outwith)",
    scot.compute_validity_status("2026-08-10T08:00:00", "2026-08-10T18:00:00", _now, fallback="active") == "planned",
)
check(
    "boundaries are inclusive (exactly at start/end still counts as active)",
    scot.compute_validity_status("2026-08-20T12:00:00", "2026-08-20T18:00:00", _now, fallback="planned") == "active"
    and scot.compute_validity_status("2026-08-20T08:00:00", "2026-08-20T12:00:00", _now, fallback="planned") == "active",
)
check(
    "start present but end missing -> compares against start alone, not a blind fallback",
    scot.compute_validity_status("2026-08-20T08:00:00", "", _now, fallback="planned") == "active"  # now is after start
    and scot.compute_validity_status("2026-08-25T08:00:00", "", _now, fallback="active") == "planned",  # now is before start
)
check(
    "even start missing/unparseable -> falls back to the listing-page status (nothing real left to compare)",
    scot.compute_validity_status("", "", _now, fallback="active") == "active"
    and scot.compute_validity_status("not-a-date", "also-not-a-date", _now, fallback="planned") == "planned",
)

section("traffic_scotland: fetch_from_traffic_scotland overrides a stale listing-page label with real status")

live_listing_html = """
<html><body><div class="views-row">
<h2>M74 J8 - J9 SB - Currently Happening</h2>
<p>Location:M74 (J8 Off Slip to J9 On Slip), Southbound</p>
<p>Start time:20th of July 2026, 8:00pm</p>
<p>Description:Works:<br>Barrier Repair</p>
<a href="https://www.traffic.gov.scot/more-details?sid=cSWLIVE&type=roadworks">More details</a>
</div></body></html>
"""
live_detail_html = """
<html><body><main>
<h2>Roadwork details</h2>
Location
M74 J8 - J9 SB - Currently Happening
Direction
Southbound
Starting
1st of January 2020, 12:00am
Ending
1st of January 2099, 12:00am
Roadwork description
Works:
Barrier Repair
Traffic Management:
Lane Closure (40mph)
</main>
Did you find what you were looking for?
</body></html>
"""


def fake_fetch_text_live(url, headers=None):
    if "planned-roadworks" in url:
        return live_listing_html  # deliberately found on the "planned" page
    if "more-details" in url:
        return live_detail_html
    return "<html><body>none</body></html>"


_original_fetch_text = scot.fetch_text
scot.fetch_text = fake_fetch_text_live
try:
    live_results = scot.fetch_from_traffic_scotland("M74")
finally:
    scot.fetch_text = _original_fetch_text

check("entry found", len(live_results) == 1)
check(
    "status is 'active' based on the real time window, not the stale 'planned' page it came from",
    live_results[0]["validity_status"] == "active",
)


section("matching: lane_info flows through rows_for_leg into the rendered row")

closure_with_lane_info = {
    "record_id": "x", "road_name": "M74", "direction": "Southbound",
    "location_description": "M74 J8 - J10 SB", "comment": "",
    "lane_info": "Road Closure.",
    "start_datetime": "2026-08-20T22:00:00", "end_datetime": "2026-08-21T06:00:00",
    "validity_status": "planned", "cause_type": "Barrier Repair",
    "lanes_restricted": None, "lanes_operational": None, "source_label": "test",
}
rows = matching.rows_for_leg([closure_with_lane_info], "M74", "Southbound", 8, 22)
check("row.lane_info populated", rows[0]["lane_info"] == "Road Closure.")

nh_closure_unaffected = {
    "record_id": "y", "road_name": "M6", "direction": "southBound",
    "location_description": "M6 southbound J40 to J39", "comment": "",
    "start_datetime": "2026-08-20T00:00:00", "end_datetime": "",
    "validity_status": "active", "cause_type": "roadMaintenance",
    "lanes_restricted": 1, "lanes_operational": 2, "source_label": "Live API",
}
rows = matching.rows_for_leg([nh_closure_unaffected], "M6", "southBound", 45, 26)
check("National Highways rows unaffected (no lane_info key -> falls back to numeric lanes)",
      rows[0]["lane_info"] == "" and rows[0]["lanes_restricted"] == 1)




rogue_listing_html = """
<html><body><div class="views-row">
<h2>M8/M74 Interchange Closure</h2>
<p>Location:M8 (Slip Off M8 Wb) to M74 (Interchange), Southbound</p>
<p>Start time:25th of August 2026, 8:00pm</p>
<p>Description:Works:<br>Grass Cutting</p>
<a href="https://www.traffic.gov.scot/more-details?sid=cROGUE&type=roadworks">More details</a>
</div></body></html>
"""
rogue_detail_html = """
<html><body><main>
<h2>Roadwork details</h2>
Location
M8/M74 Interchange - Southbound
Direction
Southbound
Starting
25th of August 2026, 8:00pm
Ending
26th of August 2026, 6:00am
Roadwork description
Works:
Grass Cutting
Traffic Management:
Road Closure. Diversion via jct 21 and jct 23
</main>
Did you find what you were looking for?
</body></html>
"""


def fake_fetch_text(url, headers=None):
    if "planned-roadworks" in url:
        return "<html><body>none</body></html>"
    if "sid=cROGUE" in url:
        return rogue_detail_html
    if "more-details" not in url:
        return rogue_listing_html
    raise AssertionError(f"unexpected URL in test: {url}")


_original_fetch_text = scot.fetch_text
scot.fetch_text = fake_fetch_text
try:
    results = scot.fetch_from_traffic_scotland("M74")
finally:
    scot.fetch_text = _original_fetch_text

check(
    "rogue M8/M74 entry (M8's junctions, not M74's) is excluded",
    len(results) == 0,
)


section("build: content_hash (cache-busting for static assets)")

import tempfile as _tempfile
with _tempfile.NamedTemporaryFile(suffix=".css", delete=False) as f:
    f.write(b"body { color: red; }")
    tmp_path = build.Path(f.name)
hash1 = build.content_hash(tmp_path)
check("hash is a short deterministic string", len(hash1) == 10 and hash1 == build.content_hash(tmp_path))

tmp_path.write_bytes(b"body { color: blue; }")
hash2 = build.content_hash(tmp_path)
check("hash changes when file content changes (forces a fresh fetch)", hash1 != hash2)
tmp_path.unlink()


# =======================================================================
# sources/travel_alerts.py
# =======================================================================

section("travel_alerts: strip_other_road_junctions (real screenshot titles)")

check(
    "M27's junction is stripped from an A31 title (belongs to a different road)",
    "J2" not in ta.strip_other_road_junctions(
        "A31 - Between M27 J2 and A338 - Road Closure", "A31"
    ),
)
check(
    "bare junctions with no road prefix are left completely unchanged",
    ta.strip_other_road_junctions("M6 - Between J15 and J16 - Carriageway Closure", "M6")
    == "M6 - Between J15 and J16 - Carriageway Closure",
)
check(
    "cleaned A31 text has no extractable junction number (correctly excluded from range matching)",
    matching._junctions_in_text(
        ta.strip_other_road_junctions("A31 - Between M27 J2 and A338 - Road Closure", "A31")
    ) == [],
)
check(
    "cleaned M6 text correctly yields junctions 15 and 16",
    matching._junctions_in_text(
        ta.strip_other_road_junctions("M6 - Between J15 and J16 - Carriageway Closure", "M6")
    ) == [15, 16],
)

section("travel_alerts: parse_alert_cards + fetch_from_travel_alerts (real page structure + real content)")

# The real page turned out to wrap each ENTIRE card (title + subtitle +
# "More details") in one <a> with nested child elements -- NOT a simple
# separate "More details" link like earlier synthetic fixtures assumed.
# That assumption is exactly what let a real bug slip through undetected:
# find_all("a", string=lambda...) silently matches nothing when an anchor
# has multiple children, since BeautifulSoup's .string only works for a
# tag with exactly one text-only child. A live run found "0 total alerts"
# on a day the page genuinely had 4 -- this fixture uses the real,
# confirmed structure and the real 4 alerts from that same live check, so
# this test would actually have caught the bug it's guarding against.
_ta_listing_html = """
<html><body>
<a href="/roads-and-travel/live-travel-updates/travel-alerts/a31-hampshire-both-directions-fire-road-closed-between-m27-j2-and-a338/">
  <h2>A31 - Between M27 J2 and A338 - Road Closure</h2>
  <p>Hampshire - Off strategic network incident - Expect Delays - Both directions</p>
  <span>More details</span>
</a>
<a href="/roads-and-travel/live-travel-updates/travel-alerts/a303-somersetdevon-westbound-road-closed-hgv-fire-between-a358-and-a30/">
  <h2>A303 - Between A358 and A30 - Road Closure</h2>
  <p>Somerset - Vehicle fire - Expect Delays - Westbound</p>
  <span>More details</span>
</a>
<a href="/roads-and-travel/live-travel-updates/travel-alerts/a45-northamptonshire-both-directions-road-closed-between-a43-and-a509/">
  <h2>A45 - Between A43 and A509 - Road Closure</h2>
  <p>Northamptonshire - Police led incident - Expect Delays - Both directions</p>
  <span>More details</span>
</a>
<a href="/roads-and-travel/live-travel-updates/travel-alerts/m1-northamptonshire-northbound-collision-carriageway-closed-between-j16-and-j18/">
  <h2>M1 - Between J16 and J18 - Carriageway Closure</h2>
  <p>Northamptonshire - Road traffic collision - Expect Delays - Northbound</p>
  <span>More details</span>
</a>
<!-- Synthetic (not on the page the day this was checked) -- M1 isn't in
     this project's own route config, so this keeps a test exercising a
     road that actually is (M6), using the same real nested structure. -->
<a href="/roads-and-travel/live-travel-updates/travel-alerts/m6-staffordshire-northbound-road-traffic-collision-carriageway-closed-j15-j16/">
  <h2>M6 - Between J15 and J16 - Carriageway Closure</h2>
  <p>Staffordshire - Road traffic collision - Expect Delays - Northbound</p>
  <span>More details</span>
</a>
</body></html>
"""

_ta_cards = ta.parse_alert_cards(_ta_listing_html)
check("finds all 5 alerts (4 real + 1 synthetic M6)", len(_ta_cards) == 5)
_ta_m1 = next(c for c in _ta_cards if c["title"].startswith("M1"))
check(
    "title and subtitle correctly isolated from a nested anchor (not merged into one blob)",
    _ta_m1["title"] == "M1 - Between J16 and J18 - Carriageway Closure"
    and _ta_m1["subtitle"] == "Northamptonshire - Road traffic collision - Expect Delays - Northbound",
)


class _FakeTaResp:
    def __init__(self, data):
        self._data = data.encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


import urllib.request as _urllib_request

_original_ta_urlopen = _urllib_request.urlopen
_urllib_request.urlopen = lambda req, timeout=60: _FakeTaResp(_ta_listing_html)
try:
    _ta_m6_results = ta.fetch_from_travel_alerts("M6")
    _ta_a31_results = ta.fetch_from_travel_alerts("A31")
finally:
    _urllib_request.urlopen = _original_ta_urlopen

check("M6 filter finds exactly the real M6 collision", len(_ta_m6_results) == 1)
_ta_m6 = _ta_m6_results[0]
check("direction correctly extracted", _ta_m6["direction"] == "Northbound")
check("cause correctly extracted", _ta_m6["cause_type"] == "Road traffic collision")
check("validity_status is always 'active'", _ta_m6["validity_status"] == "active")
check(
    "no start/end time -- honest representation, since the source has none",
    _ta_m6["start_datetime"] == "" and _ta_m6["end_datetime"] == "",
)
check("source_label correctly set", _ta_m6["source_label"] == "Travel Alert (major incident)")

_ta_rows = matching.rows_for_leg(_ta_m6_results, "M6", "Northbound", 15, 20)
check("correctly matches an M6 J15-20 style leg", len(_ta_rows) == 1)

check("A31 filter also finds exactly 1", len(_ta_a31_results) == 1)
check(
    "A31's cleaned location has no usable junction (M27's correctly stripped)",
    matching._junctions_in_text(_ta_a31_results[0]["location_description"]) == [],
)

section("matching: BOTH_DIRECTIONS_VALUES wildcard (real Travel Alerts case)")

_both_directions_closure = {
    "road_name": "M6", "direction": "Both directions",
    "location_description": "M6 between J15 and J16",
}
check(
    "'Both directions' matches a Northbound leg",
    matching.closure_matches_leg(_both_directions_closure, "M6", "northBound", 15, 20),
)
check(
    "'Both directions' ALSO matches a Southbound leg (same closure, relevant to both)",
    matching.closure_matches_leg(_both_directions_closure, "M6", "southBound", 15, 20),
)
_normal_directional_closure = {
    "road_name": "M6", "direction": "southBound", "location_description": "M6 J40 to J39",
}
check(
    "normal single-direction closures are unaffected by the wildcard",
    matching.closure_matches_leg(_normal_directional_closure, "M6", "southBound", 45, 26)
    and not matching.closure_matches_leg(_normal_directional_closure, "M6", "northBound", 45, 26),
)
check(
    "blank/missing direction is still NOT treated as a wildcard (unchanged existing behavior)",
    not matching.closure_matches_leg(
        {"road_name": "M6", "direction": "", "location_description": "M6 J40"},
        "M6", "southBound", 45, 26,
    ),
)
check(
    "'Northbound & Southbound' (Traffic Scotland's own phrasing, seen on a real A9 closure) "
    "is ALSO treated as a wildcard, matching either direction",
    matching.closure_matches_leg(
        {"road_name": "M74", "direction": "Northbound & Southbound", "location_description": "M74 J5"},
        "M74", "Northbound", 1, 10,
    )
    and matching.closure_matches_leg(
        {"road_name": "M74", "direction": "Northbound & Southbound", "location_description": "M74 J5"},
        "M74", "Southbound", 1, 10,
    ),
)


# =======================================================================
# sources/scotland_incidents.py
# =======================================================================

section("scotland_incidents: date parsing (real examples from the live page)")

for text, expected in [
    ("17th of August 2026, 8:14am", "2026-08-17T08:14:00"),
    ("17th of August 2026, 8:29am", "2026-08-17T08:29:00"),
    ("10th of August 2026, 11:37am", "2026-08-10T11:37:00"),
    ("19th of July 2026, 11:02pm", "2026-07-19T23:02:00"),
]:
    check(f"{text!r} -> {expected}", si.parse_scottish_datetime(text) == expected)

section("scotland_incidents: strip_other_road_junctions (real hyphenated case)")

check(
    "M8's own hyphenated junction ('M8-J29') is kept when M8 IS the target",
    "J29" in si.strip_other_road_junctions("A737 M8-J29 North - Slip Off", "M8"),
)
check(
    "M8's hyphenated junction is stripped when A737 is the target instead",
    "J29" not in si.strip_other_road_junctions("A737 M8-J29 North - Slip Off", "A737"),
)

section("scotland_incidents: junction extraction from real M74 headings")

for heading, expected in [
    ("M74 J5 (Raith) North - slip off", [5]),
    ("M74 J2a (Fullarton Road Junction)", [2]),
    ("M74 J5 (Raith) South - slip off", [5]),
]:
    cleaned = si.strip_other_road_junctions(heading, "M74")
    check(f"{heading!r} -> junctions {expected}", matching._junctions_in_text(cleaned) == expected)

section("scotland_incidents: _INCIDENT_BLOCK_RE splits start-time text from the free-form detail line")

_si_block = (
    "M74 J5 (Raith) North - slip off\n"
    "Direction:Northbound\n"
    "Incident type:Queue\n"
    "Start time:17th of August 2026, 8:29am\n"
    "3 lanes restricted Northbound\n"
    "More details"
)
_si_html = f"""
<html><body><div class="incident-card">
<h2>M74 J5 (Raith) North - slip off</h2>
<p>Direction:Northbound</p>
<p>Incident type:Queue</p>
<p>Start time:17th of August 2026, 8:29am</p>
<p>3 lanes restricted Northbound</p>
<a href="/more-details?sid=c502270&type=incidents">More details</a>
</div></body></html>
"""
_si_cards = si.parse_incident_cards(_si_html)
check("one card parsed", len(_si_cards) == 1)
check(
    "start_text and detail correctly split at the date boundary "
    "(a real bug caught here: two adjacent non-greedy regex groups with no "
    "label between them took just the character '1' as the start time)",
    _si_cards[0]["start_text"] == "17th of August 2026, 8:29am"
    and _si_cards[0]["detail"] == "3 lanes restricted Northbound",
)

section("scotland_incidents: fetch_from_scotland_incidents (real listing page content)")

_si_listing_html = """
<html><body>
<div class="incident-card">
<h2>M74 J5 (Raith) North - slip off</h2>
<p>Direction:Northbound</p>
<p>Incident type:Queue</p>
<p>Start time:17th of August 2026, 8:29am</p>
<p>3 lanes restricted Northbound</p>
<a href="/more-details?sid=c502270&type=incidents">More details</a>
</div>
<div class="incident-card">
<h2>M74 J5 (Raith) South - slip off</h2>
<p>Direction:Southbound</p>
<p>Incident type:Queue</p>
<p>Start time:17th of August 2026, 7:43am</p>
<p>3 lanes restricted Southbound</p>
<a href="/more-details?sid=c502261&type=incidents">More details</a>
</div>
<div class="incident-card">
<h2>M74 J2a (Fullarton Road Junction)</h2>
<p>Direction:Northbound</p>
<p>Incident type:Queue</p>
<p>Start time:17th of August 2026, 7:44am</p>
<p>3 lanes restricted Northbound</p>
<a href="/more-details?sid=c502260&type=incidents">More details</a>
</div>
<div class="incident-card">
<h2>A9 Alness</h2>
<p>Direction:Northbound & Southbound</p>
<p>Incident type:Closure</p>
<p>Start time:17th of August 2026, 8:00am</p>
<p>The A9 at Alness is closed in both directions, due to a road traffic incident.</p>
<a href="/more-details?sid=c502264&type=incidents">More details</a>
</div>
<div class="incident-card">
<h2>M77 J2 North - Slip On</h2>
<p>Direction:Northbound</p>
<p>Incident type:Queue</p>
<p>Start time:17th of August 2026, 8:14am</p>
<p>2 lanes restricted Northbound</p>
<a href="/more-details?sid=c502269&type=incidents">More details</a>
</div>
</body></html>
"""


class _FakeSiResp:
    def __init__(self, data):
        self._data = data.encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


import urllib.request as _urllib_request_si

_original_si_urlopen = _urllib_request_si.urlopen
_urllib_request_si.urlopen = lambda req, timeout=60: _FakeSiResp(_si_listing_html)
try:
    _si_m74_results = si.fetch_from_scotland_incidents("M74")
    _si_a9_results = si.fetch_from_scotland_incidents("A9")
finally:
    _urllib_request_si.urlopen = _original_si_urlopen

check("M74 filter finds exactly the 3 real M74 entries", len(_si_m74_results) == 3)
check(
    "M77 correctly excluded (different road, not a substring false-match)",
    not any("M77" in r["location_description"] for r in _si_m74_results),
)

_si_raith_north = next(
    r for r in _si_m74_results if "J5" in r["location_description"] and r["direction"] == "Northbound"
)
check("lanes_restricted correctly extracted as a real number", _si_raith_north["lanes_restricted"] == 3)
check("start_datetime correctly parsed", _si_raith_north["start_datetime"] == "2026-08-17T08:29:00")
check("end_datetime empty -- honest, no end time in the source", _si_raith_north["end_datetime"] == "")
check("validity_status always 'active'", _si_raith_north["validity_status"] == "active")
check("cause_type is the incident type", _si_raith_north["cause_type"] == "Queue")
check("source_label correctly set", _si_raith_north["source_label"] == "Traffic Scotland Incident")

check("A9 filter finds the one closure entry", len(_si_a9_results) == 1)
_si_a9 = _si_a9_results[0]
check(
    "A9's lanes_restricted is None (detail was a description, not a lane count)",
    _si_a9["lanes_restricted"] is None,
)
check(
    "A9's free-text description correctly stored in comment instead",
    "closed in both directions" in _si_a9["comment"],
)

_si_rows = matching.rows_for_leg(_si_m74_results, "M74", "Northbound", 1, 10)
check("correctly matches an M74 J1-10 style leg (2 real northbound M74 entries)", len(_si_rows) == 2)


# =======================================================================
# sources/national_highways_traffic_search.py
# =======================================================================

section("national_highways_traffic_search: normalize_record (real production JSON)")

_nhts_real_json = json.loads('''
{"data":[
  {"id":"8e040511-58f5-4bac-b7af-91deb9adf3f4","road":"M6","region":"West Midlands",
   "title":"M6 northbound within J9","location":"The M6 northbound between junctions J9 and J11",
   "reason":"Congestion","returnToNormal":"2026-08-16T17:26:51","timeToClear":null,
   "status":"active","delay":662,"direction":"N","type":"AbnormalTraffic","laneClosures":null,
   "period":"[]","lanesClosed":"0|0|0","carriageway":"A","situationId":3442162,
   "createdDate":"2026-08-16T16:17:46.683","updatedDate":"2026-08-16T16:50:49.55","version":28,
   "isActive":true,"lanesClosedText":"","returnToNormalText":"Normal traffic conditions are expected between 17:30 and 17:45 on 16 August 2026",
   "timeToClearText":"","delayText":"There are currently delays of 12 minutes against expected traffic",
   "laneClosuresText":"","periodText":""},
  {"id":"63f72c85-5cba-485b-9b41-ec5289cd4a6b","road":"M6","region":"West Midlands",
   "title":"M6 southbound between J16 and J15","location":"The M6 southbound between junctions J16 and J15",
   "reason":"Congestion","returnToNormal":null,"timeToClear":null,"status":"active","delay":2835,
   "direction":"S","type":"AbnormalTraffic","laneClosures":null,"period":"[]","lanesClosed":"0|0|0",
   "carriageway":"B","situationId":3448440,"createdDate":"2026-08-17T03:17:38.9",
   "updatedDate":"2026-08-17T04:59:39.127","version":118,"isActive":true,"lanesClosedText":"",
   "returnToNormalText":"","timeToClearText":"",
   "delayText":"There are currently delays of 48 minutes against expected traffic",
   "laneClosuresText":"","periodText":""}
],"pagination":{"totalItems":2,"currentPage":1,"pageSize":10,"totalPages":1}}
''')

_original_nhts_fetch_page = nhts.fetch_page
nhts.fetch_page = lambda road_name, page, page_size: _nhts_real_json
try:
    _nhts_results = nhts.fetch_from_national_highways_traffic_search("M6")
finally:
    nhts.fetch_page = _original_nhts_fetch_page

check("both real records parsed", len(_nhts_results) == 2)

_nhts_r1 = _nhts_results[0]
check("direction code N mapped to northBound", _nhts_r1["direction"] == "northBound")
check("start_datetime has sub-second precision stripped", _nhts_r1["start_datetime"] == "2026-08-16T16:17:46")
check("end_datetime correctly uses returnToNormal when present", _nhts_r1["end_datetime"] == "2026-08-16T17:26:51")
check("cause_type is the reason field", _nhts_r1["cause_type"] == "Congestion")
check("comment is the delayText field", "delays of 12 minutes" in _nhts_r1["comment"])
check("record_id uses the real UUID", _nhts_r1["record_id"] == "nh-traffic-8e040511-58f5-4bac-b7af-91deb9adf3f4")
check(
    "source_label honestly flags this as the unofficial beta endpoint",
    _nhts_r1["source_label"] == "National Highways Traffic Search (beta)",
)

_nhts_r2 = _nhts_results[1]
check("direction code S mapped to southBound", _nhts_r2["direction"] == "southBound")
check(
    "end_datetime is empty (honest, not guessed) when both returnToNormal and timeToClear are null",
    _nhts_r2["end_datetime"] == "",
)

check(
    "junction extraction works via the SHARED matching.py logic with zero custom parsing needed",
    matching._junctions_in_text(_nhts_r1["location_description"]) == [9, 11]
    and matching._junctions_in_text(_nhts_r2["location_description"]) == [16, 15],
)

_nhts_rows = matching.rows_for_leg(_nhts_results, "M6", "northBound", 8, 12)
check("correctly matches an M6 J8-12 northbound leg", len(_nhts_rows) == 1)

section("national_highways_traffic_search: pagination follows through all pages")

_nhts_page1 = {"data": [{"id": "a", "road": "M6", "direction": "N", "location": "M6 J1"}],
               "pagination": {"totalItems": 3, "currentPage": 1, "pageSize": 1, "totalPages": 3}}
_nhts_page2 = {"data": [{"id": "b", "road": "M6", "direction": "S", "location": "M6 J2"}],
               "pagination": {"totalItems": 3, "currentPage": 2, "pageSize": 1, "totalPages": 3}}
_nhts_page3 = {"data": [{"id": "c", "road": "M6", "direction": "N", "location": "M6 J3"}],
               "pagination": {"totalItems": 3, "currentPage": 3, "pageSize": 1, "totalPages": 3}}
_nhts_pages_by_num = {1: _nhts_page1, 2: _nhts_page2, 3: _nhts_page3}
_nhts_calls = []


def _fake_nhts_paginated(road_name, page, page_size):
    _nhts_calls.append(page)
    return _nhts_pages_by_num[page]


nhts.fetch_page = _fake_nhts_paginated
try:
    _nhts_paged_results = nhts.fetch_from_national_highways_traffic_search("M6", page_size=1)
finally:
    nhts.fetch_page = _original_nhts_fetch_page

check("followed all 3 pages in order", _nhts_calls == [1, 2, 3])
check("all 3 records merged across pages", len(_nhts_paged_results) == 3)

section("national_highways_traffic_search: defensive road-filter")

_nhts_mixed_payload = {
    "data": [
        {"id": "x", "road": "M6", "direction": "N", "location": "M6 J1"},
        {"id": "y", "road": "M62", "direction": "N", "location": "M62 J1"},  # wrong road
    ],
    "pagination": {"totalItems": 2, "currentPage": 1, "pageSize": 10, "totalPages": 1},
}
nhts.fetch_page = lambda road_name, page, page_size: _nhts_mixed_payload
try:
    _nhts_filtered = nhts.fetch_from_national_highways_traffic_search("M6")
finally:
    nhts.fetch_page = _original_nhts_fetch_page

check(
    "a mismatched road is excluded even if the (already server-filtered) API somehow returned one",
    len(_nhts_filtered) == 1 and _nhts_filtered[0]["road_name"] == "M6",
)


# =======================================================================
# sources/tomtom_incidents.py
# =======================================================================

section("tomtom_incidents: normalize_incident (realistic sample payload)")

_tti_feature_m6_no_direction = {
    "type": "Feature",
    "geometry": {"type": "Point", "coordinates": [-2.5, 53.4]},
    "properties": {
        "id": "abc123",
        "iconCategory": 1,
        "events": [{"description": "Accident", "code": 1}],
        "startTime": "2026-08-20T14:05:11.123Z",
        "endTime": None,
        "from": "M6 J20",
        "to": "M6 J21",
        "roadNumbers": ["M6"],
    },
}
_tti_feature_wrong_road = {
    "type": "Feature",
    "geometry": {"type": "Point", "coordinates": [-2.9, 53.6]},
    "properties": {
        "id": "def456",
        "iconCategory": 1,
        "events": [{"description": "Accident"}],
        "startTime": "2026-08-20T14:10:00",
        "endTime": None,
        "from": "M62 J8",
        "to": "M62 J9",
        "roadNumbers": ["M62"],
    },
}
_tti_feature_with_direction_no_id = {
    "type": "Feature",
    "geometry": {"type": "Point", "coordinates": [-2.55, 53.42]},
    "properties": {
        "iconCategory": 128,
        "events": [{"description": "Road closed"}, {"description": "Road closed"}],
        "startTime": "2026-08-20T15:00:00",
        "endTime": "2026-08-20T18:00:00",
        "from": "M6 southbound J19",
        "to": "M6 J18",
        "roadNumbers": ["M6"],
    },
}

_r1 = tti.normalize_incident(_tti_feature_m6_no_direction, "M6")
check("real M6 accident matched (roadNumbers contains M6)", _r1 is not None)
check("record_id uses TomTom's real id when present", _r1["record_id"] == "tomtom-abc123")
check("cause_type/comment is the event description", _r1["cause_type"] == "Accident" and _r1["comment"] == "Accident")
check("location combines from/to", _r1["location_description"] == "M6 M6 J20 to M6 J21")
check("start_datetime has sub-second precision stripped", _r1["start_datetime"] == "2026-08-20T14:05:11")
check("end_datetime empty (honest, not guessed) when TomTom gives none", _r1["end_datetime"] == "")
check(
    "no explicit direction word anywhere in from/to/cause -> defaults to "
    "'Both directions' rather than guessing (see module docstring)",
    _r1["direction"] == "Both directions",
)
check("source_label correctly identifies this as TomTom", _r1["source_label"] == "TomTom Traffic Incident")

check(
    "a feature whose roadNumbers doesn't mention the target road is excluded",
    tti.normalize_incident(_tti_feature_wrong_road, "M6") is None,
)

_r3 = tti.normalize_incident(_tti_feature_with_direction_no_id, "M6")
check(
    "explicit 'southbound' in the from text IS picked up (not defaulted to Both directions)",
    _r3["direction"] == "Southbound",
)
check(
    "missing TomTom id falls back to a coordinates+startTime derived id, not dropped",
    _r3["record_id"].startswith("tomtom-") and "abc123" not in _r3["record_id"],
)
check(
    "duplicate identical event descriptions are deduped, not repeated",
    _r3["cause_type"] == "Road closed",
)

section("tomtom_incidents: junction matching works via shared matching.py logic")

check(
    "junctions extracted correctly from the combined from/to location text",
    matching._junctions_in_text(_r1["location_description"]) == [20, 21],
)
_tti_rows_both = matching.rows_for_leg([_r1], "M6", "northBound", 19, 22)
check(
    "'Both directions' default correctly matches a leg regardless of its "
    "own configured data_direction (northBound here)",
    len(_tti_rows_both) == 1,
)
_tti_rows_south = matching.rows_for_leg([_r1], "M6", "southBound", 19, 22)
check(
    "...and also matches the southbound leg for the same road/range",
    len(_tti_rows_south) == 1,
)

section("tomtom_incidents: fetch_from_tomtom_incidents (missing API key)")

_tti_calls = []
_original_tti_fetch_page = tti.fetch_page
tti.fetch_page = lambda bbox, category_filter, api_key: _tti_calls.append(1) or {"incidents": []}
tti._response_cache.clear()
tti._warned_missing_key = False
try:
    _tti_no_key_result = tti.fetch_from_tomtom_incidents("M6", api_key="")
finally:
    tti.fetch_page = _original_tti_fetch_page

check("missing API key returns an empty list rather than raising", _tti_no_key_result == [])
check("missing API key never even attempts a fetch", _tti_calls == [])

section("tomtom_incidents: shared bbox is fetched once across multiple roads (caching)")

_tti_shared_payload = {"incidents": [_tti_feature_m6_no_direction, _tti_feature_wrong_road]}
_tti_fetch_calls = []


def _fake_tti_fetch_page(bbox, category_filter, api_key):
    _tti_fetch_calls.append((bbox, category_filter))
    return _tti_shared_payload


tti.fetch_page = _fake_tti_fetch_page
tti._response_cache.clear()
try:
    _tti_m6_results = tti.fetch_from_tomtom_incidents("M6", api_key="fake-key", bbox="-3,54,-2,55")
    _tti_m62_results = tti.fetch_from_tomtom_incidents("M62", api_key="fake-key", bbox="-3,54,-2,55")
finally:
    tti.fetch_page = _original_tti_fetch_page

check("the underlying HTTP fetch happened exactly once, not once per road", len(_tti_fetch_calls) == 1)
check("M6 road_name correctly pulled only its own matching incident", len(_tti_m6_results) == 1)
check("M62 road_name correctly pulled only its own matching incident", len(_tti_m62_results) == 1)

section("tomtom_incidents: multiple bboxes are all fetched and merged, with cross-box dedup")

_tti_box_a_payload = {"incidents": [_tti_feature_m6_no_direction]}  # id "abc123"
_tti_box_b_payload = {"incidents": [_tti_feature_m6_no_direction, _tti_feature_with_direction_no_id]}
_tti_multi_calls = []


def _fake_tti_multi_fetch_page(bbox, category_filter, api_key):
    _tti_multi_calls.append(bbox)
    return {"box-a": _tti_box_a_payload, "box-b": _tti_box_b_payload}[bbox]


tti.fetch_page = _fake_tti_multi_fetch_page
tti._response_cache.clear()
try:
    _tti_multi_results = tti.fetch_from_tomtom_incidents(
        "M6", api_key="fake-key", bbox=["box-a", "box-b"],
    )
finally:
    tti.fetch_page = _original_tti_fetch_page

check("both configured bboxes were fetched", _tti_multi_calls == ["box-a", "box-b"])
check(
    "the incident present in BOTH boxes (id 'abc123', simulating boundary overlap) "
    "is only counted once, not twice, in the merged results",
    len(_tti_multi_results) == 2,
)
check(
    "the default bbox param (None) resolves to DEFAULT_BBOXES, not a single box",
    isinstance(tti.DEFAULT_BBOXES, list) and len(tti.DEFAULT_BBOXES) == 3,
)
check(
    "DEFAULT_CATEGORY_FILTER is a comma-separated string of TomTom's real "
    "category names (confirmed from TomTom's own docs), not the OR'd bitmask "
    "integer an earlier version of this module wrongly used and TomTom rejected live",
    tti.DEFAULT_CATEGORY_FILTER == "Accident,DangerousConditions,RoadClosed",
)

section("tomtom_incidents: detect_direction")

check("explicit direction word detected case-insensitively", tti.detect_direction("m6 EASTBOUND j5") == "Eastbound")
check("no direction word anywhere defaults to 'Both directions'", tti.detect_direction("M6 J5 to J6") == "Both directions")
check("empty text defaults to 'Both directions'", tti.detect_direction("") == "Both directions")


# =======================================================================
# templates/route.html: More Info disclosure cell
# =======================================================================

section("route.html: More Info cell renders truly empty (not just whitespace) when there's no comment")

from jinja2 import Environment as _JinjaEnv, FileSystemLoader as _JinjaLoader
_env = _JinjaEnv(loader=_JinjaLoader("templates"))

_row_no_comment = {
    "status": "planned", "icon": "roadworks.png", "location": "M6 J1",
    "comment": "", "lane_info": "", "lanes_restricted": None, "lanes_operational": None,
    "cause": "Road maintenance", "start": "1 Jan", "end": "2 Jan",
    "start_iso": "", "end_iso": "", "source_label": "Feed",
}
_row_with_comment = dict(_row_no_comment, comment="Diversion: Follow mainline closure")
_leg = {"road_name": "M6", "badge_class": "badge-motorway", "junction_from": 1, "junction_to": 2,
        "count": 2, "rows": [_row_no_comment, _row_with_comment]}

_html = _env.get_template("route.html").render(
    site_title="X", route_name="X", direction_label="X", leg_groups=[_leg],
    generated_at="X", feed_updated="X", style_hash="", script_hash="",
)

_more_info_cells = re.findall(r'<td class="more-info-cell"[^>]*>.*?</td>', _html, re.S)
check("both rows produced a more-info-cell", len(_more_info_cells) == 2)

_empty_inner = _more_info_cells[0].split(">", 1)[1].rsplit("<", 1)[0]
check(
    "empty-comment case renders with ZERO characters between the tags -- not just visually "
    "empty, since CSS :empty (used to collapse this cell's padding) requires that exactly; "
    "a real bug caught here: the original template left whitespace/newlines even when the "
    "{% if %} was false, which :empty does not match",
    _empty_inner == "",
)
check(
    "the comment case correctly contains the disclosure with the real comment text",
    "<details>" in _more_info_cells[1] and "Diversion: Follow mainline closure" in _more_info_cells[1],
)
check(
    "Location cell no longer duplicates the comment text (moved to More Info instead)",
    "Diversion: Follow mainline closure" not in re.search(
        r'<td class="location-cell"[^>]*>.*?</td>', _html, re.S
    ).group(0),
)


# =======================================================================
print(f"\n{'=' * 60}")
if FAILURES:
    print(f"{FAILURES} test(s) FAILED")
    sys.exit(1)
print("All tests passed.")
