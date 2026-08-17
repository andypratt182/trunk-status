"""
National Highways "Travel Alerts" (major incidents), scraped from:
https://nationalhighways.co.uk/roads-and-travel/live-travel-updates/travel-alerts/

Kept as a fully separate, best-effort additional source -- a parsing
failure here never breaks the main build (same pattern as the XLSX and
Traffic Scotland sources; see load_additional_closures() in build.py).

This is DELIBERATELY NOT folded into the Road & Lane Closures API path.
That API's "unplanned" closureType and this page turned out to be two
separate, non-overlapping systems at National Highways -- confirmed by
comparing real API output against a real live Travel Alerts screenshot
on the same day: the API had 4 generic "roadOrCarriagewayOrLaneManagement"
-tagged records, while Travel Alerts showed 3 completely different,
specific, major incidents (an M6 collision, an A31 fire closure, an A303
vehicle fire) that never appeared via the API at all.

Unlike every other source in this project, Travel Alerts has NO
structured start/end datetime anywhere -- not even on the individual
detail pages, which bury timing in free-flowing narrative prose (e.g.
"occurred at approximately 04:10 on the morning of Monday 17th August...").
Rather than attempt fragile natural-language date extraction, this
scraper deliberately does not try: alerts are treated as "active" for as
long as they appear on the listing page (National Highways removes them
once resolved), with no scheduled window. This is an honest
representation of what the data actually is (an unscheduled, ongoing
situation), not a limitation to work around -- and it's also why this
source only uses the LISTING page, not the individual detail pages: the
detail pages add narrative prose but no structured data the listing
doesn't already have, and the road/junction info the listing page's own
title already states clearly is the main thing worth extracting.

Very low volume by design: this listing typically has only 2-3 entries
covering the ENTIRE English strategic road network at any moment ("the
highest priority incidents" per the page's own description), so most
builds will likely find 0 matches for any specific route -- that's
expected, not a sign something's broken.

UNVERIFIED AGAINST LIVE MARKUP: like the Traffic Scotland listing page,
this was built against text-converted page content, not raw HTML, so the
DOM-walking in parse_alert_cards() is a best-effort guess. If a live run
logs "found 0 total alert(s)", that's the signal to check it against the
page's real structure.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.request

TRAVEL_ALERTS_URL = "https://nationalhighways.co.uk/roads-and-travel/live-travel-updates/travel-alerts/"
BASE_URL = "https://nationalhighways.co.uk"

# The road token at the very start of an alert's title, e.g. "M6" in
# "M6 - Between J15 and J16 - Carriageway Closure".
LEADING_ROAD_TOKEN_RE = re.compile(r'^\s*(M\d+[A-Z]?|A\d+\(M\)|A\d+)\s*-')

# "<ROAD> J<N>" mentions belonging to a road OTHER than the target --
# these are boundary/location references (e.g. "M27 J2" describing where
# an A31 closure starts), not a junction ON the target road, and must not
# be mistaken for one during junction-range matching downstream.
_OTHER_ROAD_JUNCTION_RE = r'\b(M\d+[A-Z]?|A\d+\(M\)|A\d+)\s+J(?:ct)?\.?\s*\d+[A-Z]?\b'


def strip_other_road_junctions(text: str, target_road: str) -> str:
    """Remove '<ROAD> J<N>' mentions for any road other than target_road,
    so shared junction-extraction logic downstream (matching.py) can't
    mistake a different road's junction (e.g. "M27 J2" while the target
    is "A31") for one on the target road. Bare junction mentions ("J15",
    "Jct 16") with no road name attached are always left alone -- those
    are the ones actually worth trusting."""
    def replace(m: re.Match) -> str:
        return m.group(0) if m.group(1).upper() == target_road.upper() else ""
    return re.sub(_OTHER_ROAD_JUNCTION_RE, replace, text, flags=re.IGNORECASE).strip()


def parse_alert_cards(html: str) -> list[dict]:
    """Parse each alert card on the listing page into a raw dict with
    title, subtitle, and detail URL. Cards are found via their "More
    details" link (a substring match, since the real structure might
    have the whole card as one clickable link ending in that phrase, or
    a separate small "More details" link within a larger card -- this
    handles both). Robust to either shape since the real markup hasn't
    been verified (see module docstring)."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("Warning: beautifulsoup4 is not installed -- skipping the "
              "Travel Alerts source (add it to requirements.txt).")
        return []

    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen_hrefs = set()

    for link in soup.find_all("a", string=lambda s: s and "more details" in s.lower()):
        href = link.get("href", "")
        if not href or href in seen_hrefs:
            continue

        # Try the link's own text first, in case the whole card (title +
        # subtitle + "More details") is one big clickable link.
        own_text = link.get_text(" ", strip=True)
        block_text = re.sub(r'\s*more details\s*$', '', own_text, flags=re.IGNORECASE).strip()

        if len(block_text) < 15:
            # The link's own text is basically just "More details" --
            # title/subtitle must live in a separate nearby element
            # instead. Walk up a bounded number of ancestors looking for
            # a container with enough real content.
            container = link
            for _ in range(8):
                container = container.parent
                if container is None:
                    break
                text = container.get_text("\n", strip=True)
                candidate = re.sub(r'\s*more details\s*$', '', text, flags=re.IGNORECASE).strip()
                if len(candidate) > 15:
                    block_text = candidate
                    break

        if not block_text:
            continue

        lines = [l.strip() for l in block_text.split("\n") if l.strip()]
        if not lines:
            continue
        title = lines[0]
        subtitle = lines[1] if len(lines) > 1 else ""

        seen_hrefs.add(href)
        absolute_href = href if href.startswith("http") else (
            BASE_URL + href if href.startswith("/") else f"{BASE_URL}/{href}"
        )
        results.append({"title": title, "subtitle": subtitle, "href": absolute_href})

    return results


def fetch_from_travel_alerts(road_name: str) -> list[dict]:
    """Fetch current Travel Alerts and filter to ones whose title starts
    with road_name. Returns closures in the standard flat record shape --
    with no start/end time (see module docstring) and validity_status
    always "active" (an alert only appears on this page while it's
    ongoing; National Highways removes it once resolved)."""
    print(f"Fetching {TRAVEL_ALERTS_URL} ...")
    req = urllib.request.Request(TRAVEL_ALERTS_URL, headers={"User-Agent": "route-closures-build/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        print(f"Warning: HTTP {e.code} {e.reason} fetching Travel Alerts -- skipping this source.")
        return []
    except Exception as e:  # noqa: BLE001 -- this source is best-effort, never fatal
        print(f"Warning: failed to fetch Travel Alerts ({e}) -- skipping this source.")
        return []

    cards = parse_alert_cards(html)
    print(f"  found {len(cards)} total alert(s) on the page (all roads)")

    results = []
    for card in cards:
        m = LEADING_ROAD_TOKEN_RE.match(card["title"])
        if not m or m.group(1).upper() != road_name.upper():
            continue

        # Subtitle is typically "<County> - <Cause> - <Impact> - <Direction>".
        parts = [p.strip() for p in card["subtitle"].split(" - ") if p.strip()]
        direction = parts[-1] if parts else ""
        cause = parts[1] if len(parts) > 1 else (parts[0] if parts else "")

        location_text = strip_other_road_junctions(card["title"], road_name)

        slug = card["href"].rstrip("/").rsplit("/", 1)[-1] or str(hash(card["href"]))
        results.append({
            "record_id": f"travelalert-{slug}",
            "road_name": road_name,
            "direction": direction,
            "location_description": location_text,
            "comment": card["subtitle"],
            "start_datetime": "",
            "end_datetime": "",
            "validity_status": "active",
            "cause_type": cause,
            "lanes_restricted": None,
            "lanes_operational": None,
            "source_label": "Travel Alert (major incident)",
        })

    print(f"  {len(results)} match {road_name}")
    if not results:
        print(f"  (this is normal -- Travel Alerts typically has only 2-3 entries "
              f"covering the whole English network at any moment)")

    return results
