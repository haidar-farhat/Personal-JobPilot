"""Lever auto-applier.

Lever's apply pages have URL pattern: jobs.lever.co/{company}/{job_id}/apply
Field structure (mostly stable across all Lever-hosted boards):
  * input[name='name']      — full name
  * input[name='email']     — email
  * input[name='phone']     — phone
  * input[name='org']       — current organization (optional)
  * input[name='urls[LinkedIn]'] — LinkedIn URL
  * input[name='urls[Portfolio]'] — portfolio URL
  * input[name='resume']    — file input
  * textarea[name='comments'] — additional comments / cover letter pasted here
  * Lever does NOT have a separate cover letter file input — it uses a textarea
"""

from __future__ import annotations

import logging
from pathlib import Path

from agents.auto_applier.base import (
    BaseAutoApplier,
    ApplyResult,
    has_captcha,
    has_login_wall,
    humanize_delay,
    fill_required_essays,
)

logger = logging.getLogger(__name__)


class LeverAutoApplier(BaseAutoApplier):
    ats_name = "lever"

    def apply(self, page, job, resume_path: str, cover_letter_path: str, result: ApplyResult) -> None:
        identity = self.profile["identity"]
        links = self.profile["links"]
        work_auth = self.profile["work_authorization"]

        page.wait_for_load_state("networkidle", timeout=15000)
        result.add_step("navigated", page.url)

        # If we're on the JD rather than /apply, click the Apply button
        if "/apply" not in page.url:
            apply_selectors = (
                "a.postings-btn:has-text('Apply')",
                "a:has-text('Apply for this job')",
                "a[href*='/apply']",
            )
            for sel in apply_selectors:
                if page.locator(sel).count() > 0:
                    self.click_safe(page, sel, log_to=result, label="apply_button", timeout_ms=4000)
                    page.wait_for_load_state("networkidle", timeout=10000)
                    break

        if has_captcha(page):
            result.success = False
            result.status = "failed_captcha"
            result.message = "Lever form has CAPTCHA"
            return

        # Core identity fields
        self.fill_safe(page, "input[name='name']", identity["full_name"], log_to=result, label="name")
        self.fill_safe(page, "input[name='email']", identity["email"], log_to=result, label="email")
        self.fill_safe(page, "input[name='phone']", identity["phone"], log_to=result, label="phone")
        self.fill_safe(page, "input[name='org']", "", log_to=result, label="org")  # leave blank

        if links.get("linkedin"):
            self.fill_safe(page, "input[name='urls[LinkedIn]']", links["linkedin"],
                           log_to=result, label="linkedin")

        # Upload resume
        if not self.upload_file(page, "input[type='file'][name='resume']",
                                resume_path, log_to=result, label="resume"):
            # Lever sometimes uses a different name attribute
            if not self.upload_file(page, "input[type='file']",
                                    resume_path, log_to=result, label="resume_fallback"):
                result.success = False
                result.status = "failed_resume_upload"
                result.message = "Could not upload resume on Lever form"
                return

        # Lever uses a textarea for cover letter content (no file upload)
        # Read the .docx text and paste it
        try:
            cover_text = self._extract_docx_text(cover_letter_path)
            if cover_text:
                self.fill_safe(page, "textarea[name='comments']", cover_text,
                               log_to=result, label="cover_letter_textarea")
        except Exception as e:
            result.add_step("cover_letter_textarea_failed", str(e), success=False)

        # Required essay questions — drafted via Gemma 4
        max_essays = self.profile.get("guardrails", {}).get("max_essays_per_app", 3)
        essay_result = fill_required_essays(self, page, job, log_to=result, max_essays=max_essays)
        if essay_result.get("skipped_over_cap", 0) > 0 and self.profile.get("guardrails", {}).get("bail_on_required_essay", True):
            result.success = False
            result.status = "failed_too_many_essays"
            result.message = f"Form has {essay_result['essay_count']} essays, cap is {max_essays} — manual review"
            return

        # Custom Lever question handling — work authorization is a common required field
        # Lever uses radio buttons inside the .application-question block
        try:
            for q in page.locator("ul.application-question").all():
                q_text = (q.inner_text() or "").lower()
                if "authorized to work" in q_text:
                    yes = q.locator("input[type='radio'][value*='Yes' i], "
                                    "label:has-text('Yes') input[type='radio']").first
                    if yes.count() > 0 and work_auth["authorized_to_work_us"]:
                        yes.check()
                        result.add_step("work_auth_yes", "checked", success=True)
                if "require" in q_text and "sponsor" in q_text:
                    no = q.locator("input[type='radio'][value*='No' i], "
                                   "label:has-text('No') input[type='radio']").first
                    if no.count() > 0 and not work_auth["requires_sponsorship"]:
                        no.check()
                        result.add_step("sponsorship_no", "checked", success=True)
        except Exception as e:
            result.add_step("custom_questions_failed", str(e), success=False)

        if has_captcha(page):
            result.success = False
            result.status = "failed_captcha"
            result.message = "Lever CAPTCHA at submit time"
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
            "button:has-text('Submit application'), button[type='submit']",
            log_to=result, label="submit", timeout_ms=8000,
        )

        if not submitted:
            result.success = False
            result.status = "failed_submit_button"
            result.message = "Submit button not found on Lever form"
            return

        # Verify success
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
            body_text = page.locator("body").inner_text(timeout=5000).lower()
            if any(s in body_text for s in ("thank you", "we've received", "your application has been submitted")):
                result.success = True
                result.status = "submitted"
                result.message = "Lever confirmation detected"
                result.add_step("confirmation_seen", page.url, success=True)
            else:
                result.success = False
                result.status = "failed_no_confirmation"
                result.message = f"No confirmation. URL: {page.url}"
        except Exception as e:
            result.success = False
            result.status = "failed_confirmation_check"
            result.message = f"Couldn't verify: {e}"

    @staticmethod
    def _extract_docx_text(path: str) -> str:
        """Read the cover letter .docx and return its plain text."""
        if not path or not Path(path).exists():
            return ""
        try:
            from docx import Document
            doc = Document(path)
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            return "\n\n".join(paragraphs)
        except Exception as e:
            logger.warning(f"[lever] Could not read cover letter docx: {e}")
            return ""
