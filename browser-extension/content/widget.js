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
        /* One light card, one green action. Everything secondary lives inside
           the panel so the resting state is a single button, not a toolbar. */
        *{ box-sizing:border-box; margin:0;
           font-family:-apple-system,'Segoe UI',Inter,Roboto,system-ui,sans-serif; }
        :host{ --g:#05a67a; --g2:#04916a; --bg:#fff; --fg:#0f172a; --mut:#64748b;
               --bd:#e4e8ee; --sub:#f6f8fa; --amb:#b45309; --ambg:#fff8ec; --ambd:#fde3b8; }
        @media (prefers-color-scheme:dark){
          :host{ --bg:#151a22; --fg:#e9edf3; --mut:#98a3b3; --bd:#2a323d; --sub:#1d232c;
                 --amb:#f0b45e; --ambg:#2a2113; --ambd:#4a3a1c; }
        }
        .dock{ display:flex; flex-direction:column-reverse; align-items:flex-end; gap:10px; }
        .bar{ display:flex; align-items:center; gap:6px; background:var(--bg);
          border:1px solid var(--bd); border-radius:999px; padding:5px 5px 5px 12px;
          box-shadow:0 6px 24px -6px rgba(15,23,42,.18), 0 1px 3px rgba(15,23,42,.08); }
        .dot{ width:7px; height:7px; border-radius:50%; background:#cbd5e1; flex:none; }
        .dot.up{ background:var(--g); }
        .dot.down{ background:#ef4444; }
        .go{ border:0; cursor:pointer; color:#fff; font-weight:600; font-size:13px;
          background:var(--g); border-radius:999px; padding:8px 16px; white-space:nowrap;
          letter-spacing:-.1px; }
        .go:hover{ background:var(--g2); }
        .go:disabled{ opacity:.6; cursor:default; }
        .more{ border:0; cursor:pointer; background:transparent; color:var(--mut); font-size:11px;
          width:24px; height:24px; border-radius:50%; line-height:1; }
        .more:hover{ background:var(--sub); color:var(--fg); }
        .panel{ width:274px; background:var(--bg); color:var(--fg); border:1px solid var(--bd);
          border-radius:16px; padding:14px; display:flex; flex-direction:column; gap:12px;
          box-shadow:0 12px 36px -8px rgba(15,23,42,.22), 0 1px 3px rgba(15,23,42,.08); }
        .panel[hidden]{ display:none; }
        .head{ display:flex; align-items:center; gap:8px; }
        .logo{ width:22px; height:22px; border-radius:7px; background:var(--g); color:#fff;
          font-size:11px; font-weight:700; display:flex; align-items:center;
          justify-content:center; flex:none; letter-spacing:-.3px; }
        .ttl{ font-weight:650; font-size:13.5px; letter-spacing:-.2px; }
        .head .dot{ margin-left:auto; }
        label{ font-size:11px; color:var(--mut); font-weight:600; display:block; margin-bottom:5px; }
        select{ width:100%; padding:8px 10px; border-radius:10px; border:1px solid var(--bd);
          background:var(--sub); color:var(--fg); font-size:12.5px; outline:none; cursor:pointer; }
        select:focus{ border-color:var(--g); }
        .res{ font-size:12px; color:var(--mut); min-height:16px; line-height:1.5; }
        .res .amber{ color:var(--amb); font-weight:600; } .res .err{ color:#dc2626; }
        .res b{ color:var(--fg); }
        .steps{ display:flex; flex-direction:column; gap:7px; }
        .steps[hidden]{ display:none; }
        .step{ display:flex; align-items:center; gap:8px; font-size:12.5px; color:var(--mut); }
        .step .ic{ width:16px; height:16px; border-radius:50%; flex:none; display:flex;
          align-items:center; justify-content:center; font-size:9px; font-weight:700;
          background:var(--sub); color:var(--mut); border:1px solid var(--bd); }
        .step.run .ic{ background:var(--g); color:#fff; border-color:var(--g);
          animation:jp-pulse 1s infinite; }
        .step.done{ color:var(--fg); }
        .step.done .ic{ background:var(--g); color:#fff; border-color:var(--g); }
        .step.skip .ic{ opacity:.6; }
        .step.fail .ic{ background:var(--amb); color:#fff; border-color:var(--amb); }
        .step .nt{ margin-left:auto; font-size:11px; color:var(--mut); max-width:96px;
          overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        @keyframes jp-pulse{ 50%{ opacity:.5; } }
        .rev{ display:flex; flex-direction:column; gap:5px; border-top:1px solid var(--bd);
          padding-top:11px; max-height:150px; overflow-y:auto; }
        .rev[hidden]{ display:none; }
        .rttl{ font-size:11px; color:var(--amb); font-weight:700; }
        .rl{ border:1px solid var(--ambd); cursor:pointer; background:var(--ambg); color:var(--amb);
          font-size:11.5px; text-align:left; padding:6px 9px; border-radius:8px; overflow:hidden;
          text-overflow:ellipsis; white-space:nowrap; }
        .rl:hover{ filter:brightness(.97); }
        .rfile{ display:flex; align-items:center; gap:7px; font-size:11.5px; margin-top:7px; }
        .rfname{ flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--mut); }
        .rswap{ border:1px solid var(--bd); cursor:pointer; background:var(--bg); color:var(--fg);
          font-size:11px; padding:4px 10px; border-radius:8px; flex:none; font-weight:600; }
        .rswap:hover{ background:var(--sub); }
        .rswap[hidden]{ display:none; }
        /* footer: the secondary actions that used to crowd the resting pill */
        .foot{ display:flex; align-items:center; gap:6px; flex-wrap:wrap;
          border-top:1px solid var(--bd); padding-top:11px; }
        .lnk{ border:0; background:transparent; color:var(--mut); cursor:pointer; font-size:11.5px;
          padding:2px 0; }
        .lnk:hover{ color:var(--fg); text-decoration:underline; }
        .lnk.sep{ color:var(--bd); cursor:default; }
        .lnk.sep:hover{ color:var(--bd); text-decoration:none; }
        .applied{ border:1px solid var(--bd); background:var(--bg); color:var(--fg); cursor:pointer;
          font-size:11.5px; font-weight:600; padding:5px 10px; border-radius:8px; }
        .applied:hover{ background:var(--sub); }
      </style>
      <div class="dock">
        <div class="bar">
          <span class="dot" id="dot" title="JobPilot status"></span>
          <button class="go" id="go">Autofill</button>
          <button class="more" id="more" title="Options" aria-label="Options">⌄</button>
        </div>
        <div class="panel" id="panel" hidden>
          <div class="head">
            <span class="logo">JP</span>
            <span class="ttl">JobPilot Autofill</span>
          </div>
          <div class="steps" id="steps" hidden></div>
          <div>
            <label for="resume">Résumé</label>
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
          <div class="foot">
            <button class="applied" id="applied" title="Record that you submitted this application in JobPilot">Mark applied</button>
            <button class="lnk" id="hide">Hide here</button>
            <span class="lnk sep">·</span>
            <button class="lnk" id="off" title="Stop the pill from appearing on any site — nothing fills until you re-enable it from the JobPilot toolbar popup.">Turn off</button>
          </div>
        </div>
      </div>`;
    document.documentElement.appendChild(hostEl);

    root.getElementById("go").onclick = run;
    const appliedBtn = root.getElementById("applied");
    appliedBtn.onclick = async () => {
      appliedBtn.disabled = true;
      appliedBtn.textContent = "Recording…";
      const res = await send({ cmd: "mark_applied",
                               url: location.href, title: document.title,
                               company: companyFromPage() });
      if (res && res.ok) {
        appliedBtn.textContent = res.already ? "✓ Already recorded" : "✓ Recorded";
      } else {
        appliedBtn.textContent = "⚠ Retry — backend offline?";
        appliedBtn.disabled = false;
        setTimeout(() => {
          if (root.getElementById("applied") === appliedBtn)
            appliedBtn.textContent = "Mark applied";
        }, 4000);
      }
    };
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
      kept: stats.kept || 0,
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
      let total = 0, review = 0, fileFlags = 0, kept = 0, offline = false, sawAny = 0, pages = 0;
      const reviewItems = [];
      const MAX_PAGES = 7;
      while (true) {
        const r = await fillCurrentPage(resumePref, mode);
        total += r.filled; review += r.needs_review; fileFlags += r.file_flags; kept += r.kept || 0;
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
        (kept ? ` · kept ${kept} you'd already answered` : "") +
        (fileFlags ? ` · attach file manually` : "") +
        (review ? ` · <span class="amber">${review} need review</span>` : "") +
        (offline ? ` · offline mode` : "")
      );
      renderReview(reviewItems);
      setTimeout(() => { if (root.getElementById("go") === go) go.textContent = "Autofill"; }, 4000);
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
  // observe. A cheap periodic check guarantees the pill comes back. The same
  // tick catches SPA route changes (the host lives on documentElement, so
  // isConnected never flips on client-side navigation) — a new URL means the
  // applied button's "✓ Recorded" state belongs to the PREVIOUS job; reset it.
  let lastHref = location.href;
  setInterval(() => {
    if (location.href !== lastHref) {
      lastHref = location.href;
      const b = root && root.getElementById("applied");
      if (b) { b.disabled = false; b.textContent = "Mark applied"; }
    }
    maybeShow();
  }, 2000);
})();
