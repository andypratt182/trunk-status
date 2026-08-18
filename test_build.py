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

import os
import sys
import urllib.error

import matching
import build
from sources import national_highways as nh
from sources import scotland_incidents as si
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
        "M6 southbound between J45 and J44",
        "M6 southbound between J30 and J29",
        "M6 southbound between J27 and J26",
    ],
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

section("matching: rows_for_leg 'near JN' annotation for comment-derived junctions")

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
check("'near J22' annotation added since location itself has no junction", "near J22" in rows[0]["location"])

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

section("travel_alerts: parse_alert_cards + fetch_from_travel_alerts (real screenshot content)")

_ta_listing_html = """
<html><body>
<div class="alert-card">
  <h2>A31 - Between M27 J2 and A338 - Road Closure</h2>
  <p>Hampshire - Off strategic network incident - Expect Delays - Both directions</p>
  <a href="/roads-and-travel/live-travel-updates/travel-alerts/a31-hampshire-both-directions-fire-road-closed-between-m27-j2-and-a338/">More details</a>
</div>
<div class="alert-card">
  <h2>M6 - Between J15 and J16 - Carriageway Closure</h2>
  <p>Staffordshire - Road traffic collision - Expect Delays - Northbound</p>
  <a href="/roads-and-travel/live-travel-updates/travel-alerts/m6-staffordshire-northbound-road-traffic-collision-carriageway-closed-j15-j16/">More details</a>
</div>
<div class="alert-card">
  <h2>A303 - Between A358 and A30 - Road Closure</h2>
  <p>Somerset - Vehicle fire - Expect Delays - Both directions</p>
  <a href="/roads-and-travel/live-travel-updates/travel-alerts/a303-somerset-both-directions-vehicle-fire-road-closed/">More details</a>
</div>
</body></html>
"""

_ta_cards = ta.parse_alert_cards(_ta_listing_html)
check("finds all 3 real alerts from the screenshot", len(_ta_cards) == 3)
check(
    "titles and subtitles parsed correctly",
    _ta_cards[1]["title"] == "M6 - Between J15 and J16 - Carriageway Closure"
    and _ta_cards[1]["subtitle"] == "Staffordshire - Road traffic collision - Expect Delays - Northbound",
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
print(f"\n{'=' * 60}")
if FAILURES:
    print(f"{FAILURES} test(s) FAILED")
    sys.exit(1)
print("All tests passed.")
