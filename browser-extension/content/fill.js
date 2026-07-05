/* Injected on demand. Defines window.__jpafApply(plan) -> Promise<stats>.
 * Sets values React/Vue-safely. NEVER submits the form. */
(() => {
  const norm = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function visibleOptions() {
    return [...document.querySelectorAll("[role='option'], li[role='menuitem']")]
      .filter((o) => o.getClientRects().length > 0);
  }

  // Tenant vocabularies differ — Workday listbox options aren't visible until the
  // popup opens, so the smart matching has to happen HERE, at fill time (the plan
  // only carries the short answer like "Yes"/"Male"/"No"). Mirrors
  // autofill_mapper._match_choice so EEO answers resolve to verbose option text.
  const OPTION_SYNONYMS = {
    "mobile": ["cellular", "cell", "mobile phone"],
    "united states": ["united states of america", "usa"],
    "male": ["man"], "female": ["woman"],
    "heterosexual": ["straight"],
    "two or more races": ["two or more", "multiracial"],
  };
  const DECLINE_HINTS = ["decline", "do not wish", "dont wish", "prefer not",
                         "not to answer", "not wish", "choose not", "rather not"];

  // Choose the best option element for `value` among a list of {el, t:normText}.
  function chooseChoice(value, opts) {
    const nv = norm(value);
    const cands = [nv, ...(OPTION_SYNONYMS[nv] || [])];
    for (const c of cands) { const o = opts.find((x) => x.t === c); if (o) return o.el; }
    if (/^(decline|prefer not|do not wish|dont wish|i do not wish|i dont wish)/.test(nv)) {
      const o = opts.find((x) => DECLINE_HINTS.some((h) => x.t.includes(h)));
      if (o) return o.el;
    }
    if (nv === "yes" || nv === "no") {
      let o = opts.find((x) => x.t.split(" ")[0] === nv);
      if (!o && nv === "no") o = opts.find((x) => x.t.startsWith("not "));
      if (o) return o.el;
    }
    for (const c of cands) {
      const o = opts.find((x) => x.t && (x.t.includes(c) || c.includes(x.t)));
      if (o) return o.el;
    }
    const dw = nv.split(" ").filter((w) => w.length > 2);  // best token overlap
    let best = null, bn = 0;
    for (const x of opts) {
      const ow = new Set(x.t.split(" "));
      const k = dw.filter((w) => ow.has(w)).length;
      if (k > bn) { best = x.el; bn = k; }
    }
    return best;
  }

  function bestOption(value) {
    const opts = visibleOptions().map((el) => ({ el, t: norm(el.innerText) }));
    return chooseChoice(value, opts);
  }

  function realClick(el) {
    // pointer/mouse prelude for React-Select style widgets that act on mousedown
    for (const t of ["pointerdown", "mousedown", "pointerup", "mouseup"])
      el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, composed: true, view: window }));
    // native click() reliably crosses the isolated/main world boundary;
    // a synthetic MouseEvent("click") from a content script does not always.
    if (typeof el.click === "function") el.click();
    else el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, composed: true, view: window }));
  }

  // Workday-style <button aria-haspopup="listbox"> / role=combobox divs:
  // open the popup, click the matching [role=option].
  async function fillAriaSelect(el, value) {
    realClick(el);
    await sleep(400);
    let opt = bestOption(value);
    if (!opt) { await sleep(500); opt = bestOption(value); }  // slow portals
    if (opt) {
      realClick(opt);
      await sleep(150);
      return true;
    }
    // close whatever we opened so we don't leave the page in a weird state
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    if (document.body) document.body.click();  // native click crosses worlds
    return false;
  }

  // Autocomplete/React-Select inputs: type the value, then pick the matching
  // suggestion if one appears (typed text alone often isn't committed).
  async function fillCombo(el, value) {
    el.focus();
    setNative(el, value);
    await sleep(500);
    const opt = bestOption(value);
    if (opt) { realClick(opt); await sleep(150); return true; }
    // no suggestion list — the typed text may still be valid; leave it
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    return true;
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

  function fillSelect(el, value) {
    const nv = norm(value);
    let opt = [...el.options].find((o) => norm(o.value) === nv || norm(o.text) === nv);
    if (!opt) {  // EEO-tolerant matching over the option texts
      const chosen = chooseChoice(value, [...el.options].map((o) => ({ el: o, t: norm(o.text) })));
      opt = chosen || null;
    }
    if (opt) {
      el.value = opt.value;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    return false;
  }

  function labelText(r) {
    if (r.id) {
      const l = document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
      if (l) return l.innerText;
    }
    const w = r.closest("label");
    return w ? w.innerText : "";
  }

  function fillChoice(el, value) {
    const nv = norm(value);
    const group = el.name ? [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)] : [el];
    // exact value/label, or yes/no synonyms
    for (const r of group) {
      const lbl = norm(labelText(r));
      const rv = norm(r.value);
      if (rv === nv || lbl === nv ||
          (nv === "yes" && /^(yes|true|1)$/.test(rv)) || (nv === "no" && /^(no|false|0)$/.test(rv))) {
        if (!r.checked) r.click();
        r.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
      }
    }
    // verbose EEO radios: match the option whose label best fits (e.g. "Yes, I
    // have a disability…" for value "Yes"; "Not Hispanic or Latino" for "No").
    if (group.length > 1) {
      const chosen = chooseChoice(value, group.map((r) => ({ el: r, t: norm(labelText(r)) })));
      if (chosen) {
        if (!chosen.checked) chosen.click();
        chosen.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
      }
    }
    if (el.type === "checkbox" && (nv === "yes" || nv === "true")) {
      if (!el.checked) el.click();
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    return false;
  }

  function mark(el, ok) {
    el.style.outline = ok ? "2px solid #0a7e07" : "2px solid #c08a00";
    el.style.outlineOffset = "1px";
  }

  function toast(msg) {
    let t = document.getElementById("__jpaf_toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "__jpaf_toast";
      t.style.cssText =
        "position:fixed;z-index:2147483647;right:16px;bottom:16px;background:#0b1220;color:#fff;" +
        "font:600 13px/1.45 system-ui,sans-serif;padding:12px 16px;border-radius:12px;" +
        "box-shadow:0 14px 40px rgba(0,0,0,.45);max-width:320px";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    clearTimeout(t._h);
    t._h = setTimeout(() => t.remove(), 7000);
  }

  window.__jpafApply = async function (plan) {
    let filled = 0, review = 0, fileFlags = 0;
    const metaById = new Map((plan._scanMeta || []).map((m) => [m.id, m]));
    // Sequential (not forEach) — ARIA dropdowns open popups that must close
    // before the next field is touched.
    for (const f of plan.fields || []) {
      const el = document.querySelector(`[data-jpaf-id="${f.id}"]`);
      if (!el) continue;
      if (f.source === "file") { mark(el, false); fileFlags++; review++; continue; }
      if (f.value == null || f.value === "") { if (f.needs_review) { mark(el, false); review++; } continue; }
      try {
        const tag = el.tagName.toLowerCase();
        const type = (el.type || "").toLowerCase();
        const isAria = !["input", "select", "textarea"].includes(tag) ||
                       (metaById.get(f.id) || {}).type === "aria_select";
        const isCombo = tag === "input" && (
          el.getAttribute("role") === "combobox" ||
          el.getAttribute("aria-autocomplete") === "list" ||
          el.getAttribute("aria-haspopup") === "listbox" ||
          (metaById.get(f.id) || {}).combo);
        let ok;
        if (isAria) ok = await fillAriaSelect(el, f.value);
        else if (tag === "select") ok = fillSelect(el, f.value);
        else if (type === "radio" || type === "checkbox") ok = fillChoice(el, f.value);
        else if (isCombo) ok = await fillCombo(el, f.value);
        else { setNative(el, f.value); ok = true; }
        mark(el, ok && !f.needs_review);
        if (ok) filled++;
        if (f.needs_review || !ok) review++;
      } catch (e) {
        mark(el, false); review++;
      }
    }
    toast(
      `JobPilot filled ${filled} field(s)` +
      (fileFlags ? " · attach résumé manually" : "") +
      (review ? ` · ${review} need review` : "") +
      " — review & click Apply"
    );
    return { filled, needs_review: review, file_flags: fileFlags };
  };
})();
