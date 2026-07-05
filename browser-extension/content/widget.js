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
    // In sub-frames (all_frames:true) only surface the pill in frames big
    // enough to hold a real application form — never in ad/captcha iframes.
    if (window.top !== window && (window.innerWidth < 400 || window.innerHeight < 300)) return false;
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
    // all:initial first, then our positioning overrides it (last wins in one declaration).
    // Bottom-right corner — clear of Simplify/JobRight side panels that own the
    // vertically-centered right edge.
    hostEl.style.cssText =
      "all:initial;position:fixed;z-index:2147483647;right:14px;bottom:18px;";
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
        .steps{ display:flex; flex-direction:column; gap:5px; }
        .steps[hidden]{ display:none; }
        .step{ display:flex; align-items:center; gap:8px; font-size:12.5px; color:#cdd8ec; }
        .step .ic{ width:16px; height:16px; border-radius:50%; flex:none; display:flex;
          align-items:center; justify-content:center; font-size:10px; font-weight:800;
          background:rgba(255,255,255,.12); color:#9fb0cc; }
        .step.run .ic{ background:#1d4ed8; color:#fff; animation:jp-pulse 1s infinite; }
        .step.done .ic{ background:#16a34a; color:#fff; }
        .step.skip .ic{ background:rgba(255,255,255,.10); color:#7e8aa6; }
        .step.fail .ic{ background:#b45309; color:#fff; }
        .step .nt{ margin-left:auto; font-size:11px; color:#7e8aa6; max-width:90px;
          overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        @keyframes jp-pulse{ 50%{ opacity:.55; } }
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
          <div class="steps" id="steps" hidden></div>
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

  const SECTION_LABELS = {
    work: "Work experience", education: "Education", skills: "Skills",
    resume: "Résumé/CV", websites: "Websites", linkedin: "LinkedIn",
    source: "How you heard", identity: "Disclosures & identity",
    fields: "Contact & questions",
  };

  function stepsInit(names) {
    const box = root.getElementById("steps");
    if (!box) return;
    box.hidden = false;
    box.innerHTML = names.map((n) =>
      `<div class="step" data-sec="${n}"><span class="ic">○</span><span>${SECTION_LABELS[n] || n}</span><span class="nt"></span></div>`
    ).join("");
  }

  function stepSet(name, status, note) {
    const box = root.getElementById("steps");
    if (!box) return;
    let row = box.querySelector(`[data-sec="${name}"]`);
    if (!row) {  // section discovered mid-run
      box.insertAdjacentHTML("beforeend",
        `<div class="step" data-sec="${name}"><span class="ic">○</span><span>${SECTION_LABELS[name] || name}</span><span class="nt"></span></div>`);
      row = box.querySelector(`[data-sec="${name}"]`);
    }
    row.className = "step " + ({ start: "run", done: "done", skip: "skip", fail: "fail" }[status] || "");
    row.querySelector(".ic").textContent = { start: "…", done: "✓", skip: "–", fail: "!" }[status] || "○";
    if (note) row.querySelector(".nt").textContent = note;
  }

  window.addEventListener("jpaf-progress", (e) => {
    const d = e.detail || {};
    if (d.section) stepSet(d.section, d.status, d.note);
  });

  async function run() {
    const go = root.getElementById("go");
    go.disabled = true;
    go.textContent = "Filling…";
    try {
      // Wizard ATSes (Workday): structured sections first, with a live checklist.
      let wizardFilled = 0;
      const isWizard = window.__jpafIsWorkday && window.__jpafIsWorkday();
      if (isWizard && window.__jpafWorkdayRun) {
        root.getElementById("panel").hidden = false;
        const secs = (window.__jpafWorkdaySections && window.__jpafWorkdaySections()) || [];
        stepsInit([...secs, "fields"]);
        const ws = await window.__jpafWorkdayRun();
        wizardFilled = (ws && ws.filled) || 0;
      }

      const fields = window.__jpafScan ? window.__jpafScan() : [];
      if (!fields.length && !wizardFilled) {
        root.getElementById("panel").hidden = false;
        setRes(`<span class="err">No application fields detected here.</span>`);
        return;
      }
      if (!fields.length) {
        stepSet("fields", "skip", "none left");
        go.textContent = `✓ ${wizardFilled} filled`;
        setRes(`<b>Filled ${wizardFilled}</b> field(s) — review &amp; continue. Never submits.`);
        return;
      }
      if (isWizard) stepSet("fields", "start");
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
      plan._scanMeta = fields;  // lets fill.js know which ids are aria/combo widgets
      const stats = window.__jpafApply ? await window.__jpafApply(plan) : { filled: 0 };
      if (isWizard) stepSet("fields", "done", `${stats.filled || 0} filled`);
      const total = (stats.filled || 0) + wizardFilled;
      go.textContent = `✓ ${total} filled`;
      root.getElementById("panel").hidden = false;
      setRes(
        `<b>Filled ${total}</b> field(s)` +
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
    if (dismissed) return;
    // SPA frameworks (React hydration, document.write) can rip our host out of
    // the DOM after we've built it — detect that and rebuild.
    if (hostEl && !hostEl.isConnected) { hostEl = null; root = null; }
    if (hostEl) return;
    if (looksLikeApplication()) build();
  }

  // Initial check + watch for SPA / dynamically-loaded forms (debounced).
  maybeShow();
  let timer = null;
  const obs = new MutationObserver(() => {
    if (dismissed) return;
    if (hostEl && hostEl.isConnected) return;
    clearTimeout(timer);
    timer = setTimeout(maybeShow, 800);
  });
  try { obs.observe(document.documentElement, { childList: true, subtree: true }); } catch (e) { /* noop */ }
  // Belt-and-suspenders: some wipes don't fire useful mutations by the time we
  // observe. A cheap periodic check guarantees the pill comes back.
  setInterval(maybeShow, 2000);
})();
