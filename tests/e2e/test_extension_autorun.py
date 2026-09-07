"""E2E: "APPLY WITH AUTOFILL" from the dashboard — the armed auto-run, and the
per-field progress events that drive the JobRight-style panel.

Drives the REAL widget.js + scan.js + fill.js on the qa_greenhouse fixture with
a chrome.runtime stub that proxies the service-worker messages to the live
backend (plan / ARMED / ARM_CONSUMED). Proves: an un-armed page shows the pill
and does nothing; after POST /api/autofill/arm for the fixture host the next
load opens the panel with the "Autofilling for …" banner and fills the form
with NO click on the Autofill button, then consumes the arm; and fill.js emits
one jpaf-progress {type:"field", index, total} event per planned field.

Run against a dev instance: JOBPILOT_BASE_URL=http://127.0.0.1:7799
pytest -m live tests/e2e/test_extension_autorun.py
"""

from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.live

_ROOT = Path(__file__).resolve().parents[2]
EXT = _ROOT / "browser-extension"
SCRIPTS = ("scan.js", "fill.js", "widget.js")

# chrome.runtime that behaves like service_worker.js for the messages the
# widget sends during a run, against the same-origin backend; chrome.storage
# in-memory. The fixture is on localhost, which the pill normally skips.
STUB = """
(base) => {
  window.__jpafMsgs = [];
  const j = (p, opt) => fetch(base + p, opt).then((r) => r.ok ? r.json() : null).catch(() => null);
  window.chrome = window.chrome || {};
  window.chrome.runtime = { sendMessage: (msg, cb) => {
    window.__jpafMsgs.push(msg.type || msg.cmd);
    let p;
    if (msg.type === "ARMED")
      p = j("/api/autofill/armed?" + new URLSearchParams({ host: msg.host, url: msg.url })).then((r) => r || {});
    else if (msg.type === "ARM_CONSUMED")
      p = j("/api/autofill/armed?host=" + encodeURIComponent(msg.host), { method: "DELETE" });
    else if (msg.cmd === "plan")
      p = j("/api/autofill/plan", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: msg.ctx.url, job_title: msg.ctx.h1 || msg.ctx.title,
                                   company: msg.ctx.company, page_text: msg.ctx.text,
                                   resume_pref: msg.resumePref || "auto", fields: msg.fields }) });
    else if (msg.cmd === "health") p = Promise.resolve({ ok: true });
    else p = Promise.resolve(null);
    p.then(cb);
    return true;
  } };
  window.chrome.storage = { local: { get: (k, cb) => cb({}), set: () => Promise.resolve() },
                            onChanged: { addListener() {} } };
  window.__jpafForceApp = true;
}
"""


def _load(page, base_url, pre=""):
    page.goto(f"{base_url}/static/qa_greenhouse.html", wait_until="domcontentloaded")
    page.evaluate(STUB, base_url)
    if pre:
        page.evaluate(pre)
    for f in SCRIPTS:
        page.add_script_tag(path=str(EXT / "content" / f))
    page.wait_for_function("() => !!document.getElementById('__jpaf_host')")


def _sh(page, expr):
    """Evaluate `expr` with `r` bound to the pill's shadow root."""
    return page.evaluate(f"() => {{ const r = document.getElementById('__jpaf_host').shadowRoot; return ({expr}); }}")


def _armed(base_url):
    return requests.get(f"{base_url}/api/autofill/armed", params={"host": "127.0.0.1"}, timeout=10).json()


def test_armed_host_autofills_without_a_click(page, base_url):
    requests.delete(f"{base_url}/api/autofill/armed", params={"host": "127.0.0.1"}, timeout=10)

    # not armed: pill only, panel closed, nothing filled
    _load(page, base_url)
    page.wait_for_function("() => window.__jpafMsgs.includes('ARMED')")
    page.wait_for_timeout(500)
    assert _sh(page, "r.getElementById('panel').hidden") is True
    assert page.eval_on_selector("#first_name", "e => e.value") == ""

    # the dashboard's "APPLY WITH AUTOFILL" (fallback form: no app_id)
    rec = requests.post(f"{base_url}/api/autofill/arm",
                        json={"url": f"{base_url}/static/qa_greenhouse.html",
                              "company": "QA Extension Co", "title": "QA Data Analyst"},
                        timeout=10).json()
    assert rec["host"] == "127.0.0.1", rec

    # reload → the widget finds the arm and runs by itself
    _load(page, base_url)
    page.wait_for_function(
        "() => !document.getElementById('__jpaf_host').shadowRoot.getElementById('panel').hidden", timeout=15000)
    assert _sh(page, "r.getElementById('banner').hidden") is False
    assert "QA Data Analyst" in _sh(page, "r.getElementById('banner').textContent")
    assert "QA Extension Co" in _sh(page, "r.getElementById('banner').textContent")

    # fields land with no click on #go (the plan may wait on the local LLM for the essay)
    page.wait_for_function(
        "() => document.getElementById('__jpaf_host').shadowRoot.getElementById('res').innerText.includes('Filled')",
        timeout=180000)
    assert page.eval_on_selector("#first_name", "e => e.value") != ""
    assert page.eval_on_selector("#email", "e => e.value") != ""
    assert _sh(page, "r.getElementById('steps').hidden") is False
    assert "Filled" in _sh(page, "r.getElementById('count').textContent")
    assert _sh(page, "r.getElementById('pfill').style.transform") == "scaleX(1)"

    # the arm is single-use
    page.wait_for_function("() => window.__jpafMsgs.includes('ARM_CONSUMED')")
    page.wait_for_timeout(300)
    assert _armed(base_url) == {}
    assert page.evaluate("() => window.__jpafMsgs.filter((m) => m === 'plan').length") == 1


def test_fill_emits_per_field_progress_events(page, base_url):
    page.goto(f"{base_url}/static/qa_greenhouse.html", wait_until="domcontentloaded")
    page.evaluate("""() => {
      window.__evts = [];
      window.addEventListener("jpaf-progress", (e) => { if (e.detail && e.detail.type === "field") window.__evts.push(e.detail); });
    }""")
    page.add_script_tag(path=str(EXT / "content" / "scan.js"))
    fields = page.evaluate("window.__jpafScan()")
    assert len(fields) >= 5
    plan = requests.post(f"{base_url}/api/autofill/plan",
                         json={"url": "http://x", "job_title": "Data Analyst", "company": "",
                               "page_text": "", "resume_pref": "ai", "fields": fields},
                         timeout=180).json()
    plan["_scanMeta"] = fields
    plan["_ctx"] = {"company": "", "title": "Data Analyst"}
    page.add_script_tag(path=str(EXT / "content" / "fill.js"))
    stats = page.evaluate("(p) => window.__jpafApply(p)", plan)
    assert stats["filled"] >= 3

    evts = page.evaluate("() => window.__evts")
    total = len(plan["fields"])
    assert len(evts) == total, (len(evts), total)
    assert [e["index"] for e in evts] == list(range(total))
    assert {e["total"] for e in evts} == {total}
    assert all("label" in e and "ok" in e for e in evts)
    assert sum(1 for e in evts if e["ok"]) == stats["filled"]
    # a filled field carries the verified outline (the mint flash has faded by now)
    assert page.eval_on_selector("#first_name", "e => e.getAttribute('data-jpaf-state')") == "verified"
