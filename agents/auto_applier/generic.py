"""Generic / custom-ATS auto-applier.

The fallback applier for any ATS we don't have a dedicated handler for.
Uses heuristic field detection: matches on `name`, `id`, `aria-label`, `placeholder`,
and `<label>` text for common patterns.

Strict bail conditions to protect application quality:
  * Login wall detected → fail_login_required
  * CAPTCHA present → fail_captcha
  * No file input found within 5 seconds → fail_no_resume_upload
  * More than `max_unmapped_required` required fields we can't map → fail_too_complex
  * No identifiable submit button → fail_no_submit_button

This applier is intentionally conservative — it's the LAST tier in our quality
ladder, and it's better to queue a job for manual review than to submit
something broken.
"""

from __future__ import annotations

import logging
import re

from agents.auto_applier.base import (
    BaseAutoApplier,
    ApplyResult,
    has_captcha,
    has_login_wall,
    humanize_delay,
    fill_required_essays,
)

logger = logging.getLogger(__name__)


# Generic field-name → profile-value mappings.
# Each entry: (regex against name/id/aria-label/placeholder, value, action label)
# We try these in order and stop at the first match per element.

def _build_field_mappings(profile: dict) -> list[tuple[str, str, str]]:
    identity = profile["identity"]
    address = profile["address"]
    links = profile["links"]
    return [
        (r"first[\W_]*name|fname|givenname|legal[_\W]*first", identity["first_name"], "first_name"),
        (r"last[\W_]*name|lname|surname|familyname|legal[_\W]*last", identity["last_name"], "last_name"),
        (r"full[\W_]*name|^name$|legal[_\W]*name", identity["full_name"], "full_name"),
        (r"e[\W_-]?mail", identity["email"], "email"),
        (r"phone|mobile|tel(?:ephone)?", identity["phone"], "phone"),
        (r"city", address.get("city", ""), "city"),
        (r"state|region|province", address.get("state", ""), "state"),
        (r"zip|postal|postcode", address.get("postal_code", ""), "postal_code"),
        (r"country", address.get("country", ""), "country"),
        (r"address.*1|street|address[_\W]*line", address.get("street", ""), "address"),
        (r"linkedin", links.get("linkedin", ""), "linkedin"),
        (r"github", links.get("github", ""), "github"),
        (r"portfolio|website", links.get("portfolio", "") or links.get("website", ""), "portfolio"),
    ]


class GenericAutoApplier(BaseAutoApplier):
    """Heuristic form filler for unknown ATS systems.

    Higher score threshold + bail-eager defaults compared to platform-specific
    appliers — this is genuinely best-effort and will fail more often than
    Greenhouse/Ashby/Lever/Workday.
    """

    ats_name = "generic"

    MAX_UNMAPPED_REQUIRED_FIELDS = 5  # bail if > this many required fields go unmapped

    def apply(self, page, job, resume_path: str, cover_letter_path: str, result: ApplyResult) -> None:
        page.wait_for_load_state("networkidle", timeout=20000)
        result.add_step("navigated", page.url)

        if has_captcha(page):
            result.success = False
            result.status = "failed_captcha"
            result.message = "Generic form has CAPTCHA"
            return

        if has_login_wall(page):
            result.success = False
            result.status = "failed_login_required"
            result.message = "Generic form requires login — bailed (security policy)"
            result.add_step("login_wall_detected", page.url, success=False)
            return

        # Click an Apply button if we're on a JD-only page
        for sel in ("button:has-text('Apply Now')", "button:has-text('Apply')",
                    "a:has-text('Apply Now')", "a:has-text('Apply')"):
            if page.locator(sel).count() > 0:
                if self.click_safe(page, sel, log_to=result, label="apply_button", timeout_ms=5000):
                    page.wait_for_load_state("networkidle", timeout=10000)
                    if has_login_wall(page):
                        result.success = False
                        result.status = "failed_login_required"
                        result.message = "Apply button led to login wall"
                        return
                    break

        # 1. Heuristic identity-field fill
        mappings = _build_field_mappings(self.profile)
        filled_count = 0
        for input_loc in self._all_text_inputs(page):
            try:
                if not input_loc.is_visible():
                    continue
                if (input_loc.input_value() or "").strip():
                    continue  # already populated
                meta = self._field_metadata(input_loc, page)
                if not meta:
                    continue
                for pattern, value, label in mappings:
                    if value and re.search(pattern, meta, re.IGNORECASE):
                        humanize_delay(150, 400)
                        try:
                            input_loc.fill(value)
                            result.add_step("fill", f"{label} via '{meta[:40]}'", success=True)
                            filled_count += 1
                            break
                        except Exception as e:
                            result.add_step("fill_failed", f"{label}: {e}", success=False)
                            break
            except Exception:
                continue

        result.add_step("identity_filled", f"count={filled_count}", success=True)

        # 2. Find and upload resume
        # Try labeled selectors first, then generic file input
        resume_uploaded = False
        for sel in (
            "input[type='file'][name*='resume' i]",
            "input[type='file'][id*='resume' i]",
            "input[type='file'][aria-label*='resume' i]",
            "input[type='file'][accept*='pdf']",
            "input[type='file']",
        ):
            if page.locator(sel).count() > 0:
                if self.upload_file(page, sel, resume_path, log_to=result, label="resume"):
                    resume_uploaded = True
                    humanize_delay(800, 1500)
                    break

        if not resume_uploaded:
            result.success = False
            result.status = "failed_no_resume_upload"
            result.message = "No file upload control found on generic form"
            return

        # 3. Cover letter — try a second file input if available
        cover_uploaded = False
        for sel in (
            "input[type='file'][name*='cover' i]",
            "input[type='file'][id*='cover' i]",
            "input[type='file'][aria-label*='cover' i]",
        ):
            if page.locator(sel).count() > 0:
                cover_uploaded = self.upload_file(page, sel, cover_letter_path,
                                                  log_to=result, label="cover_letter")
                if cover_uploaded:
                    break
        if not cover_uploaded:
            result.add_step("cover_letter_skipped", "no dedicated cover-letter input", success=True)

        # 4. Required essays via Gemma
        max_essays = self.profile.get("guardrails", {}).get("max_essays_per_app", 3)
        essay_result = fill_required_essays(self, page, job, log_to=result, max_essays=max_essays)
        if essay_result.get("skipped_over_cap", 0) > 0 and self.profile.get("guardrails", {}).get("bail_on_required_essay", True):
            result.success = False
            result.status = "failed_too_many_essays"
            result.message = f"Generic form has {essay_result['essay_count']} essays, cap is {max_essays}"
            return

        # 5. Count remaining required-but-empty fields — if too many, bail
        unmapped_required = self._count_unfilled_required(page)
        if unmapped_required > self.MAX_UNMAPPED_REQUIRED_FIELDS:
            result.success = False
            result.status = "failed_too_complex"
            result.message = f"{unmapped_required} required fields unmapped — manual review"
            result.add_step("too_complex", f"{unmapped_required} required fields empty", success=False)
            return

        # 6. CAPTCHA re-check before submit
        if has_captcha(page):
            result.success = False
            result.status = "failed_captcha"
            result.message = "CAPTCHA appeared at submit time"
            return

        # 7. Submit
        if self.dry_run:
            result.success = True
            result.status = "dry_run"
            result.message = f"Would have submitted (filled {filled_count} fields)"
            result.add_step("dry_run_skip_submit", "ok", success=True)
            return

        humanize_delay(800, 1500)
        submit_selectors = (
            "button[type='submit']:not([disabled])",
            "input[type='submit']:not([disabled])",
            "button:has-text('Submit Application')",
            "button:has-text('Submit')",
        )
        submitted = False
        for sel in submit_selectors:
            if page.locator(sel).count() > 0 and page.locator(sel).first.is_visible():
                submitted = self.click_safe(page, sel, log_to=result, label="submit", timeout_ms=8000)
                if submitted:
                    break

        if not submitted:
            result.success = False
            result.status = "failed_no_submit_button"
            result.message = "No identifiable submit button on generic form"
            return

        # 8. Verify success
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
            body_text = page.locator("body").inner_text(timeout=5000).lower()
            success_signals = ("thank you", "application received", "submitted successfully",
                               "we have received", "your application")
            if any(sig in body_text for sig in success_signals):
                result.success = True
                result.status = "submitted"
                result.message = "Generic confirmation detected"
                result.add_step("confirmation_seen", page.url, success=True)
            else:
                result.success = False
                result.status = "failed_no_confirmation"
                result.message = f"No confirmation. URL: {page.url}"
        except Exception as e:
            result.success = False
            result.status = "failed_confirmation_check"
            result.message = f"Couldn't verify: {e}"

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _all_text_inputs(page):
        """Generator-equivalent: iterate every text-ish input on the page."""
        try:
            return page.locator(
                "input[type='text'], input[type='email'], input[type='tel'], "
                "input[type='url'], input:not([type])"
            ).all()
        except Exception:
            return []

    @staticmethod
    def _field_metadata(loc, page) -> str:
        """Concatenate every signal we have for guessing what a field is for."""
        try:
            parts = []
            for attr in ("name", "id", "aria-label", "placeholder", "data-testid"):
                v = loc.get_attribute(attr)
                if v:
                    parts.append(v)
            elem_id = loc.get_attribute("id")
            if elem_id:
                lbl = page.locator(f"label[for='{elem_id}']")
                if lbl.count() > 0:
                    try:
                        txt = lbl.first.inner_text(timeout=500)
                        if txt:
                            parts.append(txt)
                    except Exception:
                        pass
            return " | ".join(parts)
        except Exception:
            return ""

    @staticmethod
    def _count_unfilled_required(page) -> int:
        """Count required fields that are still empty."""
        try:
            count = 0
            for sel in ("input[required]", "select[required]", "textarea[required]",
                        "input[aria-required='true']", "select[aria-required='true']", "textarea[aria-required='true']"):
                fields = page.locator(sel)
                for i in range(min(fields.count(), 50)):  # cap iteration
                    try:
                        f = fields.nth(i)
                        if not f.is_visible():
                            continue
                        # Skip hidden/honeypot
                        if (f.get_attribute("type") or "").lower() == "hidden":
                            continue
                        val = (f.input_value() or "").strip() if f.evaluate("el => el.tagName.toLowerCase() !== 'select'") else ""
                        if not val:
                            count += 1
                    except Exception:
                        continue
            return count
        except Exception:
            return 0
