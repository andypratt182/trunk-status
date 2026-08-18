"""
Traffic Scotland (M74 / A74(M) scraper)

Traffic Scotland has no simple self-service API (their real-time feeds
require an approved-subscriber application). This scrapes their public
pages in two stages:

  1. Two listing pages -- /traffic-information/roadworks (current) and
     /traffic-information/planned-roadworks (planned) -- list every
     roadwork on Scotland's entire trunk road network as plain-text
     blocks (Location:/Start time:/Description:/[More details]). Stage
     1 finds every entry whose Location mentions the target road (M74
     or A74(M)) and grabs its "More details" link; the listing page's
     other fields aren't used for the final record.

  2. Each matched entry's own detail page
     (/more-details?sid=...&type=roadworks) has clean, structured
     fields: Location (often has an explicit junction range, e.g. "M74
     J8 - J9 SB"), Direction, Starting, Ending, and a Roadwork
     description (Works: / Traffic Management: / sometimes Diversion
     Information:). Critically, this is the only place a real end date
     is available -- the listing pages never showed one.

Some closures also publish a "Days & times affected" section with an
expandable Activity Periods list giving the EXACT overnight windows a
closure is actually active (e.g. "Thu 20th Aug - 22:00 to 23:59"), while
the overall Starting/Ending dates can span many weeks -- the road usually
isn't closed continuously for that whole span, only on specific nights
within it. When present, one row is produced per merged period instead
of a single row spanning the misleading overall range.

Both stage 1 (find_road_entries) and stage 2 (parse_detail_page) have
been tested against real page content fetched live from the site --
including a real cross-road example (an A701 entry whose location
mentions M74 only as part of a diversion route, correctly excluded by
the cross-road ambiguity guard since it has no M74-specific junction
number). The DOM structure find_road_entries walks up through to find
each entry's containing block was verified against the real page's text
content, not its exact HTML tags/classes. If a live run logs "found 0
... entries" on a listing page, that's the signal to check
find_road_entries() against the real markup -- a fetch failure on any
individual detail page is logged and skipped rather than failing the
whole build.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from matching import _junctions_in_text

ROADWORKS_URL = "https://www.traffic.gov.scot/traffic-information/roadworks"
PLANNED_ROADWORKS_URL = "https://www.traffic.gov.scot/traffic-information/planned-roadworks"
BASE_URL = "https://www.traffic.gov.scot"

# Roads that share a physical carriageway under different names/eras --
# matching any alias is treated as a match for the canonical name.
ROAD_ALIASES: dict[str, set[str]] = {
    "M74": {"M74", "A74(M)"},
}

# Whole-word road name, e.g. "M74" or "A74(M)" -- used for the clean
# DETAIL page Location field (e.g. "M74 J8 - J9 SB - Lane Closures").
ROAD_TOKEN_RE = re.compile(r'\b(M\d+[A-Z]?|A\d+\(M\)|A\d+)\b')

# The road token at the START of a location segment on the LISTING page,
# e.g. "M74 (" or "A701 (" -- that page's format is "ROAD (from) to ROAD
# (to)", different from the detail page's clean short string.
LISTING_ROAD_TOKEN_RE = re.compile(r'\b(M\d+[A-Z]?|A\d+\(M\)|A\d+)\s*\(')

# "20th of July 2026, 8:00pm" -- ordinal day, month name, year, 12-hour
# time. No timezone is given on the site; treated as naive local (UK) time.
DATE_RE = re.compile(
    r'(\d{1,2})\w{0,2}\s+of\s+([A-Za-z]+)\s+(\d{4}),\s*(\d{1,2}):(\d{2})\s*([ap]m)',
    re.IGNORECASE,
)
MONTHS = {name.lower(): i for i, name in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}
# Activity Period lines use abbreviated month names ("Aug"), while
# Starting/Ending use full names ("August") -- this lookup handles both
# by matching on the first three letters either way.
MONTH_ABBR = {name[:3]: num for name, num in MONTHS.items()}

# Detail page field labels, in the order they appear. scan_labeled_fields()
# slices the page's text between consecutive labels found here, so it
# doesn't matter if an optional one (e.g. "Diversion Information:") is
# missing for a given entry.
DETAIL_LABELS = [
    "Location", "Direction", "Starting", "Ending",
    "Days & times affected", "Roadwork description",
    "Works:", "Traffic Management:", "Diversion Information:",
    "Did you find",
]

# Within "Days & times affected", an expandable "Activity Periods" list
# gives the exact overnight windows a closure is actually active, e.g.
# "Thu 20th Aug - 22:00 to 23:59". The overall Starting/Ending span can
# cover many weeks while the road is only actually closed on specific
# nights within it -- these lines are the ground truth for that.
ACTIVITY_PERIOD_RE = re.compile(
    r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2})\w{0,2}\s+([A-Za-z]+)\s*-\s*'
    r'(\d{1,2}):(\d{2})\s*to\s*(\d{1,2}):(\d{2})',
    re.IGNORECASE,
)

# Matches one full entry's labeled-text block on a LISTING page, keyed off
# stable label strings rather than markup. This is how entries are found
# and their "More details" link extracted -- the listing page's other
# fields aren't used for the final record; the detail page (stage 2) is
# parsed separately for the real data, including the end date the
# listing page never has.
LISTING_ENTRY_RE = re.compile(
    r'Location:\s*(?P<location>.+?)\s*'
    r'Start time:\s*(?P<start>.+?)\s*'
    r'Description:',
    re.DOTALL,
)


def fetch_text(url: str, headers: dict | None = None) -> str:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_scottish_datetime(text: str) -> str:
    m = DATE_RE.search(text)
    if not m:
        return ""
    day, month_name, year, hour, minute, ampm = m.groups()
    month = MONTHS.get(month_name.lower())
    if not month:
        return ""
    hour = int(hour)
    if ampm.lower() == "pm" and hour != 12:
        hour += 12
    elif ampm.lower() == "am" and hour == 12:
        hour = 0
    try:
        return datetime(int(year), month, int(day), hour, int(minute)).isoformat()
    except ValueError:
        return ""


def extract_road_tokens(text: str) -> list[str]:
    return ROAD_TOKEN_RE.findall(text)


def canonical_road(token: str) -> str:
    """Map a raw road token to its canonical name via ROAD_ALIASES (e.g.
    both "M74" and "A74(M)" canonicalize to "M74"), so an entry mentioning
    both isn't mistaken for a genuine cross-road entry."""
    token_upper = token.upper()
    for canonical, aliases in ROAD_ALIASES.items():
        if token_upper in {a.upper() for a in aliases}:
            return canonical
    return token_upper


def isolate_road_segment(text: str, target_aliases: set[str]) -> str:
    """When text describes more than one road (e.g. "M8 (Sec C/Way Jct
    22) to M74 SB (Sec C/Way Jct 3a)"), return just the portion
    describing our target road, so a coincidental junction number
    belonging to a DIFFERENT road (e.g. the M8's own "Jct 22") can't be
    mistaken for one of ours purely because both numbers appear together
    in one combined string -- seen in practice: an M8/M74 interchange
    entry where the M8's "Jct 22" happened to fall inside the M74 leg's
    configured J8-22 range, incorrectly matching a closure that's
    actually describing M74's own (out-of-range) Junction 3A.

    Splits the text at every road-token occurrence (any road, not just
    the target) and keeps only the segment(s) starting at a target-road
    token, up to the next DIFFERENT road's token or the end of the
    string. Returns the text unchanged if it only mentions one road (or
    none), so the common single-road case is untouched."""
    occurrences = [(m.start(), m.group(1).upper()) for m in ROAD_TOKEN_RE.finditer(text)]
    if len(occurrences) <= 1:
        return text

    target_upper = {a.upper() for a in target_aliases}
    segments = []
    for i, (pos, token) in enumerate(occurrences):
        if token not in target_upper:
            continue
        end = occurrences[i + 1][0] if i + 1 < len(occurrences) else len(text)
        segments.append(text[pos:end].strip())

    if not segments:
        return text  # target road wasn't actually a token here -- leave unchanged
    return " / ".join(segments)


def find_road_entries(html: str, aliases: set[str]) -> list[dict]:
    """Stage 1: find each entry block on a listing page whose Location
    field mentions one of the given road aliases (e.g. M74 or A74(M)),
    returning its raw location text (for the cross-road ambiguity check
    -- see fetch_from_traffic_scotland) and its "More details" link.

    Entries are plain-text blocks with stable labels (Location:/Start
    time:/Description:...), NOT clickable titles -- only a separate
    "More details" link at the end of each block goes anywhere. This
    walks up from each such link to find its own containing block (the
    nearest ancestor whose text has both "Location:" and "Start time:"),
    so each link is correctly tied to its own entry regardless of
    document order or a malformed neighboring block."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("Warning: beautifulsoup4 is not installed -- skipping the "
              "Traffic Scotland source (add it to requirements.txt).")
        return []

    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen_hrefs = set()

    for link in soup.find_all("a", string=lambda s: s and "more details" in s.lower()):
        href = link.get("href", "")
        if not href or href in seen_hrefs:
            continue

        container = link
        block_text = ""
        for _ in range(8):  # walk up a bounded number of ancestors
            container = container.parent
            if container is None:
                break
            text = container.get_text("\n", strip=True)
            if "Location:" in text and "Start time:" in text:
                block_text = text
                break

        if not block_text:
            continue

        entry_match = LISTING_ENTRY_RE.search(block_text)
        if not entry_match:
            continue
        location_text = re.sub(r'\s+', ' ', entry_match.group("location")).strip()

        tokens = {t.upper() for t in LISTING_ROAD_TOKEN_RE.findall(location_text)}
        if not (tokens & aliases):
            continue

        seen_hrefs.add(href)
        absolute_href = href if href.startswith("http") else (
            BASE_URL + href if href.startswith("/") else f"{BASE_URL}/{href}"
        )
        results.append({"location_text": location_text, "href": absolute_href})

    return results


def scan_labeled_fields(text: str) -> dict[str, str]:
    """Slice text into label -> content by finding where each known label
    (in DETAIL_LABELS) first occurs, then taking everything up to the
    next label that's actually present. Robust to optional sections
    being missing (e.g. no "Diversion Information:") since it only
    slices between labels that were actually found, in the order they
    appear -- not a fixed positional template."""
    positions = []
    for label in DETAIL_LABELS:
        idx = text.find(label)
        if idx != -1:
            positions.append((idx, label))
    positions.sort()

    fields = {}
    for i, (idx, label) in enumerate(positions):
        start = idx + len(label)
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        fields[label] = text[start:end].strip()
    return fields


def parse_activity_periods(text: str, reference_start: str,
                            reference_end: str) -> list[tuple[str, str]]:
    """Parse 'Thu 20th Aug - 22:00 to 23:59' style lines out of the "Days
    & times affected" field text into (start_iso, end_iso) tuples. The
    year isn't given on each line, so it's inferred by picking whichever
    of the overall Starting/Ending years places the resulting date within
    (or close to) that overall range -- these periods should always fall
    inside it."""
    ref_start_dt = None
    ref_end_dt = None
    try:
        if reference_start:
            ref_start_dt = datetime.fromisoformat(reference_start)
        if reference_end:
            ref_end_dt = datetime.fromisoformat(reference_end)
    except ValueError:
        pass

    candidate_years = sorted({dt.year for dt in (ref_start_dt, ref_end_dt) if dt} or {datetime.now().year})

    periods = []
    for m in ACTIVITY_PERIOD_RE.finditer(text):
        day_str, month_name, sh, sm, eh, em = m.groups()
        month = MONTH_ABBR.get(month_name.lower()[:3])
        if not month:
            continue
        day = int(day_str)

        chosen_start = None
        for year in candidate_years:
            try:
                candidate = datetime(year, month, day, int(sh), int(sm))
            except ValueError:
                continue
            if ref_start_dt and ref_end_dt:
                if (ref_start_dt - timedelta(days=1)) <= candidate <= (ref_end_dt + timedelta(days=1)):
                    chosen_start = candidate
                    break
            else:
                chosen_start = candidate
                break
        if chosen_start is None:
            try:
                chosen_start = datetime(candidate_years[0], month, day, int(sh), int(sm))
            except ValueError:
                continue

        end_dt = chosen_start.replace(hour=int(eh), minute=int(em))
        if end_dt <= chosen_start:
            end_dt += timedelta(days=1)  # end time wraps past midnight within one line

        periods.append((chosen_start.isoformat(), end_dt.isoformat()))

    return periods


# "Week commencing 17th Aug" -- header for one week's block within the
# calendar grid (no year given; inferred the same way as Activity Periods).
WEEK_HEADER_RE = re.compile(r'Week commencing\s+(\d{1,2})\w{0,2}\s+([A-Za-z]+)', re.IGNORECASE)

# "Early Morning (00:00 - 06:00)" or "Evening (18:00 - 00:00)" followed
# (with a "\u25cf" marker and arbitrary whitespace/newlines in between,
# not captured -- its exact rendering wasn't consistent between real
# pages) by the single weekday name it's checked against, e.g.
# "...Early Morning (00:00 - 06:00) \u25cf Monday." A band only appears
# in the real page at all when at least one day is checked for it, so
# this doesn't need to handle an unchecked/empty band.
BAND_DAY_RE = re.compile(
    r'(Early Morning|Evening)\s*\((\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\)'
    r'.*?'
    r'\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b\.',
    re.IGNORECASE | re.DOTALL,
)

_WEEKDAY_OFFSETS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def parse_calendar_grid_periods(text: str, reference_start: str,
                                 reference_end: str) -> list[tuple[str, str]]:
    """Fallback for when the Activity Periods bulleted list is empty but
    the weekly calendar grid still shows which specific day(s) a closure
    is active. Real case that motivated this: an entry whose "Days &
    times affected" section showed "Week commencing 17th Aug" with
    "Early Morning (00:00 - 06:00)" checked on Monday, but had a
    completely empty "Activity Periods" list underneath -- with only
    parse_activity_periods() to go on, that closure fell back to showing
    its full multi-week Starting/Ending span (misleading -- it's really
    only active one morning), even though the calendar grid clearly had
    the real answer sitting right there.

    NOTE: this is coarser than parse_activity_periods() when both are
    available -- the grid only gives a band-level time range (e.g. the
    whole "Early Morning" 00:00-06:00 window), not the precise minutes a
    bulleted Activity Periods line would give (e.g. "22:00 to 23:59").
    That's why this is only ever used as a fallback, never allowed to
    override a populated Activity Periods list -- see parse_detail_page.
    """
    ref_start_dt = None
    ref_end_dt = None
    try:
        if reference_start:
            ref_start_dt = datetime.fromisoformat(reference_start)
        if reference_end:
            ref_end_dt = datetime.fromisoformat(reference_end)
    except ValueError:
        pass

    candidate_years = sorted({dt.year for dt in (ref_start_dt, ref_end_dt) if dt} or {datetime.now().year})

    week_headers = list(WEEK_HEADER_RE.finditer(text))
    periods = []

    for i, wm in enumerate(week_headers):
        week_day_str, week_month_name = wm.groups()
        month = MONTH_ABBR.get(week_month_name.lower()[:3])
        if not month:
            continue
        week_day = int(week_day_str)

        # Bound this week's segment to just its own block -- up to the
        # next "Week commencing" header, or the end of the grid,
        # whichever comes first -- so a multi-week grid's bands/days
        # don't bleed into each other.
        segment_end = week_headers[i + 1].start() if i + 1 < len(week_headers) else len(text)
        segment = text[wm.end():segment_end]
        for boundary in ("Activity Periods", "Roadwork description"):
            idx = segment.find(boundary)
            if idx != -1:
                segment = segment[:idx]

        anchor_year = None
        for year in candidate_years:
            try:
                candidate_monday = datetime(year, month, week_day)
            except ValueError:
                continue
            if ref_start_dt and ref_end_dt:
                if (ref_start_dt - timedelta(days=7)) <= candidate_monday <= (ref_end_dt + timedelta(days=1)):
                    anchor_year = year
                    break
            else:
                anchor_year = year
                break
        if anchor_year is None:
            anchor_year = candidate_years[0]
        try:
            anchor_monday = datetime(anchor_year, month, week_day)
        except ValueError:
            continue

        for bm in BAND_DAY_RE.finditer(segment):
            _band_name, start_time, end_time, day_name = bm.groups()
            offset = _WEEKDAY_OFFSETS.get(day_name.lower())
            if offset is None:
                continue
            day_date = anchor_monday + timedelta(days=offset)
            sh, sm = map(int, start_time.split(":"))
            eh, em = map(int, end_time.split(":"))
            period_start = day_date.replace(hour=sh, minute=sm)
            period_end = day_date.replace(hour=eh, minute=em)
            if period_end <= period_start:
                period_end += timedelta(days=1)  # e.g. Evening 18:00 -> 00:00 wraps to the next day
            periods.append((period_start.isoformat(), period_end.isoformat()))

    return periods


def merge_adjacent_periods(periods: list[tuple[str, str]],
                            gap_tolerance_minutes: int = 5) -> list[tuple[str, str]]:
    """Merge periods that abut across a midnight split -- e.g. "22:00 to
    23:59" followed by "00:00 to 06:00" the next calendar day become one
    "22:00 to 06:00" period, since that's how these are actually
    published (as two grid cells either side of midnight, one minute
    apart) rather than as a single overnight line."""
    if not periods:
        return []

    parsed = sorted(
        ((datetime.fromisoformat(s), datetime.fromisoformat(e)) for s, e in periods),
        key=lambda p: p[0],
    )
    merged = [list(parsed[0])]
    for start, end in parsed[1:]:
        last_start, last_end = merged[-1]
        if (start - last_end) <= timedelta(minutes=gap_tolerance_minutes):
            merged[-1][1] = max(last_end, end)
        else:
            merged.append([start, end])

    return [(s.isoformat(), e.isoformat()) for s, e in merged]


# Traffic Management text on long-running entries can be a per-date list,
# e.g. "15/10/2024 - Portable Traffic Lights (TTLS), 16/10/2024 - ...",
# one entry for every day across the whole closure. extract_tm_for_date()
# picks out just the entry matching a specific row's own date rather than
# showing that whole list.
# "Share" / "Link for sharing" / "Copy link" (the page's share widget)
# sit right before "Did you find..." on the detail page, but aren't in
# DETAIL_LABELS -- so whichever section comes last (Diversion
# Information, or Traffic Management/Works if no diversion) absorbs that
# boilerplate as trailing text, since scan_labeled_fields() only knows to
# stop at the next label it recognizes. Anchored to the end of the string
# and requiring the full phrase (not just "Share" alone) so it can't
# accidentally trim real content that happens to end in a word like
# "shared" -- e.g. "...via the shared path" is untouched.
TRAILING_SHARE_BOILERPLATE_RE = re.compile(
    r'\s*Share\s*Link for sharing\s*Copy link\s*$',
    re.IGNORECASE,
)


def clean_field_text(text: str) -> str:
    """Normalize a raw extracted field: collapse whitespace, strip the
    page's share-widget boilerplate if it leaked in as trailing text (see
    TRAILING_SHARE_BOILERPLATE_RE), and fix a source-data spacing quirk
    where an opening parenthesis sometimes has no preceding space and an
    extra space just inside it, e.g. "Lane Closure( 40mph)" ->
    "Lane Closure (40mph)"."""
    text = re.sub(r'\s+', ' ', text or '').strip()
    text = TRAILING_SHARE_BOILERPLATE_RE.sub('', text).strip()
    text = re.sub(r'(?<=\S)\(', ' (', text)   # ensure a space before "("
    text = re.sub(r'\(\s+', '(', text)        # no space right after "("
    return text


TM_DATE_ENTRY_RE = re.compile(
    r'(\d{2})/(\d{2})/(\d{4})\s*-\s*(.+?)(?=,\s*\d{2}/\d{2}/\d{4}\s*-|\s*$)',
    re.DOTALL,
)


def extract_tm_for_date(tm_text: str, target_date_iso: str) -> str:
    """If tm_text contains a per-date list, return just the description
    for target_date_iso's calendar date, with the date prefix stripped.
    If tm_text has no such per-date structure (the common case -- a
    single description like "Lane Closure (40mph)" or "Road Closure."),
    return it unchanged. Falls back to the first listed date's
    description if the target date isn't found in the list (safer than
    showing the whole raw list)."""
    tm_text = clean_field_text(tm_text)
    matches = list(TM_DATE_ENTRY_RE.finditer(tm_text))
    if not matches:
        return tm_text

    target_date_str = ""
    if target_date_iso:
        try:
            target_date_str = datetime.fromisoformat(target_date_iso).strftime("%d/%m/%Y")
        except ValueError:
            pass

    for day, month, year, desc in (m.groups() for m in matches):
        if f"{day}/{month}/{year}" == target_date_str:
            return clean_field_text(desc.rstrip(","))

    return clean_field_text(matches[0].group(4).rstrip(","))


def parse_detail_page(html: str, href: str) -> list[dict]:
    """Stage 2: parse one entry's detail page into one or more closure
    dicts (still missing road_name/validity_status/source_label -- the
    caller fills those in). Returns a list because a single detail page
    can expand into several rows: when "Days & times affected" gives
    specific overnight Activity Periods (the real ground truth for when
    the road is actually closed), one row per merged period is returned
    instead of a single row spanning the overall Starting/Ending range,
    which can be misleadingly wide (e.g. "5 weeks" when the road is only
    actually shut a few nights within that span). Falls back to a single
    row using Starting/Ending when no such periods are found. Returns an
    empty list if the page's structure didn't match what's expected."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []

    soup = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text("\n", strip=True)

    # Bound the scan to just the roadwork-details card, so page chrome
    # (nav/footer) can't collide with a label name.
    start_marker = full_text.find("Roadwork details")
    text = full_text[start_marker:] if start_marker != -1 else full_text

    fields = scan_labeled_fields(text)
    location = fields.get("Location", "").strip()
    if not location:
        return []  # page structure didn't match what's expected here

    direction = fields.get("Direction", "").strip()
    start_text = fields.get("Starting", "").strip()
    end_text = fields.get("Ending", "").strip()
    days_times_text = fields.get("Days & times affected", "")
    works = clean_field_text(fields.get("Works:", ""))
    tm = clean_field_text(fields.get("Traffic Management:", ""))
    diversion = clean_field_text(fields.get("Diversion Information:", ""))

    # "Works" (cause) already has its own Cause column, and Traffic
    # Management moves to the Lanes column below (computed per-row,
    # since it can be date-specific) -- the location subtext only needs
    # Diversion info now, which has nowhere else to go.
    comment = f"Diversion: {diversion}" if diversion else ""

    sid_match = re.search(r'[?&]sid=([^&]+)', href)
    base_record_id = f"scotland-{sid_match.group(1)}" if sid_match else f"scotland-{hash(href)}"

    overall_start = parse_scottish_datetime(start_text)
    overall_end = parse_scottish_datetime(end_text)

    shared_fields = {
        "location_description": location,
        "direction": direction,
        "comment": comment,
        "cause_type": works,
    }

    raw_periods = parse_activity_periods(days_times_text, overall_start, overall_end)
    if not raw_periods:
        # Some entries have a populated calendar grid (Week commencing /
        # band / day checked) but an empty Activity Periods bulleted
        # list underneath it -- fall back to the grid itself rather than
        # falling all the way back to the misleading full Starting/Ending
        # span. See parse_calendar_grid_periods()'s docstring for the
        # real example that motivated this.
        raw_periods = parse_calendar_grid_periods(days_times_text, overall_start, overall_end)
    merged_periods = merge_adjacent_periods(raw_periods)

    if merged_periods:
        return [
            {
                **shared_fields,
                "record_id": f"{base_record_id}-p{i + 1}",
                "start_datetime": period_start,
                "end_datetime": period_end,
                "lane_info": extract_tm_for_date(tm, period_start),
            }
            for i, (period_start, period_end) in enumerate(merged_periods)
        ]

    return [{
        **shared_fields,
        "record_id": base_record_id,
        "start_datetime": overall_start,
        "end_datetime": overall_end,
        "lane_info": extract_tm_for_date(tm, overall_start),
    }]


def compute_validity_status(start_iso: str, end_iso: str, now: datetime, fallback: str) -> str:
    """Traffic Scotland's own current/planned split is just which listing
    page an entry appeared on -- not date-aware, and can be stale between
    rebuilds (a closure found on the "current" page stays labeled active
    forever if we don't check the real dates). Since entries here carry
    precise start/end times (especially after Activity Period expansion),
    compute the real status instead: 'active' only while `now` actually
    falls within [start, end], 'planned' otherwise.

    If `end` is missing or unparseable but `start` isn't, compare against
    `start` alone (active once it's begun, planned before) rather than
    falling back to the static per-page label -- a closure that's clearly
    already started shouldn't keep showing "planned", and one whose end
    date we just don't have shouldn't be stuck showing whatever label its
    listing page happened to give it. Only falls back to `fallback` when
    even `start` is missing/unparseable, since there's nothing real left
    to compare against at that point."""
    start = None
    end = None
    if start_iso:
        try:
            start = datetime.fromisoformat(start_iso)
        except ValueError:
            start = None
    if end_iso:
        try:
            end = datetime.fromisoformat(end_iso)
        except ValueError:
            end = None

    if start and end:
        return "active" if start <= now <= end else "planned"
    if start:
        return "active" if now >= start else "planned"
    return fallback


def fetch_from_traffic_scotland(road_name: str = "M74") -> list[dict]:
    aliases = ROAD_ALIASES.get(road_name, {road_name})
    pages = [
        (ROADWORKS_URL, "active"),
        (PLANNED_ROADWORKS_URL, "planned"),
    ]

    # href -> (listing_location_text, validity_status) -- de-duplicated
    # across both listing pages in case an entry somehow appears on both
    found: dict[str, tuple[str, str]] = {}
    for url, status in pages:
        print(f"Fetching {url} ...")
        try:
            html = fetch_text(url, headers={"User-Agent": "route-closures-build/1.0"})
        except urllib.error.HTTPError as e:
            print(f"Warning: HTTP {e.code} {e.reason} fetching {url} -- skipping this page.")
            continue

        entries = find_road_entries(html, aliases)
        print(f"  found {len(entries)} {road_name}-matching entr"
              f"{'y' if len(entries) == 1 else 'ies'} on this page")
        for entry in entries:
            found.setdefault(entry["href"], (entry["location_text"], status))

    if not found:
        print("Warning: 0 matching entries found from Traffic Scotland. If "
              "this is unexpected, the listing page's real block/link "
              "structure may differ from what find_road_entries() expects.")
        return []

    print(f"Fetching detail pages for {len(found)} matched entr"
          f"{'y' if len(found) == 1 else 'ies'} ...")

    # Naive UK-local "now" to compare against the (also naive, assumed
    # UK-local) start/end times stored on each entry -- see
    # compute_validity_status().
    now = datetime.now(ZoneInfo("Europe/London")).replace(tzinfo=None)

    results = []
    skipped_ambiguous = 0
    fetch_failures = 0
    for href, (listing_location_text, status) in found.items():
        try:
            detail_html = fetch_text(href, headers={"User-Agent": "route-closures-build/1.0"})
        except urllib.error.HTTPError as e:
            print(f"  Warning: HTTP {e.code} fetching {href} -- skipping this entry.")
            fetch_failures += 1
            continue
        except Exception as e:  # noqa: BLE001 -- one bad entry shouldn't fail the build
            print(f"  Warning: failed to fetch {href} ({e}) -- skipping this entry.")
            fetch_failures += 1
            continue

        entries = parse_detail_page(detail_html, href)
        if not entries:
            print(f"  Warning: could not parse detail page {href} -- skipping.")
            continue

        # Cross-road ambiguity guard: if the listing's location text (the
        # original context this entry was found in) mentions more than
        # one distinct road, isolate just our target road's own segment
        # before checking for a junction number -- otherwise a
        # coincidental junction belonging to the OTHER road can slip
        # through and incorrectly satisfy our leg's configured range
        # (real case seen in practice: an M8/M74 interchange entry where
        # the M8's own "Jct 22" happened to fall inside the M74 leg's
        # J8-22 range, even though the closure itself was actually
        # describing M74's own out-of-range Junction 3A). If, after
        # isolating our own segment, there's still no junction number to
        # go on, skip rather than guess. Checked once per detail page
        # since every period from it shares the same location_description.
        location_description = entries[0]["location_description"]
        tokens = {t.upper() for t in extract_road_tokens(
            f"{listing_location_text} {location_description}"
        )}
        distinct_roads = {canonical_road(t) for t in tokens}
        is_cross_road = len(distinct_roads) > 1

        if is_cross_road:
            isolated = isolate_road_segment(location_description, aliases)
            if isolated != location_description:
                for entry in entries:
                    entry["location_description"] = isolated
                location_description = isolated

        if is_cross_road and not _junctions_in_text(location_description):
            skipped_ambiguous += 1
            continue

        for entry in entries:
            entry["road_name"] = road_name
            entry["validity_status"] = compute_validity_status(
                entry["start_datetime"], entry["end_datetime"], now, fallback=status
            )
            entry["lanes_restricted"] = None
            entry["lanes_operational"] = None
            entry["source_label"] = "Traffic Scotland (scraped)"
            results.append(entry)

    if skipped_ambiguous:
        print(f"  skipped {skipped_ambiguous} entr{'y' if skipped_ambiguous == 1 else 'ies'} "
              f"mentioning another road with no explicit {road_name} junction number")
    if fetch_failures:
        print(f"  {fetch_failures} detail page fetch(es) failed and were skipped")

    print(f"Parsed {len(results)} {road_name} closures from Traffic Scotland detail pages")
    return results
