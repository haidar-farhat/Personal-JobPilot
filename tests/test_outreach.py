"""Unit tests for the outreach drafter — normalizer is pure (no Ollama/network);
cache + get_or_create tests stub the LLM so no real Ollama is needed."""

import agents.outreach as outreach_mod
from agents.outreach import (
    _normalize_outreach,
    _normalize_email,
    _clean_str_list,
    LINKEDIN_NOTE_LIMIT,
    generate_outreach,
    get_or_create_outreach,
)


# --------------------------------------------------------------------------- #
# _normalize_outreach — happy path + shape/type guards
# --------------------------------------------------------------------------- #

def test_normalize_full_valid():
    raw = {
        "linkedin_note": "Hi there — I'm applying to the Platform role and loved your team's work.",
        "recruiter_email": {"subject": "Platform Engineer role", "body": "Hello, I'd love to connect."},
        "hiring_manager_email": {"subject": "Excited about the team", "body": "Hi, I built JobPilot end to end."},
        "talking_points": ["Shipped Trading Bot", "Built JobPilot"],
        "roles_to_contact": ["Engineering Manager, Platform", "Technical Recruiter"],
    }
    out = _normalize_outreach(raw)
    assert out["linkedin_note"].startswith("Hi there")
    assert out["recruiter_email"] == {"subject": "Platform Engineer role", "body": "Hello, I'd love to connect."}
    assert out["hiring_manager_email"]["body"] == "Hi, I built JobPilot end to end."
    assert out["talking_points"] == ["Shipped Trading Bot", "Built JobPilot"]
    assert out["roles_to_contact"] == ["Engineering Manager, Platform", "Technical Recruiter"]


def test_linkedin_note_hard_capped_at_300():
    # Model overshoots the LinkedIn limit — normalizer MUST truncate in code.
    long_note = "x" * 500
    out = _normalize_outreach({"linkedin_note": long_note})
    assert len(out["linkedin_note"]) == LINKEDIN_NOTE_LIMIT == 300


def test_linkedin_note_short_note_untouched():
    out = _normalize_outreach({"linkedin_note": "short and sweet"})
    assert out["linkedin_note"] == "short and sweet"


def test_missing_keys_yield_stable_shape():
    out = _normalize_outreach({})
    assert out == {
        "linkedin_note": "",
        "recruiter_email": {"subject": "", "body": ""},
        "hiring_manager_email": {"subject": "", "body": ""},
        "talking_points": [],
        "roles_to_contact": [],
    }


def test_non_dict_input_is_safe():
    assert _normalize_outreach("nope")["linkedin_note"] == ""
    assert _normalize_outreach(None)["talking_points"] == []
    assert _normalize_outreach(None)["recruiter_email"] == {"subject": "", "body": ""}


def test_email_as_string_not_object_is_safe():
    # Model sometimes emits a bare string where an {subject, body} object is expected.
    out = _normalize_outreach({"recruiter_email": "just a string body"})
    assert out["recruiter_email"] == {"subject": "", "body": ""}


def test_email_as_list_not_object_is_safe():
    out = _normalize_outreach({"hiring_manager_email": ["subject", "body"]})
    assert out["hiring_manager_email"] == {"subject": "", "body": ""}


def test_email_partial_keys_filled():
    out = _normalize_outreach({"recruiter_email": {"subject": "Only subject"}})
    assert out["recruiter_email"] == {"subject": "Only subject", "body": ""}


def test_talking_points_as_object_not_list_is_safe():
    # Model sometimes emits an object instead of an array — must not raise.
    out = _normalize_outreach({"talking_points": {"0": "point"}})
    assert out["talking_points"] == []


def test_roles_as_object_not_list_is_safe():
    out = _normalize_outreach({"roles_to_contact": {"x": "y"}})
    assert out["roles_to_contact"] == []


def test_talking_points_clamped_to_five():
    out = _normalize_outreach({"talking_points": [f"p{i}" for i in range(20)]})
    assert len(out["talking_points"]) == 5


def test_roles_clamped_to_five():
    out = _normalize_outreach({"roles_to_contact": [f"role {i}" for i in range(20)]})
    assert len(out["roles_to_contact"]) == 5


def test_list_items_drop_empty_and_coerce():
    out = _normalize_outreach({"talking_points": ["a", "", "  ", 5, None], "roles_to_contact": []})
    assert out["talking_points"] == ["a", "5"]


def test_non_str_email_fields_coerced():
    out = _normalize_outreach({"recruiter_email": {"subject": 123, "body": None}})
    assert out["recruiter_email"] == {"subject": "123", "body": ""}


def test_normalize_email_direct_non_dict():
    assert _normalize_email(None) == {"subject": "", "body": ""}
    assert _normalize_email("x") == {"subject": "", "body": ""}


def test_clean_str_list_drops_empty_and_caps():
    assert _clean_str_list(["a", "", "  ", "b"], 10) == ["a", "b"]
    assert _clean_str_list(["x"] * 9, 3) == ["x", "x", "x"]
    assert _clean_str_list("notalist", 5) == []


# --------------------------------------------------------------------------- #
# generate_outreach — stubs the LLM + résumé loader (no Ollama, no config files)
# --------------------------------------------------------------------------- #

_FAKE_LLM_OUTPUT = {
    "linkedin_note": "z" * 400,  # overshoots — should be capped by generate_outreach's normalize
    "recruiter_email": {"subject": "s", "body": "b"},
    "hiring_manager_email": {"subject": "s2", "body": "b2"},
    "talking_points": ["built JobPilot", "shipped Trading Bot"],
    "roles_to_contact": ["Engineering Manager, Platform"],
}


def _stub_llm(monkeypatch, calls=None, payload=None):
    """Patch generate_json (as imported into the module) and _load_resume_summary."""
    payload = payload if payload is not None else _FAKE_LLM_OUTPUT

    def fake_generate_json(prompt, system_prompt=""):
        if calls is not None:
            calls.append(1)
        return dict(payload)

    monkeypatch.setattr(outreach_mod, "generate_json", fake_generate_json)
    monkeypatch.setattr(outreach_mod, "_load_resume_summary", lambda archetype=None: "RESUME SUMMARY")


def test_generate_outreach_normalizes_and_caps(monkeypatch):
    _stub_llm(monkeypatch)
    ctx = {"title": "Platform Engineer", "company": "Acme", "archetype": "ai_engineer",
           "key_matches": ["Python"], "key_gaps": ["prod ML"], "fit_score": 72}
    out = generate_outreach(ctx)
    assert len(out["linkedin_note"]) == 300  # hard cap enforced end-to-end
    assert out["role"] == "Platform Engineer"
    assert out["company"] == "Acme"
    assert out["talking_points"] == ["built JobPilot", "shipped Trading Bot"]


def test_generate_outreach_tolerates_bad_ctx_lists(monkeypatch):
    # key_matches/key_gaps given as wrong (non-list) shapes must not raise.
    _stub_llm(monkeypatch)
    out = generate_outreach({"title": "T", "company": "C", "key_matches": "nope", "key_gaps": None})
    assert out["role"] == "T"


# --------------------------------------------------------------------------- #
# Cache round-trip / empty-not-cached / get_or_create — patch cache dir to tmp
# --------------------------------------------------------------------------- #

def _use_tmp_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(outreach_mod, "_OUTPUT_DIR", tmp_path)


def test_cache_round_trips(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    path = outreach_mod._outreach_path(42)
    pack = _normalize_outreach(_FAKE_LLM_OUTPUT)
    outreach_mod._write_cache(path, pack)
    assert path.exists()
    got = outreach_mod._read_cache(path)
    assert got is not None
    assert got["cached"] is True
    assert got["talking_points"] == ["built JobPilot", "shipped Trading Bot"]


def test_empty_pack_is_not_cached(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    empty = _normalize_outreach({})  # no content
    assert outreach_mod._has_content(empty) is False
    calls = []
    _stub_llm(monkeypatch, calls=calls, payload={})  # LLM returns nothing usable
    out = get_or_create_outreach(7, {"title": "T", "company": "C"})
    assert out["cached"] is False
    assert not outreach_mod._outreach_path(7).exists()  # empty pack NOT written


def test_read_cache_ignores_empty_file(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    path = outreach_mod._outreach_path(99)
    outreach_mod._write_cache(path, {"linkedin_note": "", "talking_points": [], "roles_to_contact": []})
    # File exists but has no content → treated as a miss.
    assert outreach_mod._read_cache(path) is None


def test_get_or_create_uses_cache_second_call(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    calls = []
    _stub_llm(monkeypatch, calls=calls)
    ctx = {"title": "Platform Engineer", "company": "Acme"}

    first = get_or_create_outreach(123, ctx)
    assert first["cached"] is False
    assert len(calls) == 1  # LLM called once

    second = get_or_create_outreach(123, ctx)
    assert second["cached"] is True
    assert len(calls) == 1  # served from cache — LLM NOT called again
    assert second["talking_points"] == first["talking_points"]


def test_get_or_create_refresh_bypasses_cache(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    calls = []
    _stub_llm(monkeypatch, calls=calls)
    ctx = {"title": "Platform Engineer", "company": "Acme"}

    get_or_create_outreach(55, ctx)
    assert len(calls) == 1
    get_or_create_outreach(55, ctx, refresh=True)
    assert len(calls) == 2  # refresh forces a regenerate


# --------------------------------------------------------------------------- #
# QA remediation: emails-only packs are content (cache), + contact scrubbing
# --------------------------------------------------------------------------- #

def test_has_content_true_for_emails_only():
    # A pack with only email bodies (no note/points/roles) IS usable content and
    # must be cacheable — mirrors the frontend's non-empty definition.
    pack = _normalize_outreach({"recruiter_email": {"subject": "Role", "body": "Hi, let's connect."}})
    assert outreach_mod._has_content(pack) is True
    hm_only = _normalize_outreach({"hiring_manager_email": {"body": "Hi there."}})
    assert outreach_mod._has_content(hm_only) is True


def test_emails_only_pack_is_cached(monkeypatch, tmp_path):
    # Regression: an emails-only pack used to be treated as "empty" and re-run the
    # LLM on every open. It must now cache and serve from cache on the 2nd call.
    _use_tmp_cache(monkeypatch, tmp_path)
    calls = []
    payload = {
        "linkedin_note": "", "talking_points": [], "roles_to_contact": [],
        "recruiter_email": {"subject": "Role", "body": "Hello, I'd love to connect."},
        "hiring_manager_email": {"subject": "Team", "body": "Hi there, excited about the role."},
    }
    _stub_llm(monkeypatch, calls=calls, payload=payload)
    ctx = {"title": "T", "company": "C"}
    first = get_or_create_outreach(321, ctx)
    assert first["cached"] is False
    assert outreach_mod._outreach_path(321).exists()  # emails-only pack WAS written
    second = get_or_create_outreach(321, ctx)
    assert second["cached"] is True
    assert len(calls) == 1  # served from cache — no second LLM call


def test_scrub_contacts_redacts_email_and_phone():
    s = "Reach me at jane.doe@acme.com or call 555-123-4567 or 15551234567."
    scrubbed = outreach_mod._scrub_contacts(s)
    assert "jane.doe@acme.com" not in scrubbed
    assert "555-123-4567" not in scrubbed
    assert "15551234567" not in scrubbed
    assert "[email removed]" in scrubbed


def test_normalize_scrubs_hallucinated_contacts():
    # Defense-in-depth: even if the model leaks a contact, normalize strips it
    # from both the LinkedIn note and email bodies.
    out = _normalize_outreach({
        "linkedin_note": "Hi, email me at me@example.com!",
        "recruiter_email": {"subject": "Hi", "body": "Call 555-987-6543 to chat."},
    })
    assert "me@example.com" not in out["linkedin_note"]
    assert "555-987-6543" not in out["recruiter_email"]["body"]


def test_scrub_preserves_ordinary_numbers():
    # Years / short figures / salaries must NOT be redacted as phones.
    s = "5 years' experience, shipped 3 projects, targeting $210K roles in 2026."
    assert outreach_mod._scrub_contacts(s) == s
