"""E2E: the in-page pill's account-wall + verification-code flow.

Drives the REAL widget.js + account.js on the auth_pages fixture with a
stubbed chrome.runtime (canned service-worker answers) — no backend needed.
Proves: the wall buttons appear only where they apply; "Fill account" refuses
without an ATS password, then fills email + both password boxes and records
the start; a code box auto-fetches the code once and fills it; and with Gmail
unconnected it explains instead of guessing. Never clicks, never ticks terms.
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
EXT = _ROOT / "browser-extension"
FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "auth_pages.html").as_uri()

STUB = """
window.chrome = window.chrome || {};
window.__jpafMsgs = [];
window.chrome.runtime = { sendMessage: (msg, cb) => {
  window.__jpafMsgs.push(msg.cmd);
  const answers = {
    health: { ok: true }, resume_meta: {}, profile: {},
    account_creds: { email: "me@example.com", password: window.__jpafPw || "", hasPassword: !!window.__jpafPw },
    account_started: { ok: true },
    account_since: { host: location.hostname, ts: Date.now() - 1000 },
    gmail_code: window.__jpafGmail || { ok: false, connected: false, reason: "Gmail not set up" },
    gmail_health: { connected: false },
  };
  setTimeout(() => cb(msg.cmd in answers ? answers[msg.cmd] : null), 0);
  return true;
} };
window.chrome.storage = { local: { get: (k, cb) => cb({}), set: () => Promise.resolve() },
                          onChanged: { addListener() {} } };
window.__jpafForceApp = true;
"""
SCRIPTS = ("scan.js", "fill.js", "account.js", "widget.js")


def _load(page, mode, pre=""):
    page.goto(f"{FIXTURE}?mode={mode}", wait_until="domcontentloaded")
    page.evaluate(STUB)
    if pre:
        page.evaluate(pre)
    for f in SCRIPTS:
        page.add_script_tag(path=str(EXT / "content" / f))
    page.wait_for_function("() => !!document.getElementById('__jpaf_host')")


def _sh(page, expr):
    """Evaluate `expr` with `r` bound to the pill's shadow root."""
    return page.evaluate(f"() => {{ const r = document.getElementById('__jpaf_host').shadowRoot; return ({expr}); }}")


def _res_has(page, text):
    page.wait_for_function(
        "t => document.getElementById('__jpaf_host').shadowRoot.getElementById('res').innerText.includes(t)",
        arg=text, timeout=8000)


def test_wall_buttons_only_where_they_apply(page):
    _load(page, "create")
    assert _sh(page, "r.getElementById('acct').hidden") is False
    assert _sh(page, "r.getElementById('otp').hidden") is True
    _load(page, "otp-one")
    assert _sh(page, "r.getElementById('acct').hidden") is True
    assert _sh(page, "r.getElementById('otp').hidden") is False


def test_fill_account_refuses_without_an_ats_password(page):
    _load(page, "create")
    _sh(page, "r.getElementById('acct').click()")
    _res_has(page, "ATS password")
    assert page.input_value("#c_pw") == ""
    assert "account_started" not in page.evaluate("window.__jpafMsgs")


def test_fill_account_fills_blanks_records_start_never_clicks(page):
    _load(page, "create")
    page.evaluate("() => { window.__jpafPw = 'Str0ng!Pass'; }")
    _sh(page, "r.getElementById('acct').click()")
    page.wait_for_function("() => document.querySelector('#c_pw').value === 'Str0ng!Pass'")
    assert page.input_value("#c_email") == "me@example.com"
    assert page.input_value("#c_pw2") == "Str0ng!Pass"
    assert page.is_checked("#c_terms") is False
    assert page.evaluate("window.__clicked") is False
    assert "account_started" in page.evaluate("window.__jpafMsgs")
    _res_has(page, "Create Account")


def test_code_box_auto_fetches_once_and_fills(page):
    _load(page, "otp-seg",
          pre="window.__jpafGmail = { ok: true, connected: true, found: true, code: '482913', subject: 'Your Workday code' }")
    page.wait_for_function(
        "() => [...document.querySelectorAll('.boxes input')].map(e => e.value).join('') === '482913'")
    _res_has(page, "482913")
    assert page.evaluate("window.__clicked") is False
    page.wait_for_timeout(2500)   # two watchdog ticks — must not fetch again
    assert page.evaluate("window.__jpafMsgs.filter(c => c === 'gmail_code').length") == 1


def test_code_box_without_gmail_explains_instead_of_guessing(page):
    _load(page, "otp-one")
    _res_has(page, "Gmail not connected")
    assert page.input_value("#o_code") == ""
