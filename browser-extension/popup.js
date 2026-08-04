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
      h.llm_provider === "openai" ? `Backend + ${h.llm_model || "GPT-5.6"} ready`
      : h.ollama_up ? "Backend + local AI ready"
      : h.fallback_llm ? "Backend ready (cloud AI fallback — GPT-5.6)"
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
    const onBoard = r.board && r.board.ok
      ? ` · <span class="ok">✓ on your JobPilot board</span>` : "";
    $("#result").innerHTML =
      `<b>Filled ${s.filled || 0}</b> field(s)` +
      (s.needs_review ? ` · <span class="amber">${s.needs_review} need review</span>` : "") +
      onBoard +
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

refreshHealth();
refreshEnabled();
