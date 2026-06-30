/* Injected on demand. Defines window.__jpafApply(plan) -> stats.
 * Sets values React/Vue-safely. NEVER submits the form. */
(() => {
  const norm = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

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
    const opt =
      [...el.options].find((o) => norm(o.value) === nv || norm(o.text) === nv) ||
      [...el.options].find((o) => { const t = norm(o.text); return t && (t.includes(nv) || nv.includes(t)); });
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
    const group = el.name ? document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`) : [el];
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

  window.__jpafApply = function (plan) {
    let filled = 0, review = 0, fileFlags = 0;
    (plan.fields || []).forEach((f) => {
      const el = document.querySelector(`[data-jpaf-id="${f.id}"]`);
      if (!el) return;
      if (f.source === "file") { mark(el, false); fileFlags++; review++; return; }
      if (f.value == null || f.value === "") { if (f.needs_review) { mark(el, false); review++; } return; }
      try {
        const tag = el.tagName.toLowerCase();
        const type = (el.type || "").toLowerCase();
        let ok;
        if (tag === "select") ok = fillSelect(el, f.value);
        else if (type === "radio" || type === "checkbox") ok = fillChoice(el, f.value);
        else { setNative(el, f.value); ok = true; }
        mark(el, ok && !f.needs_review);
        if (ok) filled++;
        if (f.needs_review || !ok) review++;
      } catch (e) {
        mark(el, false); review++;
      }
    });
    toast(
      `JobPilot filled ${filled} field(s)` +
      (fileFlags ? " · attach résumé manually" : "") +
      (review ? ` · ${review} need review` : "") +
      " — review & click Apply"
    );
    return { filled, needs_review: review, file_flags: fileFlags };
  };
})();
