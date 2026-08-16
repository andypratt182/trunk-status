# Route Closures

A small static site that shows current/upcoming roadworks for a handful of
hand-picked road sections ("routes"), each split into northbound and
southbound pages -- and each direction can chain together several roads
(e.g. M74 -> M6 -> M58 -> M57) to model a real driving route. Built with Python +
Jinja2, rebuilt automatically on a schedule by GitHub Actions, and published
with GitHub Pages.

## How it works

1. `routes.yaml` -- you define your routes here: road name, junction range,
   and direction for each leg of each route, plus which data source to use.
2. `build.py` -- fetches closures (from the live National Highways API or a
   flat JSON mirror, see below), filters them per leg, and renders static
   HTML into `_site/`.
3. `.github/workflows/build-deploy.yml` -- runs `build.py` on a schedule
   (default: every 10 minutes) and publishes `_site/` to GitHub Pages.

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
- Rate limit: 10 calls/minute per key. This build makes 1 call per run,
  so this is not a concern even at a frequent rebuild schedule.
- Known data limitation (per National Highways' own docs): closures are
  only reported where physical signs/signals (VSS) are actively set on
  the network, so some real-world closures may not appear.

## Data source: flat JSON mirror (alternative, no API key)

If you'd rather not deal with an API key, `routes.yaml` has a commented-out
`source: "flat_json"` option pointing at a pre-flattened JSON snapshot with
the same field names build.py expects (`road_name`, `direction`,
`location_description`, etc.). Swap the `site:` block in `routes.yaml` to
use this instead -- see the comments in that file.

## Day filter

The all-routes page (`index.html`) has a row of filter buttons — **Today**,
**Tomorrow**, named weekdays out to 7 days (matching the advance-notice
report's own window; the API can have data further out, still visible
under "All"), and **All** last. Selecting a day there:

- recomputes each route/direction's closure count live on the index page
  itself (from embedded per-closure date data, no rebuild needed), and
- carries through to whichever route page you click into next, via
  `localStorage` — that page's table is pre-filtered to the same day on
  load, with a small hint ("Showing closures for Tomorrow — change this on
  the all routes page") linking back to the index page to change it. There
  are no buttons on the route pages themselves.

Defaults to **Today** on a first-ever visit (no stored preference yet).
This all runs client-side — the JS lives in `static/day-filter.js`.

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

**This is unverified against a real download** — I wrote the column-matching
flexibly (by keyword, not exact position) and with verbose logging rather
than against a confirmed schema, since I couldn't fetch the actual binary
file to inspect it directly. On your first live build, check the Actions
log for lines like:

```
sheet 'Friday 14 August' headers: ('Road', 'Direction', 'Location', ...)
sheet 'Friday 14 August': parsed 12 closure rows
```

If a sheet instead logs `no recognizable 'road' column`, or the parsed row
count is unexpectedly 0, share that log output and the column-matching in
`build.py` (`XLSX_HEADER_SYNONYMS`) can be corrected to match the real
headers. A failure in this source never breaks the rest of the build — it's
wrapped to fail gracefully and just log a warning if something's wrong.

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
   this is the only place
   a real **end date** is available — the listing pages never showed one.

Rows show as "Traffic Scotland (scraped)" in the Source column.

**The M74/A74(M) alias**: the same physical road is signed "A74(M)" on
its southern stretch near the Scotland/England border, becoming "M74"
further north towards Glasgow. `build.py` treats these as the same road
(`SCOTLAND_ROAD_ALIASES`) and always normalizes the output to "M74", so
route legs just use `road_name: "M74"` regardless of which name a given
closure was published under.

**Known limitations**:
- **Cross-road closures need their own explicit junction number.** Some
  closures span two different roads (e.g. an M8/M74 interchange). If such
  a closure has no M74-specific junction number stated directly in its
  location text, it's excluded entirely rather than risk borrowing a
  junction number from the diversion text that might actually belong to
  the *other* road (a real case: an M8/M74 closure whose diversion
  mentioned M8's own junctions 21 and 23, which would otherwise have been
  wrongly treated as M74 junctions since they happened to fall inside the
  M74 J8–22 range). The build log reports how many entries were skipped
  this way.
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
