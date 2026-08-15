/*
 * Day filter for route pages. Builds a row of buttons -- All, Today,
 * Tomorrow, then named weekdays out to 7 days -- and shows/hides table
 * rows whose [data-start, data-end] window overlaps the selected day.
 *
 * Runs entirely client-side against the already-rendered table, so
 * switching days is instant and needs no rebuild. Day boundaries are
 * computed from the visitor's local clock; closure times are shown in
 * UTC elsewhere on the page, so a closure starting right at midnight
 * could occasionally land a day off from what the visitor expects --
 * an acceptable approximation for a quick filter, not a precise log.
 */
(function () {
  "use strict";

  const filterBar = document.querySelector("[data-day-filter]");
  if (!filterBar) return; // index page has no filter bar

  const tables = document.querySelectorAll("[data-day-filter-table]");
  if (!tables.length) return; // nothing to filter (e.g. all legs empty)

  const WEEKDAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const DAY_MS = 24 * 60 * 60 * 1000;
  const WINDOW_DAYS = 7; // matches the advance-notice report's own window

  function startOfDay(date) {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate());
  }

  function formatShort(date) {
    return `${date.getDate()} ${date.toLocaleString(undefined, { month: "short" })}`;
  }

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

  function makeButton(label, isActive) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "day-filter-btn";
    btn.setAttribute("role", "tab");
    btn.setAttribute("aria-selected", isActive ? "true" : "false");
    btn.textContent = label;
    return btn;
  }

  const allBtn = makeButton("All", true);
  filterBar.appendChild(allBtn);
  const dayButtons = days.map((d) => makeButton(d.label, false));
  dayButtons.forEach((btn) => filterBar.appendChild(btn));

  function setActive(activeBtn) {
    [allBtn, ...dayButtons].forEach((btn) => {
      const isActive = btn === activeBtn;
      btn.classList.toggle("is-active", isActive);
      btn.setAttribute("aria-selected", isActive ? "true" : "false");
    });
  }

  function rowOverlapsDay(row, dayStart, dayEnd) {
    const startAttr = row.getAttribute("data-start");
    const endAttr = row.getAttribute("data-end");
    if (!startAttr) return false; // no date info -- can't place it on a specific day
    const rowStart = new Date(startAttr);
    const rowEnd = endAttr ? new Date(endAttr) : rowStart;
    if (isNaN(rowStart.getTime())) return false;
    const effectiveEnd = isNaN(rowEnd.getTime()) ? rowStart : rowEnd;
    return rowStart < dayEnd && effectiveEnd >= dayStart;
  }

  function applyFilter(dayStart, dayEnd) {
    tables.forEach((table) => {
      const rows = table.querySelectorAll("tbody tr");
      let visibleCount = 0;
      rows.forEach((row) => {
        const show = dayStart === null || rowOverlapsDay(row, dayStart, dayEnd);
        row.hidden = !show;
        if (show) visibleCount++;
      });

      const wrap = table.closest(".table-wrap");
      const emptyNote = wrap && wrap.nextElementSibling && wrap.nextElementSibling.hasAttribute("data-day-filter-empty")
        ? wrap.nextElementSibling
        : null;
      if (emptyNote) {
        emptyNote.hidden = visibleCount !== 0;
      }
      if (wrap) {
        wrap.hidden = visibleCount === 0 && dayStart !== null;
      }
    });
  }

  allBtn.addEventListener("click", () => {
    setActive(allBtn);
    applyFilter(null, null);
  });

  dayButtons.forEach((btn, i) => {
    btn.addEventListener("click", () => {
      setActive(btn);
      applyFilter(days[i].start, days[i].end);
    });
  });
})();
