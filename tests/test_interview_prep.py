"""Unit tests for the interview-prep normalizer (pure, no Ollama/network)."""

from agents.interview_prep import _normalize_prep, _clean_str_list


def test_normalize_full_valid():
    raw = {
        "questions": [
            {"question": "Tell me about a hard bug.", "type": "behavioral",
             "why": "gauge grit", "strategy": "use Trading Bot", "points": ["a", "b"]},
        ],
        "talking_points": ["ship fast"],
        "gaps_to_address": [{"gap": "no prod ML", "reframe": "built JobPilot end to end"}],
        "questions_to_ask": ["what's the team size?"],
    }
    out = _normalize_prep(raw)
    assert len(out["questions"]) == 1
    q = out["questions"][0]
    assert q["type"] == "behavioral"
    assert q["points"] == ["a", "b"]
    assert out["talking_points"] == ["ship fast"]
    assert out["gaps_to_address"][0]["gap"] == "no prod ML"
    assert out["questions_to_ask"] == ["what's the team size?"]


def test_unknown_type_falls_back_to_role_specific():
    out = _normalize_prep({"questions": [{"question": "Q?", "type": "brainteaser"}]})
    assert out["questions"][0]["type"] == "role-specific"


def test_type_is_normalized_case_and_space():
    out = _normalize_prep({"questions": [{"question": "Q?", "type": "Role Specific"}]})
    assert out["questions"][0]["type"] == "role-specific"


def test_questions_without_text_are_dropped():
    out = _normalize_prep({"questions": [{"type": "technical"}, {"question": "  "}, "notadict"]})
    assert out["questions"] == []


def test_missing_keys_yield_empty_lists():
    out = _normalize_prep({})
    assert out == {"questions": [], "talking_points": [], "gaps_to_address": [], "questions_to_ask": []}


def test_non_dict_input_is_safe():
    assert _normalize_prep("nope")["questions"] == []
    assert _normalize_prep(None)["talking_points"] == []


def test_questions_clamped_to_eight():
    raw = {"questions": [{"question": f"Q{i}", "type": "technical"} for i in range(20)]}
    assert len(_normalize_prep(raw)["questions"]) == 8


def test_points_clamped_to_four():
    raw = {"questions": [{"question": "Q?", "points": [str(i) for i in range(10)]}]}
    assert len(_normalize_prep(raw)["questions"][0]["points"]) == 4


def test_gap_without_gap_text_dropped():
    out = _normalize_prep({"gaps_to_address": [{"reframe": "orphan"}, {"gap": "real", "reframe": "ok"}]})
    assert len(out["gaps_to_address"]) == 1
    assert out["gaps_to_address"][0]["gap"] == "real"


def test_clean_str_list_drops_empty_and_caps():
    assert _clean_str_list(["a", "", "  ", "b"], 10) == ["a", "b"]
    assert _clean_str_list(["x"] * 9, 3) == ["x", "x", "x"]
    assert _clean_str_list("notalist", 5) == []


def test_questions_as_object_not_list_is_safe():
    # Model sometimes emits an object instead of an array — must not raise (FG-4)
    out = _normalize_prep({"questions": {"0": {"question": "Q?"}}})
    assert out["questions"] == []


def test_gaps_as_object_not_list_is_safe():
    out = _normalize_prep({"gaps_to_address": {"x": "y"}})
    assert out["gaps_to_address"] == []
