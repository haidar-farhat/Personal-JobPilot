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

  // Resolve a form control: the automation-id node itself, or a control inside it.
  function control(panel, id, kind = "input, textarea") {
    const n = aid(panel, id);
    if (!n) return null;
    if (n.matches && n.matches(kind)) return n;
    return n.querySelector(kind);
  }

  // Fill a plain text control unless it already holds a value (never clobber
  // what the user — or another tool — already entered).
  function fillText(panel, id, value) {
    const el = control(panel, id);
    if (!el || value == null || value === "") return false;
    if ((el.value || "").trim()) { done(el); return false; }
    setNative(el, String(value));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
    done(el);
    return true;
  }

  function visibleOptions() {
    return [...document.querySelectorAll("[role='option'], li[role='menuitem']")]
      .filter((o) => o.getClientRects().length > 0);
  }

  // EEO-tolerant option matching (mirrors fill.js / autofill_mapper._match_choice)
  const OPTION_SYNONYMS = {
    "mobile": ["cellular", "cell"], "united states": ["united states of america", "usa"],
    "male": ["man"], "female": ["woman"], "heterosexual": ["straight"],
    "two or more races": ["two or more", "multiracial"],
  };
  const DECLINE_HINTS = ["decline", "do not wish", "dont wish", "prefer not",
                         "not to answer", "not wish", "choose not", "rather not"];

  function chooseChoice(value, opts) {  // opts: [{el, t:normText}]
    const nv = norm(value);
    const cands = [nv, ...(OPTION_SYNONYMS[nv] || [])];
    for (const c of cands) { const o = opts.find((x) => x.t === c); if (o) return o.el; }
    if (/^(decline|prefer not|do not wish|dont wish|i do not wish|i dont wish)/.test(nv)) {
      const o = opts.find((x) => DECLINE_HINTS.some((h) => x.t.includes(h))); if (o) return o.el;
    }
    if (nv === "yes" || nv === "no") {
      let o = opts.find((x) => x.t.split(" ")[0] === nv);
      if (!o && nv === "no") o = opts.find((x) => x.t.startsWith("not "));
      if (o) return o.el;
    }
    for (const c of cands) { const o = opts.find((x) => x.t && (x.t.includes(c) || c.includes(x.t))); if (o) return o.el; }
    const dw = nv.split(" ").filter((w) => w.length > 2);
    let best = null, bn = 0;
    for (const x of opts) { const ow = new Set(x.t.split(" ")); const k = dw.filter((w) => ow.has(w)).length; if (k > bn) { best = x.el; bn = k; } }
    return best;
  }

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

  // Find a wizard section by automation id, falling back to a heading match.
  function findSection(autoId, headingRe) {
    const s = aid(document, autoId);
    if (s) return s;
    const h = [...document.querySelectorAll("h2, h3, h4")]
      .find((x) => headingRe.test(x.innerText || "") && x.getClientRects().length > 0);
    return h ? (h.closest("[data-automation-id]") || h.parentElement) : null;
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

  async function fillWork(history) {
    const section = findSection("workExperienceSection", /work experience/i);
    if (!section) return null;
    progress("work", "start");
    section.setAttribute("data-jpaf-owned", "1");
    let filled = 0;
    const items = history.work || [];
    if (!items.length) { progress("work", "skip", "no history"); return { filled }; }
    const panels = await ensurePanels(section, "workExperience-", items.length);
    for (let i = 0; i < Math.min(panels.length, items.length); i++) {
      const p = panels[i], w = items[i];
      if (fillText(p, "jobTitle", w.title)) filled++;
      if (fillText(p, "company", w.company)) filled++;
      if (fillText(p, "location", w.location)) filled++;
      if (w.current) setCheckbox(p, "currentlyWorkHere", true);
      filled += fillDateGroup(p, "formField-startDate", w.start_month, w.start_year);
      if (!w.current) filled += fillDateGroup(p, "formField-endDate", w.end_month, w.end_year);
      if (fillText(p, "description", w.description)) filled++;
      await sleep(150);
    }
    progress("work", "done", `${Math.min(panels.length, items.length)} entr${items.length === 1 ? "y" : "ies"}`);
    return { filled };
  }

  async function fillEducation(history) {
    const section = findSection("educationSection", /education/i);
    if (!section) return null;
    progress("education", "start");
    section.setAttribute("data-jpaf-owned", "1");
    let filled = 0;
    const items = history.education || [];
    if (!items.length) { progress("education", "skip", "no history"); return { filled }; }
    const panels = await ensurePanels(section, "education-", items.length);
    for (let i = 0; i < Math.min(panels.length, items.length); i++) {
      const p = panels[i], e = items[i];
      if (fillText(p, "school", e.school)) filled++;
      if (await fillDropdown(p, "degree", e.degree_label, e.degree)) filled++;
      // field of study is a type-ahead multiselect on most tenants
      const fos = control(p, "field-of-study") || control(p, "fieldOfStudy") ||
                  (aid(p, "formField-fieldOfStudy") && aid(p, "formField-fieldOfStudy").querySelector("input"));
      if (fos && e.field_of_study && !(fos.value || "").trim() &&
          !p.querySelector("[data-automation-id='selectedItem']")) {
        fos.focus();
        setNative(fos, e.field_of_study);
        await sleep(700);
        const opt = bestOption(e.field_of_study) || visibleOptions()[0];
        if (opt) { realClick(opt); filled++; }
        else fos.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        done(fos);
        await sleep(150);
      }
      if (e.gpa) { if (fillText(p, "gpa", e.gpa)) filled++; }
      if (e.end_year) filled += fillDateGroup(p, "formField-endDate", null, e.end_year);
      await sleep(150);
    }
    progress("education", "done", `${Math.min(panels.length, items.length)} entr${items.length === 1 ? "y" : "ies"}`);
    return { filled };
  }

  async function fillSkills(history) {
    const section = findSection("skillsSection", /^skills$|add skills/i);
    if (!section) return null;
    progress("skills", "start");
    section.setAttribute("data-jpaf-owned", "1");
    const input = section.querySelector("input[type='text'], input:not([type])");
    if (!input) { progress("skills", "skip", "no input"); return { filled: 0 }; }
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
    const section = findSection("websiteSection", /websites?/i);
    if (!section) return null;
    const links = history.links || {};
    const urls = [links.portfolio, links.github, links.website].filter(Boolean);
    if (!urls.length) return { filled: 0 };
    progress("websites", "start");
    section.setAttribute("data-jpaf-owned", "1");
    let filled = 0;
    const panels = await ensurePanels(section, "websitePanelSet-", urls.length);
    for (let i = 0; i < Math.min(panels.length, urls.length); i++) {
      if (fillText(panels[i], "website", urls[i])) filled++;
    }
    // single bare input variant
    if (!panels.length && fillText(section, "website", urls[0])) filled++;
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
    const wrap = aid(document, "linkedinQuestion");
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

  // Which wizard sections exist on the current step (for the progress panel).
  window.__jpafWorkdaySections = function () {
    const out = [];
    if (findSection("workExperienceSection", /work experience/i)) out.push("work");
    if (findSection("educationSection", /education/i)) out.push("education");
    if (findSection("skillsSection", /^skills$|add skills/i)) out.push("skills");
    if (document.querySelector("input[type='file']")) out.push("resume");
    if (findSection("websiteSection", /websites?/i)) out.push("websites");
    if (isDisclosureStep()) out.push("identity");
    return out;
  };

  window.__jpafWorkdayRun = async function () {
    const stats = { filled: 0, sections: {} };
    if (!window.__jpafIsWorkday()) return stats;
    const history = await send({ cmd: "history" });
    if (!history || history.error) { progress("wizard", "fail", "backend offline"); return stats; }
    const ctx = {
      company: (location.hostname.split(".")[0] || "").replace(/[^a-z0-9]/gi, " "),
      title: ((document.querySelector("h1, h2") || {}).innerText || document.title || "").slice(0, 120),
    };
    const steps = [
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
