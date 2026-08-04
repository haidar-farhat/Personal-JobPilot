"""Inline multi-entry sections (Apple-style, 2026-07-22): the Nth repeat of an
experience/education field maps to history entry N — never entry 0 duplicated
into every block, and blocks beyond our history stay deterministically blank."""

from agents.autofill_mapper import build_plan

PROFILE = {
    "identity": {"first_name": "Matthew", "last_name": "Cromaz"},
    "address": {"city": "San Francisco", "state": "CA", "location_line": "San Francisco, CA"},
    "employment": [
        {"title": "Freelance Software Engineer", "company": "Leasing Agent 415 (Compass)",
         "location": "San Francisco, CA", "current": True,
         "start_month": 6, "start_year": 2026, "description": "Sole engineer for a rental site."},
        {"title": "Behavioral Technician", "company": "Behavioral Intervention Associates (BIA)",
         "location": "San Mateo, CA", "current": True,
         "start_month": 8, "start_year": 2025, "description": "Collect session data."},
        {"title": "Economic Consultant (Graduate Capstone)", "company": "Rithum",
         "location": "Remote", "current": False, "start_month": 3, "start_year": 2025,
         "end_month": 6, "end_year": 2025, "description": "Built demand models."},
    ],
    "education": [
        {"school": "California Polytechnic State University, San Luis Obispo",
         "degree": "Master's Degree", "gpa": "3.467", "end_year": 2025},
        {"school": "Reed College", "degree": "Bachelor's Degree", "end_year": 2024},
    ],
}


def F(fid, label, name="", section="Professional Experience", ftype="text"):
    return {"id": fid, "label": label, "name": name, "type": ftype, "section": section}


def _values(fields):
    plan = build_plan(fields, PROFILE, "ai", "resume summary")
    return {o["id"]: o["value"] for o in plan["fields"]}


def test_repeated_experience_blocks_get_distinct_entries():
    fields = []
    for n in range(4):  # one block more than we have history entries
        fields += [F(f"c{n}", "Employer", f"employer-{n}"),
                   F(f"t{n}", "Job Title", f"jobtitle-{n}"),
                   F(f"l{n}", "Location", f"location-{n}"),
                   F(f"d{n}", "Role Description", f"desc-{n}")]
    got = _values(fields)
    assert got["c0"] == "Leasing Agent 415 (Compass)"
    assert got["c1"] == "Behavioral Intervention Associates (BIA)"
    assert got["c2"] == "Rithum"
    assert got["t2"] == "Economic Consultant (Graduate Capstone)"
    assert got["d1"] == "Collect session data."
    # role location comes from THAT entry, not the applicant's home address
    assert got["l1"] == "San Mateo, CA"
    assert got["l2"] == "Remote"
    # 4th block: beyond our history — blank, not a Leasing Agent 415 duplicate
    assert got["c3"] is None and got["t3"] is None and got["l3"] is None


def test_numbered_labels_count_as_repeats():
    fields = [F("a", "Employer 1"), F("b", "Employer 2"), F("c", "Employer 3")]
    got = _values(fields)
    assert got["a"] == "Leasing Agent 415 (Compass)"
    assert got["b"] == "Behavioral Intervention Associates (BIA)"
    assert got["c"] == "Rithum"


def test_repeated_education_blocks():
    fields = [F("s0", "School", "school-0", "Education"),
              F("s1", "School", "school-1", "Education"),
              F("s2", "School", "school-2", "Education"),
              F("g0", "GPA", "gpa-0", "Education"),
              F("g1", "GPA", "gpa-1", "Education")]
    got = _values(fields)
    assert got["s0"].startswith("California Polytechnic")
    assert got["s1"] == "Reed College"
    assert got["s2"] is None                     # beyond history — blank
    assert got["g0"] == "3.467"
    assert got["g1"] is None                     # Reed has no GPA — stays blank


def test_single_entry_behavior_unchanged():
    got = _values([F("c", "Employer", "employer"), F("t", "Title", "title")])
    assert got["c"] == "Leasing Agent 415 (Compass)"
    assert got["t"] == "Freelance Software Engineer"
