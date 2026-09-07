"""JobRight-parity QA screenshots (2026-09-07). Run against a dev instance:

    JOBPILOT_BASE_URL=http://127.0.0.1:7799 venv/Scripts/python.exe tests/qa-screenshots/jr_shots.py

Writes jr-*.png next to this file and prints any console/page errors.
"""
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("JOBPILOT_BASE_URL", "http://127.0.0.1:7799")
OUT = Path(__file__).parent
errors = []


def shot(page, name, full=False):
    page.wait_for_timeout(350)
    page.screenshot(path=str(OUT / f"jr-{name}.png"), full_page=full)
    print("saved", name)


with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1440, "height": 900})
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_selector(".card", timeout=20000)
    shot(page, "01-list")
    page.hover(".card >> nth=0")
    shot(page, "02-card-hover")
    page.click('[data-chip="roles"]')
    shot(page, "03-chip-dropdown")
    page.keyboard.press("Escape")
    page.click("#filterBtn")
    shot(page, "04-all-filters-drawer")
    page.keyboard.press("Escape")
    page.locator(".card").first.click()
    page.wait_for_selector(".d-head h2", timeout=5000)
    shot(page, "05-detail")
    shot(page, "05b-detail-full", full=True)
    page.click('[data-dvtab="company"]')
    page.wait_for_timeout(600)
    shot(page, "06-detail-company")
    page.click('[data-dvtab="overview"]')
    page.click("#tailorBtn")
    page.wait_for_selector("#wizard:not([hidden])")
    page.wait_for_timeout(800)
    shot(page, "07-wizard-step1")
    page.click("#wzClose")
    page.click("#detail [data-ask]")
    page.wait_for_selector("#copilot:not([hidden])")
    shot(page, "08-copilot")
    page.click('#cpQuick [data-q="tips"]')
    page.wait_for_timeout(500)
    shot(page, "08b-copilot-tips")
    page.click("#cpClose")
    page.click("#fitBtn")
    page.wait_for_timeout(800)
    shot(page, "09-detail-fit")
    page.click('.topnav a[data-view="applied"]')
    page.wait_for_selector("#appliedList .ap-row", timeout=5000)
    shot(page, "10-applied")
    page.click('.topnav a[data-view="resume"]')
    page.wait_for_selector("#resumeBody table", timeout=10000)
    shot(page, "11-resume")
    page.click('.topnav a[data-view="profile"]')
    page.wait_for_selector("#profileBody .pf-sec", timeout=10000)
    shot(page, "12-profile", full=True)
    page.click('.topnav a[data-view="agent"]')
    page.wait_for_selector("#agentBody .ag-status", timeout=10000)
    shot(page, "13-agent")
    page.click('.topnav a[data-view="jobs"]')
    page.wait_for_selector(".card", timeout=5000)
    page.click("#themeToggle")
    shot(page, "14-dark")
    page.click("#themeToggle")
    page.set_viewport_size({"width": 900, "height": 900})
    shot(page, "15-narrow")
    b.close()

print("\n".join(errors) if errors else "no console/page errors")
sys.exit(1 if errors else 0)
