"""Workday auto-applier.

Workday is the trickiest mainstream ATS. URL pattern is:
    {company}.{instance}.myworkdayjobs.com/{site}/job/{location}/{job-id}

Reality check: ~60% of Workday-hosted boards require account creation before
submitting an application. Account creation is forbidden by our security policy
(see CLAUDE Acceptable-Use rules — never create accounts on the user's behalf).
For those boards, this applier bails immediately with `failed_workday_login_required`
and the application stays in MATERIALS_READY for manual handling.

The other ~40% support a guest "Apply Manually" or "Autofill from Resume" flow.
For those, we walk the multi-step wizard:
    1. Click Apply / Apply Manually
    2. Upload resume (Workday auto-fills name + contact from the .docx)
    3. Confirm contact info
    4. Answer voluntary disclosures (Decline to answer for EEOC)
    5. Click Submit

We use Workday's stable data-automation-id selectors where possible:
    [data-automation-id='applyManually']
    [data-automation-id='resume-upload']
    [data-automation-id='legalNameSection_firstName']
    [data-automation-id='emailAddress']
    [data-automation-id='phoneNumber']
    [data-automation-id='bottom-navigation-next-button']
    [data-automation-id='bottom-navigation-submit-button']

Heuristic: if we hit a step we don't recognize, we bail. Better to queue manually
than submit a half-broken application.
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


class WorkdayAutoApplier(BaseAutoApplier):
    ats_name = "workday"

    # Maximum number of "Next" clicks we'll do walking the wizard.
    # Most Workday flows are 3-5 steps; if we hit > MAX, the form has unfamiliar
    # steps and we should bail to manual.
    MAX_WIZARD_STEPS = 8

    def apply(self, page, job, resume_path: str, cover_letter_path: str, result: ApplyResult) -> None:
        identity = self.profile["identity"]
        address = self.profile["address"]
        links = self.profile["links"]
        work_auth = self.profile["work_authorization"]

        page.wait_for_load_state("networkidle", timeout=20000)
        result.add_step("navigated", page.url)

        # 1. Sanity check — confirm we're actually on a Workday domain
        if "myworkdayjobs.com" not in (page.url or "").lower():
            result.success = False
            result.status = "failed_not_workday"
            result.message = f"URL is not a Workday domain: {page.url}"
            return

        # 2. Bail on CAPTCHA
        if has_captcha(page):
            result.success = False
            result.status = "failed_captcha"
            result.message = "Workday form has CAPTCHA"
            result.add_step("captcha_detected", "halting", success=False)
            return

        # 3. Click the top-level Apply button if we're on the JD page
        apply_selectors = (
            "[data-automation-id='adventureButton']",
            "button:has-text('Apply')",
            "a:has-text('Apply')",
        )
        for sel in apply_selectors:
            if page.locator(sel).count() > 0:
                self.click_safe(page, sel, log_to=result, label="apply_button", timeout_ms=5000)
                page.wait_for_load_state("networkidle", timeout=10000)
                break

        # 4. The "Apply" click usually shows a chooser: Apply Manually / Apply with LinkedIn / ...
        # We strongly prefer "Apply Manually" since LinkedIn OAuth requires user interaction
        if page.locator("[data-automation-id='applyManually']").count() > 0:
            self.click_safe(page, "[data-automation-id='applyManually']",
                            log_to=result, label="apply_manually", timeout_ms=5000)
            page.wait_for_load_state("networkidle", timeout=10000)

        # 5. CRITICAL: account-creation / login wall check.
        # Workday almost always shows this after "Apply Manually".
        # Per security policy we DO NOT auto-create accounts.
        if has_login_wall(page):
            # Check if there's a "Continue as Guest" or similar option first
            guest_selectors = (
                "[data-automation-id='applyAsGuest']",
                "button:has-text('Continue as Guest')",
                "a:has-text('Apply as Guest')",
            )
            guest_clicked = False
            for sel in guest_selectors:
                if page.locator(sel).count() > 0:
                    self.click_safe(page, sel, log_to=result, label="apply_as_guest", timeout_ms=5000)
                    page.wait_for_load_state("networkidle", timeout=10000)
                    guest_clicked = True
                    break

            # Re-check after guest button click
            if not guest_clicked or has_login_wall(page):
                result.success = False
                result.status = "failed_workday_login_required"
                result.message = "Workday requires account creation — bailed (security policy: no auto account creation)"
                result.add_step("login_wall_detected", page.url, success=False)
                return

        # 6. Upload resume — Workday's auto-extract reads the .docx and pre-populates fields
        resume_uploaded = False
        for sel in (
            "[data-automation-id='resume-upload'] input[type='file']",
            "[data-automation-id='file-upload-input-ref']",
            "input[type='file'][accept*='pdf'], input[type='file'][accept*='doc']",
        ):
            if page.locator(sel).count() > 0:
                if self.upload_file(page, sel, resume_path, log_to=result, label="resume"):
                    resume_uploaded = True
                    page.wait_for_load_state("networkidle", timeout=20000)
                    # Workday spinner: wait for "Uploading..." to disappear
                    humanize_delay(2000, 4000)
                    break

        if not resume_uploaded:
            result.success = False
            result.status = "failed_resume_upload"
            result.message = "Could not find Workday resume upload control"
            return

        # 7. Step through the wizard — Workday is a multi-step form
        for step_num in range(1, self.MAX_WIZARD_STEPS + 1):
            result.add_step(f"wizard_step_{step_num}", page.url, success=True)

            # CAPTCHA can appear at any step
            if has_captcha(page):
                result.success = False
                result.status = "failed_captcha"
                result.message = f"CAPTCHA appeared at wizard step {step_num}"
                return

            # Fill any visible identity fields on this step
            self._fill_identity_fields(page, identity, address, links, result)

            # Fill work-authorization questions if present
            self._answer_work_auth(page, work_auth, result)

            # Default EEOC to "Decline to answer"
            self._decline_eeoc(page, result)

            # Detect and fill any required essay textareas via Gemma
            max_essays = self.profile.get("guardrails", {}).get("max_essays_per_app", 3)
            essay_result = fill_required_essays(self, page, job, log_to=result, max_essays=max_essays)
            if essay_result.get("skipped_over_cap", 0) > 0 and self.profile.get("guardrails", {}).get("bail_on_required_essay", True):
                result.success = False
                result.status = "failed_too_many_essays"
                result.message = f"Workday step {step_num} has {essay_result['essay_count']} essays, cap is {max_essays}"
                return

            # Try to advance: click "Next", "Continue", or "Save and Continue"
            advanced = False
            advance_selectors = (
                "[data-automation-id='bottom-navigation-next-button']",
                "[data-automation-id='wizard-next-button']",
                "button:has-text('Save and Continue')",
                "button:has-text('Continue')",
                "button:has-text('Next')",
            )
            for sel in advance_selectors:
                if page.locator(sel).count() > 0 and page.locator(sel).first.is_visible():
                    if self.click_safe(page, sel, log_to=result, label=f"step_{step_num}_next", timeout_ms=5000):
                        page.wait_for_load_state("networkidle", timeout=15000)
                        humanize_delay(800, 1500)
                        advanced = True
                        break

            # If we couldn't advance, look for a Submit button (we may be at the end)
            if not advanced:
                submit_selectors = (
                    "[data-automation-id='bottom-navigation-submit-button']",
                    "[data-automation-id='wizard-submit-button']",
                    "button:has-text('Submit'):not([disabled])",
                )
                for sel in submit_selectors:
                    if page.locator(sel).count() > 0 and page.locator(sel).first.is_visible():
                        if self.dry_run:
                            result.success = True
                            result.status = "dry_run"
                            result.message = "Would have submitted Workday application"
                            result.add_step("dry_run_skip_submit", "ok", success=True)
                            return
                        humanize_delay(800, 1500)
                        if self.click_safe(page, sel, log_to=result, label="submit", timeout_ms=8000):
                            return self._verify_submission(page, result)
                        else:
                            result.success = False
                            result.status = "failed_submit_button"
                            result.message = "Found submit but click failed"
                            return

                # No advance, no submit, no recognizable controls: bail
                result.success = False
                result.status = "failed_workday_unknown_step"
                result.message = f"Workday step {step_num} has no Next/Submit we recognize"
                return

        # Walked too many steps without finding submit — bail
        result.success = False
        result.status = "failed_workday_too_many_steps"
        result.message = f"Walked {self.MAX_WIZARD_STEPS} wizard steps without reaching Submit"

    # ------------------------------------------------------------------
    # Step helpers
    # ------------------------------------------------------------------

    def _fill_identity_fields(self, page, identity, address, links, result):
        """Fill any of the standard Workday identity fields visible on this step."""
        mappings = (
            ("[data-automation-id='legalNameSection_firstName']", identity["first_name"], "first_name"),
            ("[data-automation-id='legalNameSection_lastName']", identity["last_name"], "last_name"),
            ("[data-automation-id='emailAddress']", identity["email"], "email"),
            ("input[data-automation-id='phoneNumber'], input[data-automation-id='phone-number']",
             identity["phone"], "phone"),
            ("[data-automation-id='addressSection_addressLine1']", address.get("street", ""), "address_line_1"),
            ("[data-automation-id='addressSection_city']", address.get("city", ""), "city"),
            ("[data-automation-id='addressSection_postalCode']", address.get("postal_code", ""), "postal_code"),
        )
        for selector, value, label in mappings:
            self.fill_safe(page, selector, value, log_to=result, label=label)

        # LinkedIn URL
        if links.get("linkedin"):
            for sel in ("input[data-automation-id*='linkedin' i]", "input[id*='linkedin' i]"):
                if page.locator(sel).count() > 0:
                    self.fill_safe(page, sel, links["linkedin"], log_to=result, label="linkedin")
                    break

    def _answer_work_auth(self, page, work_auth, result):
        """Find work-authorization radio/select questions and answer them."""
        try:
            # Workday phrasing: "Are you legally authorized to work in {country}?"
            radios = page.locator("input[type='radio']").all()
            for r in radios:
                try:
                    label_text = (r.evaluate("el => el.closest('label')?.textContent || ''") or "").lower()
                    if not label_text:
                        # Try sibling label
                        label_text = (r.evaluate(
                            "el => { const id=el.getAttribute('id'); "
                            "if (!id) return ''; "
                            "const lbl=document.querySelector(`label[for='${id}']`); "
                            "return lbl ? lbl.textContent : ''; }") or "").lower()
                    val_attr = (r.get_attribute("value") or "").lower()
                    if "authorized" in label_text or "authorization" in label_text:
                        if work_auth["authorized_to_work_us"] and ("yes" in label_text or val_attr == "yes" or val_attr == "true"):
                            r.check()
                            result.add_step("work_auth_yes", "checked", success=True)
                    elif "sponsor" in label_text:
                        wants = "yes" if work_auth["requires_sponsorship"] else "no"
                        if wants in label_text or val_attr == wants:
                            r.check()
                            result.add_step("sponsorship_answer", wants, success=True)
                except Exception:
                    continue
        except Exception as e:
            result.add_step("work_auth_failed", str(e), success=False)

    def _decline_eeoc(self, page, result):
        """Set every EEOC dropdown to its 'Decline to answer' option."""
        decline_phrases = ("Decline to self-identify", "Decline to answer",
                           "I don't wish to answer", "I do not wish to answer",
                           "Prefer not to say")
        try:
            for sel in page.locator(
                "select[data-automation-id*='gender' i], "
                "select[data-automation-id*='race' i], "
                "select[data-automation-id*='ethnicity' i], "
                "select[data-automation-id*='veteran' i], "
                "select[data-automation-id*='disability' i]"
            ).all():
                for phrase in decline_phrases:
                    try:
                        sel.select_option(label=phrase)
                        result.add_step("eeoc_decline", sel.get_attribute("data-automation-id") or "?", success=True)
                        break
                    except Exception:
                        continue
        except Exception as e:
            result.add_step("eeoc_select_failed", str(e), success=False)

    def _verify_submission(self, page, result):
        """Look for a Workday confirmation page after Submit."""
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
            body_text = page.locator("body").inner_text(timeout=5000).lower()
            success_signals = (
                "thank you for applying",
                "your application has been submitted",
                "we have received your application",
                "submitted successfully",
                "application complete",
            )
            if any(sig in body_text for sig in success_signals):
                result.success = True
                result.status = "submitted"
                result.message = "Workday confirmation detected"
                result.add_step("confirmation_seen", page.url, success=True)
            elif "/Applications" in page.url or "myApplications" in page.url:
                # Workday redirects to the user's application list on success
                result.success = True
                result.status = "submitted"
                result.message = "Workday redirected to applications list"
                result.add_step("confirmation_redirect", page.url, success=True)
            else:
                result.success = False
                result.status = "failed_no_confirmation"
                result.message = f"No Workday confirmation found. URL: {page.url}"
                result.add_step("confirmation_missing", page.url, success=False)
        except Exception as e:
            result.success = False
            result.status = "failed_confirmation_check"
            result.message = f"Workday confirmation check error: {e}"
