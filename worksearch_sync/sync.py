"""Ties the tracker, the planner and the portal together and runs the interactive loop."""
from __future__ import annotations

import logging
from datetime import date, datetime

from .console import AUTO, MENU, CancelForm, SkipItem, ask_menu, ask_step
from .models import Application, Plan
from .planner import PlanError, build_plan, duplicate_keys, ledger_indexes, match_entries
from .portal import Portal, dismiss_timeout_dialog
from .state import load_config, load_ledger, save_config, save_ledger
from .tracker import pick_tracker_file, read_tracker

log = logging.getLogger("worksearch_sync")


class Options:
    """Settings the session needs, copied from the command line."""

    def __init__(self, args):
        self.max_pages = args.max_pages
        self.max_add = args.max_add
        self.limit = args.limit
        self.from_number = args.from_number
        self.ignore_ledger = args.ignore_ledger
        self.dry_run = args.dry_run
        self.step = args.step
        self.yes = args.yes
        self.skip_unlogged_rejected = args.skip_unlogged_rejected
        self.include_interested = args.include_interested
        self.notes_mode = args.notes_mode


class _Pass:
    """State for one run through the queues, started from a menu choice."""

    def __init__(self, mode: str):
        self.run_updates = mode in ("y", "u", "t", "s")
        self.run_additions = mode in ("y", "a", "t", "s")
        self.stepping = mode in ("t", "s")
        self.single = mode == "t"
        self.processed = 0
        self.back_to_menu = False


class SyncSession:
    def __init__(self, portal: Portal, apps: list[Application], opts: Options):
        self.portal = portal
        self.apps = apps
        self.opts = opts
        self.plan = Plan(anchor=None)
        self.ledger: list[dict] = []
        self.updated = 0
        self.added = 0

    # ---- planning ----
    def scan_portal(self) -> tuple[list, int] | None:
        """Read list pages until every tracker row at or below the newest match has been seen."""
        entries, scanned = [], 0
        actionable = [i for i, a in enumerate(self.apps) if not a.skipped]
        today = date.today()

        for n in range(1, self.opts.max_pages + 1):
            page_entries = self.portal.read_page(n)
            if page_entries is None:
                return None
            if not page_entries:
                break
            scanned += 1
            page_matches = match_entries(self.apps, page_entries, today)
            entries.extend(page_entries)
            matches = match_entries(self.apps, entries, today)

            if matches:
                newest = max(matches)
                if all(i in matches for i in actionable if i <= newest):
                    break  # everything older than the newest match is accounted for
                if not page_matches and scanned >= 2:
                    break
            if not self.portal.has_next_page():
                break

        if scanned > 1:
            self.portal.go_to_list()
        return entries, scanned

    def refresh(self) -> bool:
        """Re-scan the portal and rebuild the plan. False means something went wrong."""
        result = self.scan_portal()
        if result is None:
            print(f"STOP: the portal sent the browser to {self.portal.page.url} (signed out?). "
                  "Details are in .uc-data/uc_debug.log")
            return False
        entries, scanned = result
        matched = match_entries(self.apps, entries, date.today())
        self.ledger = [] if self.opts.ignore_ledger else load_ledger()
        try:
            plan = build_plan(self.apps, matched, ledger_indexes(self.apps, self.ledger), self.opts.from_number)
        except PlanError as exc:
            print(f"STOP: {exc}")
            return False
        if self.opts.limit and plan.additions:
            plan.additions = plan.additions[: self.opts.limit]
        self.plan = plan
        print(f"\nScanned {scanned} page(s), {len(entries)} portal entries.")
        return True

    def print_queue(self) -> None:
        plan, apps = self.plan, self.apps
        if plan.anchor is not None and plan.anchor >= 0:
            a = apps[plan.anchor]
            print(f"Anchor: #{a.number} {a.title}")
        if plan.cutoff >= 0:
            c = apps[plan.cutoff]
            print(f"Last row to log: #{c.number} {c.title} ({c.status_text} -> {c.target.label})")

        if plan.updates:
            print(f"\n{len(plan.updates)} existing entr(ies) with a different status:")
            for app, entry in plan.updates:
                print(f"  [UPDATE {entry.status or 'Applied'} -> {app.target.label}] "
                      f"#{app.number}  {app.applied_on}  {app.title}")
        else:
            print("\nNo existing entries need a status change.")

        if plan.additions:
            dupes = duplicate_keys(apps)
            print(f"\n{len(plan.additions)} to add, oldest first (up to #{apps[plan.cutoff].number}):")
            for app in plan.additions:
                tag = "[ADD as Applied]" if app.target.name == "APPLIED" else f"[ADD -> {app.target.label}]"
                note = "   [same Role+Company appears more than once in the tracker]" if app.key in dupes else ""
                print(f"  {tag:24s} #{app.number}  {app.applied_on}  {app.title}{note}")
        else:
            print("\nNothing new to add.")

    # ---- step prompt ----
    def _step_prompt(self, description: str) -> str:
        dismiss_timeout_dialog(self.portal.page)
        try:
            return ask_step(description)
        except (SkipItem, CancelForm):
            self.portal.go_to_list()  # abandon the half-filled form
            raise

    # ---- processing ----
    def _drain_updates(self, p: _Pass) -> None:
        queue = self.plan.updates
        while queue:
            app, entry = queue[0]
            try:
                decision = self.portal.update(app, entry.update_url, self._step_prompt if p.stepping else None)
                queue.pop(0)
                self.updated += 1
                p.processed += 1
                log.info("updated #%s %s -> %s", app.number, app.title, app.target.label)
                print(f"  OK  updated #{app.number} {app.title} -> {app.target.label}")
                if decision == AUTO:
                    print("  >> switching to automatic for the rest")
                    p.stepping = p.single = False
                    p.run_additions = True
                elif decision == MENU or p.single:
                    p.back_to_menu = True
                    return
            except SkipItem:
                skipped, _ = queue.pop(0)
                print(f"  skipped #{skipped.number} ({skipped.title})")
                if p.single:
                    p.back_to_menu = True
                    return
            except CancelForm:
                print("  form discarded, back to the menu")
                p.back_to_menu = True
                return
            except Exception as exc:
                self._report_failure("update", app, exc)
                p.back_to_menu = True
                return

    def _drain_additions(self, p: _Pass) -> None:
        queue = self.plan.additions
        while queue:
            app = queue[0]
            try:
                total, decision = self.portal.add(app, self._step_prompt if p.stepping else None)
                queue.pop(0)
                self.added += 1
                p.processed += 1
                self.ledger.append({
                    "date": app.applied_on.isoformat(),
                    "key": app.key,
                    "n": app.number,
                    "status": app.target.label,
                    "at": datetime.now().isoformat(timespec="seconds"),
                })
                save_ledger(self.ledger)  # saved after every entry so a crash loses nothing
                outcome = "added" if not app.needs_followup else f"added, then {app.target.label}"
                log.info("%s #%s %s total=%s", outcome, app.number, app.title, total)
                print(f"  OK  {outcome} #{app.number} {app.title} (portal total {total})")
                if decision == AUTO:
                    print("  >> switching to automatic for the rest")
                    p.stepping = p.single = False
                elif decision == MENU or p.single:
                    p.back_to_menu = True
                    return
            except SkipItem:
                skipped = queue.pop(0)
                print(f"  skipped #{skipped.number} ({skipped.title})")
                if p.single:
                    p.back_to_menu = True
                    return
            except CancelForm:
                print("  form discarded, back to the menu")
                p.back_to_menu = True
                return
            except Exception as exc:
                self._report_failure("add", app, exc)
                p.back_to_menu = True
                return

    def _report_failure(self, verb: str, app: Application, exc: Exception) -> None:
        self.portal.snapshot(f"error_{verb}_{app.number}")
        log.error("failed to %s #%s: %s", verb, app.number, exc)
        print(f"\n  ERROR on #{app.number} {app.title}: {exc}")
        print("  Back to the menu: [r] re-scans the portal, [q] quits.")

    def process(self, mode: str) -> None:
        p = _Pass(mode)
        if p.run_updates:
            self._drain_updates(p)
        if p.back_to_menu or mode == "u":
            return
        if p.run_additions and not (p.single and p.processed >= 1):
            self._drain_additions(p)

    # ---- entry point ----
    def run(self) -> bool:
        """Returns True if the run stopped on a problem the user should look at."""
        if not self.refresh():
            return True
        plan = self.plan
        if plan.cutoff == -1:
            print("STOP: the tracker has no rows that should be logged.")
            return True

        self.print_queue()
        if not plan.updates and not plan.additions:
            print("\nNothing to update or add. The portal is up to date.")
            return False
        if len(plan.additions) > self.opts.max_add:
            print(f"STOP: {len(plan.additions)} additions is more than --max-add {self.opts.max_add}. "
                  "Check the tracker and the anchor, then raise --max-add.")
            return True
        if self.opts.dry_run:
            print("\n--dry-run: nothing submitted.")
            return False

        forced = "y" if self.opts.yes else ("s" if self.opts.step else None)
        while self.plan.updates or self.plan.additions:
            mode = forced or ask_menu(len(self.plan.updates), len(self.plan.additions))
            forced = None

            if mode == "q":
                print(f"\nDone for now: {self.updated} status update(s), {self.added} new entr(ies).")
                return False
            if mode == "p":
                self.print_queue()
            elif mode == "f":
                if not self._switch_tracker():
                    return True
            elif mode == "r":
                if not self.refresh():
                    return True
                self.print_queue()
            else:
                self.process(mode)

        print(f"\nAll queued items done: {self.updated} updated, {self.added} added.")
        return False

    def _switch_tracker(self) -> bool:
        path = pick_tracker_file()
        cfg = load_config()
        cfg["last_tracker_path"] = str(path)
        save_config(cfg)
        self.apps[:] = read_tracker(
            path,
            skip_unlogged_rejected=self.opts.skip_unlogged_rejected,
            include_interested=self.opts.include_interested,
            notes_mode=self.opts.notes_mode,
        )
        if not self.refresh():
            return False
        self.print_queue()
        return True
