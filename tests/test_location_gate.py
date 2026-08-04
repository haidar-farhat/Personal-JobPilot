"""Scan-time non-US location gate (2026-08-03 relocation pivot).

The gate exists to stop paying for LLM scoring on reqs Matthew can't take.
The interesting cases are all in the overlap: postings that name a foreign
office AND a US one, which a naive deny-list would throw away wholesale.
"""

import pytest

from agents.scanner.base import BaseScanner


class _Scanner(BaseScanner):
    """Concrete stub — BaseScanner.scan() is abstract."""

    source_name = "test"

    def scan(self):
        return []


@pytest.fixture
def scanner():
    return _Scanner({
        "search": {
            "excluded_locations": [
                "india", "bangalore", "hyderabad", "canada", "mexico",
                "united kingdom", "london", "ireland", "dublin", "israel",
                "yokneam", "japan", "tokyo", "emea", "apac",
            ]
        }
    })


@pytest.mark.parametrize("location", [
    "India - Hyderabad",
    "India - Bangalore",
    "Mexico - Mexico City",
    "Ireland - Dublin",
    "United Kingdom - London",
    "Israel, Yokneam",
    "Japan - Tokyo",
    "Canada - Remote (ON, AB, BC, or NS Only)",  # ", or NS" must NOT read as Oregon
    "London, ON",                                 # Ontario, not a US state code
    "EMEA - Remote",
])
def test_rejects_foreign(scanner, location):
    assert scanner._should_skip_location(location) is True


@pytest.mark.parametrize("location", [
    "San Francisco, CA",
    "Austin, TX",
    "US, CA, Santa Clara",
    "United States - Remote",
    "Remote - USA",
    "Bay Area, CA, United States of America",
    "Phoenix, AZ",
    "Portland, OR",
    "",                        # blank: plenty of real US reqs omit location
    "Not specified",
])
def test_keeps_us_and_unknown(scanner, location):
    assert scanner._should_skip_location(location) is False


@pytest.mark.parametrize("location", [
    # Foreign office named alongside a US-eligible one — keep, the US half is real.
    "San Francisco, CA | London, UK",
    "London, United Kingdom; New York, NY",
    "Remote - US, Canada",
    "Austin, TX; Dublin, Ireland",
    "Bangalore, India; United States - Remote",
])
def test_keeps_mixed_us_and_foreign(scanner, location):
    assert scanner._should_skip_location(location) is False


def test_disabled_when_unconfigured():
    """No excluded_locations in settings -> gate is a no-op, nothing gets dropped."""
    s = _Scanner({"search": {}})
    assert s._should_skip_location("India - Hyderabad") is False


def test_real_settings_yaml_drops_the_foreign_rows_in_the_db():
    """Wire check against the shipped config, not a hand-built fixture."""
    import yaml
    from pathlib import Path

    cfg_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    s = _Scanner(cfg)
    # Locations observed in jobpilot.db on 2026-08-03.
    assert s._should_skip_location("India - Hyderabad") is True
    assert s._should_skip_location("Canada - Remote (ON, AB, BC, or NS Only)") is True
    assert s._should_skip_location("Israel, Yokneam") is True
    assert s._should_skip_location("Cupertino, CA") is False
    assert s._should_skip_location("United States - Remote") is False
    assert s._should_skip_location("San Francisco, CA; Remote, US") is False

    # Leaks found by auditing 13 live ATS boards on 2026-08-03.
    for foreign in ("Remote-Vietnam", "Ho Chi Minh City, Ho Chi Minh City",
                    "Riyadh, Saudi Arabia", "Stockholm, Sweden", "Remote-Austria",
                    "South Africa, Remote", "Auckland; Melbourne", "Zurich",
                    "Alberta; British Columbia; Manitoba; Nova Scotia; Quebec"):
        assert s._should_skip_location(foreign) is True, foreign

    # US towns sharing a foreign city's name must survive on their state code —
    # this is what keeps the broad country deny-list safe.
    for us_namesake in ("Dublin, OH", "Rome, GA", "Vienna, VA", "Paris, TX",
                        "Berlin, NH", "Athens, GA", "Manchester, NH"):
        assert s._should_skip_location(us_namesake) is False, us_namesake

    # US places that merely CONTAIN a country name as a substring. These carry
    # no state code to rescue them, so only word-boundary matching saves them.
    # Both were live in the DB scoring in the top 200 on 2026-08-03.
    for substring_trap in ("Indiana - Remote", "Indiana - Indianapolis",
                           "Indianapolis, Indiana", "Indiana"):
        assert s._should_skip_location(substring_trap) is False, substring_trap

    # ", CA" is California after a city but CANADA after a province code.
    for canada in ("Toronto, ON, CA", "Vancouver, BC, CA", "Montreal, QC, CA"):
        assert s._should_skip_location(canada) is True, canada
    for california in ("San Francisco, CA", "Los Angeles, CA", "San Jose, CA"):
        assert s._should_skip_location(california) is False, california

    # Multi-region remotes that DO include the US must not be dropped.
    for keep in ("Remote-NORAM", "Remote-Americas", "Anywhere USA"):
        assert s._should_skip_location(keep) is False, keep
