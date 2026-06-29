"""EdJoin card parser — Bay Area filtering + BT relevance + hourly capture.

These exercise the pure `_card_to_rawjob` parser against fixture card dicts
(the shape EdJoin's rendered DOM yields). No network / browser.
"""

from agents.scanner.edjoin import EdJoinScanner
from utils.comp import parse_hourly


def _card(title, district_line, salary):
    text = f"{title}\n\n {district_line}\n\n Deadline: Until Filled\n\n{salary}"
    return {"title": title, "href": "/Home/JobPosting/123456", "text": text}


def test_bay_area_bt_card_parsed():
    card = _card("Behavior Technician",
                 "Oakland Unified School District - Oakland, Alameda County, CA",
                 "$28 - $34  Per Hour")
    raw = EdJoinScanner._card_to_rawjob(card)
    assert raw is not None
    assert raw.source == "edjoin"
    assert raw.url == "https://www.edjoin.org/Home/JobPosting/123456"
    assert "Oakland Unified" in raw.company
    assert "Oakland" in raw.location
    # salary text drives the hourly parser used at store time
    assert parse_hourly(raw.salary_text) == (28.0, 34.0)


def test_non_bay_area_card_rejected():
    card = _card("Behavior Technician",
                 "Rosedale Union School District - Bakersfield, Kern County, CA",
                 "$19.47 - $30.35  Per Hour")
    assert EdJoinScanner._card_to_rawjob(card) is None


def test_non_bt_title_rejected():
    card = _card("Cafeteria Worker",
                 "Oakland Unified School District - Oakland, Alameda County, CA",
                 "$20 - $25  Per Hour")
    assert EdJoinScanner._card_to_rawjob(card) is None


def test_registered_behavior_technician_sf_parsed():
    card = _card("Registered Behavior Technician (RBT)",
                 "San Francisco Unified School District - San Francisco, CA",
                 "$30 - $36  Per Hour")
    raw = EdJoinScanner._card_to_rawjob(card)
    assert raw is not None
    assert "San Francisco" in raw.location
    assert parse_hourly(raw.salary_text) == (30.0, 36.0)


def test_missing_href_returns_none():
    assert EdJoinScanner._card_to_rawjob({"title": "Behavior Technician", "href": "", "text": "x"}) is None
