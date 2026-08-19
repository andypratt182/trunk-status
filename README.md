# Route Disruptions

A small static site that shows current/upcoming roadworks for a handful of
hand-picked road sections ("routes"), each split into northbound and
southbound pages -- and each direction can chain together several roads
(e.g. M74 -> M6 -> M58 -> M57) to model a real driving route. Built with Python +
Jinja2, rebuilt automatically on a schedule by GitHub Actions, and published
with GitHub Pages.

## How it works

1. `routes.yaml` -- you define your routes here: road name, junction range,
   and direction for each leg of each route, plus which data source to use.
2. `build.py` -- orchestration only: loads `routes.yaml`, calls each
   configured source, matches closures to routes, and renders `_site/`.
   The actual data-fetching logic lives in `sources/` (one module per
   source), and the shared route/leg matching logic lives in `matching.py`
   -- see "Project structure" below.
3. `.github/workflows/build-deploy.yml` -- runs `build.py` on a schedule
   (default: every 10 minutes) and publishes `_site/` to GitHub Pages.

**Static asset cache-busting**: `static/style.css` and `static/day-filter.js`
are referenced with a `?v=<content hash>` query string (`content_hash()`
in `build.py`), so a genuine change to either file always forces a fresh
fetch. The HTML itself is always fresh on every build (it has a new "Page
built" timestamp baked in every time), but without this, a browser or
GitHub's CDN could keep serving a stale cached copy of the CSS/JS
specifically, indefinitely, even once the HTML on the page is visibly a
new build — this is why a CSS fix can sometimes not show up on a phone
even after a successful rebuild. The hash only changes when the file's
actual content changes, so it doesn't force a refetch on every rebuild.

## Normalized location display

Each source formats its location text completely differently: Traffic
Scotland's raw text puts the direction at the *end* of the string (e.g.
"M74 J8 - J9 SB"), National Highways' traffic-search API spells
"northbound" out in a full sentence ("The M6 northbound between
junctions J9 and J11"), and junctions show up as both "J9" and "Jct 9"
depending on the source. Rather than try to reformat each source's free
text (fragile across that many formats), `matching.format_location()`
builds a consistent `"M74(S) J9"` style summary from the already-
extracted structured fields every row has anyway (`resolve_road_name()`,
the leg's own canonical `data_direction`, `extract_junctions()`) — reused
directly, not re-parsed. A closure spanning two junctions shows as
`"M74(S) J8-J9"`; one whose junction only came from the comment fallback
(e.g. a diversion instruction like "leave the motorway at J22") gets a
`"(near)"` qualifier, since that's an inferred proxy for the closure's
location, not a stated fact about where it is.

Using the *leg's own* `data_direction` rather than each closure's own
direction field is deliberate: every closure reaching this point has
already been confirmed to match that leg's direction (including the
"Both directions" wildcard case), so the leg's own canonical value is
both simpler and more reliable than re-parsing whatever format each
source happened to use for its own direction field.

**Slip-road terminology is detected and normalized too**, via
`matching.detect_slip_road()` — real variants seen across sources:
Traffic Scotland uses "Offslip" (no separator), "slip off" (reversed
order), "Slip Off" (capitalized), plausible "Onslip"/"on-slip"/"slip on"
equivalents, and bare "slip road" with no on/off specified; National
Highways instead uses the official UK terminology, "entry slip road"/
"exit slip road" — a real bug caught in production: the original regex
only recognized "on"/"off" next to "slip" (Traffic Scotland's style), so
every National Highways slip-road entry fell through to the generic
"Slip road" label with no direction, since "entry"/"exit" were never
recognized at all. All variants normalize to "Entry Slip Road", "Exit
Slip Road", or "Slip road" and append as a qualifier, e.g. `"M74(S) J9
(Exit Slip Road)"`. This was added as a deliberate second pass: the
first version of the normalized location dropped slip-road information
entirely, which is a real, meaningful distinction (a slip-road closure
behaves very differently from a mainline one), not just descriptive
noise safe to strip out. Qualifiers combine into one parenthetical when
more than one applies, e.g. `"M74(S) J22 (Exit Slip Road, near)"`,
rather than stacking separate parenthetical groups.

**Deliberately scans ONLY the raw location text, never the comment/
diversion text, for slip-road detection.** A real bug caught in
production: a genuine mainline closure ("M74 SB Jct 7 to Jct 8 - Road
closure") was misclassified as a slip-road closure, because its
diversion instructions incidentally said "...rejoin M74 south jct 8 on
slip" — describing how traffic gets back onto the motorway at the end of
the diversion, nothing to do with what the closure itself is. `choose_icon()`
was affected the same way (it independently re-scanned the same
diversion text with its own keyword list) and has been changed to check
the already-normalized location qualifier instead, which also guarantees
the icon and the visible location text can never disagree with each
other.

**Named service stations (e.g. "Gretna Services") are preferred over a
fallback-derived junction number**, via `matching.extract_services_name()`.
A service station doesn't have its own junction number — it sits
*between* two — so a fallback junction pulled from a diversion (e.g.
"J21 SB Offslip", routing traffic to use J21's own slip road since the
service station's own is closed) was showing as `"M74(S) J21 (near)"`,
implying the closure is at/near J21 specifically. Gretna Services
actually sits between J21 and J22; the diversion's junction is a real,
useful routing detail, but it's an inferred proxy for location, not a
stated fact the way the station's own name is. Scoped specifically to
the fallback case — a junction stated directly in the location text is
reliable and isn't overridden by this. The junction is still used
correctly for leg *matching* internally either way; only the *displayed*
text changes. The place-name regex requires proper Title Case (capital +
lowercase, e.g. "Gretna") rather than any capitalized word — a real bug
caught while building this: it first matched "SB Gretna Services"
instead of just "Gretna Services", since direction codes like "SB" also
start with a capital letter.

**Nothing is discarded** — each source's original, more descriptive text
(place names, closure type, lane specifics) moves into the comment/More
Info section instead of the primary label, rather than being dropped.
Confirmed directly: a real M6 entry's normalized location is `"M6(S)
J40"`, while its More Info panel still shows the full original text,
"M6 southbound within J40 — M6 Southbound Jct 40 to 39 Lane 3 closure
with Narrow lanes (Mp 459/4 - 455/7)".

## More Info column

Diversion text (Traffic Scotland: `"Diversion: <text>"`, added by
`sources/traffic_scotland.py` itself, not part of the raw source) and
XLSX advance-notice's "Overall Scheme Details" text were previously
shown inline under Location, which cluttered it — especially for the
many M74 entries that have a diversion. Moved to its own column, between
End and Source, as a native HTML `<details>/<summary>` disclosure — the
collapse/expand behaviour (including the triangle marker) comes free
from the browser, no JS needed. Only rendered when `row.comment` is set
and differs from `row.location` (unchanged from before, just relocated).

The empty case needed care: Jinja's default whitespace handling leaves
newlines/indentation in the output even when the `{% if %}` is false, so
a naive `{% if %}...{% endif %}` inside the `<td>` produces whitespace,
not a truly empty element — and CSS's `:empty` selector (used to
collapse this cell's own padding when there's nothing in it) requires
exactly zero child nodes, including whitespace text nodes, to match.
Fixed with Jinja's `{%-`/`-%}` trim markers placed so the `<td>` renders
as `<td ...></td>` with literally zero characters between the tags when
there's no comment — confirmed with a test that checks the raw rendered
HTML, not just that the visible page looks right.

## Named termini and cross-road junction continuations

Two related, deliberately narrow display improvements, both from real
reported entries where a bare junction number alone was misleading:

**A junction paired with a named terminus** (e.g. "M58 westbound jct 1
to Switch Island carriageway closure" — Switch Island, Merseyside, is
where the M58 physically ends and meets the M57/A5036).
`matching.extract_junction_to_place()` detects the "J&lt;N&gt; to
&lt;Place&gt;" pattern in raw location text and, once the extracted
junction number is confirmed to match one already found, shows `"M58(W)
J1 - Switch Island"` instead of just `"M58(W) J1"` — the bare number
alone doesn't convey that the closure reaches all the way to Switch
Island. Unlike the service-station case above, this isn't scoped to the
fallback-junction case specifically — the junction here *is* stated
directly, it's just paired with a place name for the range's other end.

**An out-of-range junction gets labeled with the road it actually
belongs to.** M74 Southbound becomes M6 Southbound at J22/J45 in this
project's own configured routes (both Axis and Omega). Traffic
Scotland's raw text sometimes states a range extending past M74's own
junctions — e.g. "M74 SB J22 - J45", where J45 doesn't exist as an M74
junction at all (M74 tops out around J22 in this project's own
configuration) — it's the M6's J45, on the other side of the merge point.
`matching.label_junction_for_display()` checks whether a junction falls
outside the *leg's own configured range* (`j_from`/`j_to`, already
available in `rows_for_leg`) and, if so, prefixes it with a known
continuing road's name: `"M74(S) J22 - M6 J45"` instead of the
misleading `"M74(S) J22-J45"`.

**This one is deliberately hardcoded and narrow, unlike everything else
in this file.** Every other normalization in this project derives
purely from the text itself; this one requires knowing that M74 doesn't
have a J45 at all, which is real-world road topology knowledge that
can't be derived from text alone — it has to come from somewhere, and
the leg's own configured range is the only source of that knowledge
already available here. `matching._KNOWN_ROAD_CONTINUATIONS` is a
one-entry dict (`{"M74": "M6"}`) specific to this project's own routes.
If the route configuration ever changes — a different road connecting
to M74, or this M74→M6 sequence being removed — this mapping needs
updating by hand; it will not automatically infer a different
continuation from `routes.yaml`. Confirmed with tests that a normal
same-road range (both junctions in range) is completely unaffected and
still uses the plain no-space hyphen, and that M6 itself (the
continuation *target*, not source) has no continuation configured and
is left alone.

## M6/M62 interchange "link road" filtering (narrow, direction-specific)

The Croft Interchange (M6 ~J21 / M62 ~J10) connects the M6 and M62 via
several physical slip roads, which National Highways calls "link
roads" — real confirmed text: "M62 Westbound to M6 Southbound link
road closure". There are 8 possible direction combinations at this one
interchange (2 M6 directions × 2 M62 directions, each as source or
destination), but only 2 correspond to the actual path this project's
Omega route takes through it: M6 South → M62 West (the southbound leg
continues this way), and M62 East → M6 North (the northbound leg comes
from this way). The other 6 describe link roads serving a completely
different journey through the *same* interchange — genuine, real
closures, just not relevant to Omega's route, even though they mention
both M6 and M62 by name and would otherwise match either leg.

`matching.is_excluded_m6_m62_link_road()` parses the "&lt;Road&gt;
&lt;Direction&gt;bound to &lt;Road&gt; &lt;Direction&gt;bound link road"
pattern and checks it against an explicit allow-list of exactly the two
combinations Omega's route actually uses — necessarily an allow-list,
not a derived rule, since there's no way to tell "does this link serve
Omega's route" from the text alone. **Only 1 of the 8 combinations was
independently confirmed against real text** (the excluded example
above); the other 7 (including both allowed ones) are inferred to
follow the same phrasing convention, on the assumption this is a
templated/auto-generated description. The regex matches `M6`/`M62`
literally rather than a generic road pattern, so it can never fire for
a different interchange (e.g. M56/M6) even if that also happened to use
"link road" phrasing — confirmed with a test. Applied independently of
which leg/direction is currently being built (unlike the M61/M6 rule
below), since the same closure could otherwise match either the M6 leg
or the M62 leg and should be excluded from both if it isn't one of the
two allowed combinations.

**A real bug was caught and fixed while building this**, not in the
logic itself but in how it got written: `_ALLOWED_M6_M62_LINKS` and
`_LINK_ROAD_RE` were accidentally defined *twice* in the file, and
because Python resolves module-level names at call time rather than at
function-definition time, the second (differently-cased) definition was
silently overriding the first — causing every lowercase key lookup to
fail, misclassifying every combination as excluded. Caught by testing
the full 8-combination table directly rather than just the one
confirmed real example, which happened to still work by coincidence
(both parts of the duplicate were internally self-consistent for that
one case). Consolidated into a single definition of each; only one
`_LINK_ROAD_RE` and one `_ALLOWED_M6_M62_LINKS` exist now.

## M61/M6 merge exclusion (narrow, road-specific)

National Highways describes an M61-origin closure reaching the M61/M6
merge point (a real, well-known interchange near Preston — the M61
terminates into the M6 northbound at J30) with a `location_description`
like "M6 northbound between J30 and J31" — even though the closure is
fundamentally on the M61, not the M6 mainline. The `comment` field
reveals the real picture: "M61 Northbound Jct 9 to M6 Jct 30 carriageway
closure." `matching.is_m61_m6_merge_closure()` detects this specific
"M61 ... to M6" phrasing and excludes it from the M6 northbound leg.

Deliberately scoped **narrow** — `road_name == "M6"` and northbound
only, matching the exact real reported case — rather than a general
"any cross-road mention" rule. A broad rule risks hiding genuinely
relevant M6 closures that just happen to mention another road as part
of a diversion route, the same false-positive class already fixed twice
elsewhere in this project (Traffic Scotland's M8/M74 junction
contamination, Travel Alerts' A31/M27 boundary reference). Kept as a
separate function rather than folded into `closure_matches_leg()`
itself, so it's easy to find, adjust, or remove later without touching
the general-purpose matcher every other road relies on. Confirmed with
tests that an M6 closure merely *mentioning* the M61 (e.g. as an
alternative diversion route) is correctly left alone — only the
specific merge-point phrasing triggers the exclusion — and that the
same phrasing on a different road entirely (M57) has no effect, since
the rule is scoped to M6 by name, not by general text matching.

## Row icons

Each closure/incident row shows a small icon (`static/logos/*.png`),
chosen by `matching.choose_icon()` from the row's cause/location/comment/
lane text — checked in priority order, most specific/actionable first:

1. `accident.png` — collision, fire, congestion, queue, breakdown, or
   similar incident-flavoured cause
2. `slip_road_closed.png` — location mentions a slip road specifically
   (e.g. "M74 J9 Offslip SB")
3. `road_closed.png` — a full/total/carriageway closure, or
   `lanes_operational == 0`
4. `lane_closure.png` — a real lane-restriction number, "lane closure"
   text, or "single lane running" text (in either `lane_info` or the
   description/comment), even when the underlying cause is ordinary
   roadworks (this is deliberately impact-based: a roadworks entry that
   closes one lane gets the lane-closure icon, not the generic
   roadworks one)
5. `roadworks.png` — fallback when nothing more specific matched; the
   most common case in practice

The five PNGs are supplied by the project owner directly into
`static/logos/` — not part of this codebase's own files, and not
regenerated or overwritten by any build step. `build.py`'s existing
`shutil.copytree(STATIC_DIR, ...)` already copies the whole `static/`
directory on every build, so nothing extra was needed to make icons
ship correctly — dropping a new PNG in `static/logos/` and referencing
its filename in `choose_icon()` is the entire integration surface.

**Desktop**: its own dedicated column between Status and Location
(`<th class="icon-header">`/`<td class="icon-cell">`).

**Mobile card view**: the icon (80×45px, larger than the 20×20px desktop
column version) is pinned to the card's top-right corner via
`position: absolute`, aligned with the top of Status and matching the
card's own padding (14px/16px) on both the top and right — not its own
full-width card row like every other field, and not left to wherever it
happens to fall in document flow. This went through two earlier
approaches first: `position: absolute` was tried originally, but
abandoned when the icon was sized much larger (160×90) and visibly
covered the Location heading underneath it, since absolute positioning
removes an element from the document flow entirely and nothing else
"knows" to leave room for it. Switched to `float: right`, which let text
wrap around it automatically at any size — safer, but positioned the
icon wherever it fell in flow (next to Location) rather than at the
card's actual top-right corner. Back to `position: absolute` once the
icon settled at 80×45, confirmed visually (not assumed) that there's no
overlap with Location's text at this smaller size. Requires the `<tr>`
itself to be `position: relative`, since the card's border/padding/
rounded corners all live on the `<tr>` in this design, not a wrapping
`<div>`. Its own `::before` "TYPE" label is suppressed — a corner icon
like this reads as decoration, not a labelled field like the others.

## Project structure

```
build.py                        orchestration: load config, call sources, render templates
matching.py                     shared route/leg matching (junction extraction, sorting, filtering)
sources/
  national_highways.py          live API + flat JSON mirror
  national_highways_traffic_search.py  live traffic search (unofficial beta)
  xlsx_advance_notice.py        National Highways advance-notice spreadsheet
  traffic_scotland.py           Traffic Scotland scraper (M74)
  travel_alerts.py               National Highways Travel Alerts scraper (major incidents)
  scotland_incidents.py          Traffic Scotland incidents scraper (M74)
test_build.py                   test suite for matching.py + all three sources
routes.yaml                     route/leg definitions + data source config
templates/, static/             Jinja templates and the day-filter JS/CSS
```

Each source module exposes a `fetch_*(...) -> list[dict]` function that
returns closures in one common flat shape (`road_name`, `direction`,
`location_description`, `comment`, `start_datetime`, `end_datetime`,
`validity_status`, `cause_type`, `lanes_restricted`, `lanes_operational`,
`source_label`, `record_id`) -- everything downstream in `matching.py`
works identically regardless of which source a closure came from. Adding
a new source later means adding a new module here, not editing the
others.

`cause_type` in particular comes in two shapes depending on the source:
camelCase machine identifiers (National Highways' DATEX `causeType`,
e.g. `"roadMaintenance"`; this project's own XLSX-source placeholder,
`"advanceNoticeFullClosure"`) and already-human text (Traffic Scotland's
"Works:" field, e.g. `"Barrier Repair, Filter Drain"`). `matching.humanize_cause()`
splits the first kind into words (`"roadMaintenance"` -> `"Road maintenance"`)
and leaves the second kind's casing alone -- a bare `capitalize()` filter
would otherwise turn a camelCase identifier into one long run-together
word (`"advanceNoticeFullClosure"` -> `"Advancenoticefullclosure"`, a
real bug this project had) *and* forcibly lowercase already-well-cased
human text.

### Running the tests

```bash
python test_build.py
```

Covers `matching.py`'s junction extraction/sorting/leg-matching, and each
source module -- several tests use real page/API content captured from
live fetches rather than synthetic data (noted in the test names).

## Data source: the live National Highways API (default)

This is the official Road & Lane Closures v2 API, documented at
https://developer.data.nationalhighways.co.uk. It needs a free API key.

**Get a key:**
1. Register an account at https://developer.data.nationalhighways.co.uk
2. Sign in, go to **Subscriptions**, subscribe to *Road and Lane Closures*
3. Go to your **Profile** to find your API key

**Add it to GitHub Actions:**
1. In your repo, go to **Settings -> Secrets and variables -> Actions**
2. Click **New repository secret**
3. Name: `NATIONAL_HIGHWAYS_API_KEY`, Value: your key
4. The workflow already references this secret -- no further setup needed

**Run it locally:**
```bash
export NATIONAL_HIGHWAYS_API_KEY=your-key-here   # Windows: set NATIONAL_HIGHWAYS_API_KEY=...
python build.py
```

Notes on this data source:
- The API returns a nested DATEX II JSON payload; `build.py` flattens it
  into the same simple record shape used throughout the script
  (`normalize_datex_response()`), so route matching works identically
  regardless of which source you use.
- `lookahead_days` in `routes.yaml` (default 30, and clamped to 30 if set
  higher) controls how far ahead closures are fetched. The API enforces a
  hard maximum 30-day window between `startDateTime` and `endDateTime` and
  returns an HTTP 500 if you exceed it -- and its own default window is
  much narrower still (effectively "today") if no date range is given at
  all, so this is set explicitly to make sure upcoming roadworks show up.
- **`closureType`: "both" is fetched as two explicit requests, not by
  omitting the parameter.** Omitting `closureType` was originally assumed
  to mean "both planned and unplanned" (per this API's apparent design),
  but that assumption was never verified against a live response -- and
  turned out to be wrong. A real run with `closure_type: null` returned
  only `roadMaintenance`/`constructionWork`/`authorityOperation` causes
  across 3,201 records, with zero unplanned ones (confirmed separately,
  since an *earlier* real run — closer in time — did return 4 genuinely
  unplanned records once the fix below was in place; the network simply
  had 0 active unplanned closures at that first snapshot, which is a
  normal, non-alarming result). `closure_type: null` now makes two
  separate requests (`closureType=planned` and `closureType=unplanned`)
  and merges the results, rather than trusting an unverified default.
  Setting `closure_type` to an explicit single value still makes just one
  request, unchanged.
- **Each closure is tagged `closure_category: "planned"` or `"unplanned"`
  based on which query actually returned it — not guessed from `cause_type`
  text.** An early version tried to spot incidents by keyword-matching
  `cause_type` (looking for words like "accident" or "collision"), but a
  real unplanned closure turned out to have the generic, bureaucratic-sounding
  `cause_type` of `"roadOrCarriagewayOrLaneManagement"` — indistinguishable
  by text from ordinary planned maintenance. The `closureType` the API was
  actually asked for is the one reliable signal for "was this unplanned",
  so that's what gets carried through now.
- Rate limit: 10 calls/minute per key. This build makes 1 or 2 calls per
  run depending on `closure_type` (2 when left as `null`/"both"), so this
  is not a concern even at a frequent rebuild schedule.
- Known data limitation (per National Highways' own docs): closures are
  only reported where physical signs/signals (VSS) are actively set on
  the network, so some real-world closures may not appear.
- The build log prints a `cause_type` breakdown **per closureType category**
  every run (`cause_type breakdown for closureType=unplanned (N records): ...`),
  so it's easy to see at a glance what's actually coming through and
  decide whether it's worth adding a visual "Incident"/"Unplanned"
  distinction on the site.

## Data source: flat JSON mirror (alternative, no API key)

If you'd rather not deal with an API key, `routes.yaml` has a commented-out
`source: "flat_json"` option pointing at a pre-flattened JSON snapshot with
the same field names build.py expects (`road_name`, `direction`,
`location_description`, etc.). Swap the `site:` block in `routes.yaml` to
use this instead -- see the comments in that file.

## Mobile closures view

On narrow screens (≤640px), the closures table restyles into one bordered
card per closure instead of a horizontally-scrolling table. This is
pure CSS on the *same* `<tr>`/`<td>` markup as the desktop table (each
`<td>` has a `data-label` attribute, shown via a CSS `::before` at that
width) — nothing in the DOM changes, so the day-filter and live-status
JS keep working completely unchanged; they operate on the same elements
either way. One thing this requires: `table.closures tr[hidden] { display:
none; }` explicitly overrides the responsive `display: block` rule for
hidden rows, since a plain attribute selector like `[hidden]` alone has
lower CSS specificity than a compound one like `table.closures tr` and
would otherwise lose that fight, leaving day-filtered-out rows visibly
showing as cards.

## Day filter

The all-routes page (`index.html`) has a row of filter buttons — **Today**,
**Tomorrow**, named weekdays out to 7 days (matching the advance-notice
report's own window; the API can have data further out, still visible
under "All"), and **All** last. Selecting a day there:

- recomputes each route/direction's closure count live on the index page
  itself (from embedded per-closure date data, no rebuild needed), and
- carries through to whichever route page you click into next, via
  `localStorage` — that page's table is pre-filtered to the same day on
  load, with a small hint ("Showing disruptions for Tomorrow — change this on
  the all routes page") linking back to the index page to change it. There
  are no buttons on the route pages themselves.

Defaults to **Today** on a first-ever visit (no stored preference yet).
This all runs client-side — the JS lives in `static/day-filter.js`.

**Already-ended closures are hidden under any specific-day filter.**
"Today" (and every other day option) is calendar-day overlap plus a check
that the closure hasn't already fully finished — a closure running 22:00
yesterday to 06:00 today technically overlaps today's calendar date, but
by mid-morning it's simply over and showing it under "what's happening
today" is misleading. This only ever affects "Today" in practice, since
every other day option is entirely in the future by construction and
can't already have ended. **All** is unaffected — it's the explicit
"show me everything" view, past included, and never applies this check.

## Additional source: National Highways live traffic search (unofficial, beta)

An internal endpoint (`/trafficsearchapi/events?road=<ROAD>`) found by
inspecting network requests on National Highways' new, still-in-beta
"Check current incidents, disruptions and delays" page — that page
shows placeholder/Lorem-Ipsum content on initial load and only fetches
real data once you search, so the underlying API call had to be found
via the browser's network inspector rather than by reading the page's
static HTML. **Not auto-enabled** — add a `type:
"national_highways_traffic_search"` entry under `additional_sources` per
English road (commented-out example already in `routes.yaml`).

**Unofficial and undocumented, unlike everything else this project
talks to.** This isn't a published, versioned product on National
Highways' developer portal — it's an internal endpoint powering one
specific page's search box, found by inspecting network traffic while
testing that page. Treat it as more fragile than every other source in
this project: it could change shape or be withdrawn without notice at
any time.

**Fills a genuinely different gap.** This is real-time traffic-*flow*
data (`"reason": "Congestion"`, `"type": "AbnormalTraffic"`) — distinct
from scheduled roadworks (the main API / XLSX report) and from Travel
Alerts' hand-curated, highest-severity-only incidents. A situation here
can have a real machine-readable end time (`returnToNormal`) when known
— most other incident-style sources in this project have no end time at
all.

**Clean JSON in, almost no custom parsing needed.** Unlike every scraped
source in this project, this is real structured JSON with a clean
`location` field (e.g. "The M6 northbound between junctions J9 and
J11") that the *existing* shared junction-extraction logic in
`matching.py` already parses correctly with zero new code — verified
directly against real production data. The API is also already
filtered server-side by the `road=` query parameter, so (unlike every
Scotland/Travel-Alerts source) no cross-road junction-contamination
guard was needed here — though a cheap defensive road check is still
applied in case that ever changes.

**England only** (M6/M57/M58/M62 in this project's own route config) —
Scotland's equivalent live-incidents source is
`sources/scotland_incidents.py`, a completely separate module for a
completely separate country's road network.

## Additional source: Traffic Scotland incidents

Traffic Scotland's public "Current Incidents" page
(`/traffic-information/incidents`), complementing the roadworks scraper
above with live incidents — queues, breakdowns, and closures. **Not
auto-enabled** — add a `type: "scotland_incidents_scraper"` entry under
`additional_sources` per road (commented-out example already in
`routes.yaml`).

**Fully self-contained** — `sources/scotland_incidents.py` has no
imports from `sources/traffic_scotland.py`, deliberately, so a change to
one Scotland source can never affect the other.

**Single-stage, unlike the roadworks scraper.** Every field worth having
(Direction, Incident type, Start time, and either a lane-restriction
count or a free-text description) is already on the listing page
itself — no separate detail-page fetch needed.

**No end time, same reasoning as Travel Alerts.** This page has no
structured end time anywhere, only a start time. `validity_status` is
always `"active"` — an incident only appears on this page while it's
ongoing.

**Known limitation, left deliberately unresolved for now**: some
`Incident type: Closure` entries turned out to actually be
roadworks-caused (e.g. one read "closed... to allow for essential
roadworks"), so the incident type label alone isn't a fully reliable way
to separate "genuine live incident" from "roadworks also listed here."
This means the same physical closure could plausibly appear twice — once
via this source, once via the roadworks/planned-roadworks scraper.
Real-world testing (including whether this overlap actually happens for
your specific routes, and what to do about it if so) was deliberately
deferred until there's a live M74 incident to check the full pipeline
against, rather than guessing at a deduplication rule now.

**Cross-road junction contamination guard, extended for a new pattern.**
Same lesson as the roadworks scraper and Travel Alerts, but with a
real hyphenated variant seen here that a space-only regex would miss:
"A737 M8-J29 North - Slip Off" has no space between the road name and
the junction. `strip_other_road_junctions()` here handles both the
space and hyphen forms.

**"Both directions" wildcard, extended for Traffic Scotland's own
phrasing.** A real A9 closure used `"Northbound & Southbound"` instead
of National Highways' `"Both directions"` — confirming this needed more
than one exact string. Both are now in `matching.BOTH_DIRECTIONS_VALUES`.

**Unverified against live markup** (same caveat as every other scraper
in this project). If a live run logs `found 0 total incident(s)`, check
`parse_incident_cards()` against the page's real structure.

## Additional source: Travel Alerts (major incidents)

National Highways' public "Travel Alerts" page lists the highest-priority
current incidents (major collisions, fires, serious closures) across the
*entire* English strategic network — typically only 2-3 entries at any
given moment. **Not auto-enabled** — add a
`type: "travel_alerts_scraper"` entry per road you want covered under
`additional_sources` in `routes.yaml` (commented-out example already
there).

**This is a genuinely separate data source from the API's own
"unplanned" `closureType`**, confirmed directly: on the same day, the
same build, the API's unplanned closures were 4 generic
`roadOrCarriagewayOrLaneManagement`-tagged records, while Travel Alerts
showed 3 completely different, specific, named incidents (an M6
collision, an A31 fire closure, an A303 vehicle fire) that never
appeared via the API at all. They're two separate systems at National
Highways, not two views onto the same data.

**No start/end time, deliberately.** Unlike every other source in this
project, Travel Alerts has no structured schedule anywhere — not even on
the individual incident pages, which bury timing in narrative prose
("occurred at approximately 04:10 on the morning of..."). Rather than
attempt fragile natural-language date parsing, this source doesn't try:
an alert is `validity_status: "active"` for as long as it appears on the
listing page (National Highways removes it once resolved), with blank
Start/End columns — an honest representation of what the data actually
is, not a gap to paper over.

**Cross-road junction contamination guard, same lesson as Traffic
Scotland.** Titles like "A31 - Between M27 J2 and A338" reference a
*different* road's junction (M27's) as a location marker for where the
A31 closure is — `strip_other_road_junctions()` removes any `<ROAD> J<N>`
mention belonging to a road other than the target before junction
matching runs, so a coincidental in-range junction number on the
*wrong* road can't be mistaken for one on the target road. Bare
junctions with no road name attached (e.g. "Between J15 and J16") are
always trusted.

**"Both directions" is treated as a wildcard**, matching either
direction's leg — real Travel Alerts frequently report this (2 of the
3 checked while building this feature did), and without this,
`closure_matches_leg()`'s exact-direction comparison would have matched
neither a route's northbound nor southbound leg, silently dropping the
alert from both. See `matching.BOTH_DIRECTIONS_VALUES`.

**A real "found 0 total alert(s)" bug was caught and fixed.** A live run
found 0 alerts on a day the page genuinely had 4 (A31, A303, A45, M1,
confirmed by fetching the page directly). Root cause: the real page
wraps each *entire* card (title + subtitle + "More details") in one
`<a>` with nested child elements, and `find_all("a", string=lambda...)`
— which every scraper in this project originally used to find "More
details" links — relies on BeautifulSoup's `.string` property, which
silently returns `None` (matching nothing) for any tag with more than
one child, even simple ones. Fixed by using `get_text()` instead, which
handles both a simple single-text link and a nested one correctly. Also
applied defensively to the two Scotland scrapers, which happened to work
by luck (their real "More details" links are simple single-text anchors)
but relied on the same fragile assumption. The test fixture for this
source was rewritten to use the real nested-anchor structure and the
real 4 alerts from that live check — the old fixture used a
simple-anchor structure that could never have caught this class of bug.
A parsing failure here is still just a warning, never a build-breaking
error.

## Additional source: advance-notice full closures (XLSX)

`routes.yaml` also layers in National Highways' public "7-day closure
report" by default — a spreadsheet of **full closures** (whole carriageway
shut, usually 8pm–6am) published up to 7 days ahead. This can show a
closure *before* it appears via the API, because the API only reports what's
currently signed on the road (see the note above), while this report is
published from the works schedule itself.

Rows from this source appear in the site's **Source** column as "Advance
notice (full closure)" so they're clearly distinguishable from live API
rows.

Real column headers, confirmed against a live download: `Road number`,
`Direction`, `Location`, `Scheduled start time`, `Scheduled end time`,
`Closure details, including diversions` — matched flexibly by keyword,
not exact position, in `XLSX_HEADER_SYNONYMS`.

Only the first `MAX_COLUMNS_TO_READ` (20) columns are read per sheet, even
though a sheet can *report* far more than that — some sheets have cell
formatting applied out to hundreds of otherwise-empty columns (seen on a
live "Tuesday" sheet), and `openpyxl` reports a sheet's "used" range out
to wherever formatting was ever applied, not just where real data is.
Reading all of that for every row is real overhead with no data in it, so
this is capped with generous headroom beyond the 6 known real columns.

To turn this off, delete the `additional_sources:` block from `routes.yaml`.

## Additional source: Traffic Scotland (M74)

Also layered in by default: a scraper for Traffic Scotland's roadworks
data, filtered to the M74. Unlike National Highways, Traffic Scotland
doesn't offer a simple self-service API key — their real-time feeds
require an approved-subscriber application — so this scrapes their public
pages instead, in two stages:

1. **Listing pages** — `/traffic-information/roadworks` (current) and
   `/traffic-information/planned-roadworks` (planned) — list every
   roadwork on Scotland's trunk road network as plain-text blocks
   (`Location:`/`Start time:`/`Description:`/`[More details]`). This
   stage finds every entry whose Location mentions M74 or A74(M) and
   grabs its "More details" link; the listing page's other fields aren't
   used for the final record.
2. **Each matched entry's own detail page**
   (`/more-details?sid=...&type=roadworks`) has clean, structured fields:
   Location (often has an explicit junction range, e.g. "M74 J8 - J9 SB"),
   Direction, Starting, Ending, and a Roadwork description (Works: /
   Traffic Management: / sometimes Diversion Information:). Critically,
   this is the only place a real **end date** is available — the listing
   pages never showed one.

Rows show as "Traffic Scotland (scraped)" in the Source column.

**Status is computed from real time, not the listing page — both at build
time and live in the browser.** Traffic Scotland's own current/planned
split is just which listing page an entry appeared on. `compute_validity_status()`
in `sources/traffic_scotland.py` checks whether "now" (UK local time)
actually falls within the entry's own start/end window at build time:
**active** only while that's true, **planned** otherwise (comparing
against `start` alone if `end` is missing, and only falling back to the
listing-page label if even `start` is unusable).

That alone isn't enough, though — a closure's real window is often just a
few hours (an overnight closure), so a status baked in at build time can
go stale well before the next scheduled rebuild. `static/day-filter.js`
also recomputes status **live in the browser** on page load, for Traffic
Scotland rows only (`data-source="Traffic Scotland (scraped)"`) — other
sources are left as server-rendered, since they can have a status (e.g.
"suspended") that isn't derivable from dates alone. This also feeds into
the index page's live active-count recompute (see the day filter section
below), so the two stay consistent with each other.

**Column layout.** Works (cause) shows in the table's own Cause column.
Traffic Management goes in the **Lanes** column (e.g. "Lane Closure
(40mph)", "Road Closure.") — it's not lane-count data the way National
Highways' numeric fields are, but Lanes was otherwise always empty for
this source, and Traffic Management is the closest equivalent info.
Some long-running entries publish Traffic Management as a per-date list
(one entry for every day across the whole closure, e.g. "15/10/2024 -
Portable Traffic Lights (TTLS), 16/10/2024 - ..."); `extract_tm_for_date()`
picks out just the entry matching each row's own date and drops the date
prefix, rather than showing that whole list. Diversion info, which has
nowhere else to go, is the only thing left in the location subtext.

**Activity Periods — expanded into individual rows, not one misleading
block.** Some closures also publish a "Days & times affected" section
giving the *exact* overnight windows they're actually active (e.g. "Thu
20th Aug - 22:00 to 23:59"), while their overall Starting/Ending dates
can span many weeks — the road usually isn't closed continuously for
that whole span, only on specific nights within it. When present,
`parse_detail_page()` returns **one row per actual closure
window** instead of a single row spanning the misleading overall range.
Periods either side of midnight (e.g. "22:00 to 23:59" then "00:00 to
06:00" the next day) are merged into one continuous window, since
that's how the site publishes an overnight closure — as two grid cells
a minute apart, not one line.

**Calendar-grid fallback, for when the Activity Periods list is empty
but the schedule grid isn't.** A real entry ("M74 J9 Offslip SB - Total
Closure") had a populated "Days & times affected" calendar — "Week
commencing 17th Aug", "Early Morning (00:00-06:00)" checked on Monday —
but a completely empty Activity Periods bulleted list underneath it. With
only the bulleted-list parser to go on, that closure fell back all the
way to its full Starting/Ending span (2nd Aug to 4th Sep — misleading,
since it's really only active one Monday morning).
`parse_calendar_grid_periods()` parses the calendar grid itself as a
second fallback: "Week commencing X" gives the anchor Monday, each band
(Early Morning / Evening) checked against a specific weekday gives the
day offset and a coarse time range. This is less precise than a real
Activity Periods line when one exists (a whole band, e.g. the full
00:00-06:00 "Early Morning" window, rather than exact minutes) — so it's
only ever used when the bulleted list is genuinely empty, never allowed
to override it. Closures with neither section at all still fall back to
a single row using the overall Starting/Ending, same as before.

**The M74/A74(M) alias**: the same physical road is signed "A74(M)" on
its southern stretch near the Scotland/England border, becoming "M74"
further north towards Glasgow. `build.py` treats these as the same road
(`SCOTLAND_ROAD_ALIASES`) and always normalizes the output to "M74", so
route legs just use `road_name: "M74"` regardless of which name a given
closure was published under.

**Known limitations**:
- **Cross-road closures only use the target road's own segment.** Some
  closures span two different roads (e.g. an M8/M74 interchange). When
  that happens, `isolate_road_segment()` splits the text at each road
  mention and keeps only the portion describing the target road, so a
  coincidental junction number belonging to the *other* road can't be
  mistaken for one of ours purely because both numbers appear together
  in one combined string — this was a real bug, not just a hypothetical
  one: an M8/M74 entry whose location read "M8 (...Jct 22) to M74 SB
  (...Jct 3a)" was incorrectly matching the M74 J8–22 leg on the M8's own
  "Jct 22", even though the closure was actually describing M74's own
  out-of-range Junction 3A. If, after isolating the target road's own
  segment, there's still no junction number to go on at all, the entry is
  excluded rather than guessed at — the build log reports how many
  entries were skipped this way.
- **No timezone given on the site** — start/end times are stored as
  naive local (UK) time, not authoritative to the minute across a DST
  boundary.
- **One extra HTTP request per matched entry.** Stage 2 fetches a detail
  page for every M74/A74(M) title found in stage 1 — fine for the
  handful of M74 entries expected at a time, but worth knowing this
  source makes more requests than the others.
- **Stage 1 tested against real page content.** Both `scotland_find_road_entries`
  (stage 1, listing pages) and `scotland_parse_detail_page` (stage 2,
  detail pages) have been tested against actual content fetched live from
  the site — including a real cross-road example (an A701 entry whose
  location mentions M74 only as part of a diversion route, correctly
  excluded by the cross-road guard since it has no M74-specific junction
  number). The one part that's still a live-markup assumption is the DOM
  structure `scotland_find_road_entries` walks up through to find each
  entry's containing block — it was verified against the real page's
  *text content*, not its exact HTML tags/classes, since the fetch tool
  used while building this converts pages to text. If a live run logs
  `found 0 ... entries` on a listing page, that's the signal to check
  `scotland_find_road_entries()` against the real markup — a fetch
  failure on any individual detail page is logged and skipped rather
  than failing the whole build.

## Set up your routes

Edit `routes.yaml`. Each route has a northbound and southbound direction,
and each direction is a list of one or more `legs`:

- `road_name` -- must match how the road appears in the feed (e.g. `"M6"`)
- `junction_from` / `junction_to` -- the section you care about (leave both
  as `null` for "entire road", e.g. for a short road with no useful
  junction breakdown)
- `data_direction` -- the literal value the feed uses for that road:
  `northBound` / `southBound` / `eastBound` / `westBound`. Not every road
  is signed north/south (e.g. the M62 runs mostly east/west) -- check the
  feed if unsure. `label` controls what visitors see regardless.

## Run it locally

```bash
python -m venv .venv
source .venv/bin/activate      # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
export NATIONAL_HIGHWAYS_API_KEY=your-key-here   # only if using the live API
python build.py
```

Then open `_site/index.html` in a browser.

## Publish on GitHub Pages

1. Push this repo to GitHub.
2. If using the live API, add the `NATIONAL_HIGHWAYS_API_KEY` secret (see
   above) -- do this before the first run, or the build will fail with a
   clear error telling you the secret is missing.
3. In the repo, go to **Settings -> Pages** and set **Source** to
   **GitHub Actions**.
4. The workflow runs automatically on push, on its schedule, and can be
   triggered manually from the **Actions** tab (**Run workflow**).
5. Your site will be published at
   `https://<your-username>.github.io/<repo-name>/`.

## Adjusting the rebuild schedule

Edit the `cron` line in `.github/workflows/build-deploy.yml`. It's currently
`*/10 * * * *` (every 10 minutes, UTC). Use https://crontab.guru to build a
different schedule.
