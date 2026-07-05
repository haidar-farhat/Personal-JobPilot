"""Unit tests for the pure autofill field mapper."""

from agents.autofill_mapper import (
    normalize, choose_archetype, map_standard_field, build_plan, _match_choice,
)

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


def test_map_full_name_label_with_name_attr():
    f = {"id": "fn", "label": "Full name", "name": "name", "type": "text"}
    assert map_standard_field(f, PROFILE)["value"] == "Matthew Cromaz"


def test_company_name_is_not_full_name():
    f = {"id": "cn", "label": "Company name", "name": "company", "type": "text"}
    assert map_standard_field(f, PROFILE) is None


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


# --- EEO / voluntary-disclosure choice matching (verbose ATS option text) ---

def test_match_choice_verbose_disability_yes():
    opts = ["Yes, I have a disability, or have had one in the past",
            "No, I do not have a disability", "I do not want to answer"]
    assert _match_choice("Yes", opts) == opts[0]


def test_match_choice_no_maps_to_not_hispanic():
    opts = ["Hispanic or Latino", "Not Hispanic or Latino", "Decline to answer"]
    assert _match_choice("No", opts) == "Not Hispanic or Latino"


def test_match_choice_gender_synonym_man():
    assert _match_choice("Male", ["Man", "Woman", "Decline To Self Identify"]) == "Man"


def test_match_choice_decline_synonym():
    opts = ["Male", "Female", "I don't wish to answer"]
    assert _match_choice("Decline to answer", opts) == "I don't wish to answer"


def test_match_choice_token_overlap_two_or_more_races():
    opts = ["White", "Asian", "Two or More Races (Not Hispanic or Latino)", "Decline"]
    assert _match_choice("Two or More Races", opts) == opts[2]


def test_eeoc_fields_resolve_against_verbose_options():
    # Synthetic EEO answers — these exercise the verbose-option matching only and
    # are NOT anyone's real self-identification (never commit real EEO data).
    profile = {
        "eeoc": {"gender": "Female", "race_ethnicity": "Asian",
                 "hispanic_latino": "No", "veteran_status": "Decline to answer",
                 "disability_status": "Decline to answer", "lgbtq": "Yes",
                 "sexual_orientation": "Prefer not to say"},
    }
    def val(label, options):
        return map_standard_field(
            {"id": "x", "label": label, "name": "", "type": "text", "options": options}, profile)
    r = val("What is your gender?", ["Man", "Woman", "Non-binary", "Decline"])
    assert r["value"] == "Woman" and not r["needs_review"]      # Female → Woman synonym
    r = val("Are you Hispanic or Latino?", ["Hispanic or Latino", "Not Hispanic or Latino", "Decline"])
    assert r["value"] == "Not Hispanic or Latino"               # No → verbose
    r = val("Please select your race", ["White", "Asian", "Two or More Races", "Decline"])
    assert r["value"] == "Asian"
