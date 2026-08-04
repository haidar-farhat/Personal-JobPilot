/* JobPilot Autofill — Workday wizard engine.
 *
 * Workday's application wizard ("My Experience" etc.) is built from repeating
 * entry panels, segmented date inputs, type-ahead multiselects, and file
 * uploads — none of which the flat scan/plan/fill path can express. This
 * module drives those widgets directly via Workday's stable data-automation-id
 * attributes, pulling structured history from the JobPilot backend.
 *
 * It NEVER clicks Save/Next/Submit; it fills the current wizard step only.
 * Every element (and section container) it manages is tagged data-jpaf-done /
 * data-jpaf-owned so the flat pass that follows won't double-fill.
 *
 * Progress is broadcast as CustomEvent("jpaf-progress", {detail:{section,
 * status, note}}) for the widget's JobRight-style checklist.
 */
(() => {
  if (window.__jpafWorkdayLoaded) return;
  window.__jpafWorkdayLoaded = true;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const norm = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

  const aid = (root, id) => (root || document).querySelector(`[data-automation-id="${id}"]`);
  const aids = (root, prefix) => [...(root || document).querySelectorAll(`[data-automation-id^="${prefix}"]`)];

  function progress(section, status, note) {
    try {
      window.dispatchEvent(new CustomEvent("jpaf-progress", { detail: { section, status, note: note || "" } }));
    } catch (e) { /* display only */ }
  }

  async function waitFor(fn, timeout = 4000, step = 150) {
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

  // Resolve a form control: the automation-id node itself, or a control inside
  // it. Tenants differ — NVIDIA wraps controls in formField-<id> divs — so try
  // the raw id, the formField- prefixed wrapper, and any alias ids given.
  function control(panel, id, kind = "input, textarea") {
    const ids = Array.isArray(id) ? id : [id];
    for (const one of ids) {
      const n = aid(panel, one) || aid(panel, `formField-${one}`);
      if (!n) continue;
      if (n.matches && n.matches(kind)) return n;
      const c = n.querySelector(kind);
      if (c) return c;
    }
    return null;
  }

  // Fill a plain text element unless it already holds a value (never clobber
  // what the user — or another tool — already entered).
  function fillTextEl(el, value) {
    if (!el || value == null || value === "") return false;
    if ((el.value || "").trim()) { done(el); return false; }
    setNative(el, String(value));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
    done(el);
    return true;
  }

  function fillText(panel, id, value) {
    return fillTextEl(control(panel, id), value);
  }

  // Force-overwrite variants for POSITION-OWNED sections (work experience):
  // Workday's résumé-parser values must yield to history — wrong order,
  // "Company | Location" mashups, stale locations. Skips only when equal.
  function setTextEl(el, value) {
    if (!el || value == null || value === "") return false;
    const want = String(value).trim();
    if ((el.value || "").trim() === want) { done(el); return false; }
    setNative(el, want);
    el.dispatchEvent(new Event("blur", { bubbles: true }));
    done(el);
    return true;
  }

  function setText(panel, id, value) {
    return setTextEl(control(panel, id), value);
  }

  function visibleOptions() {
    return [...document.querySelectorAll("[role='option'], li[role='menuitem']")]
      .filter((o) => o.getClientRects().length > 0);
  }

  // Option matching lives in fill.js and is shared by every engine — one gate,
  // one place, one test table (tests/js/choice_matching.mjs). This file used to
  // carry its own weaker copy that would commit "Master of Science" for a
  // Bachelor's. Resolved at CALL time, not load time: the e2e harness injects
  // workday.js before fill.js, and both are present by the time anything runs.
  const chooseChoice = (value, opts) => window.__jpafHelpers.chooseChoice(value, opts);

  function bestOption(value) {
    return chooseChoice(value, visibleOptions().map((el) => ({ el, t: norm(el.innerText) })));
  }

  // Workday <button aria-haspopup="listbox"> dropdowns (degree, etc.)
  async function fillDropdown(panel, id, ...candidates) {
    const wrap = aid(panel, id) || aid(panel, `formField-${id}`);
    if (!wrap) return false;
    const btn = (wrap.matches && wrap.matches("button")) ? wrap : wrap.querySelector("button[aria-haspopup='listbox']");
    if (!btn) return false;
    const current = norm(btn.innerText);
    if (current && !/select one|^select$|^$/.test(current)) { done(btn); return false; }  // already chosen
    realClick(btn);
    await sleep(450);
    let opt = null;
    for (const c of candidates.filter(Boolean)) { opt = bestOption(c); if (opt) break; }
    if (!opt) { await sleep(400); for (const c of candidates.filter(Boolean)) { opt = bestOption(c); if (opt) break; } }
    if (opt) { realClick(opt); await sleep(150); done(btn); return true; }
    btn.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    if (document.body) document.body.click();
    done(btn);
    return false;
  }

  // Segmented date inputs (MM / YYYY spinbuttons)
  function fillDateGroup(panel, wrapperId, month, year) {
    const wrap = aid(panel, wrapperId);
    if (!wrap) return 0;
    let n = 0;
    const mEl = wrap.querySelector("[data-automation-id$='dateSectionMonth-input']");
    const yEl = wrap.querySelector("[data-automation-id$='dateSectionYear-input']");
    if (mEl && month && !(mEl.value || "").trim()) { setNative(mEl, String(month).padStart(2, "0")); mEl.dispatchEvent(new Event("blur", { bubbles: true })); done(mEl); n++; }
    if (yEl && year && !(yEl.value || "").trim()) { setNative(yEl, String(year)); yEl.dispatchEvent(new Event("blur", { bubbles: true })); done(yEl); n++; }
    return n;
  }

  // Force variant for position-owned sections: parser dates must yield.
  // "8" and "08" count as the same month — don't churn equal values.
  function setDateGroup(panel, wrapperId, month, year) {
    const wrap = aid(panel, wrapperId);
    if (!wrap) return 0;
    let n = 0;
    const put = (sel, v, pad) => {
      const el = wrap.querySelector(sel);
      if (!el || !v) return;
      const want = pad ? String(v).padStart(2, "0") : String(v);
      const cur = (el.value || "").trim();
      if (cur && cur.replace(/^0+/, "") === want.replace(/^0+/, "")) { done(el); return; }
      setNative(el, want);
      el.dispatchEvent(new Event("blur", { bubbles: true }));
      done(el); n++;
    };
    put("[data-automation-id$='dateSectionMonth-input']", month, true);
    put("[data-automation-id$='dateSectionYear-input']", year, false);
    return n;
  }

  // STRICT option matcher for searchable prompts: exact, then containment —
  // NEVER token-overlap or first-option fallbacks. A weak match once planted
  // "Afghanistan (+93)" in Cisco's Country Phone Code while the list was still
  // unfiltered, and +93 then rejected the US phone number as invalid-length.
  function promptOption(value) {
    const nv = norm(value);
    if (!nv) return null;
    const opts = visibleOptions().map((el) => ({ el, t: norm(el.innerText) }));
    const o = opts.find((x) => x.t === nv) ||
              opts.find((x) => x.t.includes(nv)) ||
              opts.find((x) => x.t.length >= 4 && nv.includes(x.t));
    return o ? o.el : null;
  }

  // Workday searchable prompt (multiselectInputContainer): type into the
  // Search input, click the matching promptOption; commits as a selectedItem
  // chip. Plain typing WITHOUT the pick never registers (NVIDIA's Country
  // Phone Code stayed empty that way). A chip that does NOT match what we
  // want (bogus pick from an old run) is removed and replaced.
  async function fillPrompt(wrap, ...values) {
    if (!wrap) return false;
    const vals = values.filter(Boolean).map(String);
    const wanted = vals.map(norm);
    const chipSel = "[data-automation-id^='selectedItem']";
    const chips = () => [...wrap.querySelectorAll(chipSel)];
    if (chips().length) {
      const ok = chips().some((c) => {
        const t = norm(c.innerText);
        return wanted.some((v) => t.includes(v) || (t && v.includes(t)));
      });
      if (ok) { wrap.setAttribute("data-jpaf-done", "1"); return false; }
      for (const c of chips()) {           // wrong value — remove the chip(s)
        realClick(c.querySelector("button") || c);
        await sleep(250);
      }
      if (chips().length) { wrap.setAttribute("data-jpaf-done", "1"); return false; }
    }
    const inp = wrap.querySelector("input");
    if (!inp) return false;
    inp.focus();
    realClick(inp);
    for (const v of vals) {
      setNative(inp, v);
      const opt = await waitFor(() => promptOption(v), 4000);
      if (opt) {
        realClick(opt);
        await sleep(250);
        done(inp);
        return true;
      }
    }
    setNative(inp, "");
    inp.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    done(inp);
    return false;
  }

  function setCheckbox(panel, id, checked) {
    const el = control(panel, id, "input[type='checkbox']") ||
               (aid(panel, id) && aid(panel, id).querySelector("input[type='checkbox']"));
    if (!el) return false;
    done(el);
    if (!!el.checked === !!checked) return false;
    el.click();
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  // Find a wizard section: automation id, then NVIDIA-style role=group with
  // aria-labelledby "<Name>-section", then a heading-scoped container. The old
  // heading fallback (h.closest("[data-automation-id]")) resolved to the WHOLE
  // PAGE on NVIDIA and fillSkills typed skills into the first work entry's Job
  // Title — the container must hold this section's own fields and stop growing
  // before it swallows a neighbouring section's heading.
  const SECTION_HEADS = /work experience|education|^skills$|add skills|resume|websites|social network|certific|languages/i;
  function findSection(autoId, headingRe, groupName, probeSel) {
    const s = aid(document, autoId);
    if (s) return s;
    if (groupName) {
      const g = document.querySelector(`div[role='group'][aria-labelledby*='${groupName}' i]`);
      if (g) return g;
    }
    const h = [...document.querySelectorAll("h2, h3, h4")]
      .find((x) => headingRe.test(x.innerText || "") && x.getClientRects().length > 0);
    if (!h) return null;
    let n = h.parentElement, best = null;
    for (let i = 0; n && n !== document.body && i < 6; i++) {
      const foreign = [...n.querySelectorAll("h2, h3, h4")]
        .some((x) => x !== h && SECTION_HEADS.test((x.innerText || "").trim()));
      if (foreign) break;
      if (!probeSel || n.querySelector(probeSel)) best = n;
      n = n.parentElement;
    }
    return best;
  }

  // Repeating entry panels inside a section: classic "workExperience-N"
  // automation ids, NVIDIA-style "<Name>-N-panel" role groups, or — last
  // resort — one group per anchor field (e.g. formField-jobTitle).
  function entryPanels(section, classicPrefix, anchorSel) {
    const classic = aids(section, classicPrefix);
    if (classic.length) return classic;
    const named = [...section.querySelectorAll("div[role='group'][aria-labelledby*='-panel']")];
    if (named.length) return named;
    return [...section.querySelectorAll(anchorSel)].map((a) => {
      let scope = a, n = a.parentElement;
      while (n && n !== section && n.querySelectorAll(anchorSel).length === 1) { scope = n; n = n.parentElement; }
      return scope;
    });
  }

  function addEntryButton(section) {
    return [...section.querySelectorAll("button")].find((b) =>
      b.getAttribute("data-automation-id") === "add-button" ||
      /^add( another)?$/i.test((b.innerText || "").trim()));
  }

  // Grow a repeating section to `count` entries by clicking Add / Add Another.
  async function ensurePanels(section, panelPrefix, count) {
    for (let guard = 0; guard < count + 2; guard++) {
      const have = aids(section, panelPrefix).length;
      if (have >= count) return aids(section, panelPrefix);
      const btn = [...section.querySelectorAll("button")].find((b) =>
        b.getAttribute("data-automation-id") === "add-button" || /^add( another)?$/i.test((b.innerText || "").trim()));
      if (!btn) return aids(section, panelPrefix);
      realClick(btn);
      const grew = await waitFor(() => aids(section, panelPrefix).length > have, 5000);
      if (!grew) return aids(section, panelPrefix);
      await sleep(250);
    }
    return aids(section, panelPrefix);
  }

  // ---------------- sections ----------------

  // "My Information" step (NVIDIA 2026-07-05): previous-worker radios,
  // country/state/phone-type LISTBOX BUTTONS, the Country Phone Code
  // searchable prompt, and the Phone Extension trap (an earlier flat pass
  // typed the phone number into it — clear that and own the field).
  async function fillMyInfo(profile) {
    const page = aid(document, "applyFlowMyInfoPage") ||
                 (aid(document, "formField-candidateIsPreviousWorker") && document.body);
    if (!page) return null;
    const prof = profile || {};
    const ident = prof.identity || {}, addr = prof.address || {}, prefs = prof.preferences || {};
    progress("contact", "start");
    let filled = 0;

    const pw = aid(page, "formField-candidateIsPreviousWorker");
    if (pw) {
      const radios = [...pw.querySelectorAll("input[type='radio']")];
      if (radios.length && !radios.some((r) => r.checked)) {
        const want = prefs.worked_here_before ? "true" : "false";
        const r = radios.find((x) => (x.value || "").toLowerCase() === want);
        if (r) { realClick(r); r.dispatchEvent(new Event("change", { bubbles: true })); filled++; }
      }
      radios.forEach(done);
    }

    // candidates ordered exact-first: "United States" as a bare substring can
    // hit "United States Minor Outlying Islands" in an alphabetical list
    if (await fillDropdown(page, "country", "United States of America", addr.country)) filled++;
    if (await fillDropdown(page, "countryRegion", addr.state_full, addr.state)) filled++;
    if (await fillDropdown(page, "phoneType", "Mobile")) filled++;

    if (fillText(page, "formField-legalName--firstName", ident.first_name)) filled++;
    if (fillText(page, "formField-legalName--lastName", ident.last_name)) filled++;
    if (fillText(page, "formField-addressLine1", addr.street)) filled++;
    if (fillText(page, "formField-city", addr.city)) filled++;
    if (fillText(page, "formField-postalCode", addr.postal_code)) filled++;

    // Stale-account trap: a FULL street address parked in Address Line 2 (we
    // have no line 2 — that slot is apt/suite only). Cisco 2026-07-06 carried
    // "1670A 32nd Avenue" there. Clear it; keep genuine apt/suite values.
    const a2 = control(page, ["addressLine2"]);
    if (a2) {
      const v2 = (a2.value || "").trim();
      if (v2 && /\d/.test(v2) &&
          /\b(ave|avenue|st|street|blvd|boulevard|rd|road|dr|drive|way|ln|lane|ct|court|pl|place|ter|terrace)\b/i.test(v2) &&
          !/\b(apt|apartment|suite|ste|unit|#|floor|fl)\b/i.test(v2)) {
        setNative(a2, "");
        a2.dispatchEvent(new Event("blur", { bubbles: true }));
        filled++;
      }
      done(a2);
    }

    // Phone is OWNED as bare national digits — the country code lives in its
    // own field, and strict tenants (Cisco) reject formatted values outright.
    const phoneDigits = (ident.phone || "").replace(/\D+/g, "");
    const phEl = control(page, ["phoneNumber"]);
    if (phEl && phoneDigits) {
      if ((phEl.value || "").trim() !== phoneDigits) {
        if (setTextEl(phEl, phoneDigits)) filled++;
      } else done(phEl);
    }

    const cpc = aid(page, "formField-countryPhoneCode");
    if (cpc && await fillPrompt(cpc, "United States of America", addr.country)) filled++;

    // Phone extension: we have none — own it so the flat pass can't misfill
    // it, and clear the damage if an earlier run typed the phone number here.
    const extWrap = aid(page, "formField-extension");
    if (extWrap) {
      const ei = (extWrap.matches && extWrap.matches("input")) ? extWrap : extWrap.querySelector("input");
      if (ei) {
        const digits = (s) => (s || "").replace(/\D+/g, "");
        if ((ei.value || "").trim() && digits(ei.value) === digits(ident.phone)) {
          setNative(ei, "");
          ei.dispatchEvent(new Event("blur", { bubbles: true }));
          filled++;
        }
        done(ei);
      }
    }

    progress("contact", "done", `${filled} field${filled === 1 ? "" : "s"}`);
    return { filled };
  }

  const W_TITLE = ["jobTitle", "title"], W_COMPANY = ["company", "companyName"],
        W_DESC = ["description", "roleDescription"];
  const W_ANCHOR = "[data-automation-id='formField-jobTitle'], [data-automation-id='jobTitle']";

  async function fillWork(history) {
    const section = findSection("workExperienceSection", /work experience/i, "Work-Experience",
                                W_ANCHOR + ", [data-automation-id^='workExperience-'], [data-automation-id='add-button']");
    if (!section) return null;
    progress("work", "start");
    section.setAttribute("data-jpaf-owned", "1");
    let filled = 0;
    const items = history.work || [];
    if (!items.length) { progress("work", "skip", "no history"); return { filled }; }
    const skillsSet = new Set((history.skills || []).map(norm));
    const val = (p, ids) => { const el = control(p, ids); return el ? (el.value || "").trim() : ""; };
    const getPanels = () => entryPanels(section, "workExperience-", W_ANCHOR);

    // POSITION-OWNED: history order (newest job FIRST) is the source of truth
    // — panel i gets item i, so the current role sits at the top. Workday's
    // résumé-parser pre-creates panels in ITS order with "Company | Location"
    // mashups and stale locations (Cisco 2026-07-06: parser leftovers held the
    // top slot and the newest job was missing entirely); those values are ours
    // to overwrite. A panel is a writable slot when it's empty or its company/
    // title matches SOME history item. Anything unrecognizable is treated as
    // hand-entered: skipped and flagged for review, never touched.
    const sameCompany = (a, b) => {
      const na = norm(a), nb = norm(b);
      return !!na && !!nb && (na === nb || na.includes(nb) || nb.includes(na));
    };
    const ours = (p) => {
      const c = val(p, W_COMPANY), t = val(p, W_TITLE);
      if (!c && !t) return true;
      if (t && !c && skillsSet.has(norm(t))) return true;  // old skill-into-title damage
      return items.some((w) => sameCompany(c, w.company) || (!!t && norm(t) === norm(w.title)));
    };
    let slots = getPanels().filter(ours);
    let placed = 0;
    for (let i = 0; i < items.length; i++) {
      const w = items[i];
      let panel = slots[i];
      if (!panel) {
        const btn = addEntryButton(section);
        if (!btn) break;
        const before = getPanels().length;
        realClick(btn);
        const grew = await waitFor(() => getPanels().length > before, 5000);
        if (!grew) break;
        await sleep(250);
        slots = getPanels().filter(ours);
        panel = slots[i];
        if (!panel) break;
      }
      if (setText(panel, W_TITLE, w.title)) filled++;
      if (setText(panel, W_COMPANY, w.company)) filled++;
      if (setText(panel, "location", w.location)) filled++;
      if (setCheckbox(panel, "currentlyWorkHere", !!w.current)) filled++;
      filled += setDateGroup(panel, "formField-startDate", w.start_month, w.start_year);
      if (!w.current) {
        // unchecking "current" renders the End Date group asynchronously
        await waitFor(() => aid(panel, "formField-endDate"), 1500);
        filled += setDateGroup(panel, "formField-endDate", w.end_month, w.end_year);
      }
      if (setText(panel, W_DESC, w.description)) filled++;
      placed++;
      await sleep(150);
    }
    const finalPanels = getPanels();
    const foreign = finalPanels.filter((p) => !ours(p)).length;
    const extra = Math.max(0, finalPanels.length - foreign - placed);
    progress("work", "done",
             `${placed} entr${placed === 1 ? "y" : "ies"}` +
             (extra ? ` · ${extra} duplicate — delete` : "") +
             (foreign ? ` · ${foreign} existing — review` : ""));
    return { filled };
  }

  const E_ANCHOR = "[data-automation-id*='school' i], [data-automation-id='formField-schoolName']";

  async function fillEducation(history) {
    const section = findSection("educationSection", /^education\b/i, "Education",
                                E_ANCHOR + ", [data-automation-id^='education-'], [data-automation-id='add-button']");
    if (!section) return null;
    progress("education", "start");
    section.setAttribute("data-jpaf-owned", "1");
    let filled = 0;
    const items = history.education || [];
    if (!items.length) { progress("education", "skip", "no history"); return { filled }; }
    let panels = await ensurePanels(section, "education-", items.length);
    if (!panels.length) {
      // NVIDIA-style tenants: grow via Add, panels are "<Name>-N-panel" groups
      const getPanels = () => entryPanels(section, "education-", E_ANCHOR);
      for (let guard = 0; getPanels().length < items.length && guard < items.length + 1; guard++) {
        const btn = addEntryButton(section);
        if (!btn) break;
        const before = getPanels().length;
        realClick(btn);
        const grew = await waitFor(() => getPanels().length > before, 5000);
        if (!grew) break;
        await sleep(250);
      }
      panels = getPanels();
    }
    for (let i = 0; i < Math.min(panels.length, items.length); i++) {
      const p = panels[i], e = items[i];
      // Cisco renders School as a searchable PROMPT — typed text never commits
      // (it sat as an inputAlert while Cal Poly auto-resolved); it needs the
      // type-and-pick flow. Plain-input tenants keep the text path.
      const schoolWrap = aid(p, "formField-school") || aid(p, "formField-schoolName") || aid(p, "school");
      if (schoolWrap && schoolWrap.querySelector(
            "[data-automation-id='multiselectInputContainer'], [data-automation-id='multiSelectContainer']")) {
        if (await fillPrompt(schoolWrap, e.school)) filled++;
      } else if (fillText(p, ["school", "schoolName", "schoolItem"], e.school)) filled++;
      if (await fillDropdown(p, "degree", e.degree_label, e.degree)) filled++;
      // field of study is a type-ahead multiselect on most tenants
      const fos = control(p, ["field-of-study", "fieldOfStudy"]) ||
                  (aid(p, "formField-fieldOfStudy") && aid(p, "formField-fieldOfStudy").querySelector("input"));
      if (fos && e.field_of_study && !(fos.value || "").trim() &&
          !p.querySelector("[data-automation-id='selectedItem']")) {
        fos.focus();
        setNative(fos, e.field_of_study);
        await sleep(700);
        // STRICT match only — a first-option fallback here is how "Afghanistan
        // (+93)" class bugs happen; better unfilled than wrong
        const opt = promptOption(e.field_of_study);
        if (opt) { realClick(opt); filled++; }
        else fos.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        done(fos);
        await sleep(150);
      }
      if (e.gpa) { if (fillText(p, ["gpa", "gradeAverage"], e.gpa)) filled++; }
      if (e.end_year) {
        const got = fillDateGroup(p, "formField-endDate", null, e.end_year) ||
                    fillDateGroup(p, "formField-lastYearAttended", null, e.end_year);
        filled += got;
      }
      await sleep(150);
    }
    progress("education", "done", `${Math.min(panels.length, items.length)} entr${items.length === 1 ? "y" : "ies"}`);
    return { filled };
  }

  async function fillSkills(history) {
    const section = findSection("skillsSection", /^skills$|add skills/i, "Skills",
                                "[data-automation-id='formField-skills'], [data-automation-id='searchBox'], " +
                                "[data-automation-id='multiselectInputContainer']");
    if (!section) return null;
    progress("skills", "start");
    section.setAttribute("data-jpaf-owned", "1");
    // ONLY the skills prompt input — a loose input grab typed skills into the
    // first work entry's Job Title on NVIDIA ("Matplotlib" as a job title)
    const input = section.querySelector(
      "[data-automation-id='formField-skills'] input, [data-automation-id='searchBox'], " +
      "[data-automation-id='multiselectInputContainer'] input, input[placeholder='Search' i]");
    if (!input) { progress("skills", "skip", "no skills box"); return { filled: 0 }; }
    const existing = new Set(
      [...section.querySelectorAll("[data-automation-id='selectedItem']")].map((x) => norm(x.innerText)));
    let added = 0;
    const want = (history.skills || []).slice(0, 12);
    for (const skill of want) {
      if (existing.has(norm(skill))) continue;
      input.focus();
      setNative(input, skill);
      const opt = await waitFor(() => bestOption(skill), 2500);
      if (opt) {
        realClick(opt);
        added++;
        progress("skills", "start", `${added} added…`);
      } else {
        input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      }
      await sleep(250);
    }
    done(input);
    progress("skills", "done", `${added} added`);
    return { filled: added };
  }

  function send(msg) {
    return new Promise((resolve) => {
      try { chrome.runtime.sendMessage(msg, resolve); }
      catch (e) { resolve(null); }
    });
  }

  async function fillResume(ctx) {
    // any visible file input on the step (resume/attachments section)
    const input = document.querySelector(
      "input[data-automation-id='file-upload-input-ref'], [data-automation-id='resumeSection'] input[type='file'], " +
      "[data-automation-id='attachments'] input[type='file'], input[type='file']");
    if (!input) return null;
    // already attached? Workday lists uploads as file-upload-item rows — one
    // attachments area per step, so a document-level check is the reliable one
    // (input.closest("[data-automation-id]") would match the input itself).
    if (document.querySelector("[data-automation-id^='file-upload-item']") ||
        (input.files && input.files.length)) {
      progress("resume", "skip", "already attached");
      done(input);
      return { filled: 0 };
    }
    progress("resume", "start");
    const f = await send({ cmd: "resume_file", company: ctx.company, title: ctx.title });
    if (!f || !f.b64) { progress("resume", "fail", (f && f.error) || "backend offline"); return { filled: 0 }; }
    try {
      const bytes = Uint8Array.from(atob(f.b64), (c) => c.charCodeAt(0));
      const file = new File([bytes], f.filename || "resume.docx",
        { type: f.mime || "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      done(input);
      progress("resume", "done", f.filename || "");
      return { filled: 1 };
    } catch (e) {
      progress("resume", "fail", e.message);
      return { filled: 0 };
    }
  }

  async function fillWebsites(history) {
    const section = findSection("websiteSection", /websites?/i, "Websites",
                                "[data-automation-id='formField-url'], [data-automation-id^='websitePanelSet-'], " +
                                "[data-automation-id='add-button']");
    if (!section) return null;
    const links = history.links || {};
    const urls = [links.portfolio, links.github, links.website].filter(Boolean);
    if (!urls.length) return { filled: 0 };
    progress("websites", "start");
    section.setAttribute("data-jpaf-owned", "1");
    let filled = 0;
    let panels = await ensurePanels(section, "websitePanelSet-", urls.length);
    if (!panels.length)
      panels = entryPanels(section, "websitePanelSet-", "[data-automation-id='formField-url']");
    for (let i = 0; i < Math.min(panels.length, urls.length); i++) {
      if (fillText(panels[i], ["website", "url"], urls[i])) filled++;
    }
    // single bare input variant
    if (!panels.length && fillText(section, ["website", "url"], urls[0])) filled++;
    progress("websites", "done", `${filled}`);
    return { filled };
  }

  // "How Did You Hear About Us?" — a hierarchical category picker (e.g.
  // Website → NVIDIA.COM). Type-ahead doesn't open it; it needs a click-walk.
  async function fillSource() {
    const wrap = aid(document, "formField-source");
    if (!wrap) return null;
    if (wrap.querySelector("[data-automation-id='selectedItem']")) {  // already answered
      wrap.setAttribute("data-jpaf-owned", "1");
      return { filled: 0 };
    }
    // Cisco variant: a plain LISTBOX button, not a searchable prompt. If it
    // already shows a value (e.g. "LinkedIn" carried in by the job link),
    // leave it — don't report a scary "fail" for an answered field.
    const lb = wrap.querySelector("button[aria-haspopup='listbox']");
    if (lb) {
      wrap.setAttribute("data-jpaf-owned", "1");
      const cur = norm(lb.innerText);
      if (cur && !/select one|^select$/.test(cur)) return { filled: 0 };
      progress("source", "start");
      const ok = await fillDropdown(document, "source", "LinkedIn", "Job Board");
      progress("source", ok ? "done" : "skip", ok ? (lb.innerText || "").trim() : "left for you");
      return { filled: ok ? 1 : 0 };
    }
    const input = wrap.querySelector("input");
    if (!input) return null;
    progress("source", "start");
    wrap.setAttribute("data-jpaf-owned", "1");
    realClick(input);
    const optsNow = () => [...document.querySelectorAll("[data-automation-id='promptOption']")]
      .filter((o) => o.getClientRects().length);
    let cats = await waitFor(() => (optsNow().length ? optsNow() : null), 3000);
    if (!cats) { progress("source", "fail", "menu didn't open"); return { filled: 0 }; }
    // prefer the company's own site, then job boards
    const pickCat = (res) => cats.find((o) => res.test(o.innerText));
    const cat = pickCat(/website/i) || pickCat(/job board/i) || pickCat(/other/i) || cats[0];
    realClick(cat);
    await sleep(700);
    const subs = optsNow().filter((o) => o !== cat);
    if (subs.length) {
      const slug = (location.hostname.split(".")[0] || "").toLowerCase();
      const sub = subs.find((o) => o.innerText.toLowerCase().includes(slug)) ||
                  subs.find((o) => /\.com|career|company/i.test(o.innerText)) || subs[0];
      realClick(sub);
      await sleep(400);
    }
    if (document.body) document.body.click();  // close the menu
    const picked = wrap.querySelector("[data-automation-id='selectedItem']");
    progress("source", picked ? "done" : "fail", picked ? picked.innerText.trim() : "");
    return { filled: picked ? 1 : 0 };
  }

  function fillLinkedIn(history) {
    const wrap = aid(document, "linkedinQuestion") || aid(document, "formField-linkedInAccount");
    if (!wrap) return null;
    const el = wrap.querySelector("input");
    const li = (history.links || {}).linkedin;
    if (el && li && !(el.value || "").trim()) {
      setNative(el, li);
      done(el);
      return { filled: 1 };
    }
    if (el) done(el);
    return { filled: 0 };
  }

  // ---- Voluntary Disclosures + Self Identify (EEO) ----

  const txt = (s) => (s || "").replace(/\s+/g, " ").trim();

  // Human label for a Workday control (its formField wrapper's <label>/<legend>).
  function wdLabel(el) {
    const wrap = el.closest("[data-automation-id^='formField-']") ||
                 el.closest("fieldset") || el.closest("[data-automation-id]") || el.parentElement;
    if (wrap) {
      const lab = wrap.querySelector("label, legend");
      if (lab && txt(lab.innerText)) return txt(lab.innerText);
    }
    return txt(el.getAttribute && el.getAttribute("aria-label")) || "";
  }

  function radioLabel(r) {
    if (r.id) {
      const l = document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
      if (l && txt(l.innerText)) return txt(l.innerText);
    }
    const w = r.closest("label");
    if (w && txt(w.innerText)) return txt(w.innerText);
    const sib = r.parentElement && r.parentElement.querySelector("label, span");
    return (sib && txt(sib.innerText)) || r.value || "";
  }

  // Map a disclosure field's label to the stored answer.
  function eeoValue(label, H) {
    const n = norm(label), e = H.eeoc || {}, wa = H.work_authorization || {};
    if (!n) return null;
    if (/transgender/.test(n)) return e.transgender || "No";
    if (/sexual orientation/.test(n) || /(^| )orientation/.test(n)) return e.sexual_orientation;
    if (/lgbtq|lgbt/.test(n)) return e.lgbtq;
    if (/hispanic|latino|latinx/.test(n)) return e.hispanic_latino;
    if (/gender/.test(n)) return e.gender;
    if (/race|ethnic/.test(n)) return e.race_ethnicity;
    if (/veteran|military/.test(n)) return e.veteran_status;
    if (/disab/.test(n)) return e.disability_status;
    if (/sponsor/.test(n)) return wa.requires_sponsorship ? "Yes" : "No";
    if (/legally authorized|authorized to work|eligible to work|work authorization/.test(n))
      return wa.authorized_to_work_us === false ? "No" : "Yes";
    if (/citizen/.test(n)) return wa.citizenship;
    return null;
  }

  function todayParts() {
    const d = new Date();
    return { m: d.getMonth() + 1, d: d.getDate(), y: d.getFullYear() };
  }

  // Fill a MM/DD/YYYY (or partial) segmented Workday date wrapper with today.
  function fillDateToday(wrap) {
    if (!wrap) return 0;
    const t = todayParts();
    let n = 0;
    const seg = (suffix, val) => {
      const el = wrap.querySelector(`[data-automation-id$='${suffix}']`);
      if (el && !(el.value || "").trim()) {
        setNative(el, String(val)); el.dispatchEvent(new Event("blur", { bubbles: true })); done(el); n++;
      }
    };
    seg("dateSectionMonth-input", String(t.m).padStart(2, "0"));
    seg("dateSectionDay-input", String(t.d).padStart(2, "0"));
    seg("dateSectionYear-input", String(t.y));
    return n;
  }

  function isDisclosureStep() {
    const heads = [...document.querySelectorAll("h1,h2,h3,h4")].map((h) => norm(h.innerText)).join(" | ");
    if (/voluntary disclosure|self identif|self-identif|equal employ|demographic|disability/.test(heads)) return true;
    const labels = [...document.querySelectorAll("label, legend")].map((l) => norm(l.innerText)).join(" | ");
    return /\bgender\b|ethnic|\bveteran\b|disability|hispanic|\brace\b/.test(labels);
  }

  // Voluntary Disclosures + Self Identify: EEO listboxes, radio groups, the
  // disability self-identification, a name+date "signature", and consent boxes.
  async function fillDisclosures(history) {
    if (!isDisclosureStep()) return null;
    progress("identity", "start");
    let filled = 0;

    // 1. listbox dropdowns (gender / ethnicity / veteran status / …)
    const listboxes = [...document.querySelectorAll("button[aria-haspopup='listbox']")]
      .filter((b) => b.getClientRects().length && !b.getAttribute("data-jpaf-done"));
    for (const btn of listboxes) {
      const val = eeoValue(wdLabel(btn), history);
      if (!val) continue;
      const cur = norm(btn.innerText);
      if (cur && !/select one|^select$|^$|choose/.test(cur)) { done(btn); continue; }
      realClick(btn);
      await sleep(450);
      let opt = bestOption(val);
      if (!opt) { await sleep(400); opt = bestOption(val); }
      if (opt) { realClick(opt); await sleep(150); filled++; }
      else { btn.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); if (document.body) document.body.click(); }
      done(btn);
    }

    // 2. radio groups (disability status, hispanic/latino, gender variants)
    const groups = new Map();
    document.querySelectorAll("input[type='radio']").forEach((r) => {
      if (r.getAttribute("data-jpaf-done") || !r.getClientRects().length) return;
      const wrap = r.closest("[data-automation-id^='formField-']") || r.closest("fieldset");
      const key = r.name || (wrap && wrap.getAttribute("data-automation-id")) || "grp";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(r);
    });
    for (const radios of groups.values()) {
      const val = eeoValue(wdLabel(radios[0]), history);
      if (!val) continue;
      if (radios.some((r) => r.checked)) { radios.forEach(done); continue; }  // already answered
      const chosen = chooseChoice(val, radios.map((r) => ({ el: r, t: norm(radioLabel(r)) })));
      if (chosen) { realClick(chosen); chosen.dispatchEvent(new Event("change", { bubbles: true })); filled++; }
      radios.forEach(done);
    }

    // 3. Self-Identify "signature": legal name + today's date
    const idn = history.identity || {};
    const nameInputs = [...document.querySelectorAll("input[type='text'], input:not([type])")]
      .filter((el) => el.getClientRects().length && !el.getAttribute("data-jpaf-done") && !(el.value || "").trim());
    for (const el of nameInputs) {
      const n = norm(wdLabel(el));
      if (/your name|please enter your name|employee name|^name$|full name|electronic signature/.test(n) && idn.full_name) {
        setNative(el, idn.full_name); done(el); filled++;
      }
    }
    [...document.querySelectorAll("[data-automation-id^='formField-']")].forEach((wrap) => {
      if (wrap.getAttribute("data-jpaf-done")) return;
      const n = norm(wdLabel(wrap.querySelector("input") || wrap));
      if (/date|today/.test(n) && wrap.querySelector("[data-automation-id$='dateSectionYear-input']")) {
        const added = fillDateToday(wrap);
        if (added) { filled += 1; wrap.setAttribute("data-jpaf-done", "1"); }
      }
    });

    // 4. consent / acknowledgement checkboxes (required to proceed; user still
    //    reviews before submitting). Only tick boxes whose label reads as consent.
    document.querySelectorAll("input[type='checkbox']").forEach((cb) => {
      if (cb.checked || cb.getAttribute("data-jpaf-done") || !cb.getClientRects().length) return;
      const n = norm(wdLabel(cb) + " " + radioLabel(cb));
      if (/read|understand|acknowledge|consent|certify|agree|terms|reviewed/.test(n)) {
        cb.click(); cb.dispatchEvent(new Event("change", { bubbles: true })); done(cb); filled++;
      }
    });

    progress("identity", "done", `${filled} field${filled === 1 ? "" : "s"}`);
    return { filled };
  }

  window.__jpafIsWorkday = function () {
    return /myworkdayjobs\.com|myworkdaysite\.com|workday\.com/i.test(location.hostname) &&
           !!document.querySelector("[data-automation-id]");
  };

  // Advance the wizard one step: click Next / Save and Continue — NEVER Submit
  // or anything on the review step. Returns {clicked, reason?, label?} after the
  // step actually changes (or validation errors / timeout stop us).
  window.__jpafWorkdayNext = async function () {
    const btn = aid(document, "bottom-navigation-next-button") ||
                aid(document, "pageFooterNextButton") ||
                [...document.querySelectorAll("button")].find((b) =>
                  b.getClientRects().length &&
                  /^(save and continue|continue|next)$/i.test((b.innerText || "").trim()));
    if (!btn) return { clicked: false, reason: "no-next-button" };
    const label = (btn.innerText || "").trim();
    if (/submit|review/i.test(label)) return { clicked: false, reason: "at-" + label.toLowerCase() };
    const sig = () => {
      const prog = aid(document, "progressBarActiveStep");
      const h = document.querySelector("h2, h3");
      return ((prog && prog.innerText) || "") + "|" + ((h && h.innerText) || "");
    };
    const before = sig();
    realClick(btn);
    const end = Date.now() + 12000;
    while (Date.now() < end) {
      await sleep(350);
      const err = document.querySelector(
        "[data-automation-id='errorBanner'], [data-automation-id='alertMessage'], " +
        "[data-automation-id='inlineAlertMessage']");
      if (err && err.getClientRects().length) return { clicked: false, reason: "validation-errors", label };
      if (sig() !== before) {
        // wait for the new step's form to render before the caller refills
        await waitFor(() => document.querySelector("[data-automation-id^='formField-'], input, button[aria-haspopup='listbox']"), 6000);
        await sleep(600);
        return { clicked: true, label };
      }
    }
    return { clicked: false, reason: "timeout", label };
  };

  // Which wizard sections exist on the current step (for the progress panel).
  window.__jpafWorkdaySections = function () {
    const out = [];
    if (aid(document, "applyFlowMyInfoPage") || aid(document, "formField-candidateIsPreviousWorker"))
      out.push("contact");
    if (findSection("workExperienceSection", /work experience/i, "Work-Experience", W_ANCHOR + ", [data-automation-id='add-button']")) out.push("work");
    if (findSection("educationSection", /^education\b/i, "Education", E_ANCHOR + ", [data-automation-id='add-button']")) out.push("education");
    if (findSection("skillsSection", /^skills$|add skills/i, "Skills", "[data-automation-id='formField-skills'], [data-automation-id='searchBox'], [data-automation-id='multiselectInputContainer']")) out.push("skills");
    if (document.querySelector("input[type='file']")) out.push("resume");
    if (findSection("websiteSection", /websites?/i, "Websites", "[data-automation-id='formField-url'], [data-automation-id='add-button']")) out.push("websites");
    if (isDisclosureStep()) out.push("identity");
    return out;
  };

  window.__jpafWorkdayRun = async function () {
    const stats = { filled: 0, sections: {} };
    if (!window.__jpafIsWorkday()) return stats;
    const ctx = {
      company: (location.hostname.split(".")[0] || "").replace(/[^a-z0-9]/gi, " "),
      title: ((document.querySelector("h1, h2") || {}).innerText || document.title || "").slice(0, 120),
    };
    // company/title let the backend serve the TAILORED resume's entries, so
    // experience panels match the attached .docx
    const history = await send({ cmd: "history", company: ctx.company, title: ctx.title });
    if (!history || history.error) { progress("wizard", "fail", "backend offline"); return stats; }
    const profile = await send({ cmd: "profile" });
    const steps = [
      ["contact", () => fillMyInfo(profile)],
      ["work", () => fillWork(history)],
      ["education", () => fillEducation(history)],
      ["skills", () => fillSkills(history)],
      ["resume", () => fillResume(ctx)],
      ["websites", () => fillWebsites(history)],
      ["linkedin", () => fillLinkedIn(history)],
      ["source", () => fillSource()],
      ["identity", () => fillDisclosures(history)],
    ];
    for (const [name, fn] of steps) {
      try {
        const r = await fn();
        if (r) { stats.sections[name] = r.filled; stats.filled += r.filled; }
      } catch (e) {
        progress(name, "fail", e.message);
      }
    }
    return stats;
  };
})();
