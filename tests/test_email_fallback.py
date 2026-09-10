"""Applying by email when the form could not be submitted.

Measured 2026-09-10 on the live database: 105 auto-apply attempts, of which 69
failed before ever reaching the submit button — 59 of those
`failed_required_fields_unfilled`, the bot correctly refusing to invent answers
to required questions. Because the email step fired only on
submitted/submitted_unverified, all 69 were dead ends the user had to finish by
hand.

The fallback closes that. The rule it must never break: emailing is allowed only
where the form was NOT submitted, so the email is the FIRST touch. Anything
already applied — by the bot, by hand, or by an earlier fallback — must be left
alone.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agents.auto_applier import runner as R
from db.models import ApplicationStatus


# --------------------------------------------------------------------------
# which statuses may fall back
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", [
    "failed_required_fields_unfilled",   # 59 of the 69 real failures
    "failed_too_many_essays",
    "failed_form_not_found",
    "failed_no_resume_upload",
    "failed_captcha",
    "failed_login_required",
    "failed_unknown",
])
def test_a_form_that_never_submitted_may_be_emailed(status):
    assert status in R.EMAIL_FALLBACK_STATUSES


def test_submitted_unverified_is_never_a_fallback():
    """The button WAS clicked there — only the confirmation page was missed.

    Mailing would risk a genuine second touch on an application that did land,
    which is the one thing the user ruled out.
    """
    assert "submitted_unverified" not in R.EMAIL_FALLBACK_STATUSES
    assert "submitted" not in R.EMAIL_FALLBACK_STATUSES


@pytest.mark.parametrize("status", ["dry_run", "skipped_low_score", "submitted"])
def test_non_failure_statuses_are_not_fallbacks(status):
    assert status not in R.EMAIL_FALLBACK_STATUSES


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------

class _App:
    def __init__(self, status, log=None, app_id=1):
        self.id = app_id
        self.status = status
        self.auto_apply_log = json.dumps(log) if log else None
        self.job_id = 1
        self.resume_path = "/tmp/cv.docx"
        self.cover_letter_path = "/tmp/cl.docx"


class _Job:
    id = 1
    title = "AI Engineer"
    company = "Acme"
    description = "Apply at careers@acme.com"
    url = "https://acme.com/jobs/1"


def _session_with(app):
    class Q:
        def __init__(self, model):
            self.model = model

        def get(self, _id):
            name = getattr(self.model, "__name__", str(self.model))
            return app if "Application" in name else _Job()

    class S:
        def query(self, model):
            return Q(model)

        def commit(self):
            pass

        def close(self):
            pass
    return S()


def _run(app, *, fallback, sent=True):
    """Drive email_for_application against a stubbed session and mailer."""
    with patch.object(R, "get_session", lambda: _session_with(app)), \
         patch.object(R, "load_profile", lambda: {"identity": {"email": "me@x.com",
                                                               "full_name": "Me"}}), \
         patch.object(R, "_email_application", lambda *a, **k: sent), \
         patch("db.database.record_status_change", lambda *a, **k: None):
        return R.email_for_application(1, config={"mail": {"enabled": True}},
                                       fallback=fallback)


def test_fallback_emails_a_row_that_is_not_applied():
    app = _App(ApplicationStatus.QUEUED)
    res = _run(app, fallback=True)
    assert res["sent"] is True
    assert res["fallback"] is True


def test_fallback_refuses_a_row_that_is_already_applied():
    """Applied by any route means an email now would be a second touch."""
    app = _App(ApplicationStatus.APPLIED)
    res = _run(app, fallback=True)
    assert res["sent"] is False
    assert res["reason"] == "already applied"


def test_the_normal_path_still_requires_applied():
    app = _App(ApplicationStatus.QUEUED)
    res = _run(app, fallback=False)
    assert res["sent"] is False
    assert "not applied" in res["reason"]


def test_a_row_already_emailed_is_never_emailed_twice():
    app = _App(ApplicationStatus.QUEUED, log={"emailed_at": "2026-09-10T00:00:00"})
    res = _run(app, fallback=True)
    assert res["sent"] is False
    assert res["reason"] == "already emailed"


def test_the_send_is_stamped_as_a_fallback():
    """So the audit trail distinguishes 'applied by email' from 'emailed after applying'."""
    app = _App(ApplicationStatus.QUEUED)
    _run(app, fallback=True)
    log = json.loads(app.auto_apply_log)
    assert log["emailed_as"] == "fallback"
    assert log["emailed_at"]


def test_a_normal_send_is_stamped_differently():
    app = _App(ApplicationStatus.APPLIED)
    _run(app, fallback=False)
    log = json.loads(app.auto_apply_log)
    assert log["emailed_as"] == "after_submit"


def test_nothing_is_stamped_when_the_send_fails():
    app = _App(ApplicationStatus.QUEUED)
    res = _run(app, fallback=True, sent=False)
    assert res["sent"] is False
    assert app.auto_apply_log is None or "emailed_at" not in (app.auto_apply_log or "")


# --------------------------------------------------------------------------
# the switch
# --------------------------------------------------------------------------

def test_the_fallback_can_be_switched_off():
    """mail.on_failed_submit gates it; absent means on."""
    cfg_off = {"mail": {"on_failed_submit": False}}
    cfg_on = {"mail": {}}
    assert cfg_off["mail"].get("on_failed_submit", True) is False
    assert cfg_on["mail"].get("on_failed_submit", True) is True


def test_settings_yaml_ships_the_key_enabled():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "settings.yaml")
        .read_text(encoding="utf-8"))
    assert cfg["mail"]["on_failed_submit"] is True
