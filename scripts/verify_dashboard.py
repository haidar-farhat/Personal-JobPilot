"""Headless render check for the dashboard — captures console errors + a screenshot.

Loads the live dashboard, waits for the data to load, records any console
errors/page exceptions, and saves a full-page screenshot to logs/.
Run: venv/Scripts/python.exe scripts/verify_dashboard.py
"""

import sys
from pathlib import Path

URL = "http://127.0.0.1:7777/"
OUT = Path(__file__).parent.parent / "logs" / "dashboard_preview.png"

try:
    from playwright.sync_api import sync_playwright
except Exception as e:
    print("PLAYWRIGHT_MISSING", e)
    sys.exit(2)

errors = []
with sync_playwright() as p:
    try:
        browser = p.chromium.launch()
    except Exception as e:
        print("CHROMIUM_MISSING", e)
        sys.exit(3)
    page = browser.new_page(viewport={"width": 1512, "height": 982})
    page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type in ("error",) else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    # SSE keeps the connection open, so networkidle never fires — use 'load'.
    page.goto(URL, wait_until="load", timeout=30000)
    page.wait_for_timeout(4000)  # let data fetch + charts animate/render
    # Pull a few signals out of the rendered DOM
    title = page.title()
    svg_count = page.eval_on_selector_all("svg", "els => els.length")
    insights = page.eval_on_selector_all("text=Insights", "els => els.length") if False else None
    body_text_has_insights = "insights" in page.content().lower()
    OUT.parent.mkdir(exist_ok=True)
    page.screenshot(path=str(OUT), full_page=True)
    page.screenshot(path=str(OUT.parent / "dashboard_preview_fold.png"), full_page=False)
    browser.close()

print("TITLE:", title)
print("SVG_ELEMENTS:", svg_count)
print("INSIGHTS_PRESENT:", body_text_has_insights)
print("CONSOLE_ERRORS:", len(errors))
for e in errors[:20]:
    print("  ", e)
print("SCREENSHOT:", OUT)
