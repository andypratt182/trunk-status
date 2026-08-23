/*
 * Regression tests for static/day-filter.js -- specifically the client-
 * side day-filter logic used by both the index page's "N disruptions ·
 * M active" counts (applyIndexFilter) and each route page's row
 * visibility (applyTableFilter), since both share the same overlapsDay()
 * / hasDefinitivelyEnded() functions this file tests directly.
 *
 * Run with: node test_day_filter.js
 *
 * No test framework/dependency -- just Node's built-in assert module,
 * matching test_build.py's own "no framework, just print PASS/FAIL"
 * style. day-filter.js itself needed a small, behavior-preserving guard
 * (see its own comments) so it can be require()'d here without a DOM;
 * that guard changes nothing about how the file runs in a real browser.
 */
"use strict";

const assert = require("assert");
const { overlapsDay, hasDefinitivelyEnded, liveStatusFor } = require("./static/day-filter.js");

let failures = 0;

function check(label, cond) {
  const status = cond ? "PASS" : "FAIL";
  console.log(`[${status}] ${label}`);
  if (!cond) failures++;
}

function hoursFromNow(h) {
  return new Date(Date.now() + h * 60 * 60 * 1000).toISOString();
}

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

const today = startOfDay(new Date());
const todayEnd = new Date(today.getTime() + 24 * 60 * 60 * 1000);

console.log("\n--- overlapsDay: CONFIRMED LIVE BUG regression guard (no end time) ---");

check(
  "an ongoing incident with NO end time that started hours ago overlaps Today -- " +
  "this is the exact bug a user caught live: the index page's own active counts " +
  "came out LOWER under 'Today' than under 'All' for every route/direction shown, " +
  "which can never legitimately happen (active = happening now = always within today)",
  overlapsDay(hoursFromNow(-3), "", today, todayEnd) === true,
);
check(
  "same case, but started several DAYS ago -- still overlaps Today (genuinely " +
  "ongoing, no reason a longer-running incident should be treated any differently)",
  overlapsDay(hoursFromNow(-72), "", today, todayEnd) === true,
);
check(
  "an incident with no end time that starts tomorrow (unambiguously after today "
  + "ends, regardless of what time it currently is) does not overlap Today",
  overlapsDay(new Date(todayEnd.getTime() + 5 * 60 * 60 * 1000).toISOString(), "", today, todayEnd) === false,
);
check(
  "empty-string end (not just missing/null) is treated the same as no end at all",
  overlapsDay(hoursFromNow(-3), "", today, todayEnd) === true,
);
check(
  "an unparseable (garbage) end string is treated the same as no end at all, not "
  + "as a parse failure that hides the row",
  overlapsDay(hoursFromNow(-3), "not-a-real-date", today, todayEnd) === true,
);

console.log("\n--- overlapsDay: known-end cases are unaffected by the fix ---");

check(
  "a closure with a real end that's already passed does NOT overlap Today",
  overlapsDay(hoursFromNow(-30), hoursFromNow(-20), today, todayEnd) === false,
);
check(
  "a closure with a real end still in the future DOES overlap Today",
  overlapsDay(hoursFromNow(-3), hoursFromNow(3), today, todayEnd) === true,
);
check(
  "a closure entirely in the future (both start and end tomorrow+) does not overlap Today",
  overlapsDay(hoursFromNow(30), hoursFromNow(40), today, todayEnd) === false,
);
check(
  "no start time at all -> never overlaps any day (nothing to compare)",
  overlapsDay("", "", today, todayEnd) === false,
);

console.log("\n--- hasDefinitivelyEnded: unaffected by this fix, checked for consistency ---");

check(
  "no end time at all -> never considered 'definitively ended' (matches overlapsDay's "
  + "now-consistent treatment of the same case)",
  hasDefinitivelyEnded("") === false,
);
check(
  "a real end time in the past -> definitively ended",
  hasDefinitivelyEnded(hoursFromNow(-1)) === true,
);
check(
  "a real end time in the future -> not yet ended",
  hasDefinitivelyEnded(hoursFromNow(1)) === false,
);

console.log("\n--- liveStatusFor: real-time active/planned recompute (Traffic Scotland rows only) ---");

check(
  "within [start, end] -> active",
  liveStatusFor(hoursFromNow(-1), hoursFromNow(1), "planned") === "active",
);
check(
  "before start -> planned",
  liveStatusFor(hoursFromNow(1), hoursFromNow(2), "active") === "planned",
);
check(
  "after end -> planned, not stuck on a stale 'active'",
  liveStatusFor(hoursFromNow(-2), hoursFromNow(-1), "active") === "planned",
);
check(
  "no start time -> falls back to the server-rendered status untouched",
  liveStatusFor("", "", "active") === "active",
);

console.log(`\n${"=".repeat(60)}`);
if (failures) {
  console.log(`${failures} test(s) FAILED`);
  process.exit(1);
}
console.log("All tests passed.");
