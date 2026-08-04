"""Unit tests for the pure Google-Sheet sync logic (no network)."""

from agents.sheet_sync import (
    normalize,
    normalize_url,
    name_key,
    map_headers,
    default_header,
    canonical_header_map,
    build_row,
    compare,
    format_pay,
    status_label,
    CANONICAL_COLUMNS,
    APPLIED_STATUSES,
)


def test_normalize():
    assert normalize("  Date  Applied! ") == "date applied"
    assert normalize(None) == ""


def test_normalize_url_canonicalizes():
    a = normalize_url("https://www.Greenhouse.io/Jobs/123/")
    b = normalize_url("http://greenhouse.io/jobs/123")
    assert a == b == "greenhouse.io/jobs/123"


def test_url_query_preserved():
    # Query string is the identity for many ATS postings — must NOT be stripped.
    assert normalize_url("https://x.com/a?gh_jid=1") != normalize_url("https://x.com/a?gh_jid=2")


def test_name_key_empty_and_normalized():
    assert name_key("", "") == ""
    assert name_key("  Acme,Inc ", "AI Engineer") == "acme inc :: ai engineer"


def test_map_headers_exact_user_layout():
    hdr = ["Company", "Role", "Date Applied", "Status", "Link", "Notes"]
    m = map_headers(hdr)
    assert m["company"] == 0
    assert m["title"] == 1       # "Role" -> title
    assert m["date_applied"] == 2
    assert m["results"] == 3     # old "Status" column carries the Results value
    assert m["url"] == 4         # "Link" -> url
    assert m["notes"] == 5


def test_map_headers_bianca_job_log_layout():
    hdr = ["Date Sent", "Company Name", "Job Title", "Where it was sent",
           "Link to Application", "Interview?", "Follow Up?", "Results"]
    m = map_headers(hdr)
    assert m == {"date_applied": 0, "company": 1, "title": 2, "source": 3,
                 "url": 4, "interview": 5, "followup": 6, "results": 7}


def test_map_headers_no_substring_false_match():
    # "Candidate" must NOT match the "date" synonym (the substring trap).
    m = map_headers(["Candidate", "Company"])
    assert "date_applied" not in m
    assert m["company"] == 1


def test_map_headers_each_field_claimed_once():
    m = map_headers(["Company", "Employer"])   # both want 'company'; first wins
    assert m["company"] == 0
    assert len([k for k in m if m[k] == 1]) == 0


def test_default_header_matches_canonical():
    assert default_header() == [c[1] for c in CANONICAL_COLUMNS]
    # Bianca's Job Log layout (2026-07-22)
    assert default_header() == ["Date Sent", "Company Name", "Job Title",
                                "Where it was sent", "Link to Application",
                                "Interview?", "Follow Up?", "Results"]


def test_canonical_header_map():
    m = canonical_header_map()
    assert m["date_applied"] == 0
    assert len(m) == len(CANONICAL_COLUMNS)


def test_build_row_aligns_to_user_columns():
    # Sheet with only 3 cols: Company(0), Role(1), Link(2)
    hmap = {"company": 0, "title": 1, "url": 2}
    app = {"company": "Acme", "title": "AI Engineer", "url": "https://x.com/1",
           "status_label": "Applied", "notes": "n/a"}
    assert build_row(app, hmap, 3) == ["Acme", "AI Engineer", "https://x.com/1"]


def test_build_row_blanks_unmapped_columns():
    hmap = canonical_header_map()
    app = {"company": "Acme", "title": "Eng", "url": "u",
           "date_applied": "2026-06-30", "status": "applied", "status_label": "Applied"}
    row = build_row(app, hmap, len(hmap))
    assert row[hmap["company"]] == "Acme"
    assert row[hmap["source"]] == ""      # not provided -> blank
    assert row[hmap["followup"]] == ""    # always manual


def test_build_row_bianca_status_columns():
    hmap = canonical_header_map()
    pending = {"company": "A", "title": "T", "status": "applied", "status_label": "Applied"}
    row = build_row(pending, hmap, len(hmap))
    assert row[hmap["interview"]] == ""   # not interviewing yet
    assert row[hmap["results"]] == ""     # still pending -> blank, not "Applied"
    interviewing = {"company": "A", "title": "T", "status": "interview", "status_label": "Interview"}
    row = build_row(interviewing, hmap, len(hmap))
    assert row[hmap["interview"]] == "Yes"
    assert row[hmap["results"]] == "Interview"
    rejected = {"company": "A", "title": "T", "status": "rejected", "status_label": "Rejected"}
    row = build_row(rejected, hmap, len(hmap))
    assert row[hmap["results"]] == "Rejected"


def test_compare_matches_by_url_despite_name_diff():
    hmap = {"company": 0, "title": 1, "url": 2}
    data = [["Acme", "Engineer", "https://x.com/1"]]
    apps = [
        {"company": "Acme Inc", "title": "Software Eng", "url": "http://x.com/1/"},  # same url
        {"company": "Beta", "title": "Analyst", "url": "https://y.com/2"},
    ]
    res = compare(apps, data, hmap)
    assert [a["company"] for a in res["in_sheet"]] == ["Acme Inc"]
    assert [a["company"] for a in res["new"]] == ["Beta"]


def test_compare_matches_by_company_title_when_no_url():
    hmap = {"company": 0, "title": 1}
    data = [["Acme", "AI Engineer"]]
    apps = [{"company": "ACME", "title": "ai engineer", "url": ""}]
    res = compare(apps, data, hmap)
    assert len(res["in_sheet"]) == 1
    assert len(res["new"]) == 0


def test_compare_counts_orphans():
    hmap = {"company": 0, "title": 1, "url": 2}
    data = [["Acme", "Eng", "https://x.com/1"], ["Ghost", "Role", "https://z.com/9"]]
    apps = [{"company": "Acme", "title": "Eng", "url": "https://x.com/1"}]
    res = compare(apps, data, hmap)
    assert res["orphans"] == 1   # the Ghost row isn't tracked in JobPilot


def test_format_pay_variants():
    assert format_pay(salary_text="$120k") == "$120k"
    assert format_pay(hourly_min=30, hourly_max=45) == "$30 – $45/hr"
    assert format_pay(hourly_min=35) == "$35/hr"
    assert format_pay(salary_min=90000, salary_max=110000) == "$90,000 – $110,000"
    assert format_pay() == ""


def test_status_label():
    assert status_label("applied") == "Applied"
    assert status_label("response_received") == "Response Received"
    assert status_label("weird_custom") == "Weird Custom"


def test_applied_statuses():
    assert "applied" in APPLIED_STATUSES and "interview" in APPLIED_STATUSES
    assert "found" not in APPLIED_STATUSES
