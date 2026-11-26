"""Read a tracker file (.md table, .csv, .tsv or .json) into Application objects."""
from __future__ import annotations

import csv
import json
import os
import re
from datetime import date, datetime
from pathlib import Path

from .config import COLUMN_ALIASES, NOTE_LIMIT, PROJECT_ROOT, STATUS_KEYWORDS, STATUS_MAP
from .models import Application, Target
from .state import load_config, save_config


class TrackerError(Exception):
    pass


def clean(value) -> str:
    """Strip markdown noise and collapse whitespace."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", str(value or ""))  # [text](url) -> text
    text = text.replace("**", "").replace("`", "").replace("\\|", "|")
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()


def normalise(value) -> str:
    return clean(value).casefold()


def make_key(role: str, company: str) -> str:
    """The portal shows an entry as 'Role - Company', so that is what we match on."""
    return normalise(f"{role} - {company}")


def parse_date(value) -> date | None:
    text = clean(value)
    if not text:
        return None
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)  # 2026-10-26
    if m:
        return _safe_date(int(m[1]), int(m[2]), int(m[3]))
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", text)  # 26/10/2026 (UK order)
    if m:
        return _safe_date(int(m[3]), int(m[2]), int(m[1]))
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def classify_status(raw: str, include_interested: bool = False) -> Target:
    status = normalise(raw)
    if not status:
        return Target.APPLIED
    target = STATUS_MAP.get(status)
    if target is None:
        target = Target.SKIP
        for words, candidate in STATUS_KEYWORDS:
            if any(w in status for w in words):
                target = candidate
                break
    if target is Target.INTERESTED and not include_interested:
        return Target.SKIP
    return target


def resolve_columns(headers: list[str]) -> dict[str, str]:
    """Map our field names to whatever the tracker calls its columns."""
    by_norm = {normalise(h): h for h in headers if clean(h)}
    found = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in by_norm:
                found[field] = by_norm[alias]
                break
    missing = [f for f in ("date", "company", "role") if f not in found]
    if missing:
        raise TrackerError(f"Could not find column(s) {missing} in headers {headers}")
    return found


def load_records(path: Path) -> tuple[list[str], list[dict]]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8-sig")

    if suffix == ".json":
        data = json.loads(text)
        if isinstance(data, dict) and "applications" in data:
            data = data["applications"]
        if not isinstance(data, list) or not data:
            raise TrackerError(f"No JSON array of applications in {path}")
        return list(data[0].keys()), data

    if suffix in (".csv", ".tsv"):
        reader = csv.DictReader(text.splitlines(), delimiter="\t" if suffix == ".tsv" else ",")
        rows = list(reader)
        return list(reader.fieldnames or []), rows

    lines = [ln for ln in text.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 3:
        raise TrackerError(f"No markdown table found in {path}")
    headers = [clean(c) for c in lines[0].strip().strip("|").split("|")]
    rows = []
    for line in lines[2:]:  # lines[1] is the |---|---| separator
        cells = [clean(c) for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r"[-: ]*", c) for c in cells):
            continue
        rows.append(dict(zip(headers, cells)))
    return headers, rows


def read_tracker(path: Path, *, skip_unlogged_rejected=False, include_interested=False,
                 notes_mode="none", today: date | None = None) -> list[Application]:
    today = today or date.today()
    headers, records = load_records(path)
    cols = resolve_columns(headers)
    apps = []

    for position, rec in enumerate(records, 1):
        role = clean(rec.get(cols["role"], ""))
        company = clean(rec.get(cols["company"], ""))
        applied_on = parse_date(rec.get(cols["date"], ""))
        if not (role and company and applied_on):
            continue

        number = clean(rec.get(cols["number"], "")) if "number" in cols else ""
        number = number or f"{position:03d}"
        status_text = clean(rec.get(cols["status"], "Applied")) if "status" in cols else "Applied"
        target = classify_status(status_text, include_interested)

        note = ""
        notes_val = clean(rec.get(cols["notes"], "")) if "notes" in cols else ""
        via_val = clean(rec.get(cols["via"], "")) if "via" in cols else ""
        if notes_mode == "notes" and notes_val and notes_val != "-":
            note = notes_val[:NOTE_LIMIT]
        elif notes_mode == "via" and via_val and via_val not in ("\u2014", "-"):
            note = f"Applied via {via_val}"[:NOTE_LIMIT]

        if skip_unlogged_rejected:
            eligible = target in (Target.APPLIED, Target.INTERVIEW, Target.SUCCESSFUL, Target.INTERESTED)
        else:
            eligible = target is not Target.SKIP

        if applied_on > today and eligible:
            print(f"  ! #{number} has a future date ({applied_on.isoformat()}) - skipped")
            eligible = False

        apps.append(Application(number, applied_on, company, role, status_text, target,
                                make_key(role, company), note, eligible))
    return apps


def pick_tracker_file() -> Path:
    """File dialog if tkinter is available, otherwise ask for a path."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askopenfilename(
            title="Select job applications tracker",
            filetypes=[("Tracker files", "*.md *.csv *.tsv *.json"), ("All files", "*.*")],
        )
        root.destroy()
        if chosen and Path(chosen).is_file():
            return Path(chosen).resolve()
    except Exception:
        pass

    answer = input("\nPath to your tracker file (.md/.csv/.tsv/.json): ").strip().strip("\"'")
    if answer and Path(answer).is_file():
        return Path(answer).resolve()
    raise TrackerError("No valid tracker file provided.")


def find_tracker(arg_path: str | None) -> Path:
    """Explicit path first, then the usual places, then the remembered path, then a picker."""
    cfg = load_config()

    def remember(p: Path) -> Path:
        if cfg.get("last_tracker_path") != str(p):
            cfg["last_tracker_path"] = str(p)
            save_config(cfg)
        return p

    if arg_path:
        p = Path(arg_path.strip().strip("\"'")).resolve()
        if not p.is_file():
            raise TrackerError(f"Tracker file not found: {p}")
        return remember(p)

    candidates = [
        os.environ.get("CAREER_OPS_TRACKER"),
        PROJECT_ROOT / "applications.md",
        PROJECT_ROOT / "data" / "applications.md",
        Path.cwd() / "applications.md",
        Path.cwd() / "data" / "applications.md",
        cfg.get("last_tracker_path"),
        PROJECT_ROOT.parent / "career-ops" / "data" / "applications.md",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return remember(Path(candidate).resolve())
    return remember(pick_tracker_file())
