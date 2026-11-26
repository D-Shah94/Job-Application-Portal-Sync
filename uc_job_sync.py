#!/usr/bin/env python3
r"""
uc_job_sync.py (v10 — Standalone Universal Edition)
Syncs ANY job application tracker (.md Markdown table, .csv, .tsv, or .json) to the
GOV.UK Universal Credit 'Job applications' portal (/work-search).

Key upgrades in v10:
  1. Standalone & Clean Folder Layout:
     - Stores all runtime state (uc_chrome_profile/, uc_ledger.json, uc_config.json,
       uc_debug.log, error snapshots) inside a single neat `.uc-data/` subfolder.
     - Remembers your last-used tracker path in `.uc-data/uc_config.json` so you can run it
       from ANY folder (e.g. C:\Users\Dhara\Repos\uc-worksearch-sync) without cluttering career-ops!
     - If no tracker path is configured, opens a native Windows file-picker dialog (or accepts
       a drag-and-dropped file path in the terminal, or press [f] in the Action Menu anytime).
  2. Universal Tracker Parser (.md, .csv, .tsv, .json):
     - Auto-detects column headers across custom trackers (Role/Job Title/Position,
       Company/Employer, Date/Date Applied, Status/Stage/Outcome, Notes/Comments, Via/Source).
     - Auto-generates row numbers (001, 002...) if the tracker has no '#' column.
     - Parses ISO dates (2026-10-26), UK dates (26/10/2026, 26-10-2026), and text dates (26 Oct 2026).
  3. Full 5-Status Universal Credit Lifecycle:
     - Maps tracker statuses to all 5 UC radio states:
         * APPLIED      (#clickable-APPLIED      -> "Applied" + application date)
         * INTERVIEW    (#clickable-INTERVIEW    -> "Going for an interview")
         * SUCCESSFUL   (#clickable-SUCCESSFUL   -> "Successful")
         * UNSUCCESSFUL (#clickable-UNSUCCESSFUL -> "Unsuccessful")
         * INTERESTED   (#clickable-INTERESTED   -> "Interested", opt-in via --include-interested)
         * SKIP         (Evaluated, Discarded, Draft, Pending, Skipped -> ignored)
     - Updates existing UC jobs whenever their status changes (e.g. Applied -> Unsuccessful,
       Applied -> Going for an interview, or Going for an interview -> Unsuccessful/Successful).
     - Logs optional UC Notes (#id-notes, up to 3,000 chars) when --notes-mode is enabled.
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from playwright.sync_api import sync_playwright

if os.name == "nt":
    import msvcrt

BASE = "https://www.universal-credit.service.gov.uk"
LIST_URL = f"{BASE}/work-search"
HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / ".uc-data"
LOG = DATA_DIR / "uc_debug.log"
LEDGER = DATA_DIR / "uc_ledger.json"
CONFIG_FILE = DATA_DIR / "uc_config.json"

LIST_RE = re.compile(r"/work-search/?(\?.*)?$")
POST_LOGIN_REDIRECT_RE = re.compile(
    r"https://www\.universal-credit\.service\.gov\.uk/(to-do|dashboard|journal|work-search-commitments|home)(/.*|\?.*)?$"
)

# Universal column header aliases (case-insensitive)
COLUMN_ALIASES = {
    "id": ["#", "id", "no", "no.", "number", "row", "index", "ref"],
    "date": ["date", "date applied", "applied date", "application date", "applied on", "submitted", "applied"],
    "company": ["company", "employer", "employer name", "company name", "organisation", "organization", "firm"],
    "role": ["role", "job title", "title", "position", "job", "role title", "vacancy"],
    "status": ["status", "stage", "outcome", "state", "result", "application status", "uc status"],
    "notes": ["notes", "note", "comments", "comment", "details"],
    "via": ["via", "source", "channel", "platform", "board"],
}

# Maps normalised tracker status strings -> UC target state
DEFAULT_STATUS_MAP = {
    # Applied
    "applied": "APPLIED",
    "submitted": "APPLIED",
    "sent": "APPLIED",
    "in progress": "APPLIED",
    "awaiting": "APPLIED",
    "awaiting response": "APPLIED",
    # Going for an interview
    "interview": "INTERVIEW",
    "interviewing": "INTERVIEW",
    "going for an interview": "INTERVIEW",
    "phone screen": "INTERVIEW",
    "screening": "INTERVIEW",
    "technical test": "INTERVIEW",
    "oa": "INTERVIEW",
    "assessment": "INTERVIEW",
    "final round": "INTERVIEW",
    # Successful
    "offer": "SUCCESSFUL",
    "accepted": "SUCCESSFUL",
    "successful": "SUCCESSFUL",
    "hired": "SUCCESSFUL",
    # Unsuccessful
    "rejected": "UNSUCCESSFUL",
    "unsuccessful": "UNSUCCESSFUL",
    "declined": "UNSUCCESSFUL",
    "ghosted": "UNSUCCESSFUL",
    "closed": "UNSUCCESSFUL",
    # Interested (only synced if --include-interested is passed)
    "interested": "INTERESTED",
    "saved": "INTERESTED",
    "bookmarked": "INTERESTED",
    # Skipped (never logged to UC)
    "evaluated": "SKIP",
    "discarded": "SKIP",
    "skipped": "SKIP",
    "pending": "SKIP",
    "draft": "SKIP",
    "withdrawn": "SKIP",
}

UC_TARGETS = {
    "INTERESTED": {"radio": "#clickable-INTERESTED", "label": "Interested"},
    "APPLIED": {"radio": "#clickable-APPLIED", "label": "Applied"},
    "INTERVIEW": {"radio": "#clickable-INTERVIEW", "label": "Going for an interview"},
    "SUCCESSFUL": {"radio": "#clickable-SUCCESSFUL", "label": "Successful"},
    "UNSUCCESSFUL": {"radio": "#clickable-UNSUCCESSFUL", "label": "Unsuccessful"},
}


class StepAbortToMenu(Exception):
    """Raised when the user presses 'q' at a [STEP] prompt to return cleanly to the Action Menu."""


class StepSkipItem(Exception):
    """Raised when the user presses 'k' at a [STEP] prompt to skip the current item."""


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def log(msg):
    ensure_data_dir()
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {msg}\n")
    if "--verbose" in sys.argv:
        print("  [log] " + msg)


def load_config():
    try:
        if CONFIG_FILE.is_file():
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_config(cfg):
    ensure_data_dir()
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ---------------- universal tracker parser (.md, .csv, .tsv, .json) ----------------
def clean(s):
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", str(s or ""))  # [text](url) -> text
    s = s.replace("**", "").replace("`", "").replace("\\|", "|")
    return re.sub(r"\s+", " ", s.replace("\u00a0", " ")).strip()


def norm(s):
    return clean(s).casefold()


def key(role, company):
    return norm(f"{role} - {company}")


def parse_flexible_date(val):
    s = clean(val)
    if not s:
        return None
    # 1. ISO YYYY-MM-DD
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # 2. UK DD/MM/YYYY or DD-MM-YYYY
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    # 3. Text dates ("26 Oct 2026", "8 October 2026")
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def resolve_column_map(headers):
    """Maps a tracker's custom column headers to canonical fields (id, date, company, role, status, notes, via)."""
    norm_to_actual = {norm(h): h for h in headers if clean(h)}
    col_map = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in norm_to_actual:
                col_map[canonical] = norm_to_actual[alias]
                break
    missing = [need for need in ("date", "company", "role") if need not in col_map]
    if missing:
        sys.exit(
            f"Could not auto-detect required column(s) {missing} in tracker headers: {headers}.\n"
            f"Supported aliases: Date={COLUMN_ALIASES['date']}, Company={COLUMN_ALIASES['company']}, Role={COLUMN_ALIASES['role']}"
        )
    return col_map


def parse_raw_records(path):
    """Reads .md, .csv, .tsv, or .json into (headers_list, list_of_row_dicts)."""
    ext = path.suffix.lower()
    text = path.read_text(encoding="utf-8-sig")

    if ext == ".json":
        data = json.loads(text)
        if isinstance(data, dict) and "applications" in data:
            data = data["applications"]
        if not isinstance(data, list) or not data:
            sys.exit(f"No JSON array of applications found in {path}")
        headers = list(data[0].keys())
        return headers, data

    if ext in (".csv", ".tsv"):
        delimiter = "\t" if ext == ".tsv" else ","
        reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
        rows = list(reader)
        return list(reader.fieldnames or []), rows

    # Default: Markdown table (.md or any text file containing pipe tables)
    lines = [l for l in text.splitlines() if l.strip().startswith("|")]
    if len(lines) < 3:
        sys.exit(f"No markdown table found in {path}")
    hdr = [clean(c) for c in lines[0].strip().strip("|").split("|")]
    raw_rows = []
    for l in lines[2:]:
        cells = [clean(c) for c in l.strip().strip("|").split("|")]
        if all(re.match(r"^[-: ]*$", c) for c in cells):
            continue
        raw_rows.append(dict(zip(hdr, cells)))
    return hdr, raw_rows


def pick_tracker_file_interactively():
    """Opens a native Windows file-picker dialog or prompts for a drag-and-dropped path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askopenfilename(
            title="Select Job Applications Tracker (.md, .csv, .tsv, .json)",
            filetypes=[
                ("Tracker files", "*.md *.csv *.tsv *.json"),
                ("Markdown files", "*.md"),
                ("CSV / TSV files", "*.csv *.tsv"),
                ("All files", "*.*"),
            ],
        )
        root.destroy()
        if selected and Path(selected).is_file():
            return Path(selected).resolve()
    except Exception:
        pass

    ans = input("\nDrag & drop your tracker file (.md/.csv/.tsv/.json) here and press ENTER: ").strip().strip('"').strip("'")
    if ans and Path(ans).is_file():
        return Path(ans).resolve()
    sys.exit("No valid tracker file provided.")


def find_tracker(arg_path, remember=True):
    cfg = load_config()
    if arg_path:
        p = Path(arg_path.strip().strip('"').strip("'")).resolve()
        if not p.is_file():
            sys.exit(f"Tracker file not found: {p}")
        if remember:
            cfg["last_tracker_path"] = str(p)
            save_config(cfg)
        return p

    # Check environment variable, local folder, remembered config path, or career-ops sibling folder
    candidates = [
        os.environ.get("CAREER_OPS_TRACKER"),
        HERE / "applications.md",
        HERE / "data" / "applications.md",
        Path.cwd() / "applications.md",
        Path.cwd() / "data" / "applications.md",
        cfg.get("last_tracker_path"),
        HERE.parent / "career-ops" / "data" / "applications.md",
        HERE.parent / "career-ops" / "applications.md",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            resolved = Path(c).resolve()
            if remember and cfg.get("last_tracker_path") != str(resolved):
                cfg["last_tracker_path"] = str(resolved)
                save_config(cfg)
            return resolved

    picked = pick_tracker_file_interactively()
    if remember:
        cfg["last_tracker_path"] = str(picked)
        save_config(cfg)
    return picked


def map_status_to_uc_target(raw_status, include_interested=False):
    st = norm(raw_status)
    if not st:
        return "APPLIED"
    target = DEFAULT_STATUS_MAP.get(st)
    if target is None:
        if any(w in st for w in ("reject", "unsuccessful", "declin", "ghost")):
            target = "UNSUCCESSFUL"
        elif any(w in st for w in ("interview", "screen", "test", "assessment")):
            target = "INTERVIEW"
        elif any(w in st for w in ("offer", "success", "hired", "accept")):
            target = "SUCCESSFUL"
        elif any(w in st for w in ("eval", "discard", "skip", "draft", "withdraw")):
            target = "SKIP"
        elif any(w in st for w in ("interest", "save", "bookmark")):
            target = "INTERESTED"
        elif any(w in st for w in ("appl", "submit", "sent")):
            target = "APPLIED"
        else:
            target = "SKIP"

    if target == "INTERESTED" and not include_interested:
        return "SKIP"
    return target


def read_tracker(path, skip_unlogged_rejected=False, include_interested=False, notes_mode="none"):
    headers, raw_rows = parse_raw_records(path)
    col_map = resolve_column_map(headers)
    today, rows = dt.date.today(), []

    for idx, raw in enumerate(raw_rows, 1):
        role = clean(raw.get(col_map["role"], ""))
        company = clean(raw.get(col_map["company"], ""))
        date_raw = clean(raw.get(col_map["date"], ""))
        parsed_date = parse_flexible_date(date_raw)

        if not (role and company and parsed_date):
            continue

        row_id = clean(raw.get(col_map["id"], "")) if "id" in col_map else f"{idx:03d}"
        if not row_id:
            row_id = f"{idx:03d}"

        status_raw = clean(raw.get(col_map["status"], "Applied")) if "status" in col_map else "Applied"
        uc_target = map_status_to_uc_target(status_raw, include_interested=include_interested)

        via_val = clean(raw.get(col_map["via"], "")) if "via" in col_map else ""
        notes_val = clean(raw.get(col_map["notes"], "")) if "notes" in col_map else ""

        uc_note = ""
        if notes_mode == "notes" and notes_val and notes_val != "-":
            uc_note = notes_val[:3000]
        elif notes_mode == "via" and via_val and via_val not in ("—", "-"):
            uc_note = f"Applied via {via_val}"[:3000]

        is_skipped = uc_target == "SKIP"
        is_rejected = uc_target == "UNSUCCESSFUL"
        is_applied_family = uc_target in ("APPLIED", "INTERVIEW", "SUCCESSFUL", "INTERESTED")

        if skip_unlogged_rejected:
            eligible_to_add = is_applied_family
        else:
            eligible_to_add = not is_skipped

        if parsed_date > today and eligible_to_add:
            print(f"  ! #{row_id} has a future date ({parsed_date.isoformat()}) - skipped")
            eligible_to_add = False

        rows.append({
            "#": row_id,
            "Date": parsed_date.isoformat(),
            "Company": company,
            "Role": role,
            "Status": status_raw,
            "_status": status_raw.casefold(),
            "_uc_target": uc_target,
            "_uc_label": UC_TARGETS[uc_target]["label"] if not is_skipped else "Skip",
            "_uc_radio": UC_TARGETS[uc_target]["radio"] if not is_skipped else "",
            "_date": parsed_date,
            "_key": key(role, company),
            "_is_skipped": is_skipped,
            "_is_rejected": is_rejected,
            "_is_applied": is_applied_family,
            "_eligible_to_add": eligible_to_add,
            "_uc_note": uc_note,
        })
    return rows


# ---------------- matching / planning (pure logic, unit-testable) ----------------
def match_rows(rows, items, today):
    """
    items: list of dicts {'key': str, 'age': int|None, 'uc_status': str, 'update_url': str} from UC.
    Returns dict mapping matched md row index -> UC item dict.
    """
    key_count = Counter(r["_key"] for r in rows if not r["_is_skipped"])
    pool = {}
    for it in items:
        pool.setdefault(it["key"], []).append(it)
    matched = {}
    for i in range(len(rows) - 1, -1, -1):
        r = rows[i]
        if r["_is_skipped"]:
            continue
        candidates = pool.get(r["_key"])
        if not candidates:
            continue
        if key_count[r["_key"]] == 1:  # unique pair: text match is enough
            matched[i] = candidates.pop(0)
            continue
        exp = (today - r["_date"]).days  # duplicate pair: confirm with date (+/- 2 days)
        matched_idx = None
        for j, it in enumerate(candidates):
            a = it["age"]
            if a is None or abs(a - exp) <= 2:
                matched_idx = j
                break
        if matched_idx is not None:
            matched[i] = candidates.pop(matched_idx)
    return matched


def ledger_indexes(rows, ledger):
    if not isinstance(ledger, list):
        return set()
    seen = {(e["date"], e["key"]) for e in ledger if isinstance(e, dict) and "date" in e and "key" in e}
    return {i for i, r in enumerate(rows) if (r["Date"][:10], r["_key"]) in seen}


def find_last_actionable_index(rows):
    """Returns the index of the latest logged application in the tracker (skipping Evaluated & Discarded)."""
    last_idx = -1
    for i, r in enumerate(rows):
        if not r["_is_skipped"]:
            last_idx = i
    return last_idx


def current_uc_status_needs_update(current_label, target_label):
    """Returns True if the UC portal status differs from the tracker's target UC status."""
    return current_label.casefold() != target_label.casefold()


def make_plan(rows, matched_map, ledger_idx, from_number=None):
    """
    Returns (anchor_idx, to_update_list, to_add_list, last_actionable_idx)
      - to_update_list: [(row, uc_item)] for rows already on UC whose target UC status label
        differs from their current UC status on the portal (e.g. Applied -> Unsuccessful,
        Applied -> Going for an interview, Going for an interview -> Successful/Unsuccessful).
      - to_add_list: [row] for eligible rows after anchor_idx up to last_actionable_idx.
    """
    last_actionable_idx = find_last_actionable_index(rows)
    if last_actionable_idx == -1:
        return None, [], [], -1

    # 1. Any existing UC role whose tracker status maps to a different UC label than what is currently shown on UC
    to_update = []
    for idx in sorted(matched_map.keys()):
        r = rows[idx]
        if r["_is_skipped"]:
            continue
        uc_item = matched_map[idx]
        current_uc_label = uc_item.get("uc_status", "").strip()
        target_uc_label = r["_uc_label"]
        if current_uc_status_needs_update(current_uc_label, target_uc_label):
            if uc_item.get("update_url"):
                to_update.append((r, uc_item))

    # 2. Determine anchor for adding new roles
    if from_number:
        idx = [i for i, r in enumerate(rows) if r.get("#") == from_number]
        if not idx:
            sys.exit(f"--from-number {from_number} not found in tracker")
        anchor = idx[0] - 1
        todo_slice = rows[idx[0] : last_actionable_idx + 1]
        to_add = [r for r in todo_slice if r["_eligible_to_add"] and rows.index(r) not in matched_map]
        return anchor, to_update, to_add, last_actionable_idx

    cands = set(matched_map.keys()) | ledger_idx
    if not cands:
        # First-time user with an empty UC portal: start at the beginning of the tracker!
        to_add = [r for r in rows[: last_actionable_idx + 1] if r["_eligible_to_add"]]
        return -1, to_update, to_add, last_actionable_idx

    anchor = max(cands)
    todo_slice = rows[anchor + 1 : last_actionable_idx + 1]
    to_add = [r for r in todo_slice if r["_eligible_to_add"]]
    return anchor, to_update, to_add, last_actionable_idx


def load_ledger():
    try:
        data = json.loads(LEDGER.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_ledger(entries):
    ensure_data_dir()
    LEDGER.write_text(json.dumps(entries, indent=1), encoding="utf-8")


# ---------------- Playwright helpers ----------------
def attach_diagnostics(ctx):
    def watch(pg):
        log("NEW TAB opened")
        pg.on("framenavigated", lambda fr: fr == pg.main_frame and log(f"NAV      {fr.url}"))

        def on_resp(resp):
            try:
                if resp.request.resource_type == "document":
                    loc = resp.headers.get("location", "")
                    log(f"DOC {resp.status}  {resp.url}" + (f"  -> Location: {loc}" if loc else ""))
                    if "ejected" in resp.url or "ejected" in loc:
                        log("!!! 'ejected' seen - see the DOC/NAV lines just above")
            except Exception:
                pass

        pg.on("response", on_resp)
        pg.on("close", lambda: log("TAB CLOSED"))

    for pg in ctx.pages:
        watch(pg)
    ctx.on("page", watch)


def snapshot(page, tag):
    try:
        ensure_data_dir()
        page.screenshot(path=str(DATA_DIR / f"uc_{tag}.png"), full_page=True)
        (DATA_DIR / f"uc_{tag}.html").write_text(page.content(), encoding="utf-8")
        log(f"SNAPSHOT uc_{tag}.png/.html url={page.url}")
    except Exception as e:
        log(f"snapshot failed: {e}")


def live_url(pg):
    try:
        return pg.evaluate("location.href")
    except Exception:
        return pg.url


def dismiss_timeout_dialog_if_present(page):
    """If UC's 15-minute session timeout warning modal is visible, clicks 'Stay signed in'."""
    try:
        keep_alive_btn = page.locator("button:has-text('Stay signed in'), a:has-text('Stay signed in')")
        if keep_alive_btn.count() > 0 and keep_alive_btn.first.is_visible():
            keep_alive_btn.first.click()
            log("Clicked 'Stay signed in' on timeout dialog")
    except Exception:
        pass


def check_enter_pressed_nonblocking():
    """Checks if ENTER was pressed in the console without spawning a blocking stdin thread."""
    if os.name == "nt":
        pressed = False
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                pressed = True
        return pressed
    return False


def wait_until_ready(ctx, manual, timeout):
    """
    Waits for the user to log in. As soon as the browser lands on /to-do (or /dashboard,
    /journal, /work-search-commitments), automatically redirects to /work-search!
    """
    print(
        "\n>>> In the Chrome window: log in to Universal Credit (incl. 2FA if prompted).\n"
        "    • As soon as you land on '/to-do' (or '/dashboard'), the script will automatically\n"
        "      navigate to '/work-search' ('Job applications') and continue!\n"
        "    • (Or press ENTER here at any time once you are on 'Job applications'.)",
        flush=True,
    )
    pump = ctx.pages[0] if ctx.pages else ctx.new_page()
    end, seen_since = time.time() + timeout, None
    last_auto_redirect = 0.0

    while time.time() < end:
        try:
            pump.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)
        tabs = [(pg, live_url(pg)) for pg in ctx.pages if not pg.is_closed()]
        ready = None
        for pg, u in tabs:
            try:
                dismiss_timeout_dialog_if_present(pg)
                if not manual and POST_LOGIN_REDIRECT_RE.match(u) and (time.time() - last_auto_redirect > 3.0):
                    print(f"  -> Detected post-login page ({u.split('.gov.uk')[-1]}); auto-navigating to {LIST_URL}...", flush=True)
                    log(f"Auto-redirecting from {u} to {LIST_URL}")
                    last_auto_redirect = time.time()
                    pg.goto(LIST_URL, wait_until="domcontentloaded")
                    u = live_url(pg)

                if LIST_RE.search(u) and pg.locator("#add-job").count():
                    ready = pg
            except Exception:
                pass
        if check_enter_pressed_nonblocking():
            log("ENTER pressed; TABS: " + " | ".join(u for _, u in tabs))
            return ready or next((pg for pg, u in tabs if "/work-search" in u), None)
        if not manual and ready:
            seen_since = seen_since or time.time()
            if time.time() - seen_since > 0.8:
                log("auto-detected Job applications tab")
                return ready
        else:
            seen_since = None
    return None


def read_items(page, n):
    url = LIST_URL if n == 1 else f"{LIST_URL}?page={n}"
    resp = page.goto(url, wait_until="domcontentloaded")
    log(f"list page {n} -> {page.url} ({resp.status if resp else '?'})")
    if "/work-search" not in page.url:
        snapshot(page, "not_on_list")
        return None
    items = []
    for li in page.locator("ul.job-list > li").all():
        title = li.locator("h3").first.inner_text()
        st_label = li.locator(".job-list__status-label")
        uc_status = st_label.first.inner_text().strip() if st_label.count() else ""
        st = li.locator(".job-list__status-text")
        txt = st.first.inner_text() if st.count() else ""
        m = re.search(r"(\d+)\s+day", txt)
        age = int(m.group(1)) if m else (0 if "today" in txt.lower() else None)
        link = li.locator("a.job-list__item-link")
        href = link.first.get_attribute("href") if link.count() else ""
        if href and href.startswith("/"):
            href = f"{BASE}{href}"
        items.append({
            "key": norm(title),
            "title": clean(title),
            "uc_status": uc_status,
            "age": age,
            "update_url": href or "",
        })
    return items


def has_next(page):
    return page.locator("a.pagination__link", has_text="Next").count() > 0


def total_results(page):
    el = page.locator(".pagination__summary")
    if el.count():
        m = re.search(r"of\s+([\d,]+)\s+results", el.first.inner_text())
        if m:
            return int(m.group(1).replace(",", ""))
    return page.locator("ul.job-list > li").count()


def scan_uc_pages(page, rows, max_pages):
    today = dt.date.today()
    all_uc_items = []
    scanned = 0
    actionable_indices = [i for i, r in enumerate(rows) if not r["_is_skipped"]]

    for n in range(1, max_pages + 1):
        items = read_items(page, n)
        if items is None:
            return None, scanned
        if not items:
            break
        scanned += 1
        page_matches = match_rows(rows, items, today)
        all_uc_items.extend(items)
        combined_matches = match_rows(rows, all_uc_items, today)

        if combined_matches:
            highest_matched = max(combined_matches.keys())
            expected_below_anchor = [i for i in actionable_indices if i <= highest_matched]
            if all(i in combined_matches for i in expected_below_anchor):
                break
            if not page_matches and scanned >= 2:
                break

        if not has_next(page):
            break

    if scanned > 1:
        page.goto(LIST_URL, wait_until="domcontentloaded")

    return all_uc_items, scanned


def prompt_step_decision(page, description):
    while True:
        dismiss_timeout_dialog_if_present(page)
        print(f"    [STEP] Look at Chrome: {description}")
        print(
            "           [ENTER] Save & continue   [y] Save & run ALL remaining automatically!   [m] Save & open Menu\n"
            "           [k]     Skip this item    [q] Cancel unsaved form & return to Menu"
        )
        ans = input("           Action [ENTER/y/m/k/q]: ").strip().lower()
        if ans == "":
            return "save_continue"
        if ans == "y":
            return "save_auto_all"
        if ans == "m":
            return "save_menu"
        if ans == "k":
            page.goto(LIST_URL, wait_until="domcontentloaded")
            raise StepSkipItem()
        if ans == "q":
            page.goto(LIST_URL, wait_until="domcontentloaded")
            raise StepAbortToMenu()
        print(f"           Please press ENTER, or type y, m, k, or q (got {ans!r}).")


def fill_optional_notes_if_configured(page, row):
    note = row.get("_uc_note", "")
    if not note:
        return
    notes_el = page.locator("textarea#id-notes, textarea[name='notes']")
    if notes_el.count() > 0 and notes_el.first.is_visible():
        existing = notes_el.first.input_value().strip()
        if not existing:
            notes_el.first.fill(note[:3000])


def update_uc_job_status(page, row, update_url, pause, step_mode=False):
    """
    Navigates to a UC job's Update page, selects the target UC radio status
    ('Unsuccessful', 'Going for an interview', 'Successful', etc.), verifies it is checked, and saves.
    """
    target_radio_id = row["_uc_radio"]
    target_label = row["_uc_label"]
    input_id = target_radio_id.lstrip("#")

    print(f"  -> Navigating to Update page for #{row.get('#')} ({row['Role']} - {row['Company']}) -> {target_label}...", flush=True)
    resp = page.goto(update_url, wait_until="domcontentloaded")
    log(f"update page #{row.get('#')} -> {page.url} ({resp.status if resp else '?'})")
    if "/work-search/job/update/" not in page.url:
        raise RuntimeError(f"Expected update URL, landed on {page.url}")

    radio_el = page.locator(target_radio_id)
    radio_el.wait_for(state="attached", timeout=10000)
    page.locator(f"label[for='{input_id}']").click()

    if not radio_el.is_checked():
        radio_el.check(force=True)
    if not radio_el.is_checked():
        raise RuntimeError(f"Read-back check failed: '{target_label}' radio button is not checked!")

    fill_optional_notes_if_configured(page, row)

    decision = "save_continue"
    if step_mode:
        decision = prompt_step_decision(
            page,
            f"#{row.get('#')} ({row['Role']} - {row['Company']}) now has '{target_label}' selected.",
        )

    with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
        page.locator("#id-submit-button").click()

    errs = [e.inner_text().strip() for e in page.locator("[data-validation-error]").all() if e.inner_text().strip()]
    if errs or "/work-search" not in page.url:
        raise RuntimeError(f"Update failed. Errors: {errs or 'none visible'} (url={page.url})")

    time.sleep(pause)
    return decision


def find_newest_update_url_for_row(page, row):
    if not LIST_RE.search(page.url) or "page=" in page.url:
        page.goto(LIST_URL, wait_until="domcontentloaded")
    for li in page.locator("ul.job-list > li").all():
        title = li.locator("h3").first.inner_text()
        if norm(title) == row["_key"]:
            link = li.locator("a.job-list__item-link")
            if link.count():
                href = link.first.get_attribute("href") or ""
                return f"{BASE}{href}" if href.startswith("/") else href
    return None


def add_job(page, row, pause, step_mode=False):
    """
    Adds a new job on UC.
    - If target is INTERESTED: logs directly as 'Interested'.
    - Otherwise (APPLIED, INTERVIEW, SUCCESSFUL, UNSUCCESSFUL): logs as 'Applied' first so UC
      records the application date ('When did you apply?'), and if the final status is
      INTERVIEW, SUCCESSFUL, or UNSUCCESSFUL, immediately updates the newly created entry!
    """
    target_state = row["_uc_target"]
    target_label = row["_uc_label"]
    print(f"  -> Adding #{row.get('#')} ({row['Role']} - {row['Company']}, Date {row['Date']})...", flush=True)
    if not LIST_RE.search(page.url) or "page=" in page.url or not page.locator("#add-job").count():
        page.goto(LIST_URL, wait_until="domcontentloaded")

    before = total_results(page)

    page.locator("#add-job").click()
    page.wait_for_url(re.compile(r".*/work-search/job/?$"))
    page.locator("#id-jobTitle").fill(row["Role"])
    page.locator("#id-employer").fill(row["Company"])

    if target_state == "INTERESTED":
        page.locator("label[for='clickable-INTERESTED']").click()
        if not page.locator("#clickable-INTERESTED").is_checked():
            page.locator("#clickable-INTERESTED").check(force=True)
    else:
        page.locator("label[for='clickable-APPLIED']").click()
        day = page.locator('input[name="applicationDate.day"]')
        mon = page.locator('input[name="applicationDate.month"]')
        yr = page.locator('input[name="applicationDate.year"]')
        day.wait_for(state="visible", timeout=5000)
        d = row["_date"]
        day.fill(str(d.day))
        mon.fill(str(d.month))
        yr.fill(str(d.year))

        got = (
            page.locator("#id-jobTitle").input_value(),
            page.locator("#id-employer").input_value(),
            page.locator("#clickable-APPLIED").is_checked(),
            day.input_value(),
            mon.input_value(),
            yr.input_value(),
        )
        want = (row["Role"], row["Company"], True, str(d.day), str(d.month), str(d.year))
        if got != want:
            raise RuntimeError(f"Read-back mismatch.\n  wanted {want}\n  got    {got}")

    fill_optional_notes_if_configured(page, row)

    decision = "save_continue"
    if step_mode:
        extra = f" (will then mark '{target_label}')" if target_state not in ("APPLIED", "INTERESTED") else ""
        decision = prompt_step_decision(
            page,
            f"#{row.get('#')} ({row['Role']} - {row['Company']}, Date {row['Date']}) is filled & verified{extra}.",
        )

    with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
        page.locator("#id-submit-button").click()
    banner = page.locator(".govuk-notification-banner--success")
    if not (banner.count() and "job added" in banner.first.inner_text().lower()):
        errs = [e.inner_text().strip() for e in page.locator("[data-validation-error]").all() if e.inner_text().strip()]
        raise RuntimeError(f"No 'Job added' banner. Errors: {errs or 'none visible'} (url={page.url})")
    after = total_results(page)
    if after != before + 1:
        raise RuntimeError(f"Result count {before} -> {after} (expected +1)")
    time.sleep(pause)

    # If the final target status is INTERVIEW, SUCCESSFUL, or UNSUCCESSFUL, immediately update it!
    if target_state not in ("APPLIED", "INTERESTED"):
        update_url = find_newest_update_url_for_row(page, row)
        if not update_url:
            raise RuntimeError(f"Added #{row.get('#')} but could not find its Update job link on Page 1")
        sub_step = step_mode and (decision == "save_continue")
        sub_decision = update_uc_job_status(page, row, update_url, pause, step_mode=sub_step)
        if sub_decision in ("save_auto_all", "save_menu"):
            decision = sub_decision

    return after, decision


def print_queue_summary(rows, anchor, last_actionable_idx, to_update, to_add):
    if anchor is not None and anchor >= 0:
        print(f"Anchor: #{rows[anchor].get('#')} {rows[anchor]['Role']} - {rows[anchor]['Company']}")
    if last_actionable_idx >= 0:
        print(
            f"Latest logged application cutoff: #{rows[last_actionable_idx].get('#')} "
            f"{rows[last_actionable_idx]['Role']} - {rows[last_actionable_idx]['Company']} "
            f"({rows[last_actionable_idx].get('Status')} -> {rows[last_actionable_idx]['_uc_label']})"
        )

    if to_update:
        print(f"\n{len(to_update)} existing UC role(s) queued for status update:")
        for r, u in to_update:
            print(
                f"  [UPDATE: {u.get('uc_status', 'Applied')} -> {r['_uc_label']}] "
                f"#{r.get('#')}  {r['Date']}  {r['Role']} - {r['Company']}"
            )
    else:
        print("\n0 existing UC roles need status updates.")

    if to_add:
        dups = [r for r in to_add if sum(x["_key"] == r["_key"] for x in rows if not x["_is_skipped"]) > 1]
        print(f"\n{len(to_add)} new role(s) queued to add (oldest first, up to #{rows[last_actionable_idx].get('#')}):")
        for r in to_add:
            tag = "[ADD as Applied]" if r["_uc_target"] == "APPLIED" else f"[ADD -> {r['_uc_label']}]"
            dup_note = "   [duplicate Role+Company in tracker]" if r in dups else ""
            print(f"  {tag:24s} #{r.get('#')}  {r['Date']}  {r['Role']} - {r['Company']}{dup_note}")
    else:
        print("\n0 new roles need to be added.")


def prompt_action_menu(page, to_update, to_add):
    while True:
        dismiss_timeout_dialog_if_present(page)
        u_cnt = len(to_update)
        a_cnt = len(to_add)
        print(
            f"\n╭─── Action Menu ({u_cnt} update(s) remaining, {a_cnt} addition(s) remaining) ────────────────╮\n"
            "│  [y] Run ALL remaining items automatically (no pauses)                        │\n"
            "│  [t] Test-step the NEXT 1 item visually (pauses before Save, then returns)    │\n"
            "│  [s] Step-by-step mode (pauses before each Save; press 'y' anytime to auto)   │\n"
            "│  [u] Run ONLY remaining status updates automatically                          │\n"
            "│  [a] Run ONLY remaining new additions automatically                           │\n"
            "│  [p] Print remaining queue summary                                            │\n"
            "│  [r] Re-scan UC portal pages & refresh queue                                  │\n"
            "│  [f] Switch / pick a different tracker file (.md, .csv, .tsv, .json)          │\n"
            "│  [q] Quit / close browser (all completed items are already saved)             │\n"
            "╰───────────────────────────────────────────────────────────────────────────────╯"
        )
        ans = input("Choice [y/t/s/u/a/p/r/f/q]: ").strip().lower()
        if ans in ("y", "t", "s", "u", "a", "p", "r", "f", "q", "n"):
            return "q" if ans == "n" else ans
        print(f"  Please choose one of y, t, s, u, a, p, r, f, or q (got {ans!r}).")


def launch(p, a):
    ensure_data_dir()
    if a.cdp:
        ctx = p.chromium.connect_over_cdp(a.cdp).contexts[0]
    else:
        kind = p.firefox if a.browser == "firefox" else p.chromium
        kw = dict(headless=False, slow_mo=a.slow_mo)
        if a.browser == "chrome":
            kw["channel"] = "chrome"
            kw["args"] = ["--disable-blink-features=AutomationControlled"]
        prof = a.profile
        if prof.lower() in ("none", "temp", ""):
            prof = tempfile.mkdtemp(prefix="uc_tmp_profile_")
        else:
            prof_path = Path(prof)
            if not prof_path.is_absolute():
                legacy_path = HERE / prof_path
                prof_path = legacy_path if legacy_path.exists() else (DATA_DIR / prof_path)
            prof = str(prof_path)
        ctx = kind.launch_persistent_context(prof, **kw)
    attach_diagnostics(ctx)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    if not a.cdp:
        page.goto(LIST_URL)
    return ctx


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--md", "--file", dest="md", help="path to tracker file (.md, .csv, .tsv, .json)")
    ap.add_argument("--pick-file", action="store_true", help="open file picker dialog to choose tracker file")
    ap.add_argument("--browser", choices=["firefox", "chromium", "chrome"], default="chrome", help="default: chrome")
    ap.add_argument("--profile", default="uc_chrome_profile", help="folder name for saved UC cookies/2FA trust")
    ap.add_argument("--cdp", help="attach to your own Chrome (started with --remote-debugging-port), e.g. http://localhost:9222")
    ap.add_argument("--skip-unlogged-rejected", action="store_true", help="only add 'Applied/Interview/Offer' rows after anchor")
    ap.add_argument("--include-interested", action="store_true", help="also sync rows marked 'Interested / Bookmarked'")
    ap.add_argument("--notes-mode", choices=["none", "via", "notes"], default="none", help="populate UC optional Notes field (default: none)")
    ap.add_argument("--max-pages", type=int, default=6, help="UC list pages to scan for existing jobs & anchor (15 entries each)")
    ap.add_argument("--max-add", type=int, default=40, help="refuse to add more than this in one run")
    ap.add_argument("--limit", type=int, default=0, help="limit number of additions")
    ap.add_argument("--from-number", help='force start at this tracker "#"')
    ap.add_argument("--ignore-ledger", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="scan and print plan only")
    ap.add_argument("--step", action="store_true", help="start directly in step-by-step mode")
    ap.add_argument("--yes", action="store_true", help="run all updates & additions automatically without prompting")
    ap.add_argument("--manual-enter", action="store_true", help="wait for ENTER instead of auto-detecting the page")
    ap.add_argument("--login-timeout", type=int, default=900)
    ap.add_argument("--keep-open", action="store_true", help="keep Chrome open until ENTER is pressed at the end")
    ap.add_argument("--slow-mo", type=int, default=0)
    ap.add_argument("--pause", type=float, default=0.3)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    ensure_data_dir()
    LOG.write_text("", encoding="utf-8")
    tracker_path = pick_tracker_file_interactively() if a.pick_file else find_tracker(a.md)
    rows = read_tracker(
        tracker_path,
        skip_unlogged_rejected=a.skip_unlogged_rejected,
        include_interested=a.include_interested,
        notes_mode=a.notes_mode,
    )
    counts = Counter(r["_uc_target"] for r in rows)
    print(
        f"Read {len(rows)} rows from {tracker_path}: "
        f"{counts['APPLIED']} Applied, {counts['INTERVIEW']} Interview, "
        f"{counts['SUCCESSFUL']} Offer, {counts['UNSUCCESSFUL']} Rejected, {counts['SKIP']} Skipped"
    )
    log(f"=== run start tracker={tracker_path} browser={a.browser} cdp={a.cdp} profile={a.profile}")
    with sync_playwright() as p:
        ctx = launch(p, a)
        stopped = False
        try:
            stopped = run(ctx, rows, a)
        finally:
            if stopped or a.keep_open:
                try:
                    input("\nPress ENTER to close the browser... ")
                except EOFError:
                    pass
            try:
                ctx.close()
            except Exception:
                pass


def run(ctx, rows, a):
    page = wait_until_ready(ctx, a.manual_enter, a.login_timeout)
    if page is None:
        print("STOP: never saw the Job applications page (see .uc-data/uc_debug.log).")
        return True

    def refresh_plan():
        all_uc_items, scanned = scan_uc_pages(page, rows, a.max_pages)
        if all_uc_items is None:
            print(f"STOP: UC sent the browser to {page.url} (signed out?). See .uc-data/uc_debug.log")
            return None
        matched_map = match_rows(rows, all_uc_items, dt.date.today())
        ledger = [] if a.ignore_ledger else load_ledger()
        lidx = ledger_indexes(rows, ledger)
        anchor, to_update, to_add, last_actionable_idx = make_plan(rows, matched_map, lidx, a.from_number)
        if a.limit and to_add:
            to_add = to_add[: a.limit]
        print(f"\nScanned {scanned} UC page(s) ({len(all_uc_items)} UC entries inspected).")
        return anchor, to_update, to_add or [], last_actionable_idx, ledger

    plan = refresh_plan()
    if plan is None:
        return True
    anchor, to_update, to_add, last_actionable_idx, ledger = plan

    if anchor is None and not to_update:
        print(
            "STOP: no UC entry (and no ledger record) matches tracker - refusing to guess.\n"
            "      Raise --max-pages, or use --from-number <#> to say where to start."
        )
        return True

    print_queue_summary(rows, anchor, last_actionable_idx, to_update, to_add)

    if not to_update and not to_add:
        print("\nNothing to update or add - UC is completely up to date!")
        return False

    if len(to_add) > a.max_add:
        print(
            f"STOP: {len(to_add)} entries to add exceeds --max-add {a.max_add}. "
            "Check the tracker/anchor, then raise --max-add."
        )
        return True

    if a.dry_run:
        print("\n--dry-run: nothing submitted.")
        return False

    total_updated_session = 0
    total_added_session = 0
    next_mode = "y" if a.yes else ("s" if a.step else None)

    while to_update or to_add:
        mode = next_mode or prompt_action_menu(page, to_update, to_add)
        next_mode = None

        if mode == "q":
            print(
                f"\nExited Action Menu. Session totals: "
                f"{total_updated_session} status update(s), {total_added_session} new role(s) added."
            )
            return False

        if mode == "p":
            print_queue_summary(rows, anchor, last_actionable_idx, to_update, to_add)
            continue

        if mode == "f":
            new_tracker = pick_tracker_file_interactively()
            cfg = load_config()
            cfg["last_tracker_path"] = str(new_tracker)
            save_config(cfg)
            rows[:] = read_tracker(
                new_tracker,
                skip_unlogged_rejected=a.skip_unlogged_rejected,
                include_interested=a.include_interested,
                notes_mode=a.notes_mode,
            )
            plan = refresh_plan()
            if plan is None:
                return True
            anchor, to_update, to_add, last_actionable_idx, ledger = plan
            print_queue_summary(rows, anchor, last_actionable_idx, to_update, to_add)
            continue

        if mode == "r":
            plan = refresh_plan()
            if plan is None:
                return True
            anchor, to_update, to_add, last_actionable_idx, ledger = plan
            print_queue_summary(rows, anchor, last_actionable_idx, to_update, to_add)
            continue

        run_updates = mode in ("y", "u", "t", "s")
        run_additions = mode in ("y", "a", "t", "s")
        is_step = mode in ("t", "s")
        single_item_only = mode == "t"
        items_processed_this_pass = 0
        return_to_menu = False

        # 1. Process Updates Queue
        if run_updates:
            while to_update:
                r, u = to_update[0]
                try:
                    decision = update_uc_job_status(page, r, u["update_url"], a.pause, step_mode=is_step)
                    to_update.pop(0)
                    total_updated_session += 1
                    items_processed_this_pass += 1
                    log(f"UPDATED #{r.get('#')} {r['Role']} - {r['Company']} -> {r['_uc_label']}")
                    print(f"  OK [Updated -> {r['_uc_label']}] #{r.get('#')} {r['Role']} - {r['Company']}")

                    if decision == "save_auto_all":
                        print("  >> Switched to automatic mode for all remaining items!")
                        is_step = False
                        single_item_only = False
                        run_additions = True
                    elif decision == "save_menu" or single_item_only:
                        return_to_menu = True
                        break
                except StepSkipItem:
                    skipped_r, _ = to_update.pop(0)
                    print(f"  ⏭️  Skipped #{skipped_r.get('#')} ({skipped_r['Role']} - {skipped_r['Company']}).")
                    if single_item_only:
                        return_to_menu = True
                        break
                except StepAbortToMenu:
                    print("  ↩️  Cancelled unsaved form — returning to Action Menu.")
                    return_to_menu = True
                    break
                except Exception as e:
                    snapshot(page, f"error_update_{r.get('#', 'x')}")
                    log(f"ERROR updating #{r.get('#')}: {e}")
                    print(f"\n  ⚠️ Error updating #{r.get('#')} {r['Role']} - {r['Company']}: {e}")
                    print("  Returning to Action Menu (you can pick [r] to re-scan or [q] to quit).")
                    return_to_menu = True
                    break

        if return_to_menu or (mode == "u"):
            continue

        # 2. Process Additions Queue
        if run_additions and not (single_item_only and items_processed_this_pass >= 1):
            while to_add:
                r = to_add[0]
                try:
                    count, decision = add_job(page, r, a.pause, step_mode=is_step)
                    to_add.pop(0)
                    total_added_session += 1
                    items_processed_this_pass += 1
                    ledger.append({
                        "date": r["Date"][:10],
                        "key": r["_key"],
                        "n": r.get("#"),
                        "status": r["_uc_label"],
                        "at": dt.datetime.isoformat(dt.datetime.now(), timespec="seconds"),
                    })
                    save_ledger(ledger)
                    status_str = f"Added as {r['_uc_label']}" if r["_uc_target"] in ("APPLIED", "INTERESTED") else f"Added + {r['_uc_label']}"
                    log(f"ADDED #{r.get('#')} {r['Role']} - {r['Company']} ({status_str}) total={count}")
                    print(f"  OK [{status_str}] #{r.get('#')} {r['Role']} - {r['Company']} (UC total {count})")

                    if decision == "save_auto_all":
                        print("  >> Switched to automatic mode for all remaining items!")
                        is_step = False
                        single_item_only = False
                    elif decision == "save_menu" or single_item_only:
                        return_to_menu = True
                        break
                except StepSkipItem:
                    skipped_r = to_add.pop(0)
                    print(f"  ⏭️  Skipped #{skipped_r.get('#')} ({skipped_r['Role']} - {skipped_r['Company']}).")
                    if single_item_only:
                        return_to_menu = True
                        break
                except StepAbortToMenu:
                    print("  ↩️  Cancelled unsaved form — returning to Action Menu.")
                    return_to_menu = True
                    break
                except Exception as e:
                    snapshot(page, f"error_add_{r.get('#', 'x')}")
                    log(f"ERROR adding #{r.get('#')}: {e}")
                    print(f"\n  ⚠️ Error at #{r.get('#')} {r['Role']} - {r['Company']}: {e}")
                    print("  Returning to Action Menu (you can pick [r] to re-scan or [q] to quit).")
                    return_to_menu = True
                    break

    print(
        f"\n🎉 All queued items complete! Session totals: "
        f"Updated {total_updated_session} role(s); Added {total_added_session} new role(s)."
    )
    return False


if __name__ == "__main__":
    main()