"""Outreach routes over TestClient — shapes, the 422/409 refusals, the ledger.

SMTP is replaced wholesale and `fake_send.calls` is asserted on every refusal
path, because the contract this feature sells is "nothing leaves the machine
until you explicitly say SEND with the hash of the exact bytes you previewed".
An endpoint that returns the right status code while still having opened a
connection would satisfy the JSON assertions and violate the contract.

utils.linkedin_outreach is a sibling deliverable written concurrently, so these
tests force the router's built-in regex fallback (`_lo_extract = None`) rather
than depending on it. That keeps this file green whether or not that module has
landed, and exercises the degraded path that must work if it ever breaks.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server.dashboard as dash
import server.outreach_mail as om
from db.models import (Application, ApplicationStatus, Job, OutreachSend,
                       OutreachSuppression)

# server/dashboard.py registers this router in the same feature branch; a test
# must not depend on the order the two land in, and including a router twice
# would duplicate every path.
if not any(getattr(r, "path", "").startswith("/api/outreach") for r in dash.app.routes):
    dash.app.include_router(om.router)

client = TestClient(dash.app)

POST_WITH_ADDRESS = ("We are hiring a Senior Python Engineer. Send your CV to "
                     "careers@acme.com with the subject 'Senior Python 2026'. "
                     "Accessibility requests go to accommodation@acme.com.")
POST_BLOCKED_ONLY = ("Hiring a Data Engineer. For compliance questions email "
                     "compliance@acme.com — applications through our portal.")


class FakeSender:
    def __init__(self):
        self.calls: list[dict] = []
        self.result = {"sent": True, "to": "careers@acme.com",
                       "attachments": ["r.docx", "c.docx"]}

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.result)


@pytest.fixture()
def materials(tmp_path):
    resume = tmp_path / "acme_resume.docx"
    cover = tmp_path / "acme_cover_letter.docx"
    resume.write_text("resume", encoding="utf-8")
    cover.write_text("cover", encoding="utf-8")
    return str(resume), str(cover)


@pytest.fixture()
def cfg():
    return {**om.OUTREACH_DEFAULTS, "enabled": True, "daily_cap": 20,
            "max_per_company_per_day": 99, "per_recipient_cooldown_days": 0,
            "min_seconds_between_sends": 0, "max_batch": 10}


@pytest.fixture(autouse=True)
def fake_send(monkeypatch, session_factory, cfg):
    sender = FakeSender()
    monkeypatch.setattr(om, "send_application_email", sender)
    monkeypatch.setattr(om, "mailer_ready", lambda: (True, ""))
    monkeypatch.setattr(om, "load_dead", lambda: set())
    monkeypatch.setattr(om, "get_session", session_factory)
    monkeypatch.setattr(om, "_outreach_cfg", lambda: dict(cfg))
    monkeypatch.setattr(om, "_profile", lambda: {"identity": {"full_name": "Test User"},
                                                 "links": {}})
    monkeypatch.setattr(om, "_sender_email", lambda: "me@example.com")
    monkeypatch.setattr(om, "_lo_extract", None)      # force the built-in extractor
    monkeypatch.setattr(om, "_lo_dedup_key", None)
    monkeypatch.setattr(om, "_lo_build_body", None)
    monkeypatch.setattr(om, "_lo_subject_for", None)
    monkeypatch.setattr(om, "_lo_is_suppressed", None)
    monkeypatch.setattr(om, "_lo_scan_optouts", None)
    om._BATCH.update(active=False, total=0, done=0, results=[], started_at=None)
    return sender


def seed(session, *, title, company="Acme Robotics", description="", suffix="1",
         source="linkedin_post_unipile", resume=None, cover=None, fit=None) -> int:
    job = Job(title=title, company=company, url=f"https://li.test/{suffix}",
              source=source, source_id=suffix, dedup_hash=f"dh-{suffix}",
              description=description, location="Berlin, Germany")
    session.add(job)
    session.flush()
    if fit is not None:
        from db.models import JobScore
        session.add(JobScore(job_id=job.id, fit_score=fit))
    app_obj = Application(job_id=job.id, status=ApplicationStatus.FOUND,
                          resume_path=resume, cover_letter_path=cover)
    session.add(app_obj)
    session.commit()
    return app_obj.id


@pytest.fixture()
def seeded(session_factory, materials):
    r, c = materials
    s = session_factory()
    try:
        good = seed(s, title="Senior Python Engineer", description=POST_WITH_ADDRESS,
                    suffix="1", resume=r, cover=c, fit=78)
        blocked = seed(s, title="Data Engineer", company="Beta Industries",
                       description=POST_BLOCKED_ONLY, suffix="2",
                       resume=r, cover=c, fit=40)
        seed(s, title="Ignored Non-LinkedIn Role", description=POST_WITH_ADDRESS,
             suffix="3", source="greenhouse", resume=r, cover=c)
        return {"good": good, "blocked": blocked}
    finally:
        s.close()


# --------------------------------------------------------------------------
# candidates
# --------------------------------------------------------------------------

def test_candidates_shape(seeded, fake_send):
    body = client.get("/api/outreach/candidates").json()
    assert set(body) == {"candidates", "counts", "cap"}
    assert set(body["cap"]) == {"daily_cap", "sent_today", "remaining"}
    assert set(body["counts"]) == {"total", "eligible", "no_recipient",
                                   "already_sent", "no_materials"}

    by_id = {c["application_id"]: c for c in body["candidates"]}
    # the non-LinkedIn job is not an outreach candidate at all
    assert len(by_id) == 2

    good = by_id[seeded["good"]]
    assert good["recipient"] == "careers@acme.com"
    assert good["eligible"] is True and good["blocked"] is None
    assert good["materials_ready"] is True
    assert good["fit_score"] == 78
    assert good["dedup_key"]
    # the accommodation inbox in the same post was screened out, not offered
    assert "accommodation@acme.com" not in str(good["recipient"])
    assert any(r["address"] == "accommodation@acme.com" and r["reason"] == "blocked_local"
               for r in good["rejected"])

    bad = by_id[seeded["blocked"]]
    assert bad["recipient"] is None
    assert bad["blocked"] == "no_recipient"
    assert bad["eligible"] is False
    assert fake_send.calls == []


def test_candidates_orders_by_fit_score(seeded):
    ids = [c["application_id"] for c in client.get("/api/outreach/candidates").json()["candidates"]]
    assert ids[0] == seeded["good"]        # fit 78 before fit 40


def test_candidates_min_score_filter(seeded):
    body = client.get("/api/outreach/candidates?min_score=60").json()
    assert [c["application_id"] for c in body["candidates"]] == [seeded["good"]]


# --------------------------------------------------------------------------
# preview — the dry-run default
# --------------------------------------------------------------------------

def test_preview_writes_nothing(seeded, session_factory, fake_send):
    for _ in range(2):
        assert client.get(f"/api/outreach/preview/{seeded['good']}?use_llm=false").status_code == 200
    client.post("/api/outreach/preview",
                json={"application_ids": [seeded["good"]], "use_llm": False})
    s = session_factory()
    try:
        assert s.query(OutreachSend).count() == 0
    finally:
        s.close()
    assert fake_send.calls == []


def test_preview_returns_stable_hash(seeded):
    url = f"/api/outreach/preview/{seeded['good']}?use_llm=false"
    a, b = client.get(url).json(), client.get(url).json()
    assert a["preview_hash"] == b["preview_hash"]
    assert a["dry_run"] is True
    assert a["to"] == "careers@acme.com"
    assert a["subject"] and a["body"]
    assert {x["name"] for x in a["attachments"]} == {"acme_resume.docx",
                                                     "acme_cover_letter.docx"}
    assert all(x["exists"] for x in a["attachments"])
    assert a["headers"]["List-Unsubscribe"] == "<mailto:me@example.com?subject=unsubscribe>"


def test_preview_batch_over_max_is_422(seeded):
    r = client.post("/api/outreach/preview", json={"application_ids": list(range(30))})
    assert r.status_code == 422


def test_preview_batch_reports_per_row_errors(seeded):
    r = client.post("/api/outreach/preview",
                    json={"application_ids": [seeded["good"], 99999], "use_llm": False})
    body = r.json()
    assert len(body["previews"]) == 1
    assert body["errors"][0]["application_id"] == 99999


def test_preview_404_for_unknown_application():
    assert client.get("/api/outreach/preview/424242").status_code == 404


# --------------------------------------------------------------------------
# send refusals — every one asserts zero SMTP calls
# --------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"preview_hash": "x"},                       # confirm omitted entirely
    {"confirm": "send", "preview_hash": "x"},    # lowercase is not confirmation
    {"confirm": "SEND ", "preview_hash": "x"},   # nor is a near-miss
])
def test_send_without_confirm_is_422(seeded, fake_send, payload):
    r = client.post(f"/api/outreach/send/{seeded['good']}", json=payload)
    assert r.status_code == 422
    assert fake_send.calls == []


def test_send_with_stale_preview_hash_is_409(seeded, fake_send):
    r = client.post(f"/api/outreach/send/{seeded['good']}",
                    json={"confirm": "SEND", "preview_hash": "deadbeef"})
    assert r.status_code == 409
    assert "expected" in r.json()["detail"]
    assert fake_send.calls == []


def test_send_after_body_edit_requires_new_hash(seeded, fake_send):
    pv = client.get(f"/api/outreach/preview/{seeded['good']}?use_llm=false").json()
    stale = client.post(f"/api/outreach/send/{seeded['good']}",
                        json={"confirm": "SEND", "preview_hash": pv["preview_hash"],
                              "body": pv["body"] + "\n\nPS edited by hand."})
    assert stale.status_code == 409
    assert fake_send.calls == []

    fresh_hash = stale.json()["detail"]["expected"]
    ok = client.post(f"/api/outreach/send/{seeded['good']}",
                     json={"confirm": "SEND", "preview_hash": fresh_hash,
                           "body": pv["body"] + "\n\nPS edited by hand."})
    assert ok.status_code == 200 and ok.json()["sent"] is True
    assert len(fake_send.calls) == 1
    assert fake_send.calls[0]["body"].endswith("PS edited by hand.")


def test_send_to_hand_edited_unsafe_address_is_blocked(seeded, fake_send):
    """The edit path is re-screened, not trusted."""
    pv = client.get(f"/api/outreach/preview/{seeded['good']}?use_llm=false").json()
    probe = client.post(f"/api/outreach/send/{seeded['good']}",
                        json={"confirm": "SEND", "preview_hash": "x",
                              "to": "legal@acme.com"})
    r = client.post(f"/api/outreach/send/{seeded['good']}",
                    json={"confirm": "SEND", "to": "legal@acme.com",
                          "preview_hash": probe.json()["detail"]["expected"]})
    assert r.status_code == 200
    assert r.json()["blocked"] == "unsafe_recipient"
    assert fake_send.calls == []
    assert pv["to"] == "careers@acme.com"


def test_mailer_not_configured_is_400(monkeypatch, seeded, fake_send):
    monkeypatch.setattr(om, "mailer_ready",
                        lambda: (False, "config/gmail.yaml needs address + app_password"))
    r = client.post(f"/api/outreach/send/{seeded['good']}",
                    json={"confirm": "SEND", "preview_hash": "x"})
    assert r.status_code == 400
    assert "gmail.yaml" in r.json()["detail"]
    assert fake_send.calls == []


def test_send_is_403_when_outreach_disabled(monkeypatch, seeded, fake_send, cfg):
    monkeypatch.setattr(om, "_outreach_cfg", lambda: {**cfg, "enabled": False})
    r = client.post(f"/api/outreach/send/{seeded['good']}",
                    json={"confirm": "SEND", "preview_hash": "x"})
    assert r.status_code == 403
    assert fake_send.calls == []


def test_send_happy_path_writes_the_ledger(seeded, fake_send, session_factory):
    pv = client.get(f"/api/outreach/preview/{seeded['good']}?use_llm=false").json()
    r = client.post(f"/api/outreach/send/{seeded['good']}",
                    json={"confirm": "SEND", "preview_hash": pv["preview_hash"]})
    body = r.json()
    assert r.status_code == 200 and body["sent"] is True
    assert set(body) >= {"sent", "send_id", "to", "attachments", "sent_at",
                         "remaining_today", "blocked", "error"}
    assert body["to"] == "careers@acme.com"
    assert body["remaining_today"] == 19
    assert len(fake_send.calls) == 1

    s = session_factory()
    try:
        row = s.query(OutreachSend).one()
        assert row.status == "sent" and row.body == pv["body"]
        assert row.dedup_key == pv["dedup_key"]
    finally:
        s.close()


# --------------------------------------------------------------------------
# batch
# --------------------------------------------------------------------------

def test_batch_over_max_is_422(seeded, fake_send):
    items = [{"application_id": seeded["good"], "preview_hash": "x"} for _ in range(11)]
    r = client.post("/api/outreach/send-batch", json={"confirm": "SEND", "items": items})
    assert r.status_code == 422
    assert fake_send.calls == []


def test_batch_without_confirm_is_422(seeded, fake_send):
    r = client.post("/api/outreach/send-batch",
                    json={"items": [{"application_id": seeded["good"]}]})
    assert r.status_code == 422
    assert fake_send.calls == []


def test_batch_is_singleton(seeded, fake_send):
    """Two clicks must not start two batches (unlike /api/job-sources/scan)."""
    om._BATCH["active"] = True
    try:
        r = client.post("/api/outreach/send-batch",
                        json={"confirm": "SEND",
                              "items": [{"application_id": seeded["good"],
                                         "preview_hash": "x"}]})
        assert r.status_code == 409
        assert fake_send.calls == []
    finally:
        om._BATCH["active"] = False


def test_batch_sends_then_reports_done(seeded, fake_send):
    pv = client.get(f"/api/outreach/preview/{seeded['good']}?use_llm=false").json()
    r = client.post("/api/outreach/send-batch",
                    json={"confirm": "SEND",
                          "items": [{"application_id": seeded["good"],
                                     "preview_hash": pv["preview_hash"]}]})
    assert r.json() == {"started": True, "total": 1}
    thread = om._BATCH["thread"]
    thread.join(timeout=30)
    assert om._BATCH["active"] is False
    assert om._BATCH["done"] == 1
    assert om._BATCH["results"][0]["sent"] is True
    assert len(fake_send.calls) == 1


def test_batch_refuses_a_stale_hash_without_sending(seeded, fake_send):
    r = client.post("/api/outreach/send-batch",
                    json={"confirm": "SEND",
                          "items": [{"application_id": seeded["good"],
                                     "preview_hash": "deadbeef"}]})
    assert r.json()["started"] is True
    om._BATCH["thread"].join(timeout=30)
    assert om._BATCH["results"][0]["blocked"] == "preview_stale"
    assert fake_send.calls == []


# --------------------------------------------------------------------------
# status / history / audit
# --------------------------------------------------------------------------

def test_status_reports_cap_and_readiness(seeded, session_factory, cfg):
    s = session_factory()
    try:
        s.add(OutreachSend(dedup_key="k1", company="Acme", company_normalized="acme",
                           job_title="Eng", recipient="careers@acme.com",
                           recipient_source="post_text", status="sent",
                           sent_at=om._utc_now()))
        s.add(OutreachSuppression(value="stop@acme.com", scope="address",
                                  source="reply_scan"))
        s.commit()
    finally:
        s.close()

    body = client.get("/api/outreach/status").json()
    assert body["mailer_ready"] is True and body["dry_run_default"] is True
    assert body["enabled"] is True
    assert body["cap"]["daily_cap"] == cfg["daily_cap"]
    assert body["cap"]["sent_today"] == 1
    assert body["cap"]["remaining"] == cfg["daily_cap"] - 1
    assert set(body["cap"]) >= {"max_per_company_per_day", "per_recipient_cooldown_days",
                                "max_batch", "min_seconds_between_sends"}
    assert body["suppressions"] == 1
    assert body["totals"]["sent"] == 1
    assert isinstance(body["providers"], list)
    assert set(body) >= {"batch", "dead_addresses", "mailer_reason"}


def test_status_reports_mailer_failure_reason(monkeypatch):
    monkeypatch.setattr(om, "mailer_ready", lambda: (False, "no app password"))
    body = client.get("/api/outreach/status").json()
    assert body["mailer_ready"] is False
    assert body["mailer_reason"] == "no app password"


def test_history_returns_ledger_rows(seeded, fake_send):
    pv = client.get(f"/api/outreach/preview/{seeded['good']}?use_llm=false").json()
    client.post(f"/api/outreach/send/{seeded['good']}",
                json={"confirm": "SEND", "preview_hash": pv["preview_hash"]})
    body = client.get("/api/outreach/history?limit=10").json()
    assert body["count"] == 1
    row = body["sends"][0]
    assert set(row) >= {"id", "company", "job_title", "recipient", "status",
                        "block_reason", "error", "subject", "sent_at",
                        "created_at", "post_url"}
    assert row["status"] == "sent"
    assert "body" not in row              # the list view is not the audit view


def test_history_filters_by_status(session_factory):
    s = session_factory()
    try:
        for i, st in enumerate(("sent", "failed", "blocked")):
            s.add(OutreachSend(dedup_key=f"k{i}", company="A", company_normalized="a",
                               job_title="T", recipient="a@a.com",
                               recipient_source="post_text", status=st))
        s.commit()
    finally:
        s.close()
    body = client.get("/api/outreach/history?status=failed").json()
    assert [r["status"] for r in body["sends"]] == ["failed"]


def test_send_detail_returns_exact_body(seeded, fake_send):
    pv = client.get(f"/api/outreach/preview/{seeded['good']}?use_llm=false").json()
    sent = client.post(f"/api/outreach/send/{seeded['good']}",
                       json={"confirm": "SEND", "preview_hash": pv["preview_hash"]}).json()
    detail = client.get(f"/api/outreach/send/{sent['send_id']}").json()
    assert detail["body"] == pv["body"]
    assert detail["subject"] == pv["subject"]
    assert detail["attachments"] == ["r.docx", "c.docx"]
    assert detail["recipient"] == "careers@acme.com"


def test_send_detail_404():
    assert client.get("/api/outreach/send/987654").status_code == 404


# --------------------------------------------------------------------------
# suppressions
# --------------------------------------------------------------------------

def test_suppress_endpoint_idempotent(session_factory):
    first = client.post("/api/outreach/suppress",
                        json={"value": "Careers@Acme.com", "scope": "address"}).json()
    second = client.post("/api/outreach/suppress",
                         json={"value": "careers@acme.com", "scope": "address"}).json()
    assert first["created"] is True and second["created"] is False
    assert first["id"] == second["id"]
    s = session_factory()
    try:
        assert s.query(OutreachSuppression).count() == 1
        assert s.query(OutreachSuppression).one().value == "careers@acme.com"
    finally:
        s.close()


def test_suppression_blocks_a_later_send(seeded, fake_send):
    client.post("/api/outreach/suppress", json={"value": "acme.com", "scope": "domain"})
    pv = client.get(f"/api/outreach/preview/{seeded['good']}?use_llm=false").json()
    assert pv["blocked"] == "suppressed"
    r = client.post(f"/api/outreach/send/{seeded['good']}",
                    json={"confirm": "SEND", "preview_hash": pv["preview_hash"]})
    assert r.json()["blocked"] == "suppressed"
    assert fake_send.calls == []


def test_reply_scan_suppression_cannot_be_deleted(session_factory):
    s = session_factory()
    try:
        row = OutreachSuppression(value="stop@acme.com", scope="address",
                                  source="reply_scan", reason="reply: STOP")
        s.add(row)
        s.commit()
        rid = row.id
    finally:
        s.close()
    assert client.delete(f"/api/outreach/suppress/{rid}").status_code == 403


def test_manual_suppression_can_be_deleted(session_factory):
    rid = client.post("/api/outreach/suppress",
                      json={"value": "oops@acme.com"}).json()["id"]
    assert client.delete(f"/api/outreach/suppress/{rid}").status_code == 200
    s = session_factory()
    try:
        assert s.query(OutreachSuppression).count() == 0
    finally:
        s.close()


def test_suppression_delete_404():
    assert client.delete("/api/outreach/suppress/9182736").status_code == 404


# --------------------------------------------------------------------------
# adapter over the concurrently-written utils.linkedin_outreach
# --------------------------------------------------------------------------

def test_adapter_accepts_the_narrow_extract_contact_signature(monkeypatch):
    """`extract_contact(text, *, company=None)` is the minimum documented API.

    The router calls the richer keyword set first and falls back on TypeError, so
    whichever of the two entry points that module actually ships still works.
    """
    seen = {}

    def narrow(text, *, company=None):
        seen["company"] = company
        return {"address": "Careers@Acme.com", "source": "post_text",
                "channel": "email", "instructions": "Send your CV."}

    monkeypatch.setattr(om, "_lo_extract", narrow)
    out = om.extract_application_contact("text", company="Acme", job_title="Eng")
    assert out["address"] == "careers@acme.com"
    assert out["channel"] == "email"
    assert seen["company"] == "Acme"
    assert set(out) == set(om._BLANK_CONTACT)


def test_adapter_tolerates_none_and_screens_the_upstream_address(monkeypatch):
    """A None result, and an unsafe address the upstream module let through."""
    monkeypatch.setattr(om, "_lo_extract", lambda *a, **k: None)
    blank = om.extract_application_contact("text")
    assert blank["address"] is None and set(blank) == set(om._BLANK_CONTACT)

    monkeypatch.setattr(om, "_lo_extract",
                        lambda *a, **k: {"address": "legal@acme.com", "channel": "email"})
    screened = om.extract_application_contact("text")
    assert screened["address"] is None
    assert screened["rejected"] == [{"address": "legal@acme.com", "reason": "blocked_local"}]


def test_adapter_falls_back_when_the_dependency_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("half-written module")

    monkeypatch.setattr(om, "_lo_extract", boom)
    out = om.extract_application_contact(POST_WITH_ADDRESS, company="Acme Robotics")
    assert out["address"] == "careers@acme.com"      # regex fallback still delivers
