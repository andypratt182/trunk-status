# Route Closures

A small static site that shows current/upcoming roadworks for a handful of
hand-picked road sections ("routes"), each split into northbound and
southbound pages. Built with Python + Jinja2, rebuilt automatically on a
schedule by GitHub Actions, and published with GitHub Pages.

## How it works

1. `routes.yaml` — you define your routes here: road name, junction range,
   and the two directions.
2. `build.py` — downloads the live closures feed (`site.data_url` in
   `routes.yaml`), filters it per route, and renders static HTML into `_site/`.
3. `.github/workflows/build-deploy.yml` — runs `build.py` on a schedule
   (default: every 6 hours) and publishes `_site/` to GitHub Pages.

## Set up your routes

Edit `routes.yaml`. For each route you need:

- `road_name` — must match how the road appears in the feed (e.g. `"M6"`)
- `junction_from` / `junction_to` — the section you care about
- `data_direction` for each direction — the literal value the feed uses.
  Not every road is signed north/south (the M62 is mostly east/west, for
  instance) — check what values actually appear for your road and use those;
  `label` controls what visitors see regardless.

To check what direction values exist for a road, you can run:

```bash
python3 -c "
import json
data = json.load(open('road_data.json'))
roads = {c['road_name'] for c in data['closures'] if c['road_name'] == 'M6'}
dirs = {c['direction'] for c in data['closures'] if c['road_name'] == 'M6'}
print(dirs)
"
```

## Run it locally

```bash
python -m venv .venv
source .venv/bin/activate      # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
python build.py
```

Then open `_site/index.html` in a browser.

## Publish on GitHub Pages

1. Push this repo to GitHub.
2. In the repo, go to **Settings → Pages** and set **Source** to
   **GitHub Actions**.
3. The workflow runs automatically on push, on its schedule, and can be
   triggered manually from the **Actions** tab (**Run workflow**).
4. Your site will be published at
   `https://<your-username>.github.io/<repo-name>/`.

## Adjusting the rebuild schedule

Edit the `cron` line in `.github/workflows/build-deploy.yml`. It's currently
`0 */6 * * *` (every 6 hours, UTC). Use https://crontab.guru to build a
different schedule.
