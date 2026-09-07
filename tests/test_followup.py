"""Unit tests for the follow-up nudge drafter — normalizer is pure (no Ollama);
cache + get_or_create tests stub the LLM so no real Ollama is needed."""

import agents.followup as followup_mod
from agents.followup import (
    _normalize_followup,
    LINKEDIN_DM_LIMIT,
    generate_followup,
    get_or_create_followup,
)


# --------------------------------------------------------------------------- #
# _normalize_followup — happy path + shape/type guards
# --------------------------------------------------------------------------- #

def test_normalize_full_valid():
    raw = {
        "followup_email": {"subject": "Following up — Platform role", "body": "Hi there, checking in."},
        "linkedin_message": "Hi! Just following up on my application.",
        "tips": ["Send Tuesday morning", "Email first, DM if quiet"],
    }
    out = _normalize_followup(raw)
    assert out["followup_email"] == {"subject": "Following up — Platform role", "body": "Hi there, checking in."}
    assert out["linkedin_message"].startswith("Hi!")
    assert out["tips"] == ["Send Tuesday morning", "Email first, DM if quiet"]


def test_dm_hard_capped():
    out = _normalize_followup({"linkedin_message": "x" * 2000})
    assert len(out["linkedin_message"]) == LINKEDIN_DM_LIMIT == 800


def test_missing_keys_yield_stable_shape():
    out = _normalize_followup({})
    assert out == {"followup_email": {"subject": "", "body": ""}, "linkedin_message": "", "tips": []}


def test_non_dict_input_is_safe():
    assert _normalize_followup(None)["linkedin_message"] == ""
    assert _normalize_followup("nope")["followup_email"] == {"subject": "", "body": ""}


def test_email_as_string_not_object_is_safe():
    out = _normalize_followup({"followup_email": "just a string body"})
    assert out["followup_email"] == {"subject": "", "body": ""}


def test_hallucinated_contacts_scrubbed():
    # Defense-in-depth: a fabricated email/phone must never reach the UI.
    raw = {
        "followup_email": {"subject": "s", "body": "Reach me at jane.doe@acme.com or 415-555-1234."},
        "linkedin_message": "Ping recruiter@acme.com!",
        "tips": ["Call 650-555-9999 directly"],
    }
    out = _normalize_followup(raw)
    assert "@" not in out["followup_email"]["body"]
    assert "@" not in out["linkedin_message"]
    assert "650-555-9999" not in out["tips"][0]


def test_tips_clamped_to_three():
    out = _normalize_followup({"tips": [f"tip {i}" for i in range(10)]})
    assert len(out["tips"]) == 3


# --------------------------------------------------------------------------- #
# generate_followup — stubs the LLM + résumé loader (no Ollama, no config files)
# --------------------------------------------------------------------------- #

_FAKE_LLM_OUTPUT = {
    "followup_email": {"subject": "Following up", "body": "Hi there, still very interested."},
    "linkedin_message": "z" * 1200,  # overshoots — should be capped end-to-end
    "tips": ["Tuesday works best"],
}


def _stub_llm(monkeypatch, calls=None, payload=None):
    payload = payload if payload is not None else _FAKE_LLM_OUTPUT

    def fake_generate_json(prompt, system_prompt=""):
        if calls is not None:
            calls.append(1)
        return dict(payload)

    monkeypatch.setattr(followup_mod, "generate_json", fake_generate_json)
    monkeypatch.setattr(followup_mod, "_load_resume_summary", lambda archetype=None: "RESUME SUMMARY")


def test_generate_followup_caps_and_annotates(monkeypatch):
    _stub_llm(monkeypatch)
    ctx = {"title": "Platform Engineer", "company": "Acme", "days_since_applied": 12,
           "key_matches": ["Python"], "fit_score": 72, "status": "applied"}
    out = generate_followup(ctx)
    assert len(out["linkedin_message"]) == LINKEDIN_DM_LIMIT
    assert out["role"] == "Platform Engineer"
    assert out["days_since_applied"] == 12


def test_generate_followup_tolerates_bad_ctx(monkeypatch):
    _stub_llm(monkeypatch)
    out = generate_followup({"title": "T", "company": "C", "key_matches": "nope",
                             "days_since_applied": "soon"})
    assert out["days_since_applied"] is None  # non-int coerced to None, no raise


# --------------------------------------------------------------------------- #
# Cache round-trip / empty-not-cached / get_or_create — patch cache dir to tmp
# --------------------------------------------------------------------------- #

def _use_tmp_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(followup_mod, "_OUTPUT_DIR", tmp_path)


def test_cache_round_trips(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    path = followup_mod._followup_path(42)
    pack = _normalize_followup(_FAKE_LLM_OUTPUT)
    followup_mod._write_cache(path, pack)
    got = followup_mod._read_cache(path)
    assert got is not None and got["cached"] is True


def test_empty_pack_is_not_cached(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    _stub_llm(monkeypatch, payload={})
    out = get_or_create_followup(7, {"title": "T", "company": "C"})
    assert out["cached"] is False
    assert not followup_mod._followup_path(7).exists()


def test_get_or_create_uses_cache_second_call(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    calls = []
    _stub_llm(monkeypatch, calls=calls)
    ctx = {"title": "Platform Engineer", "company": "Acme"}

    first = get_or_create_followup(123, ctx)
    assert first["cached"] is False and len(calls) == 1

    second = get_or_create_followup(123, ctx)
    assert second["cached"] is True and len(calls) == 1  # served from cache
