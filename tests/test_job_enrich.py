"""Deterministic enrichment of the fields the LinkedIn guest scraper leaves blank.

Titles and locations marked REAL are copied verbatim out of jobpilot.db's 106
`linkedin_public` rows (2026-09-09). Their descriptions are genuinely '' in the
database, which is why the description cases below are synthetic — they encode
the phrasings the extractor must survive once the detail fetch starts working.
"""

import pytest

from utils.job_enrich import (
    EMPLOYMENT_TYPES,
    SENIORITY_LEVELS,
    WORK_MODES,
    detect_comp,
    detect_employment_type,
    detect_remote,
    detect_seniority,
    detect_work_mode,
    enrich,
    normalize_employment_type,
    normalize_seniority,
)


# ---------------------------------------------------------------------------
# work mode / remote
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,location,desc,expected", [
    # REAL rows: the only two of the 106 that state a mode in the title.
    ("Artificial Intelligence Engineer (On-Site, IN)", "Carmel, IN", "", "onsite"),
    ("AI Engineer - On Site", "Grand Rapids, MI", "", "onsite"),
    # REAL rows: a bare city says "not remote" but cannot separate onsite/hybrid.
    ("AI Engineer", "Miami, FL", "", None),
    ("Machine Learning Engineer", "San Francisco Bay Area", "", None),
    ("Applied AI Engineer", "Florida, United States", "", None),
    # Location-field markers, in every shape LinkedIn emits them.
    ("Software Engineer", "Remote", "", "remote"),
    ("Software Engineer", "Remote (US)", "", "remote"),
    ("Software Engineer", "Austin, TX (Remote)", "", "remote"),
    ("Software Engineer", "United States - Remote", "", "remote"),
    ("Software Engineer", "Chicago, IL (Hybrid)", "", "hybrid"),
    ("Software Engineer", "Boston, MA (On-site)", "", "onsite"),
    # THE TRAP: location names a head office, body says the role is remote.
    ("Software Engineer", "New York, NY", "This is a fully remote position.", "remote"),
    ("Software Engineer", "New York, NY", "The role is remote.", "remote"),
    ("Software Engineer", "Austin, TX", "You will work from home.", "remote"),
    # Hybrid outranks remote: a hybrid post advertises its remote days.
    ("Software Engineer", "Austin, TX",
     "Hybrid schedule: 2 days remote, 3 days in the office.", "hybrid"),
    ("Software Engineer", "Austin, TX", "3 days per week in office.", "hybrid"),
    # Explicit negation beats everything else in the body.
    ("Software Engineer", "Austin, TX",
     "Remote work is not available for this position.", "onsite"),
    ("Software Engineer", "Austin, TX", "This role is not remote.", "onsite"),
    # Title beats location.
    ("Remote Software Engineer", "Austin, TX", "", "remote"),
])
def test_detect_work_mode(title, location, desc, expected):
    assert detect_work_mode(title, location, desc) == expected


@pytest.mark.parametrize("desc", [
    # Company-culture boilerplate on an ON-SITE req. None of these promise that
    # THIS role is remote, so none may set the filter.
    "We have a remote-first culture and offices in four cities. Onsite in Austin.",
    "Remote-friendly perks. This position is on-site 5 days a week.",
    "Join our remote team of 200. In-office presence required in Denver.",
    "We are a distributed team. This is an in-office role.",
])
def test_remote_culture_blurb_does_not_make_an_onsite_role_remote(desc):
    assert detect_work_mode("Software Engineer", "Denver, CO", desc) == "onsite"
    assert detect_remote("Software Engineer", "Denver, CO", desc) is False


def test_remote_culture_blurb_alone_asserts_nothing():
    """No mode marker anywhere but a culture line: fall back to the location."""
    desc = "We're a remote-first culture that trusts its people."
    assert detect_work_mode("Software Engineer", "Austin, TX", desc) is None
    assert detect_remote("Software Engineer", "Austin, TX", desc) is False


def test_onsite_interview_is_not_an_onsite_role():
    desc = "Our loop ends with an onsite interview. Final round is in-person interview."
    assert detect_work_mode("Software Engineer", "", desc) is None


@pytest.mark.parametrize("title,location,desc,expected", [
    ("AI Engineer", "Miami, FL", "", False),                     # REAL
    ("AI Engineer - On Site", "Grand Rapids, MI", "", False),     # REAL
    ("Software Engineer", "Remote (US)", "", True),
    ("Software Engineer", "New York, NY", "This is a 100% remote role.", True),
    # Hybrid still requires an office, so the boolean column says False.
    ("Software Engineer", "Chicago, IL (Hybrid)", "", False),
    # Nothing at all to go on.
    ("Software Engineer", "", "", None),
])
def test_detect_remote(title, location, desc, expected):
    assert detect_remote(title, location, desc) is expected


def test_work_mode_values_are_from_the_published_vocabulary():
    for title, loc in [("AI Engineer - On Site", "Grand Rapids, MI"),
                       ("Software Engineer", "Remote"),
                       ("Software Engineer", "Chicago, IL (Hybrid)")]:
        assert detect_work_mode(title, loc, "") in WORK_MODES


# ---------------------------------------------------------------------------
# seniority
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    # REAL rows carrying a level marker — the roman-numeral ladder is the only
    # seniority signal that actually appears in this corpus.
    ("AI/ML Engineer II", "mid"),
    ("AI Engineer II", "mid"),
    # III is a company-defined rung; 'mid' is the floor that cannot be wrong
    # by two steps, so that is what it maps to.
    ("AI Engineer III", "mid"),
    ("Machine Learning Engineer III", "mid"),
    ("Machine Learning Engineer II", "mid"),
    # REAL rows with no level marker at all.
    ("AI Engineer", None),
    ("Machine Learning Engineer", None),
    ("Applied AI Engineer", None),
    ("Full Stack Engineer", None),
    ("AI Engineer x3", None),
    ("Agentic AI Engineer - Anthropic/Claude", None),
    # REAL row: "Internal" must not read as "Intern".
    ("AI Engineer, Internal Enablement & Productivity", None),
    # REAL row: a company-specific numeric ladder is not portable.
    ("Machine Learning Engineer, Level 4", None),
    # Title vocabulary, including the settings.yaml excluded-title words.
    ("Senior Software Engineer", "senior"),
    ("Sr. Machine Learning Engineer", "senior"),
    ("Junior Data Engineer", "junior"),
    ("Jr Backend Developer", "junior"),
    ("Entry-Level Software Engineer", "entry"),
    ("Software Engineering Intern", "intern"),
    ("Machine Learning Internship", "intern"),
    ("Mid-Level Platform Engineer", "mid"),
    ("Lead Software Engineer", "lead"),
    ("Principal Engineer", "principal"),
    ("Director of Machine Learning", "director"),
    ("VP of Engineering", "executive"),
    ("Head of AI", "executive"),
    # Highest rung wins: "Senior Staff" is a staff role.
    ("Senior Staff Software Engineer", "staff"),
    # A sales title that merely contains "lead".
    ("Lead Generation Specialist", None),
])
def test_detect_seniority_from_title(title, expected):
    assert detect_seniority(title, "") == expected


@pytest.mark.parametrize("desc", [
    # THE TRAP the task names: a description mentioning senior PEOPLE.
    "You will work with senior stakeholders across the business.",
    "Partner with senior engineers to ship features.",
    "Report to our senior leadership team.",
    "Mentorship from senior members of the team is provided.",
])
def test_senior_mentions_of_other_people_do_not_set_seniority(desc):
    assert detect_seniority("Software Engineer", desc) is None


@pytest.mark.parametrize("desc,expected", [
    ("Seniority level: Mid-Senior level", "mid"),
    ("Seniority Level: Entry level", "entry"),
    ("This is a senior-level position on the platform team.", "senior"),
    ("We are looking for a principal engineer to own the roadmap.", "principal"),
    ("Requires 8 years of professional experience.", "senior"),
    ("Minimum of 3 years of experience with Python.", "mid"),
    ("At least 1 year of experience.", "entry"),
    # Not a requirement — a brag about the founders.
    ("Our founders have 20 years of experience in fintech.", None),
])
def test_detect_seniority_from_description(desc, expected):
    assert detect_seniority("Software Engineer", desc) == expected


def test_title_beats_description_for_seniority():
    """A junior req whose body praises the senior team stays junior."""
    assert detect_seniority(
        "Junior Software Engineer",
        "You will work with senior architects and senior stakeholders daily.",
    ) == "junior"


def test_seniority_values_are_from_the_published_vocabulary():
    for title in ["Senior Software Engineer", "AI Engineer II", "Principal Engineer",
                  "VP of Engineering", "Software Engineering Intern"]:
        assert detect_seniority(title, "") in SENIORITY_LEVELS


@pytest.mark.parametrize("label,expected", [
    ("Mid-Senior level", "mid"),          # LinkedIn's own criteria string
    ("mid-senior level", "mid"),
    ("Entry level", "entry"),
    ("Internship", "intern"),
    ("Director", "director"),
    ("senior", "senior"),
    ("", None),
    (None, None),
    ("Not Applicable", None),
])
def test_normalize_seniority(label, expected):
    assert normalize_seniority(label) == expected


# ---------------------------------------------------------------------------
# employment type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,desc,expected", [
    # REAL rows: nothing in these titles states an employment type.
    ("AI Engineer", "", None),
    ("Machine Learning Engineer", "", None),
    ("Python FullStack (F2F Interview)", "", None),
    # Titles that do.
    ("Part-Time Data Engineer", "", "part_time"),
    ("Software Engineer (Contract)", "", "contract"),
    ("Machine Learning Intern", "", "internship"),
    ("Software Engineering Internship", "", "internship"),
    ("Full-Time Backend Engineer", "", "full_time"),
    # REAL row: "Internal" is not "Intern".
    ("AI Engineer, Internal Enablement & Productivity", "", None),
    # Description phrasings, all scoped to the posting.
    ("Software Engineer", "Employment type: Full-time", "full_time"),
    ("Software Engineer", "This is a full-time position.", "full_time"),
    ("Software Engineer", "6-month contract with possible extension.", "contract"),
    ("Software Engineer", "Contract-to-hire opportunity.", "contract"),
    ("Software Engineer", "Part-time role, 20 hours per week.", "part_time"),
    ("Behavior Technician", "per diem shifts available", "per_diem"),
])
def test_detect_employment_type(title, desc, expected):
    assert detect_employment_type(title, desc) == expected


@pytest.mark.parametrize("desc", [
    # utils.comp.parse_employment_type returns "contract" for every one of
    # these because it substring-matches; the scoped patterns must not.
    "You will support contract negotiation with enterprise customers.",
    "Our platform manages government contracts for 40 agencies.",
    "Experience with smart contracts on Ethereum is a plus.",
    "Reviews vendor contracts alongside legal.",
])
def test_incidental_contract_mentions_are_not_an_employment_type(desc):
    assert detect_employment_type("Software Engineer", desc) is None


def test_employment_type_values_are_from_the_published_vocabulary():
    for title in ["Part-Time Data Engineer", "Software Engineer (Contract)",
                  "Machine Learning Intern", "Full-Time Backend Engineer"]:
        assert detect_employment_type(title, "") in EMPLOYMENT_TYPES


@pytest.mark.parametrize("label,expected", [
    ("Full-time", "full_time"),
    ("Part time", "part_time"),
    ("Internship", "internship"),
    ("Temporary", "contract"),
    ("Permanent", "full_time"),
    ("unknown", None),
    (None, None),
])
def test_normalize_employment_type(label, expected):
    assert normalize_employment_type(label) == expected


# ---------------------------------------------------------------------------
# compensation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("desc,expected", [
    ("Base salary range: $120,000 - $150,000 per year.",
     {"pay_period": "annual", "salary_min": 120000.0, "salary_max": 150000.0}),
    ("Compensation is $120,000-$150,000 annually.",
     {"pay_period": "annual", "salary_min": 120000.0, "salary_max": 150000.0}),
    ("The salary band is 120k-150k.",
     {"pay_period": "annual", "salary_min": 120000.0, "salary_max": 150000.0}),
    ("Salary: $120K – $150K",
     {"pay_period": "annual", "salary_min": 120000.0, "salary_max": 150000.0}),
    ("Pay range between $95,000 and $130,000.",
     {"pay_period": "annual", "salary_min": 95000.0, "salary_max": 130000.0}),
    ("Annual salary of USD 120000 plus equity.",
     {"pay_period": "annual", "salary_min": 120000.0, "salary_max": 120000.0}),
    ("Base salary $150,000.",
     {"pay_period": "annual", "salary_min": 150000.0, "salary_max": 150000.0}),
    # Hourly, delegated to utils.comp.parse_hourly.
    ("Rate is $60-75/hr depending on experience.",
     {"pay_period": "hourly", "hourly_min": 60.0, "hourly_max": 75.0}),
    ("Pay: $32.50 per hour.",
     {"pay_period": "hourly", "hourly_min": 32.5, "hourly_max": 32.5}),
    ("$28 – $35 an hour", {"pay_period": "hourly", "hourly_min": 28.0, "hourly_max": 35.0}),
])
def test_detect_comp_positive(desc, expected):
    got = detect_comp(desc, None)
    for key, value in expected.items():
        assert got.get(key) == value, f"{key}: {got}"


@pytest.mark.parametrize("desc", [
    # THE most common comp string in real postings — a salary word, no number.
    "Competitive salary and equity.",
    "We offer a competitive compensation package.",
    "Salary commensurate with experience.",
    "Compensation DOE.",
    "Pay depends on experience.",
    # Money that is not pay.
    "We raised $150,000,000 in our Series C.",
    "Our team saved customers $500,000 last year.",
    "The platform processed $250,000 in transactions on day one.",
    # A bare number with no currency and no scale word.
    "Our model was trained on 120000 - 150000 examples.",
    # A lone in-range figure in prose with no annual marker at all.
    "We shipped $75,000 of hardware to the Denver office.",
    # REAL: the actual state of all 106 linkedin_public descriptions.
    "",
    None,
])
def test_detect_comp_finds_nothing(desc):
    assert detect_comp(desc, None) == {}


def test_salary_text_field_is_authoritative():
    """A dedicated comp field needs no annual marker; the field IS the marker."""
    got = detect_comp("Some long description with no pay in it.", "USD 120000")
    assert got["pay_period"] == "annual"
    assert got["salary_min"] == 120000.0


def test_hourly_and_annual_never_both_reported():
    got = detect_comp("$60-75/hr, roughly $125,000 per year equivalent.", None)
    assert got["pay_period"] == "hourly"
    assert "salary_min" not in got


# ---------------------------------------------------------------------------
# enrich(): never clobber, never guess, always the same answer
# ---------------------------------------------------------------------------

def test_enrich_real_row_with_empty_description():
    """REAL row 699. Location is all the evidence there is, and it shows."""
    got = enrich("AI Engineer", "Fort Myers, FL", "")
    assert got == {"is_remote": False}


def test_enrich_real_row_with_a_level_and_an_onsite_title():
    """REAL rows 717 (AI/ML Engineer II) and 713 (On-Site) combined shape."""
    got = enrich("AI/ML Engineer II", "Sarasota, FL", "")
    assert got["seniority_level"] == "mid"
    assert got["is_remote"] is False
    assert "work_mode" not in got          # city alone cannot rule out hybrid
    assert "employment_type" not in got
    assert "pay_period" not in got


def test_enrich_full_posting():
    got = enrich(
        "Senior Machine Learning Engineer",
        "New York, NY",
        "This is a full-time position. The role is fully remote. "
        "Base salary range: $180,000 - $220,000 per year.",
    )
    assert got["seniority_level"] == "senior"
    assert got["employment_type"] == "full_time"
    assert got["work_mode"] == "remote"
    assert got["is_remote"] is True
    assert got["pay_period"] == "annual"
    assert got["salary_min"] == 180000.0
    assert got["salary_max"] == 220000.0


@pytest.mark.parametrize("existing,absent", [
    ({"seniority_level": "Mid-Senior level"}, "seniority_level"),
    ({"employment_type": "contract"}, "employment_type"),
    ({"is_remote": True}, "is_remote"),
    ({"pay_period": "annual", "salary_min": 90000.0}, "salary_min"),
    ({"work_mode": "hybrid"}, "work_mode"),
])
def test_enrich_never_overwrites_a_value_a_scanner_already_set(existing, absent):
    got = enrich(
        "Senior Software Engineer",
        "Austin, TX",
        "Full-time position, fully remote. Base salary range: $150,000 - $180,000.",
        existing=existing,
    )
    assert absent not in got


@pytest.mark.parametrize("existing", [
    {"seniority_level": ""},
    {"seniority_level": None},
    {"employment_type": "unknown"},
    {"pay_period": "unknown"},
    {"is_remote": False},
    {"salary_min": 0},
])
def test_enrich_treats_scanner_placeholders_as_unset(existing):
    """'', 'unknown' and 0 are what the scanners write when they give up."""
    got = enrich(
        "Senior Software Engineer",
        "Austin, TX",
        "Full-time position, fully remote. Base salary range: $150,000 - $180,000.",
        existing=existing,
    )
    key = next(iter(existing))
    assert key in got, f"{key} should have been filled: {got}"


def test_enrich_omits_everything_it_cannot_determine():
    """No title signal, no location, no description -> nothing at all."""
    assert enrich("Engineer", "", "") == {}


def test_enrich_is_deterministic():
    args = ("Senior Machine Learning Engineer II", "Austin, TX (Hybrid)",
            "Full-time. Base salary range: $150,000 - $180,000 per year.")
    first = enrich(*args)
    for _ in range(25):
        assert enrich(*args) == first


def test_detectors_are_pure_and_accept_none():
    assert detect_work_mode(None, None, None) is None
    assert detect_remote(None, None, None) is None
    assert detect_seniority(None, None) is None
    assert detect_employment_type(None, None) is None
    assert detect_comp(None, None) == {}
    assert enrich(None, None, None) == {}


def test_enrich_keys_are_writable_job_columns():
    """Everything enrich() emits, except work_mode, is an existing Job column."""
    from db.models import Job

    columns = {c.name for c in Job.__table__.columns}
    got = enrich(
        "Senior Software Engineer",
        "Remote (US)",
        "Full-time position. Base salary range: $150,000 - $180,000 per year.",
    )
    unknown = set(got) - columns - {"work_mode"}
    assert not unknown, f"enrich() emitted non-columns: {unknown}"


@pytest.mark.parametrize("desc", [
    # REAL: jobpilot.db job 562. utils.comp.parse_hourly, pointed at a whole
    # description, reads this work SCHEDULE as an $8.00/hour wage.
    "Work Schedule: This is a full-time position, Monday - Friday, "
    "with basic 8hr/day work requirement between 6:00 a.m. and 6:00 p.m.",
    "Expect 40 hours per week; 8hr/day on core days.",
    "Standard 8 hr/day schedule.",
])
def test_schedule_hours_are_not_an_hourly_wage(desc):
    assert detect_comp(desc, None) == {}


@pytest.mark.parametrize("desc", [
    # REAL descriptions from jobpilot.db (jobs 360, 422, 429): "on-site" here
    # is the CUSTOMER's site, reached by travel — it says nothing about where
    # the job itself is based.
    "This role requires frequent on-site engagement at customer and partner "
    "facilities across the region.",
    "Have the opportunity to travel: spend at least 25% of your time onsite, "
    "working closely with our most strategic customers.",
    "Travel up to 20-40% to work on-site with customers and partners.",
    # REAL (job 434): a NEGATED in-office clause.
    "You may sit in any of the office locations listed - there is no minimum "
    "in-office qualification requirement.",
])
def test_customer_site_and_negated_office_clauses_are_not_onsite(desc):
    assert detect_work_mode("Software Engineer", "", desc) is None


def test_real_hybrid_description_is_hybrid():
    """REAL description shared by jobs 344/349/353/355 in jobpilot.db."""
    desc = ("This role is based in San Francisco, CA. We use a hybrid work model "
            "of 3 days in the office per week and offer relocation assistance.")
    assert detect_work_mode("Software Engineer, Data Infrastructure",
                            "San Francisco", desc) == "hybrid"
    assert detect_remote("Software Engineer, Data Infrastructure",
                         "San Francisco", desc) is False


def test_real_remote_description_is_remote():
    """REAL description from jobs 436/437 in jobpilot.db."""
    desc = ("This role is open to candidates based in North America. "
            "You can work from anywhere within this region.")
    assert detect_work_mode("Analytics Engineer", "North America", desc) == "remote"
    assert detect_remote("Analytics Engineer", "North America", desc) is True


@pytest.mark.parametrize("location", ["US - Remote", "US-IL-Remote", "Remote - US"])
def test_real_remote_location_strings(location):
    """REAL location strings from jobs 400/408/423 in jobpilot.db."""
    assert detect_work_mode("Data Scientist, Cybersecurity", location, "") == "remote"


def test_in_office_perks_list_is_not_a_work_mode():
    """REAL description shared by jobs 438-442: a benefits list, not a location."""
    desc = ("Benefits: 16 weeks parental leave at 100% pay - pet insurance - "
            "in-office perks: lunch, snacks, drinks, and more - relocation support.")
    assert detect_work_mode("Applied AI Engineer", "New York, NY (HQ)", desc) is None


def test_real_in_person_attendance_clause_is_onsite():
    """REAL description from job 428 — an actual attendance requirement."""
    desc = ("Able to adapt and thrive in a semi-structured environment. This role "
            "requires in-person attendance at our new Menlo Park, CA office.")
    assert detect_work_mode("Corporate FP&A Analyst", "US-CA-Menlo Park", desc) == "onsite"


def test_work_mode_menu_boilerplate_is_not_onsite():
    """REAL description from job 510 — an HR taxonomy, not this req's mode."""
    desc = ("We support flexibility and trust. Work personas (flexible, remote, or "
            "required in office) are categories assigned to employees.")
    assert detect_work_mode("Solution Architect", "Staines", desc) is None


def test_in_office_benefits_line_items_are_not_a_work_mode():
    """REAL description from job 453 — every 'in-office' here is a perk."""
    desc = ("Flexible time off + holidays. Commuter benefits (in-office & US only). "
            "In office set-up reimbursement (in-office only). "
            "In office amenities (in-office only).")
    assert detect_work_mode("Forward Deployed Engineer", "Foster City, CA", desc) is None


@pytest.mark.parametrize("desc,expected", [
    # REAL (job 804): the day count comes AFTER the marker.
    ("Associates will work onsite 4 days per week at the Richmond, VA office.",
     "hybrid"),
    ("3 days per week in office, 2 remote.", "hybrid"),
    # Five days is not hybrid, however it is phrased.
    ("You will be in the office 5 days a week.", "onsite"),
    ("This position is on-site 5 days a week.", "onsite"),
])
def test_days_in_office_split_hybrid_from_onsite(desc, expected):
    assert detect_work_mode("Analyst", "Richmond, VA", desc) == expected
