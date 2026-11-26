"""Work out what needs adding or updating. Pure logic: no browser, no I/O."""
from __future__ import annotations

from collections import Counter
from datetime import date

from .models import Application, Plan, PortalEntry

DATE_TOLERANCE_DAYS = 2  # the portal only shows "N days ago", so allow a little slack


class PlanError(Exception):
    pass


def match_entries(apps: list[Application], entries: list[PortalEntry], today: date) -> dict[int, PortalEntry]:
    """Pair tracker rows with portal entries. Returns {tracker index: portal entry}.

    A Role+Company that appears once in the tracker is matched on text alone. If it appears
    more than once, the entry's age has to agree with the row's date as well, otherwise two
    applications to the same job would be confused with each other.
    """
    counts = Counter(a.key for a in apps if not a.skipped)
    pool: dict[str, list[PortalEntry]] = {}
    for entry in entries:
        pool.setdefault(entry.key, []).append(entry)

    matched: dict[int, PortalEntry] = {}
    for i in range(len(apps) - 1, -1, -1):  # newest first, so newer rows claim newer entries
        app = apps[i]
        candidates = pool.get(app.key)
        if app.skipped or not candidates:
            continue
        if counts[app.key] == 1:
            matched[i] = candidates.pop(0)
            continue
        expected_age = (today - app.applied_on).days
        for j, entry in enumerate(candidates):
            if entry.age_days is None or abs(entry.age_days - expected_age) <= DATE_TOLERANCE_DAYS:
                matched[i] = candidates.pop(j)
                break
    return matched


def ledger_indexes(apps: list[Application], ledger: list[dict]) -> set[int]:
    """Tracker rows this tool has already added, according to the local ledger."""
    seen = {(e["date"], e["key"]) for e in ledger if isinstance(e, dict) and "date" in e and "key" in e}
    return {i for i, a in enumerate(apps) if (a.applied_on.isoformat(), a.key) in seen}


def last_loggable_index(apps: list[Application]) -> int:
    last = -1
    for i, a in enumerate(apps):
        if not a.skipped:
            last = i
    return last


def duplicate_keys(apps: list[Application]) -> set[str]:
    counts = Counter(a.key for a in apps if not a.skipped)
    return {key for key, n in counts.items() if n > 1}


def build_plan(apps: list[Application], matched: dict[int, PortalEntry], ledger_idx: set[int],
               from_number: str | None = None) -> Plan:
    cutoff = last_loggable_index(apps)
    if cutoff == -1:
        return Plan(anchor=None, cutoff=-1)

    # Rows already on the portal whose status there differs from the tracker's
    updates = []
    for i in sorted(matched):
        app, entry = apps[i], matched[i]
        if app.skipped or not entry.update_url:
            continue
        if entry.status.strip().casefold() != app.target.label.casefold():
            updates.append((app, entry))

    if from_number:
        starts = [i for i, a in enumerate(apps) if a.number == from_number]
        if not starts:
            raise PlanError(f"--from-number {from_number} not found in tracker")
        start = starts[0]
        additions = [a for i, a in enumerate(apps[start:cutoff + 1], start) if a.eligible and i not in matched]
        return Plan(start - 1, updates, additions, cutoff)

    known = set(matched) | ledger_idx
    if not known:
        # Nothing on the portal matches: treat it as a first run and start from the top.
        return Plan(-1, updates, [a for a in apps[:cutoff + 1] if a.eligible], cutoff)

    anchor = max(known)  # newest tracker row we know is already logged
    return Plan(anchor, updates, [a for a in apps[anchor + 1:cutoff + 1] if a.eligible], cutoff)
