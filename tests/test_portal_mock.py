"""End-to-end run against a mock of the portal served through Playwright's request interception.

The mock reproduces the parts of the real pages the tool relies on (selectors, the date fields that
appear after choosing "Applied", the success banner and the result count).
Needs a Playwright browser:  python -m playwright install chromium
"""
from argparse import Namespace
from datetime import date, timedelta
from html import escape
from urllib.parse import parse_qs, urlparse

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

import worksearch_sync.portal as portal_mod
import worksearch_sync.state as state_mod
from worksearch_sync.portal import Portal, wait_for_login
from worksearch_sync.sync import Options, SyncSession
from worksearch_sync.tracker import read_tracker

HOST = "https://www.universal-credit.service.gov.uk"
STATUSES = [("INTERESTED", "Interested"), ("APPLIED", "Applied"), ("INTERVIEW", "Going for an interview"),
            ("SUCCESSFUL", "Successful"), ("UNSUCCESSFUL", "Unsuccessful")]
LABELS = dict(STATUSES)
ADDED_BANNER = '<div class="govuk-notification-banner--success"><h2>Job added</h2></div>'
UPDATED_BANNER = '<div class="govuk-notification-banner--success"><h2>Job updated</h2></div>'


class MockPortal:
    def __init__(self, today):
        self.today = today
        self.jobs = []
        self.seq = 0

    def add(self, title, status, applied_on):
        self.seq += 1
        self.jobs.append({"id": f"id{self.seq}", "title": title, "status": status, "date": applied_on, "seq": self.seq})

    def _ordered(self):
        return sorted(self.jobs, key=lambda j: (j["date"], j["seq"]), reverse=True)

    def list_page(self, page=1, banner=""):
        jobs = self._ordered()
        shown = jobs[(page - 1) * 15: page * 15]
        items = "".join(
            f'<li class="job-list__item"><h3 class="job-list__item-heading">{escape(j["title"])}</h3>'
            f'<p class="job-list__status-text"><span class="job-list__status-label">{LABELS[j["status"]]}</span> '
            f'{(self.today - j["date"]).days} day(s) ago</p>'
            f'<a class="job-list__item-link" href="/work-search/job/update/{j["id"]}">Update job</a></li>'
            for j in shown)
        nxt = '<a class="pagination__link" href="/work-search?page=%d">Next</a>' % (page + 1) if page * 15 < len(jobs) else ""
        return (f'<html><body>{banner}<h1>Job applications</h1><a id="add-job" href="/work-search/job">Add a job</a>'
                f'<ul class="job-list">{items}</ul><p class="pagination__summary">Showing {len(shown)} of {len(jobs)} results</p>'
                f'{nxt}</body></html>')

    def form_page(self, job=None):
        radios = "".join(
            f'<input type="radio" id="clickable-{k}" name="jobStatus" value="{k}" style="opacity:0;position:absolute"'
            f'{" checked" if job and job["status"] == k else ""}><label for="clickable-{k}">{v}</label>' for k, v in STATUSES)
        title = escape(job["title"].split(" - ")[0]) if job else ""
        employer = escape(job["title"].split(" - ")[1]) if job else ""
        return (f'<html><body><form method="post"><input id="id-jobTitle" name="jobTitle" value="{title}">'
                f'<input id="id-employer" name="employer" value="{employer}">{radios}'
                '<div id="dates" style="display:none"><input name="applicationDate.day"><input name="applicationDate.month">'
                '<input name="applicationDate.year"></div><textarea id="id-notes" name="notes"></textarea>'
                '<input id="id-submit-button" type="submit" value="Save"></form>'
                '<script>document.getElementById("clickable-APPLIED").addEventListener("change",function(){'
                'document.getElementById("dates").style.display="block"});</script></body></html>')

    def handle(self, route):
        req = route.request
        url = urlparse(req.url)
        path, query = url.path, parse_qs(url.query)
        html = lambda body, status=200: route.fulfill(status=status, content_type="text/html", body=body)

        if path == "/dashboard":
            return html("<html><body>Home</body></html>")
        if path == "/work-search":
            return html(self.list_page(int(query.get("page", ["1"])[0])))
        if path == "/work-search/job":
            if req.method == "POST":
                f = {k: v[0] for k, v in parse_qs(req.post_data or "").items()}
                applied = date(int(f["applicationDate.year"]), int(f["applicationDate.month"]), int(f["applicationDate.day"]))
                self.add(f"{f['jobTitle']} - {f['employer']}", f["jobStatus"], applied)
                return html(self.list_page(banner=ADDED_BANNER))  # the real site redirects; the end result is the same
            return html(self.form_page())
        if path.startswith("/work-search/job/update/"):
            job = next(j for j in self.jobs if j["id"] == path.rsplit("/", 1)[1])
            if req.method == "POST":
                job["status"] = parse_qs(req.post_data or "")["jobStatus"][0]
                return html(self.list_page(banner=UPDATED_BANNER))
            return html(self.form_page(job))
        return route.fulfill(status=404, body="")


@pytest.fixture
def browser_page():
    with playwright_sync.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # browser not installed
            pytest.skip(f"Chromium not available: {exc}")
        ctx = browser.new_context()
        yield ctx
        browser.close()


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    for module in (state_mod, portal_mod):
        monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(state_mod, "LEDGER_FILE", tmp_path / "ledger.json")
    monkeypatch.setattr(portal_mod, "enter_pressed", lambda: False)
    return tmp_path


def options(**overrides):
    base = dict(max_pages=6, max_add=40, limit=0, from_number=None, ignore_ledger=False, dry_run=False,
                step=False, yes=True, skip_unlogged_rejected=False, include_interested=False, notes_mode="none")
    base.update(overrides)
    return Options(Namespace(**base))


def build_tracker(tmp_path, today):
    d = lambda n: (today - timedelta(days=n)).isoformat()
    rows = [("001", d(20), "Alpha", "Dev One", "Applied"), ("002", d(15), "Beta", "Dev Two", "Rejected"),
            ("003", d(12), "Gamma", "Dev Three", "Evaluated"), ("004", d(5), "Delta", "Dev Four", "Applied"),
            ("005", d(3), "Echo", "Dev Five", "Interview"), ("006", d(2), "Foxtrot", "Dev Six", "Rejected")]
    lines = ["| # | Date | Company | Role | Status |", "|---|---|---|---|---|"]
    lines += [f"| {n} | {dt} | {c} | {r} | {s} |" for n, dt, c, r, s in rows]
    path = tmp_path / "tracker.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_full_sync_adds_updates_and_is_idempotent(browser_page, isolated_state):
    today = date.today()
    mock = MockPortal(today)
    mock.add("Dev One - Alpha", "APPLIED", today - timedelta(days=20))
    mock.add("Dev Two - Beta", "APPLIED", today - timedelta(days=15))
    mock.add("Recruiter Role - Agency", "APPLIED", today - timedelta(days=1))  # not in the tracker
    browser_page.route(HOST + "/**", mock.handle)
    page = browser_page.new_page()
    page.goto(HOST + "/work-search")

    apps = read_tracker(build_tracker(isolated_state, today))
    session = SyncSession(Portal(page, pause=0), apps, options())
    assert session.run() is False

    by_title = {j["title"]: j for j in mock.jobs}
    assert len(mock.jobs) == 6
    assert by_title["Dev Two - Beta"]["status"] == "UNSUCCESSFUL"      # updated
    assert by_title["Dev Four - Delta"]["status"] == "APPLIED"          # added
    assert by_title["Dev Four - Delta"]["date"] == today - timedelta(days=5)
    assert by_title["Dev Five - Echo"]["status"] == "INTERVIEW"         # added, then updated
    assert by_title["Dev Six - Foxtrot"]["status"] == "UNSUCCESSFUL"
    assert "Dev Three - Gamma" not in by_title                          # skipped status
    assert (session.added, session.updated) == (3, 1)

    # second run finds nothing left to do
    again = SyncSession(Portal(page, pause=0), apps, options())
    assert again.run() is False
    assert (again.added, again.updated) == (0, 0)
    assert len(mock.jobs) == 6


def test_dry_run_changes_nothing(browser_page, isolated_state):
    today = date.today()
    mock = MockPortal(today)
    mock.add("Dev One - Alpha", "APPLIED", today - timedelta(days=20))
    browser_page.route(HOST + "/**", mock.handle)
    page = browser_page.new_page()
    page.goto(HOST + "/work-search")
    apps = read_tracker(build_tracker(isolated_state, today))
    assert SyncSession(Portal(page, pause=0), apps, options(dry_run=True)).run() is False
    assert len(mock.jobs) == 1


def test_wait_for_login_follows_the_post_login_page(browser_page, isolated_state):
    mock = MockPortal(date.today())
    browser_page.route(HOST + "/**", mock.handle)
    page = browser_page.new_page()
    page.goto(HOST + "/dashboard")
    found = wait_for_login(browser_page, manual=False, timeout=20)
    assert found is not None and found.url.endswith("/work-search")
