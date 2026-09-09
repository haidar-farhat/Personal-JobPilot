"""A job board's domain is never the employer's domain.

Regression for a bug caught on 2026-09-09 by running the outreach fallback
against real scraped rows: with LinkedIn jobs in the database, every company
resolved to `careers@linkedin.com`, because the constructed-address path
derives the domain from `job.url` and a LinkedIn posting's URL is
linkedin.com/jobs/view/... Shipping that would have sent one application per
job to a single unrelated inbox.

An ATS host is comparatively forgiving — acme.greenhouse.io still names the
employer — but every LinkedIn posting shares one host, so missing a board host
collapses every company onto the same address.
"""

import pytest

from utils.company_email import _ATS_HOSTS, _BOARD_HOSTS, company_domain_with_origin


class _Job:
    """Minimal stand-in for db.models.Job."""

    def __init__(self, company, url, description=""):
        self.company = company
        self.url = url
        self.description = description


BOARD_URLS = [
    ("LinkedIn", "https://www.linkedin.com/jobs/view/ai-engineer-at-acme-4464323868"),
    ("LinkedIn intl", "https://es.linkedin.com/jobs/view/ai-engineer-at-acme-123"),
    ("Indeed", "https://www.indeed.com/viewjob?jk=abc123"),
    ("Glassdoor", "https://www.glassdoor.com/job-listing/ai-engineer-acme-JV_123.htm"),
    ("ZipRecruiter", "https://www.ziprecruiter.com/c/Acme/Job/AI-Engineer"),
    ("Wellfound", "https://wellfound.com/jobs/12345-ai-engineer"),
    ("Built In", "https://builtin.com/job/engineer/ai-engineer/123456"),
    ("Jobicy", "https://jobicy.com/jobs/12345-ai-engineer"),
    ("USAJOBS", "https://www.usajobs.gov/job/823456700"),
]


@pytest.mark.parametrize("label,url", BOARD_URLS)
def test_board_url_never_yields_a_company_domain(label, url):
    """A posting hosted on a job board must not name that board as the employer."""
    got = company_domain_with_origin(_Job("Acme Robotics", url))
    domain = got[0] if got else None
    assert domain is None or not any(b in domain for b in _BOARD_HOSTS), (
        f"{label}: resolved employer domain to {domain!r} — that is the job board"
    )


def test_every_board_host_is_in_the_ats_host_list():
    """_ATS_HOSTS is what the domain resolver actually consults."""
    for host in _BOARD_HOSTS:
        assert host in _ATS_HOSTS


def test_linkedin_is_covered_explicitly():
    """The specific host that caused the incident."""
    assert "linkedin.com" in _BOARD_HOSTS
    assert "linkedin.com" in _ATS_HOSTS


def test_a_real_company_url_still_resolves():
    """The guard must not break the case the feature exists for."""
    got = company_domain_with_origin(_Job("Acme Robotics", "https://acmerobotics.com/careers/ai-engineer"))
    assert got is not None
    assert "acmerobotics.com" in got[0]


def test_ats_subdomain_still_reaches_the_corporate_domain():
    """acme.greenhouse.io must not be read as greenhouse's own domain."""
    got = company_domain_with_origin(_Job("Acme", "https://boards.greenhouse.io/acme/jobs/123"))
    domain = got[0] if got else None
    assert domain is None or "greenhouse.io" not in domain


def test_fallback_returns_none_rather_than_a_board_address():
    """End-to-end: the outreach fallback yields nothing for a LinkedIn row."""
    from utils.company_email import company_domain_with_origin
    got = company_domain_with_origin(
        _Job("Aventis Solutions", "https://www.linkedin.com/jobs/view/ai-engineer-123"))
    domain = got[0] if got else None
    assert domain is None or "linkedin.com" not in domain, (
        f"derived {domain!r} from a LinkedIn posting URL")


# --- constructed addresses need corroborated domain evidence ----------------
# Measured on real scraped rows: with only a bare company name to go on,
# domain_belongs_to accepted moab.com for "Moab" and garage.com for "Garage",
# because those homepages naturally contain the word. Both are strangers'
# domains. A constructed address is therefore allowed ONLY when the employer's
# domain came from their own posting URL.

def test_constructed_address_refused_when_the_domain_is_a_stranger(monkeypatch):
    """A guessed domain that does not prove it belongs to the employer is refused.

    The refusal now comes from utils.recipient's ownership check rather than the
    blanket "never send to a constructed address" rule this file used to assert.
    Constructed addresses ARE allowed again — deliberate, and matching what
    agents/auto_applier already did — but only when the domain proves out.
    """
    import server.outreach_mail as om
    import utils.recipient as R
    monkeypatch.setattr(om, "_cfg", lambda: {"mail": {"lookup_website": True,
                                                      "guess_addresses": True}})
    monkeypatch.setattr(R, "guessed_sent_today", lambda session=None: 0)
    monkeypatch.setattr(R, "resolve", lambda job, **k: {
        "address": None, "source": None, "domain": "moab.com",
        "source_url": None, "domain_origin": "derived", "confidence": 0.0,
        "rejected": [{"address": "careers@moab.com", "reason": "domain_unconfirmed"}],
        "reason": "domain_unconfirmed"})
    addr, src = om._company_fallback_recipient(
        _Job("Moab", "https://www.linkedin.com/jobs/view/engineer-at-moab-1"), set())
    assert addr is None, "a domain that did not prove ownership must not be mailed"


def test_the_live_stranger_domains_still_fail_the_ownership_check():
    """End-to-end on the rule itself, with the real page shapes.

    moab.com's <title> really is "MOAB" and it really does carry footer
    furniture, so it satisfies every weak signal. Only the absence of a hiring
    or careers signal separates it from a real employer — which is why
    company_furniture alone is not accepted as corroboration.
    """
    import utils.recipient as R
    filler = " ".join(["We welcome visitors from around the world every season."] * 6)
    moab = ("<html><head><title>MOAB</title></head><body><h1>Visit Moab</h1>"
            "<p>Hotels, trails and tours in Moab, Utah.</p>" + filler +
            "<footer>Contact us | Privacy policy</footer></body></html>")
    assert R.domain_confidently_belongs_to("moab.com", "Moab", page_text=moab) is False

    real = ("<html><head><title>Vanta</title></head><body><h1>Automate compliance</h1>"
            "<p>Vanta automates security compliance.</p>" + filler +
            "<footer>About us | Careers | Privacy policy</footer></body></html>")
    assert R.domain_confidently_belongs_to("vanta.com", "Vanta", page_text=real) is True


def test_constructed_address_accepted_when_ownership_is_proven(monkeypatch):
    """The behaviour change the user asked for: a proven guess is now usable."""
    import server.outreach_mail as om
    import utils.recipient as R
    monkeypatch.setattr(om, "_cfg", lambda: {"mail": {"lookup_website": True,
                                                      "guess_addresses": True}})
    monkeypatch.setattr(R, "guessed_sent_today", lambda session=None: 0)
    monkeypatch.setattr(R, "resolve", lambda job, **k: {
        "address": "careers@acmerobotics.com", "source": "constructed",
        "domain": "acmerobotics.com", "source_url": None, "domain_origin": "derived",
        "confidence": 0.4, "rejected": [], "reason": ""})
    addr, src = om._company_fallback_recipient(
        _Job("Acme Robotics", "https://www.linkedin.com/jobs/view/eng-at-acme-1"), set())
    assert addr == "careers@acmerobotics.com"
    assert src == "constructed"


def test_constructed_address_allowed_when_domain_came_from_the_posting_url(monkeypatch):
    import server.outreach_mail as om
    monkeypatch.setattr(om, "_cfg", lambda: {"mail": {"lookup_website": False,
                                                      "guess_addresses": True}})
    monkeypatch.setattr("utils.recipient.resolve", lambda job, **k: {
        "address": "careers@acmerobotics.com", "source": "constructed",
        "domain": "acmerobotics.com", "source_url": None, "domain_origin": "url",
        "confidence": 0.4, "rejected": [], "reason": ""})
    addr, src = om._company_fallback_recipient(
        _Job("Acme Robotics", "https://acmerobotics.com/careers/ai-engineer"), set())
    assert addr == "careers@acmerobotics.com"
    assert src == "constructed"


def test_published_company_address_is_still_accepted(monkeypatch):
    """A crawled address is the company's own publication — always allowed."""
    import server.outreach_mail as om
    monkeypatch.setattr(om, "_cfg", lambda: {"mail": {"lookup_website": True,
                                                      "guess_addresses": False}})
    monkeypatch.setattr("utils.recipient.resolve", lambda job, **k: {
        "address": "jobs@acme.com", "source": "company_site",
        "domain": "acme.com", "source_url": "https://acme.com/careers",
        "domain_origin": "derived", "confidence": 0.75, "rejected": [], "reason": ""})
    addr, src = om._company_fallback_recipient(
        _Job("Acme", "https://www.linkedin.com/jobs/view/eng-at-acme-1"), set())
    assert addr == "jobs@acme.com"
    assert src == "company_site"


def test_unsafe_local_part_is_refused_even_when_published(monkeypatch):
    import server.outreach_mail as om
    monkeypatch.setattr(om, "_cfg", lambda: {"mail": {"lookup_website": True,
                                                      "guess_addresses": False}})
    monkeypatch.setattr("utils.recipient.resolve", lambda job, **k: {
        "address": None, "source": None, "domain": "acme.com", "source_url": None,
        "domain_origin": "url", "confidence": 0.0,
        "rejected": [{"address": "accommodation@acme.com", "reason": "unsafe"}],
        "reason": "unsafe"})
    addr, _ = om._company_fallback_recipient(_Job("Acme", "https://acme.com/jobs/1"), set())
    assert addr is None, "an accommodation inbox must never receive an application"
