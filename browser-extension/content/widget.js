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
  const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");

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
        /* JobRight-style: a small mint pill at rest, a 360px white card with a
           mint header while it works. Everything secondary lives inside the card. */
        *{ box-sizing:border-box; margin:0;
           font-family:Inter,-apple-system,'Segoe UI',Roboto,system-ui,sans-serif; }
        :host{ --g:#12c98f; --gd:#0b7a57; --tint:#e6fbf4; --tintb:#b9f0dc; --bg:#fff; --fg:#111827;
               --mut:#6b7280; --bd:#e8eaee; --sub:#f5f6f8; --amb:#b45309; --ambg:#fff8ec; --ambd:#fde3b8; }
        @media (prefers-color-scheme:dark){
          :host{ --gd:#2ee6a6; --tint:#0f2f2a; --tintb:#1d5c4c; --bg:#151a22; --fg:#e9edf3;
                 --mut:#98a3b3; --bd:#2a323d; --sub:#1d232c;
                 --amb:#f0b45e; --ambg:#2a2113; --ambd:#4a3a1c; }
        }
        .dock{ display:flex; flex-direction:column-reverse; align-items:flex-end; gap:10px; }
        /* resting pill: mint, green dot, "Autofill" */
        .bar{ display:flex; align-items:center; gap:6px; background:var(--tint);
          border:1px solid var(--g); border-radius:999px; padding:4px 4px 4px 12px;
          box-shadow:0 6px 24px -6px rgba(15,23,42,.18), 0 1px 3px rgba(15,23,42,.08); }
        .dot{ width:8px; height:8px; border-radius:50%; background:var(--g); flex:none; }
        .dot.up{ background:var(--g); }
        .dot.down{ background:#ef4444; }
        .go{ border:0; cursor:pointer; color:var(--gd); font-weight:700; font-size:13px;
          background:transparent; border-radius:999px; padding:6px 8px; white-space:nowrap;
          letter-spacing:-.1px; }
        .go:hover{ background:rgba(18,201,143,.16); }
        .go:disabled{ opacity:.6; cursor:default; }
        .more{ border:0; cursor:pointer; background:transparent; color:var(--gd); font-size:12px;
          width:26px; height:26px; border-radius:50%; line-height:1; }
        .more:hover{ background:rgba(18,201,143,.16); }
        /* the card */
        .panel{ width:360px; max-width:calc(100vw - 28px); background:var(--bg); color:var(--fg);
          border:1px solid var(--bd); border-radius:16px; overflow:hidden;
          box-shadow:0 16px 48px -12px rgba(15,23,42,.28), 0 1px 3px rgba(15,23,42,.08); }
        .panel[hidden]{ display:none; }
        .head{ display:flex; align-items:center; gap:8px; background:var(--g); color:#fff;
          padding:11px 12px 11px 14px; }
        .logo{ width:22px; height:22px; border-radius:7px; background:rgba(255,255,255,.22);
          font-size:12px; display:flex; align-items:center; justify-content:center; flex:none; }
        .ttl{ font-weight:700; font-size:14px; letter-spacing:-.2px; }
        .close{ margin-left:auto; border:0; cursor:pointer; background:transparent; color:#fff;
          width:26px; height:26px; border-radius:50%; font-size:15px; line-height:1; }
        .close:hover{ background:rgba(255,255,255,.2); }
        .body{ padding:14px; display:flex; flex-direction:column; gap:12px; }
        .banner{ font-size:12px; color:var(--gd); background:var(--tint); border:1px solid var(--tintb);
          border-radius:10px; padding:8px 10px; font-weight:600; line-height:1.4; }
        .banner[hidden]{ display:none; }
        /* progress */
        .prog{ display:flex; flex-direction:column; gap:6px; }
        .prog[hidden]{ display:none; }
        .track{ height:6px; border-radius:999px; background:var(--tint); overflow:hidden; }
        .pfill{ height:100%; width:100%; background:var(--g); border-radius:999px;
                transform:scaleX(0); transform-origin:left; transition:transform .35s ease; }
        .count{ font-size:12px; color:var(--mut); font-weight:600; }
        /* checklist: ○ pending · spinner running · ✓ done · – skipped · ! failed */
        .steps{ display:flex; flex-direction:column; gap:8px; }
        .steps[hidden]{ display:none; }
        .step{ display:flex; align-items:center; gap:9px; font-size:12.5px; color:var(--mut); }
        .step .ic{ width:18px; height:18px; border-radius:50%; flex:none; display:flex;
          align-items:center; justify-content:center; font-size:10px; font-weight:700;
          color:var(--mut); border:1.5px solid var(--bd); }
        .step.run .ic{ border-color:var(--g); border-top-color:transparent; color:transparent;
          animation:jp-spin .8s linear infinite; }
        .step.run{ color:var(--fg); }
        .step.done{ color:var(--fg); }
        .step.done .ic{ background:var(--g); color:#fff; border-color:var(--g); }
        .step.skip .ic{ color:var(--mut); }
        .step.fail .ic{ background:var(--amb); color:#fff; border-color:var(--amb); }
        .step .nt{ margin-left:auto; font-size:11px; color:var(--mut); max-width:130px;
          overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        @keyframes jp-spin{ to{ transform:rotate(360deg); } }
        /* result + review */
        .res{ font-size:12.5px; color:var(--mut); min-height:16px; line-height:1.5; }
        .res .amber{ color:var(--amb); font-weight:600; } .res .err{ color:#dc2626; }
        .res b{ color:var(--fg); }
        .res a{ color:var(--gd); }
        .res .cta{ margin-top:4px; font-size:11.5px; }
        .rev{ display:flex; flex-direction:column; gap:6px; border-top:1px solid var(--bd);
          padding-top:11px; max-height:170px; overflow-y:auto; }
        .rev[hidden]{ display:none; }
        .rttl{ font-size:11px; color:var(--amb); font-weight:700; }
        .rrow{ display:flex; align-items:center; gap:8px; background:var(--ambg);
          border:1px solid var(--ambd); border-radius:8px; padding:5px 6px 5px 9px; }
        .rlab{ flex:1; font-size:11.5px; color:var(--amb); overflow:hidden; text-overflow:ellipsis;
          white-space:nowrap; }
        .rl{ border:1px solid var(--ambd); cursor:pointer; background:var(--bg); color:var(--amb);
          font-size:11px; font-weight:600; padding:3px 9px; border-radius:6px; flex:none; }
        .rl:hover{ filter:brightness(.97); }
        /* résumé chooser */
        label{ font-size:11px; color:var(--mut); font-weight:600; display:block; margin-bottom:5px; }
        select{ width:100%; padding:8px 10px; border-radius:10px; border:1px solid var(--bd);
          background:var(--sub); color:var(--fg); font-size:12.5px; outline:none; cursor:pointer; }
        select:focus{ border-color:var(--g); }
        .rfile{ display:flex; align-items:center; gap:7px; font-size:11.5px; margin-top:7px; }
        .rfname{ flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--mut); }
        .rswap{ border:1px solid var(--bd); cursor:pointer; background:var(--bg); color:var(--fg);
          font-size:11px; padding:4px 10px; border-radius:8px; flex:none; font-weight:600; }
        .rswap:hover{ background:var(--sub); }
        .rswap[hidden]{ display:none; }
        .auth{ display:flex; gap:6px; flex-wrap:wrap; }
        /* footer: the secondary actions */
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
            <span class="logo">✦</span>
            <span class="ttl">JobPilot Autofill</span>
            <button class="close" id="close" title="Collapse" aria-label="Collapse">⌄</button>
          </div>
          <div class="body">
            <div class="banner" id="banner" hidden></div>
            <div class="prog" id="prog" hidden>
              <div class="track"><div class="pfill" id="pfill"></div></div>
              <div class="count" id="count">Analyzing form…</div>
            </div>
            <div class="steps" id="steps" hidden></div>
            <div class="res" id="res">Fills the form — never submits. You review &amp; click Apply.</div>
            <div class="rev" id="review" hidden></div>
            <div class="auth" id="auth" hidden>
              <button class="applied" id="acct" title="Fill your email + ATS password into this sign-up / sign-in form. You click the button.">Fill account</button>
              <button class="applied" id="otp" title="Read the verification code the ATS just emailed you (Gmail, read-only) and fill it in">Get code from Gmail</button>
            </div>
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
            <div class="foot">
              <button class="applied" id="applied" title="Record that you submitted this application in JobPilot">Mark applied</button>
              <button class="lnk" id="hide">Hide here</button>
              <span class="lnk sep">·</span>
              <button class="lnk" id="off" title="Stop the pill from appearing on any site — nothing fills until you re-enable it from the JobPilot toolbar popup.">Turn off</button>
            </div>
          </div>
        </div>
      </div>`;
    document.documentElement.appendChild(hostEl);

    root.getElementById("go").onclick = run;
    root.getElementById("close").onclick = () => { root.getElementById("panel").hidden = true; };
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
    root.getElementById("acct").onclick = fillAccount;
    root.getElementById("otp").onclick = () => fetchCode(false);
    health();
    refreshResumeMeta();
    refreshAuth();
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

  // ---- Account walls + verification codes (content/account.js does the DOM work) ----
  // Fills email + the ATS password into EMPTY sign-up/sign-in fields; the user
  // clicks Create Account / Sign In / Verify. Once an account fill happened on
  // this host, a code box showing up within 15 min fetches the code from Gmail
  // by itself (read-only, via the local backend).
  const otpTried = new Set();   // hrefs we already auto-fetched a code for
  function authState() {
    try { return (window.__jpafAuthState && window.__jpafAuthState()) || {}; } catch (e) { return {}; }
  }
  function wallKind() { const k = authState().kind; return k === "create" || k === "signin" ? k : ""; }

  function refreshAuth() {
    const box = root && root.getElementById("auth");
    if (!box || !hostEl || !hostEl.isConnected) return;
    const st = authState(), wall = wallKind();
    const a = root.getElementById("acct"), o = root.getElementById("otp");
    a.hidden = !wall;
    if (wall && !/^✓/.test(a.textContent))
      a.textContent = wall === "create" ? "Fill account (email + ATS password)" : "Fill sign-in";
    o.hidden = !st.otpFields;
    box.hidden = !(wall || st.otpFields);
    if (st.otpFields && !otpTried.has(location.href)) { otpTried.add(location.href); maybeAutoCode(); }
  }

  async function fillAccount() {
    const a = root.getElementById("acct");
    root.getElementById("panel").hidden = false;
    const creds = await send({ cmd: "account_creds" });
    if (!creds || !creds.hasPassword) {
      setRes(`<span class="amber">Set an ATS password first</span> — click the JobPilot toolbar icon → “ATS password”. One password for every ATS account; it stays in this browser only.`);
      return;
    }
    if (!creds.email) { setRes(`<span class="err">No email in your JobPilot profile.</span>`); return; }
    const r = window.__jpafFillAccount ? window.__jpafFillAccount(creds) : { error: "account assist not loaded" };
    if (r.error) { setRes(`<span class="err">${esc(r.error)}</span>`); return; }
    await send({ cmd: "account_started", host: location.hostname });
    const did = [r.email && "email", r.password && "password"].filter(Boolean).join(" + ") || "nothing new (already filled)";
    a.textContent = "✓ Filled";
    setRes(`Filled ${did}. Tick any agreement box and click <b>${r.kind === "create" ? "Create Account" : "Sign In"}</b> yourself — when a verification-code box appears, the code is fetched from Gmail.`);
  }

  async function maybeAutoCode() {
    const s = await send({ cmd: "account_since", host: location.hostname });
    if (!s || !s.ts || Date.now() - s.ts > 15 * 60 * 1000) return;   // only right after an account fill here
    fetchCode(true, s.ts);
  }

  async function fetchCode(auto, sinceTs) {
    const o = root.getElementById("otp");
    root.getElementById("panel").hidden = false;
    if (!sinceTs) {
      const s = await send({ cmd: "account_since", host: location.hostname });
      sinceTs = (s && s.ts && Date.now() - s.ts < 60 * 60 * 1000) ? s.ts : Date.now() - 10 * 60 * 1000;
    }
    o.disabled = true; o.textContent = "Checking Gmail…";
    setRes(`${auto ? "Code box spotted — " : ""}waiting for the verification email (up to a minute)…`);
    // three short backend polls rather than one long one: an MV3 service worker
    // can be torn down mid-request; this content script cannot.
    let r = null;
    for (let i = 0; i < 3; i++) {
      r = await send({ cmd: "gmail_code", since: new Date(sinceTs).toISOString(), hint: location.hostname, wait: 20 });
      if (!r || r.connected === false || r.code || r.link) break;
    }
    o.disabled = false; o.textContent = "Get code from Gmail";
    if (!r || r.connected === false) {
      setRes(`<span class="amber">Gmail not connected</span> — ${esc((r && (r.reason || r.error)) || "JobPilot backend offline")}. Set it up from the dashboard (“Sync from email”).`);
      return;
    }
    if (r.code) {
      const f = window.__jpafFillOtp ? window.__jpafFillOtp(r.code) : { ok: false };
      setRes(f.ok ? `✓ Code <b>${esc(r.code)}</b> filled from “${esc(r.subject || "")}”. Click <b>Verify / Continue</b> yourself.`
                  : `Code <b>${esc(r.code)}</b> arrived (“${esc(r.subject || "")}”) but the box couldn't be filled — type it in.`);
      return;
    }
    if (r.link) {
      setRes(`Verification is a link, not a code: <a href="${esc(r.link)}" target="_blank" rel="noopener">open it ↗</a> (from “${esc(r.subject || "")}”).`);
      return;
    }
    setRes(`No verification email yet — check spam or hit “Resend”, then click <b>Get code from Gmail</b> again.`);
  }

  const SECTION_LABELS = {
    analyze: "Analyzing form",
    contact: "Contact info", work: "Work experience", education: "Education",
    skills: "Skills", resume: "Résumé/CV", websites: "Websites", linkedin: "LinkedIn",
    source: "How you heard", identity: "Disclosures & identity",
    fields: "Contact & questions", page: "Next page",
  };

  // the pending icon is the empty bordered circle itself (○)
  const stepRow = (n) =>
    `<div class="step" data-sec="${n}"><span class="ic"></span><span>${SECTION_LABELS[n] || n}</span><span class="nt"></span></div>`;

  function stepsInit(names) {
    const box = root.getElementById("steps");
    if (!box) return;
    box.hidden = false;
    box.innerHTML = names.map(stepRow).join("");
  }

  function stepSet(name, status, note) {
    const box = root && root.getElementById("steps");
    if (!box) return;
    let row = box.querySelector(`[data-sec="${name}"]`);
    if (!row) {  // section discovered mid-run
      box.insertAdjacentHTML("beforeend", stepRow(name));
      row = box.querySelector(`[data-sec="${name}"]`);
    }
    row.className = "step " + ({ start: "run", done: "done", skip: "skip", fail: "fail" }[status] || "");
    row.querySelector(".ic").textContent = { start: "", done: "✓", skip: "–", fail: "!" }[status] || "";
    if (note) row.querySelector(".nt").textContent = note;
  }

  // Progress bar + "Filling 12 / 30 fields" counter, paced by fill.js's
  // per-field events (see emit() there).
  function progress(n, total, text) {
    const p = root && root.getElementById("prog");
    if (!p) return;
    p.hidden = false;
    root.getElementById("pfill").style.transform = "scaleX(" + (total ? n / total : 0) + ")";
    root.getElementById("count").textContent = text;
  }

  window.addEventListener("jpaf-progress", (e) => {
    const d = e.detail || {};
    if (d.section) stepSet(d.section, d.status, d.note);
    if (d.type === "field") {
      const n = (d.index || 0) + 1, t = d.total || n;
      progress(n, t, `Filling ${n} / ${t} fields`);
    }
  });

  // JobRight-style review list: fields the fill flagged, "Jump to" each.
  function renderReview(items) {
    const box = root && root.getElementById("review");
    if (!box) return;
    if (!items.length) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
    const req = items.filter((r) => r.required).length;
    box.innerHTML = `<div class="rttl">Needs your review (${items.length})` +
      (req ? ` — <b>${req} required</b>` : "") + `</div>` +
      items.slice(0, 10).map((r) =>
        `<div class="rrow"><span class="rlab" title="${esc(r.label)}">${esc(r.label)}` +
        (r.required ? ` <b style="color:#c08a00">· required</b>` : "") + `</span>` +
        `<button class="rl" data-rid="${esc(r.id)}">Jump to</button></div>`).join("");
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
    stepSet("analyze", "done", `${fields.length} field${fields.length === 1 ? "" : "s"}`);
    if (!fields.length) {
      stepSet("fields", "skip", "none left");
      return { filled: wizardFilled, needs_review: 0, file_flags: 0, scanned: 0, wizard: wizardFilled };
    }
    stepSet("fields", "start");
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
    stepSet("fields", "done", `${stats.filled || 0} filled`);
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

  // Returns true when the page had application fields (the arm is consumed
  // only then — a JD page / wizard landing keeps it for the form that follows).
  async function run() {
    const go = root.getElementById("go");
    if (go.disabled) return false;   // already filling
    go.disabled = true;
    go.textContent = "Filling…";
    root.getElementById("panel").hidden = false;
    renderReview([]);
    progress(0, 1, "Analyzing form…");
    setRes("Filling — never submits. You review &amp; click Apply.");
    try {
      const isWD = window.__jpafIsWorkday && window.__jpafIsWorkday();
      const ghSecs = (!isWD && window.__jpafIsGreenhouse && window.__jpafIsGreenhouse() &&
                      window.__jpafGreenhouseSections) ? window.__jpafGreenhouseSections() : [];
      const mode = { wizard: isWD ? "workday" : (ghSecs.length ? "greenhouse" : null) };
      const secs = isWD
        ? ((window.__jpafWorkdaySections && window.__jpafWorkdaySections()) || [])
        : ghSecs;
      stepsInit(["analyze", ...secs, "fields"]);
      stepSet("analyze", "start");
      const resumePref = root.getElementById("resume").value;

      // Fill this page; on Workday keep advancing (Next / Save and Continue —
      // NEVER Submit or the review step) and filling each new step.
      let total = 0, review = 0, fileFlags = 0, kept = 0, offline = false, sawAny = 0, pages = 0, scanned = 0;
      const reviewItems = [];
      const MAX_PAGES = 7;
      while (true) {
        const r = await fillCurrentPage(resumePref, mode);
        total += r.filled; review += r.needs_review; fileFlags += r.file_flags; kept += r.kept || 0;
        reviewItems.push(...(r.review_fields || []));
        offline = offline || !!r.offline;
        sawAny += r.scanned + r.filled;
        scanned += r.scanned;
        pages++;
        if (!isWD || pages >= MAX_PAGES || !window.__jpafWorkdayNext) break;
        const nxt = await window.__jpafWorkdayNext();
        if (!nxt || !nxt.clicked) {
          if (nxt && /^at-/.test(nxt.reason || "")) stepSet("page", "done", "review step — your turn");
          else if (nxt && nxt.reason === "validation-errors") stepSet("page", "fail", "fix highlighted fields");
          else if (nxt && nxt.reason === "no-next-button" && wallKind()) stepSet("page", "skip", "account wall — use “Fill account” below");
          break;
        }
        stepSet("page", "done", nxt.label || "next");
        const secs2 = (window.__jpafWorkdaySections && window.__jpafWorkdaySections()) || [];
        stepsInit(["analyze", ...secs2, "fields"]);
        stepSet("analyze", "start");
      }

      if (!sawAny) {
        progress(0, 1, "No fields found");
        setRes(`<span class="err">No application fields detected here.</span>`);
        return false;
      }
      go.textContent = `✓ ${total} filled`;
      progress(1, 1, `Filled ${total} / ${Math.max(scanned, total)} fields`);
      setRes(
        `<b>✓ Filled ${total} field${total === 1 ? "" : "s"}</b>` +
        (review ? ` · <span class="amber">${review} need your review</span>` : "") +
        (pages > 1 ? ` · ${pages} pages` : "") +
        (kept ? ` · kept ${kept} you'd already answered` : "") +
        (fileFlags ? ` · attach file manually` : "") +
        (offline ? ` · offline mode` : "") +
        `<div class="cta">Review &amp; submit yourself — JobPilot never clicks Apply.</div>`
      );
      renderReview(reviewItems);
      setTimeout(() => { if (root.getElementById("go") === go) go.textContent = "Autofill"; }, 4000);
      return true;
    } catch (e) {
      setRes(`<span class="err">${esc(e.message || e)}</span>`);
      return false;
    } finally {
      go.disabled = false;
    }
  }

  // ---- "APPLY WITH AUTOFILL" from the dashboard ----
  // The dashboard armed this host right before opening the link. Ask once per
  // URL, and only once the page actually shows a form (a JD page or a Workday
  // landing has no inputs — the arm waits for the step that does). One run per
  // page load / SPA step; a reload of the same URL never refills.
  const ARM_FLAG = "jpaf_armed_ran";
  let armState = null;    // null = not asked for this URL yet; {} = not armed; {host,…} = armed
  let armBusy = false, autoRan = false;

  async function maybeAutoRun() {
    if (!root || disabled || autoRan || armBusy) return;
    if (visibleInputs().length < 3 || wallKind()) return;
    if (armState === null) {
      armBusy = true;
      const href = location.href, host = location.hostname.replace(/^www\./, "");
      const a = (await send({ type: "ARMED", host, url: href })) || {};
      armBusy = false;
      if (location.href !== href) return;   // SPA moved on mid-flight — ask again there
      armState = a;
      if (!root || autoRan) return;
    }
    if (!armState.host) return;
    autoRan = true;
    try { if (sessionStorage.getItem(ARM_FLAG) === location.href) return; } catch (e) { /* opaque origin */ }
    try { sessionStorage.setItem(ARM_FLAG, location.href); } catch (e) { /* noop */ }
    const b = root.getElementById("banner");
    b.hidden = false;
    b.textContent = `Autofilling for ${armState.title || "this role"} @ ${armState.company || armState.host} — from JobPilot`;
    root.getElementById("panel").hidden = false;
    const ok = await run();
    if (ok) {
      armState = {};
      send({ type: "ARM_CONSUMED", host: location.hostname.replace(/^www\./, "") });
    }
  }

  function maybeShow() {
    if (disabled || dismissed) return;
    // SPA frameworks (React hydration, document.write) can rip our host out of
    // the DOM after we've built it — detect that and rebuild.
    if (hostEl && !hostEl.isConnected) { hostEl = null; root = null; }
    if (hostEl) return;
    if (looksLikeApplication()) { build(); maybeAutoRun(); }
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
      const ab = root && root.getElementById("acct");
      if (ab) ab.textContent = "Fill account";
      const bn = root && root.getElementById("banner");
      if (bn) bn.hidden = true;
      armState = null; autoRan = false;   // a new SPA step may be the armed form
    }
    maybeShow();
    refreshAuth();   // sign-up wall / code box can appear on any SPA step
    maybeAutoRun();  // forms that render after load (SPA) — cheap until armed
  }, 2000);
})();
