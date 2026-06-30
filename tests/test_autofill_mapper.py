"""Unit tests for the pure autofill field mapper."""

from agents.autofill_mapper import normalize, choose_archetype, map_standard_field, build_plan

PROFILE = {
    "identity": {"first_name": "Matthew", "last_name": "Cromaz", "full_name": "Matthew Cromaz",
                 "email": "m@x.com", "phone": "415-745-5603"},
    "address": {"city": "Oakland", "state": "CA", "state_full": "California",
                "postal_code": "94601", "country": "United States"},
    "links": {"linkedin": "https://li/in/x", "github": "https://gh/x", "portfolio": ""},
    "work_authorization": {"authorized_to_work_us": True, "requires_sponsorship": False,
                           "citizenship": "U.S. Citizen"},
    "experience": {"total_years": 1, "highest_education": "Master's Degree",
                   "current_employer": "BIA", "current_title": "Behavioral Technician"},
    "eeoc": {"gender": "Decline to answer", "race_ethnicity": "Decline to answer",
             "veteran_status": "I am not a protected veteran"},
    "salary": {"preferred_text": "Open / negotiable"},
    "referral": {"default_source": "LinkedIn"},
}


def test_normalize():
    assert normalize("First  Name*") == "first name"


def test_map_email():
    f = {"id": "f1", "label": "Email Address", "name": "email", "type": "email"}
    assert map_standard_field(f, PROFILE)["value"] == "m@x.com"


def test_map_first_name():
    f = {"id": "f2", "label": "First Name", "name": "first_name", "type": "text"}
    assert map_standard_field(f, PROFILE)["value"] == "Matthew"


def test_map_last_name():
    f = {"id": "f2b", "label": "Last Name", "name": "last_name", "type": "text"}
    assert map_standard_field(f, PROFILE)["value"] == "Cromaz"


def test_map_linkedin():
    f = {"id": "f3", "label": "LinkedIn Profile", "name": "linkedin", "type": "url"}
    assert map_standard_field(f, PROFILE)["value"] == "https://li/in/x"


def test_map_work_auth_yesno():
    f = {"id": "f4", "label": "Are you authorized to work in the US?", "name": "auth",
         "type": "select", "options": ["Yes", "No"]}
    assert map_standard_field(f, PROFILE)["value"] == "Yes"


def test_map_sponsorship_no():
    f = {"id": "f5", "label": "Will you require sponsorship?", "name": "sponsor",
         "type": "select", "options": ["Yes", "No"]}
    assert map_standard_field(f, PROFILE)["value"] == "No"


def test_map_state_select_full_name():
    f = {"id": "f6", "label": "State", "name": "state", "type": "select",
         "options": ["California", "Nevada", "Oregon"]}
    assert map_standard_field(f, PROFILE)["value"] == "California"


def test_state_does_not_match_statement():
    f = {"id": "f6b", "label": "Personal statement", "name": "statement", "type": "textarea"}
    assert map_standard_field(f, PROFILE) is None


def test_ethnicity_not_matched_as_city():
    f = {"id": "f7", "label": "Race / Ethnicity", "name": "ethnicity", "type": "select",
         "options": ["Decline to answer", "Asian", "White"]}
    assert map_standard_field(f, PROFILE)["value"] == "Decline to answer"


def test_unknown_field_returns_none():
    f = {"id": "f9", "label": "Favorite color", "name": "color", "type": "text"}
    assert map_standard_field(f, PROFILE) is None


def test_file_field_flagged():
    f = {"id": "f10", "label": "Upload resume", "name": "resume", "type": "file"}
    m = map_standard_field(f, PROFILE)
    assert m["source"] == "file" and m["needs_review"] is True and m["value"] is None


def test_choose_archetype_bt_by_title():
    arch = {"behavioral_technician": {"keywords": ["behavior technician", "rbt", "aba"]},
            "ai_engineer": {"keywords": ["ai engineer"]}}
    assert choose_archetype("Behavior Technician", "", arch) == "behavioral_technician"


def test_choose_archetype_by_page_text():
    arch = {"behavioral_technician": {"keywords": ["aba"]}, "ai_engineer": {"keywords": ["ai engineer"]}}
    assert choose_archetype("Specialist", "We are an ABA clinic", arch) == "behavioral_technician"


def test_choose_archetype_pref_override_ai():
    arch = {"behavioral_technician": {"keywords": ["aba"]}}
    assert choose_archetype("ABA role", "", arch, resume_pref="ai") is None


def test_choose_archetype_pref_override_bt():
    assert choose_archetype("Anything", "", {}, resume_pref="bt") == "behavioral_technician"


def test_build_plan_routes_essays_to_fn():
    fields = [{"id": "f1", "label": "Email", "name": "email", "type": "email"},
              {"id": "f2", "label": "Why do you want to work here?", "name": "why", "type": "textarea"}]
    seen = []

    def essay_fn(field, ctx):
        seen.append(field["id"])
        return "Because reasons."

    plan = build_plan(fields, PROFILE, None, "RESUME", essay_fn=essay_fn)
    byid = {f["id"]: f for f in plan["fields"]}
    assert byid["f1"]["value"] == "m@x.com" and byid["f1"]["source"] == "deterministic"
    assert byid["f2"]["value"] == "Because reasons." and byid["f2"]["source"] == "llm"
    assert seen == ["f2"] and plan["stats"]["filled"] == 2


def test_build_plan_essay_cap():
    fields = [{"id": f"e{i}", "label": "Tell us something?", "name": f"e{i}", "type": "textarea"}
              for i in range(5)]
    plan = build_plan(fields, PROFILE, None, "R", essay_fn=lambda f, c: "x", max_essays=2)
    assert plan["stats"]["llm_used"] == 2
