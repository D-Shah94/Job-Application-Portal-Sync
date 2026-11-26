"""Command line entry point."""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter

from playwright.sync_api import sync_playwright

from .browser import open_context
from .config import DATA_DIR, LOG_FILE
from .models import Target
from .portal import Portal, wait_for_login
from .sync import Options, SyncSession
from .tracker import TrackerError, find_tracker, pick_tracker_file, read_tracker

DESCRIPTION = """Sync a job-application tracker (.md table, .csv, .tsv or .json) to the
Universal Credit 'Job applications' page.

New rows after the newest one already on the portal are added; existing entries whose status
has changed in the tracker (applied -> interview -> unsuccessful ...) are updated."""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="worksearch_sync", description=DESCRIPTION,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", "--md", dest="md", help="tracker file; searched for automatically if omitted")
    ap.add_argument("--pick-file", action="store_true", help="choose the tracker in a file dialog")
    ap.add_argument("--browser", choices=["firefox", "chromium", "chrome"], default="chrome")
    ap.add_argument("--profile", default="uc_chrome_profile",
                    help="folder that keeps your sign-in/2FA cookies; 'none' for a fresh profile each run")
    ap.add_argument("--cdp", help="attach to a Chrome you started with --remote-debugging-port, e.g. http://localhost:9222")
    ap.add_argument("--skip-unlogged-rejected", action="store_true",
                    help="do not add rows that are already rejected; only applied/interview/offer rows")
    ap.add_argument("--include-interested", action="store_true", help="also sync 'interested' / 'saved' rows")
    ap.add_argument("--notes-mode", choices=["none", "via", "notes"], default="none",
                    help="what to put in the portal's notes box (default: nothing)")
    ap.add_argument("--max-pages", type=int, default=6, help="portal pages to scan, 15 entries each (default 6)")
    ap.add_argument("--max-add", type=int, default=40, help="refuse to add more than this in one run (default 40)")
    ap.add_argument("--limit", type=int, default=0, help="add at most this many entries")
    ap.add_argument("--from-number", help='start adding at this tracker "#"')
    ap.add_argument("--ignore-ledger", action="store_true", help="ignore the record of what this tool added earlier")
    ap.add_argument("--dry-run", action="store_true", help="show the plan and stop")
    ap.add_argument("--step", action="store_true", help="pause before every Save")
    ap.add_argument("--yes", action="store_true", help="run the whole queue without the menu")
    ap.add_argument("--manual-enter", action="store_true", help="wait for ENTER instead of detecting the page")
    ap.add_argument("--login-timeout", type=int, default=900, help="seconds to wait for you to sign in")
    ap.add_argument("--keep-open", action="store_true", help="leave the browser open until ENTER at the end")
    ap.add_argument("--slow-mo", type=int, default=0, help="milliseconds to slow each browser action")
    ap.add_argument("--pause", type=float, default=0.3, help="seconds to wait after each save")
    ap.add_argument("--verbose", action="store_true", help="echo the debug log to the terminal")
    return ap


def setup_logging(verbose: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("worksearch_sync")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    to_file = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    to_file.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d  %(message)s", "%H:%M:%S"))
    logger.addHandler(to_file)
    if verbose:
        to_screen = logging.StreamHandler()
        to_screen.setFormatter(logging.Formatter("  [log] %(message)s"))
        logger.addHandler(to_screen)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    log = logging.getLogger("worksearch_sync")

    try:
        path = pick_tracker_file() if args.pick_file else find_tracker(args.md)
        apps = read_tracker(path, skip_unlogged_rejected=args.skip_unlogged_rejected,
                            include_interested=args.include_interested, notes_mode=args.notes_mode)
    except TrackerError as exc:
        print(f"Tracker problem: {exc}")
        return 2

    counts = Counter(a.target for a in apps)
    print(f"Read {len(apps)} rows from {path}: {counts[Target.APPLIED]} applied, {counts[Target.INTERVIEW]} interview, "
          f"{counts[Target.SUCCESSFUL]} offer, {counts[Target.UNSUCCESSFUL]} rejected, {counts[Target.SKIP]} skipped")
    log.info("run start: tracker=%s browser=%s cdp=%s", path, args.browser, args.cdp)

    stopped = False
    with sync_playwright() as pw:
        ctx, _ = open_context(pw, browser=args.browser, profile=args.profile, cdp=args.cdp, slow_mo=args.slow_mo)
        try:
            page = wait_for_login(ctx, args.manual_enter, args.login_timeout)
            if page is None:
                print(f"STOP: never saw the Job applications page (see {LOG_FILE}).")
                stopped = True
            else:
                session = SyncSession(Portal(page, args.pause), apps, Options(args))
                stopped = session.run()
        finally:
            if stopped or args.keep_open:
                try:
                    input("\nPress ENTER to close the browser... ")
                except EOFError:
                    pass
            try:
                ctx.close()
            except Exception:
                pass
    return 1 if stopped else 0


if __name__ == "__main__":
    sys.exit(main())
