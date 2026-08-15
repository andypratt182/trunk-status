# Route Closures

A small static site that shows current/upcoming roadworks for a handful of
hand-picked road sections ("routes"), each split into northbound and
southbound pages -- and each direction can chain together several roads
(e.g. M6 -> M58 -> M57) to model a real driving route. Built with Python +
Jinja2, rebuilt automatically on a schedule by GitHub Actions, and published
with GitHub Pages.

## How it works

1. `routes.yaml` -- you define your routes here: road name, junction range,
   and direction for each leg of each route, plus which data source to use.
2. `build.py` -- fetches closures (from the live National Highways API or a
   flat JSON mirror, see below), filters them per leg, and renders static
   HTML into `_site/`.
3. `.github/workflows/build-deploy.yml` -- runs `build.py` on a schedule
   (default: every 6 hours) and publishes `_site/` to GitHub Pages.

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

Each route page has a row of filter buttons — **All**, **Today**,
**Tomorrow**, then named weekdays out to 7 days (matching the
advance-notice report's own window; the API can have data further out,
which is still visible under "All"). This runs entirely client-side
against the already-rendered table, so switching days is instant and
needs no rebuild — the JS lives in `static/day-filter.js`.

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
`0 */6 * * *` (every 6 hours, UTC). Use https://crontab.guru to build a
different schedule.
