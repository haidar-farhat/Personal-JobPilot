"""Mail Agent — the queue, the prepare worker's cost guard, and the send refusals.

Two properties are asserted by CALL COUNT rather than by response body, because
the body would look identical if either one were broken:

  - `tailor.calls` proves the cost guard. Preparation resolves the recipient
    BEFORE generating anything, and "no address" is the measured common case
    (0 of 843 live rows had both an address and a CV). A worker that tailors
    first and discovers the missing address afterwards still returns
    blocked/no_recipient — while having spent ~3.5 minutes of LLM time per row.
  - `fake_send.calls` proves the send guards. A cap, suppression or
    not-ready check that runs after send_application_email has opened SMTP is
    not a guard at all.

Nothing here may reach the network, the LLM or the real jobpilot.db:
agents.tailor is replaced by a stub that writes two small files, the mailer is
replaced wholesale, the dead-address file is stubbed and every session comes
from the tmp SQLite fixtures in tests/conftest.py.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.mail_agent as ma
import server.outreach_mail as om
from db.models import (Application, ApplicationStatus, Job, JobScore,
                       MailQueueItem, OutreachSend, OutreachSuppression)

app = FastAPI()
app.include_router(ma.router)
client = TestClient(app)

BASE = "/api/mail-agent"

POST_WITH_ADDRESS = ("We are hiring a Senior Python Engineer. Send your CV to "
                     "careers@acme.com with the subject 'Senior Python 2026'.")
POST_NO_ADDRESS = ("We are hiring a Senior Python Engineer. Apply through the "
                   "LinkedIn Easy Apply button on this post.")
POST_BLOCKED_ONLY = ("Hiring a Data Engineer. Compliance questions to "
                     "compliance@acme.com — applications through our portal.")


# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------

class FakeSender:
    """Stands in for utils.mailer.send_application_email."""

    def __init__(self):
        self.calls: list[dict] = []
        self.result = {"sent": True, "to": "careers@acme.com",
                       "attachments": ["r.docx", "c.docx"]}

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.result)


class FakeTailor:
    """Stands in for agents.tailor.tailor_for_job.

    Writes real files, because the worker checks materials with is_file() rather
    than with a non-null column — output/ is routinely pruned and a path to a
    deleted .docx must not read as "materials ready".

    `gate` lets a test hold the worker inside a row, which is how the singleton
    guard and the cooperative cancel are exercised deterministically instead of
    with a sleep race.
    """

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.calls: list[tuple[str, str]] = []
        self.entered = threading.Event()
        self.gate: threading.Event | None = None

    def __call__(self, job, job_score, *, cover_letter=True, max_rounds=None):
        self.calls.append((job.title, job.company))
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(timeout=20), "gate never released"
        n = len(self.calls)
        resume = self.tmp_path / f"tailored_{n}_resume.docx"
        cover = self.tmp_path / f"tailored_{n}_cover_letter.docx"
        resume.write_text("resume", encoding="utf-8")
        cover.write_text("cover", encoding="utf-8")
        return {"resume_docx": str(resume), "cover_letter_docx": str(cover),
                "resume_pdf": None, "pages": 1}


@pytest.fixture()
def cfg():
    """Permissive-but-complete config; each test tightens the one knob it tests."""
    return {**om.OUTREACH_DEFAULTS, "enabled": True, "daily_cap": 50,
            "max_batch": 10, "max_per_company_per_day": 99,
            "per_recipient_cooldown_days": 0, "min_seconds_between_sends": 0,
            "require_attachments": True}


@pytest.fixture(autouse=True)
def env(monkeypatch, session_factory, cfg, tmp_path):
    """Isolate both modules. Returns the two doubles the assertions read."""
    sender = FakeSender()
    tailor = FakeTailor(tmp_path)

    monkeypatch.setattr(ma, "get_session", session_factory)
    monkeypatch.setattr(om, "get_session", session_factory)
    monkeypatch.setattr(om, "send_application_email", sender)
    monkeypatch.setattr(om, "mailer_ready", lambda: (True, ""))
    monkeypatch.setattr(om, "load_dead", lambda: set())
    monkeypatch.setattr(om, "_outreach_cfg", lambda: dict(cfg))
    monkeypatch.setattr(om, "_profile", lambda: {"identity": {"full_name": "Test User"},
                                                 "links": {}})
    monkeypatch.setattr(om, "_sender_email", lambda: "me@example.com")
    # utils/company_email does live DNS + HTTP. Off unless a test opts in.
    monkeypatch.setattr(om, "_company_fallback_recipient", lambda job, dead: (None, None))
    # utils.linkedin_outreach is a sibling deliverable; force the in-repo
    # fallbacks so these assertions depend only on code that exists today.
    for hook in ("_lo_extract", "_lo_dedup_key", "_lo_build_body", "_lo_subject_for",
                 "_lo_is_suppressed", "_lo_scan_optouts"):
        monkeypatch.setattr(om, hook, None)
    monkeypatch.setattr(ma, "_lo_scan_optouts", None)
    monkeypatch.setattr(ma, "tailor_for_job", tailor)

    # Module-level worker state outlives a single test the way it outlives a
    # single request; reset it so ordering cannot leak a running flag.
    ma._WORKER.update(thread=None, active=False, cancel=False, phase="idle",
                      done=0, total=0, started_at=None, current=None, error=None,
                      prepared=0, blocked=0, failed=0)
    yield sender, tailor
    ma._WORKER["cancel"] = True
    t = ma._WORKER["thread"]
    if t is not None:
        t.join(timeout=20)


@pytest.fixture()
def fake_send(env):
    return env[0]


@pytest.fixture()
def tailor(env):
    return env[1]


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    yield session
    session.close()


@pytest.fixture()
def materials(tmp_path):
    resume = tmp_path / "existing_resume.docx"
    cover = tmp_path / "existing_cover_letter.docx"
    resume.write_text("resume", encoding="utf-8")
    cover.write_text("cover", encoding="utf-8")
    return str(resume), str(cover)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def seed(session, *, company="Acme Robotics", title="Senior Python Engineer",
         suffix="1", description=POST_WITH_ADDRESS, resume=None, cover=None,
         score=70) -> int:
    job = Job(title=title, company=company, url=f"https://li.test/{suffix}",
              source="linkedin_post_unipile", source_id=suffix,
              dedup_hash=f"h-{company}-{suffix}"[:60], description=description)
    session.add(job)
    session.flush()
    if score is not None:
        session.add(JobScore(job_id=job.id, fit_score=score, ats_keywords=["python"],
                             key_matches=["python"], key_gaps=[]))
    app_obj = Application(job_id=job.id, status=ApplicationStatus.FOUND,
                          resume_path=resume, cover_letter_path=cover)
    session.add(app_obj)
    session.commit()
    return app_obj.id


def queue(application_ids: list[int], added_from="manual") -> dict:
    r = client.post(f"{BASE}/queue", json={"application_ids": application_ids,
                                           "added_from": added_from})
    assert r.status_code == 200, r.text
    return r.json()


def wait_idle(timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f"{BASE}/status").json()
        if not st["active"]:
            return st
        time.sleep(0.02)
    raise AssertionError("prepare run did not finish")


def prepare_and_wait(**body) -> dict:
    r = client.post(f"{BASE}/prepare", json=body or {})
    assert r.status_code == 200, r.text
    return wait_idle()


def only_item(db) -> MailQueueItem:
    rows = db.query(MailQueueItem).all()
    assert len(rows) == 1, rows
    db.refresh(rows[0])
    return rows[0]


# --------------------------------------------------------------------------
# queue
# --------------------------------------------------------------------------

def test_queue_add_is_idempotent(db):
    """Re-adding a queued application is a no-op, enforced by the DB constraint."""
    aid = seed(db)
    first = queue([aid], added_from="jobs_filter")
    second = queue([aid], added_from="jobs_filter")

    assert first["added"] == 1 and first["skipped"] == 0
    assert second["added"] == 0 and second["skipped"] == 1
    assert db.query(MailQueueItem).count() == 1


def test_queue_refuses_an_application_already_emailed(db):
    aid = seed(db)
    job = db.query(Job).one()
    db.add(OutreachSend(
        dedup_key=om.outreach_dedup_key(job.company, job.title, str(job.source_id)),
        company=job.company, company_normalized="acme robotics", job_title=job.title,
        recipient="careers@acme.com", recipient_source="post_text",
        status="sent"))
    db.commit()

    res = queue([aid])
    assert res == {**res, "added": 0, "already_sent": 1}
    assert db.query(MailQueueItem).count() == 0


def test_queue_list_delete_and_clear(db):
    a1 = seed(db, suffix="1")
    a2 = seed(db, company="Beta Labs", suffix="2")
    queue([a1, a2])

    listed = client.get(f"{BASE}/queue").json()
    assert listed["total"] == 2
    assert listed["counts_by_status"]["queued"] == 2
    card = listed["items"][0]
    for key in ("id", "application_id", "title", "company", "status", "fit_score",
                "recipient", "preview_hash", "block_reason", "added_at"):
        assert key in card

    victim = listed["items"][0]["id"]
    assert client.delete(f"{BASE}/queue/{victim}").json()["removed"] == 1
    assert client.delete(f"{BASE}/queue/{victim}").status_code == 404

    assert client.post(f"{BASE}/queue/clear", json={}).json()["removed"] == 1
    assert db.query(MailQueueItem).count() == 0


def test_queue_clear_by_status_leaves_other_statuses(db):
    a1 = seed(db, suffix="1")
    a2 = seed(db, company="Beta Labs", suffix="2")
    queue([a1, a2])
    row = db.query(MailQueueItem).first()
    row.status = "ready"
    db.commit()

    assert client.post(f"{BASE}/queue/clear", json={"status": "queued"}).json()["removed"] == 1
    assert [r.status for r in db.query(MailQueueItem).all()] == ["ready"]


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------

def test_prepare_blocks_no_recipient_without_tailoring(db, tailor):
    """THE cost guard: no address is discovered before a single LLM call."""
    aid = seed(db, description=POST_NO_ADDRESS)
    queue([aid])

    st = prepare_and_wait()

    item = only_item(db)
    assert item.status == "blocked"
    assert item.block_reason == "no_recipient"
    assert tailor.calls == [], "tailored a job that can never be emailed"
    assert item.resume_path is None
    assert st["done"] == st["total"] == 1
    assert st["counts_by_status"]["blocked"] == 1


def test_prepare_blocks_when_only_an_unsafe_address_is_published(db, tailor):
    """compliance@ is screened out at extraction, so the row has no recipient."""
    aid = seed(db, description=POST_BLOCKED_ONLY)
    queue([aid])

    prepare_and_wait()

    assert only_item(db).block_reason == "no_recipient"
    assert tailor.calls == []


def test_prepare_blocks_when_already_emailed(db, tailor):
    aid = seed(db)
    queue([aid])
    job = db.query(Job).one()
    db.add(OutreachSend(
        dedup_key=om.outreach_dedup_key(job.company, job.title, str(job.source_id)),
        company=job.company, company_normalized="acme robotics", job_title=job.title,
        recipient="careers@acme.com", recipient_source="post_text",
        status="sent"))
    db.commit()

    prepare_and_wait()

    assert only_item(db).block_reason == "already_emailed"
    assert tailor.calls == []


def test_prepare_generates_materials_only_when_missing(db, tailor, materials):
    """A job with usable files on disk is not re-tailored; one without is."""
    have = seed(db, suffix="1", resume=materials[0], cover=materials[1])
    lacks = seed(db, company="Beta Labs", suffix="2",
                 description="Apply by email: jobs@beta.test")
    queue([have, lacks])

    prepare_and_wait()

    assert [c[1] for c in tailor.calls] == ["Beta Labs"]
    rows = {r.application_id: r for r in db.query(MailQueueItem).all()}
    assert rows[have].resume_path == materials[0]
    assert rows[lacks].resume_path is not None
    assert rows[lacks].resume_path != materials[0]
    # The paths land on BOTH records — Application is what the rest of the
    # pipeline reads, the queue row is what the send path attaches.
    app_obj = db.query(Application).filter(Application.id == lacks).one()
    db.refresh(app_obj)
    assert app_obj.resume_path == rows[lacks].resume_path
    assert app_obj.cover_letter_path == rows[lacks].cover_letter_path


def test_prepared_row_is_ready_with_a_preview(db, tailor):
    aid = seed(db)
    queue([aid])

    st = prepare_and_wait()

    item = only_item(db)
    assert item.status == "ready"
    assert item.recipient == "careers@acme.com"
    assert item.subject
    assert item.body
    assert item.preview_hash, "a ready row with no preview hash can never be sent"
    assert item.prepared_at is not None
    assert item.block_reason is None
    assert st["prepared"] == 1
    assert client.get(f"{BASE}/queue").json()["counts_by_status"]["ready"] == 1


def test_prepare_works_while_sending_is_disabled(db, cfg, tailor):
    """Preparing is not sending — a preview-only install must still prepare."""
    cfg["enabled"] = False
    aid = seed(db)
    queue([aid])

    prepare_and_wait()

    assert only_item(db).status == "ready"


def test_one_failing_row_does_not_stop_the_run(db, tailor, monkeypatch):
    boom = seed(db, suffix="1", description="Apply by email: jobs@boom.test",
                company="Boom Inc")
    fine = seed(db, suffix="2", company="Beta Labs",
                description="Apply by email: jobs@beta.test")
    queue([boom, fine])

    real = ma.tailor_for_job

    def flaky(job, score, **kw):
        if job.company == "Boom Inc":
            raise RuntimeError("Word crashed")
        return real(job, score, **kw)

    monkeypatch.setattr(ma, "tailor_for_job", flaky)
    st = prepare_and_wait()

    rows = {r.application_id: r for r in db.query(MailQueueItem).all()}
    assert rows[boom].status == "failed"
    assert "Word crashed" in (rows[boom].error or "")
    assert rows[fine].status == "ready"
    assert st["done"] == 2 and st["failed"] == 1 and st["prepared"] == 1


def test_prepare_is_a_singleton(db, tailor):
    aid = seed(db)
    queue([aid])
    tailor.gate = threading.Event()

    assert client.post(f"{BASE}/prepare", json={}).json()["started"] is True
    assert tailor.entered.wait(timeout=20)

    second = client.post(f"{BASE}/prepare", json={})
    assert second.status_code == 409, second.text

    tailor.gate.set()
    wait_idle()
    assert len(tailor.calls) == 1


def test_stop_cancels_the_run_between_rows(db, tailor):
    ids = [seed(db, suffix=str(i), company=f"Co {i}",
                description=f"Apply by email: jobs@co{i}.test") for i in range(1, 4)]
    queue(ids)
    tailor.gate = threading.Event()

    client.post(f"{BASE}/prepare", json={})
    assert tailor.entered.wait(timeout=20)
    assert client.post(f"{BASE}/stop").json()["stopped"] is True
    tailor.gate.set()
    st = wait_idle()

    assert st["phase"] == "cancelled"
    assert st["done"] == 1 and st["total"] == 3
    statuses = sorted(r.status for r in db.query(MailQueueItem).all())
    assert statuses == ["queued", "queued", "ready"]
    # Cancel is cooperative: the row that was in flight finished cleanly rather
    # than being abandoned half-written.
    assert len(tailor.calls) == 1


def test_stop_when_idle_is_not_an_error():
    assert client.post(f"{BASE}/stop").json() == {"stopped": False, "reason": "not running"}


# --------------------------------------------------------------------------
# send
# --------------------------------------------------------------------------

def _prepare_one_ready(db, **kw) -> MailQueueItem:
    aid = seed(db, **kw)
    queue([aid])
    prepare_and_wait()
    item = db.query(MailQueueItem).filter(MailQueueItem.application_id == aid).one()
    db.refresh(item)
    assert item.status == "ready", item.block_reason
    return item


def test_send_requires_confirm(db, fake_send):
    item = _prepare_one_ready(db)

    r = client.post(f"{BASE}/send", json={"ids": [item.id], "confirm": "yes"})

    assert r.status_code == 422
    assert fake_send.calls == []
    db.refresh(item)
    assert item.status == "ready"


def test_send_refuses_a_row_that_is_not_ready(db, fake_send):
    aid = seed(db)
    queue([aid])
    item = only_item(db)
    assert item.status == "queued"

    r = client.post(f"{BASE}/send", json={"ids": [item.id], "confirm": "SEND"})

    assert r.status_code == 200
    assert r.json()["results"][0]["refused"] == "not_ready"
    assert fake_send.calls == [], "opened SMTP for a row with no reviewed preview"


def test_send_refuses_a_row_whose_preview_hash_no_longer_matches(db, fake_send):
    item = _prepare_one_ready(db)
    item.body = (item.body or "") + "\n\nPS: hand-edited after the preview."
    db.commit()

    r = client.post(f"{BASE}/send", json={"ids": [item.id], "confirm": "SEND"})

    assert r.json()["results"][0]["refused"] == "preview_stale"
    assert fake_send.calls == []
    db.refresh(item)
    assert item.status == "blocked" and item.block_reason == "preview_stale"


def test_send_delivers_a_ready_row_and_records_the_ledger(db, fake_send):
    item = _prepare_one_ready(db)

    body = client.post(f"{BASE}/send", json={"ids": [item.id], "confirm": "SEND"}).json()

    assert body["sent"] == 1
    assert len(fake_send.calls) == 1
    assert fake_send.calls[0]["to_addr"] == "careers@acme.com"
    db.refresh(item)
    assert item.status == "sent" and item.sent_at is not None
    ledger = db.query(OutreachSend).filter(OutreachSend.status == "sent").one()
    assert item.outreach_send_id == ledger.id


def test_daily_cap_blocks_the_next_send_and_records_it(db, cfg, fake_send):
    cfg["daily_cap"] = 1
    first = _prepare_one_ready(db, suffix="1", company="Acme Robotics")
    second = _prepare_one_ready(db, suffix="2", company="Beta Labs",
                                description="Apply by email: jobs@beta.test")

    body = client.post(f"{BASE}/send",
                       json={"ids": [first.id, second.id], "confirm": "SEND"}).json()

    assert body["sent"] == 1
    assert len(fake_send.calls) == 1, "the cap ran after SMTP opened"
    assert body["results"][1]["blocked"] == "cap_reached"
    db.refresh(second)
    assert second.status == "blocked" and second.block_reason == "cap_reached"
    # The refusal is auditable, and it did not burn the dedup key: the ledger
    # row carries the `#b<nonce>` sentinel so tomorrow's send is still allowed.
    capped = db.query(OutreachSend).filter(OutreachSend.status == "blocked").one()
    assert capped.block_reason == "cap_reached"
    assert "#b" in capped.dedup_key


def test_a_suppressed_recipient_is_never_sent_to(db, fake_send):
    """Suppression added AFTER preparation — the send path must re-screen."""
    item = _prepare_one_ready(db)
    db.add(OutreachSuppression(value="careers@acme.com", scope="address",
                               reason="reply: STOP", source="reply_scan"))
    db.commit()

    body = client.post(f"{BASE}/send", json={"ids": [item.id], "confirm": "SEND"}).json()

    assert body["results"][0]["blocked"] == "suppressed"
    assert fake_send.calls == []
    db.refresh(item)
    assert item.status == "blocked" and item.block_reason == "suppressed"


def test_an_unsafe_recipient_is_never_sent_to(db, fake_send):
    """A stored address that would fail is_safe_recipient today is refused.

    The hash is recomputed for the edited address so the row is genuinely
    `ready` and hash-valid: this asserts the send-time re-screen, not the
    staleness check that would otherwise mask it.
    """
    item = _prepare_one_ready(db)
    job = db.query(Job).one()
    item.recipient = "compliance@acme.com"
    item.preview_hash = om._preview_hash(
        om.outreach_dedup_key(job.company, job.title, str(job.source_id)),
        item.recipient, item.subject or "", item.body or "")
    db.commit()

    body = client.post(f"{BASE}/send", json={"ids": [item.id], "confirm": "SEND"}).json()

    assert body["results"][0]["blocked"] == "unsafe_recipient"
    assert fake_send.calls == []


def test_send_refuses_when_outreach_is_disabled(db, cfg, fake_send):
    item = _prepare_one_ready(db)
    cfg["enabled"] = False

    r = client.post(f"{BASE}/send", json={"ids": [item.id], "confirm": "SEND"})

    assert r.status_code == 403
    assert fake_send.calls == []


def test_status_reports_the_cap_and_the_counts(db):
    aid = seed(db)
    queue([aid])

    st = client.get(f"{BASE}/status").json()

    assert st["active"] is False
    assert st["phase"] == "idle"
    assert st["counts_by_status"]["queued"] == 1
    assert st["cap"] == {"daily_cap": 50, "sent_today": 0, "remaining": 50}
    assert st["started_at"] is None
