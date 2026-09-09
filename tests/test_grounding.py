"""Provenance gate — nothing unsupported by the base résumé may ship.

Regression suite for the 2026-09-09 audit: 158 of 197 shipped résumés claimed
Pandas/NumPy/scikit-learn/Statsmodels, 3 claimed a Tableau Desktop Specialist
certification and 1 claimed Reed College — none present in base_resume.yaml.
"""

import pytest

from agents.grounding import (
    GroundingReport,
    base_vocabulary,
    detect_template_leaks,
    enforce,
    _phrase_supported,
    _singular,
    _tokens,
)


BASE = {
    "contact": {"name": "Haidar Farhat"},
    "education": [
        {"institution": "Lebanese International University",
         "degree": "B.S. in Computer Science", "date": "Jan 2025"},
    ],
    "skills": {
        "Languages": ["Python", "TypeScript", "PHP", "Rust", "SQL"],
        "AI / ML": ["Computer Vision", "LLMs", "RAG", "Prompt Engineering"],
        "AI Tools & Infrastructure": ["Ollama", "Gemini API", "OpenAI-compatible APIs"],
        "Backend": ["Laravel", "Node.js", "REST APIs"],
    },
    "work_experience": [
        {"title": "Full-Stack Developer", "organization": "Carepool",
         "bullets": ["Built healthcare coordination features in Laravel and MySQL."]},
    ],
    "project_experience": [
        {"title": "Vigil", "bullets": ["Computer vision platform with local GPU inference."]},
    ],
    "certifications": [{"name": "Cisco CCNA", "year": ""}],
}


# --------------------------------------------------------------- tokenizer --

@pytest.mark.parametrize("word,expected", [
    ("apis", "api"), ("agents", "agent"), ("aws", "aws"),
    ("css", "css"), ("analysis", "analysis"), ("llms", "llm"),
])
def test_singular_only_trims_real_plurals(word, expected):
    assert _singular(word) == expected


def test_hyphenated_compound_contributes_its_parts():
    toks = _tokens("OpenAI-compatible APIs")
    assert "openai" in toks and "api" in toks


def test_stopwords_are_not_claims():
    assert not (_tokens("built with the data") & {"built", "with", "the", "data"})


# ------------------------------------------------------------- phrase gate --

@pytest.mark.parametrize("phrase", [
    "Python", "Laravel", "Computer Vision", "Ollama", "REST APIs",
    "OpenAI API",          # base says "OpenAI-compatible APIs" — same claim
    "Cisco CCNA",
])
def test_supported_phrases_pass(phrase):
    assert _phrase_supported(phrase, base_vocabulary(BASE))


@pytest.mark.parametrize("phrase", [
    "Pandas", "NumPy", "scikit-learn", "Statsmodels", "TensorFlow", "PyTorch",
    "Tableau Desktop Specialist", "CFA Institute", "Kubernetes", "FastAPI",
])
def test_unsupported_phrases_fail(phrase):
    assert not _phrase_supported(phrase, base_vocabulary(BASE))


def test_multiword_needs_every_token():
    """'machine learning' must not pass on an unrelated 'learning' elsewhere."""
    vocab = base_vocabulary({"skills": {"x": ["Deep Learning Theory"]}})
    assert not _phrase_supported("Machine Learning", vocab)


# -------------------------------------------------------------- enforce() ---

def _generated(**over):
    data = {
        "skills": {"Languages": "Python, SQL", "Data & ML": "Pandas, NumPy, scikit-learn"},
        "education": [{"institution": "Lebanese International University",
                       "degree": "B.S. in Computer Science"}],
        "certifications": [{"name": "Cisco CCNA", "year": ""},
                           {"name": "Tableau Desktop Specialist", "year": "2025"}],
        "work_experience": [{"title": "Full-Stack Developer", "organization": "Carepool",
                             "bullets": ["Built REST APIs in Laravel."]}],
        "project_experience": [{"title": "Vigil", "bullets": ["Computer vision pipeline."]}],
    }
    data.update(over)
    return data


def test_fabricated_skills_are_stripped():
    cleaned, report = enforce(_generated(), BASE)
    assert cleaned["skills"]["Languages"] == "Python, SQL"
    assert "Data & ML" not in cleaned["skills"]      # emptied category removed
    assert any("Pandas" in s for s in report.removed_skills)
    assert any("scikit-learn" in s for s in report.removed_skills)


def test_fabricated_certification_is_stripped_and_real_one_kept():
    cleaned, report = enforce(_generated(), BASE)
    names = [c["name"] for c in cleaned["certifications"]]
    assert names == ["Cisco CCNA"]
    assert report.removed_certs == ["Tableau Desktop Specialist"]


def test_fabricated_education_is_stripped():
    data = _generated(education=[{"institution": "Reed College",
                                  "degree": "MS Quantitative Economics"}])
    cleaned, report = enforce(data, BASE)
    assert cleaned["education"] == []
    assert report.removed_education


def test_invented_employer_is_stripped():
    data = _generated(work_experience=[
        {"title": "Data Scientist", "organization": "Goldman Sachs", "bullets": ["x"]}])
    cleaned, report = enforce(data, BASE)
    assert cleaned["work_experience"] == []
    assert report.removed_entries


def test_real_content_survives_untouched():
    data = {
        "skills": {"Languages": "Python, Rust"},
        "education": BASE["education"],
        "certifications": [{"name": "Cisco CCNA", "year": ""}],
        "work_experience": [{"title": "Full-Stack Developer", "organization": "Carepool",
                             "bullets": ["Built REST APIs in Laravel."]}],
        "project_experience": [],
    }
    cleaned, report = enforce(data, BASE)
    assert report.clean
    assert cleaned["skills"] == {"Languages": "Python, Rust"}
    assert len(cleaned["work_experience"]) == 1


def test_enforce_does_not_mutate_input():
    data = _generated()
    before = data["skills"]["Data & ML"]
    enforce(data, BASE)
    assert data["skills"]["Data & ML"] == before


def test_empty_base_refuses_rather_than_approving_everything():
    with pytest.raises(ValueError):
        enforce(_generated(), {})


def test_bullets_are_flagged_not_deleted():
    """Reframing prose is legitimate; fabricated specifics are still reported."""
    data = _generated(work_experience=[{
        "title": "Full-Stack Developer", "organization": "Carepool",
        "bullets": ["Deployed models on Kubernetes with TensorFlow serving."]}])
    cleaned, report = enforce(data, BASE)
    assert len(cleaned["work_experience"][0]["bullets"]) == 1   # kept
    flagged = " ".join(t for b in report.flagged_bullets for t in b["unsupported_terms"])
    assert "kubernetes" in flagged and "tensorflow" in flagged


# ------------------------------------------------------- template leakage ---

def test_template_placeholders_are_detected():
    leaks = detect_template_leaks({"contact": {"name": "Jane Doe"},
                                   "education": [{"institution": "Your University"}]})
    assert "jane doe" in leaks and "your university" in leaks


def test_clean_resume_reports_no_leaks():
    assert detect_template_leaks({"contact": {"name": "Haidar Farhat"}}) == []


def test_report_summary_is_readable():
    r = GroundingReport()
    assert "no unsupported claims" in r.summary()
    r.removed_certs.append("Tableau Desktop Specialist")
    assert "1 cert" in r.summary()
