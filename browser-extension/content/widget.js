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
  let disabled = false;     // global kill switch — persisted, all sites
  const DISABLE_KEY = "jpaf_disabled";

  function visibleInputs() {
    return [...document.querySelectorAll("input, textarea, select")].filter((e) => {
      const t = (e.type || "").toLowerCase();
      return !["hidden", "submit", "button", "image", "reset"].includes(t) &&
        !e.disabled && e.getClientRects().length > 0;
    });
  }

  function looksLikeApplication() {
    if (window.__jpafForceApp) return true;  // test/debug hook
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
        .rev{ display:flex; flex-direction:column; gap:4px; border-top:1px solid rgba(255,255,255,.12);
          padding-top:8px; max-height:150px; overflow-y:auto; }
        .rev[hidden]{ display:none; }
        .rttl{ font-size:11px; color:#fbbf24; font-weight:700; }
        .rl{ border:0; cursor:pointer; background:rgba(251,191,36,.10); color:#fde68a; font-size:11.5px;
          text-align:left; padding:5px 8px; border-radius:7px; overflow:hidden; text-overflow:ellipsis;
          white-space:nowrap; }
        .rl:hover{ background:rgba(251,191,36,.22); }
        .rfile{ display:flex; align-items:center; gap:7px; font-size:11.5px; margin-top:5px; }
        .rfname{ flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#cdd8ec; }
        .rswap{ border:0; cursor:pointer; background:rgba(255,255,255,.10); color:#cdd8ec;
          font-size:11px; padding:3px 9px; border-radius:7px; flex:none; }
        .rswap:hover{ background:rgba(255,255,255,.2); }
        .rswap[hidden]{ display:none; }
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
            <div class="rfile">
              <span class="rfname" id="rfname">…</span>
              <button class="rswap" id="rswap" title="Upload a newer résumé PDF — it becomes the file autofill attaches">Replace</button>
              <input type="file" id="rfinput" accept="application/pdf,.pdf" hidden>
            </div>
          </div>
          <div class="res" id="res">Fills the form — never submits. You review &amp; click Apply.</div>
          <div class="rev" id="review" hidden></div>
          <button class="hide" id="hide">Hide on this page</button>
          <button class="hide" id="off" title="Stop the pill from appearing on any site — nothing fills until you re-enable it from the JobPilot toolbar popup.">Turn off autofill (all sites)</button>
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
    root.getElementById("off").onclick = () => {
      try { chrome.storage.local.set({ [DISABLE_KEY]: true }); } catch (e) { /* no chrome.storage (tests) */ }
      setDisabled(true);
    };
    root.getElementById("resume").onchange = refreshResumeMeta;
    root.getElementById("rswap").onclick = () => root.getElementById("rfinput").click();
    root.getElementById("rfinput").onchange = uploadResume;
    health();
    refreshResumeMeta();
  }

  // Show WHICH résumé file autofill will attach, so it can be swapped as the
  // résumé iterates (a company-matched tailored résumé still overrides it).
  async function refreshResumeMeta() {
    const nameEl = root && root.getElementById("rfname");
    if (!nameEl) return;
    const pref = root.getElementById("resume").value;
    const m = await send({ cmd: "resume_meta", resumePref: pref });
    const swap = root.getElementById("rswap");
    if (m && !m.error) {
      const when = m.uploaded_at ? ` · ${String(m.uploaded_at).slice(0, 10)}` : "";
      nameEl.textContent = `📄 ${m.original_name || m.serve_name}${when}`;
      nameEl.title = `Attaches as ${m.serve_name}. ${m.note || ""}`;
      swap.hidden = m.source !== "profile_pdf";
    } else {
      nameEl.textContent = "📄 résumé: backend offline";
      nameEl.title = "";
      swap.hidden = true;
    }
  }

  async function uploadResume(ev) {
    const f = ev.target.files && ev.target.files[0];
    ev.target.value = "";
    if (!f) return;
    const nameEl = root.getElementById("rfname");
    if (!/pdf$/i.test(f.name) && f.type !== "application/pdf") {
      nameEl.textContent = "📄 only PDF files supported";
      return;
    }
    nameEl.textContent = `📄 uploading ${f.name}…`;
    const b64 = await new Promise((resolve, reject) => {
      const rd = new FileReader();
      rd.onload = () => resolve(String(rd.result).split(",")[1] || "");
      rd.onerror = reject;
      rd.readAsDataURL(f);
    });
    const pref = root.getElementById("resume").value === "bt" ? "bt" : "ai";
    const r = await send({ cmd: "resume_upload", resumePref: pref, filename: f.name, b64 });
    if (r && r.ok) {
      nameEl.textContent = `📄 ${r.original_name} ✓`;
      setTimeout(refreshResumeMeta, 2500);
    } else {
      nameEl.textContent = `📄 upload failed: ${(r && r.error) || "backend offline"}`;
    }
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
    contact: "Contact info", work: "Work experience", education: "Education",
    skills: "Skills", resume: "Résumé/CV", websites: "Websites", linkedin: "LinkedIn",
    source: "How you heard", identity: "Disclosures & identity",
    fields: "Contact & questions", page: "Next page",
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

  // JobRight-style review list: fields the fill flagged, click to jump there.
  function renderReview(items) {
    const box = root && root.getElementById("review");
    if (!box) return;
    if (!items.length) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
    box.innerHTML = `<div class="rttl">Needs your review (${items.length})</div>` +
      items.slice(0, 10).map((r) =>
        `<button class="rl" data-rid="${esc(r.id)}" title="${esc(r.label)}">${esc(r.label)}</button>`).join("");
    box.querySelectorAll(".rl").forEach((b) => {
      b.onclick = () => {
        const el = document.querySelector(`[data-jpaf-id="${b.dataset.rid}"]`);
        if (!el) return;
        const t = el.closest(".select__control") || el;
        t.scrollIntoView({ behavior: "smooth", block: "center" });
        const old = t.style.outline;
        t.style.outline = "3px solid #f59e0b";
        setTimeout(() => { t.style.outline = old || "2px solid #c08a00"; }, 1400);
        try { el.focus({ preventScroll: true }); } catch (e) { /* non-focusable */ }
      };
    });
  }

  function companyFromPage() {
    const h = location.hostname, seg = location.pathname.split("/").filter(Boolean);
    if (/greenhouse\.io$/i.test(h) || /lever\.co$/i.test(h) || /ashbyhq\.com$/i.test(h)) return seg[0] || "";
    if (/myworkdayjobs\.com$|myworkdaysite\.com$/i.test(h)) return h.split(".")[0] || "";
    return "";
  }

  // Fill the current page/step: wizard engine first (Workday / Greenhouse own
  // their structured sections), then the flat scan → plan → fill pass.
  async function fillCurrentPage(resumePref, mode) {
    let wizardFilled = 0;
    if (mode.wizard === "workday" && window.__jpafWorkdayRun) {
      const ws = await window.__jpafWorkdayRun();
      wizardFilled = (ws && ws.filled) || 0;
    } else if (mode.wizard === "greenhouse" && window.__jpafGreenhouseRun) {
      const gs = await window.__jpafGreenhouseRun();
      wizardFilled = (gs && gs.filled) || 0;
    }

    const fields = window.__jpafScan ? window.__jpafScan() : [];
    if (!fields.length) {
      if (mode.steps) stepSet("fields", "skip", "none left");
      return { filled: wizardFilled, needs_review: 0, file_flags: 0, scanned: 0, wizard: wizardFilled };
    }
    if (mode.steps) stepSet("fields", "start");
    const ctx = {
      url: location.href,
      title: document.title,
      h1: (document.querySelector("h1") || {}).innerText || "",
      text: document.body ? document.body.innerText.slice(0, 4000) : "",
      company: companyFromPage(),
    };
    const plan = await send({ cmd: "plan", fields, ctx, resumePref });
    if (!plan || plan.error) throw new Error((plan && plan.error) || "Failed to get a plan.");
    plan._scanMeta = fields;  // lets fill.js know which ids are aria/combo widgets
    plan._ctx = { company: ctx.company, title: ctx.h1 || ctx.title };
    const stats = window.__jpafApply ? await window.__jpafApply(plan) : { filled: 0 };
    if (mode.steps) stepSet("fields", "done", `${stats.filled || 0} filled`);
    return {
      filled: (stats.filled || 0) + wizardFilled,
      needs_review: stats.needs_review || 0,
      file_flags: stats.file_flags || 0,
      scanned: fields.length,
      offline: !!plan._offline,
      wizard: wizardFilled,
      review_fields: stats.review_fields || [],
    };
  }

  async function run() {
    const go = root.getElementById("go");
    go.disabled = true;
    go.textContent = "Filling…";
    try {
      const isWD = window.__jpafIsWorkday && window.__jpafIsWorkday();
      const ghSecs = (!isWD && window.__jpafIsGreenhouse && window.__jpafIsGreenhouse() &&
                      window.__jpafGreenhouseSections) ? window.__jpafGreenhouseSections() : [];
      const mode = {
        wizard: isWD ? "workday" : (ghSecs.length ? "greenhouse" : null),
        steps: isWD || ghSecs.length > 0,
      };
      if (mode.steps) {
        root.getElementById("panel").hidden = false;
        const secs = isWD
          ? ((window.__jpafWorkdaySections && window.__jpafWorkdaySections()) || [])
          : ghSecs;
        stepsInit([...secs, "fields"]);
      }
      const resumePref = root.getElementById("resume").value;

      // Fill this page; on Workday keep advancing (Next / Save and Continue —
      // NEVER Submit or the review step) and filling each new step.
      let total = 0, review = 0, fileFlags = 0, offline = false, sawAny = 0, pages = 0;
      const reviewItems = [];
      const MAX_PAGES = 7;
      while (true) {
        const r = await fillCurrentPage(resumePref, mode);
        total += r.filled; review += r.needs_review; fileFlags += r.file_flags;
        reviewItems.push(...(r.review_fields || []));
        offline = offline || !!r.offline;
        sawAny += r.scanned + r.filled;
        pages++;
        if (!isWD || pages >= MAX_PAGES || !window.__jpafWorkdayNext) break;
        const nxt = await window.__jpafWorkdayNext();
        if (!nxt || !nxt.clicked) {
          if (nxt && /^at-/.test(nxt.reason || "")) stepSet("page", "done", "review step — your turn");
          else if (nxt && nxt.reason === "validation-errors") stepSet("page", "fail", "fix highlighted fields");
          break;
        }
        stepSet("page", "done", nxt.label || "next");
        const secs = (window.__jpafWorkdaySections && window.__jpafWorkdaySections()) || [];
        stepsInit([...secs, "fields"]);
      }

      if (!sawAny) {
        root.getElementById("panel").hidden = false;
        setRes(`<span class="err">No application fields detected here.</span>`);
        return;
      }
      go.textContent = `✓ ${total} filled`;
      root.getElementById("panel").hidden = false;
      setRes(
        `<b>Filled ${total}</b> field(s)` +
        (pages > 1 ? ` across ${pages} pages` : "") +
        (fileFlags ? ` · attach file manually` : "") +
        (review ? ` · <span class="amber">${review} need review</span>` : "") +
        (offline ? ` · offline mode` : "")
      );
      renderReview(reviewItems);
      setTimeout(() => { if (root.getElementById("go") === go) go.textContent = "⚡ Autofill"; }, 4000);
    } catch (e) {
      root.getElementById("panel").hidden = false;
      setRes(`<span class="err">${e.message || e}</span>`);
    } finally {
      go.disabled = false;
    }
  }

  function maybeShow() {
    if (disabled || dismissed) return;
    // SPA frameworks (React hydration, document.write) can rip our host out of
    // the DOM after we've built it — detect that and rebuild.
    if (hostEl && !hostEl.isConnected) { hostEl = null; root = null; }
    if (hostEl) return;
    if (looksLikeApplication()) build();
  }

  // Kill switch: flipping it off tears the pill down everywhere; flipping it
  // back on (from the toolbar popup) rebuilds it live — no page refresh.
  function setDisabled(off) {
    disabled = !!off;
    if (disabled) {
      if (hostEl) { try { hostEl.remove(); } catch (e) { /* already gone */ } }
      hostEl = null; root = null;
    } else {
      maybeShow();
    }
  }

  // Initial check — respect the persisted switch before first paint, then
  // watch for SPA / dynamically-loaded forms (debounced).
  try {
    chrome.storage.local.get(DISABLE_KEY, (v) => setDisabled(!!(v && v[DISABLE_KEY])));
    chrome.storage.onChanged.addListener((ch, area) => {
      if (area === "local" && ch[DISABLE_KEY]) setDisabled(!!ch[DISABLE_KEY].newValue);
    });
  } catch (e) { maybeShow(); }  // no chrome.storage (tests) — default on
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
