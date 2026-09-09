"""Tests for utils/recipient.py — the shared recipient resolver.

The ownership table below is the point of the exercise. Its companies are real
rows from the `jobs` table of jobpilot.db (Moab, Garage, Cape, Air, Campfire,
Anthropic, Datadog, Doximity, Ramp, Hex, Vanta, Axon, Notion, Salt, Brex,
Remote, Lyft, Scale AI, Xcel Energy, Aventis Solutions), and the page text is
RECORDED/SYNTHETIC — written to match what each kind of site actually looks
like. Nothing in this file touches the network, SMTP, IMAP or the real database.

`test_ownership_table_beats_the_old_rule` re-implements the OLD
utils.company_email.domain_belongs_to check over the same pages and asserts the
new rule is strictly better, so a future "simplification" back to a plain
substring test fails here instead of in someone's inbox.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from utils import recipient as R


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _Job:
    """The three attributes the resolver reads off a db.models.Job."""

    def __init__(self, description: str = "", company: str = "", url: str = ""):
        self.description = description
        self.company = company
        self.url = url


def _page(title: str, body: str, *, og_site: str = "") -> str:
    og = f'<meta property="og:site_name" content="{og_site}">' if og_site else ""
    return f"<html><head><title>{title}</title>{og}</head><body>{body}</body></html>"


def _filler(topic: str) -> str:
    """Enough ordinary prose that a page is not rejected as too thin."""
    return (f"{topic} " * 8) + (
        "Read more about what we do, browse the latest updates, and find "
        "answers to common questions in our help centre. Subscribe for news. "
        "This page uses cookies to improve your experience. ")


@pytest.fixture(autouse=True)
def _clear_cache():
    R.clear_ownership_cache()
    yield
    R.clear_ownership_cache()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing in this file may reach the network. Every real entry point that
    would is replaced with a loud failure; a test that needs one stubs it."""
    import utils.company_email as ce
    monkeypatch.setattr(ce, "find_company_email", lambda job, **kw: pytest.fail(
        "unstubbed utils.company_email.find_company_email — this would hit the network"))
    monkeypatch.setattr(R, "_fetch_homepage", lambda d: pytest.fail(
        f"unstubbed homepage fetch of {d} — this would hit the network"))


# ---------------------------------------------------------------------------
# recorded pages
# ---------------------------------------------------------------------------

# --- the measured false positives -----------------------------------------
MOAB_TRAVEL = _page(
    "Moab, Utah | Official Travel Guide",
    "<h1>Welcome to Moab</h1><p>About Moab: red rock country, gateway to "
    "Arches and Canyonlands National Parks. Plan your trip to Moab with "
    "lodging, guided tours, permits and trail maps. Moab is best visited in "
    "spring and autumn.</p>" + _filler("Moab hiking and mountain biking") +
    "<footer>Contact us | Privacy policy</footer>")

GARAGE_STRANGER = _page(
    "Garage.com — parking, storage and workshop space",
    "<h1>Garage</h1><p>Rent a garage near you. Garage listings for parking, "
    "storage and workshop space in every major city. List your garage and "
    "start earning.</p>" + _filler("garage rentals and storage units") +
    "<footer>Terms of use | Privacy policy</footer>")

CAPE_PARKED = _page(
    "cape.com",
    "<h1>cape.com</h1><p>This domain may be for sale. Buy this domain and "
    "make an offer through our brokerage. Interested in this domain? Our "
    "domain experts will contact you shortly.</p>" + _filler("premium domain names"))

AIR_PLACEHOLDER = _page(
    "air.com", "<h1>Coming soon</h1><p>Stay tuned.</p>")

CAMPFIRE_BLOG = _page(
    "Notes from the trail",
    "<h1>Campfire</h1><p>How to build a campfire that lasts all night. A good "
    "campfire needs tinder, kindling and patience. Campfire cooking recipes, "
    "campfire songs and stories from ten years of backcountry trips.</p>"
    + _filler("campfire safety and fire bans") +
    "<footer>&copy; 2026 | Privacy policy</footer>")

SCALEAI_STRANGER = _page(
    "ScaleUp Marketing — growth consulting",
    "<h1>Grow at scale</h1><p>We help founders scale AI adoption across their "
    "marketing stack. Scale AI experiments, measure, repeat.</p>"
    + _filler("growth marketing and demand generation") +
    "<footer>Terms of service | Privacy policy</footer>")

JOBBOARD_STRANGER = _page(
    "job-board.com — post a job",
    "<h1>Job board software</h1><p>Launch a job board in minutes. We are "
    "hiring too — see our open positions.</p>" + _filler("job board software") +
    "<footer>Contact us | Privacy policy</footer>")

SALT_REGISTRAR = _page(
    "salt.com",
    "<p>Register your domain today. This domain is for sale. Domain parking "
    "provided by your registrar.</p>" + _filler("domain registrar services"))

# --- real employers --------------------------------------------------------
ANTHROPIC = _page(
    "Anthropic",
    "<h1>AI research and products that put safety at the frontier</h1>"
    "<p>Anthropic is an AI safety and research company. Claude is built by "
    "Anthropic.</p>" + _filler("AI safety research") +
    "<footer>Careers &middot; Privacy policy &middot; Contact us</footer>")

DATADOG = _page(
    "Datadog | Cloud Monitoring as a Service",
    "<h1>See inside any stack</h1><p>Modern monitoring and security.</p>"
    + _filler("infrastructure monitoring and APM") +
    "<footer>About us | Privacy policy | Open positions</footer>")

DOXIMITY = _page(
    "Doximity - Medical Network",
    "<h1>The medical network</h1><p>Doximity is the leading digital platform "
    "for U.S. medical professionals.</p>" + _filler("clinician network") +
    "<footer>Privacy policy | Contact us</footer>")

RAMP = _page(
    "Ramp - The finance platform designed to save time and money",
    "<h1>Spend less time and money</h1><p>Corporate cards, bill payments and "
    "accounting automation.</p>" + _filler("corporate spend management") +
    "<footer>Careers | Privacy policy | Contact us</footer>")

HEX = _page(
    "The data workspace",
    "<h1>Do more with data</h1><p>Notebooks, apps and agents for analytics "
    "teams. Life at Hex: we are a small team shipping fast.</p>"
    + _filler("collaborative analytics notebooks") +
    "<footer>Open roles | Privacy policy | Contact us</footer>",
    og_site="Hex")

# Footer carries a Careers link, as every real company homepage does. Without
# one, owning the title is not evidence of being an EMPLOYER — the live
# moab.com owns its title too and is a tourism site.
VANTA = _page(
    "Vanta | Automated Security and Compliance",
    "<h1>Automate compliance</h1><p>SOC 2, ISO 27001 and HIPAA on autopilot.</p>"
    + _filler("continuous compliance monitoring") +
    "<footer>About us | Careers | Privacy policy</footer>")

AXON = _page(
    "Public Safety Technology &amp; Solutions",
    "<h1>Protect life</h1><p>Body cameras, in-car systems and digital "
    "evidence management. Careers at Axon: join our team and help us make the "
    "bullet obsolete.</p>" + _filler("public safety technology") +
    "<footer>Investors | Privacy policy | Contact us</footer>")

NOTION = _page(
    "Notion – The AI workspace that works for you",
    "<h1>One workspace. Every team.</h1><p>Write, plan and organise.</p>"
    + _filler("docs wikis and projects") +
    "<footer>Careers | Privacy policy | Contact us</footer>")

REMOTE = _page(
    "Remote | Global HR Platform for International Teams",
    "<h1>Hire anywhere</h1><p>Payroll, benefits and compliance in 180 "
    "countries.</p>" + _filler("employer of record services") +
    "<footer>About us | Privacy policy | Open positions</footer>")

BREX = _page(
    "Brex - The AI-powered spend platform",
    "<h1>Spend smarter</h1><p>Corporate cards and expense management for "
    "startups.</p>" + _filler("startup banking and cards") +
    "<footer>Careers | Privacy policy | Contact us</footer>")

XCEL = _page(
    "Home",
    "<h1>Powering millions of homes</h1><p>Xcel Energy provides electricity "
    "and natural gas across eight states.</p>" + _filler("utility services") +
    "<footer>Contact us | Privacy policy</footer>")

AVENTIS = _page(
    "Home",
    "<h1>Consulting that ships</h1><p>Aventis Solutions builds data platforms "
    "for mid-market manufacturers.</p>" + _filler("data engineering consultancy") +
    "<footer>Contact us | Privacy policy</footer>")

ACME_ROBOTICS = _page(
    "Home",
    "<h1>Autonomous handling</h1><p>Acme Robotics designs warehouse picking "
    "arms.</p>" + _filler("warehouse automation") +
    "<footer>Contact us | Privacy policy</footer>")


# (domain, company, expected, page) — 20 cases.
OWNERSHIP_TABLE = [
    # measured false positives of the old rule — all strangers
    ("moab.com", "Moab", False, MOAB_TRAVEL),
    ("garage.com", "Garage", False, GARAGE_STRANGER),
    ("cape.com", "Cape", False, CAPE_PARKED),
    ("air.com", "Air", False, AIR_PLACEHOLDER),
    ("campfire.com", "Campfire", False, CAMPFIRE_BLOG),
    ("scaleai.co", "Scale AI", False, SCALEAI_STRANGER),
    ("job-board.com", "Lyft", False, JOBBOARD_STRANGER),
    ("salt.com", "Salt", False, SALT_REGISTRAR),
    # distinctive names — a plain mention is enough
    ("anthropic.com", "Anthropic", True, ANTHROPIC),
    ("doximity.com", "Doximity", True, DOXIMITY),
    ("xcelenergy.com", "Xcel Energy", True, XCEL),
    ("aventissolutions.com", "Aventis Solutions", True, AVENTIS),
    ("acmerobotics.com", "Acme Robotics", True, ACME_ROBOTICS),
    # short names on their REAL sites — must still pass
    ("datadoghq.com", "Datadog", True, DATADOG),
    ("ramp.com", "Ramp", True, RAMP),
    ("hex.tech", "Hex", True, HEX),
    ("vanta.com", "Vanta", True, VANTA),
    ("axon.com", "Axon", True, AXON),
    ("notion.so", "Notion", True, NOTION),
    ("brex.com", "Brex", True, BREX),
]


def _old_domain_belongs_to(page_html: str, company: str) -> bool:
    """utils.company_email.domain_belongs_to's rule, offline.

    Verbatim: tokens longer than two characters, first two must both appear
    anywhere in the lowercased homepage HTML.
    """
    tokens = [t for t in re.split(r"[^a-z0-9]+", (company or "").lower()) if len(t) > 2]
    if not tokens:
        return False
    blob = page_html[:60000].lower()
    return all(t in blob for t in tokens[:2])


# ---------------------------------------------------------------------------
# the ownership rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("domain,company,expected,page",
                         OWNERSHIP_TABLE,
                         ids=[f"{d}~{c}" for d, c, _, _ in OWNERSHIP_TABLE])
def test_ownership_table(domain, company, expected, page):
    got = R.domain_confidently_belongs_to(domain, company, page_text=page)
    assert got is expected, (
        f"{domain} / {company!r}: expected {expected}, got {got} "
        f"({R.ownership_reason(domain, company, page)})")


def test_ownership_table_beats_the_old_rule():
    """The new rule must score better than domain_belongs_to on the same table."""
    new_ok = sum(1 for d, c, exp, p in OWNERSHIP_TABLE
                 if R.domain_confidently_belongs_to(d, c, page_text=p) is exp)
    old_ok = sum(1 for d, c, exp, p in OWNERSHIP_TABLE
                 if _old_domain_belongs_to(p, c) is exp)
    assert new_ok == len(OWNERSHIP_TABLE)
    assert old_ok < new_ok, "old rule unexpectedly matched — table lost its teeth"


def test_the_two_measured_false_positives_specifically():
    """moab.com/Moab and garage.com/Garage: True under the old rule, False now."""
    assert _old_domain_belongs_to(MOAB_TRAVEL, "Moab") is True
    assert _old_domain_belongs_to(GARAGE_STRANGER, "Garage") is True
    assert R.domain_confidently_belongs_to("moab.com", "Moab", page_text=MOAB_TRAVEL) is False
    assert R.domain_confidently_belongs_to("garage.com", "Garage",
                                           page_text=GARAGE_STRANGER) is False


@pytest.mark.parametrize("marker", [
    "This domain is for sale.",
    "Buy this domain — brokerage by HugeDomains.",
    "Domain parking provided by ParkingCrew.",
    "Welcome to nginx!",
    "Apache2 Ubuntu Default Page",
    "Register your domain with our registrar.",
])
def test_parked_and_placeholder_pages_never_pass(marker):
    """Short .com names are exactly the ones that are parked or squatted."""
    page = _page("Ramp", f"<h1>Ramp</h1><p>{marker}</p>" + _filler("ramp") +
                 "<footer>Careers at Ramp | Privacy policy | Contact us</footer>")
    assert R.domain_confidently_belongs_to("ramp.com", "Ramp", page_text=page) is False
    assert R.ownership_reason("ramp.com", "Ramp", page) == "parked_or_placeholder"


def test_a_distinctive_name_still_passes_on_a_plain_mention():
    page = _page("Home", "<p>Aventis Solutions is hiring nobody in particular, "
                         "but this is our site.</p>" + _filler("consulting"))
    assert R.domain_confidently_belongs_to("aventissolutions.com",
                                           "Aventis Solutions", page_text=page) is True
    assert R.ownership_reason("aventissolutions.com", "Aventis Solutions",
                              page) == "distinctive_name_mentioned"


def test_a_short_name_needs_more_than_a_mention():
    """Same page shape, only the name changes — the mention alone is worthless."""
    body = "<p>Cape cod, cape town, a cape of good hope.</p>" + _filler("capes")
    weak = _page("Coastal photography", body)
    assert R.domain_confidently_belongs_to("cape.com", "Cape", page_text=weak) is False
    assert R.ownership_reason("cape.com", "Cape", weak) == \
        "generic_name_without_ownership_signal"

    strong = _page("Coastal photography", body +
                   "<p>Careers at Cape: we are hiring.</p>")
    assert R.domain_confidently_belongs_to("cape.com", "Cape", page_text=strong) is True
    assert R.ownership_reason("cape.com", "Cape", strong) == "named_as_employer"


def test_short_name_needs_more_than_owning_the_page_title():
    """Owning the <title> is NOT sufficient for a generic one-word name.

    Verified against the LIVE site: moab.com's title is exactly "MOAB", so the
    identity signal fires, yet it is a Utah tourism site and not the employer
    "Moab". This test previously asserted that identity alone was enough, which
    would have sent a CV to that stranger. Corroboration must be a HIRING or
    EMPLOYER signal specifically — "Contact us / Privacy policy" furniture is on
    every website, moab.com included, so it proves nothing.
    """
    bare = _page("Vanta", "<p>Compliance automation.</p>" + _filler("compliance"))
    assert R.domain_confidently_belongs_to("vanta.com", "Vanta", page_text=bare) is False

    hiring = _page("Vanta", "<p>Compliance automation. Careers at Vanta.</p>"
                   + _filler("compliance"))
    assert R.domain_confidently_belongs_to("vanta.com", "Vanta", page_text=hiring) is True

    og = _page("Automated compliance", "<p>Vanta. We are hiring engineers.</p>"
               + _filler("compliance"), og_site="Vanta")
    assert R.domain_confidently_belongs_to("vanta.com", "Vanta", page_text=og) is True


def test_a_tourism_site_named_like_the_company_is_refused():
    """The exact live shape of moab.com, as a fixture so it cannot regress."""
    page = _page("MOAB", "<h1>Visit Moab</h1><p>Hotels, trails and tours in Moab, Utah.</p>"
                 + _filler("red rock country") +
                 "<footer>Contact us | Privacy policy</footer>")
    ev = R.ownership_evidence("moab.com", "Moab", page)
    assert ev["identity_match"] is True, "the title really does say MOAB"
    assert ev["company_furniture"] is True, "and it really does have footer furniture"
    assert ev["hiring_signal"] is False and ev["employer_context"] is False
    assert R.domain_confidently_belongs_to("moab.com", "Moab", page_text=page) is False


def test_short_name_passes_when_domain_is_the_name_and_the_page_hires():
    page = _page("Spend management for startups",
                 "<h1>Meet Ramp</h1><p>Ramp gives finance teams control.</p>"
                 + _filler("corporate cards") +
                 "<p>We're hiring across engineering.</p>"
                 "<footer>Privacy policy | Contact us</footer>")
    ev = R.ownership_evidence("ramp.com", "Ramp", page)
    assert ev["domain_is_name"] and ev["hiring_signal"] and ev["company_furniture"]
    assert R.domain_confidently_belongs_to("ramp.com", "Ramp", page_text=page) is True


def test_name_absent_from_the_page_is_refused():
    assert R.domain_confidently_belongs_to("job-board.com", "Lyft",
                                           page_text=JOBBOARD_STRANGER) is False
    assert R.ownership_reason("job-board.com", "Lyft",
                              JOBBOARD_STRANGER) == "name_absent_from_page"


def test_thin_page_proves_nothing():
    page = _page("Ramp", "<h1>Ramp</h1>")
    assert R.domain_confidently_belongs_to("ramp.com", "Ramp", page_text=page) is False
    assert R.ownership_reason("ramp.com", "Ramp", page) == "page_too_thin"


def test_ownership_check_never_fetches_when_page_text_is_supplied(monkeypatch):
    monkeypatch.setattr(R, "_fetch_homepage",
                        lambda d: pytest.fail(f"fetched {d} during a test"))
    assert R.domain_confidently_belongs_to("anthropic.com", "Anthropic",
                                           page_text=ANTHROPIC) is True


def test_unreachable_homepage_is_a_refusal_and_is_not_memoised(monkeypatch):
    calls = []
    monkeypatch.setattr(R, "_fetch_homepage", lambda d: calls.append(d) or None)
    assert R.domain_confidently_belongs_to("ramp.com", "Ramp") is False
    assert R.domain_confidently_belongs_to("ramp.com", "Ramp") is False
    assert len(calls) == 2, "a transient outage must be retried, not cached"


def test_fetched_verdict_is_memoised_per_domain(monkeypatch):
    calls = []
    monkeypatch.setattr(R, "_fetch_homepage", lambda d: calls.append(d) or RAMP)
    assert R.domain_confidently_belongs_to("ramp.com", "Ramp") is True
    assert R.domain_confidently_belongs_to("ramp.com", "Ramp") is True
    assert len(calls) == 1, "six roles at one company must not fetch six times"


def test_empty_inputs_are_refused():
    assert R.domain_confidently_belongs_to("", "Ramp", page_text=RAMP) is False
    assert R.domain_confidently_belongs_to("ramp.com", "", page_text=RAMP) is False


@pytest.mark.parametrize("name,expected", [
    ("Moab", False), ("Cape", False), ("Air", False), ("Garage", False),
    ("Campfire", False), ("Scale AI", False), ("Hex", False), ("Ramp", False),
    ("Anthropic", True), ("Doximity", True), ("Aventis Solutions", True),
    ("Xcel Energy", True), ("Acme Robotics", True), ("Aventis Solutions, Inc.", True),
])
def test_distinctiveness(name, expected):
    assert R.is_distinctive_name(name) is expected


@pytest.mark.parametrize("domain,label", [
    ("hex.tech", "hex"), ("www.ramp.com", "ramp"), ("datadoghq.com", "datadoghq"),
    ("careers.acme.co.uk", "acme"), ("notion.so", "notion"),
])
def test_domain_label(domain, label):
    assert R.domain_label(domain) == label


# ---------------------------------------------------------------------------
# resolution order and refusals
# ---------------------------------------------------------------------------

CFG = {"lookup_website": True, "guess_addresses": True,
       "max_guessed_per_day": 5, "address": "me@example.com"}

STABLE_KEYS = {"address", "source", "domain", "source_url", "domain_origin",
               "confidence", "rejected", "reason"}


def _stub_lookup(monkeypatch, rec):
    import utils.company_email as ce
    seen = {}

    def fake(job, **kw):
        seen.update(kw)
        seen["called"] = True
        return dict(rec)

    monkeypatch.setattr(ce, "find_company_email", fake)
    return seen


def _forbid_lookup(monkeypatch):
    import utils.company_email as ce
    monkeypatch.setattr(ce, "find_company_email",
                        lambda job, **kw: pytest.fail("company lookup should not run"))


def test_posting_wins_and_short_circuits_the_company_lookup(monkeypatch):
    _forbid_lookup(monkeypatch)
    job = _Job(description="Send your CV to Careers@acme.io today.", company="Acme")
    out = R.resolve(job, mail_cfg=CFG)
    assert out["address"] == "careers@acme.io"
    assert out["source"] == "posting"
    assert out["domain"] == "acme.io"
    assert out["domain_origin"] == "posting"
    assert out["confidence"] == 1.0
    assert out["rejected"] == [] and out["reason"] == ""


def test_falls_through_to_a_crawled_company_address(monkeypatch):
    _stub_lookup(monkeypatch, {"address": "jobs@acme.io", "source": "crawl",
                               "domain": "acme.io", "source_url": "https://acme.io/careers",
                               "domain_origin": "url"})
    out = R.resolve(_Job(description="No address here.", company="Acme"), mail_cfg=CFG)
    assert (out["address"], out["source"]) == ("jobs@acme.io", "company_site")
    assert out["source_url"] == "https://acme.io/careers"
    assert out["confidence"] == 0.75


def test_constructed_address_accepted_when_ownership_is_proven(monkeypatch):
    monkeypatch.setattr(R, "_fetch_homepage", lambda d: RAMP)
    _stub_lookup(monkeypatch, {"address": "careers@ramp.com", "source": "guess",
                               "domain": "ramp.com", "source_url": None,
                               "domain_origin": "derived"})
    out = R.resolve(_Job(company="Ramp"), mail_cfg=CFG, sent_today_guessed=0)
    assert (out["address"], out["source"]) == ("careers@ramp.com", "constructed")
    assert out["confidence"] == 0.4
    assert out["rejected"] == []


def test_constructed_address_refused_when_the_domain_is_a_stranger(monkeypatch):
    """The Moab case, end to end — the Mail Agent may now try, and still says no."""
    monkeypatch.setattr(R, "_fetch_homepage", lambda d: MOAB_TRAVEL)
    _stub_lookup(monkeypatch, {"address": "careers@moab.com", "source": "guess",
                               "domain": "moab.com", "source_url": None,
                               "domain_origin": "derived"})
    out = R.resolve(_Job(company="Moab"), mail_cfg=CFG)
    assert out["address"] is None
    assert out["reason"] == R.REASON_DOMAIN_UNCONFIRMED
    assert out["rejected"] == [{"address": "careers@moab.com",
                                "reason": R.REASON_DOMAIN_UNCONFIRMED}]


def test_domain_from_the_employers_own_url_skips_the_ownership_fetch(monkeypatch):
    monkeypatch.setattr(R, "_fetch_homepage",
                        lambda d: pytest.fail("authoritative domain must not be re-proven"))
    _stub_lookup(monkeypatch, {"address": "careers@moab.com", "source": "guess",
                               "domain": "moab.com", "source_url": None,
                               "domain_origin": "url"})
    out = R.resolve(_Job(company="Moab", url="https://moab.com/jobs/12"), mail_cfg=CFG)
    assert out["address"] == "careers@moab.com"
    assert out["source"] == "constructed"


@pytest.mark.parametrize("source,rec", [
    ("posting", None),
    ("crawl", {"address": "legal@acme.io", "source": "crawl", "domain": "acme.io",
               "source_url": "https://acme.io/contact", "domain_origin": "url"}),
    ("guess", {"address": "legal@acme.io", "source": "guess", "domain": "acme.io",
               "source_url": None, "domain_origin": "url"}),
])
def test_a_blocked_local_part_is_refused_from_every_source(monkeypatch, source, rec):
    """accommodation/legal/no-reply inboxes never receive a CV — not even when
    the employer printed the address in the posting itself."""
    if source == "posting":
        _stub_lookup(monkeypatch, {"address": None, "source": None,
                                   "domain": None, "source_url": None})
        job = _Job(description="Questions? legal@acme.io", company="Acme")
    else:
        _stub_lookup(monkeypatch, rec)
        job = _Job(description="No address here.", company="Acme")
    out = R.resolve(job, mail_cfg=CFG)
    assert out["address"] is None
    assert out["reason"] == R.REASON_UNSAFE
    assert {"address": "legal@acme.io", "reason": R.REASON_UNSAFE} in out["rejected"]


def test_posting_refusal_does_not_stop_the_company_lookup(monkeypatch):
    _stub_lookup(monkeypatch, {"address": "jobs@acme.io", "source": "crawl",
                               "domain": "acme.io", "source_url": "https://acme.io/jobs",
                               "domain_origin": "url"})
    job = _Job(description="Accessibility: accommodation@acme.io", company="Acme")
    out = R.resolve(job, mail_cfg=CFG)
    assert out["address"] == "jobs@acme.io"
    assert out["rejected"] == [{"address": "accommodation@acme.io",
                                "reason": R.REASON_UNSAFE}]


def test_dead_address_is_refused(monkeypatch):
    _stub_lookup(monkeypatch, {"address": None, "source": None, "domain": None,
                               "source_url": None})
    job = _Job(description="Apply to careers@acme.io", company="Acme")
    out = R.resolve(job, mail_cfg=CFG, dead={"CAREERS@acme.io"})
    assert out["address"] is None
    assert out["reason"] == R.REASON_DEAD


def test_our_own_address_is_never_an_employer(monkeypatch):
    _stub_lookup(monkeypatch, {"address": None, "source": None, "domain": None,
                               "source_url": None})
    job = _Job(description="Auto-reply from me@example.com", company="Acme")
    out = R.resolve(job, mail_cfg=CFG)
    assert out["address"] is None
    assert out["reason"] == R.REASON_OWN_ADDRESS


def test_lookup_disabled_when_both_switches_are_off(monkeypatch):
    _forbid_lookup(monkeypatch)
    out = R.resolve(_Job(company="Acme"),
                    mail_cfg={"lookup_website": False, "guess_addresses": False})
    assert out["address"] is None
    assert out["reason"] == R.REASON_LOOKUP_DISABLED


def test_a_raising_lookup_is_reported_not_propagated(monkeypatch):
    import utils.company_email as ce

    def boom(job, **kw):
        raise RuntimeError("DNS exploded")

    monkeypatch.setattr(ce, "find_company_email", boom)
    out = R.resolve(_Job(company="Acme"), mail_cfg=CFG)
    assert out["address"] is None
    assert out["reason"] == R.REASON_LOOKUP_FAILED


def test_allow_guess_argument_overrides_the_config(monkeypatch):
    seen = _stub_lookup(monkeypatch, {"address": None, "source": None,
                                      "domain": None, "source_url": None})
    R.resolve(_Job(company="Acme"), mail_cfg=CFG, allow_guess=False)
    assert seen["allow_guess"] is False
    R.resolve(_Job(company="Acme"), mail_cfg={"lookup_website": True,
                                              "guess_addresses": False},
              allow_guess=True)
    assert seen["allow_guess"] is True


def test_stable_shape_when_nothing_is_found(monkeypatch):
    _stub_lookup(monkeypatch, {"address": None, "source": None, "domain": None,
                               "source_url": None})
    out = R.resolve(_Job(company="Acme"), mail_cfg=CFG)
    assert set(out) == STABLE_KEYS
    assert out == {"address": None, "source": None, "domain": None,
                   "source_url": None, "domain_origin": None, "confidence": 0.0,
                   "rejected": [], "reason": R.REASON_NO_CANDIDATE}


def test_every_return_path_has_the_same_keys(monkeypatch):
    monkeypatch.setattr(R, "_fetch_homepage", lambda d: MOAB_TRAVEL)
    cases = [
        (_Job(description="careers@acme.io", company="Acme"),
         {"address": None, "source": None, "domain": None, "source_url": None}),
        (_Job(company="Moab"),
         {"address": "careers@moab.com", "source": "guess", "domain": "moab.com",
          "source_url": None, "domain_origin": "derived"}),
        (_Job(company="Acme"),
         {"address": "jobs@acme.io", "source": "crawl", "domain": "acme.io",
          "source_url": "https://acme.io/jobs", "domain_origin": "url"}),
    ]
    for job, rec in cases:
        _stub_lookup(monkeypatch, rec)
        assert set(R.resolve(job, mail_cfg=CFG)) == STABLE_KEYS


# ---------------------------------------------------------------------------
# the constructed-address quota
# ---------------------------------------------------------------------------

def test_quota_blocks_a_constructed_address(monkeypatch):
    monkeypatch.setattr(R, "_fetch_homepage", lambda d: RAMP)
    _stub_lookup(monkeypatch, {"address": "careers@ramp.com", "source": "guess",
                               "domain": "ramp.com", "source_url": None,
                               "domain_origin": "derived"})
    cfg = dict(CFG, max_guessed_per_day=3)
    assert R.resolve(_Job(company="Ramp"), mail_cfg=cfg,
                     sent_today_guessed=2)["address"] == "careers@ramp.com"
    out = R.resolve(_Job(company="Ramp"), mail_cfg=cfg, sent_today_guessed=3)
    assert out["address"] is None
    assert out["reason"] == R.REASON_QUOTA
    assert out["rejected"] == [{"address": "careers@ramp.com",
                                "reason": R.REASON_QUOTA}]


def test_a_cap_of_zero_forbids_constructed_addresses_entirely(monkeypatch):
    monkeypatch.setattr(R, "_fetch_homepage", lambda d: RAMP)
    _stub_lookup(monkeypatch, {"address": "careers@ramp.com", "source": "guess",
                               "domain": "ramp.com", "source_url": None,
                               "domain_origin": "derived"})
    out = R.resolve(_Job(company="Ramp"), mail_cfg=dict(CFG, max_guessed_per_day=0),
                    sent_today_guessed=0)
    assert out["reason"] == R.REASON_QUOTA


@pytest.mark.parametrize("rec,expected_source", [
    ({"address": "jobs@acme.io", "source": "crawl", "domain": "acme.io",
      "source_url": "https://acme.io/jobs", "domain_origin": "url"}, "company_site"),
])
def test_published_addresses_are_never_quota_limited(monkeypatch, rec, expected_source):
    _stub_lookup(monkeypatch, rec)
    out = R.resolve(_Job(company="Acme"), mail_cfg=dict(CFG, max_guessed_per_day=1),
                    sent_today_guessed=9999)
    assert out["source"] == expected_source and out["address"] == "jobs@acme.io"

    _forbid_lookup(monkeypatch)
    posted = R.resolve(_Job(description="careers@acme.io", company="Acme"),
                       mail_cfg=dict(CFG, max_guessed_per_day=1),
                       sent_today_guessed=9999)
    assert posted["source"] == "posting"


def test_resolve_never_increments_anything(monkeypatch):
    """The runner's cap counted by SIDE EFFECT inside its predicate; this one
    does not, so calling resolve twice for a preview cannot burn the quota."""
    monkeypatch.setattr(R, "_fetch_homepage", lambda d: RAMP)
    _stub_lookup(monkeypatch, {"address": "careers@ramp.com", "source": "guess",
                               "domain": "ramp.com", "source_url": None,
                               "domain_origin": "derived"})
    job = _Job(company="Ramp")
    for _ in range(5):
        assert R.resolve(job, mail_cfg=CFG, sent_today_guessed=0)["address"] == \
            "careers@ramp.com"


# ---------------------------------------------------------------------------
# the DB-backed counter
# ---------------------------------------------------------------------------

def _send(session, recipient, source, status, when):
    from db.models import OutreachSend
    row = OutreachSend(dedup_key=f"{recipient}|{source}|{status}|{when.isoformat()}",
                       company="Acme", company_normalized="acme",
                       job_title="Engineer", recipient=recipient,
                       recipient_source=source, status=status, sent_at=when)
    session.add(row)
    session.commit()
    return row


def test_guessed_sent_today_counts_only_todays_sent_constructed_rows(tmp_session):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    midday = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if midday > now:                     # early-morning runs: use "now" instead
        midday = now
    yesterday = now - timedelta(days=1, hours=2)

    _send(tmp_session, "a@x.com", "constructed", "sent", midday)
    _send(tmp_session, "b@x.com", "constructed", "sent", midday)
    _send(tmp_session, "c@x.com", "constructed", "failed", midday)   # never arrived
    _send(tmp_session, "d@x.com", "constructed", "blocked", midday)  # never arrived
    _send(tmp_session, "e@x.com", "company_site", "sent", midday)    # published
    _send(tmp_session, "f@x.com", "post_text", "sent", midday)       # published
    _send(tmp_session, "g@x.com", "constructed", "sent", yesterday)  # not today

    assert R.guessed_sent_today(tmp_session) == 2


def test_guessed_sent_today_is_zero_on_an_empty_ledger(tmp_session):
    assert R.guessed_sent_today(tmp_session) == 0


def test_a_failed_count_blocks_constructed_sends(monkeypatch):
    """The fail-closed count, fed back into resolve, must refuse the send."""
    class _Broken:
        def query(self, *a, **kw):
            raise RuntimeError("database is locked")

    _stub_lookup(monkeypatch, {"address": "careers@ramp.com", "source": "guess",
                               "domain": "ramp.com", "source_url": None,
                               "domain_origin": "url"})
    out = R.resolve(_Job(company="Ramp"), mail_cfg=CFG,
                    sent_today_guessed=R.guessed_sent_today(_Broken()))
    assert out["address"] is None
    assert out["reason"] == R.REASON_QUOTA


def test_guessed_sent_today_fails_closed_when_the_ledger_is_unreadable():
    """A DB problem must block constructed sends, not unblock them."""
    class _Broken:
        def query(self, *a, **kw):
            raise RuntimeError("database is locked")

    count = R.guessed_sent_today(_Broken())
    assert count >= 10 ** 6
