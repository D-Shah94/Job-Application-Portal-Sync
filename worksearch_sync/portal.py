"""Everything that talks to the Universal Credit pages through Playwright."""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Callable

from .config import DATA_DIR, LIST_PATH_RE, LIST_URL, NOTE_LIMIT, PORTAL_URL, POST_LOGIN_RE
from .console import CONTINUE, AUTO, MENU, enter_pressed
from .models import Application, PortalEntry, Target
from .tracker import clean, normalise

log = logging.getLogger("worksearch_sync")

BeforeSave = Callable[[str], str]

LOGIN_HELP = (
    "\n>>> Sign in to Universal Credit in the browser window (including 2FA).\n"
    "    As soon as you land on /to-do or /dashboard the script opens 'Job applications' itself.\n"
    "    You can also press ENTER here once you are on that page."
)


def live_url(page) -> str:
    """Ask the browser directly; page.url can be stale while we are not pumping events."""
    try:
        return page.evaluate("location.href")
    except Exception:
        return page.url


def dismiss_timeout_dialog(page) -> None:
    """Click 'Stay signed in' if the 15-minute session warning is showing."""
    try:
        button = page.locator("button:has-text('Stay signed in'), a:has-text('Stay signed in')")
        if button.count() and button.first.is_visible():
            button.first.click()
            log.info("clicked 'Stay signed in'")
    except Exception:
        pass


def wait_for_login(ctx, manual: bool, timeout: int):
    """Wait until some tab is on the Job applications page. Returns that page, or None on timeout."""
    print(LOGIN_HELP, flush=True)
    pump = ctx.pages[0] if ctx.pages else ctx.new_page()
    deadline = time.time() + timeout
    ready_since = None
    last_redirect = 0.0

    while time.time() < deadline:
        try:
            pump.wait_for_timeout(250)  # lets Playwright process browser events
        except Exception:
            time.sleep(0.25)

        tabs = [(pg, live_url(pg)) for pg in ctx.pages if not pg.is_closed()]
        ready = None
        for pg, url in tabs:
            try:
                dismiss_timeout_dialog(pg)
                if not manual and POST_LOGIN_RE.match(url) and time.time() - last_redirect > 3.0:
                    print(f"  -> signed in ({url.split('.gov.uk')[-1]}); opening {LIST_URL}", flush=True)
                    last_redirect = time.time()
                    pg.goto(LIST_URL, wait_until="domcontentloaded")
                    url = live_url(pg)
                if LIST_PATH_RE.search(url) and pg.locator("#add-job").count():
                    ready = pg
            except Exception:
                pass

        if enter_pressed():
            log.info("ENTER pressed; tabs: %s", " | ".join(u for _, u in tabs))
            return ready or next((pg for pg, u in tabs if "/work-search" in u), None)

        if not manual and ready:
            ready_since = ready_since or time.time()
            if time.time() - ready_since > 0.8:  # stable for a moment, not just passing through
                log.info("job applications page detected")
                return ready
        else:
            ready_since = None
    return None


class Portal:
    def __init__(self, page, pause: float = 0.3):
        self.page = page
        self.pause = pause

    # ---- navigation and reading ----
    def go_to_list(self) -> None:
        self.page.goto(LIST_URL, wait_until="domcontentloaded")

    def _ensure_on_first_page(self) -> None:
        url = self.page.url
        if not LIST_PATH_RE.search(url) or "page=" in url or not self.page.locator("#add-job").count():
            self.go_to_list()

    def read_page(self, n: int) -> list[PortalEntry] | None:
        """Entries on list page n, or None if the portal sent us somewhere else (signed out)."""
        url = LIST_URL if n == 1 else f"{LIST_URL}?page={n}"
        resp = self.page.goto(url, wait_until="domcontentloaded")
        log.info("list page %s -> %s (%s)", n, self.page.url, resp.status if resp else "?")
        if "/work-search" not in self.page.url:
            self.snapshot("not_on_list")
            return None

        entries = []
        for item in self.page.locator("ul.job-list > li").all():
            title = item.locator("h3").first.inner_text()
            status_el = item.locator(".job-list__status-label")
            status = status_el.first.inner_text().strip() if status_el.count() else ""
            text_el = item.locator(".job-list__status-text")
            text = text_el.first.inner_text() if text_el.count() else ""
            m = re.search(r"(\d+)\s+day", text)
            age = int(m[1]) if m else (0 if "today" in text.lower() else None)
            link = item.locator("a.job-list__item-link")
            href = (link.first.get_attribute("href") or "") if link.count() else ""
            if href.startswith("/"):
                href = PORTAL_URL + href
            entries.append(PortalEntry(normalise(title), clean(title), status, age, href))
        return entries

    def has_next_page(self) -> bool:
        return self.page.locator("a.pagination__link", has_text="Next").count() > 0

    def total_results(self) -> int:
        summary = self.page.locator(".pagination__summary")
        if summary.count():
            m = re.search(r"of\s+([\d,]+)\s+results", summary.first.inner_text())
            if m:
                return int(m[1].replace(",", ""))
        return self.page.locator("ul.job-list > li").count()

    def find_update_url(self, app: Application) -> str | None:
        if not LIST_PATH_RE.search(self.page.url) or "page=" in self.page.url:
            self.go_to_list()
        for item in self.page.locator("ul.job-list > li").all():
            if normalise(item.locator("h3").first.inner_text()) == app.key:
                link = item.locator("a.job-list__item-link")
                if link.count():
                    href = link.first.get_attribute("href") or ""
                    return PORTAL_URL + href if href.startswith("/") else href
        return None

    # ---- writing ----
    def _fill_note(self, app: Application) -> None:
        if not app.note:
            return
        box = self.page.locator("textarea#id-notes, textarea[name='notes']")
        if box.count() and box.first.is_visible() and not box.first.input_value().strip():
            box.first.fill(app.note[:NOTE_LIMIT])

    def _choose(self, radio_id: str) -> None:
        radio = self.page.locator(f"#{radio_id}")
        self.page.locator(f"label[for='{radio_id}']").click()
        if not radio.is_checked():
            radio.check(force=True)

    def _submit(self) -> None:
        with self.page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
            self.page.locator("#id-submit-button").click()

    def _validation_errors(self) -> list[str]:
        found = (e.inner_text().strip() for e in self.page.locator("[data-validation-error]").all())
        return [text for text in found if text]

    def add(self, app: Application, before_save: BeforeSave | None = None) -> tuple[int, str]:
        """Add one entry. Returns (portal total afterwards, step decision)."""
        page = self.page
        print(f"  -> adding #{app.number} ({app.title}, {app.applied_on})", flush=True)
        self._ensure_on_first_page()
        total_before = self.total_results()

        page.locator("#add-job").click()
        page.wait_for_url(re.compile(r".*/work-search/job/?$"))
        page.locator("#id-jobTitle").fill(app.role)
        page.locator("#id-employer").fill(app.company)

        if app.target is Target.INTERESTED:
            self._choose(Target.INTERESTED.radio_id)
        else:
            self._choose(Target.APPLIED.radio_id)
            day = page.locator('input[name="applicationDate.day"]')
            month = page.locator('input[name="applicationDate.month"]')
            year = page.locator('input[name="applicationDate.year"]')
            day.wait_for(state="visible", timeout=5000)  # the date fields appear after choosing Applied
            d = app.applied_on
            day.fill(str(d.day))
            month.fill(str(d.month))
            year.fill(str(d.year))

            # Read the form back before saving; refuse to submit anything that differs from the tracker.
            got = (page.locator("#id-jobTitle").input_value(), page.locator("#id-employer").input_value(),
                   page.locator("#clickable-APPLIED").is_checked(),
                   day.input_value(), month.input_value(), year.input_value())
            want = (app.role, app.company, True, str(d.day), str(d.month), str(d.year))
            if got != want:
                raise RuntimeError(f"Form does not match tracker.\n  wanted {want}\n  got    {got}")

        self._fill_note(app)

        decision = CONTINUE
        if before_save:
            extra = f" (will then be marked '{app.target.label}')" if app.needs_followup else ""
            decision = before_save(f"#{app.number} ({app.title}, {app.applied_on}) is filled and checked{extra}.")

        self._submit()
        banner = page.locator(".govuk-notification-banner--success")
        if not (banner.count() and "job added" in banner.first.inner_text().lower()):
            raise RuntimeError(f"No 'Job added' banner. Errors: {self._validation_errors() or 'none shown'} "
                               f"(url={page.url})")
        total_after = self.total_results()
        if total_after != total_before + 1:  # second check that exactly one entry was created
            raise RuntimeError(f"Total went {total_before} -> {total_after}, expected +1")
        time.sleep(self.pause)

        if app.needs_followup:
            update_url = self.find_update_url(app)
            if not update_url:
                raise RuntimeError(f"Added #{app.number} but could not find its update link on page 1")
            hook = before_save if decision == CONTINUE else None
            follow = self.update(app, update_url, before_save=hook)
            if follow in (AUTO, MENU):
                decision = follow
        return total_after, decision

    def update(self, app: Application, update_url: str, before_save: BeforeSave | None = None) -> str:
        """Change an existing entry's status. Returns the step decision."""
        page = self.page
        target = app.target
        print(f"  -> updating #{app.number} ({app.title}) to {target.label}", flush=True)
        resp = page.goto(update_url, wait_until="domcontentloaded")
        log.info("update page #%s -> %s (%s)", app.number, page.url, resp.status if resp else "?")
        if "/work-search/job/update/" not in page.url:
            raise RuntimeError(f"Expected an update page, landed on {page.url}")

        page.locator(f"#{target.radio_id}").wait_for(state="attached", timeout=10000)
        self._choose(target.radio_id)
        if not page.locator(f"#{target.radio_id}").is_checked():
            raise RuntimeError(f"'{target.label}' could not be selected")
        self._fill_note(app)

        decision = CONTINUE
        if before_save:
            decision = before_save(f"#{app.number} ({app.title}) now has '{target.label}' selected.")

        self._submit()
        errors = self._validation_errors()
        if errors or "/work-search" not in page.url:
            raise RuntimeError(f"Update failed. Errors: {errors or 'none shown'} (url={page.url})")
        time.sleep(self.pause)
        return decision

    # ---- diagnostics ----
    def snapshot(self, tag: str) -> None:
        """Screenshot and HTML of the current page, for working out what went wrong."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(DATA_DIR / f"uc_{tag}.png"), full_page=True)
            Path(DATA_DIR / f"uc_{tag}.html").write_text(self.page.content(), encoding="utf-8")
            log.info("snapshot saved: uc_%s (url=%s)", tag, self.page.url)
        except Exception as exc:
            log.warning("snapshot failed: %s", exc)
