"""Generic auto-apply engine — drives the extension's autofill mapper from Playwright.

Replaces the per-ATS selector scripts (ashby.py / lever.py / greenhouse.py /
generic.py) whose hard-coded aria-labels rotted: 94 attempts, 0 submissions.
The Chrome extension already handles every ATS and is heavily tested —
browser-extension/content/scan.js (field list), POST /api/autofill/plan
(mapping + drafted answers) and fill.js (typing/choosing). This engine just
injects those two scripts into the page. The one thing fill.js cannot do here
is attach files (no service worker), so file inputs are set from Python.

Flow: navigate to the form → bail on CAPTCHA / login wall → attach résumé +
cover letter first (ATSs parse the résumé and re-render) → scan → plan → fill,
then once more for fields that appeared conditionally → required-field audit
→ submit → verify.

Statuses this engine adds (both permanent in the runner):
  failed_required_fields_unfilled  — form still has empty required fields;
                                     dashboard shows "Needs manual apply".
  submitted_unverified             — Submit clicked, no confirmation seen;
                                     never retried so we never double-apply.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import httpx

from agents.auto_applier.base import (ApplyResult, BaseAutoApplier, has_captcha,
                                      has_login_wall, humanize_delay)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
EXT_CONTENT = ROOT / "browser-extension" / "content"
BACKEND = "http://127.0.0.1:7777"

# Something the application form has and a job description / careers index does not.
FORM_HINT = "input[type='file'], input[type='email'], input[name*='email' i], textarea"

APPLY_CONTROLS = (
    "a#apply_button",
    "a.postings-btn:has-text('Apply')",
    "a:has-text('Apply for this job')", "button:has-text('Apply for this')",
    "a:has-text('Apply now')", "button:has-text('Apply now')",
    "a:has-text('Apply')", "button:has-text('Apply')",
)
SUBMIT_CONTROLS = (
    "button:has-text('Submit application')",
    "button[type='submit']:not([disabled])",
    "input[type='submit']:not([disabled])",
    "button:has-text('Submit')",
)
CONFIRMATION_SIGNALS = ("thank you for applying", "thanks for applying", "application received",
                        "application submitted", "application has been submitted",
                        "submitted successfully", "received your application")

# Required fields (per scan meta) that are still blank after filling.
_EMPTY_REQUIRED_JS = """
(metas) => {
  const PLACEHOLDER = /^(|select|select one|select an option|select\\.\\.\\.|choose|choose one|please select|none|n\\/?a|-+)$/i;
  const out = [];
  for (const m of metas) {
    if (!m.required) continue;
    const el = document.querySelector(`[data-jpaf-id="${m.id}"]`);
    if (!el) continue;
    if (m.type !== 'file' && el.getClientRects().length === 0) continue;  // hidden → not asked
    const tag = el.tagName.toLowerCase();
    const type = (el.type || '').toLowerCase();
    let empty;
    if (type === 'file') empty = !(el.files && el.files.length);
    else if (type === 'radio' || type === 'checkbox') {
      const group = el.name ? [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)] : [el];
      empty = !group.some((r) => r.checked);
    } else if (tag === 'select') {
      const o = el.selectedOptions && el.selectedOptions[0];
      empty = PLACEHOLDER.test(((o ? o.text : el.value) || '').trim());
    } else if (tag === 'input' || tag === 'textarea') {
      // react-select clears its input once an option is committed and re-mounts the
      // node (losing fill.js's verify mark) — the chosen value lives in the control.
      const ctl = el.closest('[class*="control"], [class*="Control"]');
      const chosen = ctl && (ctl.querySelector('[class*="single-value"], [class*="singleValue"], ' +
                                               '[class*="multi-value"], [class*="multiValue"]') ||
                             ctl.querySelector('[class*="has-value"]'));
      empty = !(el.value || '').trim() && el.getAttribute('data-jpaf-state') !== 'verified' && !chosen;
    } else {
      empty = el.getAttribute('data-jpaf-state') !== 'verified' && PLACEHOLDER.test((el.innerText || '').trim());
    }
    if (empty) out.push(m.label || m.name || m.id);
  }
  return out;
}
"""

# Scan meta the extension's scan.js gets wrong on these boards:
#  * Lever custom-question cards — the question sits in .application-question
#    > .application-label, so scan.js falls back to the card title ("USA CORP").
#  * radio / checkbox groups carry no `options`, so the mapper can't bind an
#    answer to a choice.
# ponytail: belongs in scan.js labelFor(); moved here because the extension is
# owned elsewhere. Drop this once scan.js handles both.
_ENRICH_JS = """
(metas) => metas.map((m) => {
  const el = document.querySelector(`[data-jpaf-id="${m.id}"]`);
  if (!el) return m;
  const out = Object.assign({}, m);
  const weak = !m.label || m.label === m.section || m.label === m.name ||
               m.label === (el.getAttribute('placeholder') || '') || /^\\w+\\[/.test(m.label);
  if (weak) {
    // Lever only — anything broader (e.g. [class*="question"]) walks up to the whole
    // Greenhouse form and hands every weak field the first label on the page.
    const q = el.closest('.application-question');
    const lab = q && q.querySelector('.application-label');
    const t = lab && lab.innerText.replace(/\\s+/g, ' ').replace(/[✱*]/g, '').trim();
    if (t) out.label = t.slice(0, 200);
  }
  const type = (el.type || '').toLowerCase();
  if ((type === 'radio' || type === 'checkbox') && el.name && !out.options) {
    const group = [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)];
    out.options = group.map((r) => {
      const l = r.closest('label') || (r.id && document.querySelector(`label[for="${CSS.escape(r.id)}"]`));
      return ((l && l.innerText) || r.value || '').replace(/\\s+/g, ' ').trim();
    }).filter(Boolean);
  }
  return out;
})
"""

# Options of an OPEN combobox (react-select & co.): the listbox it aria-controls.
# > 40 options = a searchable autocomplete (countries, cities) — fill.js types into those.
_COMBO_OPTIONS_JS = """
(el) => {
  const id = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
  const box = id && document.getElementById(id);
  if (!box) return null;
  const opts = [...box.querySelectorAll('[role="option"]')].map((o) => o.innerText.replace(/\\s+/g, ' ').trim()).filter(Boolean);
  return opts.length > 40 ? null : opts;
}
"""

# Post-submit snapshot: is the form still there, did the browser/ATS flag fields invalid?
_FORM_STATE_JS = """
() => {
  const tagged = [...document.querySelectorAll('[data-jpaf-id]')];
  const invalid = tagged.filter((e) => e.getAttribute('aria-invalid') === 'true' ||
                                (typeof e.checkValidity === 'function' && !e.checkValidity()))
                        .map((e) => e.getAttribute('data-jpaf-id'));
  return { form_present: tagged.some((e) => e.getClientRects().length > 0), invalid,
           text: (document.body ? document.body.innerText : '').slice(0, 20000).toLowerCase() };
}
"""


class MapperApplier(BaseAutoApplier):
    ats_name = "mapper"

    # ----- entry ---------------------------------------------------------------

    def apply(self, page, job, resume_path: str, cover_letter_path: str, result: ApplyResult) -> None:
        self._goto_form(page, result)
        if has_captcha(page):
            return self._fail(result, "failed_captcha", "CAPTCHA on application form")
        if has_login_wall(page):
            return self._fail(result, "failed_login_required", "Application form is behind a sign-in wall")

        fields = self._scan(page)
        if not fields:
            return self._fail(result, "failed_form_not_found", f"No form fields found at {page.url}")

        # 1. Files first — Ashby/Greenhouse parse the résumé and re-render the form.
        attached = self._attach_files(page, fields, resume_path, cover_letter_path, result)
        if attached["file_fields"] == 0:
            return self._fail(result, "failed_no_resume_upload", "Form has no file input for a résumé")
        if not attached["resume"]:
            return self._fail(result, "failed_resume_field_not_found", "Could not attach the résumé to any file input")
        time.sleep(self.settle_seconds)

        # 2. scan → plan → fill; a second pass catches conditional fields that
        #    only render after the first answers land.
        totals = {"filled": 0, "needs_review": 0, "kept": 0, "llm": 0, "planned": 0}
        planned: set[tuple] = set()
        for pass_no in (1, 2):
            fields = self._scan(page)
            todo = [f for f in fields if f.get("type") != "file" and self._key(f) not in planned]
            if not todo:
                break
            planned.update(self._key(f) for f in todo)
            self._harvest_combo_options(page, todo)
            plan = self._plan(page, job, todo)
            # essays = LLM prose; an LLM picking "Yes" from a harvested option list is not one
            free_text = {f["id"] for f in todo if not f.get("options")}
            totals["llm"] += sum(1 for f in plan.get("fields", [])
                                 if f.get("source") == "llm" and f["id"] in free_text)
            totals["planned"] += len(todo)
            stats = self._apply_plan(page, plan, fields, job)
            for k in ("filled", "needs_review", "kept"):
                totals[k] += int(stats.get(k) or 0)
            result.add_step("fill_pass", f"#{pass_no}: planned={len(todo)} filled={stats.get('filled')} "
                                         f"review={stats.get('needs_review')} kept={stats.get('kept')}")
            self._attach_files(page, fields, resume_path, cover_letter_path, result)  # newly rendered file inputs
            time.sleep(1.5)

        g = self.profile.get("guardrails", {})
        cap = int(g.get("max_essays_per_app", 3))
        if totals["llm"] > cap and g.get("bail_on_required_essay", True):
            return self._fail(result, "failed_too_many_essays",
                              f"{totals['llm']} LLM-drafted answers exceed cap {cap} — manual review")

        # 3. Never submit a form with blank required fields, never invent answers.
        fields = self._scan(page)
        empty = page.evaluate(_EMPTY_REQUIRED_JS, fields)
        if empty:
            return self._fail(result, "failed_required_fields_unfilled",
                              "Required fields left empty: " + "; ".join(str(e)[:60] for e in empty[:8]))

        if has_captcha(page):
            return self._fail(result, "failed_captcha", "CAPTCHA appeared before submit")

        summary = (f"fields {len(fields)}, filled {totals['filled']}, kept {totals['kept']}, "
                   f"review {totals['needs_review']}, essays {totals['llm']}, résumé attached, "
                   f"cover letter {'attached' if attached['cover'] else 'n/a'}")
        if self.dry_run:
            result.success = True
            result.status = "dry_run"
            result.message = f"Would have submitted (dry_run): {summary}"
            result.add_step("dry_run_skip_submit", "ok")
            return

        # 4. Submit + verify.
        form_url = page.url
        before = page.evaluate("(document.body ? document.body.innerText : '').toLowerCase()")
        humanize_delay(800, 1500)
        if not self._click_first(page, SUBMIT_CONTROLS, result, "submit"):
            return self._fail(result, "failed_no_submit_button", "No visible submit button on the form")
        status, msg = self._verify_submit(page, form_url, before, fields)
        result.status = status
        result.success = status == "submitted"
        result.message = f"{msg} ({summary})"
        result.add_step("submit_verify", msg, success=result.success)

    # ----- navigation ------------------------------------------------------------

    def _goto_form(self, page, result: ApplyResult) -> None:
        url = page.url.split("?")[0].split("#")[0].rstrip("/")
        if "ashbyhq.com" in url and not url.endswith("/application"):
            self._nav(page, url + "/application")
        elif "lever.co" in url and not url.endswith("/apply"):
            self._nav(page, url + "/apply")
        else:
            self._settle(page)
            if not self._enter_embedded_greenhouse(page, result) and page.locator(FORM_HINT).count() == 0:
                if self._click_first(page, APPLY_CONTROLS, result, "apply"):
                    self._settle(page)
                    self._enter_embedded_greenhouse(page, result)
        result.add_step("navigated", page.url)

    def _nav(self, page, url: str) -> None:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self._settle(page)

    def _settle(self, page) -> None:
        # ponytail: never networkidle — analytics/SSE keep these pages busy forever
        for fn in (lambda: page.wait_for_load_state("domcontentloaded", timeout=15000),
                   lambda: page.wait_for_selector(FORM_HINT, timeout=10000, state="attached")):
            try:
                fn()
            except Exception:
                pass
        time.sleep(self.settle_seconds)

    def _enter_embedded_greenhouse(self, page, result: ApplyResult) -> bool:
        """Company careers pages embed the Greenhouse form in an iframe — go to its src."""
        if page.locator("input[type='file']").count() > 0:
            return False
        for sel in ("iframe#grnhse_iframe", "iframe[src*='greenhouse.io']", "iframe[src*='greenhouse.dev']"):
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            src = loc.get_attribute("src") or ""
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                self._nav(page, src)
                result.add_step("entered_embedded_board", src)
                return True
        return False

    def _click_first(self, page, selectors, result: ApplyResult, label: str) -> bool:
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() and loc.is_visible():
                    humanize_delay()
                    loc.click(timeout=8000)
                    result.add_step("click", f"{label} via {sel}")
                    return True
            except Exception as e:
                result.add_step("click_failed", f"{label} {sel}: {e}", success=False)
        return False

    # ----- extension engine -----------------------------------------------------

    def _inject(self, page) -> None:
        if page.evaluate("typeof window.__jpafScan === 'function' && typeof window.__jpafApply === 'function'"):
            return
        page.add_script_tag(path=str(EXT_CONTENT / "scan.js"))
        page.add_script_tag(path=str(EXT_CONTENT / "fill.js"))

    def _scan(self, page) -> list[dict]:
        self._inject(page)
        fields = page.evaluate("window.__jpafScan()") or []
        return page.evaluate(_ENRICH_JS, fields) or fields

    def _harvest_combo_options(self, page, fields: list[dict]) -> None:
        """Open each option-less combobox once and record its choices for the planner."""
        for f in fields:
            if not f.get("combo") or f.get("options"):
                continue
            loc = page.locator(f'[data-jpaf-id="{f["id"]}"]').first
            try:
                loc.click(timeout=3000)
                page.wait_for_timeout(300)
                opts = loc.evaluate(_COMBO_OPTIONS_JS)
                page.keyboard.press("Escape")
                page.wait_for_timeout(150)
            except Exception:
                continue
            if opts and len(opts) > 1:
                f["options"] = opts

    def _plan(self, page, job, fields: list[dict]) -> dict:
        text = page.evaluate("document.body ? document.body.innerText.slice(0, 8000) : ''")
        r = httpx.post(f"{BACKEND}/api/autofill/plan", timeout=120, json={
            "url": page.url, "job_title": job.title or "", "company": job.company or "",
            "page_text": text, "resume_pref": "auto", "fields": fields})
        r.raise_for_status()
        return r.json()

    def _apply_plan(self, page, plan: dict, scan_meta: list[dict], job) -> dict:
        plan["_scanMeta"] = scan_meta
        plan["_ctx"] = {"company": job.company or "", "title": job.title or ""}
        return page.evaluate("(p) => window.__jpafApply(p)", plan) or {}

    @staticmethod
    def _key(f: dict) -> tuple:
        return (f.get("label"), f.get("name"), f.get("section"), f.get("type"))

    # ----- files -------------------------------------------------------------------

    def _attach_files(self, page, fields, resume_path, cover_letter_path, result: ApplyResult) -> dict:
        file_fields = [f for f in fields if (f.get("type") or "") == "file"]
        out = {"file_fields": len(file_fields), "resume": False, "cover": False}
        for f in file_fields:
            lab = " ".join(str(f.get(k) or "") for k in ("label", "name", "section")).lower()
            is_cover = "cover" in lab
            is_resume = not is_cover and (
                bool(re.search(r"resume|résumé|curriculum|\bcv\b", lab))
                or (len(file_fields) == 1 and not re.search(r"transcript|portfolio|photo", lab)))
            path = self._resume_file(resume_path) if is_resume else (cover_letter_path if is_cover else None)
            if not path or not Path(path).exists():
                continue
            key = "resume" if is_resume else "cover"
            loc = page.locator(f'[data-jpaf-id="{f["id"]}"]').first
            if loc.count() == 0:
                continue
            try:
                if loc.evaluate("e => !!(e.files && e.files.length)"):
                    out[key] = True
                    continue
                loc.set_input_files(str(path))
                out[key] = True
                result.add_step("upload", f"{key} <- {Path(path).name} ({f.get('label') or f.get('name')})")
                humanize_delay(800, 1500)
            except Exception as e:
                result.add_step("upload_failed", f"{key} ({f.get('label') or f.get('name')}): {e}", success=False)
        return out

    @staticmethod
    def _resume_file(resume_path: str) -> str:
        """The Word-verified one-page PDF next to the tailored .docx when the tailor made one."""
        p = Path(resume_path)
        pdf = p.with_suffix(".pdf")
        return str(pdf if pdf.exists() else p)

    # ----- submit verification ------------------------------------------------------

    def _verify_submit(self, page, form_url: str, before_text: str, fields: list[dict]) -> tuple[str, str]:
        labels = {f["id"]: f.get("label") or f.get("name") or f["id"] for f in fields}
        signals = [s for s in CONFIRMATION_SIGNALS if s not in before_text]  # ignore JD boilerplate
        deadline = time.time() + 20
        gone_since = None
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                if page.url.split("#")[0] != form_url.split("#")[0]:
                    return "submitted", f"URL changed to {page.url}"
                state = page.evaluate(_FORM_STATE_JS)
            except Exception:
                continue  # mid-navigation
            if any(s in state.get("text", "") for s in signals):
                return "submitted", "Confirmation text seen"
            if has_captcha(page):
                return "failed_captcha", "CAPTCHA challenge appeared at submit"
            if not state.get("form_present"):
                gone_since = gone_since or time.time()
                if time.time() - gone_since >= 2:
                    return "submitted", "Form gone after submit"
                continue
            gone_since = None
            if state.get("invalid"):
                bad = [str(labels.get(i, i))[:60] for i in state["invalid"]]
                return "failed_required_fields_unfilled", "Form rejected submit — invalid: " + "; ".join(bad[:8])
        return ("submitted_unverified",
                f"Submit clicked but no confirmation within 20s at {page.url} — "
                "check your email before applying again")

    @staticmethod
    def _fail(result: ApplyResult, status: str, message: str) -> None:
        result.success = False
        result.status = status
        result.message = message
        result.add_step(status, message, success=False)
