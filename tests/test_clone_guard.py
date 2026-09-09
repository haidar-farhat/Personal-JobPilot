"""Clone guard — catches under-differentiated résumés without blocking honest ones."""

import json

import pytest

from agents.clone_guard import (
    CloneVerdict,
    bullets_of,
    clone_check,
    entries_of,
    fingerprint,
    jaccard,
    load_clone_corpus,
    shingles,
)


CFG = {
    "tailor": {"clone_guard": {
        "enabled": True, "shingle_n": 4,
        "max_shingle_jaccard": 0.60, "max_verbatim_bullet_ratio": 0.50,
        "jd_similarity_exempt": 0.70,
    }},
    "output": {"resumes_dir": "output/resumes"},
}


def _resume(bullets, *, org="Carepool", title="Full-Stack Developer", project="Vigil"):
    return {
        "work_experience": [{"title": title, "organization": org, "bullets": bullets[:2]}],
        "project_experience": [{"title": project, "bullets": bullets[2:]}],
    }


A_BULLETS = [
    "Engineered backend services and REST APIs using Laravel and MySQL for a healthcare platform.",
    "Designed prompt based AI workflows generating structured adaptive recommendations for users.",
    "Architected a vision to incident pipeline connecting camera feeds to geospatial context.",
    "Built an AI powered system automating job discovery and application workflows end to end.",
]
B_BULLETS = [   # lightly reworded — the realistic clone, not a byte copy
    "Engineered backend services and REST APIs using Laravel and MySQL for a healthcare product.",
    "Designed prompt based AI workflows generating structured adaptive recommendations for people.",
    "Architected a vision to incident pipeline connecting camera feeds to geospatial context.",
    "Built an AI powered platform automating job discovery and application workflows end to end.",
]
C_BULLETS = [   # genuinely different emphasis
    "Modelled spatial incident density across municipal camera networks to prioritise response.",
    "Tuned local GPU inference throughput, cutting per frame latency on commodity hardware.",
    "Normalised multi source geospatial feeds into one queryable incident layer.",
    "Instrumented evaluation harnesses comparing model variants on labelled incident footage.",
]


# ------------------------------------------------------------------ helpers --

def test_bullets_and_entries_extracted_in_render_order():
    r = _resume(A_BULLETS)
    assert len(bullets_of(r)) == 4
    ents = entries_of(r)
    assert "full-stack developer@carepool" in ents
    assert "vigil" in ents


def test_shingles_are_word_ngrams():
    s = shingles(["alpha beta gamma delta epsilon"], n=4)
    assert "alpha beta gamma delta" in s and "beta gamma delta epsilon" in s


def test_short_bullet_still_contributes():
    assert shingles(["two words"], n=4) == {"two words"}


def test_jaccard_bounds():
    assert jaccard(set(), {"a"}) == 0.0
    assert jaccard({"a"}, {"a"}) == 1.0
    assert jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)


def test_dict_bullets_supported():
    r = {"work_experience": [{"title": "T", "organization": "O",
                              "bullets": [{"text": "alpha beta gamma delta"}]}]}
    assert bullets_of(r) == ["alpha beta gamma delta"]


# ------------------------------------------------------------------ verdict --

def test_reworded_clone_is_flagged():
    v = clone_check(_resume(A_BULLETS), [fingerprint(_resume(B_BULLETS), name="OtherJob")], CFG)
    assert v.flagged
    assert v.nearest == "OtherJob"
    assert v.similarity > 0.60


def test_genuinely_different_resume_is_clean():
    v = clone_check(_resume(A_BULLETS), [fingerprint(_resume(C_BULLETS), name="OtherJob")], CFG)
    assert not v.flagged
    assert v.similarity < 0.60


def test_verbatim_bullets_alone_trigger_the_flag():
    """Identical bullets in a different order must still be caught."""
    v = clone_check(_resume(A_BULLETS),
                    [fingerprint(_resume(list(reversed(A_BULLETS))), name="Other")], CFG)
    assert v.flagged
    assert v.verbatim_bullet_ratio == pytest.approx(1.0)


def test_empty_corpus_is_clean():
    assert clone_check(_resume(A_BULLETS), [], CFG).flagged is False


def test_disabled_guard_returns_clean():
    cfg = {"tailor": {"clone_guard": {"enabled": False}}}
    v = clone_check(_resume(A_BULLETS), [fingerprint(_resume(A_BULLETS), name="X")], cfg)
    assert not v.flagged


def test_empty_resume_is_clean_not_crash():
    assert clone_check({}, [fingerprint(_resume(A_BULLETS), name="X")], CFG).flagged is False


# ---------------------------------------------------------------- exemption --

def test_near_identical_jds_are_exempt():
    """Two nearly identical postings SHOULD yield similar résumés."""
    terms = {"python", "llm", "rag", "agents", "fastapi"}
    other = fingerprint(_resume(B_BULLETS), name="Twin", jd_terms=terms)
    v = clone_check(_resume(A_BULLETS), [other], CFG, jd_terms=terms)
    assert v.exempt
    assert not v.flagged


def test_different_jds_are_not_exempt():
    other = fingerprint(_resume(B_BULLETS), name="Other",
                        jd_terms={"salesforce", "crm", "quota", "territory"})
    v = clone_check(_resume(A_BULLETS), [other],
                    CFG, jd_terms={"python", "llm", "rag", "agents"})
    assert not v.exempt
    assert v.flagged


def test_exemption_needs_jd_terms_on_both_sides():
    """No JD terms means no exemption — absence of evidence isn't evidence."""
    v = clone_check(_resume(A_BULLETS), [fingerprint(_resume(B_BULLETS), name="X")], CFG)
    assert not v.exempt
    assert v.flagged


# ----------------------------------------------------------------- feedback --

def test_feedback_names_shared_phrases_and_never_invents():
    v = clone_check(_resume(A_BULLETS), [fingerprint(_resume(B_BULLETS), name="Gusto_AI")], CFG)
    fb = v.feedback
    assert "Gusto_AI" in fb
    assert "Do NOT add anything new" in fb
    assert v.shared_shingles and v.shared_shingles[0] in fb


def test_clean_verdict_has_no_feedback():
    assert CloneVerdict().feedback == ""


def test_verdict_serialises():
    d = clone_check(_resume(A_BULLETS),
                    [fingerprint(_resume(B_BULLETS), name="X")], CFG).as_dict()
    assert d["flagged"] is True
    assert isinstance(d["similarity"], float)
    json.dumps(d)   # must be JSON-safe for the sidecar/report


# -------------------------------------------------------------------- corpus --

def test_corpus_excludes_the_jobs_own_previous_output(tmp_path):
    d = tmp_path / "resumes"
    d.mkdir()
    for name in ("Acme_Engineer", "Beta_Analyst"):
        (d / f"{name}_resume.json").write_text(json.dumps(_resume(A_BULLETS)), encoding="utf-8")
    cfg = {"output": {"resumes_dir": str(d)}}

    # _resumes_dir resolves relative to the repo root, so point it at an absolute path
    import agents.clone_guard as cg
    orig = cg._resumes_dir
    cg._resumes_dir = lambda config: d
    try:
        names = {f.name for f in load_clone_corpus(cfg, exclude_base="Acme_Engineer")}
        assert names == {"Beta_Analyst"}
    finally:
        cg._resumes_dir = orig


def test_missing_corpus_dir_returns_empty():
    assert load_clone_corpus({"output": {"resumes_dir": "does/not/exist"}}) == []


def test_corrupt_cache_is_a_miss_not_an_error(tmp_path):
    d = tmp_path / "resumes"
    d.mkdir()
    (d / "X_resume.json").write_text(json.dumps(_resume(A_BULLETS)), encoding="utf-8")
    (d / ".fingerprints.json").write_text("{not json", encoding="utf-8")
    import agents.clone_guard as cg
    orig = cg._resumes_dir
    cg._resumes_dir = lambda config: d
    try:
        assert len(load_clone_corpus({}, exclude_base="")) == 1
    finally:
        cg._resumes_dir = orig
