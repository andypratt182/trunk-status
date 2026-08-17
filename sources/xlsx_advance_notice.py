"""
National Highways' public "7-day closure report" XLSX (advance notice of
FULL closures, published before VSS signs are activated -- so it can show
closures days before they'd appear via the API, which only reports what's
currently signed on the road). See:
https://nationalhighways.co.uk/roads-and-travel/live-travel-updates/road-closure-report/

IMPORTANT: this file's exact column layout has not been verified against
a real download (no network access was available to inspect it while
writing this). The parser below matches column headers flexibly by
keyword and logs exactly what it finds in every sheet, so if the guesses
below are wrong, the build log will show the real headers to fix them
against, and the site still builds fine from the other source(s) in the
meantime (a parsing failure here is a warning, not a fatal error).
"""
from __future__ import annotations

import io
import re
import urllib.error
import urllib.request
from datetime import date as date_cls
from datetime import datetime

XLSX_HEADER_SYNONYMS: dict[str, set[str]] = {
    "road_name": {"road", "roadname", "route", "roadnumber"},
    "direction": {"direction"},
    "location_description": {
        "location", "locationdescription", "section", "extent",
        "closuresection", "closurelocation", "between", "workslocation",
    },
    "start_datetime": {
        "startdate", "start", "closurestartdate", "datefrom",
        "closurestart", "startdatetime", "scheduledstarttime", "scheduledstart",
    },
    "end_datetime": {
        "enddate", "end", "closureenddate", "dateto",
        "closureend", "enddatetime", "scheduledendtime", "scheduledend",
    },
    "comment": {
        "description", "comment", "comments", "details",
        "workdescription", "scheme", "schemedescription", "reason",
        "closuredetailsincludingdiversions", "closuredetails",
    },
    "validity_status": {"status", "closurestatus"},
}

# How many leading rows to scan (per sheet) looking for the header row --
# some sheets have a title row above the real column headers.
MAX_HEADER_SCAN_ROWS = 6

# Real data only ever uses 6 columns (Road number, Direction, Location,
# Scheduled start/end time, Closure details), but a sheet can report a
# much wider "used" range than that -- openpyxl reports it out to
# wherever cell formatting was ever applied, even to cells nobody put
# data in (seen on a live sheet: hundreds of extra reported columns with
# no content). Reading all of those for every row is real overhead, not
# just noisy logging, so this bounds how many columns are actually read.
# Generous headroom beyond the known 6 in case a future column is added.
MAX_COLUMNS_TO_READ = 20


def normalize_header(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def to_iso_datetime(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date_cls)):
        return value.isoformat()
    return str(value).strip()


def find_header_row(rows: list[tuple]) -> tuple[int, dict[str, int]] | tuple[None, None]:
    """Scan the first few rows of a sheet for the one that looks like a
    header row (i.e. has a cell matching a known road-name synonym), since
    the real header row isn't always row 1 -- some sheets have a title row
    above it. Returns (row_index, col_map) or (None, None) if not found."""
    for row_idx, row in enumerate(rows[:MAX_HEADER_SCAN_ROWS]):
        headers = [normalize_header(h) for h in row]
        col_map: dict[str, int] = {}
        for field, synonyms in XLSX_HEADER_SYNONYMS.items():
            for idx, h in enumerate(headers):
                if h in synonyms:
                    col_map[field] = idx
                    break
        if "road_name" in col_map:
            return row_idx, col_map
    return None, None


def trim_trailing_empty(row: tuple) -> tuple:
    """Drop trailing None/empty cells for cleaner logging. Some sheets
    report far more "used" columns than actually contain data -- openpyxl
    reports a sheet's used range out to wherever cell formatting was ever
    applied, even to cells that were never filled in, so iter_rows() can
    return a row padded with hundreds of Nones past the real content.
    This only affects what gets printed, not parsing (which still uses
    the full row and looks up columns by index via col_map)."""
    row = list(row)
    while row and row[-1] is None:
        row.pop()
    return tuple(row)


def fetch_from_xlsx_advance_notice(url: str) -> list[dict]:
    try:
        import openpyxl
    except ImportError:
        print("Warning: openpyxl is not installed -- skipping the XLSX "
              "advance-notice source (add it to requirements.txt).")
        return []

    print(f"Fetching {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "route-closures-build/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        print(f"Warning: XLSX fetch failed with HTTP {e.code} {e.reason} -- skipping this source.")
        return []

    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 -- best-effort source, never fatal
        print(f"Warning: could not open the XLSX file ({e}) -- skipping this source.")
        return []

    closures: list[dict] = []
    row_counter = 0

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(max_col=MAX_COLUMNS_TO_READ, values_only=True))
        if not rows:
            continue  # empty sheet

        header_idx, col_map = find_header_row(rows)
        if header_idx is None:
            preview = trim_trailing_empty(rows[0]) if rows else ()
            print(f"  sheet '{sheet_name}': no recognizable 'road' column in the "
                  f"first {MAX_HEADER_SCAN_ROWS} rows -- skipping this sheet "
                  f"(first row: {preview}).")
            continue

        print(f"  sheet '{sheet_name}' headers (row {header_idx + 1}): "
              f"{trim_trailing_empty(rows[header_idx])}")

        def get(row, field):
            idx = col_map.get(field)
            return row[idx] if idx is not None and idx < len(row) else None

        sheet_rows = 0
        for row in rows[header_idx + 1:]:
            road_name = get(row, "road_name")
            if not road_name:
                continue
            row_counter += 1
            sheet_rows += 1
            closures.append({
                "record_id": f"xlsx-{sheet_name}-{row_counter}",
                "road_name": str(road_name).strip(),
                "direction": str(get(row, "direction") or "").strip(),
                "location_description": str(get(row, "location_description") or "").strip(),
                "comment": str(get(row, "comment") or "").strip(),
                "start_datetime": to_iso_datetime(get(row, "start_datetime")),
                "end_datetime": to_iso_datetime(get(row, "end_datetime")),
                "validity_status": str(get(row, "validity_status") or "planned").strip().lower() or "planned",
                "cause_type": "advanceNoticeFullClosure",
                "lanes_restricted": None,
                "lanes_operational": 0,  # this report is full closures only
                "source_label": "Advance notice (full closure)",
            })
        print(f"  sheet '{sheet_name}': parsed {sheet_rows} closure rows")

    print(f"Parsed {len(closures)} total rows from the XLSX advance-notice report")
    return closures
