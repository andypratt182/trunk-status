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

## Project structure

```
build.py                        orchestration: load config, call sources, render templates
matching.py                     shared route/leg matching (junction extraction, sorting, filtering)
sources/
  national_highways.py          live API + flat JSON mirror
  xlsx_advance_notice.py        National Highways advance-notice spreadsheet
  traffic_scotland.py           Traffic Scotland scraper (M74)
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
a minute apart, not one line. Closures with no such section fall back
to a single row using the overall Starting/Ending, same as before.

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
