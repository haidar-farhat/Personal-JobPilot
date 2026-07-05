/* Injected on demand. Defines window.__jpafScan() and returns the field list
 * (the file's completion value is what chrome.scripting.executeScript returns). */
(() => {
  const txt = (s) => (s || "").replace(/\s+/g, " ").trim();

  function labelFor(el) {
    if (el.getAttribute("aria-label")) return txt(el.getAttribute("aria-label"));
    const lblBy = el.getAttribute("aria-labelledby");
    if (lblBy) {
      const n = document.getElementById(lblBy);
      if (n) return txt(n.innerText);
    }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && txt(l.innerText)) return txt(l.innerText);
    }
    const wrap = el.closest("label");
    if (wrap && txt(wrap.innerText)) return txt(wrap.innerText);
    // preceding label-ish sibling
    let p = el.previousElementSibling;
    let hops = 0;
    while (p && hops < 3) {
      if (/^(label|span|div|p|legend)$/i.test(p.tagName) && txt(p.innerText)) return txt(p.innerText);
      p = p.previousElementSibling; hops++;
    }
    const par = el.parentElement;
    if (par) {
      const lab = par.querySelector("label, legend");
      if (lab && txt(lab.innerText)) return txt(lab.innerText);
    }
    if (el.placeholder) return txt(el.placeholder);
    return el.name || el.id || "";
  }

  window.__jpafScan = function () {
    const nodes = document.querySelectorAll("input, select, textarea");
    const SKIP = new Set(["hidden", "submit", "button", "image", "reset"]);
    const out = [];
    const seen = new Set();  // elements already emitted by THIS scan
    let i = 0;
    nodes.forEach((el) => {
      const type = (el.type || el.tagName).toLowerCase();
      if (SKIP.has(type) || el.disabled || el.readOnly) return;
      // fields the Workday wizard engine already handled (or owns the section of)
      if (el.getAttribute("data-jpaf-done") || el.closest("[data-jpaf-owned]")) return;
      const visible = type === "file" || el.getClientRects().length > 0;
      if (!visible) return;
      // group radios/checkboxes: only emit the first of a name-group
      if ((type === "radio" || type === "checkbox") && el.name) {
        if (out.some((f) => f.name === el.name && (f.type === "radio" || f.type === "checkbox"))) return;
      }
      const id = "f" + i++;
      el.setAttribute("data-jpaf-id", id);
      seen.add(el);
      let options = null;
      if (el.tagName.toLowerCase() === "select") {
        options = [...el.options].map((o) => o.label || o.text || o.value).filter(Boolean);
      }
      // React-Select / autocomplete inputs need a type-then-pick fill
      const combo = el.getAttribute("role") === "combobox" ||
                    el.getAttribute("aria-autocomplete") === "list" ||
                    el.getAttribute("aria-haspopup") === "listbox";
      out.push({ id, label: labelFor(el), name: el.name || el.id || "", type,
                 options, required: !!el.required, ...(combo ? { combo: true } : {}) });
    });

    // ARIA dropdowns that are NOT native selects — Workday and other React UIs
    // render these as <button aria-haspopup="listbox"> or role=combobox divs.
    const ariaNodes = document.querySelectorAll(
      "button[aria-haspopup='listbox'], div[aria-haspopup='listbox'], " +
      "[role='combobox']:not(input):not(select):not(textarea)"
    );
    ariaNodes.forEach((el) => {
      if (el.getClientRects().length === 0 || el.getAttribute("aria-disabled") === "true") return;
      if (el.getAttribute("data-jpaf-done") || el.closest("[data-jpaf-owned]")) return;
      // dedupe within THIS scan only — a stale data-jpaf-id from an earlier
      // scan must not exclude the element (scan.js auto-runs once at inject)
      if (seen.has(el)) return;
      const id = "f" + i++;
      el.setAttribute("data-jpaf-id", id);
      seen.add(el);
      out.push({ id, label: labelFor(el),
                 name: el.getAttribute("name") || el.getAttribute("data-automation-id") || el.id || "",
                 type: "aria_select", options: null, required: el.getAttribute("aria-required") === "true" });
    });
    return out;
  };

  return window.__jpafScan();
})();
