"""Quick autofill verification — scans the sample-forms fixture, asks the live
backend for a plan, applies it, and prints a one-line QA summary.

Usage: set PYTHONPATH to the project root is NOT required (no agents import).
  venv\\Scripts\\python scripts\\verify_autofill.py
Requires the JobPilot dashboard running at http://127.0.0.1:7777.
"""

from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "browser-extension"
FIXTURE = (ROOT / "tests" / "e2e" / "fixtures" / "sample_forms.html").as_uri()
BACKEND = "http://127.0.0.1:7777"


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        pg = browser.new_page()
        pg.goto(FIXTURE)
        pg.add_script_tag(path=str(EXT / "content" / "scan.js"))
        fields = pg.evaluate("window.__jpafScan()")

        r = httpx.post(f"{BACKEND}/api/autofill/plan",
                       json={"url": "file://fixture", "job_title": "AI Engineer",
                             "resume_pref": "auto", "fields": fields}, timeout=180)
        plan = r.json()

        pg.add_script_tag(path=str(EXT / "content" / "fill.js"))
        stats = pg.evaluate("(p) => window.__jpafApply(p)", plan)
        submitted = pg.evaluate("window.__submitted === true")
        browser.close()

        print(f"[Autofill] | Fields: {len(fields)} | Filled: {stats.get('filled', 0)} | "
              f"Needs review: {stats.get('needs_review', 0)} | Résumé: {plan.get('resume_used')} | "
              f"Submitted: {'YES' if submitted else 'NO'}")


if __name__ == "__main__":
    main()
