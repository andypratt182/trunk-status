"""
Traffic Scotland "Current Incidents" (live incidents/closures/queues),
scraped from: https://www.traffic.gov.scot/traffic-information/incidents

Kept as a fully separate, best-effort additional source -- deliberately
self-contained with no imports from sources/traffic_scotland.py, so a
change to one Scotland source can never affect the other (matching what
was asked: a separate source so as not to break anything that already
works). A parsing failure here never breaks the main build (same pattern
as every other additional source; see load_additional_closures() in
build.py).

Unlike the roadworks/planned-roadworks pages this project already
scrapes, this listing page needs no separate detail-page fetch -- every
field worth having (Direction, Incident type, Start time, and either a
lane-restriction count or a free-text description) is already on the
listing page itself, one stage only.

Real entries seen while building this (17 Aug 2026): a mix of routine
"Queue" entries (slow traffic at a slip road) alongside "Closure",
"Breakdown", and "Roadworks" types. Some "Closure"-type entries turned
out to actually be roadworks-caused (e.g. "closed... to allow for
essential roadworks") -- the incident_type label alone isn't a fully
reliable way to separate "genuine live incident" from "roadworks also
listed here", so this scraper doesn't try to filter by type. This means
a given closure could plausibly appear on the site twice: once via this
source, once via the roadworks/planned-roadworks scraper. Deliberately
left as a known limitation rather than a guessed-at deduplication rule --
per the plan agreed when building this, real testing (including whether
this overlap actually happens in practice) is deferred until there's a
live M74 incident to check against.

No end time, same as National Highways' Travel Alerts and for the same
reason: this page has no structured end time anywhere, only a start
time. validity_status is always "active" -- an incident only appears on
this page while it's ongoing.

Cross-road junction contamination guard, same lesson learned twice now
(Traffic Scotland's roadworks scraper, National Highways' Travel Alerts
scraper): some headings mix in a different road's junction as a location
marker, e.g. "A737 M8-J29 North - Slip Off" -- note the hyphen with no
space, a real pattern seen on this page that a simple space-only regex
would miss. strip_other_road_junctions() handles both "M27 J2" (space)
and "M8-J29" (hyphen, no space) forms.

UNVERIFIED AGAINST LIVE MARKUP, same caveat as every other scraper in
this project (this project's fetch tooling converts pages to text, never
exposing raw HTML). If a live run logs "found 0 total incident(s)",
check parse_incident_cards() against the page's real structure.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.request
from datetime import datetime

INCIDENTS_URL = "https://www.traffic.gov.scot/traffic-information/incidents"
BASE_URL = "https://www.traffic.gov.scot"

# Roads that share a physical carriageway under different names/eras --
# matching any alias is treated as a match for the canonical name. Same
# alias as sources/traffic_scotland.py's, duplicated deliberately (see
# module docstring: no cross-imports between the two Scotland sources).
ROAD_ALIASES: dict[str, set[str]] = {
    "M74": {"M74", "A74(M)"},
}

# "<ROAD> J<N>" or "<ROAD>-J<N>" (space OR hyphen OR nothing between road
# and junction -- both forms seen on this page) belonging to a road OTHER
# than the target. These are location/boundary references, not a
# junction ON the target road, and must not be mistaken for one during
# junction-range matching downstream.
_OTHER_ROAD_JUNCTION_RE = r'\b(M\d+[A-Z]?|A\d+\(M\)|A\d+)[\s-]*J(?:ct)?\.?\s*\d+[A-Z]?\b'

# "20th of July 2026, 8:00pm" -- ordinal day, month name, year, 12-hour
# time. Same format already seen on the roadworks pages. No timezone is
# given on the site; treated as naive local (UK) time.
_DATE_RE = re.compile(
    r'(\d{1,2})\w{0,2}\s+of\s+([A-Za-z]+)\s+(\d{4}),\s*(\d{1,2}):(\d{2})\s*([ap]m)',
    re.IGNORECASE,
)
_MONTHS = {name.lower(): i for i, name in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

_LANES_RESTRICTED_RE = re.compile(r'(\d+)\s+lanes?\s+restricted', re.IGNORECASE)

# Matches one full incident card's labeled-text block, keyed off stable
# label strings rather than markup. Deliberately does NOT try to capture
# the start-time text and the free-form detail text (lane count or
# description) as two separate regex groups -- there's no label between
# them to anchor a split, so two adjacent non-greedy groups there produce
# an almost arbitrary boundary (confirmed: it took just the character "1"
# as the "start" text). Instead this captures everything after "Start
# time:" as one blob, and _DATE_RE.search() -- which has a distinctive,
# reliably-matchable shape -- is used afterwards to find exactly where
# the date ends and the free-form detail text begins.
_INCIDENT_BLOCK_RE = re.compile(
    r'Direction:\s*(?P<direction>.+?)\s*'
    r'Incident type:\s*(?P<incident_type>.+?)\s*'
    r'Start time:\s*(?P<start_and_detail>.+?)\s*'
    r'More details',
    re.DOTALL,
)


def canonical_road(token: str) -> str:
    token_upper = token.upper()
    for canonical, aliases in ROAD_ALIASES.items():
        if token_upper in {a.upper() for a in aliases}:
            return canonical
    return token_upper


def strip_other_road_junctions(text: str, target_road: str) -> str:
    """Remove '<ROAD> J<N>' / '<ROAD>-J<N>' mentions for any road other
    than target_road (accounting for the M74/A74(M) alias), so shared
    junction-extraction logic downstream can't mistake a different
    road's junction for one on the target road. Bare junction mentions
    ("J5", "Jct 16") with no road name attached are always left alone."""
    target_aliases = {a.upper() for a in ROAD_ALIASES.get(target_road, {target_road})}

    def replace(m: re.Match) -> str:
        return m.group(0) if m.group(1).upper() in target_aliases else ""
    return re.sub(_OTHER_ROAD_JUNCTION_RE, replace, text, flags=re.IGNORECASE).strip()


def parse_scottish_datetime(text: str) -> str:
    m = _DATE_RE.search(text)
    if not m:
        return ""
    day, month_name, year, hour, minute, ampm = m.groups()
    month = _MONTHS.get(month_name.lower())
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


def parse_incident_cards(html: str) -> list[dict]:
    """Parse each incident card into a raw dict with heading, direction,
    incident_type, start time text, a free-form "detail" line (either a
    lane-restriction count or a description sentence), and the detail
    URL. Cards are found via each "More details" link, walking up to the
    nearest ancestor whose text has all three labels -- the same
    resilient pattern already proven for the roadworks listing page and
    National Highways' Travel Alerts page."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("Warning: beautifulsoup4 is not installed -- skipping the "
              "Traffic Scotland incidents source (add it to requirements.txt).")
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
        heading = ""
        for _ in range(8):
            container = container.parent
            if container is None:
                break
            text = container.get_text("\n", strip=True)
            if "Direction:" in text and "Incident type:" in text and "Start time:" in text:
                block_text = text
                heading_tag = container.find(["h2", "h3", "h4"])
                heading = heading_tag.get_text(strip=True) if heading_tag else ""
                break

        if not block_text:
            continue

        m = _INCIDENT_BLOCK_RE.search(block_text)
        if not m:
            continue

        if not heading:
            # fall back to whatever precedes "Direction:" in the block
            heading = block_text.split("Direction:")[0].strip()

        # Split "start_and_detail" at the date pattern itself, since
        # that's the one reliably-matchable boundary in this gap (see
        # the comment on _INCIDENT_BLOCK_RE for why this isn't done as
        # two separate regex groups).
        start_and_detail = re.sub(r'\s+', ' ', m.group("start_and_detail")).strip()
        date_match = _DATE_RE.search(start_and_detail)
        if date_match:
            start_text = date_match.group(0)
            detail_text = start_and_detail[date_match.end():].strip()
        else:
            start_text = ""
            detail_text = start_and_detail

        seen_hrefs.add(href)
        absolute_href = href if href.startswith("http") else (
            BASE_URL + href if href.startswith("/") else f"{BASE_URL}/{href}"
        )
        results.append({
            "heading": heading,
            "direction": re.sub(r'\s+', ' ', m.group("direction")).strip(),
            "incident_type": re.sub(r'\s+', ' ', m.group("incident_type")).strip(),
            "start_text": start_text,
            "detail": detail_text,
            "href": absolute_href,
        })

    return results


# The listing page covers Scotland's whole trunk road network, and each
# configured road filters the same page -- so with several roads
# configured, this would otherwise re-download an identical page once
# per road, every build. Cached for the lifetime of the process (one
# build run): fresh data every build, fetched once.
_page_cache: dict[str, str | None] = {}


def fetch_listing_page() -> str | None:
    """Fetch the incidents listing page, once per build. Returns None if
    the fetch failed (already logged)."""
    if INCIDENTS_URL in _page_cache:
        return _page_cache[INCIDENTS_URL]

    print(f"Fetching {INCIDENTS_URL} ...")
    req = urllib.request.Request(INCIDENTS_URL, headers={"User-Agent": "route-closures-build/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        print(f"Warning: HTTP {e.code} {e.reason} fetching Traffic Scotland incidents -- skipping this source.")
        html = None
    except Exception as e:  # noqa: BLE001 -- this source is best-effort, never fatal
        print(f"Warning: failed to fetch Traffic Scotland incidents ({e}) -- skipping this source.")
        html = None

    _page_cache[INCIDENTS_URL] = html
    return html


def fetch_from_scotland_incidents(road_name: str = "M74") -> list[dict]:
    """Fetch current Traffic Scotland incidents and filter to ones whose
    heading mentions road_name (checking the M74/A74(M) alias). Returns
    closures in the standard flat record shape -- with no end time (see
    module docstring) and validity_status always "active"."""
    aliases = ROAD_ALIASES.get(road_name, {road_name})

    html = fetch_listing_page()
    if html is None:
        return []

    cards = parse_incident_cards(html)
    print(f"  found {len(cards)} total incident(s) on the page (all roads)")

    results = []
    for card in cards:
        matched_alias = None
        for alias in aliases:
            if re.search(rf'\b{re.escape(alias)}\b', card["heading"], re.IGNORECASE):
                matched_alias = alias
                break
        if not matched_alias:
            continue

        location_text = strip_other_road_junctions(card["heading"], road_name)

        lanes_match = _LANES_RESTRICTED_RE.search(card["detail"])
        lanes_restricted = int(lanes_match.group(1)) if lanes_match else None
        # a free-text description (not a lane count) goes in the comment
        # instead, e.g. "The A9 at Alness is closed in both directions..."
        comment = "" if lanes_match else card["detail"]

        sid_match = re.search(r'[?&]sid=([^&]+)', card["href"])
        record_id = f"scotland-incident-{sid_match.group(1)}" if sid_match else f"scotland-incident-{hash(card['href'])}"

        results.append({
            "record_id": record_id,
            "road_name": road_name,
            "direction": card["direction"],
            "location_description": location_text,
            "comment": comment,
            "start_datetime": parse_scottish_datetime(card["start_text"]),
            "end_datetime": "",
            "validity_status": "active",
            "cause_type": card["incident_type"],
            "lanes_restricted": lanes_restricted,
            "lanes_operational": None,
            "source_label": "Traffic Scotland Incident",
        })

    print(f"  {len(results)} match {road_name} (aliases: {', '.join(sorted(aliases))})")
    return results
