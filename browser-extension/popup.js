const $ = (s) => document.querySelector(s);

function send(msg) {
  return new Promise((res) => chrome.runtime.sendMessage(msg, res));
}

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
