"""Open-ended answer drafting: the LLM prompt must carry the job description,
the profile's essay_facts, and the résumé — and fall back to templates when
Ollama is down. (2026-07-05: 'answer open ended textual questions based on the
job description ... and context about me'.)"""

import server.autofill as af
from agents.autofill_mapper import build_plan

PROFILE = {
    "essay_facts": {
        "location": "San Francisco, CA",
        "earliest_start_date": "September 1, 2026",
        "biggest_accomplishment": "Solo-built an agentic job-search platform.",
    },
    "fallback_essays": {
        "why_company": "I'm drawn to {company} because of X.",
    },
}

FIELD = {"id": "f9", "label": "Why do you want to work here?", "type": "textarea"}


def test_prompt_carries_jd_facts_and_resume(monkeypatch):
    captured = {}

    def fake_generate(prompt, **kw):
        captured["prompt"] = prompt
        return "Because my actual experience matches."

    monkeypatch.setattr(af, "generate_text", fake_generate)
    out = af._draft_answer(FIELD, PROFILE, "RESUME BODY HERE", "Analytics Engineer",
                           "Coinbase", "We build data foundations for compliance ops.")
    assert out == "Because my actual experience matches."
    p = captured["prompt"]
    assert "Why do you want to work here?" in p
    assert "We build data foundations" in p          # job description excerpt
    assert "September 1, 2026" in p                   # essay_facts
    assert "Solo-built an agentic job-search platform." in p
    assert "RESUME BODY HERE" in p
    assert "never invent" in p                        # grounding instruction


def test_ollama_down_falls_back_to_template(monkeypatch):
    def boom(prompt, **kw):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(af, "generate_text", boom)
    out = af._draft_answer(FIELD, PROFILE, "resume", "Engineer", "Coinbase", "")
    assert out == "I'm drawn to Coinbase because of X."


def test_accomplishment_question_falls_back_to_fact(monkeypatch):
    def boom(prompt, **kw):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(af, "generate_text", boom)
    out = af._draft_answer({"id": "f1", "label": "What is your biggest accomplishment",
                            "type": "text"}, PROFILE, "resume", "", "", "")
    assert out == "Solo-built an agentic job-search platform."


def test_build_plan_routes_questionlike_text_inputs_to_essay():
    calls = []

    def essay_fn(field, ctx):
        calls.append(field["label"])
        return "drafted answer"

    fields = [
        {"id": "f0", "label": "Describe your biggest professional accomplishment",
         "type": "text", "options": None},           # 5+ words, no "?" — still open
        {"id": "f1", "label": "Nickname", "type": "text", "options": None},  # not open
    ]
    plan = build_plan(fields, {}, None, "resume", essay_fn=essay_fn)
    by_id = {f["id"]: f for f in plan["fields"]}
    assert calls == ["Describe your biggest professional accomplishment"]
    assert by_id["f0"]["value"] == "drafted answer"
    assert by_id["f0"]["needs_review"] is True        # AI drafts always get review
    assert by_id["f1"]["value"] is None
