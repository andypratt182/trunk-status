"""
Shared, best-effort status registry for the "Data Sources" panel shown
on the site itself (see templates/index.html and build.py's main()).

WHY THIS EXISTS: every additional source module already catches its own
fetch/parse failures internally and just prints a warning + returns []
(see e.g. sources/travel_alerts.py's fetch_listing_page()) -- by design,
so one broken source never crashes the whole build. But that also means
build.py's own dispatch loop can't tell "the fetch genuinely failed"
apart from "the fetch worked fine and found nothing" just by looking at
the returned list -- both look like []. Several of these sources are
SUPPOSED to often return 0 results (Travel Alerts typically has only
2-3 entries covering the whole English network at any moment; TomTom
and the NH beta search can both be legitimately quiet), so a status
indicator that just checked "len(results) == 0 -> red" would be
permanently, misleadingly red on ordinary working days.

This module gives each source a side channel to report what it already
knows at the exact point it decides its own outcome -- inside its own
existing try/except, right alongside the return statement it already
has. It adds no new control flow and changes no function's return type
or call signature, so it doesn't touch any existing call site or test
assertion that checks a fetch_from_X() return value.

SCOPE: deliberately covers only the additional/optional sources
(site.additional_sources in routes.yaml), not the primary source
(site.source). A primary-source failure already raises SystemExit and
stops the whole build outright (see sources/national_highways.py) --
no page is produced in that case, so there's nothing for a status badge
ON that (non-existent) page to show; that failure is already visible
via the GitHub Actions run itself failing.

LAST-BUILD ONLY, not historical: module-level state, reset once at the
start of each build via reset() (same convention already used for
sources/tomtom_incidents.py's _response_cache/_warned_missing_key --
process-lifetime state, not persisted between builds). This shows
"how did the last build go", not a trend over time -- see the README's
"Data Sources status" section for why that's a deliberate scope choice,
not an oversight.
"""
from __future__ import annotations

# Each entry: {"label": str, "state": "ok_with_results" | "ok_no_results"
# | "failed", "count": int, "error": str}
_statuses: list[dict] = []


def reset() -> None:
    """Call once at the start of a build (see build.py's main()) so
    re-running the build in the same process (e.g. under a test) doesn't
    accumulate stale entries from a previous run."""
    _statuses.clear()


def record_status(label: str, ok: bool, count: int = 0, error: str = "") -> None:
    """Record one source's outcome for this build.

    label should identify the source distinctly enough to tell entries
    apart on the page, e.g. "Travel Alerts -- M6" -- by convention,
    every source module builds this as "<human source name> -- <road>"
    (or just the source name alone for xlsx_advance_notice.py, which
    covers every road in one fetch rather than one per road).

    ok=False means the fetch or parse itself failed -- not "found 0
    results", which is ok=True, count=0. Pass a short, human-readable
    error (e.g. "HTTP 403 Forbidden"), not a full exception repr/traceback
    -- this is for a compact status badge, not a log.
    """
    if not ok:
        state = "failed"
    elif count > 0:
        state = "ok_with_results"
    else:
        state = "ok_no_results"
    _statuses.append({"label": label, "state": state, "count": count, "error": error})


def get_statuses() -> list[dict]:
    return list(_statuses)
