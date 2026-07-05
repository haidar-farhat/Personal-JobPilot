"""GenericCareersScanner page-text cleanup.

Harvested job detail pages (source='custom') used to store raw
document.body.innerText, which starts with cookie-consent banners,
screen-reader helpers, and site navigation before the actual job content
(seen live on ABA provider sites). _clean_page_text must drop that
boilerplate while preserving the job description itself — including short
bullet lines and comp figures.
"""

from agents.scanner.company_sites import _clean_page_text

# Modeled on the Centria Autism / Autism Learning Partners pages as stored.
COOKIE_AND_NAV_PAGE = """\
Press Option+1 for screen-reader mode, Option+0 to cancel
Accessibility Screen-Reader Guide, Feedback, and Issue Reporting | New window

We value your privacy

We use cookies to enhance your browsing experience, serve personalised ads or content, and analyse our traffic. By clicking "Accept All", you consent to our use of cookies.

Customise
Reject All
Accept All
ABA Therapy
Pediatric Nursing
Contact Us
Careers
View All Positions
Behavior Technician Positions

Behavior Technician Jobs Near You

Are you passionate about helping children with autism achieve their full potential? As a Behavior Technician, you'll have the opportunity to make a real difference in the lives of children and families.

Why Work for Centria?

At Centria, we are dedicated to providing our Behavior Technicians with the support, training and resources they need to succeed.
"""

SKIP_LINKS_PAGE = """\
Skip to main content
Skip to footer
About
Services
Locations
Insurance
Contact
Careers
(855) 767-3540
Behavior Technician Positions
Home
Apply Now
Start Your Career as a Behavior Technician

Take on a meaningful role where you'll make a real difference in a child's life, session by session.

What Does a Behavior Technician Do?

As a Behavior Technician (BT), you'll work one-on-one with children to provide ABA therapy in various settings, helping them build new skills and reach their goals.
"""


def test_cookie_banner_and_screen_reader_hints_dropped():
    cleaned = _clean_page_text(COOKIE_AND_NAV_PAGE)
    assert "We value your privacy" not in cleaned
    assert "cookies" not in cleaned.lower()
    assert "Reject All" not in cleaned
    assert "Accept All" not in cleaned
    assert "screen-reader" not in cleaned.lower()
    assert "New window" not in cleaned


def test_leading_nav_stripped_content_preserved():
    cleaned = _clean_page_text(COOKIE_AND_NAV_PAGE)
    assert "Pediatric Nursing" not in cleaned  # nav menu entry
    assert "View All Positions" not in cleaned
    assert cleaned.startswith("Are you passionate about helping children")
    assert "Why Work for Centria?" in cleaned  # short headings after prose kept
    assert "support, training and resources" in cleaned


def test_skip_links_and_phone_nav_stripped():
    cleaned = _clean_page_text(SKIP_LINKS_PAGE)
    assert "Skip to main content" not in cleaned
    assert "Skip to footer" not in cleaned
    assert "(855) 767-3540" not in cleaned
    assert cleaned.startswith("Take on a meaningful role")
    assert "What Does a Behavior Technician Do?" in cleaned


def test_clean_prose_passes_through():
    text = (
        "We are looking for a Registered Behavior Technician to join our team.\n"
        "You will work one-on-one with clients under BCBA supervision.\n"
    )
    cleaned = _clean_page_text(text)
    assert "Registered Behavior Technician" in cleaned
    assert "BCBA supervision" in cleaned


def test_short_bullets_after_prose_survive():
    text = (
        "Join our team as a Behavior Technician and change lives every day.\n"
        "Requirements:\n"
        "High school diploma or GED\n"
        "Reliable transportation\n"
        "Pass a background check\n"
    )
    cleaned = _clean_page_text(text)
    assert "High school diploma or GED" in cleaned
    assert "Reliable transportation" in cleaned


def test_leading_comp_line_survives():
    text = (
        "Home\n"
        "Careers\n"
        "$20 - $25 per hour\n"
        "We provide ABA therapy services to children across the region and are hiring now.\n"
    )
    cleaned = _clean_page_text(text)
    assert "$20 - $25 per hour" in cleaned
    assert "Home" not in cleaned.splitlines()


def test_page_with_no_prose_kept_minus_boilerplate():
    text = "Accept All\nReject All\nOpen Positions\nBehavior Technician\n"
    cleaned = _clean_page_text(text)
    assert "Behavior Technician" in cleaned
    assert "Accept All" not in cleaned


def test_limit_enforced():
    text = "This is a long enough prose line to anchor the content block here.\n" * 500
    assert len(_clean_page_text(text, limit=1000)) <= 1000
