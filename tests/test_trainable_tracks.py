"""Trainable / low-barrier track wiring (2026-08-11).

Guards the chain that puts a car-sales / data-center-tech / ATC job on the
dashboard: scan-time title gate -> archetype routing -> per-archetype comp
floor -> auto-apply hourly gate. Config-driven, so a well-meaning edit to
settings.yaml or archetypes.yaml is what these catch.
"""

import pytest
import yaml

from agents import ranker
from agents.auto_applier.runner import _passes_hourly_floor
from agents.scanner.base import BaseScanner
from agents.scanner.company_sites import _LOOKS_LIKE_LOCATION_RE

NEW_ARCHETYPES = ("skilled_technician", "sales_representative", "operations_trainee")


class _Scanner(BaseScanner):
    source_name = "test"

    def scan(self):
        return []


@pytest.fixture(scope="module")
def settings():
    from pathlib import Path
    p = Path(__file__).parent.parent / "config" / "settings.yaml"
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def archetypes():
    from pathlib import Path
    p = Path(__file__).parent.parent / "config" / "archetypes.yaml"
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)["archetypes"]


@pytest.mark.parametrize("title", [
    "Data Center Critical Facilities II",
    "Automotive Sales Consultant",
    "Air Traffic Control Specialist - Trainee",
    "Manufacturing Technician",
    "Claims Adjuster - Remote",
    "Power Plant Trainee",
    "Lineman Apprentice",
])
def test_scan_gate_admits_trainable_titles(settings, title):
    """These titles reach the ranker only if settings.search.keywords covers them."""
    s = _Scanner(settings)
    assert s._title_is_relevant(title), f"{title!r} would be dropped at scan time"
    assert not s._should_skip_title(title)


@pytest.mark.parametrize("title", ["Barista", "Registered Nurse", "Senior Staff Engineer"])
def test_scan_gate_still_rejects_offtrack_titles(settings, title):
    """The new keywords must not turn the gate into a pass-through."""
    s = _Scanner(settings)
    assert not (s._title_is_relevant(title) and not s._should_skip_title(title))


@pytest.mark.parametrize("title,expected", [
    ("Data Center Critical Facilities II", "skilled_technician"),
    ("Manufacturing Technician", "skilled_technician"),
    ("Automotive Sales Consultant", "sales_representative"),
    ("Inside Sales Rep", "sales_representative"),
    ("Air Traffic Control Specialist - Trainee", "operations_trainee"),
    ("Claims Adjuster - Remote", "operations_trainee"),
])
def test_archetype_title_routing(archetypes, title, expected):
    assert ranker._match_archetype_by_title(title, archetypes) == expected


def test_new_archetype_weights_sum_to_one(archetypes):
    for key in NEW_ARCHETYPES:
        total = sum(archetypes[key]["weights"].values())
        assert abs(total - 1.0) < 0.001, f"{key} weights sum to {total}"


def test_salary_floor_is_per_archetype():
    """$30/hr x 2080 for the trainable tracks; the AI/data track keeps $85k."""
    for key in NEW_ARCHETYPES:
        assert ranker._salary_floor(key) == 62400
    assert ranker._salary_floor("data_analyst") == 85000
    assert ranker._salary_floor(None) == 85000


def test_auto_apply_needs_confirmed_hourly_rate(settings):
    """Unconfirmed pay must NOT auto-submit on the new tracks."""
    floors = settings["comp"]["min_hourly_by_archetype"]
    for key in NEW_ARCHETYPES:
        assert floors.get(key) == 30, f"{key} lost its auto-apply hourly gate"
        assert not _passes_hourly_floor(key, None, None, floors)
        assert not _passes_hourly_floor(key, 22.0, 24.0, floors)
        assert _passes_hourly_floor(key, 30.0, 38.0, floors)


@pytest.mark.parametrize("text,ok", [
    ("Austin, TX", True),
    ("Remote", True),
    ("Nationwide", True),
    ("ARE NOT due to lack of funding.", False),
    ("of positions offered to each selectee will be based upon the", False),
])
def test_scraped_location_shape_guard(text, ok):
    """The generic scraper only believes a location that looks like one."""
    assert bool(_LOOKS_LIKE_LOCATION_RE.search(text)) is ok
