/*
 * Global day filter, shared between index.html and every route page.
 *
 * The button bar itself only renders on the index page ([data-day-filter]
 * only exists there). The selected day is stored in localStorage so it
 * carries across page loads -- picking "Tomorrow" on the index page and
 * then clicking into a route keeps that same filter applied there, with
 * no buttons needed on the route page itself (just a small hint linking
 * back to the index page to change it).
 *
 * Defaults to "Today" on a first-ever visit (no stored preference yet).
 * "All" is intentionally the last button, after the 7 day options.
 *
 * On the index page, switching days recomputes each route/direction's
 * closure count live from embedded per-closure date data (see
 * [data-day-filter-summary] script blocks), rather than requiring a
 * rebuild.
 *
 * Traffic Scotland rows also get their Status column refreshed on load
 * (and their contribution to the index page's active-count) by comparing
 * the closure's own start/end against the visitor's actual clock, rather
 * than trusting the server-rendered snapshot from build time -- a
 * closure's real window is often just a few hours, so a build-time
 * status can go stale well before the next scheduled rebuild. Other
 * sources are left as server-rendered, since they can have a status
 * (e.g. "suspended") that isn't derivable from dates alone.
 */
(function () {
  "use strict";

  const WEEKDAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const DAY_MS = 24 * 60 * 60 * 1000;
  const WINDOW_DAYS = 7; // matches the advance-notice report's own window
  const STORAGE_KEY = "routeClosuresDayFilter"; // "all" or "0".."6" (days from today)

  function startOfDay(date) {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate());
  }

  function formatShort(date) {
    return `${date.getDate()} ${date.toLocaleString(undefined, { month: "short" })}`;
  }

  function buildDays() {
    const today = startOfDay(new Date());
    const days = [];
    for (let i = 0; i < WINDOW_DAYS; i++) {
      const dayStart = new Date(today.getTime() + i * DAY_MS);
      let label;
      if (i === 0) label = "Today";
      else if (i === 1) label = "Tomorrow";
      else label = `${WEEKDAY_NAMES[dayStart.getDay()]} ${formatShort(dayStart)}`;
      days.push({ start: dayStart, end: new Date(dayStart.getTime() + DAY_MS), label });
    }
    return days;
  }

  function getStoredSelection() {
    try {
      const v = window.localStorage.getItem(STORAGE_KEY);
      return v === null ? "0" : v; // default: Today
    } catch (e) {
      return "0"; // storage unavailable (e.g. private browsing) -- default to Today
    }
  }

  function setStoredSelection(value) {
    try {
      window.localStorage.setItem(STORAGE_KEY, value);
    } catch (e) {
      /* ignore -- filter still works for this page load via the JS variable */
    }
  }

  function resolveRange(days, selection) {
    if (selection === "all") return { start: null, end: null };
    const idx = parseInt(selection, 10);
    if (isNaN(idx) || !days[idx]) return { start: null, end: null };
    return { start: days[idx].start, end: days[idx].end };
  }

  // A closure with a definite, KNOWN end time that has already passed
  // shouldn't show under ANY view, including "All" -- unlike the
  // "hide under Today" logic inside overlapsDay() below (which only
  // ever mattered for Today specifically, since every other day option
  // is entirely in the future by construction), this applies
  // universally: "All" is meant to mean "all CURRENT and UPCOMING
  // disruptions", not literally every closure that has ever existed.
  // Deliberately returns false (never "ended") for a BLANK or
  // unparseable end time, rather than treating an unknown end as an
  // ended one -- sources like Travel Alerts genuinely have no end time
  // at all by design (the source itself never states one), and an
  // entry with no known end could easily still be ongoing; hiding it
  // just because we don't know when it ends would be wrong.
  function hasDefinitivelyEnded(endAttr) {
    if (!endAttr) return false;
    const end = new Date(endAttr);
    if (isNaN(end.getTime())) return false;
    return end < new Date();
  }

  function overlapsDay(startAttr, endAttr, dayStart, dayEnd) {
    if (!startAttr) return false;
    const start = new Date(startAttr);
    if (isNaN(start.getTime())) return false;
    const endParsed = endAttr ? new Date(endAttr) : start;
    const end = isNaN(endParsed.getTime()) ? start : endParsed;

    // A closure that's already fully ended shouldn't show under any
    // specific-day filter (Today, Tomorrow, ...), even if its window
    // technically touched that calendar day -- e.g. one running 22:00
    // yesterday to 06:00 today did overlap "today" by calendar date, but
    // by mid-morning it's simply over and no longer relevant to show
    // under a "what's happening today" view. This only ever affects
    // "Today" in practice, since every other day option is entirely in
    // the future by construction and can't already have ended. This is
    // ALSO now applied to "All" separately, via hasDefinitivelyEnded()
    // above at each call site -- kept here too rather than removed, so
    // this function's own behavior for Today/specific-day selections is
    // completely unchanged either way.
    if (end < new Date()) return false;

    return start < dayEnd && end >= dayStart;
  }

  // Traffic Scotland's own current/planned split is just which listing
  // page an entry appeared on -- not date-aware, and this is baked into
  // the static HTML once at build time. A closure's real window is
  // usually only a few hours (an overnight closure), so status can go
  // stale well before the next scheduled rebuild. Recompute it live from
  // the closure's own start/end against the visitor's actual clock,
  // rather than trusting the server-rendered snapshot -- but ONLY for
  // Traffic Scotland rows: other sources can have a status (e.g.
  // "suspended") that isn't derivable from dates alone, so their
  // server-rendered value is left untouched.
  const TRAFFIC_SCOTLAND_SOURCE_LABEL = "Traffic Scotland (scraped)";

  function liveStatusFor(startAttr, endAttr, fallbackStatus) {
    if (!startAttr) return fallbackStatus;
    const start = new Date(startAttr);
    if (isNaN(start.getTime())) return fallbackStatus;
    const now = new Date();
    if (!endAttr) return now >= start ? "active" : "planned";
    const end = new Date(endAttr);
    if (isNaN(end.getTime())) return now >= start ? "active" : "planned";
    return now >= start && now <= end ? "active" : "planned";
  }

  // ---- Route pages: refresh Traffic Scotland rows' status to reflect "now" ----
  function refreshLiveStatus() {
    const rows = document.querySelectorAll("[data-day-filter-table] tbody tr[data-source]");
    rows.forEach((row) => {
      if (row.getAttribute("data-source") !== TRAFFIC_SCOTLAND_SOURCE_LABEL) return;

      const currentMatch = row.className.match(/status-(\w+)/);
      const serverStatus = currentMatch ? currentMatch[1] : "planned";
      const liveStatus = liveStatusFor(
        row.getAttribute("data-start"), row.getAttribute("data-end"), serverStatus
      );
      if (liveStatus === serverStatus) return;

      row.classList.remove(`status-${serverStatus}`);
      row.classList.add(`status-${liveStatus}`);
      const label = row.querySelector("[data-status-label]");
      if (label) label.textContent = liveStatus.charAt(0).toUpperCase() + liveStatus.slice(1);
    });
  }

  // ---- Button bar: only rendered where [data-day-filter] exists (index page) ----
  function renderButtonBar(days, selection, onChange) {
    const bar = document.querySelector("[data-day-filter]");
    if (!bar) return;

    function makeButton(label, value) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "day-filter-btn";
      btn.setAttribute("role", "tab");
      btn.textContent = label;
      btn.dataset.value = value;
      return btn;
    }

    const dayButtons = days.map((d, i) => makeButton(d.label, String(i)));
    const allBtn = makeButton("All", "all"); // deliberately last
    const allButtons = [...dayButtons, allBtn];
    allButtons.forEach((btn) => bar.appendChild(btn));

    function refreshActiveStyles() {
      allButtons.forEach((btn) => {
        const isActive = btn.dataset.value === selection;
        btn.classList.toggle("is-active", isActive);
        btn.setAttribute("aria-selected", isActive ? "true" : "false");
      });
    }
    refreshActiveStyles();

    allButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        selection = btn.dataset.value;
        setStoredSelection(selection);
        refreshActiveStyles();
        onChange(selection);
      });
    });
  }

  // ---- Route pages: filter the visible closure tables ----
  function applyTableFilter(days, selection) {
    const tables = document.querySelectorAll("[data-day-filter-table]");
    if (!tables.length) return;

    const { start: dayStart, end: dayEnd } = resolveRange(days, selection);

    tables.forEach((table) => {
      const rows = table.querySelectorAll("tbody tr");
      let visibleCount = 0;
      rows.forEach((row) => {
        const endAttr = row.getAttribute("data-end");
        const show = !hasDefinitivelyEnded(endAttr) && (
          dayStart === null || overlapsDay(row.getAttribute("data-start"), endAttr, dayStart, dayEnd)
        );
        row.hidden = !show;
        if (show) visibleCount++;
      });

      const wrap = table.closest(".table-wrap");
      const emptyNote = wrap && wrap.nextElementSibling &&
        wrap.nextElementSibling.hasAttribute("data-day-filter-empty")
        ? wrap.nextElementSibling : null;
      if (emptyNote) emptyNote.hidden = visibleCount !== 0;
      if (wrap) wrap.hidden = visibleCount === 0 && dayStart !== null;
    });

    // "Showing disruptions for <day> -- change this on the all routes page" hint
    const hint = document.querySelector("[data-day-filter-hint]");
    if (hint) {
      if (dayStart === null) {
        hint.hidden = true;
      } else {
        const idx = parseInt(selection, 10);
        const label = document.querySelector("[data-day-filter-hint-label]");
        if (label && days[idx]) label.textContent = days[idx].label;
        hint.hidden = false;
      }
    }
  }

  // ---- Index page: recompute each direction's closure count live ----
  function applyIndexFilter(days, selection) {
    const blocks = document.querySelectorAll("[data-day-filter-summary]");
    if (!blocks.length) return;

    const { start: dayStart, end: dayEnd } = resolveRange(days, selection);

    blocks.forEach((block) => {
      let entries;
      try {
        entries = JSON.parse(block.textContent);
      } catch (e) {
        return;
      }
      const pageId = block.getAttribute("data-day-filter-summary");
      const countEl = document.querySelector(`[data-count-for="${pageId}"]`);
      if (!countEl) return;

      let total = 0;
      let active = 0;
      entries.forEach((c) => {
        const show = !hasDefinitivelyEnded(c.end) && (dayStart === null || overlapsDay(c.start, c.end, dayStart, dayEnd));
        if (show) {
          total++;
          const effectiveStatus = c.source === TRAFFIC_SCOTLAND_SOURCE_LABEL
            ? liveStatusFor(c.start, c.end, c.status)
            : c.status;
          if (effectiveStatus === "active") active++;
        }
      });

      countEl.innerHTML = `${total} disruption${total !== 1 ? "s" : ""}` +
        (active > 0 ? `<span class="active-flag"> \u00b7 ${active} active</span>` : "");
    });
  }

  const days = buildDays();
  let selection = getStoredSelection();

  renderButtonBar(days, selection, (newSelection) => {
    selection = newSelection;
    applyTableFilter(days, selection);
    applyIndexFilter(days, selection);
  });

  refreshLiveStatus();
  applyTableFilter(days, selection);
  applyIndexFilter(days, selection);
})();
