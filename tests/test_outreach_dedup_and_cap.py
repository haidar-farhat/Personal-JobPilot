"""Outreach guardrails — dedup, daily cap, suppression, screening.

Every test here asserts on the MOCKED SMTP CALL COUNT, not just on the response
body. That is the actual property under test: a dedup check, a cap or a
suppression that runs *after* send_application_email has opened a connection is
not a guardrail at all. `fake_send.calls` is therefore the primary assertion in
most of these, and the JSON is the secondary one.

Nothing in this file may reach the network or the real jobpilot.db: the mailer is
replaced wholesale, the dead-address file is stubbed, and every session comes
from the tmp SQLite fixtures in conftest.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

import server.outreach_mail as om
from db.models import (Application, ApplicationStatus, Job, OutreachSend,
                       OutreachSuppression)
from utils.company_names import normalize_company_name


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

class FakeSender:
    """Stands in for utils.mailer.send_application_email.

    Records every call. `result` is what the next call returns, so a test can
    make delivery fail without touching smtplib.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.result = {"sent": True, "to": "x@y.z",
                       "attachments": ["r.docx", "c.docx"]}

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.result)


@pytest.fixture()
def materials(tmp_path):
    """Two real files on disk — `require_attachments` is checked with is_file()."""
    resume = tmp_path / "acme_resume.docx"
    cover = tmp_path / "acme_cover_letter.docx"
    resume.write_text("resume", encoding="utf-8")
    cover.write_text("cover", encoding="utf-8")
    return str(resume), str(cover)


@pytest.fixture()
def cfg():
    """Permissive-but-complete config; individual tests tighten one knob."""
    return {**om.OUTREACH_DEFAULTS, "enabled": True, "daily_cap": 50,
            "max_per_company_per_day": 99, "per_recipient_cooldown_days": 0,
            "min_seconds_between_sends": 0}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, session_factory, cfg):
    fake = FakeSender()
    monkeypatch.setattr(om, "send_application_email", fake)
    monkeypatch.setattr(om, "mailer_ready", lambda: (True, ""))
    monkeypatch.setattr(om, "load_dead", lambda: set())
    monkeypatch.setattr(om, "get_session", session_factory)
    monkeypatch.setattr(om, "_outreach_cfg", lambda: dict(cfg))
    monkeypatch.setattr(om, "_profile", lambda: {"identity": {"full_name": "Test User"},
                                                 "links": {}})
    monkeypatch.setattr(om, "_sender_email", lambda: "me@example.com")
    # utils.linkedin_outreach is written concurrently; neutralise every hook into
    # it so these guardrail assertions depend only on code in this repo today.
    for hook in ("_lo_extract", "_lo_dedup_key", "_lo_build_body", "_lo_subject_for",
                 "_lo_is_suppressed", "_lo_scan_optouts"):
        monkeypatch.setattr(om, hook, None)
    return fake


@pytest.fixture()
def fake_send(_isolate):
    return _isolate


def make_cand(*, company="Acme Robotics", title="Senior Python Engineer",
              recipient="careers@acme.com", external_id="1",
              application_id=0, job_id=0, resume=None, cover=None) -> om._Candidate:
    return om._Candidate(
        application_id=application_id, job_id=job_id, title=title, company=company,
        company_normalized=normalize_company_name(company) or "",
        location="Berlin", url="https://li.test/1", post_url="https://li.test/1",
        source="linkedin_post_unipile", external_id=external_id, fit_score=70,
        dedup_key=om.outreach_dedup_key(company, title, external_id),
        recipient=recipient, recipient_source="post_text", channel="email",
        contact={"instructions": "Send your CV."},
        resume_path=resume, cover_letter_path=cover,
        subject=f"{title} — Test User", body=f"Body for {title} at {company}.",
        headers={})


def seed_application(session, *, company="Acme Robotics",
                     title="Senior Python Engineer", suffix="1",
                     resume=None, cover=None) -> tuple[int, int]:
    job = Job(title=title, company=company, url=f"https://li.test/{suffix}",
              source="linkedin_post_unipile", source_id=suffix,
              dedup_hash=f"h{company}{suffix}"[:60],
              description=f"Email careers@acme.com about {title}.")
    session.add(job)
    session.flush()
    app_obj = Application(job_id=job.id, status=ApplicationStatus.FOUND,
                          resume_path=resume, cover_letter_path=cover)
    session.add(app_obj)
    session.commit()
    return app_obj.id, job.id


# --------------------------------------------------------------------------
# dedup
# --------------------------------------------------------------------------

def test_second_send_same_company_job_is_blocked(tmp_session, fake_send, cfg, materials):
    """The point is `len(fake_send.calls) == 1`.

    A dedup check that runs after SMTP is not a dedup check.
    """
    r, c = materials
    first = om._send_one(tmp_session, make_cand(resume=r, cover=c), cfg)
    assert first["sent"] is True
    assert len(fake_send.calls) == 1
    assert tmp_session.query(OutreachSend).filter_by(status="sent").count() == 1

    second = om._send_one(tmp_session, make_cand(resume=r, cover=c), cfg)
    assert second["sent"] is False
    assert second["blocked"] == "already_emailed"
    assert len(fake_send.calls) == 1          # <-- the guarantee
    assert tmp_session.query(OutreachSend).count() == 1


def test_dedup_is_a_db_constraint_not_a_python_check(tmp_session):
    """Proves create_all really built the UNIQUE constraint on this schema."""
    def row():
        return OutreachSend(dedup_key="deadbeef", company="Acme",
                            company_normalized="acme", job_title="Eng",
                            recipient="careers@acme.com", recipient_source="post_text",
                            status="sent")
    tmp_session.add(row())
    tmp_session.commit()
    tmp_session.add(row())
    with pytest.raises(IntegrityError):
        tmp_session.commit()
    tmp_session.rollback()


def test_blocked_row_does_not_burn_the_dedup_key(tmp_session, fake_send, cfg, materials):
    """A block is not a send: fix the cause and the row must still be sendable.

    Blocked rows carry the `<key>#b<nonce>` sentinel precisely so that a missing
    CV, once prepared, does not read as "already emailed" forever.
    """
    r, c = materials
    blocked = om._send_one(tmp_session, make_cand(resume="nope.docx", cover=c), cfg)
    assert blocked["blocked"] == "no_attachments"
    assert not fake_send.calls

    ok = om._send_one(tmp_session, make_cand(resume=r, cover=c), cfg)
    assert ok["sent"] is True
    assert len(fake_send.calls) == 1
    keys = {om._canonical_key(r_.dedup_key) for r_ in tmp_session.query(OutreachSend).all()}
    assert keys == {om.outreach_dedup_key("Acme Robotics", "Senior Python Engineer", "1")}


# --------------------------------------------------------------------------
# daily cap
# --------------------------------------------------------------------------

def test_daily_cap_blocks_the_next_send(tmp_session, fake_send, cfg, materials):
    r, c = materials
    cfg["daily_cap"] = 2
    results = [om._send_one(tmp_session,
                            make_cand(title=f"Engineer {i}", external_id=str(i),
                                      resume=r, cover=c), cfg)
               for i in range(3)]
    assert [x["sent"] for x in results] == [True, True, False]
    assert results[2]["blocked"] == "cap_reached"
    assert len(fake_send.calls) == 2
    blocked_rows = tmp_session.query(OutreachSend).filter_by(status="blocked").all()
    assert [b.block_reason for b in blocked_rows] == ["cap_reached"]


def test_cap_counts_only_sent_rows_today(tmp_session, cfg):
    """Yesterday's send and today's failure both leave the cap untouched."""
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    tmp_session.add(OutreachSend(dedup_key="k-old", company="A", company_normalized="a",
                                 job_title="Old", recipient="a@a.com",
                                 recipient_source="post_text", status="sent",
                                 sent_at=yesterday))
    tmp_session.add(OutreachSend(dedup_key="k-fail", company="A", company_normalized="a",
                                 job_title="Failed", recipient="b@a.com",
                                 recipient_source="post_text", status="failed"))
    tmp_session.commit()
    cfg["daily_cap"] = 5
    assert om._sent_today(tmp_session) == 0
    assert om._remaining_today(tmp_session, cfg) == 5


def test_cap_survives_a_new_session(session_factory, fake_send, cfg, materials):
    """The property the auto-applier's in-memory counter does not have.

    Fill the cap through one session, then count it from a brand-new one — a
    restart, another worker, the scheduler process. It is still zero.
    """
    r, c = materials
    cfg["daily_cap"] = 1
    s1 = session_factory()
    assert om._send_one(s1, make_cand(resume=r, cover=c), cfg)["sent"] is True
    s1.close()

    s2 = session_factory()
    try:
        assert om._remaining_today(s2, cfg) == 0
        blocked = om._send_one(s2, make_cand(title="Other", external_id="2",
                                             resume=r, cover=c), cfg)
        assert blocked["blocked"] == "cap_reached"
    finally:
        s2.close()
    assert len(fake_send.calls) == 1


def test_per_company_cap(tmp_session, fake_send, cfg, materials):
    r, c = materials
    cfg["max_per_company_per_day"] = 1
    first = om._send_one(tmp_session, make_cand(title="Role A", external_id="a",
                                                resume=r, cover=c), cfg)
    second = om._send_one(tmp_session, make_cand(title="Role B", external_id="b",
                                                 resume=r, cover=c), cfg)
    assert first["sent"] is True
    assert second["blocked"] == "company_capped"
    assert len(fake_send.calls) == 1


def test_recipient_cooldown(tmp_session, fake_send, cfg, materials):
    """Five jobs at one careers@ must not become five emails to it."""
    r, c = materials
    cfg["per_recipient_cooldown_days"] = 30
    om._send_one(tmp_session, make_cand(company="Acme Robotics", external_id="a",
                                        resume=r, cover=c), cfg)
    blocked = om._send_one(tmp_session,
                           make_cand(company="Beta Industries", title="Other Role",
                                     external_id="b", resume=r, cover=c), cfg)
    assert blocked["blocked"] == "cooldown"
    assert len(fake_send.calls) == 1


# --------------------------------------------------------------------------
# screening
# --------------------------------------------------------------------------

def test_suppressed_address_never_sent(tmp_session, fake_send, cfg, materials):
    r, c = materials
    tmp_session.add(OutreachSuppression(value="careers@acme.com", scope="address",
                                        source="reply_scan", reason="reply: STOP"))
    tmp_session.commit()
    res = om._send_one(tmp_session, make_cand(resume=r, cover=c), cfg)
    assert res["blocked"] == "suppressed"
    assert fake_send.calls == []


def test_suppressed_domain_blocks_every_address_at_it(tmp_session, fake_send, cfg,
                                                      materials):
    r, c = materials
    tmp_session.add(OutreachSuppression(value="acme.com", scope="domain",
                                        source="reply_scan"))
    tmp_session.commit()
    res = om._send_one(tmp_session,
                       make_cand(recipient="jobs@acme.com", resume=r, cover=c), cfg)
    assert res["blocked"] == "suppressed"
    assert fake_send.calls == []


@pytest.mark.parametrize("addr", ["accommodation@acme.com", "legal@acme.com",
                                  "noreply@acme.com", "compliance@acme.com"])
def test_unsafe_recipient_rechecked_at_send_time(tmp_session, fake_send, cfg,
                                                 materials, addr):
    """The address may have been hand-edited after preview — re-screen it."""
    r, c = materials
    res = om._send_one(tmp_session, make_cand(recipient=addr, resume=r, cover=c), cfg)
    assert res["blocked"] == "unsafe_recipient"
    assert fake_send.calls == []


def test_dead_address_blocks_send(monkeypatch, tmp_session, fake_send, cfg, materials):
    r, c = materials
    monkeypatch.setattr(om, "load_dead", lambda: {"careers@acme.com"})
    res = om._send_one(tmp_session, make_cand(resume=r, cover=c), cfg)
    assert res["blocked"] == "dead_address"
    assert fake_send.calls == []


def test_missing_attachments_blocks_send(tmp_session, fake_send, cfg, materials):
    """Every email is individually tailored — no CV for THIS job, no mail."""
    _, c = materials
    res = om._send_one(tmp_session,
                       make_cand(resume="output/resumes/does_not_exist.docx",
                                 cover=c), cfg)
    assert res["blocked"] == "no_attachments"
    assert fake_send.calls == []


def test_no_recipient_blocks_send(tmp_session, fake_send, cfg, materials):
    r, c = materials
    res = om._send_one(tmp_session, make_cand(recipient=None, resume=r, cover=c), cfg)
    assert res["blocked"] == "no_recipient"
    assert fake_send.calls == []


# --------------------------------------------------------------------------
# failure / retry / bookkeeping
# --------------------------------------------------------------------------

def test_failed_send_keeps_row_and_allows_retry(tmp_session, fake_send, cfg,
                                                materials, session_factory,
                                                monkeypatch):
    r, c = materials
    fake_send.result = {"sent": False, "error": "SMTP auth failed"}
    res = om._send_one(tmp_session, make_cand(resume=r, cover=c), cfg)
    assert res["sent"] is False and res["error"] == "SMTP auth failed"
    row = tmp_session.query(OutreachSend).one()
    assert row.status == "failed" and row.sent_at is None
    assert om._remaining_today(tmp_session, cfg) == cfg["daily_cap"]  # cap untouched
    send_id = row.id
    tmp_session.close()

    fake_send.result = {"sent": True, "to": "careers@acme.com",
                        "attachments": ["r.docx", "c.docx"]}
    out = om.retry_send(send_id, om.ConfirmRequest(confirm="SEND"))
    assert out["sent"] is True
    assert len(fake_send.calls) == 2

    s = session_factory()
    try:
        assert s.query(OutreachSend).get(send_id).status == "sent"
    finally:
        s.close()


def test_sent_row_cannot_be_retried(tmp_session, fake_send, cfg, materials):
    """'sent' is terminal — the retry route must not offer a second delivery."""
    from fastapi import HTTPException
    r, c = materials
    res = om._send_one(tmp_session, make_cand(resume=r, cover=c), cfg)
    tmp_session.close()
    with pytest.raises(HTTPException) as e:
        om.retry_send(res["send_id"], om.ConfirmRequest(confirm="SEND"))
    assert e.value.status_code == 404
    assert len(fake_send.calls) == 1


def test_retry_requires_confirm(tmp_session, fake_send, cfg, materials):
    from fastapi import HTTPException
    r, c = materials
    fake_send.result = {"sent": False, "error": "SMTP auth failed"}
    res = om._send_one(tmp_session, make_cand(resume=r, cover=c), cfg)
    tmp_session.close()
    with pytest.raises(HTTPException) as e:
        om.retry_send(res["send_id"], om.ConfirmRequest(confirm="send"))
    assert e.value.status_code == 422
    assert len(fake_send.calls) == 1


def test_successful_send_stamps_emailed_at(tmp_session, fake_send, cfg, materials):
    """auto_apply_log['emailed_at'] is what stops the auto-applier double-mailing."""
    r, c = materials
    app_id, job_id = seed_application(tmp_session, resume=r, cover=c)
    cand = make_cand(application_id=app_id, job_id=job_id, resume=r, cover=c)
    assert om._send_one(tmp_session, cand, cfg)["sent"] is True

    app_obj = tmp_session.query(Application).get(app_id)
    assert app_obj.status == ApplicationStatus.APPLIED
    assert json.loads(app_obj.auto_apply_log)["emailed_at"]


def test_ledger_stores_the_exact_body_sent(tmp_session, fake_send, cfg, materials):
    r, c = materials
    cand = make_cand(resume=r, cover=c)
    om._send_one(tmp_session, cand, cfg)
    row = tmp_session.query(OutreachSend).one()
    assert row.body == cand.body
    assert fake_send.calls[0]["body"] == cand.body
    assert row.body_hash == om._preview_hash(cand.dedup_key, cand.recipient,
                                             cand.subject, cand.body)


# --------------------------------------------------------------------------
# preview is free of side effects
# --------------------------------------------------------------------------

def test_preview_performs_zero_smtp_calls(tmp_session, fake_send, cfg, materials,
                                          session_factory):
    """Dry-run is the default: previewing must never touch the mailer or the ledger."""
    r, c = materials
    app_id, _ = seed_application(tmp_session, resume=r, cover=c)
    tmp_session.close()

    for _ in range(3):
        om.preview_one(app_id, use_llm=False)
    om.preview_batch(om.PreviewBatchRequest(application_ids=[app_id], use_llm=False))

    assert fake_send.calls == []
    s = session_factory()
    try:
        assert s.query(OutreachSend).count() == 0
    finally:
        s.close()
