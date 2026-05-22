"""Greenhouse auto-applier.

Greenhouse forms have a very consistent shape across companies:
  * URL pattern: boards.greenhouse.io/{company}/jobs/{id}  (job description page)
  * Apply button leads to: boards.greenhouse.io/{company}/jobs/{id}#app
  * Form fields use semi-stable name attributes:
      - first_name, last_name, email, phone
      - resume (file input), cover_letter (file input)
      - location.city, location.region (sometimes)
      - urls[LinkedIn] (sometimes)
  * EEOC questions appear at the bottom — answers labeled "Decline to answer" exist
  * Submit button: button[type='submit']

Greenhouse rarely uses CAPTCHAs but does use honeypot fields — we detect those
and bail before clicking submit.
"""

from __future__ import annotations

import logging

from agents.auto_applier.base import (
    BaseAutoApplier,
    ApplyResult,
    has_captcha,
    has_login_wall,
    humanize_delay,
    fill_required_essays,
)

logger = logging.getLogger(__name__)


class GreenhouseAutoApplier(BaseAutoApplier):
    ats_name = "greenhouse"

    def apply(self, page, job, resume_path: str, cover_letter_path: str, result: ApplyResult) -> None:
        identity = self.profile["identity"]
        address = self.profile["address"]
        links = self.profile["links"]
        eeoc = self.profile["eeoc"]
        work_auth = self.profile["work_authorization"]

        # 1. Make sure the application form is loaded — Greenhouse anchors it on #app
        page.wait_for_load_state("networkidle", timeout=15000)
        result.add_step("navigated", page.url)

        # If we're on the JD page rather than the apply form, click the Apply button
        apply_btn_selectors = (
            "a#apply_button",
            "a.template-btn-submit",
            "a[href*='#app']",
            "button:has-text('Apply')",
        )
        for sel in apply_btn_selectors:
            if page.locator(sel).count() > 0:
                self.click_safe(page, sel, log_to=result, label="apply_button", timeout_ms=4000)
                page.wait_for_load_state("networkidle", timeout=10000)
                break

        # 2. Bail if we hit a CAPTCHA
        if has_captcha(page):
            result.success = False
            result.status = "failed_captcha"
            result.message = "Greenhouse form has CAPTCHA — manual review needed"
            result.add_step("captcha_detected", "halting", success=False)
            return

        # 3. Fill core identity fields
        self.fill_safe(page, "input[name='first_name']", identity["first_name"], log_to=result, label="first_name")
        self.fill_safe(page, "input[name='last_name']", identity["last_name"], log_to=result, label="last_name")
        self.fill_safe(page, "input[name='email']", identity["email"], log_to=result, label="email")
        self.fill_safe(page, "input[name='phone']", identity["phone"], log_to=result, label="phone")

        # Location (some boards split these, others combine)
        self.fill_safe(page, "input[name='location.city']", address.get("city", ""), log_to=result, label="city")
        self.fill_safe(page, "input[name='location']", f"{address.get('city', '')}, {address.get('state', '')}", log_to=result, label="location_combined")

        # LinkedIn (Greenhouse uses urls[LinkedIn] notation)
        if links.get("linkedin"):
            for sel in ("input[name='urls[LinkedIn]']", "input[name*='linkedin' i]", "input[id*='linkedin' i]"):
                if page.locator(sel).count() > 0:
                    self.fill_safe(page, sel, links["linkedin"], log_to=result, label="linkedin")
                    break

        # 4. Upload resume + cover letter
        # Greenhouse uses input[type='file'] with name 'resume' and 'cover_letter'
        # Some companies use ID-based selectors instead
        resume_selectors = (
            "input[type='file'][name='resume']",
            "input[type='file'][id*='resume']",
            "input[type='file'][id*='Resume']",
        )
        for sel in resume_selectors:
            if page.locator(sel).count() > 0:
                if not self.upload_file(page, sel, resume_path, log_to=result, label="resume"):
                    result.success = False
                    result.status = "failed_resume_upload"
                    result.message = f"Could not upload resume to {sel}"
                    return
                break
        else:
            result.success = False
            result.status = "failed_resume_field_not_found"
            result.message = "No resume file input found on the form"
            return

        # Cover letter is usually optional but we always upload if present
        cover_selectors = (
            "input[type='file'][name='cover_letter']",
            "input[type='file'][id*='cover']",
            "input[type='file'][id*='Cover']",
        )
        for sel in cover_selectors:
            if page.locator(sel).count() > 0:
                self.upload_file(page, sel, cover_letter_path, log_to=result, label="cover_letter")
                break

        # 5. Work authorization radio buttons (when present)
        # Greenhouse forms often phrase these with select dropdowns rather than radios.
        # We look for any select with options like "Yes, I am authorized" / "No, I require sponsorship"
        try:
            for sel in page.locator("select").all():
                label_attr = sel.get_attribute("name") or sel.get_attribute("id") or ""
                lower = label_attr.lower()
                if "authoriz" in lower or "work" in lower and "us" in lower:
                    if work_auth["authorized_to_work_us"]:
                        sel.select_option(label="Yes")
                        result.add_step("select_authorized", "Yes", success=True)
                if "sponsor" in lower:
                    sel.select_option(label="No" if not work_auth["requires_sponsorship"] else "Yes")
                    result.add_step("select_sponsorship", "set", success=True)
        except Exception as e:
            result.add_step("work_auth_select_failed", str(e), success=False)

        # 6. EEOC defaults — set every demographic dropdown to "Decline to answer" if available
        try:
            for sel in page.locator("select[id*='eeo' i], select[name*='eeo' i], "
                                    "select[id*='gender' i], select[id*='race' i], "
                                    "select[id*='veteran' i], select[id*='disability' i]").all():
                try:
                    sel.select_option(label="Decline to self-identify")
                    result.add_step("eeoc_decline", sel.get_attribute("name") or "?", success=True)
                except Exception:
                    try:
                        sel.select_option(label="Decline to answer")
                    except Exception:
                        try:
                            sel.select_option(label="I don't wish to answer")
                        except Exception:
                            pass
        except Exception as e:
            result.add_step("eeoc_select_failed", str(e), success=False)

        # 6.5. Required essay questions — drafted via Gemma 4
        max_essays = self.profile.get("guardrails", {}).get("max_essays_per_app", 3)
        essay_result = fill_required_essays(self, page, job, log_to=result, max_essays=max_essays)
        if essay_result.get("skipped_over_cap", 0) > 0 and self.profile.get("guardrails", {}).get("bail_on_required_essay", True):
            result.success = False
            result.status = "failed_too_many_essays"
            result.message = f"Form has {essay_result['essay_count']} essays, cap is {max_essays} — manual review"
            return

        # 7. Honeypot detection — Greenhouse hides a field that bots typically fill.
        # If we ever filled a hidden field, bail.
        try:
            for hp in page.locator("input[name*='honeypot' i], input[name*='bot' i]").all():
                if hp.input_value():
                    result.success = False
                    result.status = "failed_honeypot"
                    result.message = "Honeypot field was filled — aborting"
                    result.add_step("honeypot_triggered", "halting", success=False)
                    return
        except Exception:
            pass

        # 8. Re-check CAPTCHA before submit
        if has_captcha(page):
            result.success = False
            result.status = "failed_captcha"
            result.message = "CAPTCHA appeared just before submit"
            result.add_step("captcha_detected_pre_submit", "halting", success=False)
            return

        # 9. Submit (or dry-run)
        if self.dry_run:
            result.success = True
            result.status = "dry_run"
            result.message = "Would have clicked submit — dry_run mode is on"
            result.add_step("dry_run_skip_submit", "all_fields_filled", success=True)
            return

        humanize_delay(800, 1500)
        submitted = self.click_safe(
            page,
            "button[type='submit']:has-text('Submit'), input[type='submit'], button#submit_app",
            log_to=result,
            label="submit",
            timeout_ms=8000,
        )

        if not submitted:
            result.success = False
            result.status = "failed_submit_button"
            result.message = "Could not click the submit button"
            return

        # 10. Verify success — Greenhouse shows a confirmation page or a #thanks anchor
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
            url_after = page.url
            body_text = page.locator("body").inner_text(timeout=5000).lower()
            success_signals = ("thank you", "application received", "we've received", "we have received", "thanks for applying")
            if any(sig in body_text for sig in success_signals) or "#thanks" in url_after or "submitted" in url_after.lower():
                result.success = True
                result.status = "submitted"
                result.message = "Greenhouse confirmation detected"
                result.add_step("confirmation_seen", url_after, success=True)
            else:
                result.success = False
                result.status = "failed_no_confirmation"
                result.message = f"No confirmation found after submit. URL: {url_after}"
                result.add_step("confirmation_missing", url_after, success=False)
        except Exception as e:
            result.success = False
            result.status = "failed_confirmation_check"
            result.message = f"Couldn't verify confirmation: {e}"
            result.add_step("confirmation_check_failed", str(e), success=False)
