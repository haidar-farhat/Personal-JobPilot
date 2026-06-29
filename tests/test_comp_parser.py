"""Hourly-comp + employment-type parsing for the Behavioral Technician track."""

import pytest

from utils.comp import parse_hourly, parse_employment_type


@pytest.mark.parametrize("text,expected", [
    ("$30/hr", (30.0, 30.0)),
    ("$30.00 per hour", (30.0, 30.0)),
    ("$28-$35 an hour", (28.0, 35.0)),
    ("$28 – $35 per hour", (28.0, 35.0)),
    ("Pay: $24 to $34 hourly", (24.0, 34.0)),
    ("30/hr DOE", (30.0, 30.0)),
    ("Rate: $32.50/hour", (32.5, 32.5)),
])
def test_parse_hourly_positive(text, expected):
    assert parse_hourly(text) == expected


@pytest.mark.parametrize("text", [
    "Compensation: $120,000/yr",
    "$95,000 - $130,000 per year",
    "Salary commensurate with experience",
    "",
    None,
])
def test_parse_hourly_none(text):
    assert parse_hourly(text) is None


def test_job_has_hourly_columns():
    """Hourly comp + employment_type columns (migration 005, 2026-06-29)."""
    from db.models import Job
    cols = {c.name for c in Job.__table__.columns}
    for c in ["pay_period", "hourly_min", "hourly_max", "employment_type"]:
        assert c in cols, f"missing Job column: {c}"


@pytest.mark.parametrize("title,desc,expected", [
    ("Part-Time Behavior Technician", "", "part_time"),
    ("Behavior Technician", "This is a full-time position", "full_time"),
    ("RBT", "per diem shifts available", "per_diem"),
    ("Behavior Interventionist (Contract)", "", "contract"),
    ("Behavioral Technician", "", "unknown"),
])
def test_parse_employment_type(title, desc, expected):
    assert parse_employment_type(title, desc) == expected
