const $ = (s) => document.querySelector(s);
const DISABLE_KEY = "jpaf_disabled";

function send(msg) {
  return new Promise((res) => chrome.runtime.sendMessage(msg, res));
}

// Kill switch: persisted flag the in-page pill also watches (it tears itself
// down / rebuilds live on every open tab when this flips).
function applyEnabledUI(on) {
  $("#go").disabled = !on;
  $("#offNote").hidden = on;
}

async function refreshEnabled() {
  const v = await chrome.storage.local.get(DISABLE_KEY);
  const on = !(v && v[DISABLE_KEY]);
  $("#enabled").checked = on;
  applyEnabledUI(on);
}

$("#enabled").onchange = async (e) => {
  const on = e.target.checked;
  await chrome.storage.local.set({ [DISABLE_KEY]: !on });
  applyEnabledUI(on);
};

async function refreshHealth() {
  const h = await send({ cmd: "health" });
  const dot = $("#dot"), t = $("#statusText");
  if (h && h.ok) {
    dot.className = "dot up";
    t.textContent =
      h.llm_provider === "openai" ? `Backend + ${h.llm_model || "configured cloud AI"} ready`
      : h.ollama_up ? "Backend + local AI ready"
      : h.fallback_llm ? "Backend ready (authorized cloud fallback)"
      : "Backend ready (AI off — using templates)";
  } else {
    dot.className = "dot down";
    t.textContent = "JobPilot offline — start it for AI drafting";
  }
}

$("#go").onclick = async () => {
  const btn = $("#go");
  btn.disabled = true;
  btn.textContent = "Filling…";
  $("#result").textContent = "";
  const r = await send({ cmd: "autofill", resumePref: $("#resume").value });
  btn.disabled = false;
  btn.textContent = "Autofill this application";
  if (r && r.ok) {
    const s = r.stats || {};
    $("#result").innerHTML =
      `<b>Filled ${s.filled || 0}</b> field(s)` +
      (s.needs_review ? ` · <span class="amber">${s.needs_review} need review</span>` : "") +
      `<div class="review-state">Manual review required — JobPilot did not submit or mark this applied.</div>` +
      (r.stopped_at === "no-next-button"
        ? `<div class="meta">Stopped at a sign-in / create-account wall — use the in-page pill's “Fill account”.</div>` : "") +
      `<div class="meta">${r.resume || ""}${r.offline ? " · offline mode" : ""}</div>`;
  } else {
    $("#result").innerHTML = `<span class="err">${(r && r.error) || "Failed"}</span>`;
  }
};

// ---- Company scan: rank this company's open roles against the résumé ----
function roleRow(job) {
  const row = document.createElement("div");
  row.className = "rolerow";
  const badge = document.createElement("span");
  badge.className = "scorebadge" + (job.score >= 60 ? " hot" : "");
  badge.textContent = job.score;
  const body = document.createElement("div");
  body.className = "rolebody";
  const link = document.createElement("a");
  link.href = job.url;
  link.target = "_blank";
  link.textContent = job.title;            // textContent — titles are external data
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = [job.location || (job.is_remote ? "Remote" : ""),
                      (job.matched || []).slice(0, 4).join(", ")]
                     .filter(Boolean).join(" · ");
  body.append(link, meta);
  const save = document.createElement("button");
  save.className = "savebtn";
  save.textContent = "+ Board";
  save.title = "Save to your JobPilot board";
  save.onclick = async () => {
    save.disabled = true;
    save.textContent = "Saving…";
    const r = await send({ cmd: "save_job", ensureScore: false, job: {
      url: job.url, title: job.title, company: job.company,
      description: job.description || "", location: job.location || "" } });
    save.textContent = r && r.ok ? "✓ Saved" : "⚠ Retry";
    save.disabled = !!(r && r.ok);
  };
  row.append(badge, body, save);
  return row;
}

$("#scan").onclick = async () => {
  const btn = $("#scan"), out = $("#scanResult");
  btn.disabled = true;
  btn.textContent = "Scanning company board…";
  out.textContent = "";
  const r = await send({ cmd: "company_scan" });
  btn.disabled = false;
  btn.textContent = "Find my best roles at this company";
  if (!r || r.error || !r.ok) {
    out.innerHTML = `<span class="err"></span>`;
    out.firstChild.textContent = (r && (r.error || "Scan failed.")) || "Scan failed.";
    return;
  }
  const head = document.createElement("div");
  head.className = "meta";
  head.textContent = `${r.company} · ${r.ats} · ${r.jobs_found} matching role(s), ranked for you:`;
  out.replaceChildren(head);
  if (!r.jobs.length) {
    head.textContent = `${r.company}: no roles matching your profile right now.`;
    return;
  }
  for (const job of r.jobs.slice(0, 8)) out.appendChild(roleRow(job));
};

// ---- Tailor chain: save → score → tailored résumé + cover letter ----
$("#tailorBtn").onclick = async () => {
  const btn = $("#tailorBtn"), out = $("#tailorResult");
  btn.disabled = true;
  btn.textContent = "Tailoring… (local LLM, 1–3 min)";
  out.textContent = "Saving job → scoring match → writing your one-page résumé…";
  const r = await send({ cmd: "tailor" });
  btn.disabled = false;
  btn.textContent = "Tailor résumé for this job";
  if (r && r.ok) {
    out.innerHTML = `<span class="ok">✓ Tailored résumé ready</span>` +
      `<div class="meta"></div>`;
    out.querySelector(".meta").textContent =
      `${r.resume_filename || ""} · match ${r.fit_score != null ? r.fit_score + "%" : "n/a"}` +
      ` · Autofill will attach it automatically`;
  } else {
    out.innerHTML = `<span class="err"></span>`;
    out.firstChild.textContent = (r && r.error) || "Tailoring failed.";
  }
};

// ---- ATS password (one for every ATS account) + Gmail status ----
// chrome.storage.local only — the backend never sees it. The in-page pill's
// "Fill account" asks the service worker for it at fill time.
const ATS_PW_KEY = "jpaf_ats_password";

function genPassword() {
  const sets = ["ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnopqrstuvwxyz", "23456789", "!@#$%&*?"];
  const rnd = (n) => crypto.getRandomValues(new Uint32Array(1))[0] % n;
  const pick = (s) => s[rnd(s.length)];
  const chars = [];
  sets.forEach((s) => chars.push(pick(s), pick(s)));      // every class an ATS demands, twice
  const all = sets.join("");
  while (chars.length < 16) chars.push(pick(all));
  for (let i = chars.length - 1; i > 0; i--) { const j = rnd(i + 1); [chars[i], chars[j]] = [chars[j], chars[i]]; }
  return chars.join("");
}

async function loadAtsPassword() {
  const v = await chrome.storage.local.get(ATS_PW_KEY);
  $("#atsPw").value = (v && v[ATS_PW_KEY]) || "";
}
$("#pwGen").onclick = () => { $("#atsPw").value = genPassword(); $("#atsPw").type = "text"; };
$("#pwShow").onclick = () => { const i = $("#atsPw"); i.type = i.type === "password" ? "text" : "password"; };
$("#pwSave").onclick = async () => {
  const pw = $("#atsPw").value;
  await chrome.storage.local.set({ [ATS_PW_KEY]: pw });
  $("#pwResult").innerHTML = pw
    ? `<span class="ok">✓ Saved</span><div class="meta">Keep a copy in your password manager — this is what “Fill account” types on ATS sign-up pages.</div>`
    : `<span class="meta">Cleared.</span>`;
};

async function refreshGmail() {
  const g = await send({ cmd: "gmail_health" });
  const dot = $("#gdot"), t = $("#gmailText");
  if (g && g.connected) { dot.className = "dot up"; t.textContent = `Gmail connected (${g.address || ""}) — codes auto-fill`; }
  else { dot.className = "dot"; t.textContent = "Gmail not connected — verification codes stay manual"; }
}

refreshHealth();
refreshEnabled();
loadAtsPassword();
refreshGmail();
