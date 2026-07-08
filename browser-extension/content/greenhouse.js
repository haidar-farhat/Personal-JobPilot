/* JobPilot Autofill — Greenhouse engine.
 *
 * Greenhouse job-boards forms hold the pieces the flat scan/plan/fill path
 * can't express: a multi-entry Education section whose School / Degree /
 * Discipline controls are async React-Select comboboxes, plus an
 * "Add another education" flow. This engine drives those directly from the
 * applicant profile's education list. Location, EEO, and the résumé attach
 * ride the flat path (fill.js).
 *
 * It NEVER submits. Every entry it manages is tagged data-jpaf-owned /
 * data-jpaf-done so the flat pass that follows won't double-fill.
 *
 * Progress is broadcast as CustomEvent("jpaf-progress") for the widget's
 * checklist, mirroring workday.js.
 */
(() => {
  if (window.__jpafGreenhouseLoaded) return;
  window.__jpafGreenhouseLoaded = true;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const norm = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const txt = (s) => (s || "").replace(/\s+/g, " ").trim();

  function progress(section, status, note) {
    try {
      window.dispatchEvent(new CustomEvent("jpaf-progress", { detail: { section, status, note: note || "" } }));
    } catch (e) { /* display only */ }
  }

  async function waitFor(fn, timeout = 4000, step = 200) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      const v = fn();
      if (v) return v;
      await sleep(step);
    }
    return null;
  }

  function realClick(el) {
    for (const t of ["pointerdown", "mousedown", "pointerup", "mouseup"])
      el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, composed: true, view: window }));
    if (typeof el.click === "function") el.click();
  }

  function setNative(el, value) {
    const proto = el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function done(el) { if (el) el.setAttribute("data-jpaf-done", "1"); }

  function visibleOptions() {
    return [...document.querySelectorAll("[role='option'], li[role='menuitem']")]
      .filter((o) => o.getClientRects().length > 0);
  }

  // Token-overlap option matcher (subset of fill.js chooseChoice — education
  // values here are long proper nouns, so overlap is the signal that matters).
  function bestOption(value) {
    const nv = norm(value);
    const opts = visibleOptions().map((el) => ({ el, t: norm(el.innerText) }));
    let o = opts.find((x) => x.t === nv);
    if (o) return o.el;
    o = opts.find((x) => x.t && (x.t.includes(nv) || nv.includes(x.t)));
    if (o) return o.el;
    const dw = nv.split(" ").filter((w) => w.length > 2);
    let best = null, bn = 0;
    for (const x of opts) {
      const ow = new Set(x.t.split(" "));
      const k = dw.filter((w) => ow.has(w)).length;
      if (k > bn) { best = x.el; bn = k; }
    }
    return bn >= Math.max(1, Math.floor(dw.length / 3)) ? best : null;
  }

  function send(msg) {
    return new Promise((resolve) => {
      try { chrome.runtime.sendMessage(msg, resolve); }
      catch (e) { resolve(null); }
    });
  }

  // Right after a browser start the service worker may not be listening yet —
  // the first sendMessage resolves undefined and a whole section would be
  // skipped ("no data in profile"). Retry briefly before giving up.
  async function sendRetry(msg, ok, tries = 3) {
    for (let i = 0; i < tries; i++) {
      const r = await send(msg);
      if (r && (!ok || ok(r))) return r;
      await sleep(700 * (i + 1));
    }
    return send(msg);
  }

  // ---------------- education section discovery ----------------

  const SCHOOL_SEL = "input[id^='school'], input[name^='school'], select[id^='school'], " +
                     "select[name*='school_name'], input[aria-label^='School' i], input[id*='education_school']";
  const DEGREE_SEL = "input[id^='degree'], select[id^='degree'], select[name*='degree'], input[aria-label^='Degree' i]";
  const DISC_SEL = "input[id^='discipline'], select[id^='discipline'], select[name*='discipline'], " +
                   "input[aria-label^='Discipline' i], input[id*='major'], input[aria-label*='field of study' i]";

  function schoolInputs() {
    return [...document.querySelectorAll(SCHOOL_SEL)].filter((e) => !e.disabled);
  }

  function addButton() {
    // Greenhouse job-boards labels it just "Add another", so scope the loose
    // match to the education container; only the explicit wording may match
    // globally.
    const container = document.querySelector(".education--container") ||
                      (schoolInputs()[0] && schoolInputs()[0].closest("form")) || null;
    const vis = (b) => b.getClientRects().length > 0;
    const global = [...document.querySelectorAll("button, a[role='button'], a")]
      .find((b) => /add another education|add education/i.test(txt(b.innerText)) && vis(b));
    if (global) return global;
    if (!container) return null;
    return [...container.querySelectorAll("button, a[role='button'], a")]
      .find((b) => /^add( another)?$/i.test(txt(b.innerText)) && vis(b)) || null;
  }

  // Smallest ancestor holding this school control AND a degree control but only
  // ONE school control — i.e. the single education entry's wrapper.
  function entryScope(schoolEl) {
    let n = schoolEl.parentElement;
    while (n && n !== document.body) {
      if (n.querySelector(DEGREE_SEL) && n.querySelectorAll(SCHOOL_SEL).length === 1) return n;
      n = n.parentElement;
    }
    return schoolEl.closest("form") || document.body;
  }

  function labelText(el) {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && txt(l.innerText)) return txt(l.innerText);
    }
    const w = el.closest("label");
    if (w && txt(w.innerText)) return txt(w.innerText);
    return txt(el.getAttribute("aria-label")) || el.name || el.id || "";
  }

  // ---------------- fill helpers ----------------

  function fillSelectLike(sel, value) {
    if (!value) return false;
    const nv = norm(value);
    let opt = [...sel.options].find((o) => norm(o.text) === nv || norm(o.value) === nv);
    if (!opt) opt = [...sel.options].find((o) => { const t = norm(o.text); return t && (t.includes(nv) || nv.includes(t)); });
    if (!opt) {  // token overlap for verbose lists
      const dw = nv.split(" ").filter((w) => w.length > 2);
      let bn = 0;
      for (const o of sel.options) {
        const ow = new Set(norm(o.text).split(" "));
        const k = dw.filter((w) => ow.has(w)).length;
        if (k > bn) { opt = o; bn = k; }
      }
      if (!bn) opt = null;
    }
    if (!opt) return false;
    sel.value = opt.value;
    sel.dispatchEvent(new Event("input", { bubbles: true }));
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    done(sel);
    return true;
  }

  // Combobox (React-Select) fill — delegate to fill.js's shared implementation
  // (scoped options, open-before-type, commit verification). Falls back to a
  // local type-and-pick if the helpers aren't loaded.
  function comboAlreadyCommitted(el) {
    const H = window.__jpafHelpers;
    if (H && H.comboCommitted) return H.comboCommitted(el);
    return !!(el.value || "").trim();
  }

  async function typeAndPick(el, value, altValue) {
    if (!el || value == null || value === "") return false;
    if (comboAlreadyCommitted(el)) { done(el); return false; }  // never clobber
    const H = window.__jpafHelpers;
    if (H && H.fillCombo) {
      let ok = await H.fillCombo(el, String(value));
      if (!ok && altValue) ok = await H.fillCombo(el, String(altValue));
      if (ok) done(el);
      return ok;
    }
    // fallback: legacy local implementation
    el.focus();
    realClick(el);
    setNative(el, String(value));
    let opt = await waitFor(() => bestOption(value), 3000, 250);
    if (!opt && altValue) {
      setNative(el, String(altValue));
      opt = await waitFor(() => bestOption(altValue) || bestOption(value), 2500, 250);
    }
    if (opt) { realClick(opt); await sleep(200); done(el); return true; }
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    return false;
  }

  function fillTextPlain(el, value) {
    if (!el || value == null || value === "") return false;
    if ((el.value || "").trim()) { done(el); return false; }  // never clobber
    setNative(el, String(value));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
    done(el);
    return true;
  }

  async function fillControl(scope, selector, value, altValue) {
    const el = scope.querySelector(selector);
    if (!el) return false;
    if (el.tagName === "SELECT") return fillSelectLike(el, value || altValue);
    const isCombo = el.getAttribute("role") === "combobox" ||
                    el.getAttribute("aria-autocomplete") === "list" ||
                    (el.className || "").toString().includes("select__input");
    if (isCombo) return typeAndPick(el, value, altValue);
    return fillTextPlain(el, value);
  }

  const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
                  "August", "September", "October", "November", "December"];

  // Month/year date controls matching `re` inside the entry, in every shape the
  // wild serves: native <select>s, react-select COMBOS (Coinbase: month is a
  // select__input combobox), plain text inputs (Coinbase: year), or one MM/YYYY box.
  async function fillDateControls(scope, re, m, y) {
    let n = 0;
    for (const sel of scope.querySelectorAll("select")) {
      if (sel.getAttribute("data-jpaf-done")) continue;
      const lbl = norm(labelText(sel) + " " + (sel.name || "") + " " + (sel.id || ""));
      if (!re.test(lbl)) continue;
      const optTexts = [...sel.options].map((o) => (o.text || "").trim());
      const isYear = optTexts.some((t) => /^\d{4}$/.test(t));
      if (isYear && y && fillSelectLike(sel, String(y))) n++;
      else if (!isYear && m && fillSelectLike(sel, MONTHS[Number(m) - 1])) n++;
    }
    // month/year inputs — combo (January…December list) or plain text
    for (const el of scope.querySelectorAll("input")) {
      if (el.getAttribute("data-jpaf-done")) continue;
      const lbl = norm(labelText(el) + " " + (el.name || "") + " " + (el.id || ""));
      if (!re.test(lbl)) continue;
      const isCombo = el.getAttribute("role") === "combobox" ||
                      el.getAttribute("aria-autocomplete") === "list" ||
                      (el.className || "").toString().includes("select__input");
      if (/year/.test(lbl) && y) {
        if (isCombo ? await typeAndPick(el, String(y)) : fillTextPlain(el, String(y))) n++;
      } else if (/month/.test(lbl) && m) {
        if (isCombo ? await typeAndPick(el, MONTHS[Number(m) - 1])
                    : fillTextPlain(el, String(m).padStart(2, "0"))) n++;
      }
    }
    // single text input variant (MM/YYYY) — month/year-specific inputs handled above
    const ti = [...scope.querySelectorAll("input")].find((i) => {
      const lbl = norm(labelText(i) + " " + (i.id || ""));
      return re.test(lbl) && /date/.test(lbl) && !/month|year/.test(lbl) &&
             !i.getAttribute("data-jpaf-done") && !(i.value || "").trim() &&
             i.getAttribute("role") !== "combobox";
    });
    if (ti && m && y) {
      setNative(ti, `${String(m).padStart(2, "0")}/${y}`);
      done(ti); n++;
    }
    return n;
  }

  async function fillEntry(scope, e) {
    let filled = 0;
    if (await fillControl(scope, SCHOOL_SEL, e.school)) filled++;
    if (await fillControl(scope, DEGREE_SEL, e.degree, e.degree_full)) filled++;
    if (await fillControl(scope, DISC_SEL, e.discipline, e.field_of_study)) filled++;
    const gpa = scope.querySelector("input[id^='gpa'], input[name*='gpa'], input[aria-label*='gpa' i]");
    if (gpa && e.gpa && !(gpa.value || "").trim()) { setNative(gpa, String(e.gpa)); done(gpa); filled++; }
    filled += await fillDateControls(scope, /end|graduat|complet/, e.end_month, e.end_year);
    return filled;
  }

  // ---------------- employment / work-experience section ----------------

  const COMPANY_SEL = "input[id^='company'], input[name^='company'], input[aria-label^='Company' i], " +
                      "select[id^='company'], input[id*='employment_company']";
  const TITLE_SEL = "input[id^='title'], input[name^='title'], input[aria-label^='Title' i], " +
                    "input[aria-label*='job title' i], input[id*='employment_title']";

  function employmentContainer() {
    const c = document.querySelector(".employment--container");
    if (c) return c;
    const h = [...document.querySelectorAll("h1, h2, h3, h4, p, legend")]
      .find((x) => /^(employment|work experience|work history)$/i.test(txt(x.innerText)) &&
                   x.getClientRects().length);
    let n = h && h.parentElement;
    for (let hops = 0; n && hops < 5; hops++) {
      if (n.querySelector(COMPANY_SEL)) return n;
      n = n.parentElement;
    }
    return null;
  }

  function companyInputs() {
    const c = employmentContainer();
    return c ? [...c.querySelectorAll(COMPANY_SEL)].filter((e) => !e.disabled) : [];
  }

  function addWorkButton() {
    const vis = (b) => b.getClientRects().length > 0;
    const global = [...document.querySelectorAll("button, a[role='button'], a")]
      .find((b) => /add another (employment|position|job|work)|add (employment|position|work experience)/i
        .test(txt(b.innerText)) && vis(b));
    if (global) return global;
    const c = employmentContainer();
    if (!c) return null;
    return [...c.querySelectorAll("button, a[role='button'], a")]
      .find((b) => /^add( another)?$/i.test(txt(b.innerText)) && vis(b)) || null;
  }

  function workEntryScope(companyEl) {
    let n = companyEl.parentElement;
    while (n && n !== document.body) {
      if (n.querySelector(TITLE_SEL) && n.querySelectorAll(COMPANY_SEL).length === 1) return n;
      n = n.parentElement;
    }
    return companyEl.closest("form") || document.body;
  }

  async function fillWorkEntry(scope, w) {
    let filled = 0;
    if (await fillControl(scope, COMPANY_SEL, w.company)) filled++;
    if (await fillControl(scope, TITLE_SEL, w.title)) filled++;
    // "I currently work here" — must go before dates so end-date gets skipped
    const cur = [...scope.querySelectorAll("input[type='checkbox']")]
      .find((c) => /current|present/i.test(labelText(c)));
    if (cur) {
      if (w.current && !cur.checked) {
        cur.click();
        cur.dispatchEvent(new Event("change", { bubbles: true }));
        filled++;
      }
      done(cur);
    }
    filled += await fillDateControls(scope, /start|from/, w.start_month, w.start_year);
    if (!w.current) filled += await fillDateControls(scope, /end|\bto\b/, w.end_month, w.end_year);
    const locEl = scope.querySelector("input[id^='location'], input[aria-label^='Location' i]");
    if (locEl && w.location && fillTextPlain(locEl, w.location)) filled++;
    return filled;
  }

  // ---------------- public API ----------------

  window.__jpafIsGreenhouse = function () {
    return /(^|\.)greenhouse\.io$/i.test(location.hostname) ||
           !!document.querySelector("form[action*='greenhouse'], meta[content*='greenhouse']");
  };

  window.__jpafGreenhouseSections = function () {
    const out = [];
    if (schoolInputs().length || addButton()) out.push("education");
    if (employmentContainer() || addWorkButton()) out.push("work");
    return out;
  };

  // Grow a repeating section to `count` entries, then fill each entry and tag
  // it data-jpaf-owned so the flat pass leaves it alone.
  async function fillSection(name, inputsFn, addBtnFn, scopeFn, entries, fillFn, stats) {
    if (!entries.length) { progress(name, "skip", "no data in profile"); return; }
    progress(name, "start");
    for (let guard = 0; inputsFn().length < entries.length && guard < 5; guard++) {
      const btn = addBtnFn();
      if (!btn) break;
      const before = inputsFn().length;
      realClick(btn);
      const grew = await waitFor(() => inputsFn().length > before, 3000);
      if (!grew) break;
      await sleep(200);
    }
    const inputs = inputsFn();
    let filled = 0;
    const count = Math.min(inputs.length, entries.length);
    for (let i = 0; i < count; i++) {
      const scope = scopeFn(inputs[i]);
      scope.setAttribute("data-jpaf-owned", "1");
      filled += await fillFn(scope, entries[i]);
      await sleep(120);
    }
    stats.filled += filled;
    stats.sections[name] = filled;
    progress(name, filled ? "done" : "fail",
             `${count} entr${count === 1 ? "y" : "ies"}, ${filled} field${filled === 1 ? "" : "s"}`);
  }

  window.__jpafGreenhouseRun = async function () {
    const stats = { filled: 0, sections: {} };
    // Gate on the SHAPE (GH-style education/employment sections), not the
    // hostname — boards get embedded on company sites and fixtures run local.
    const hasEdu = schoolInputs().length > 0 || !!addButton();
    const hasWork = !!employmentContainer() || !!addWorkButton();
    if (!hasEdu && !hasWork) return stats;

    if (hasEdu) {
      const profile = await sendRetry({ cmd: "profile" }, (p) => (p.education || []).length > 0);
      const edu = (profile && profile.education) || [];
      // debug breadcrumb readable from any world / devtools: why did education skip?
      try {
        document.documentElement.dataset.jpafEduDebug = JSON.stringify({
          got: !!profile, edu: edu.length, stale: (profile && profile._stale) || "",
          keys: profile ? Object.keys(profile).slice(0, 20).join(",") : "",
          body: profile ? JSON.stringify(profile).slice(0, 300) : "" });
      } catch (e) { /* diagnostics only */ }
      if (!edu.length && profile && profile._stale)
        progress("education", "skip", `backend unreachable (${profile._stale})`);
      else
        await fillSection("education", schoolInputs, addButton, entryScope, edu, fillEntry, stats);
    }
    if (hasWork) {
      const history = await sendRetry({ cmd: "history" }, (h) => (h.work || []).length > 0);
      const work = ((history && history.work) || []).slice(0, 4);
      await fillSection("work", companyInputs, addWorkButton, workEntryScope,
                        work, fillWorkEntry, stats);
    }
    return stats;
  };
})();
