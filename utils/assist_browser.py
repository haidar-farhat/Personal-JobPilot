"""Open a job application in a dedicated Chromium with the JobPilot extension.

Why this exists: "Continue application" used to call webbrowser.open(), which
hands the URL to the OS default browser. That browser is frequently not the one
the extension is installed in — on this machine the default is Firefox, where a
Chrome MV3 extension cannot run at all — so the user got a blank form and no
autofill whatsoever.

Instead we launch Playwright's bundled Chromium with the unpacked extension at
browser-extension/ loaded, in its own profile. Verified: the extension's
service_worker.js registers under --load-extension, so the arm flow works
exactly as it does in a hand-installed browser.

The process is deliberately DETACHED via subprocess.Popen rather than driven by
Playwright: a Playwright context dies with the script that created it, and this
window has to outlive the HTTP request and stay open until the human submits.
Launching the same --user-data-dir again re-uses the running window and opens a
new tab, so clicking Continue on several jobs does not litter the desktop.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = ROOT / "browser-extension"
PROFILE_DIR = ROOT / "output" / "assist-profile"   # output/ is gitignored


def chromium_path() -> str | None:
    """Path to Playwright's Chromium, or None when it isn't installed."""
    override = (os.environ.get("JOBPILOT_CHROMIUM") or "").strip()
    if override and os.path.exists(override):
        return override

    # Scan the browser cache directly. Deliberately NOT via sync_playwright():
    # that spins up the driver just to read a string, and the sync API raises
    # outright if it is ever reached from inside an asyncio loop.
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
        pattern, name = "chromium-*/chrome-win64", "chrome.exe"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "ms-playwright"
        pattern, name = "chromium-*/chrome-mac", "Chromium.app/Contents/MacOS/Chromium"
    else:
        base = Path.home() / ".cache" / "ms-playwright"
        pattern, name = "chromium-*/chrome-linux", "chrome"

    if base.exists():
        # highest build number wins — that is the most recently installed
        for d in sorted(base.glob(pattern), reverse=True):
            exe = d / name
            if exe.exists():
                return str(exe)

    try:                                   # last resort: ask Playwright itself
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
            if exe and os.path.exists(exe):
                return exe
    except Exception as e:
        logger.debug(f"[assist] playwright path lookup failed: {e}")
    return None


def is_available() -> bool:
    return chromium_path() is not None


def open_assisted(url: str) -> dict:
    """Open `url` in the assist browser. Returns {ok, pid|error}.

    Never raises — a browser problem must not fail the caller's request.
    """
    if not url:
        return {"ok": False, "error": "no URL for this job"}

    exe = chromium_path()
    if not exe:
        return {"ok": False,
                "error": "Chromium is not installed — run: "
                         "venv\\Scripts\\python.exe -m playwright install chromium"}
    if not (EXTENSION_DIR / "manifest.json").exists():
        return {"ok": False, "error": f"extension not found at {EXTENSION_DIR}"}

    try:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "error": f"could not create browser profile: {e}"}

    ext = str(EXTENSION_DIR)
    args = [
        exe,
        f"--user-data-dir={PROFILE_DIR}",
        f"--disable-extensions-except={ext}",
        f"--load-extension={ext}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=ChromeWhatsNewUI",
        # Playwright's Chromium otherwise advertises itself as automation, which
        # some ATS front-ends treat differently.
        "--disable-blink-features=AutomationControlled",
        url,
    ]

    try:
        creationflags = 0
        if sys.platform == "win32":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — the window must not
            # die when the dashboard restarts or this request ends.
            creationflags = 0x00000008 | 0x00000200
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            start_new_session=(sys.platform != "win32"),
        )
        logger.info(f"[assist] opened {url} in the JobPilot browser (pid={proc.pid})")
        return {"ok": True, "pid": proc.pid, "url": url}
    except Exception as e:
        logger.error(f"[assist] could not launch Chromium: {e}")
        return {"ok": False, "error": str(e)}
