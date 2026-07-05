"""Deterministic surfaces of the resume optimizer (hiring-agent-style rules)."""

from unittest.mock import patch

from agents.resume_optimizer import (
    CATEGORY_MAX,
    MAX_DEDUCTIONS,
    feedback_block,
    keyword_presence,
    render_resume_text,
    score_materials,
)


class _FakeJob:
    title = "AI Engineer"
    company = "Acme"
    description = "Build LLM agents with Python."


class _FakeScore:
    ats_keywords = ["Python", "LLM agents", "Kubernetes"]


def test_keyword_presence_split():
    present, missing = keyword_presence(
        ["Python", "LLM agents", "Kubernetes"],
        "Built LLM agents in Python with FastAPI.",
    )
    assert present == ["Python", "LLM agents"]
    assert missing == ["Kubernetes"]


def test_keyword_presence_normalizes_case_and_punctuation():
    present, missing = keyword_presence(["CI/CD", "scikit-learn"], "ci/cd pipelines; Scikit-Learn models")
    assert present == ["CI/CD", "scikit-learn"]
    assert missing == []


def test_render_resume_text_includes_all_sections():
    text = render_resume_text({
        "education": [{"degree": "MS Econ", "institution": "Cal Poly", "date": "2025",
                       "coursework": "ML Econometrics"}],
        "skills": {"Languages": "Python, SQL"},
        "project_experience": [{"title": "Trading Bot", "tech_stack": "Python",
                                "bullets": ["Built a bot"]}],
        "work_experience": [{"title": "Engineer", "organization": "Acme", "dates": "2026",
                             "bullets": [{"text": "Shipped a platform"}]}],
        "certifications": [{"name": "Tableau", "year": 2025}],
    })
    for expected in ("MS Econ", "Python, SQL", "Trading Bot", "Shipped a platform", "Tableau"):
        assert expected in text


def _fake_llm(scores=None, deductions=None):
    return {
        "scores": scores or {},
        "deductions": deductions or {"total": 0, "reasons": "none"},
        "missing_keywords": ["Kubernetes"],
        "weak_bullets": ["Built a bot"],
        "improvements": ["Quantify the bot's results"],
        "cover_letter_notes": "fine",
    }


def test_score_caps_enforced_in_python():
    inflated = {cat: {"score": 999, "max": cap, "evidence": "solid evidence"}
                for cat, cap in CATEGORY_MAX.items()}
    with patch("agents.resume_optimizer.generate_json", return_value=_fake_llm(inflated)):
        report = score_materials(_FakeJob(), _FakeScore(), "resume text python", "cover")
    for cat, cap in CATEGORY_MAX.items():
        assert report["scores"][cat]["score"] == cap
    assert report["overall"] == 100  # sum of caps, no deductions


def test_no_evidence_means_low_score():
    no_evidence = {cat: {"score": cap, "max": cap, "evidence": ""}
                   for cat, cap in CATEGORY_MAX.items()}
    with patch("agents.resume_optimizer.generate_json", return_value=_fake_llm(no_evidence)):
        report = score_materials(_FakeJob(), _FakeScore(), "resume", "cover")
    for cat, cap in CATEGORY_MAX.items():
        assert report["scores"][cat]["score"] <= cap * 0.3


def test_deductions_clamped():
    ok = {cat: {"score": cap, "max": cap, "evidence": "e"} for cat, cap in CATEGORY_MAX.items()}
    with patch("agents.resume_optimizer.generate_json",
               return_value=_fake_llm(ok, {"total": 500, "reasons": "stuffing"})):
        report = score_materials(_FakeJob(), _FakeScore(), "resume", "cover")
    assert report["deductions"]["total"] == MAX_DEDUCTIONS
    assert report["overall"] == 100 - MAX_DEDUCTIONS


def test_feedback_block_mentions_missing_keywords_and_weak_bullets():
    fb = feedback_block({
        "overall": 60,
        "missing_keywords": ["Kubernetes"],
        "keywords_missing": [],
        "weak_bullets": ["Built a bot"],
        "improvements": ["Quantify results"],
    })
    assert "Kubernetes" in fb
    assert "Built a bot" in fb
    assert "Quantify results" in fb
