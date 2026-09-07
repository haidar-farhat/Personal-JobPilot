/* JobPilot Autofill — account-creation + verification-code assist.
 *
 * ATS login walls (Workday, iCIMS, SmartRecruiters…) used to end an autofill
 * run silently. This fills the user's email + the ONE ATS password they chose
 * in the toolbar popup into Create Account / Sign In forms, and fills the code
 * Gmail received into the OTP box. It never clicks Create Account / Sign In /
 * Verify, never ticks "I agree", and only ever writes into EMPTY fields.
 *
 * Same isolated world as fill.js (loaded after it) — reuses its helpers.
 */
(() => {
  if (window.__jpafAccountLoaded) return;
  window.__jpafAccountLoaded = true;

  const txt = (s) => (s || "").replace(/\s+/g, " ").trim();
  const vis = (el) => !el.disabled && el.getClientRects().length > 0;
  const OTP_RE = /verification code|security code|confirmation code|authentication code|one[- ]?time|passcode|\botp\b|enter (the |your )?(\d[- ]?digit )?code|code (we|that was) (sent|emailed)|\b(sms|text) code\b|\bpin\b/i;
  const CREATE_RE = /create (an |your |new )?account|sign ?up|register|new user|get started|join now/i;
  const SIGNIN_RE = /sign ?in|log ?in|login/i;
  const EMAIL_RE = /e-?mail|username|user ?name|user id|login id/i;
  const NOT_TEXT = /password|hidden|submit|button|checkbox|radio|file/i;

  function labelText(el) {
    const bits = [el.getAttribute("aria-label"), el.placeholder, el.name, el.id, el.getAttribute("autocomplete")];
    const by = el.getAttribute("aria-labelledby");
    if (by) by.split(/\s+/).forEach((id) => { const n = document.getElementById(id); if (n) bits.push(n.innerText); });
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) bits.push(l.innerText); }
    const wrap = el.closest("label");
    if (wrap) bits.push(wrap.innerText);
    const prev = el.previousElementSibling;
    if (prev && /^(label|span|div|p)$/i.test(prev.tagName)) bits.push(prev.innerText);
    return txt(bits.filter(Boolean).join(" "));
  }
  const inputs = () => [...document.querySelectorAll("input")].filter(vis);
  const passwordFields = () => inputs().filter((el) => (el.type || "").toLowerCase() === "password");

  function emailField() {
    const cands = inputs().filter((el) => !NOT_TEXT.test(el.type || ""));
    return cands.find((el) => (el.type || "").toLowerCase() === "email" ||
                              ["email", "username"].includes(el.getAttribute("autocomplete") || "")) ||
           cands.find((el) => EMAIL_RE.test(labelText(el))) || null;
  }

  function otpFields() {
    const cands = inputs().filter((el) => !NOT_TEXT.test(el.type || "") && (el.type || "") !== "email");
    // Segmented boxes: 4–8 single-character inputs sharing a parent or grandparent.
    const boxes = cands.filter((el) => el.maxLength === 1);
    if (boxes.length >= 4 && boxes.length <= 8) {
      const par = new Set(boxes.map((b) => b.parentElement));
      const gp = new Set(boxes.map((b) => b.parentElement && b.parentElement.parentElement));
      if (par.size === 1 || gp.size === 1) return boxes;
    }
    return cands.filter((el) => el.getAttribute("autocomplete") === "one-time-code" || OTP_RE.test(labelText(el)));
  }

  function buttonText() {
    return [...document.querySelectorAll("button, input[type='submit'], [role='button']")]
      .filter(vis).map((b) => txt(b.innerText || b.value || b.getAttribute("aria-label"))).join(" | ");
  }

  window.__jpafAuthState = function () {
    const pw = passwordFields(), otp = otpFields(), em = emailField();
    const btns = buttonText();
    let kind = null;
    if (otp.length && !pw.length) kind = "otp";
    else if (pw.length >= 2) kind = "create";
    else if (pw.length === 1) kind = (CREATE_RE.test(btns) && !SIGNIN_RE.test(btns)) ? "create" : "signin";
    return { kind, passwordFields: pw.length, otpFields: otp.length,
             emailPresent: !!em, emailFilled: !!(em && em.value.trim()) };
  };

  // Only EMPTY fields are written — an address the user typed stays.
  window.__jpafFillAccount = function (creds) {
    const H = window.__jpafHelpers || {};
    if (!H.setNative) return { error: "fill helpers not loaded" };
    creds = creds || {};
    const out = { email: false, password: 0, kind: window.__jpafAuthState().kind };
    const em = emailField();
    if (em && creds.email && !em.value.trim()) { H.setNative(em, creds.email); H.mark(em, true); out.email = true; }
    if (creds.password) {
      for (const p of passwordFields()) {
        if (!p.value) { H.setNative(p, creds.password); H.mark(p, true); out.password++; }
      }
    }
    return out;
  };

  window.__jpafFillOtp = function (code) {
    const H = window.__jpafHelpers || {};
    const f = otpFields();
    code = String(code || "").trim();
    if (!H.setNative) return { ok: false, reason: "fill helpers not loaded" };
    if (!f.length) return { ok: false, reason: "no-otp-field" };
    if (!code) return { ok: false, reason: "no-code" };
    if (f.length === 1) {
      if (f[0].value.trim()) return { ok: false, reason: "already-filled" };
      H.setNative(f[0], code);
      const ok = f[0].value === code;
      H.mark(f[0], ok);
      return { ok, boxes: 1 };
    }
    const chars = code.split("");
    f.forEach((box, i) => { if (chars[i] != null && !box.value) { box.focus(); H.setNative(box, chars[i]); } });
    const ok = f.every((b, i) => chars[i] == null || b.value === chars[i]);
    f.forEach((b) => H.mark(b, ok));
    return { ok, boxes: f.length };
  };
})();
