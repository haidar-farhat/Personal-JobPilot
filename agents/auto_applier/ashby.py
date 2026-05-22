"""Ashby auto-applier.

Ashby's posting pages have URL pattern: jobs.ashbyhq.com/{company}/{job_id}
Apply form is on the same page. Field structure:
  * inputs identified by data-testid or aria-label rather than name
  * resume upload uses dropzone div + hidden input
  * Most fields are React-controlled — we use page.fill() which dispatches input events
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


class AshbyAutoApplier(BaseAutoApplier):
    ats_name = "ashby"

    def apply(self, page, job, resume_path: str, cover_letter_path: str, result: ApplyResult) -> None:
        identity = self.profile["identity"]
        address = self.profile["address"]
        links = self.profile["links"]

        page.wait_for_load_state("networkidle", timeout=20000)
        result.add_step("navigated", page.url)

        # Ashby has an explicit Apply button on the JD
        apply_selectors = (
            "button:has-text('Apply for')",
            "button:has-text('Apply Now')",
            "a:has-text('Apply for this Job')",
        )
        for sel in apply_selectors:
            if page.locator(sel).count() > 0:
                self.click_safe(page, sel, log_to=result, label="apply_button", timeout_ms=4000)
                page.wait_for_load_state("networkidle", timeout=10000)
                break

        if has_captcha(page):
            result.success = False
            result.status = "failed_captcha"
            result.message = "Ashby form has CAPTCHA"
            result.add_step("captcha_detected", "halting", success=False)
            return

        # Ashby uses aria-labels for accessibility — these are stable selectors
        # Field names: "Full Name", "Email", "Phone", "Location", "LinkedIn URL"
        self.fill_safe(page, "input[aria-label='Full Name'], input[name='_systemfield_name']",
                       identity["full_name"], log_to=result, label="full_name")
        self.fill_safe(page, "input[aria-label='Email'], input[name='_systemfield_email']",
                       identity["email"], log_to=result, label="email")
        self.fill_safe(page, "input[aria-label='Phone'], input[name='_systemfield_phone']",
                       identity["phone"], log_to=result, label="phone")
        self.fill_safe(page, "input[aria-label='Current Location'], input[aria-label='Location']",
                       f"{address.get('city', '')}, {address.get('state', '')}",
                       log_to=result, label="location")
        if links.get("linkedin"):
            self.fill_safe(page, "input[aria-label*='LinkedIn' i]", links["linkedin"], log_to=result, label="linkedin")

        # Upload resume — Ashby uses a hidden file input behind a dropzone
        resume_uploaded = self.upload_file(
            page,
            "input[type='file'][aria-label*='Resume' i], input[type='file'][name*='resume' i]",
            resume_path,
            log_to=result,
            label="resume",
        )
        if not resume_uploaded:
            # Try a generic file input as fallback
            resume_uploaded = self.upload_file(
                page, "input[type='file']", resume_path,
                log_to=result, label="resume_fallback",
            )

        if not resume_uploaded:
            result.success = False
            result.status = "failed_resume_upload"
            result.message = "Could not find Ashby resume upload"
            return

        # Cover letter — Ashby's is usually a separate "Additional Information" section
        # Try labeled file input first, then any unused file input
        cover_inputs = page.locator("input[type='file'][aria-label*='Cover' i], input[type='file'][name*='cover' i]")
        if cover_inputs.count() > 0:
            self.upload_file(page, "input[type='file'][aria-label*='Cover' i]",
                             cover_letter_path, log_to=result, label="cover_letter")

        # Required essay questions — drafted via Gemma 4
        max_essays = self.profile.get("guardrails", {}).get("max_essays_per_app", 3)
        essay_result = fill_required_essays(self, page, job, log_to=result, max_essays=max_essays)
        if essay_result.get("skipped_over_cap", 0) > 0 and self.profile.get("guardrails", {}).get("bail_on_required_essay", True):
            result.success = False
            result.status = "failed_too_many_essays"
            result.message = f"Form has {essay_result['essay_count']} essays, cap is {max_essays} — manual review"
            return

        # EEOC selectors — Ashby uses radio groups labeled 'Decline to self-identify'
        try:
            decline_radios = page.locator("input[type='radio'][value*='decline' i], "
                                          "label:has-text('Decline to self-identify') input[type='radio']")
            for i in range(decline_radios.count()):
                try:
                    decline_radios.nth(i).check()
                    result.add_step("eeoc_decline", f"radio_{i}", success=True)
                except Exception:
                    pass
        except Exception as e:
            result.add_step("eeoc_radio_failed", str(e), success=False)

        # Re-check CAPTCHA before submit
        if has_captcha(page):
            result.success = False
            result.status = "failed_captcha"
            result.message = "Ashby CAPTCHA at submit time"
            return

        if self.dry_run:
            result.success = True
            result.status = "dry_run"
            result.message = "Would have submitted (dry_run)"
            result.add_step("dry_run_skip_submit", "ok", success=True)
            return

        humanize_delay(800, 1500)
        submitted = self.click_safe(
            page,
            "button:has-text('Submit Application'), button:has-text('Submit'), button[type='submit']",
            log_to=result, label="submit", timeout_ms=8000,
        )

        if not submitted:
            result.success = False
            result.status = "failed_submit_button"
            result.message = "Submit button not found on Ashby form"
            return

        # Verify success
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
            body_text = page.locator("body").inner_text(timeout=5000).lower()
            if any(s in body_text for s in ("thank you", "submitted successfully", "application received")):
                result.success = True
                result.status = "submitted"
                result.message = "Ashby confirmation detected"
                result.add_step("confirmation_seen", page.url, success=True)
            else:
                result.success = False
                result.status = "failed_no_confirmation"
                result.message = f"No confirmation. URL: {page.url}"
        except Exception as e:
            result.success = False
            result.status = "failed_confirmation_check"
            result.message = f"Couldn't verify: {e}"
