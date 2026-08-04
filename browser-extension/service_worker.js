/* JobPilot Autofill — service worker.
 * Orchestrates: scan page -> ask local backend for a fill-plan -> apply it.
 * All network calls go only to the local JobPilot backend (host_permissions).
 */

// ponytail: NO self-reload here. A `getManifest().version !== SW_BUILD ->
// chrome.runtime.reload()` self-heal used to live here and it bricked the
// extension (2026-08-03): reload() re-reads the SAME file, so the hardcoded
// constant stays stale, so it reloads again — forever. Chrome kills an
// extension that reloads 5x in 10s with "This extension reloaded itself too
// frequently." The check also cannot detect what it was written for: a truly
// stale worker reports the OLD manifest version too, so both agree and it
// never fires. It was a no-op when correct and a brick when not.
// After editing this file, hit ↻ on chrome://extensions like every other MV3
// extension. tests/e2e/test_extension_no_self_reload.py keeps it from coming back.

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
  let err = "";
  try {
    const r = await fetch(`${BACKEND}/api/autofill/profile`, { cache: "no-store" });
    if (r.ok) {
      const p = await r.json();
      try { await chrome.storage.local.set({ profile: p }); } catch (e) { /* cache is best-effort */ }
      return p;
    }
    err = `HTTP ${r.status}`;
  } catch (e) { err = String(e && e.message || e); }
  // offline — stored copy, stamped so the UI can say WHY data may be stale
  const { profile } = await chrome.storage.local.get("profile");
  if (profile) profile._stale = err || "fetch failed";
  return profile || null;
}

// Structured work/education/skills for wizard ATSes (Workday). Cached for offline.
// company/title route to the tailored resume's entries when one exists, so
// filled panels match the attached tailored .docx.
async function fetchHistory(company, title) {
  try {
    const qs = new URLSearchParams({ company: company || "", job_title: title || "" });
    const r = await fetch(`${BACKEND}/api/autofill/history?${qs}`, { cache: "no-store" });
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
async function fetchFileB64(endpoint, company, title) {
  try {
    const qs = new URLSearchParams({ company: company || "", job_title: title || "" });
    const r = await fetch(`${BACKEND}/api/autofill/${endpoint}?${qs}`, { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const disp = r.headers.get("content-disposition") || "";
    const m = disp.match(/filename="?([^";]+)"?/);
    const buf = await r.arrayBuffer();
    let bin = "";
    const bytes = new Uint8Array(buf);
    for (let i = 0; i < bytes.length; i += 0x8000)
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return { b64: btoa(bin), filename: m ? m[1] : "file.docx",
             mime: r.headers.get("content-type") || "" };
  } catch (e) {
    return { error: "File fetch failed: " + e.message };
  }
}

// ---- Offline deterministic mapper (mirrors agents/autofill_mapper.py basics) ----
function jnorm(s) { return (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim(); }
function jTok(n, ...w) { const t = new Set(n.split(" ")); return w.some((x) => t.has(x)); }
function jSub(n, ...s) { return s.some((x) => n.includes(x)); }

// Fields asking about someone who is NOT the applicant. "Emergency contact
// phone number" contains "phone" and "Reference's email" contains "email", so
// without this the identity rules below hand over his own details for a
// different person. Mirrors _THIRD_PARTY in agents/autofill_mapper.py.
// Both halves required: a question may NAME a relative without asking for their
// details ("Are you a close relative of a government official (…spouse/partner,
// parent/guardian…)?" is a yes/no about him and must still be answered).
const J_THIRD_PARTY = ["emergency contact", "emergency", "next of kin", "spouse",
  "guardian", "beneficiary", "dependent", "reference", "references", "referee",
  "supervisor", "previous manager", "parent guardian"];
const J_THIRD_PARTY_DETAIL = ["name", "email", "e mail", "phone", "mobile",
  "telephone", "address", "relationship", "contact number"];

// Safe option matching — the same two gates as content/fill.js and
// agents/autofill_mapper.py. Kept in sync by tests/js/choice_matching.mjs,
// which runs one shared REJECT/ACCEPT table against all three.
const jAnchored = (short, long) => long === short || long.startsWith(short + " ");

// The one option that is `c` plus a trailing qualifier. Ties refuse: matching
// "Bachelor of Science" against both "…in Physics" and "…in Economics" would
// otherwise pick him a major.
function jAnchoredMatch(c, opts) {
  if (!c) return null;
  const hits = opts.filter((o) => { const t = jnorm(o); return t && (jAnchored(c, t) || jAnchored(t, c)); });
  if (!hits.length) return null;
  if (hits.length === 1) return hits[0];
  hits.sort((a, b) => jnorm(a).split(" ").length - jnorm(b).split(" ").length);
  const n0 = jnorm(hits[0]).split(" ").length, n1 = jnorm(hits[1]).split(" ").length;
  return n0 !== n1 ? hits[0] : null;
}

// Gated token overlap: an option must contain EVERY distinctive word of the
// target. Ungated, "Master of Science" matched "Bachelor of Science" on the
// shared word "science" and filled a degree he does not hold.
function jTokenOverlap(nv, opts) {
  const dw = nv.split(" ").filter((w) => w.length > 2);
  if (!dw.length) return null;
  const covering = opts.filter((o) => { const ow = new Set(jnorm(o).split(" ")); return dw.every((w) => ow.has(w)); });
  if (!covering.length) return null;
  if (covering.length === 1) return covering[0];
  covering.sort((a, b) => jnorm(a).split(" ").length - jnorm(b).split(" ").length);
  const n0 = jnorm(covering[0]).split(" ").length, n1 = jnorm(covering[1]).split(" ").length;
  return n0 !== n1 ? covering[0] : null;
}

function jmap(field, p) {
  const type = (field.type || "text").toLowerCase();
  if (type === "file") return { value: null, source: "file", needs_review: true, confidence: 1 };
  const nl = jnorm(field.label || ""), nn = jnorm(field.name || "");
  const n = (nl + " " + nn).trim();
  if (!n) return null;
  const LEAVE_EMPTY = { value: null, source: "deterministic", needs_review: true, confidence: 1 };
  // someone else's details — we hold none, so flag it rather than fill his own
  if (jSub(n + " " + jnorm(field.section || ""), ...J_THIRD_PARTY) &&
      jSub(n, ...J_THIRD_PARTY_DETAIL)) return LEAVE_EMPTY;
  // a referral CODE is an identifier, not the "how did you hear" channel
  if (jSub(n, "referral", "referrer", "referred") && jTok(n, "code", "id", "number", "token"))
    return LEAVE_EMPTY;
  const id = p.identity || {}, ad = p.address || {}, li = p.links || {}, wa = p.work_authorization || {},
        ex = p.experience || {}, sa = p.salary || {}, rf = p.referral || {}, ee = p.eeoc || {},
        ed = (p.education || [])[0] || {}, pr = p.preferences || {},
        w0 = (p.employment || [])[0] || {};
  const nsec = jnorm(field.section || "");
  if ((jSub(nsec, "employment", "work experience", "work history") || jSub(n, "employment")) && w0.company) {
    if (jTok(n, "company", "employer", "organization")) return T(w0.company);
    if (jTok(n, "title", "position", "role")) return T(w0.title);
  }
  const opts = field.options || [];
  const T = (v) => v ? { value: v, source: "deterministic", needs_review: false, confidence: 0.9 } : null;
  const O = (v) => {
    if (!v) return null;
    if (!opts.length) return { value: v, source: "deterministic", needs_review: false, confidence: 0.9 };
    const nv = jnorm(v);
    // anchored + tightest: "United States" → "United States of America", never
    // "…Minor Outlying Islands", and never "Home Economics" for "Economics"
    const m = opts.find((o) => jnorm(o) === nv) || jAnchoredMatch(nv, opts);
    return m ? { value: m, source: "deterministic", needs_review: false, confidence: 0.9 }
             : { value: v, source: "deterministic", needs_review: true, confidence: 0.5 };
  };
  // EEO-tolerant choice matcher (mirrors autofill_mapper._match_choice)
  // "dont wish" can never fire — jnorm turns "don't" into "don t"
  const DECLINE = ["decline", "do not wish", "prefer not", "not to answer", "not wish",
                   "choose not", "rather not", "wish to answer", "want to answer",
                   "wish to disclose", "not to disclose", "not to identify", "not specified"];
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
      if (!m) {
        const AFFIRM = ["confirmed", "confirm", "i confirm", "i agree", "agree", "i acknowledge",
                        "acknowledge", "i accept", "accept", "i understand", "i consent", "i have read"];
        const NEGATE = ["not", "i do not", "i dont", "i have not", "i am not", "i havent", "never", "none"];
        const hints = nv === "yes" ? AFFIRM : NEGATE;
        m = opts.find((o) => { const no = jnorm(o); return hints.some((h) => no === h || no.startsWith(h + " ")); });
      }
    }
    if (!m) for (const c of cands) { m = jAnchoredMatch(c, opts); if (m) break; }
    if (!m) m = jTokenOverlap(nv, opts);
    return m ? { value: m, source: "deterministic", needs_review: false, confidence: 0.9 }
             : { value: v, source: "deterministic", needs_review: true, confidence: 0.5 };
  };
  const YN = (b) => C(b ? "Yes" : "No");

  if (jTok(n, "first", "given") && jTok(n, "name")) return T(id.first_name);
  if (jTok(n, "last", "surname", "family") && jTok(n, "name")) return T(id.last_name);
  if (["name", "full name", "your name", "legal name"].includes(nl) ||
      ["name", "full name", "your name", "legal name"].includes(nn)) return T(id.full_name);
  // SMS/WhatsApp consent fine print mentions "email"/"telephone" — check first
  if (jSub(n, "sms", "whatsapp", "text message")) return YN(pr.sms_opt_in !== false);
  if (jSub(n, "email")) return T(id.email);
  if (jSub(n, "country code", "country phone", "phone country", "phone code")) return O(ad.country);
  if (jSub(n, "phone device", "device type", "phone type")) return O("Mobile");
  // "Phone Extension" stays empty — decide before the phone rule
  if (jTok(n, "extension", "ext")) return { value: null, source: "deterministic", needs_review: false, confidence: 1 };
  if (jSub(n, "phone", "mobile", "telephone")) return T(id.phone);
  if (jSub(n, "linkedin")) return T(li.linkedin);
  if (jSub(n, "github")) return T(li.github);
  if (jSub(n, "sponsor")) return YN(!!wa.requires_sponsorship);
  if ((jSub(n, "authorized", "authorization", "eligible", "permitted") && jSub(n, "work")) ||
      jSub(n, "work authorization", "right to work"))
    return YN(wa.authorized_to_work_us !== false);
  if (jSub(n, "citizen")) return T(wa.citizenship);
  // relocation / on-site willingness (already in SF — yes to both)
  if (jSub(n, "relocat", "willing to move", "open to moving")) return YN(pr.willing_to_relocate !== false);
  if (jSub(n, "in person", "onsite", "on site", "in office", "in the office", "hybrid", "commute") &&
      jSub(n, "willing", "are you", "can you", "able to", "comfortable", "open to"))
    return YN(pr.willing_onsite !== false);
  if (jSub(n, "transgender")) return C(ee.transgender || "No");
  if (jSub(n, "sexual orientation") || jTok(n, "orientation")) return C(ee.sexual_orientation);
  if (jSub(n, "lgbtq", "lgbtqia", "lgbt")) return C(ee.lgbtq);
  if (jSub(n, "hispanic", "latino", "latinx")) return C(ee.hispanic_latino);
  if (jSub(n, "gender")) return C(ee.gender);
  if (jSub(n, "race", "ethnicity")) {
    // checkbox "select all that apply": components, one box per ";" part
    const rc = ee.race_components || [];
    if (type === "checkbox" && rc.length > 1)
      return { value: rc.join("; "), source: "deterministic", needs_review: false, confidence: 0.9 };
    return C(ee.race_ethnicity);
  }
  if (jSub(n, "veteran", "military")) return C(ee.veteran_status);
  if (jSub(n, "disability", "disabled")) return C(ee.disability_status);
  // token match, not substring: "prospective INSTITUTIONAL client" is not a school
  if (jTok(n, "school", "university", "college", "institution", "institute") && !jSub(n, "high school"))
    return ed.school ? (opts.length ? O(ed.school) : T(ed.school)) : null;
  if (jSub(n, "discipline", "major", "field of study")) return C(ed.discipline || ed.field_of_study);
  if (jTok(n, "gpa")) return T(ed.gpa);
  if (jTok(n, "degree") && !jSub(n, "highest")) return opts.length ? C(ed.degree) : T(ed.degree_full || ed.degree);
  if ((jTok(n, "location") && !jSub(n, "office")) ||
      jSub(n, "where are you located", "currently located", "where do you live", "where are you based",
           "currently based", "currently reside", "city of residence", "current city")) {
    if (opts.length) {
      // the list may be cities, states, OR countries — try each granularity
      for (const c of [ad.city, ad.state_full, ad.country]) {
        if (c) { const m = O(c); if (m && !m.needs_review) return m; }
      }
      return O(ad.city);
    }
    return T(ad.location_line || ad.city);
  }
  if (jSub(n, "when can you start", "when could you start", "when can you begin",
           "available to start", "earliest start", "start date", "date available",
           "availability date", "want to start working", "when do you want to start"))
    return T(pr.earliest_start_date);
  if (jSub(n, "worked at", "worked for", "worked here", "previously employed", "been employed by", "employed by"))
    return YN(!!pr.worked_here_before);
  if (jSub(n, "18 years", "at least 18", "age of 18", "over 18", "minimum age")) return YN(pr.age_18_or_older !== false);
  if (jSub(n, "government official", "government agency", "public office"))
    return jSub(n, "relative") ? YN(!!pr.relative_of_government_official) : YN(!!pr.government_official);
  if (jSub(n, "conflict of interest")) return YN(!!pr.conflict_of_interest);
  if (jSub(n, "were you referred", "referred to this position", "referred to this role")) return YN(!!pr.referred_by_insider);
  if (jSub(n, "how you use ai", "you use ai tools", "your use of ai")) return pr.ai_tools_usage ? C(pr.ai_tools_usage) : null;
  if (jSub(n, "privacy", "acknowledg", "consent", "i understand", "confirm receipt", "i have read"))
    return YN(pr.privacy_acknowledged !== false);
  // Line 2 (apt/suite) is optional — deterministic no-fill. Checked before
  // line 1: "street address line 2" also contains "street address".
  if (jSub(n, "address line 2", "address 2") || jTok(n, "apt", "apartment", "suite"))
    return { value: null, source: "deterministic", needs_review: false, confidence: 1 };
  if (jSub(n, "address line 1", "address 1", "street address") || jTok(n, "street")) return T(ad.street);
  if (jSub(n, "home address", "mailing address", "current address", "residential address"))
    return T([ad.street, ad.city, ad.state].filter(Boolean).join(", ") + (ad.postal_code ? " " + ad.postal_code : ""));
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
        url: ctx.url || "", job_title: ctx.h1 || ctx.title || "", company: ctx.company || "",
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

function companyFromUrl(u) {
  try {
    const url = new URL(u || "");
    const h = url.hostname;
    const seg = url.pathname.split("/").filter(Boolean);
    if (/greenhouse\.io$/i.test(h)) return seg[0] || "";  // job-boards.greenhouse.io/<board>/jobs/<id>
    if (/lever\.co$/i.test(h)) return seg[0] || "";
    if (/ashbyhq\.com$/i.test(h)) return seg[0] || "";
    if (/myworkdayjobs\.com$|myworkdaysite\.com$/i.test(h)) return h.split(".")[0] || "";
    return "";
  } catch (e) { return ""; }
}

const isWorkdayUrl = (u) => /myworkdayjobs\.com|myworkdaysite\.com|workday\.com/i.test(u || "");
const isGreenhouseUrl = (u) => /greenhouse\.io/i.test(u || "");

// Fill everything on the CURRENT page/step: wizard engines first (Workday and
// Greenhouse own their structured sections and tag them data-jpaf-owned), then
// the flat scan→plan→fill pass per frame.
async function fillPageOnce(tab, ctx, resumePref) {
  const totals = { filled: 0, needs_review: 0, file_flags: 0 };
  let planMeta = null, lastError = null, sawFields = false;

  if (isWorkdayUrl(tab.url)) {
    try {
      const res = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => (window.__jpafWorkdayRun ? window.__jpafWorkdayRun() : null),
      });
      const s = res && res[0] && res[0].result;
      if (s && s.filled) { totals.filled += s.filled; sawFields = true; }
    } catch (e) { /* wizard engine unavailable — flat fill still runs */ }
  }
  if (isGreenhouseUrl(tab.url)) {
    try {
      const res = await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true },
        func: () => (window.__jpafGreenhouseRun ? window.__jpafGreenhouseRun() : null),
      });
      for (const r of res || [])
        if (r && r.result && r.result.filled) { totals.filled += r.result.filled; sawFields = true; }
    } catch (e) { /* engine unavailable — flat fill still runs */ }
  }

  // scan every frame we can reach — company sites often embed the real ATS
  // form in an iframe (Greenhouse/Lever/Ashby embeds). Frames we lack host
  // permissions for are silently skipped by Chrome.
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
      return { totals, planMeta, lastError: "Scan failed: " + e2.message, sawFields };
    }
  }

  // per frame: fetch a plan, apply it (each frame is its own form)
  for (const scan of scans) {
    sawFields = true;
    const plan = await fetchPlan(scan.fields, ctx, resumePref);
    if (plan.error) { lastError = plan.error; continue; }
    plan._scanMeta = scan.fields;
    plan._ctx = { company: ctx.company || "", title: ctx.h1 || ctx.title || "" };
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
  return { totals, planMeta, lastError, sawFields };
}

// Page context (top frame): title/h1/body text + company guess from the URL.
// textLimit 4000 for fill-plans; the tailor chain wants the whole JD (12000).
async function collectJobCtx(tab, textLimit) {
  let ctx = { url: tab.url || "", title: tab.title || "", h1: "", text: "",
              company: companyFromUrl(tab.url) };
  try {
    const c = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: (lim) => ({
        title: document.title,
        h1: (document.querySelector("h1") || {}).innerText || "",
        text: document.body ? document.body.innerText.slice(0, lim) : "",
      }),
      args: [textLimit || 4000],
    });
    if (c && c[0] && c[0].result) ctx = { ...ctx, ...c[0].result };
  } catch (e) { /* non-fatal */ }
  return ctx;
}

async function runAutofill(resumePref) {
  const tab = await getActiveTab();
  if (!tab || !tab.id) return { error: "No active tab." };
  if (/^(chrome|edge|about|chrome-extension|view-source):/i.test(tab.url || ""))
    return { error: "Can't autofill this page." };

  await cacheProfile();

  // page context for archetype routing + essays (top frame)
  const ctx = await collectJobCtx(tab, 4000);

  // Fill the current page; on Workday, keep advancing (Next / Save and
  // Continue — NEVER Submit or the review step) and filling each new step,
  // up to a page cap. Other ATSes are single-page: one pass.
  const grand = { filled: 0, needs_review: 0, file_flags: 0 };
  let planMeta = null, lastError = null, sawAny = false, pages = 0, stoppedAt = "";
  const MAX_PAGES = 7;
  while (true) {
    const r = await fillPageOnce(tab, ctx, resumePref);
    grand.filled += r.totals.filled;
    grand.needs_review += r.totals.needs_review;
    grand.file_flags += r.totals.file_flags;
    planMeta = planMeta || r.planMeta;
    lastError = r.lastError || lastError;
    sawAny = sawAny || r.sawFields;
    pages++;
    if (!isWorkdayUrl(tab.url) || pages >= MAX_PAGES) break;
    let adv = null;
    try {
      const res = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => (window.__jpafWorkdayNext ? window.__jpafWorkdayNext() : { clicked: false }),
      });
      adv = res && res[0] && res[0].result;
    } catch (e) { adv = null; }
    if (!adv || !adv.clicked) { stoppedAt = (adv && adv.reason) || ""; break; }
  }

  if (!sawAny) return { error: lastError || "No application fields detected on this page." };
  if (!planMeta && !grand.filled) return { error: lastError || "Could not build a fill plan." };

  // Autofill success == the user is applying here — put it on the JobPilot
  // board right away (server side is idempotent, never regresses a status).
  let board = null;
  try {
    const r = await fetch(`${BACKEND}/api/applied/record`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: tab.url, title: ctx.h1 || ctx.title || "",
                             company: ctx.company || "",
                             lead_source: "autofill", source: "extension" }),
    });
    if (r.ok) board = await r.json();
  } catch (e) { /* board tracking is best-effort — never fail the fill */ }

  return { ok: true, stats: grand, pages, stopped_at: stoppedAt,
           resume: planMeta && planMeta.resume_used,
           archetype: planMeta && planMeta.archetype_label,
           offline: !!(planMeta && planMeta._offline),
           board };
}

// ---- Company scan / save-to-board / tailor chain ----

async function companyScan() {
  const tab = await getActiveTab();
  if (!tab || !tab.url) return { error: "No active tab." };
  try {
    const r = await fetch(`${BACKEND}/api/extension/company-scan`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: tab.url }),
    });
    return await r.json();
  } catch (e) { return { error: "JobPilot backend offline — start it and retry." }; }
}

async function saveJob(job, ensureScore) {
  try {
    const r = await fetch(`${BACKEND}/api/extension/job`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: job.url, title: job.title || "",
                             company: job.company || "",
                             description: job.description || "",
                             location: job.location || "",
                             ensure_score: !!ensureScore }),
    });
    return await r.json();
  } catch (e) { return { error: "JobPilot backend offline — start it and retry." }; }
}

// Save the job on the current page, make sure it's scored, then tailor a
// one-page résumé + cover letter for it. Slow (local LLM) — the popup shows
// progress. Autofill picks the tailored .docx up automatically afterwards
// (/api/autofill/resume_file prefers a tailored file for this company/role).
async function tailorCurrentJob() {
  const tab = await getActiveTab();
  if (!tab || !tab.id) return { error: "No active tab." };
  const ctx = await collectJobCtx(tab, 12000);
  const saved = await saveJob({
    url: ctx.url, title: ctx.h1 || ctx.title, company: ctx.company,
    description: ctx.text,
  }, true);
  if (saved.error || !saved.ok) return { error: saved.error || "Could not save this job." };
  if (!saved.scored)
    return { error: "Saved to board, but scoring failed" +
             (saved.score_error ? ` (${saved.score_error})` : " — is Ollama running?") };
  try {
    const r = await fetch(`${BACKEND}/api/application/${saved.app_id}/tailor`, { method: "POST" });
    const t = await r.json();
    if (!t.ok) return { error: t.error || "Tailoring failed.", app_id: saved.app_id };
    return { ok: true, resume_filename: t.resume_filename,
             cover_filename: t.cover_filename,
             fit_score: saved.fit_score, app_id: saved.app_id };
  } catch (e) { return { error: "Tailoring request failed: " + e.message }; }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (msg.cmd === "health") sendResponse(await health());
    else if (msg.cmd === "profile") sendResponse(await cacheProfile());
    else if (msg.cmd === "autofill") sendResponse(await runAutofill(msg.resumePref));
    else if (msg.cmd === "plan") { await cacheProfile(); sendResponse(await fetchPlan(msg.fields, msg.ctx, msg.resumePref)); }
    else if (msg.cmd === "history") sendResponse(await fetchHistory(msg.company, msg.title));
    else if (msg.cmd === "resume_file") sendResponse(await fetchFileB64("resume_file", msg.company, msg.title));
    else if (msg.cmd === "cover_letter_file") sendResponse(await fetchFileB64("cover_letter_file", msg.company, msg.title));
    else if (msg.cmd === "resume_meta") {
      try {
        const r = await fetch(`${BACKEND}/api/autofill/resume_meta?resume_pref=${encodeURIComponent(msg.resumePref || "auto")}`, { cache: "no-store" });
        sendResponse(r.ok ? await r.json() : { error: `HTTP ${r.status}` });
      } catch (e) { sendResponse({ error: String(e && e.message || e) }); }
    }
    else if (msg.cmd === "resume_upload") {
      try {
        const r = await fetch(`${BACKEND}/api/autofill/resume_upload`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ resume_pref: msg.resumePref || "ai",
                                 filename: msg.filename || "", b64: msg.b64 }),
        });
        sendResponse(await r.json());
      } catch (e) { sendResponse({ error: String(e && e.message || e) }); }
    }
    else if (msg.cmd === "company_scan") sendResponse(await companyScan());
    else if (msg.cmd === "save_job") sendResponse(await saveJob(msg.job || {}, msg.ensureScore));
    else if (msg.cmd === "tailor") sendResponse(await tailorCurrentJob());
    else if (msg.cmd === "mark_applied") {
      try {
        const r = await fetch(`${BACKEND}/api/applied/record`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: msg.url, title: msg.title || "",
                                 company: msg.company || "",
                                 source: "extension" }),
        });
        sendResponse(r.ok ? await r.json() : { error: `HTTP ${r.status}` });
      } catch (e) { sendResponse({ error: String(e && e.message || e) }); }
    }
    else sendResponse({ error: "unknown cmd" });
  })();
  return true; // keep the message channel open for the async response
});
