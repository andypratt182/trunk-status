"""
TEMPORARY investigation aid -- NOT a permanent feature of this project.

Logs every NEW closure record (by record_id, first time seen) from a
small set of "known often-quiet" sources -- Travel Alerts and the NH
Traffic Search (beta) endpoint -- to a JSONL file committed back into
the repo, so their real output can be reviewed over time instead of
only during the few minutes something happens to be live. Exists to
help answer one open question raised while building those sources: is
"usually 0 results" genuinely normal, or does it hide a bug? (See each
source module's own "if this is unexpected" log comments -- this gives
something concrete to check against once a real entry does show up.)

(A TomTom Incidents source used to be tracked here too, for a second
question about possible overlap between sources -- removed along with
sources/tomtom_incidents.py, since TomTom's free tier didn't fit this
project's build cadence. See routes.yaml's comment where that source
used to be configured for the full story.)

WHEN YOU'RE DONE: delete this file, its call in build.py's main(), the
"Commit investigation log" step in
.github/workflows/build-deploy.yml, and that workflow's `contents:
write` permission override on the build job (revert to the top-level
`contents: read` default) -- none of this should stay once the two
questions above are answered. The investigation-log/ directory itself
can also be deleted at that point (or kept as a historical record, your
call).

WHERE THE LOG LIVES: investigation-log/tracked-source-entries.jsonl,
one JSON object per line, appended to (never rewritten) -- so it's
diff-friendly in git and cheap to append to without re-parsing the
whole file's content, just its record_ids (see _load_seen_record_ids()).
Growing with one line per NEWLY-seen record_id, not per build -- an
incident still active on the next build isn't re-logged, so a build
finding nothing new here (the common case) doesn't touch the file at
all, and the workflow's commit step has nothing to commit most runs.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOG_PATH = Path(__file__).parent / "investigation-log" / "tracked-source-entries.jsonl"

# Exact source_label strings -- see each module's own "source_label":
# assignment. Must match exactly; this is a set membership check, not a
# substring/fuzzy match, since a fuzzy match risks silently tracking (or
# silently missing) the wrong source if a label is ever reworded.
TRACKED_SOURCE_LABELS = {
    "Travel Alert (major incident)",
    "National Highways Traffic Search (beta)",
}


def _load_seen_record_ids(log_path: Path = LOG_PATH) -> set[str]:
    if not log_path.exists():
        return set()
    seen: set[str] = set()
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # A malformed line shouldn't break logging for every
                # entry after it -- skip just this one line.
                continue
            record_id = entry.get("record_id")
            if record_id:
                seen.add(record_id)
    return seen


def log_new_entries(closures: list[dict], log_path: Path = LOG_PATH) -> int:
    """Append any closure from a TRACKED_SOURCE_LABELS source whose
    record_id hasn't been logged before, tagged with when it was first
    seen. Returns how many new entries were appended this build -- 0 on
    most builds, which is expected, not a sign anything's broken (see
    module docstring)."""
    tracked = [c for c in closures if c.get("source_label") in TRACKED_SOURCE_LABELS]
    if not tracked:
        return 0

    seen = _load_seen_record_ids(log_path)
    new_entries = [c for c in tracked if c.get("record_id") not in seen]
    if not new_entries:
        return 0

    log_path.parent.mkdir(parents=True, exist_ok=True)
    first_seen_at = datetime.now(ZoneInfo("Europe/London")).isoformat()
    with log_path.open("a", encoding="utf-8") as f:
        for entry in new_entries:
            logged = dict(entry)
            logged["first_seen_at"] = first_seen_at
            f.write(json.dumps(logged, ensure_ascii=False) + "\n")

    print(f"Investigation log: {len(new_entries)} new entr"
          f"{'y' if len(new_entries) == 1 else 'ies'} appended to {log_path}")
    return len(new_entries)
