"""content/account.js against a fixture of ATS auth walls. Browser only — no backend.

Contract: detect Create Account / Sign In / OTP pages, fill ONLY empty fields
with the email + ATS password handed in, fill a code into single or segmented
boxes, and never click Create/Sign In/Verify or tick an agreement box.
"""

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
EXT = _ROOT / "browser-extension"
FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "auth_pages.html").as_uri()


def _open(page, mode):
    page.goto(f"{FIXTURE}?mode={mode}", wait_until="domcontentloaded")
    for f in ("scan.js", "fill.js", "account.js"):
        page.add_script_tag(path=str(EXT / "content" / f))
    return page


def test_scan_never_emits_password_fields(page):
    _open(page, "create")
    types = page.evaluate("window.__jpafScan().map(f => f.type)")
    assert "password" not in types


@pytest.mark.parametrize("mode,kind", [("create", "create"), ("signin", "signin"),
                                       ("otp-seg", "otp"), ("otp-one", "otp")])
def test_detects_each_wall(page, mode, kind):
    _open(page, mode)
    assert page.evaluate("window.__jpafAuthState().kind") == kind


def test_fill_account_only_touches_blank_fields_and_never_clicks(page):
    _open(page, "create")
    page.fill("#c_email", "typed.by.hand@example.com")
    r = page.evaluate("window.__jpafFillAccount({email: 'me@example.com', password: 'Str0ng!Pass'})")
    assert r["kind"] == "create" and r["email"] is False and r["password"] == 2
    assert page.input_value("#c_email") == "typed.by.hand@example.com"
    assert page.input_value("#c_pw") == "Str0ng!Pass" and page.input_value("#c_pw2") == "Str0ng!Pass"
    assert page.is_checked("#c_terms") is False
    assert page.evaluate("window.__clicked") is False


def test_fill_signin_fills_email_when_blank(page):
    _open(page, "signin")
    r = page.evaluate("window.__jpafFillAccount({email: 'me@example.com', password: 'Str0ng!Pass'})")
    assert r["kind"] == "signin" and r["email"] is True and r["password"] == 1
    assert page.input_value("#s_email") == "me@example.com"
    assert page.evaluate("window.__clicked") is False


def test_otp_segmented_boxes(page):
    _open(page, "otp-seg")
    r = page.evaluate("window.__jpafFillOtp('482913')")
    assert r["ok"] and r["boxes"] == 6
    assert page.eval_on_selector_all(".boxes input", "els => els.map(e => e.value).join('')") == "482913"
    assert page.evaluate("window.__clicked") is False


def test_otp_single_box_and_no_overwrite(page):
    _open(page, "otp-one")
    assert page.evaluate("window.__jpafFillOtp('112233')")["ok"]
    assert page.input_value("#o_code") == "112233"
    assert page.evaluate("window.__jpafFillOtp('999999')")["reason"] == "already-filled"
    assert page.input_value("#o_code") == "112233"
