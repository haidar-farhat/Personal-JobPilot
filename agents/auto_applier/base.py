"""Shared utilities for ATS auto-appliers — Playwright context, profile loader, CAPTCHA detection.

Every ATS-specific filler subclasses BaseAutoApplier and implements `apply()`.
The base class owns:
  * Browser/context lifecycle (with realistic user-agent + cookie settling)
  * Profile loading from config/applicant_profile.yaml
  * CAPTCHA / honeypot detection helpers
  * Standardized result reporting (ApplyResult)
  * Action logging (every fill / click is recorded for replay)
"""

from __future__ import annotations

import json
import logging
import time
import random
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# ============================================================
# Result shape
# ============================================================

@dataclass
class ApplyResult:
    """The outcome of one auto-apply attempt."""
    success: bool
    status: str  # "submitted", "failed_captcha", "failed_required_field", "failed_missing_files", "failed_navigation", "failed_unknown", "dry_run"
    message: str = ""
    log: list[dict] = field(default_factory=list)
    duration_seconds: float = 0.0

    def add_step(self, action: str, detail: str = "", success: bool = True) -> None:
        """Record one action the bot took."""
        self.log.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "detail": detail,
            "success": success,
        })

    def to_log_json(self) -> str:
        """Serialize the log for storage in the DB."""
        return json.dumps({
            "status": self.status,
            "message": self.message,
            "duration_seconds": round(self.duration_seconds, 2),
            "steps": self.log,
        })


# ============================================================
# Profile loader
# ============================================================

_PROFILE_CACHE: dict | None = None


def load_profile() -> dict:
    """Load and cache the applicant profile from config/applicant_profile.yaml."""
    global _PROFILE_CACHE
    if _PROFILE_CACHE is None:
        config_path = Path(__file__).parent.parent.parent / "config" / "applicant_profile.yaml"
        with open(config_path) as f:
            _PROFILE_CACHE = yaml.safe_load(f)
    return _PROFILE_CACHE


def reload_profile() -> dict:
    """Force-reload the profile (useful when settings change mid-run)."""
    global _PROFILE_CACHE
    _PROFILE_CACHE = None
    return load_profile()


# ============================================================
# CAPTCHA / honeypot detection
# ============================================================

CAPTCHA_INDICATORS = (
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='cloudflare']",
    "div.g-recaptcha",
    "div.h-captcha",
    "div.cf-turnstile",
    "div[id*='captcha']",
    "div[class*='captcha']",
)


def has_captcha(page) -> bool:
    """Check if the page contains any visible CAPTCHA challenge."""
    for selector in CAPTCHA_INDICATORS:
        try:
            loc = page.locator(selector)
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                # 2026-09-07: the invisible reCAPTCHA badge (every Greenhouse board)
                # and hCaptcha's passive enclave frame (Lever) are not challenges.
                if el.evaluate("e => !!e.closest('.grecaptcha-badge') || "
                               "/size=invisible|enclave/.test(e.getAttribute('src') || '')"):
                    continue
                if el.is_visible():
                    logger.warning(f"[auto_apply] CAPTCHA detected via {selector}")
                    return True
        except Exception:
            continue
    return False


# ============================================================
# Login-wall detection (Workday and many custom ATS systems require an account)
# ============================================================

LOGIN_INDICATORS = (
    "input[type='password']",
    "button:has-text('Sign In'):not([aria-label*='resume' i])",
    "button:has-text('Log In')",
    "button:has-text('Create Account')",
    "a:has-text('Create an Account')",
    "form[action*='login' i]",
    "form[action*='signin' i]",
)


def has_login_wall(page) -> bool:
    """Detect whether the page is gating us behind a sign-in / create-account flow.

    We treat any visible login control as a hard bail — auto-creating accounts
    on the user's behalf is explicitly forbidden by our security policy.
    """
    url = (page.url or "").lower()
    if any(p in url for p in ("/login", "/signin", "/sign-in", "/auth/")):
        return True
    for selector in LOGIN_INDICATORS:
        try:
            if page.locator(selector).count() > 0 and page.locator(selector).first.is_visible():
                logger.warning(f"[auto_apply] login wall detected via {selector}")
                return True
        except Exception:
            continue
    return False


# ============================================================
# Essay drafting via Gemma 4 (Ollama, local)
# ============================================================

ESSAY_SYSTEM_PROMPT = """You are an expert career coach drafting answers to job-application short-answer questions.

You produce concise, specific, professional answers in the candidate's voice — never generic, never AI-sounding.

Constraints:
- 2-4 short paragraphs, total length 80-180 words
- First-person, professional tone
- Reference at least one concrete fact from the candidate's profile (a project, a skill, an experience)
- Reference at least one concrete detail about the role or company when possible
- No corporate cliches ("synergy", "passionate about", "rockstar")
- No hedging openings ("I believe that...", "It is my opinion that...")
- Plain text only — no markdown, no bullet points unless the question explicitly asks
"""

ESSAY_PROMPT_TEMPLATE = """Draft a tailored answer to this application question.

QUESTION:
{question}

ABOUT THE ROLE:
Title: {job_title}
Company: {company}
Description excerpt:
{job_excerpt}

ABOUT THE CANDIDATE:
{candidate_summary}

FALLBACK TEMPLATE (use only as last-resort backbone if you can't think of anything specific):
{fallback}

Respond with ONLY the answer text, no preamble."""


def _candidate_summary_for_essay(profile: dict) -> str:
    """Compact 4-6 line description of the candidate suitable for prompt context."""
    identity = profile.get("identity", {})
    exp = profile.get("experience", {})
    return (
        f"Name: {identity.get('full_name', '')}\n"
        f"Education: {exp.get('highest_education', '')} (MS Quantitative Economics, Cal Poly SLO)\n"
        f"Background: BA Economics from Reed College; econometrics, statistics, machine learning\n"
        f"Skills: Python, R, SQL, Pandas, Sklearn, Statsmodels, Tableau\n"
        f"Notable work: Nested logit pricing model capstone (Rithm, 2025); "
        f"$150K portfolio management at Reed Finance & Investment Club; "
        f"behavioral economics thesis on gaming microtransactions\n"
        f"Currently: {exp.get('current_title', '')} at {exp.get('current_employer', '')} (transitioning to data science / analyst roles)"
    )


def _classify_essay_question(question: str, company: str | None = None) -> str:
    """Pick the best fallback essay template based on question keywords.

    Order matters: more specific patterns are checked first so a question like
    "What is your greatest weakness?" doesn't get caught by the more general
    "strength" check.

    `company` is optional — when provided, we treat any question that mentions
    the company name + an interest/motivation signal as a why_company question.
    """
    q = (question or "").lower()

    # Strength / weakness — check first (most specific)
    if any(k in q for k in ("weakness", "weaknesses", "improve", "growth area", "area to grow", "area for development")):
        return "weakness"
    if any(k in q for k in ("strength", "strengths", "best skill", "what are you good at", "what are you best at")):
        return "greatest_strength"

    why_signals = ("why ", "interest", "interested", "want to", "drawn to",
                   "attracted to", "passion", "motivat", "excites you")

    # Company-specific — multiple ways the question might phrase it
    company_signals = ("company", "our team", "the team", " us?", " us.", "join us",
                       "work here", "this organization", "our mission", "our values")
    if any(c in q for c in company_signals) and any(w in q for w in why_signals):
        return "why_company"

    # If the company name itself appears alongside an interest signal, that's a why_company
    if company:
        cname = company.strip().lower()
        if cname and len(cname) >= 3 and cname in q and any(w in q for w in why_signals):
            return "why_company"

    if any(k in q for k in ("why this company", "why us", "why our", "why are you interested in our",
                            "interest in our company", "interest in the company")):
        return "why_company"

    # Role-specific
    if any(k in q for k in ("why this role", "why are you applying", "why interested in this position",
                            "why this position", "why this job", "why are you a good fit",
                            "what makes you qualified", "what excites you about this role")):
        return "why_role"

    return "why_role"  # safest default


def draft_essay_answer(question: str, job, profile: dict, *, max_chars: int = 1500) -> str:
    """Generate a tailored essay answer using local Gemma 4 via Ollama.

    Falls back to a profile.fallback_essays template if Ollama is unreachable
    or returns garbage. Always returns SOMETHING the bot can fill in.
    """
    fallbacks = profile.get("fallback_essays", {})
    company = getattr(job, "company", "") or ""
    title = getattr(job, "title", "") or ""

    template_key = _classify_essay_question(question, company=company)
    fallback = fallbacks.get(template_key, fallbacks.get("why_role", ""))

    # Render fallback with company/role substitution so we never paste a literal "{company}"
    rendered_fallback = fallback.format(company=company, role=title) if "{" in fallback else fallback

    try:
        from utils.ollama_client import generate_text, check_ollama_health

        if not check_ollama_health():
            logger.info("[essay] Ollama unreachable — using fallback template")
            return rendered_fallback[:max_chars]

        job_excerpt = (getattr(job, "description", "") or "")[:1200]
        prompt = ESSAY_PROMPT_TEMPLATE.format(
            question=question.strip(),
            job_title=title,
            company=company,
            job_excerpt=job_excerpt,
            candidate_summary=_candidate_summary_for_essay(profile),
            fallback=rendered_fallback[:600],
        )
        answer = generate_text(prompt, system_prompt=ESSAY_SYSTEM_PROMPT)
        answer = (answer or "").strip()

        # Guard against empty or absurdly long Gemma output
        if len(answer) < 60 or len(answer) > max_chars:
            logger.warning(f"[essay] Gemma answer length {len(answer)} out of range, falling back")
            return rendered_fallback[:max_chars]

        return answer[:max_chars]
    except Exception as e:
        logger.warning(f"[essay] Gemma draft failed ({e}) — using fallback template")
        return rendered_fallback[:max_chars]


# ============================================================
# Essay-field detection + auto-fill
# ============================================================

def detect_required_essay_fields(page, *, min_height_px: int = 60) -> list[dict]:
    """Find required textareas / large text inputs that look like essay questions.

    Returns a list of {selector_id, label, locator_handle} dicts. Only includes
    fields that:
      - Are required (aria-required, [required], or wrapped in a "*"-marked label)
      - Are visible
      - Are large enough to plausibly be an essay (textarea OR input[type=text] with min-height)

    The caller is responsible for capping how many it fills (per the
    profile.guardrails.max_essays_per_app limit) and for actually drafting + filling.
    """
    found: list[dict] = []
    try:
        # Required textareas are the strongest essay signal
        textareas = page.locator(
            "textarea[required], textarea[aria-required='true'], "
            "textarea[data-required='true'], textarea[name*='answer' i]"
        )
        count = textareas.count()
        for i in range(count):
            try:
                el = textareas.nth(i)
                if not el.is_visible():
                    continue
                # Skip ones that already have content (likely cover letter paste)
                if (el.input_value() or "").strip():
                    continue
                label = _label_for_field(page, el)
                if not label:
                    continue
                found.append({
                    "kind": "textarea",
                    "label": label,
                    "index": i,
                })
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"[essay] textarea scan failed: {e}")
    return found


def _label_for_field(page, locator) -> str:
    """Best-effort: extract the human-readable label for an input/textarea."""
    try:
        # Try aria-label / aria-labelledby
        aria_label = locator.get_attribute("aria-label")
        if aria_label:
            return aria_label.strip()
        labelledby = locator.get_attribute("aria-labelledby")
        if labelledby:
            ref = page.locator(f"#{labelledby}")
            if ref.count() > 0:
                txt = ref.first.inner_text(timeout=1000)
                if txt:
                    return txt.strip()
        # Try id-based <label for=...>
        elem_id = locator.get_attribute("id")
        if elem_id:
            label_el = page.locator(f"label[for='{elem_id}']")
            if label_el.count() > 0:
                txt = label_el.first.inner_text(timeout=1000)
                if txt:
                    return txt.strip().rstrip("*").strip()
        # Try parent .field-label / div-with-label-text
        parent_text = locator.evaluate(
            "el => { let p = el.parentElement; for (let i=0; i<3 && p; i++) { "
            "const lbl = p.querySelector('label, .field-label, .question, .label-text'); "
            "if (lbl) return lbl.textContent.trim(); p = p.parentElement; } return ''; }"
        )
        if parent_text:
            return str(parent_text).strip()
    except Exception:
        pass
    return ""


def fill_required_essays(applier, page, job, *, log_to, max_essays: int = 3) -> dict:
    """Detect required essay textareas and fill them with Gemma-drafted answers.

    Returns:
        {"filled": int, "skipped_over_cap": int, "essay_count": int}
    Caller should treat a `bail` flag from the result if essays are required and
    we couldn't fill due to Ollama unreachable (we still attempt fallbacks).
    """
    essays = detect_required_essay_fields(page)
    if not essays:
        return {"filled": 0, "skipped_over_cap": 0, "essay_count": 0}

    log_to.add_step("essays_detected", f"count={len(essays)}", success=True)

    filled = 0
    skipped = 0

    # Re-resolve all matching textareas in DOM order (most stable)
    textareas = page.locator(
        "textarea[required], textarea[aria-required='true'], "
        "textarea[data-required='true'], textarea[name*='answer' i]"
    )

    for essay in essays[:max_essays]:
        try:
            idx = essay["index"]
            label = essay["label"]
            answer = draft_essay_answer(label, job, applier.profile)
            humanize_delay(400, 900)
            textareas.nth(idx).fill(answer)
            log_to.add_step(
                "essay_filled",
                f"{label[:60]} <- {len(answer)}ch",
                success=True,
            )
            filled += 1
        except Exception as e:
            log_to.add_step("essay_fill_failed", f"{essay.get('label','')[:60]}: {e}", success=False)
            skipped += 1

    skipped_over_cap = max(0, len(essays) - max_essays)
    if skipped_over_cap > 0:
        log_to.add_step(
            "essay_cap_hit",
            f"{skipped_over_cap} essays beyond max_essays={max_essays} not filled",
            success=False,
        )

    return {
        "filled": filled,
        "skipped_over_cap": skipped_over_cap,
        "essay_count": len(essays),
    }


# ============================================================
# Human-ish timing
# ============================================================

def humanize_delay(min_ms: int = 200, max_ms: int = 700) -> None:
    """Sleep a randomized amount to look less bot-like."""
    time.sleep(random.uniform(min_ms / 1000.0, max_ms / 1000.0))


# ============================================================
# Base class
# ============================================================

class BaseAutoApplier:
    """Abstract base for ATS-specific form fillers.

    Subclasses must implement `apply(page, job, profile, result)` which
    fills in the form on the already-loaded page and submits.
    """

    ats_name: str = "unknown"

    def __init__(self, profile: dict | None = None, dry_run: bool = False, settle_seconds: float = 2.0):
        self.profile = profile or load_profile()
        self.dry_run = dry_run
        self.settle_seconds = settle_seconds  # how long to wait after navigation before interacting

    # ----- subclass hook ------------------------------------------------------

    def apply(self, page, job, resume_path: str, cover_letter_path: str, result: ApplyResult) -> None:
        """Fill out the application form on `page` and submit.

        Implementations should:
          1. Wait for the form to be present
          2. Check has_captcha(page) early and bail if so
          3. Fill all required fields from self.profile
          4. Upload resume + cover letter via the standard file inputs
          5. Click submit
          6. Verify success (look for thank-you message / URL change)
          7. Update `result` with outcome via result.add_step() and result.success/status

        Raises:
            Exception on hard failures — the caller will catch and mark failed.
        """
        raise NotImplementedError

    # ----- shared helpers (used by subclasses) --------------------------------

    def fill_safe(self, page, selector: str, value: str, *, log_to: ApplyResult, label: str = "") -> bool:
        """Fill a field if it exists; log either way. Returns True if filled."""
        if not value:
            log_to.add_step("skip_empty_value", f"{label or selector}", success=True)
            return False
        try:
            loc = page.locator(selector).first
            if loc.count() == 0:
                log_to.add_step("skip_field_absent", f"{label or selector}", success=True)
                return False
            humanize_delay()
            loc.fill(value)
            log_to.add_step("fill", f"{label or selector} = {value[:60]}", success=True)
            return True
        except Exception as e:
            log_to.add_step("fill_failed", f"{label or selector}: {e}", success=False)
            return False

    def click_safe(self, page, selector: str, *, log_to: ApplyResult, label: str = "", timeout_ms: int = 5000) -> bool:
        """Click a button if it exists. Returns True if clicked."""
        try:
            loc = page.locator(selector).first
            if loc.count() == 0:
                log_to.add_step("skip_button_absent", f"{label or selector}", success=True)
                return False
            humanize_delay()
            loc.click(timeout=timeout_ms)
            log_to.add_step("click", f"{label or selector}", success=True)
            return True
        except Exception as e:
            log_to.add_step("click_failed", f"{label or selector}: {e}", success=False)
            return False

    def upload_file(self, page, selector: str, file_path: str, *, log_to: ApplyResult, label: str = "") -> bool:
        """Upload a file via a file input element."""
        if not file_path or not Path(file_path).exists():
            log_to.add_step("upload_failed", f"file not found: {file_path}", success=False)
            return False
        try:
            loc = page.locator(selector).first
            if loc.count() == 0:
                log_to.add_step("skip_upload_absent", f"{label or selector}", success=True)
                return False
            loc.set_input_files(file_path)
            log_to.add_step("upload", f"{label or selector} <- {Path(file_path).name}", success=True)
            humanize_delay(500, 1200)  # uploads need a beat
            return True
        except Exception as e:
            log_to.add_step("upload_failed", f"{label or selector}: {e}", success=False)
            return False
