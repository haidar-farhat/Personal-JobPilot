"""agents/email_reader — pure parsing/classification, no mailbox, no network."""

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from agents import email_reader as er


def _msg(subject, text, sender="no-reply@myworkday.com", uid="1", ago_min=1, html=None, links=None):
    when = datetime.now(timezone.utc) - timedelta(minutes=ago_min)
    return {"uid": uid, "from": sender, "subject": subject, "received_at": when,
            "text": text, "links": links or []}


# ---------------------------------------------------------------- codes

def test_code_in_subject_wins():
    assert er.extract_code("482913 is your Workday verification code", "Use code 111111 later") == "482913"


def test_code_needs_a_code_word_nearby():
    assert er.extract_code("Your order", "Order number 88213 shipped, 5 items") is None
    assert er.extract_code("Verify your email", "Enter this code to continue: 55019") == "55019"


def test_years_and_phones_are_not_codes():
    assert er.extract_code("Verification", "© 2026 Acme. Call 555-123-4567 for a code") is None


def test_verification_link_but_never_unsubscribe():
    links = ["https://acme.com/unsubscribe?token=1", "https://ats.acme.com/verify?token=abc"]
    assert er.extract_link(links) == "https://ats.acme.com/verify?token=abc"


def test_find_verification_prefers_the_hinted_ats():
    msgs = [
        _msg("Your Netflix code", "Your sign-in code is 999999", sender="info@netflix.com", uid="9"),
        _msg("Activate your account", "Your verification code is 123456", sender="acme@myworkday.com", uid="8", ago_min=3),
    ]
    hit = er.find_verification(datetime.now(timezone.utc) - timedelta(minutes=10),
                               hint="acme.wd5.myworkdayjobs.com", messages=msgs)
    assert hit["code"] == "123456" and hit["uid"] == "8"


def test_find_verification_returns_link_when_no_code():
    msgs = [_msg("Confirm your email address", "Click to confirm your account",
                 links=["https://jobs.acme.com/confirm?t=xyz"])]
    hit = er.find_verification(datetime.now(timezone.utc) - timedelta(minutes=10), messages=msgs)
    assert hit["code"] is None and hit["link"].endswith("confirm?t=xyz")


def test_find_verification_ignores_unrelated_mail():
    msgs = [_msg("Weekly digest", "12345 people read this", sender="news@x.com")]
    assert er.find_verification(datetime.now(timezone.utc) - timedelta(minutes=10), messages=msgs) is None


def test_parse_message_reads_html_body_and_links():
    m = EmailMessage()
    m["From"] = "Acme Careers <careers@acme.com>"
    m["Subject"] = "Verify your Acme account"
    m["Date"] = "Wed, 02 Sep 2026 10:00:00 +0000"
    m.set_content("plain fallback")
    m.add_alternative('<p>Your code is <b>771122</b>.</p><a href="https://acme.com/verify?x=1">Verify</a>', subtype="html")
    p = er.parse_message(m.as_bytes(), uid="42")
    assert p["uid"] == "42" and "771122" in p["text"]
    assert p["links"] == ["https://acme.com/verify?x=1"]
    assert p["received_at"].tzinfo is not None


# ---------------------------------------------------------------- tracking

def test_classify_rejection_beats_thank_you():
    assert er.classify("Update on your application",
                       "Thank you for applying. Unfortunately we will not be moving forward.") == "rejection"


def test_classify_confirmation_even_when_it_mentions_interview():
    assert er.classify("Thank you for applying to Acme",
                       "We received your application. If selected for an interview we'll reach out.") == "confirmation"


def test_classify_interview_invite():
    assert er.classify("Next steps with Acme", "We'd like to schedule a call — share your availability.") == "interview"
    assert er.classify("Data Engineer role", "We would like to invite you to an interview next week.") == "interview"


def test_classify_other_mail_is_ignored():
    assert er.classify("Your receipt", "Thanks for your purchase") is None


def test_company_matching_by_subject_or_domain():
    assert er.header_matches_company("no-reply@greenhouse.io", "Thank you for applying to Acme Robotics Inc", "Acme Robotics, Inc.")
    assert er.header_matches_company("talent@acmerobotics.com", "Next steps", "Acme Robotics")
    assert not er.header_matches_company("news@other.com", "Hello", "Acme Robotics")


def test_propose_builds_review_rows_and_never_steps_backwards():
    apps = [
        {"app_id": 1, "company": "Acme Robotics", "title": "AI Engineer", "status": "applied"},
        {"app_id": 2, "company": "Acme Robotics", "title": "Data Analyst", "status": "interview"},
        {"app_id": 3, "company": "Globex", "title": "ML Engineer", "status": "queued"},
    ]
    msgs = [
        _msg("Interview: AI Engineer at Acme Robotics", "Share your availability for a call", sender="hr@acmerobotics.com", uid="a"),
        _msg("Thank you for applying to Acme Robotics", "Data Analyst application received", sender="no-reply@greenhouse.io", uid="b"),
        _msg("Your application to Globex", "Unfortunately we will not be moving forward.", sender="jobs@globex.com", uid="c"),
        _msg("Your application to Globex", "Unfortunately we will not be moving forward.", sender="jobs@globex.com", uid="seen"),
    ]
    rows = er.propose(msgs, apps, seen_uids={"seen"})
    by_uid = {r["uid"]: r for r in rows}
    assert by_uid["a"]["app_id"] == 1 and by_uid["a"]["proposed_status"] == "interview"
    assert "b" not in by_uid            # confirmation for an app already at interview = backwards
    assert by_uid["c"]["app_id"] == 3 and by_uid["c"]["proposed_status"] == "rejected"
    assert "seen" not in by_uid         # idempotent on already-applied uids


def test_masked_address():
    assert er.masked_address("matthew@gmail.com") == "ma…@gmail.com"
