# UC Work Search Sync

A command-line tool that copies the job applications in your own tracker (a Markdown table, CSV or
JSON file) into the **Job applications** section of your Universal Credit account, so you only
record each application once.

It works by driving a real browser with [Playwright](https://playwright.dev/python/). You sign in
yourself, including 2FA. The tool never sees or stores your password.

> **Unofficial.** This project is not affiliated with or endorsed by the DWP or GOV.UK. You are
> responsible for what is recorded in your account. Only use it with applications you have really made,
> with their real dates. See [Disclaimer](#disclaimer).

## What it does

- Reads a tracker with columns for role, company and date (other column names are recognised too, see
  [Tracker format](#tracker-format)).
- Works out which rows are not on the portal yet, and adds them: job title, employer, status
  "Applied" and the application date.
- Notices when a row's status has changed in your tracker (for example *Applied* to *Interview* or
  *Rejected*) and updates the matching portal entry.
- Skips rows you do not want logged (evaluated, discarded, drafts and so on).
- Shows the plan first. Nothing is saved until you choose to run it, and you can step through one entry at a time.

## Architecture

```text
+----------------------------------+     +---------------------------------------+
| Local Tracker (.md/.csv/.tsv/.json)    | Universal Credit Portal (Playwright)  |
|   └─ tracker.py (aliases, dates,       |   └─ portal.py (read pages, fill,     |
|      status classification)            |      read-back verify, count assert)  |
+----------------+-----------------+     +-------------------+-------------------+
                 |                                           |
                 v                                           v
         [Application, ...]                          [PortalEntry, ...]
                 \                                           /
                  +------------------+   +------------------+
                                     |   |
                                     v   v
                 +-------------------------------------------+
                 | planner.py (pure logic: no I/O)           |
                 |  ├─ Match keys & duplicate date tolerance |
                 |  ├─ Anchor + local ledger reconciliation  |
                 |  └─ Build idempotent Update + Add queues  |
                 +---------------------+---------------------+
                                       |
                                       v
                 +-------------------------------------------+
                 | sync.py + console.py + state.py           |
                 |  └─ Interactive menu, step mode, ledger   |
                 +-------------------------------------------+
```

**Finding where to carry on from.** The portal lists entries newest first, 15 per page. The tool reads
pages until every tracker row up to the newest match has been seen (default maximum 6 pages), pairs
tracker rows with portal entries, and takes the newest paired row as the *anchor*. Everything after the
anchor is queued. Entries that exist only on the portal (a job found through a recruiter, say) are ignored.

**Matching.** The portal titles each entry `Role - Company`, so that string (lower-cased, markdown stripped)
is the match key. If the same Role and Company appears more than once in the tracker, the portal's
"N days ago" has to agree with the row's date, within two days, so repeat applications are not confused.

**Ledger.** After every successful add, a line goes into `.uc-data/uc_ledger.json`. If a run finds no
match on the portal pages it reads (for example after a large back-dated batch), the ledger still tells
the tool where it got to.

**Safety checks.**

- The form is read back before every save and the run stops if it differs from the tracker.
- Each add must show the portal's "Job added" banner *and* raise the total result count by exactly one.
- `--max-add` (default 40) refuses suspiciously large queues.
- Rows dated in the future are never sent.
- On any error the page is saved as a screenshot and HTML in `.uc-data/` and the run returns to the menu.

## Trade-offs & Design Decisions

- **Human-in-the-loop authentication:** The tool launches a real browser and waits for the user to sign in and complete 2FA rather than automating login or storing credentials. This keeps security boundaries clean and prevents fragile auth scraping.
- **Pure-function planning separated from Playwright I/O:** Keeping matching and diffing inside `planner.py` (with zero browser or filesystem dependencies) makes anchor selection, duplicate-title disambiguation, and status transitions fast and deterministic to unit-test.
- **Two-step add-then-update for non-`Applied` rows:** Because the portal only exposes application-date inputs when creating an `Applied` entry, rows already at `Interview`, `Successful`, or `Unsuccessful` are intentionally created as `Applied` first and immediately updated so historical dates are never lost.
- **Fail-closed form verification & mocked E2E tests:** Before clicking Save, form inputs are read back and compared to the tracker; after saving, the tool asserts both the GOV.UK success banner and an exact `+1` total result count. `tests/test_portal_mock.py` exercises the full Playwright workflow against a stateful mock portal via `route.fulfill`, trading live-site DOM drift detection for safe, repeatable CI testing.

## Requirements

- Python 3.10 or newer
- Google Chrome installed (the default), or Firefox/Chromium installed through Playwright

## Install

```bash
git clone https://github.com/D-Shah94/uc-worksearch-sync.git
cd uc-worksearch-sync
python -m venv .venv
.venv\Scripts\activate          # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt
# only if you want Firefox or Playwright's own Chromium instead of installed Chrome:
python -m playwright install firefox
```

## Usage

```bash
python -m worksearch_sync --dry-run              # sign in, show the plan, change nothing
python -m worksearch_sync                         # sign in, show the plan, then a menu
python -m worksearch_sync --file path/to/tracker.md --browser firefox
python -m worksearch_sync --yes                   # run the whole queue without the menu
```

On Windows you can also drag a tracker file onto `run.cmd`.

1. A browser window opens at the sign-in page. Sign in and complete 2FA.
2. When you reach the home page the tool opens *Job applications* itself and reads the list.
3. It prints the queue: entries to add and entries whose status changed.
4. Choose from the menu: run everything, try one entry, step through with a pause before every save, run only
   additions or only updates, re-scan, or quit.

If no file is given, the tool looks for `applications.md` next to the project, in `./data/`, at
`CAREER_OPS_TRACKER`, at the last path it used, and finally in `../career-ops/data/`. If none exist it
opens a file picker.

### Options

| Option | Meaning |
|---|---|
| `--file PATH` | Tracker file (`.md`, `.csv`, `.tsv`, `.json`) |
| `--browser {chrome,chromium,firefox}` | Browser to drive (default `chrome`) |
| `--profile NAME` | Folder that keeps your sign-in cookies; `none` gives a fresh profile every run |
| `--cdp URL` | Attach to a Chrome you started with `--remote-debugging-port` |
| `--dry-run` | Show the plan and stop |
| `--step` | Pause before every save |
| `--yes` | Run the whole queue without the menu |
| `--limit N` | Add at most N entries |
| `--from-number N` | Start adding at this tracker `#`, ignoring the anchor |
| `--max-pages N` | Portal pages to scan (default 6) |
| `--max-add N` | Refuse to add more than N in one run (default 40) |
| `--include-interested` | Also sync rows marked interested/saved |
| `--skip-unlogged-rejected` | Do not add rows that are already rejected |
| `--notes-mode {none,via,notes}` | What goes in the portal's notes box |
| `--ignore-ledger` | Ignore the record of earlier adds (use after deleting entries by hand) |
| `--verbose` | Echo the debug log to the terminal |

## Tracker format

A Markdown table works as-is:

```markdown
| # | Date       | Company        | Role                    | Status  |
|---|------------|----------------|-------------------------|---------|
| 1 | 2026-09-01 | Northwind Ltd  | Junior Python Developer | Applied |
```

Required columns are date, company and role. Header names are matched case-insensitively against a list of
aliases in `worksearch_sync/config.py` (for example *Employer*, *Job Title*, *Date Applied*). A `#` column is
optional; rows are numbered if it is missing. Dates may be `2026-10-26`, `26/10/2026` (day first) or `26 Oct 2026`.

| Tracker status (examples) | Portal state |
|---|---|
| applied, submitted, sent | Applied |
| interview, phone screen, assessment | Going for an interview |
| offer, accepted, hired | Successful |
| rejected, declined, ghosted, closed | Unsuccessful |
| saved, bookmarked (with `--include-interested`) | Interested |
| evaluated, discarded, draft, withdrawn, anything unrecognised | not sent |

The portal records an application date only for *Applied*, so rows that end as interview, successful or
unsuccessful are added as *Applied* with their date and then updated.

## Files and privacy

Everything the tool keeps is in `.uc-data/` (or the folder in `UC_SYNC_DATA_DIR`): the browser profile with
your session cookies, the ledger, the remembered tracker path, the debug log and error snapshots. All of it is
in `.gitignore`. **Do not commit it**: the ledger contains your application history and the profile contains a
live session. Use `--profile none` if you would rather not keep cookies.

## Tests

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium
python -m pytest
```

Parsing and planning are unit-tested. `tests/test_portal_mock.py` runs the whole flow in Chromium against a
mock of the portal pages (adds, status updates, a second run that finds nothing to do, and the post-sign-in redirect).
The mock mirrors the selectors in use today, so it will not notice if the real site changes its markup.

## Limitations

- It depends on the portal's current HTML (element ids and classes). If GOV.UK changes the pages it will
  need updating. Failures are reported with a screenshot rather than ignored.
- Sessions time out after 15 minutes of inactivity; the tool clicks "Stay signed in" when the warning appears.
- Only the first `--max-pages` pages are read. A very large gap between runs may need `--max-pages` or
  `--from-number`, and older tracker rows beyond those pages are not checked for status changes.
- The portal shows ages in days, not dates, so duplicate Role+Company rows are matched approximately.
- Tested on Windows with Chrome. Linux/macOS should work but are less tested.

## Disclaimer

This tool automates a government service for your own account. Check the service's terms before using it.
What you record affects your claim: entries must be accurate, with the real application dates. The tool
copies what your tracker says, so keep the tracker honest. Use at your own risk; the author accepts no
liability for incorrect entries or for any consequences of using it.

## Licence

MIT. See [LICENSE](2026 Dharam Shah).
