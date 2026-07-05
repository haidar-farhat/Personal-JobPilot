/* JobPilot Autofill — service worker.
 * Orchestrates: scan page -> ask local backend for a fill-plan -> apply it.
 * All network calls go only to the local JobPilot backend (host_permissions).
 */

const BACKEND = "http://127.0.0.1:7777";

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function health() {
  try {
    const r = await fetch(`${BACKEND}/api/autofill/health`, { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return await r.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

// Refresh + cache the profile so we can still standard-fill if the backend drops.
async function cacheProfile() {
  try {
    const r = await fetch(`${BACKEND}/api/autofill/profile`, { cache: "no-store" });
    if (r.ok) {
      const p = await r.json();
      await chrome.storage.local.set({ profile: p });
      return p;
    }
  } catch (e) { /* offline — fall through to stored copy */ }
  const { profile } = await chrome.storage.local.get("profile");
  return profile || null;
}

// Structured work/education/skills for wizard ATSes (Workday). Cached for offline.
async function fetchHistory() {
  try {
    const r = await fetch(`${BACKEND}/api/autofill/history`, { cache: "no-store" });
    if (r.ok) {
      const h = await r.json();
      await chrome.storage.local.set({ history: h });
      return h;
    }
  } catch (e) { /* offline — fall through */ }
  const { history } = await chrome.storage.local.get("history");
  return history || { error: "JobPilot backend offline and no cached history." };
}

// Best résumé file (tailored if we have one for this company/role, else base),
// base64-encoded — chrome.runtime messages can't carry binary.
async function fetchResumeFile(company, title) {
  try {
    const qs = new URLSearchParams({ company: company || "", job_title: title || "" });
    const r = await fetch(`${BACKEND}/api/autofill/resume_file?${qs}`, { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const disp = r.headers.get("content-disposition") || "";
    const m = disp.match(/filename="?([^";]+)"?/);
    const buf = await r.arrayBuffer();
    let bin = "";
    const bytes = new Uint8Array(buf);
    for (let i = 0; i < bytes.length; i += 0x8000)
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return { b64: btoa(bin), filename: m ? m[1] : "resume.docx",
             mime: r.headers.get("content-type") || "" };
  } catch (e) {
    return { error: "Résumé fetch failed: " + e.message };
  }
}

// ---- Offline deterministic mapper (mirrors agents/autofill_mapper.py basics) ----
function jnorm(s) { return (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim(); }
function jTok(n, ...w) { const t = new Set(n.split(" ")); return w.some((x) => t.has(x)); }
function jSub(n, ...s) { return s.some((x) => n.includes(x)); }

function jmap(field, p) {
  const type = (field.type || "text").toLowerCase();
  if (type === "file") return { value: null, source: "file", needs_review: true, confidence: 1 };
  const nl = jnorm(field.label || ""), nn = jnorm(field.name || "");
  const n = (nl + " " + nn).trim();
  if (!n) return null;
  const id = p.identity || {}, ad = p.address || {}, li = p.links || {}, wa = p.work_authorization || {},
        ex = p.experience || {}, sa = p.salary || {}, rf = p.referral || {}, ee = p.eeoc || {};
  const opts = field.options || [];
  const T = (v) => v ? { value: v, source: "deterministic", needs_review: false, confidence: 0.9 } : null;
  const O = (v) => {
    if (!v) return null;
    if (!opts.length) return { value: v, source: "deterministic", needs_review: false, confidence: 0.9 };
    const nv = jnorm(v);
    const m = opts.find((o) => jnorm(o) === nv) ||
              opts.find((o) => { const no = jnorm(o); return no && (no.includes(nv) || nv.includes(no)); });
    return m ? { value: m, source: "deterministic", needs_review: false, confidence: 0.9 }
             : { value: v, source: "deterministic", needs_review: true, confidence: 0.5 };
  };
  // EEO-tolerant choice matcher (mirrors autofill_mapper._match_choice)
  const DECLINE = ["decline", "do not wish", "dont wish", "prefer not", "not to answer", "not wish", "choose not", "rather not"];
  const SYN = { male: ["man"], female: ["woman"], heterosexual: ["straight"], "two or more races": ["two or more", "multiracial"] };
  const C = (v) => {
    if (!v) return null;
    if (!opts.length) return { value: v, source: "deterministic", needs_review: false, confidence: 0.9 };
    const nv = jnorm(v), cands = [nv, ...(SYN[nv] || [])];
    let m = null;
    for (const c of cands) { m = opts.find((o) => jnorm(o) === c); if (m) break; }
    if (!m && /^(decline|prefer not|do not wish|dont wish)/.test(nv))
      m = opts.find((o) => DECLINE.some((h) => jnorm(o).includes(h)));
    if (!m && (nv === "yes" || nv === "no")) {
      m = opts.find((o) => jnorm(o).split(" ")[0] === nv);
      if (!m && nv === "no") m = opts.find((o) => jnorm(o).startsWith("not "));
    }
    if (!m) for (const c of cands) { m = opts.find((o) => { const no = jnorm(o); return c && (no.includes(c) || c.includes(no)); }); if (m) break; }
    if (!m) {  // best token overlap
      const dw = nv.split(" ").filter((w) => w.length > 2);
      let best = null, bn = 0;
      for (const o of opts) { const ow = new Set(jnorm(o).split(" ")); const k = dw.filter((w) => ow.has(w)).length; if (k > bn) { best = o; bn = k; } }
      m = best;
    }
    return m ? { value: m, source: "deterministic", needs_review: false, confidence: 0.9 }
             : { value: v, source: "deterministic", needs_review: true, confidence: 0.5 };
  };
  const YN = (b) => C(b ? "Yes" : "No");

  if (jTok(n, "first", "given") && jTok(n, "name")) return T(id.first_name);
  if (jTok(n, "last", "surname", "family") && jTok(n, "name")) return T(id.last_name);
  if (["name", "full name", "your name", "legal name"].includes(nl) ||
      ["name", "full name", "your name", "legal name"].includes(nn)) return T(id.full_name);
  if (jSub(n, "email")) return T(id.email);
  if (jSub(n, "country code", "country phone", "phone country", "phone code")) return O(ad.country);
  if (jSub(n, "phone device", "device type", "phone type")) return O("Mobile");
  if (jSub(n, "phone", "mobile", "telephone")) return T(id.phone);
  if (jSub(n, "linkedin")) return T(li.linkedin);
  if (jSub(n, "github")) return T(li.github);
  if (jSub(n, "sponsor")) return YN(!!wa.requires_sponsorship);
  if ((jSub(n, "authorized", "authorization", "eligible") && jSub(n, "work")) || jSub(n, "work authorization"))
    return YN(wa.authorized_to_work_us !== false);
  if (jSub(n, "citizen")) return T(wa.citizenship);
  if (jSub(n, "transgender")) return C(ee.transgender || "No");
  if (jSub(n, "sexual orientation") || jTok(n, "orientation")) return C(ee.sexual_orientation);
  if (jSub(n, "lgbtq", "lgbtqia", "lgbt")) return C(ee.lgbtq);
  if (jSub(n, "hispanic", "latino", "latinx")) return C(ee.hispanic_latino);
  if (jSub(n, "gender")) return C(ee.gender);
  if (jSub(n, "race", "ethnicity")) return C(ee.race_ethnicity);
  if (jSub(n, "veteran", "military")) return C(ee.veteran_status);
  if (jSub(n, "disability", "disabled")) return C(ee.disability_status);
  if (jTok(n, "city")) return opts.length ? O(ad.city) : T(ad.city);
  if (jTok(n, "state", "province")) {
    if (opts.length) {
      for (const c of [ad.state_full, ad.state]) { if (c) { const m = O(c); if (m && !m.needs_review) return m; } }
      return O(ad.state);
    }
    return T(ad.state);
  }
  if (jTok(n, "zip", "zipcode", "postal") || jSub(n, "postal code", "zip code")) return T(ad.postal_code);
  if (jSub(n, "country")) return opts.length ? O(ad.country) : T(ad.country);
  if (jSub(n, "salary", "compensation")) return T(sa.preferred_text);
  if (jSub(n, "how did you hear", "referral") || jTok(n, "source")) return O(rf.default_source);
  return null;
}

async function offlinePlan(fields) {
  const { profile } = await chrome.storage.local.get("profile");
  if (!profile) return null;
  const out = fields.map((f) => {
    const m = jmap(f, profile);
    return m ? { id: f.id, ...m } : { id: f.id, value: null, source: "none", needs_review: true, confidence: 0 };
  });
  const filled = out.filter((o) => o.value).length;
  const needs = out.filter((o) => o.needs_review).length;
  return { fields: out, stats: { filled, needs_review: needs, llm_used: 0 },
           archetype_label: "offline (cached profile)", resume_used: "cached", _offline: true };
}

// Get a fill-plan from the backend, falling back to the offline mapper.
// Shared by the toolbar popup (runAutofill) and the in-page widget (cmd:"plan").
async function fetchPlan(fields, ctx, resumePref) {
  ctx = ctx || {};
  try {
    const r = await fetch(`${BACKEND}/api/autofill/plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: ctx.url || "", job_title: ctx.h1 || ctx.title || "", company: "",
        page_text: ctx.text || "", resume_pref: resumePref || "auto", fields,
      }),
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return await r.json();
  } catch (e) {
    const off = await offlinePlan(fields);
    return off || { error: "JobPilot backend offline and no cached profile. Start JobPilot, then retry." };
  }
}

async function runAutofill(resumePref) {
  const tab = await getActiveTab();
  if (!tab || !tab.id) return { error: "No active tab." };
  if (/^(chrome|edge|about|chrome-extension|view-source):/i.test(tab.url || ""))
    return { error: "Can't autofill this page." };

  await cacheProfile();

  // 0. Workday wizard sections first (multi-entry experience/education, skills,
  //    résumé upload). The engine tags what it fills so the flat pass skips it.
  const wizard = { filled: 0 };
  if (/myworkdayjobs\.com|myworkdaysite\.com|workday\.com/i.test(tab.url || "")) {
    try {
      const res = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => (window.__jpafWorkdayRun ? window.__jpafWorkdayRun() : null),
      });
      const s = res && res[0] && res[0].result;
      if (s) wizard.filled = s.filled || 0;
    } catch (e) { /* wizard engine unavailable — flat fill still runs */ }
  }

  // 1. scan every frame we can reach — company sites often embed the real ATS
  //    form in an iframe (Greenhouse/Lever/Ashby embeds). Frames we lack host
  //    permissions for are silently skipped by Chrome.
  let scans = [];
  try {
    const res = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      files: ["content/scan.js"],
    });
    scans = (res || [])
      .map((r) => ({ frameId: r.frameId ?? 0, fields: r.result || [] }))
      .filter((s) => s.fields.length > 0);
  } catch (e) {
    // allFrames can throw when some frames are inaccessible — retry top-only
    try {
      const res = await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content/scan.js"] });
      const fields = (res && res[0] && res[0].result) || [];
      if (fields.length) scans = [{ frameId: 0, fields }];
    } catch (e2) {
      return { error: "Scan failed: " + e2.message };
    }
  }
  if (!scans.length) {
    if (wizard.filled) return { ok: true, stats: { filled: wizard.filled, needs_review: 0, file_flags: 0 } };
    return { error: "No application fields detected on this page." };
  }

  // 2. page context for archetype routing + essays (top frame)
  let ctx = { url: tab.url || "", title: tab.title || "", h1: "", text: "" };
  try {
    const c = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => ({
        title: document.title,
        h1: (document.querySelector("h1") || {}).innerText || "",
        text: document.body ? document.body.innerText.slice(0, 4000) : "",
      }),
    });
    if (c && c[0] && c[0].result) ctx = { ...ctx, ...c[0].result };
  } catch (e) { /* non-fatal */ }

  // 3+4. per frame: fetch a plan, apply it (each frame is its own form)
  const totals = { filled: wizard.filled, needs_review: 0, file_flags: 0 };
  let planMeta = null, lastError = null;
  for (const scan of scans) {
    const plan = await fetchPlan(scan.fields, ctx, resumePref);
    if (plan.error) { lastError = plan.error; continue; }
    plan._scanMeta = scan.fields;
    planMeta = planMeta || plan;
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id, frameIds: [scan.frameId] },
        files: ["content/fill.js"],
      });
      const res = await chrome.scripting.executeScript({
        target: { tabId: tab.id, frameIds: [scan.frameId] },
        func: (p) => window.__jpafApply(p),   // async — executeScript awaits it
        args: [plan],
      });
      const stats = (res && res[0] && res[0].result) || {};
      totals.filled += stats.filled || 0;
      totals.needs_review += stats.needs_review || 0;
      totals.file_flags += stats.file_flags || 0;
    } catch (e) {
      lastError = "Fill failed: " + e.message;
    }
  }
  if (!planMeta) return { error: lastError || "Could not build a fill plan." };
  return { ok: true, stats: totals, resume: planMeta.resume_used,
           archetype: planMeta.archetype_label, offline: !!planMeta._offline };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (msg.cmd === "health") sendResponse(await health());
    else if (msg.cmd === "autofill") sendResponse(await runAutofill(msg.resumePref));
    else if (msg.cmd === "plan") { await cacheProfile(); sendResponse(await fetchPlan(msg.fields, msg.ctx, msg.resumePref)); }
    else if (msg.cmd === "history") sendResponse(await fetchHistory());
    else if (msg.cmd === "resume_file") sendResponse(await fetchResumeFile(msg.company, msg.title));
    else sendResponse({ error: "unknown cmd" });
  })();
  return true; // keep the message channel open for the async response
});
