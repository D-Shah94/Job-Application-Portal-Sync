"""Starting the browser."""
import logging
import tempfile
from pathlib import Path

from .config import DATA_DIR, LIST_URL, PROJECT_ROOT

log = logging.getLogger("worksearch_sync")


def _resolve_profile(name: str) -> str:
    if name.lower() in ("none", "temp", ""):
        return tempfile.mkdtemp(prefix="uc_tmp_profile_")  # throw-away, so you sign in each run
    path = Path(name)
    if not path.is_absolute():
        legacy = PROJECT_ROOT / path  # profile folders created by earlier versions
        path = legacy if legacy.exists() else DATA_DIR / path
    return str(path)


def _watch_page(page) -> None:
    log.info("new tab")
    page.on("framenavigated", lambda frame: frame == page.main_frame and log.info("NAV  %s", frame.url))

    def on_response(resp):
        try:
            if resp.request.resource_type != "document":
                return
            location = resp.headers.get("location", "")
            log.info("DOC %s  %s%s", resp.status, resp.url, f"  -> {location}" if location else "")
            if "ejected" in resp.url or "ejected" in location:
                log.warning("portal redirected to 'ejected' - see the lines above")
        except Exception:
            pass

    page.on("response", on_response)
    page.on("close", lambda: log.info("tab closed"))


def open_context(playwright, *, browser: str, profile: str, cdp: str | None, slow_mo: int):
    """Return (context, first page). Logs navigation so odd redirects can be diagnosed later."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if cdp:
        ctx = playwright.chromium.connect_over_cdp(cdp).contexts[0]
    else:
        engine = playwright.firefox if browser == "firefox" else playwright.chromium
        options = {"headless": False, "slow_mo": slow_mo}
        if browser == "chrome":
            options["channel"] = "chrome"
        ctx = engine.launch_persistent_context(_resolve_profile(profile), **options)

    for existing in ctx.pages:
        _watch_page(existing)
    ctx.on("page", _watch_page)

    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    if not cdp:
        page.goto(LIST_URL)
    return ctx, page
