"""Mapper rules added for JobRight-profile parity (2026-07-05).

Covers: education section (school/degree/discipline/GPA/end dates), location
typeaheads, recurring consent questions (worked-at / SMS / privacy), and the
disclosed EEO presets against verbose ATS option vocabularies.
"""

from agents.autofill_mapper import map_standard_field

PROFILE = {
    "identity": {"first_name": "Matthew", "last_name": "Cromaz", "full_name": "Matthew Cromaz",
                 "email": "m@x.com", "phone": "555-0142"},
    "address": {"street": "1 Example Street", "city": "San Francisco", "state": "CA",
                "state_full": "California", "postal_code": "94122", "country": "United States",
                "location_line": "San Francisco, California, United States"},
    "links": {"linkedin": "https://www.linkedin.com/in/matthew-cromaz",
              "github": "https://github.com/TheCromazone"},
    "work_authorization": {"authorized_to_work_us": True, "requires_sponsorship": False},
    "experience": {"total_years": 2, "highest_education": "Master's Degree"},
    "eeoc": {"gender": "Male", "race_ethnicity": "Two or More Races",
             "race_components": ["Asian", "White"],
             "veteran_status": "I am not a protected veteran", "disability_status": "Yes",
             "hispanic_latino": "No", "transgender": "No", "lgbtq": "No",
             "sexual_orientation": "Heterosexual"},
    "education": [
        {"school": "California Polytechnic State University, San Luis Obispo",
         "degree": "Master's Degree", "degree_full": "Master of Science",
         "discipline": "Economics", "field_of_study": "Quantitative Economics",
         "gpa": "3.467", "end_month": 6, "end_year": 2025},
        {"school": "Reed College", "degree": "Bachelor's Degree",
         "degree_full": "Bachelor of Arts", "discipline": "Economics",
         "field_of_study": "Economics", "gpa": "", "end_month": 1, "end_year": 2024},
    ],
    "preferences": {"sms_opt_in": True, "privacy_acknowledged": True, "worked_here_before": False,
                    "willing_to_relocate": True, "willing_onsite": True,
                    "age_18_or_older": True, "government_official": False,
                    "relative_of_government_official": False, "conflict_of_interest": False,
                    "referred_by_insider": False,
                    "ai_tools_usage": "I design or automate workflows with AI tools",
                    "earliest_start_date": "September 1, 2026"},
}


def m(label, type="text", options=None, section="", name=""):
    return map_standard_field(
        {"id": "f0", "label": label, "name": name, "type": type,
         "options": options, "section": section}, PROFILE)


# ---- education ----

def test_school_maps_most_recent_entry():
    assert m("School")["value"].startswith("California Polytechnic")


def test_degree_maps_dropdown_vocabulary():
    got = m("Degree", "select", ["Bachelor's Degree", "Master's Degree", "Doctorate"])
    assert got["value"] == "Master's Degree"


def test_degree_free_text_uses_full_name():
    assert m("Degree")["value"] == "Master of Science"


def test_discipline_and_gpa():
    assert m("Discipline")["value"] == "Economics"
    assert m("GPA")["value"] == "3.467"


def test_education_end_date_year_select():
    got = m("End Date Year", "select", [str(y) for y in range(2030, 2000, -1)], section="Education")
    assert got["value"] == "2025"


def test_education_end_date_month_select():
    months = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]
    got = m("End Date Month", "select", months, section="Education")
    assert got["value"] == "June"


def test_highest_education_still_maps():
    got = m("Highest level of education completed", "select",
            ["High School", "Bachelor's Degree", "Master's Degree"])
    assert got["value"] == "Master's Degree"


# ---- location ----

def test_bare_location_typeahead_gets_full_line():
    assert m("Location (City)")["value"] == "San Francisco, California, United States"


def test_location_select_gets_city_option():
    got = m("Location", "select", ["New York", "San Francisco", "Seattle"])
    assert got["value"] == "San Francisco"


def test_current_location_phrasings():
    assert m("Current Location")["value"] == "San Francisco, California, United States"
    assert m("Where are you currently located?")["value"] == "San Francisco, California, United States"
    assert m("Where are you based?")["value"] == "San Francisco, California, United States"
    assert m("City of residence")["value"] == "San Francisco, California, United States"


def test_relocate_and_in_person_yes():
    # Matthew's canonical screener answers (2026-07-06): willing to move to SF
    # and work in person — yes to relocation and to onsite/hybrid questions
    got = m("Are you willing to move to SF and work in person with us?", "select", ["Yes", "No"])
    assert got["value"] == "Yes"
    assert m("Are you willing to relocate?", "select", ["Yes", "No"])["value"] == "Yes"
    assert m("Are you able to work onsite 3 days a week?", "select", ["Yes", "No"])["value"] == "Yes"
    assert m("Are you open to a hybrid schedule?", "select", ["Yes", "No"])["value"] == "Yes"


def test_based_in_country_dropdown():
    # "Where are you currently based?" with a COUNTRY list: cascade past city/
    # state to country, and dodge the "Minor Outlying Islands" alphabetical trap
    got = m("Where are you currently based?", "select",
            ["Canada", "United Kingdom", "United States Minor Outlying Islands",
             "United States of America"])
    assert got["value"] == "United States of America"
    got = m("Where are you currently based?", "select",
            ["Canada", "United Kingdom", "United States"])
    assert got["value"] == "United States"


def test_legally_authorized_and_sponsorship():
    assert m("Are you legally authorized to work in the US?", "select", ["Yes", "No"])["value"] == "Yes"
    assert m("Will you now or in the future require visa sponsorship?", "select",
             ["Yes", "No"])["value"] == "No"


def test_street_address_variants():
    assert m("Address Line 1")["value"] == "1 Example Street"
    assert m("Street Address")["value"] == "1 Example Street"
    assert m("Address 1")["value"] == "1 Example Street"


def test_address_line_2_stays_empty():
    # Optional apt/suite line — explicit no-fill, no review, and must NOT
    # inherit line 1 even though "Street Address Line 2" contains "street address"
    for label in ("Address Line 2", "Street Address Line 2", "Apt / Suite"):
        got = m(label)
        assert got["value"] is None
        assert got["needs_review"] is False


def test_bare_home_address_gets_full_line():
    assert m("Home Address")["value"] == "1 Example Street, San Francisco, CA 94122"


def test_zip_code_maps_real_zip():
    assert m("Zip Code")["value"] == "94122"
    assert m("Postal Code")["value"] == "94122"


def test_phone_extension_stays_empty():
    # NVIDIA Workday regression: "Phone Extension" contains "phone" and got the
    # full number typed into it — it must map to an explicit no-fill, no-review
    got = m("Phone Extension")
    assert got["value"] is None
    assert got["needs_review"] is False
    assert m("Phone")["value"] == "555-0142"         # the real phone rule still fires


def test_right_to_work_phrasings():
    assert m("Do you have the right to work in the United States?", "select", ["Yes", "No"])["value"] == "Yes"
    assert m("Are you legally permitted to work in the US?", "select", ["Yes", "No"])["value"] == "Yes"


# ---- availability / start date ----

def test_start_date_questions_get_earliest_start():
    assert m("When can you start?")["value"] == "September 1, 2026"
    assert m("When do you want to start working?")["value"] == "September 1, 2026"
    assert m("Earliest start date")["value"] == "September 1, 2026"
    assert m("Date available")["value"] == "September 1, 2026"


def test_employment_start_date_still_wins_over_availability():
    # inside a work-experience section, "Start Date Month" is the JOB's start
    prof = dict(PROFILE)
    prof["employment"] = [{"company": "X", "title": "Y", "start_month": 6,
                           "start_year": 2026, "current": True}]
    got = map_standard_field(
        {"id": "f0", "label": "Start Date Month", "name": "", "type": "select",
         "options": ["January", "February", "March", "April", "May", "June", "July",
                     "August", "September", "October", "November", "December"],
         "section": "Work Experience"}, prof)
    assert got["value"] == "June"


# ---- recurring consent questions ----

def test_worked_at_prefers_explicit_not_worked_option():
    got = m("Have you worked at DoorDash?", "select",
            ["I am a previous employee", "I am a current DoorDash employee",
             "I have not worked at DoorDash"])
    assert got["value"] == "I have not worked at DoorDash"


def test_sms_and_privacy_default_yes():
    assert m("Would you like to receive communications via SMS and/or WhatsApp?",
             "select", ["Yes", "No"])["value"] == "Yes"
    assert m("Applicant Privacy Acknowledgement", "select", ["Yes", "No"])["value"] == "Yes"


def test_sms_fine_print_mentioning_email_still_maps_to_consent():
    # the real DoorDash label ends "...we will only communicate with you via
    # email and/or telephone calls" — must NOT map to the email/phone rules
    label = ("Would you like to receive communications via SMS and/or WhatsApp to the "
             "number provided [above or during the application process] about your "
             "application process? Message & data rates may apply and message frequency "
             "may vary. If you select no, we will only communicate with you via email "
             "and/or telephone calls.")
    assert m(label, "select", ["Yes", "No"])["value"] == "Yes"


# ---- EEO presets on verbose vocabularies ----

def test_gender_male():
    assert m("Gender", "checkbox")["value"] == "Male"
    assert m("What is your gender?", "select", ["Man", "Woman", "Decline"])["value"] == "Man"


def test_gender_identity_male():
    # "gender identity" questions use the same answer, across both vocabularies
    assert m("Gender Identity")["value"] == "Male"
    got = m("What gender identity do you most closely identify with?", "select",
            ["Man", "Woman", "Non-binary", "Prefer not to say"])
    assert got["value"] == "Man"
    assert m("Gender Identity", "select", ["Male", "Female", "Decline to state"])["value"] == "Male"


def test_race_two_or_more():
    got = m("Race (*Please select one option that best describes how you identify*)",
            "select", ["Asian", "Two or more races", "White", "I don't wish to answer"])
    assert got["value"] == "Two or more races"
    got = m("Race/Ethnicity", "select",
            ["Asian (Not Hispanic or Latino)", "Two or More Races (Not Hispanic or Latino)"])
    assert got["value"] == "Two or More Races (Not Hispanic or Latino)"


def test_race_checkbox_group_gets_components():
    # "select all that apply" checkbox groups: half East Asian / half White →
    # both components, ";"-joined so fill.js checks one box per part
    got = m("Race and/or Ethnicity (select all that apply)", "checkbox")
    assert got["value"] == "Asian; White"
    assert got["needs_review"] is False


def test_race_radio_group_keeps_umbrella_answer():
    # single-choice renders still get the umbrella answer, not the components
    assert m("Race", "radio")["value"] == "Two or More Races"


def test_disability_yes_verbose():
    got = m("Disability Status", "select",
            ["Yes, I have (or have previously had) a disability",
             "No, I don't have a disability", "I don't wish to answer"])
    assert got["value"] == "Yes, I have (or have previously had) a disability"


def test_hispanic_transgender_lgbtq_no():
    assert m("Are you Hispanic or Latinx?", "select", ["Yes", "No"])["value"] == "No"
    assert m("Do you identify as transgender?", "select", ["Yes", "No"])["value"] == "No"
    assert m("Do you identify as LGBTQ+?", "select", ["Yes", "No", "Prefer not to say"])["value"] == "No"


def test_sexual_orientation_matches_parenthetical():
    got = m("How would you describe your sexual orientation? (mark all that apply)",
            "checkbox", ["Bisexual", "Gay", "Heterosexual (straight)", "Lesbian"])
    assert got["value"] == "Heterosexual (straight)"


def test_veteran_exact():
    got = m("Protected Veteran Status", "select",
            ["I am one or more of the classifications of protected veterans",
             "I am not a protected veteran", "I don't wish to answer"])
    assert got["value"] == "I am not a protected veteran"


# ---- employment section (single-entry forms; engines own multi-entry) ----

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def test_employment_section_rules():
    prof = dict(PROFILE)
    prof["employment"] = [{"company": "Leasing Agent 415 (Compass)",
                           "title": "Freelance Software Engineer",
                           "start_month": 6, "start_year": 2026,
                           "end_month": None, "end_year": None, "current": True}]

    def f(label, **kw):
        return map_standard_field(
            {"id": "f0", "label": label, "name": kw.get("name", ""),
             "type": kw.get("type", "text"), "options": kw.get("options"),
             "section": kw.get("section", "Work Experience")}, prof)

    assert f("Company")["value"] == "Leasing Agent 415 (Compass)"
    assert f("Title")["value"] == "Freelance Software Engineer"
    assert f("Start Date Month", type="select", options=MONTHS)["value"] == "June"
    assert f("Start Date Year", type="select",
             options=[str(y) for y in range(2030, 2000, -1)])["value"] == "2026"
    assert f("I currently work here", type="checkbox")["value"] == "Yes"
    # current role: end date must NOT be answered
    assert f("End Date Year", type="select",
             options=[str(y) for y in range(2030, 2000, -1)]) is None


# ---- month/year date widgets ----

def test_combo_month_field_gets_month_name():
    # react-select month combos expose no options at plan time — the plan must
    # carry the month NAME ("June"), not "06", so the option matcher commits
    got = map_standard_field(
        {"id": "f0", "label": "End Date Month", "name": "", "type": "text",
         "options": None, "section": "Education", "combo": True}, PROFILE)
    assert got["value"] == "June"


def test_text_month_field_still_gets_mm():
    got = map_standard_field(
        {"id": "f0", "label": "End Date Month", "name": "", "type": "text",
         "options": None, "section": "Education", "combo": False}, PROFILE)
    assert got["value"] == "06"


# ---- Coinbase-style compliance screeners (real embed page, 2026-07-05) ----

def test_referred_by_senior_leader_is_not_a_school():
    # REGRESSION: "prospective INSTITUTIONAL client" used to substring-match the
    # school rule and type the university into this yes/no question.
    label = ("To your knowledge, were you referred to this position by a senior leader "
             "or decision‑maker at a current or prospective institutional client, "
             "business partner, or vendor of Coinbase?*")
    got = m(label, "select", ["Yes", "No"])
    assert got["value"] == "No"


def test_school_still_maps_for_legit_labels():
    assert m("School")["value"].startswith("California Polytechnic")
    assert m("School or University")["value"].startswith("California Polytechnic")
    assert m("College/University Name")["value"].startswith("California Polytechnic")


def test_at_least_18():
    assert m("Are you at least 18 years of age?*", "select", ["Yes", "No"])["value"] == "Yes"


def test_previously_been_employed_by_company():
    got = m("Have you previously been employed by Coinbase in any capacity?*",
            "select", ["Yes", "No"])
    assert got["value"] == "No"


def test_government_official_verbose_options():
    got = m("Are you a current government official or were you a government official in "
            "the last five years (e.g., employee of a government agency or a government "
            "owned/controlled company, holder of public office or a civil service position)?*",
            "select", ["No, I am not a current or former Government Official",
                       "Yes, I am a current Government Official",
                       "Yes, I am a former Government Official"])
    assert got["value"] == "No, I am not a current or former Government Official"


def test_relative_of_government_official():
    got = m("Are you a close relative of a government official (i.e., child/step-child, "
            "spouse/partner, parent/guardian, aunt/uncle, first cousin, in-law)?*",
            "select", ["Yes, I am a relative of a government official.",
                       "No, I am not a relative of a government official."])
    assert got["value"] == "No, I am not a relative of a government official."


def test_conflict_of_interest():
    label = ("To your knowledge, do you or a close relative currently hold a role, have a "
             "significant financial interest in, or maintain a close personal relationship "
             "with a senior leader at a company connected to Coinbase that could create an "
             "actual or perceived conflict of interest?*")
    assert m(label, "select", ["Yes", "No"])["value"] == "No"


def test_acknowledgement_confirmed_vocabulary():
    # single-option "Confirmed" select: mapper says Yes, matcher resolves it
    got = m("Please confirm receipt of the above linked Global Data Privacy Notice "
            "and US Arbitration Agreement.*", "select", ["Confirmed"])
    assert got["value"] == "Confirmed"


def test_i_understand_ai_tools_acknowledgement():
    got = m("I understand that Coinbase may use AI tools to assist in the application "
            "and interview process. *", "select", ["Yes"])
    assert got["value"] == "Yes"


def test_how_you_use_ai_tools_preference():
    opts = ["I am opposed to using AI tools in my workflow and prefer traditional/manual workflows.",
            "I do not use AI tools for my work but I am not against it.",
            "I have experimented with AI tools (professionally and/or personally).",
            "I regularly use AI tools to speed up existing tasks (e.g., drafting, summarizing, debugging, basic analysis).",
            "I design or automate workflows with AI tools (e.g., building agents, integrating AI into team processes)."]
    got = m("Which of the following best describes how you use AI tools today?*", "select", opts)
    assert got["value"] == opts[-1]


# ---- files ----

def test_file_fields_route_to_attach_path():
    assert m("Resume/CV", "file")["source"] == "file"
