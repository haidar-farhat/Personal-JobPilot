"""E2E: the autofill kill switch — pill hides everywhere, re-enables live.

Drives the REAL widget.js on the qa_greenhouse fixture with a stubbed
chrome.storage. Proves: the pill appears; "Turn off autofill (all sites)"
removes it and persists the flag; while off the SPA watchdogs (MutationObserver
+ 2s interval) never resurrect it; flipping the flag back (what the toolbar
popup toggle does) rebuilds the pill without a refresh; and a page that loads
with the flag already set never shows the pill at all.

Requires the JobPilot dashboard running at http://127.0.0.1:7777.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_ROOT = Path(__file__).resolve().parents[2]
EXT = _ROOT / "browser-extension"
BACKEND = "http://127.0.0.1:7777"
FIXTURE = f"{BACKEND}/static/qa_greenhouse.html"

# chrome.runtime that answers nothing (widget degrades to "backend offline")
# plus an in-memory chrome.storage.local that fires onChanged like the real one.
CHROME_STUB = """
window.chrome = window.chrome || {};
window.chrome.runtime = { sendMessage: (msg, cb) => { cb(null); return true; } };
window.__jpafStore = {};
const __jpafListeners = [];
window.chrome.storage = {
  local: {
    get: (k, cb) => cb({ [k]: window.__jpafStore[k] }),
    set: (obj) => {
      const ch = {};
      for (const k of Object.keys(obj)) {
        ch[k] = { oldValue: window.__jpafStore[k], newValue: obj[k] };
        window.__jpafStore[k] = obj[k];
      }
      __jpafListeners.forEach((f) => f(ch, "local"));
      return Promise.resolve();
    },
  },
  onChanged: { addListener: (f) => __jpafListeners.push(f) },
};
window.__jpafForceApp = true;  // fixture serves from localhost, which the pill normally skips
"""

SCRIPTS = ("scan.js", "fill.js", "widget.js")


def _host(page):
    return page.evaluate("() => !!document.getElementById('__jpaf_host')")


def test_kill_switch_hides_and_reenables_live(page):
    page.goto(FIXTURE, wait_until="domcontentloaded")
    page.evaluate(CHROME_STUB)
    for f in SCRIPTS:
        page.add_script_tag(path=str(EXT / "content" / f))
    assert _host(page) is True  # pill up on an application-looking page

    # "Turn off autofill (all sites)" from the pill's own panel
    page.evaluate(
        "() => document.getElementById('__jpaf_host').shadowRoot.getElementById('off').click()")
    assert _host(page) is False
    assert page.evaluate("() => window.__jpafStore['jpaf_disabled']") is True

    # the SPA watchdogs (2s interval + MutationObserver) must NOT resurrect it
    page.wait_for_timeout(2600)
    assert _host(page) is False

    # popup-toggle equivalent: flag flips back → pill rebuilds, no refresh
    page.evaluate("() => chrome.storage.local.set({ jpaf_disabled: false })")
    assert _host(page) is True


def test_pill_never_appears_when_loaded_disabled(page):
    page.goto(FIXTURE, wait_until="domcontentloaded")
    page.evaluate(CHROME_STUB)
    page.evaluate("() => { window.__jpafStore['jpaf_disabled'] = true; }")
    for f in SCRIPTS:
        page.add_script_tag(path=str(EXT / "content" / f))
    page.wait_for_timeout(2600)  # outlive the first periodic maybeShow tick
    assert _host(page) is False
