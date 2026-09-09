"""Unit tests for utils/linkedin_outreach.py.

Nothing here touches the network, IMAP, Ollama or the real jobpilot.db: the LLM
entry point and the IMAP helper are monkeypatched in every test, and an autouse
fixture makes the default LLM raise so an accidental live call fails loudly
instead of silently going out over the wire.

The heaviest coverage is deliberately on the safety screen. Returning an
accommodation / accessibility / legal / no-reply inbox as an application contact
is the one failure in this module that harms someone other than the user, so it
is tested against the whole BLOCKED_LOCAL table and against the LLM trying to
re-introduce a blocked address it saw in the post.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from db.models import OutreachSend, OutreachSuppression
from utils import bounce_watch, linkedin_outreach as lo
from utils.mailer import BLOCKED_LOCAL


# --------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _no_side_channels(monkeypatch):
    """No real mailbox, no real dead list, no real model."""
    monkeypatch.setattr(lo, "_own_address", lambda: "")
    monkeypatch.setattr(bounce_watch, "load_dead", lambda: set())

    import utils.ollama_client as oc

    def _boom(*a, **kw):
        raise RuntimeError("LLM must be mocked in tests")

    monkeypatch.setattr(oc, "generate_json", _boom)


@pytest.fixture()
def fake_llm(monkeypatch):
    """Install a canned generate_json payload; returns the call log."""
    import utils.ollama_client as oc
    calls = []

    def _install(payload):
        def _gen(prompt, system_prompt="", **kw):
            calls.append({"prompt": prompt, "system_prompt": system_prompt})
            if isinstance(payload, Exception):
                raise payload
            return payload
        monkeypatch.setattr(oc, "generate_json", _gen)
        return calls

    return _install


# --------------------------------------------------------------- de-mangling

MANGLED = [
    ("parenthesised at/dot", "Send your CV to careers (at) acme (dot) com by Friday."),
    ("spelled AT/DOT", "Send your CV to careers AT acme DOT com by Friday."),
    ("lowercase at/dot", "send your cv to careers at acme dot com"),
    ("square brackets", "Send your CV to careers[at]acme[.]com by Friday."),
    ("square dot word", "Send your CV to careers[at]acme[dot]com by Friday."),
    ("curly braces", "Send your CV to careers{at}acme{dot}com by Friday."),
    ("angle brackets", "Send your CV to careers <at> acme <dot> com by Friday."),
    ("hyphen separated", "Send your CV to careers-at-acme-dot-com by Friday."),
    ("spaced out", "Send your CV to careers @ acme . com by Friday."),
    ("space after at", "Send your CV to careers@ acme . com by Friday."),
    ("zero width", "Send your CV to care​ers@acme.com by Friday."),
    ("fullwidth at", "Send your CV to careers＠acme．com by Friday."),
    ("small at + ideographic dot", "Send your CV to careers﹫acme。com by Friday."),
    ("nbsp spacing", "Send your CV to careers @ acme . com by Friday."),
]


@pytest.mark.parametrize("label,text", MANGLED, ids=[m[0] for m in MANGLED])
def test_every_mangling_form_is_recovered(label, text):
    res = lo.extract_application_contact(text, company="Acme", dead=set(), use_llm=False)
    assert res["address"] == "careers@acme.com", label
    assert res["candidates"] == ["careers@acme.com"]
    assert res["channel"] == "email"


def test_obfuscated_flag_only_set_when_demangling_revealed_the_address():
    plain = lo.extract_application_contact("Mail careers@acme.com now.",
                                           dead=set(), use_llm=False)
    assert plain["obfuscated"] is False

    mangled = lo.extract_application_contact("Mail careers (at) acme (dot) com now.",
                                             dead=set(), use_llm=False)
    assert mangled["obfuscated"] is True


PROSE = [
    "We are hiring! Apply at acme.com for details.",
    "Our team is based at acme.io.",
    "Visit www.acme.com at acme.io to learn more.",
    "The dot com era is long over.",
    "Follow @acme . We are hiring across Europe.",
    "Ping @recruiter on LinkedIn. Great team.",
]


@pytest.mark.parametrize("text", PROSE)
def test_ordinary_prose_never_mints_an_address(text):
    res = lo.extract_application_contact(text, dead=set(), use_llm=False)
    assert res["address"] is None
    assert res["candidates"] == []


def test_spaced_collapse_requires_an_address_shaped_local_part():
    # "follow" is an English word: not collapsed. "john.doe" and "careers" are.
    assert "follow@acme.com" not in lo._deobfuscate("follow @ acme . com")
    assert "john.doe@acme.com" in lo._deobfuscate("john.doe @ acme . com")
    assert "careers@acme.com" in lo._deobfuscate("careers @ acme . com")


def test_spaced_collapse_stops_at_a_real_tld():
    # The sentence boundary must not be eaten into the domain.
    out = lo._deobfuscate("Write to hr @ acme . com . We reply fast.")
    assert "hr@acme.com" in out
    assert "acme.com.we" not in out.lower()


# --------------------------------------------------------------- safety screen

@pytest.mark.parametrize("local", sorted(set(BLOCKED_LOCAL)))
def test_blocked_local_parts_are_never_returned(local):
    text = f"Applicants may write to {local}@acme.com for assistance."
    res = lo.extract_application_contact(text, company="Acme", dead=set(), use_llm=False)
    assert res["address"] is None
    assert res["candidates"] == []
    assert res["rejected"] == [{"address": f"{local}@acme.com", "reason": "blocked_local"}]
    assert res["channel"] != "email"


@pytest.mark.parametrize("addr", [
    "accommodation@acme.com", "accessibility-requests@acme.com",
    "ada.compliance@acme.com", "legal@acme.com", "privacy@acme.com",
    "no-reply@acme.com", "noreply@acme.com", "donotreply@acme.com",
    "postmaster@acme.com", "unsubscribe@acme.com", "abuse@acme.com",
])
def test_accommodation_and_noreply_inboxes_are_rejected_however_they_are_found(addr):
    """Mangled, spaced or plain — a blocked inbox never survives the screen."""
    local, domain = addr.split("@")
    variants = [
        f"Contact {addr}.",
        f"Contact {local} (at) {domain.replace('.', ' (dot) ')}.",
        f"Contact {local} @ {domain.replace('.', ' . ')}.",
    ]
    for text in variants:
        res = lo.extract_application_contact(text, dead=set(), use_llm=False)
        assert res["address"] is None, text
        assert addr not in res["candidates"], text


def test_blocked_inbox_is_dropped_but_the_real_one_survives():
    text = ("Accommodation requests: accommodation@acme.com. "
            "To apply, send your CV to careers@acme.com.")
    res = lo.extract_application_contact(text, company="Acme", dead=set(), use_llm=False)
    assert res["address"] == "careers@acme.com"
    assert res["candidates"] == ["careers@acme.com"]
    assert {"address": "accommodation@acme.com", "reason": "blocked_local"} in res["rejected"]


def test_dead_address_is_rejected_with_its_own_reason():
    res = lo.extract_application_contact("Apply: careers@acme.com",
                                         dead={"careers@acme.com"}, use_llm=False)
    assert res["address"] is None
    assert res["rejected"] == [{"address": "careers@acme.com", "reason": "dead"}]


def test_dead_defaults_to_bounce_watch(monkeypatch):
    monkeypatch.setattr(bounce_watch, "load_dead", lambda: {"careers@acme.com"})
    res = lo.extract_application_contact("Apply: careers@acme.com", use_llm=False)
    assert res["address"] is None
    assert res["rejected"][0]["reason"] == "dead"


def test_corrupt_dead_list_is_logged_not_silently_ignored(monkeypatch, tmp_path, caplog):
    bad = tmp_path / "dead_addresses.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(bounce_watch, "_DEAD_PATH", bad)
    monkeypatch.setattr(bounce_watch, "load_dead", lambda: set())
    with caplog.at_level(logging.WARNING, logger="utils.linkedin_outreach"):
        res = lo.extract_application_contact("Apply: careers@acme.com", use_llm=False)
    assert res["address"] == "careers@acme.com"
    assert any("dead-address list" in r.message for r in caplog.records)


def test_own_address_is_rejected_as_self(monkeypatch):
    monkeypatch.setattr(lo, "_own_address", lambda: "me@example.com")
    res = lo.extract_application_contact("Reply to me@example.com or careers@acme.com",
                                         dead=set(), use_llm=False)
    assert res["address"] == "careers@acme.com"
    assert {"address": "me@example.com", "reason": "self"} in res["rejected"]


def test_image_filename_is_rejected_as_image_not_as_inbox():
    res = lo.extract_application_contact("logo: cdn@acme.com.png and jobs@acme.com",
                                         dead=set(), use_llm=False)
    assert res["address"] == "jobs@acme.com"
    assert {"address": "cdn@acme.com.png", "reason": "image"} in res["rejected"]


# --------------------------------------------------------------- ranking

def test_recruiting_local_part_outranks_a_personal_one():
    text = "Questions: dave@acme.com. Applications: careers@acme.com."
    res = lo.extract_application_contact(text, company="Acme", dead=set(), use_llm=False)
    assert res["candidates"][0] == "careers@acme.com"
    assert res["address"] == "careers@acme.com"


def test_company_domain_breaks_the_tie_between_two_plain_addresses():
    text = "Write to dave@gmail.com or to dave@acmerobotics.com."
    res = lo.extract_application_contact(text, company="Acme Robotics, Inc.",
                                         dead=set(), use_llm=False)
    assert res["address"] == "dave@acmerobotics.com"


def test_first_found_wins_when_nothing_else_separates_them():
    text = "Write to alpha@x.com or beta@y.com."
    res = lo.extract_application_contact(text, company="", dead=set(), use_llm=False)
    assert res["candidates"] == ["alpha@x.com", "beta@y.com"]


# --------------------------------------------------------------- channel

def test_channel_email_when_an_address_survives():
    res = lo.extract_application_contact("CV to careers@acme.com", dead=set(), use_llm=False)
    assert res["channel"] == "email"


def test_channel_easy_apply():
    res = lo.extract_application_contact("Use Easy Apply on the posting.",
                                         dead=set(), use_llm=False)
    assert res["channel"] == "linkedin_easy_apply"


def test_channel_external_link():
    res = lo.extract_application_contact("Apply via https://acme.com/careers today.",
                                         dead=set(), use_llm=False)
    assert res["channel"] == "external_link"


def test_channel_dm_when_only_a_dm_hint_is_present():
    res = lo.extract_application_contact("Interested? DM me and I'll share details.",
                                         dead=set(), use_llm=False)
    assert res["channel"] == "dm"


def test_channel_unknown_and_blank_shape():
    blank = lo.extract_application_contact("", use_llm=False)
    assert blank == {
        "address": None, "candidates": [], "rejected": [], "channel": "unknown",
        "instructions": "", "subject_hint": "", "deadline_hint": "",
        "confidence": 0.0, "source": None, "obfuscated": False,
    }
    assert lo.extract_application_contact(None, use_llm=False) == blank


def test_result_always_has_every_key():
    for text in ("", "nothing here", "careers@acme.com", "Easy Apply"):
        res = lo.extract_application_contact(text, dead=set(), use_llm=False)
        assert set(res) == set(lo._blank())


# --------------------------------------------------------------- instructions

def test_regex_instructions_take_the_address_sentence_and_the_next_one():
    text = ("We are hiring a DevOps Engineer. Send your CV to careers@acme.com. "
            "Use the subject line DevOps 2026. Unrelated closing line.")
    res = lo.extract_application_contact(text, dead=set(), use_llm=False)
    assert res["source"] == "regex"
    assert res["confidence"] == 0.55
    assert "careers@acme.com" in res["instructions"]
    assert "DevOps 2026" in res["instructions"]
    assert "Unrelated closing line" not in res["instructions"]
    assert len(res["instructions"]) <= 400


def test_regex_instructions_fall_back_to_the_hint_sentence_without_an_address():
    text = "Great role. Apply via our portal at the link. Thanks for reading."
    res = lo.extract_application_contact(text, dead=set(), use_llm=False)
    assert "Apply via our portal" in res["instructions"]


def test_llm_is_not_called_when_use_llm_is_false(fake_llm):
    calls = fake_llm({"channel": "email"})
    lo.extract_application_contact("Send your CV to careers@acme.com", dead=set(),
                                   use_llm=False)
    assert calls == []


def test_llm_refines_instructions_and_agrees_on_channel(fake_llm):
    calls = fake_llm({
        "channel": "email",
        "application_email": "careers@acme.com",
        "subject_line_required": "DevOps Engineer - 2026",
        "deadline": "30 September 2026",
        "instructions": "Email your CV to careers@acme.com with that subject line.",
    })
    res = lo.extract_application_contact("Send your CV to careers@acme.com.",
                                         dead=set(), use_llm=True)
    assert len(calls) == 1
    assert res["source"] == "regex+llm"
    assert res["confidence"] == 0.8
    assert res["subject_hint"] == "DevOps Engineer - 2026"
    assert res["deadline_hint"] == "30 September 2026"
    assert res["instructions"].startswith("Email your CV")
    assert res["address"] == "careers@acme.com"


def test_llm_disagreement_lowers_confidence_but_never_moves_the_address(fake_llm):
    fake_llm({"channel": "dm", "application_email": "", "instructions": "DM the poster."})
    res = lo.extract_application_contact("Send your CV to careers@acme.com.",
                                         dead=set(), use_llm=True)
    assert res["confidence"] == 0.6
    assert res["channel"] == "email"          # regex wins on the address
    assert res["address"] == "careers@acme.com"


def test_llm_channel_wins_only_when_regex_found_no_address(fake_llm):
    fake_llm({"channel": "dm", "instructions": "Message the poster directly."})
    res = lo.extract_application_contact("Interested candidates should reach out to me.",
                                         dead=set(), use_llm=True)
    assert res["channel"] == "dm"


def test_llm_unavailable_degrades_to_the_regex_result(fake_llm):
    fake_llm(RuntimeError("ollama is not running"))
    res = lo.extract_application_contact("Send your CV to careers@acme.com.",
                                         dead=set(), use_llm=True)
    assert res["source"] == "regex"
    assert res["confidence"] == 0.55
    assert res["address"] == "careers@acme.com"
    assert "careers@acme.com" in res["instructions"]


@pytest.mark.parametrize("payload", [None, "a string", [1, 2], 7])
def test_llm_garbage_is_ignored(fake_llm, payload):
    fake_llm(payload)
    res = lo.extract_application_contact("Send your CV to careers@acme.com.",
                                         dead=set(), use_llm=True)
    assert res["source"] == "regex"
    assert res["address"] == "careers@acme.com"


def test_llm_cannot_mint_an_address_that_is_not_in_the_post(caplog):
    with caplog.at_level(logging.WARNING, logger="utils.linkedin_outreach"):
        out = lo._normalize_instructions(
            {"channel": "email", "application_email": "ceo@acme.com"},
            allowed={"careers@acme.com"})
    assert out["application_email"] == ""
    assert any("discarded LLM recipient" in r.message for r in caplog.records)


def test_llm_cannot_resurrect_a_blocked_inbox_it_saw_in_the_post(fake_llm):
    """The single most important behaviour: a blocked inbox is not `allowed`."""
    fake_llm({"channel": "email", "application_email": "accommodation@acme.com",
              "instructions": "Email accommodation@acme.com."})
    text = "Accessibility questions to accommodation@acme.com. No other contact given."
    res = lo.extract_application_contact(text, dead=set(), use_llm=True)
    assert res["address"] is None
    assert "accommodation@acme.com" not in res["candidates"]


def test_normalize_instructions_clamps_channel_and_lengths():
    out = lo._normalize_instructions(
        {"channel": "carrier pigeon", "application_email": "",
         "instructions": "x" * 900, "subject_line_required": "y" * 500,
         "deadline": "z" * 500},
        allowed=set())
    assert out["channel"] == "unknown"
    assert len(out["instructions"]) == 400
    assert len(out["subject_hint"]) == 200
    assert len(out["deadline_hint"]) == 120


def test_normalize_instructions_rejects_non_dict():
    assert lo._normalize_instructions(["not", "a", "dict"], allowed=set()) is None


def test_extraction_never_raises(monkeypatch):
    monkeypatch.setattr(lo, "_deobfuscate", lambda t: (_ for _ in ()).throw(ValueError("boom")))
    res = lo.extract_application_contact("Send CV to careers@acme.com", use_llm=False)
    assert res == lo._blank()


# --------------------------------------------------------------- dedup key

def test_dedup_key_is_stable_and_32_hex():
    a = lo.outreach_dedup_key("Acme Robotics", "Senior Python Engineer")
    b = lo.outreach_dedup_key("Acme Robotics", "Senior Python Engineer")
    assert a == b
    assert len(a) == 32
    assert all(c in "0123456789abcdef" for c in a)


@pytest.mark.parametrize("company", [
    "Acme Robotics", "acme robotics", "  ACME   Robotics  ", "Acme Robotics, Inc.",
    "Acme Robotics Inc", "Acme Robotics LLC", "Acme Robotics Ltd.",
    "Acme Robotics GmbH", "Acme-Robotics!", "The Acme Robotics Company",
])
def test_company_variants_collapse_to_one_key(company):
    expected = lo.outreach_dedup_key("Acme Robotics", "Senior Python Engineer")
    assert lo.outreach_dedup_key(company, "Senior Python Engineer") == expected


@pytest.mark.parametrize("title", [
    "Senior Python Engineer", "senior python engineer", "Senior  Python  Engineer",
    "Senior Python Engineer!", "Senior/Python Engineer",
])
def test_title_variants_collapse_to_one_key(title):
    expected = lo.outreach_dedup_key("Acme", "Senior Python Engineer")
    assert lo.outreach_dedup_key("Acme", title) == expected


def test_dedup_key_separates_different_jobs_and_companies():
    keys = {
        lo.outreach_dedup_key("Acme", "Senior Python Engineer"),
        lo.outreach_dedup_key("Acme", "Junior Python Engineer"),
        lo.outreach_dedup_key("Beta", "Senior Python Engineer"),
        lo.outreach_dedup_key("Acme", "Senior Python Engineer", "4385163817"),
        lo.outreach_dedup_key("Acme", "Senior Python Engineer", "4385163818"),
    }
    assert len(keys) == 5


def test_dedup_key_field_separator_cannot_be_smuggled():
    """"a|b" as a company must not collide with company "a", title "b"."""
    assert lo.outreach_dedup_key("a|b", "") != lo.outreach_dedup_key("a", "b")


def test_dedup_key_external_id_is_case_and_space_insensitive():
    assert (lo.outreach_dedup_key("Acme", "Dev", " AB-12 ")
            == lo.outreach_dedup_key("Acme", "Dev", "ab-12"))


def test_dedup_key_tolerates_empty_input():
    assert len(lo.outreach_dedup_key("", "")) == 32


def test_normalize_company_key_folds_accents_and_suffixes():
    assert lo.normalize_company_key("Sagué Technologies GmbH") == "sague"
    assert lo.normalize_company_key("GmbH") == "gmbh"       # never strips the whole name


# --------------------------------------------------------------- composition

def test_subject_uses_the_posts_required_subject_verbatim():
    contact = {"subject_hint": "DevOps Engineer - 2026"}
    assert lo.subject_for("DevOps Engineer", "Acme", "Haydar", contact) == "DevOps Engineer - 2026"


def test_subject_falls_back_to_title_company_sender():
    subj = lo.subject_for("DevOps Engineer", "Acme", "Haydar", {})
    assert "DevOps Engineer" in subj and "Acme" in subj and "Haydar" in subj


def test_body_names_where_the_job_was_seen_and_carries_a_real_opt_out():
    body = lo.build_outreach_body(
        sender_name="Haydar", job_title="DevOps Engineer", company="Acme",
        contact={"subject_hint": "DevOps 2026"},
        links={"linkedin": "https://li/x", "github": "https://gh/x"},
        post_url="https://www.linkedin.com/posts/acme-1",
        cover_opening="I have run Kubernetes platforms for four years.")
    assert "I saw your post about the DevOps Engineer role at Acme" in body
    assert "https://www.linkedin.com/posts/acme-1" in body
    assert "I have run Kubernetes platforms" in body
    assert "DevOps 2026" in body
    assert "resume and cover letter" in body
    assert "reply STOP" in body and "Acme" in body
    assert "https://li/x" in body and "https://gh/x" in body
    assert "Haydar" in body


def test_body_without_a_tailored_opening_makes_no_claim_about_the_sender():
    body = lo.build_outreach_body(sender_name="Haydar", job_title="DevOps Engineer",
                                  company="Acme", contact={})
    assert "AI engineer" not in body            # mailer.default_body's fixed claim
    assert "years" not in body
    assert "reply STOP" in body


def test_body_opt_out_line_can_be_overridden():
    body = lo.build_outreach_body(sender_name="H", job_title="T", company="C",
                                  contact={}, opt_out_line="Reply STOP to opt out.")
    assert "Reply STOP to opt out." in body


def test_bodies_for_two_jobs_are_not_byte_identical():
    a = lo.build_outreach_body(sender_name="H", job_title="DevOps Engineer",
                               company="Acme", contact={},
                               cover_opening="Kubernetes platform work.")
    b = lo.build_outreach_body(sender_name="H", job_title="Data Engineer",
                               company="Beta", contact={},
                               cover_opening="Airflow and dbt pipelines.")
    assert a != b


# --------------------------------------------------------------- opt-outs

OPTOUT_YES = ["STOP", "stop", "Stop.", "please unsubscribe", "Unsubscribe me",
              "remove me from your list", "do not contact me", "opt out",
              "opt-out", "OPT OUT", "don't contact me again", "no more emails"]
OPTOUT_NO = ["stopwatch", "nonstop", "a nonstop flight", "stopwatches",
             "unsubscribed?", "stopgap", "we will not stopover",
             "thanks, sounds great", "optout123", "123stop"]


@pytest.mark.parametrize("text", OPTOUT_YES)
def test_optout_phrases_match(text):
    assert lo.is_optout(text, "") is True
    assert lo.is_optout("", text) is True


@pytest.mark.parametrize("text", OPTOUT_NO)
def test_word_boundaries_keep_stopwatch_and_nonstop_out(text):
    assert lo.is_optout(text, "") is False
    assert lo.is_optout("", text) is False


def test_optout_body_match_is_limited_to_the_first_300_chars():
    body = ("thanks for reaching out, we will review " * 12) + " stop"
    assert len(body) > 300
    assert lo.is_optout("", body) is False


# --- scan_optouts -------------------------------------------------------

@pytest.fixture()
def wired_mailbox(monkeypatch, tmp_engine):
    """Point scan_optouts at a fake INBOX and an isolated SQLite engine."""
    import agents.email_reader as er
    import db.database as dbmod

    Session = sessionmaker(bind=tmp_engine)
    monkeypatch.setattr(dbmod, "get_session", lambda: Session())
    monkeypatch.setattr(er, "load_gmail_config",
                        lambda: {"address": "me@example.com", "app_password": "pw",
                                 "enabled": True})

    def _install(messages):
        seen = {}

        def _recent(since, limit=25, conn=None, prefilter=None):
            seen["since"] = since
            seen["limit"] = limit
            if isinstance(messages, Exception):
                raise messages
            return messages

        monkeypatch.setattr(er, "recent_messages", _recent)
        return Session, seen

    return _install


def _msg(frm, subject, text=""):
    return {"uid": "1", "from": frm, "subject": subject, "text": text,
            "received_at": None, "links": []}


def test_scan_optouts_writes_an_address_suppression(wired_mailbox):
    Session, _ = wired_mailbox([_msg("Dana <dana@acme.com>", "STOP")])
    out = lo.scan_optouts()
    assert out["ok"] is True
    assert out["scanned"] == 1
    assert out["suppressed"] == ["dana@acme.com"]

    s = Session()
    rows = s.query(OutreachSuppression).all()
    assert [(r.value, r.scope, r.source) for r in rows] == [
        ("dana@acme.com", "address", "reply_scan")]
    assert rows[0].reason.startswith("reply: ")
    s.close()


def test_scan_optouts_also_suppresses_the_domain_when_we_mailed_that_company(wired_mailbox):
    Session, _ = wired_mailbox([_msg("Dana <dana@acme.com>", "please unsubscribe")])
    seed = Session()
    seed.add(OutreachSend(dedup_key="k1", company="Acme", company_normalized="acme",
                          job_title="Dev", recipient="dana@acme.com",
                          recipient_source="post_text", status="sent"))
    seed.commit()
    seed.close()

    out = lo.scan_optouts()
    assert out["ok"] is True
    assert set(out["suppressed"]) == {"dana@acme.com", "acme.com"}

    s = Session()
    scopes = {(r.value, r.scope) for r in s.query(OutreachSuppression).all()}
    assert scopes == {("dana@acme.com", "address"), ("acme.com", "domain")}
    s.close()


def test_scan_optouts_is_idempotent(wired_mailbox):
    Session, _ = wired_mailbox([_msg("Dana <dana@acme.com>", "STOP")])
    assert lo.scan_optouts()["new_suppressions"] == 1
    second = lo.scan_optouts()
    assert second["ok"] is True
    assert second["new_suppressions"] == 0
    s = Session()
    assert s.query(OutreachSuppression).count() == 1
    s.close()


def test_scan_optouts_ignores_non_optout_replies_and_our_own_mail(wired_mailbox):
    Session, _ = wired_mailbox([
        _msg("Dana <dana@acme.com>", "Re: your application", "Thanks, we will review."),
        _msg("Eve <eve@acme.com>", "nonstop hiring", "we are hiring nonstop"),
        _msg("Me <me@example.com>", "STOP"),
        _msg("No Address", "STOP"),
    ])
    out = lo.scan_optouts()
    assert out["ok"] is True
    assert out["scanned"] == 4
    assert out["suppressed"] == []
    s = Session()
    assert s.query(OutreachSuppression).count() == 0
    s.close()


def test_scan_optouts_fails_closed_when_the_mailbox_is_unreadable(wired_mailbox):
    Session, _ = wired_mailbox(OSError("imap.gmail.com unreachable"))
    out = lo.scan_optouts()
    assert out["ok"] is False              # "we do not know", not "nobody opted out"
    assert out["new_suppressions"] == 0
    assert "unreachable" in out["error"]


def test_scan_optouts_fails_closed_when_gmail_is_not_configured(monkeypatch, tmp_engine):
    import agents.email_reader as er
    monkeypatch.setattr(er, "load_gmail_config",
                        lambda: {"address": "", "app_password": "", "enabled": True})
    out = lo.scan_optouts()
    assert out["ok"] is False
    assert out["error"] == "gmail not configured"


def test_scan_optouts_fails_closed_when_the_database_write_fails(monkeypatch, wired_mailbox):
    wired_mailbox([_msg("Dana <dana@acme.com>", "STOP")])
    monkeypatch.setattr(lo, "_write_suppressions",
                        lambda pairs: (_ for _ in ()).throw(RuntimeError("db is locked")))
    out = lo.scan_optouts()
    assert out["ok"] is False
    assert "db is locked" in out["error"]


def test_scan_optouts_reads_the_requested_window(wired_mailbox):
    _, seen = wired_mailbox([])
    before = datetime.now(timezone.utc) - timedelta(hours=24)
    out = lo.scan_optouts(hours=24, limit=5)
    assert out["ok"] is True
    assert seen["limit"] == 5
    assert abs((seen["since"] - before).total_seconds()) < 60
