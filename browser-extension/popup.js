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
    t.textContent = h.ollama_up ? "Backend + AI ready" : "Backend ready (AI off — using templates)";
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
      `<div class="meta">${r.resume || ""}${r.offline ? " · offline mode" : ""}</div>`;
  } else {
    $("#result").innerHTML = `<span class="err">${(r && r.error) || "Failed"}</span>`;
  }
};

refreshHealth();
refreshEnabled();
