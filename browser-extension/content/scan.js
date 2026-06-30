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
    let i = 0;
    nodes.forEach((el) => {
      const type = (el.type || el.tagName).toLowerCase();
      if (SKIP.has(type) || el.disabled || el.readOnly) return;
      const visible = type === "file" || el.getClientRects().length > 0;
      if (!visible) return;
      // group radios/checkboxes: only emit the first of a name-group
      if ((type === "radio" || type === "checkbox") && el.name) {
        if (out.some((f) => f.name === el.name && (f.type === "radio" || f.type === "checkbox"))) return;
      }
      const id = "f" + i++;
      el.setAttribute("data-jpaf-id", id);
      let options = null;
      if (el.tagName.toLowerCase() === "select") {
        options = [...el.options].map((o) => o.label || o.text || o.value).filter(Boolean);
      }
      out.push({ id, label: labelFor(el), name: el.name || el.id || "", type, options, required: !!el.required });
    });
    return out;
  };

  return window.__jpafScan();
})();
