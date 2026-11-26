"""Paths, URLs and lookup tables."""
import os
import re
from pathlib import Path

from .models import Target

PORTAL_URL = "https://www.universal-credit.service.gov.uk"
LIST_URL = f"{PORTAL_URL}/work-search"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("UC_SYNC_DATA_DIR", PROJECT_ROOT / ".uc-data"))
LOG_FILE = DATA_DIR / "uc_debug.log"
LEDGER_FILE = DATA_DIR / "uc_ledger.json"
CONFIG_FILE = DATA_DIR / "uc_config.json"

# /work-search, /work-search/ and /work-search?page=2 all count as the list page
LIST_PATH_RE = re.compile(r"/work-search/?(\?.*)?$")

# Pages you land on straight after signing in
POST_LOGIN_RE = re.compile(
    r"https://www\.universal-credit\.service\.gov\.uk/"
    r"(to-do|dashboard|journal|work-search-commitments|home)(/.*|\?.*)?$"
)

NOTE_LIMIT = 3000  # the portal's character limit for the notes box

COLUMN_ALIASES = {
    "number": ["#", "id", "no", "no.", "number", "row", "index", "ref"],
    "date": ["date", "date applied", "applied date", "application date", "applied on", "submitted", "applied"],
    "company": ["company", "employer", "employer name", "company name", "organisation", "organization", "firm"],
    "role": ["role", "job title", "title", "position", "job", "role title", "vacancy"],
    "status": ["status", "stage", "outcome", "state", "result", "application status", "uc status"],
    "notes": ["notes", "note", "comments", "comment", "details"],
    "via": ["via", "source", "channel", "platform", "board"],
}

STATUS_MAP = {
    "applied": Target.APPLIED,
    "submitted": Target.APPLIED,
    "sent": Target.APPLIED,
    "in progress": Target.APPLIED,
    "awaiting": Target.APPLIED,
    "awaiting response": Target.APPLIED,
    "interview": Target.INTERVIEW,
    "interviewing": Target.INTERVIEW,
    "going for an interview": Target.INTERVIEW,
    "phone screen": Target.INTERVIEW,
    "screening": Target.INTERVIEW,
    "technical test": Target.INTERVIEW,
    "oa": Target.INTERVIEW,
    "assessment": Target.INTERVIEW,
    "final round": Target.INTERVIEW,
    "offer": Target.SUCCESSFUL,
    "accepted": Target.SUCCESSFUL,
    "successful": Target.SUCCESSFUL,
    "hired": Target.SUCCESSFUL,
    "rejected": Target.UNSUCCESSFUL,
    "unsuccessful": Target.UNSUCCESSFUL,
    "declined": Target.UNSUCCESSFUL,
    "ghosted": Target.UNSUCCESSFUL,
    "closed": Target.UNSUCCESSFUL,
    "interested": Target.INTERESTED,
    "saved": Target.INTERESTED,
    "bookmarked": Target.INTERESTED,
    "evaluated": Target.SKIP,
    "discarded": Target.SKIP,
    "skipped": Target.SKIP,
    "pending": Target.SKIP,
    "draft": Target.SKIP,
    "withdrawn": Target.SKIP,
}

# Fallback for statuses not listed above. Order matters: "unsuccessful" contains "success".
STATUS_KEYWORDS = [
    (("reject", "unsuccessful", "declin", "ghost"), Target.UNSUCCESSFUL),
    (("interview", "screen", "test", "assessment"), Target.INTERVIEW),
    (("offer", "success", "hired", "accept"), Target.SUCCESSFUL),
    (("eval", "discard", "skip", "draft", "withdraw"), Target.SKIP),
    (("interest", "save", "bookmark"), Target.INTERESTED),
    (("appl", "submit", "sent"), Target.APPLIED),
]
