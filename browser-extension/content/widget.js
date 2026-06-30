/* JobPilot Autofill — in-page floating button (Simplify / JobWright style).
 *
 * Auto-loads on every page (declared in manifest content_scripts) but only
 * shows a floating pill when the page looks like a job application. Clicking
 * the pill scans the form, asks the local JobPilot service worker for a
 * fill-plan, and fills the fields. It NEVER submits — you review and click
 * Apply yourself.
 *
 * Runs in the extension's isolated world alongside scan.js + fill.js, so it
 * calls their globals window.__jpafScan() / window.__jpafApply() directly.
 */
(() => {
  if (window.__jpafWidgetLoaded) return;
  window.__jpafWidgetLoaded = true;

  // Known ATS hosts — always offer the pill on these.
  const ATS = [
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "workday.com",
    "smartrecruiters.com", "icims.com", "taleo.net", "jobvite.com", "breezy.hr",
    "workable.com", "bamboohr.com", "successfactors.com", "applytojob.com",
    "rippling.com", "paylocity.com", "dayforcehcm.com", "jazz.co", "jobs.",
    "careers.", "apply.",
  ];

  let hostEl = null;        // the shadow-DOM host element (truthy once built)
  let root = null;          // shadow root
  let dismissed = false;    // user hid it on this page

  function visibleInputs() {
    return [...document.querySelectorAll("input, textarea, select")].filter((e) => {
      const t = (e.type || "").toLowerCase();
      return !["hidden", "submit", "button", "image", "reset"].includes(t) &&
        !e.disabled && e.getClientRects().length > 0;
    });
  }

  function looksLikeApplication() {
    const host = location.hostname || "";
    if (host === "127.0.0.1" || host === "localhost") return false; // not on the dashboard itself
    if (ATS.some((d) => host.includes(d))) return true;

    const inputs = visibleInputs();
    if (inputs.length < 4) return false;
    const hay = (document.body ? document.body.innerText : "").toLowerCase().slice(0, 20000);
    const hasEmail = inputs.some((e) =>
      (e.type || "") === "email" || /e-?mail/i.test((e.name || "") + (e.id || "")));
    const hasFile = !!document.querySelector('input[type="file"]');
    const applyish = /(apply|application|résumé|resume|\bcv\b|cover letter|first name|work experience|why do you want|years of experience)/.test(hay);
    return (hasEmail && (hasFile || applyish)) || (inputs.length >= 6 && applyish);
  }

  function send(msg) {
    return new Promise((resolve) => {
      try { chrome.runtime.sendMessage(msg, resolve); }
      catch (e) { resolve({ error: "Extension reloaded — refresh the page." }); }
    });
  }

  function build() {
    hostEl = document.createElement("div");
    hostEl.id = "__jpaf_host";
    // all:initial first, then our positioning overrides it (last wins in one declaration)
    hostEl.style.cssText =
      "all:initial;position:fixed;z-index:2147483647;right:20px;bottom:20px;";
    root = hostEl.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        *{ box-sizing:border-box; margin:0; font-family:'Segoe UI',system-ui,-apple-system,sans-serif; }
        .dock{ display:flex; flex-direction:column-reverse; align-items:flex-end; gap:8px; }
        .bar{ display:flex; align-items:center; gap:8px; background:#0b1220; color:#fff;
          border:1px solid rgba(255,255,255,.14); border-radius:999px; padding:6px 8px 6px 12px;
          box-shadow:0 16px 44px -12px rgba(0,0,0,.6); }
        .dot{ width:8px; height:8px; border-radius:50%; background:#94a3b8; flex:none; }
        .dot.up{ background:#22c55e; box-shadow:0 0 0 3px rgba(34,197,94,.22); }
        .dot.down{ background:#ef4444; }
        .go{ border:0; cursor:pointer; color:#fff; font-weight:700; font-size:13px;
          background:linear-gradient(135deg,#3b82f6,#6d28d9); border-radius:999px; padding:8px 15px; }
        .go:hover{ filter:brightness(1.08); }
        .go:disabled{ opacity:.65; cursor:default; }
        .more{ border:0; cursor:pointer; background:rgba(255,255,255,.10); color:#fff; font-size:12px;
          width:26px; height:26px; border-radius:50%; line-height:1; }
        .more:hover{ background:rgba(255,255,255,.2); }
        .panel{ width:240px; background:#0b1220; color:#fff; border:1px solid rgba(255,255,255,.14);
          border-radius:14px; padding:12px 13px; box-shadow:0 16px 44px -12px rgba(0,0,0,.6);
          display:flex; flex-direction:column; gap:9px; }
        .panel[hidden]{ display:none; }
        label{ font-size:11px; color:#9fb0cc; font-weight:600; }
        select{ width:100%; padding:7px 9px; border-radius:9px; border:1px solid rgba(255,255,255,.16);
          background:#111a2e; color:#fff; font-size:12.5px; outline:none; }
        .res{ font-size:12px; color:#cdd8ec; min-height:16px; line-height:1.45; }
        .res .amber{ color:#fbbf24; } .res .err{ color:#fca5a5; }
        .hide{ border:0; background:transparent; color:#7e8aa6; cursor:pointer; font-size:11.5px;
          text-align:left; padding:0; }
        .hide:hover{ color:#cdd8ec; text-decoration:underline; }
        .ttl{ font-weight:800; font-size:12.5px; letter-spacing:-.2px; }
      </style>
      <div class="dock">
        <div class="bar">
          <span class="dot" id="dot" title="JobPilot status"></span>
          <button class="go" id="go">⚡ Autofill</button>
          <button class="more" id="more" title="Options">⌄</button>
        </div>
        <div class="panel" id="panel" hidden>
          <div class="ttl">JobPilot Autofill</div>
          <div>
            <label>Résumé</label>
            <select id="resume">
              <option value="auto">Auto (match the role)</option>
              <option value="ai">AI / data résumé</option>
              <option value="bt">Behavioral Technician résumé</option>
            </select>
          </div>
          <div class="res" id="res">Fills the form — never submits. You review &amp; click Apply.</div>
          <button class="hide" id="hide">Hide on this page</button>
        </div>
      </div>`;
    document.documentElement.appendChild(hostEl);

    root.getElementById("go").onclick = run;
    root.getElementById("more").onclick = () => {
      const p = root.getElementById("panel");
      p.hidden = !p.hidden;
    };
    root.getElementById("hide").onclick = () => {
      dismissed = true;
      if (hostEl) hostEl.remove();
    };
    health();
  }

  async function health() {
    const dot = root && root.getElementById("dot");
    if (!dot) return;
    const h = await send({ cmd: "health" });
    if (h && h.ok) {
      dot.className = "dot up";
      dot.title = h.ollama_up ? "JobPilot + AI ready" : "JobPilot ready (AI off — templates)";
    } else {
      dot.className = "dot down";
      dot.title = "JobPilot offline — start it, then retry";
    }
  }

  function setRes(html) {
    const r = root && root.getElementById("res");
    if (r) r.innerHTML = html;
  }

  async function run() {
    const go = root.getElementById("go");
    go.disabled = true;
    go.textContent = "Filling…";
    try {
      const fields = window.__jpafScan ? window.__jpafScan() : [];
      if (!fields.length) {
        root.getElementById("panel").hidden = false;
        setRes(`<span class="err">No application fields detected here.</span>`);
        return;
      }
      const ctx = {
        url: location.href,
        title: document.title,
        h1: (document.querySelector("h1") || {}).innerText || "",
        text: document.body ? document.body.innerText.slice(0, 4000) : "",
      };
      const resumePref = root.getElementById("resume").value;
      const plan = await send({ cmd: "plan", fields, ctx, resumePref });
      if (!plan || plan.error) {
        root.getElementById("panel").hidden = false;
        setRes(`<span class="err">${(plan && plan.error) || "Failed to get a plan."}</span>`);
        return;
      }
      const stats = window.__jpafApply ? window.__jpafApply(plan) : { filled: 0 };
      go.textContent = `✓ ${stats.filled || 0} filled`;
      root.getElementById("panel").hidden = false;
      setRes(
        `<b>Filled ${stats.filled || 0}</b> field(s)` +
        (stats.file_flags ? ` · attach résumé manually` : "") +
        (stats.needs_review ? ` · <span class="amber">${stats.needs_review} need review</span>` : "") +
        (plan._offline ? ` · offline mode` : "")
      );
      setTimeout(() => { if (root.getElementById("go") === go) go.textContent = "⚡ Autofill"; }, 4000);
    } catch (e) {
      root.getElementById("panel").hidden = false;
      setRes(`<span class="err">${e.message || e}</span>`);
    } finally {
      go.disabled = false;
    }
  }

  function maybeShow() {
    if (dismissed || hostEl) return;
    if (looksLikeApplication()) build();
  }

  // Initial check + watch for SPA / dynamically-loaded forms (debounced).
  maybeShow();
  let timer = null;
  const obs = new MutationObserver(() => {
    if (dismissed || hostEl) return;
    clearTimeout(timer);
    timer = setTimeout(maybeShow, 800);
  });
  try { obs.observe(document.documentElement, { childList: true, subtree: true }); } catch (e) { /* noop */ }
})();
