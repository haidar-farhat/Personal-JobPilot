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

// ---- Offline deterministic mapper (mirrors agents/autofill_mapper.py basics) ----
function jnorm(s) { return (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim(); }
function jTok(n, ...w) { const t = new Set(n.split(" ")); return w.some((x) => t.has(x)); }
function jSub(n, ...s) { return s.some((x) => n.includes(x)); }

function jmap(field, p) {
  const type = (field.type || "text").toLowerCase();
  if (type === "file") return { value: null, source: "file", needs_review: true, confidence: 1 };
  const n = jnorm((field.label || "") + " " + (field.name || ""));
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
  const YN = (b) => O(b ? "Yes" : "No");

  if (jTok(n, "first", "given") && jTok(n, "name")) return T(id.first_name);
  if (jTok(n, "last", "surname", "family") && jTok(n, "name")) return T(id.last_name);
  if (["name", "full name", "your name", "legal name"].includes(n)) return T(id.full_name);
  if (jSub(n, "email")) return T(id.email);
  if (jSub(n, "phone", "mobile", "telephone")) return T(id.phone);
  if (jSub(n, "linkedin")) return T(li.linkedin);
  if (jSub(n, "github")) return T(li.github);
  if (jSub(n, "sponsor")) return YN(!!wa.requires_sponsorship);
  if ((jSub(n, "authorized", "authorization", "eligible") && jSub(n, "work")) || jSub(n, "work authorization"))
    return YN(wa.authorized_to_work_us !== false);
  if (jSub(n, "citizen")) return T(wa.citizenship);
  if (jSub(n, "gender")) return O(ee.gender);
  if (jSub(n, "race", "ethnicity")) return O(ee.race_ethnicity);
  if (jSub(n, "veteran")) return O(ee.veteran_status);
  if (jSub(n, "disability")) return O(ee.disability_status);
  if (jSub(n, "hispanic", "latino")) return O(ee.hispanic_latino);
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

async function runAutofill(resumePref) {
  const tab = await getActiveTab();
  if (!tab || !tab.id) return { error: "No active tab." };
  if (/^(chrome|edge|about|chrome-extension|view-source):/i.test(tab.url || ""))
    return { error: "Can't autofill this page." };

  await cacheProfile();

  // 1. scan the page (scan.js returns the field list as its completion value)
  let fields = [];
  try {
    const res = await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content/scan.js"] });
    fields = (res && res[0] && res[0].result) || [];
  } catch (e) {
    return { error: "Scan failed: " + e.message };
  }
  if (!fields.length) return { error: "No application fields detected on this page." };

  // 2. page context for archetype routing + essays
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

  // 3. ask the backend for a plan; fall back to offline standard-fill
  let plan;
  try {
    const r = await fetch(`${BACKEND}/api/autofill/plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: ctx.url, job_title: ctx.h1 || ctx.title, company: "",
        page_text: ctx.text, resume_pref: resumePref || "auto", fields,
      }),
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    plan = await r.json();
  } catch (e) {
    plan = await offlinePlan(fields);
    if (!plan) return { error: "JobPilot backend offline and no cached profile. Start JobPilot, then retry." };
  }

  // 4. apply the plan (fill.js defines window.__jpafApply)
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content/fill.js"] });
    const res = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: (p) => window.__jpafApply(p),
      args: [plan],
    });
    const stats = (res && res[0] && res[0].result) || {};
    return { ok: true, stats, resume: plan.resume_used, archetype: plan.archetype_label, offline: !!plan._offline };
  } catch (e) {
    return { error: "Fill failed: " + e.message };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (msg.cmd === "health") sendResponse(await health());
    else if (msg.cmd === "autofill") sendResponse(await runAutofill(msg.resumePref));
    else sendResponse({ error: "unknown cmd" });
  })();
  return true; // keep the message channel open for the async response
});
