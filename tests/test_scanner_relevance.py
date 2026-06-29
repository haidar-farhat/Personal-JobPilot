"""Scanner title-relevance gate.

The ATS scanners (Greenhouse/Lever/Ashby) gate on the job title before doing
expensive work. This gate must:
  - accept AI-track + BT-track titles (added 2026-06-29), not just data/analyst,
  - keep accepting the existing data/analyst/scientist roles,
  - use word boundaries so short tokens (rbt) don't match inside unrelated words
    and "data" doesn't match "Database".
"""

import pytest

from agents.scanner.career_pages import GreenhouseScanner

CONFIG = {"search": {"keywords": [
    "Data Analyst", "Data Scientist", "Quantitative Analyst",
    "AI Engineer", "LLM Engineer", "Solutions Engineer",
    "Forward Deployed Engineer", "Prompt Engineer",
    "Machine Learning Engineer", "AI Analyst",
    "Behavioral Technician", "Behavior Technician",
    "Registered Behavior Technician", "RBT", "ABA Therapist",
    "Behavior Interventionist",
]}}


def _scanner():
    return GreenhouseScanner(CONFIG)


@pytest.mark.parametrize("title", [
    "Data Analyst",
    "Senior Data Scientist",
    "Quantitative Analyst, Markets",
    "AI Engineer",
    "Staff AI Engineer, Platform",
    "Forward Deployed Engineer",
    "Solutions Engineer (AI)",
    "Prompt Engineer",
    "Machine Learning Engineer",
    "AI Analyst",
    "Behavioral Technician",
    "Registered Behavior Technician (RBT)",
    "Behavior Interventionist - Part Time",
    "ABA Therapist",
])
def test_relevant_titles_pass(title):
    assert _scanner()._title_is_relevant(title), f"should be relevant: {title}"


@pytest.mark.parametrize("title", [
    "Database Administrator",   # 'data' must NOT match inside 'database'
    "Marketing Manager",
    "Registered Nurse",
    "Warehouse Associate",
    "Mechanical Engineer",      # bare 'engineer' must not be a relevance term
])
def test_irrelevant_titles_rejected(title):
    assert not _scanner()._title_is_relevant(title), f"should be rejected: {title}"
