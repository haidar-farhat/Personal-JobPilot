"""Archetype-aware tailoring guidance + prompt-format integrity."""

import pytest

from agents.tailor import (
    _archetype_guidance,
    RESUME_PROMPT_TEMPLATE,
    COVER_LETTER_PROMPT_TEMPLATE,
)


def test_bt_guidance_leads_with_behavioral():
    g = _archetype_guidance("behavioral_technician").lower()
    assert "behavior" in g
    # Certifications are referenced by ROLE, never by name. This used to assert
    # "bcat" — a certification belonging to a previous candidate — which is
    # exactly the class of concrete noun that leaked into shipped résumés.
    assert "certification" in g
    assert "work_experience" in g  # lead with experience, before projects


def test_ai_guidance_mentions_ai_tools():
    g = _archetype_guidance("ai_engineer").lower()
    assert "ai" in g
    assert "agents" in g or "prompt engineering" in g


def test_default_guidance_is_string():
    assert isinstance(_archetype_guidance("data_analyst"), str)
    assert isinstance(_archetype_guidance(None), str)


def test_resume_prompt_formats_with_guidance():
    """The new {archetype_guidance} placeholder + escaped JSON braces stay consistent."""
    out = RESUME_PROMPT_TEMPLATE.format(
        resume_yaml="x", job_title="AI Engineer", company="Acme", location="SF",
        job_description="d", ats_keywords="k", key_matches="m", key_gaps="g",
        archetype_guidance=_archetype_guidance("ai_engineer"),
    )
    assert "ARCHETYPE GUIDANCE" in out
    assert '"section_order"' in out  # JSON braces survived .format()


def test_cover_prompt_formats_with_guidance():
    out = COVER_LETTER_PROMPT_TEMPLATE.format(
        name="Matthew", email="e", phone="p", candidate_location="SF",
        job_title="Behavior Technician", company="SFUSD", location="SF",
        job_description="d", key_matches="m",
        archetype_guidance=_archetype_guidance("behavioral_technician"),
    )
    assert "ARCHETYPE GUIDANCE" in out


# --- BT résumé routing (SFUSD résumé for behavioral_technician) ---
from pathlib import Path as _Path  # noqa: E402

_CFG = _Path(__file__).resolve().parents[1] / "config"
_have_resumes = (_CFG / "base_resume_bt.yaml").exists() and (_CFG / "base_resume.yaml").exists()


# BT routing is CONDITIONAL on a real BT résumé existing. These assertions used
# to require bt != ai unconditionally, which passed while base_resume_bt.yaml was
# still the unedited "Jane Doe" example — i.e. the test certified the bug. The
# contract is now: route to the BT résumé when it is real, fall back otherwise,
# and never source a template.

def _bt_resume_is_real() -> bool:
    from agents.tailor import _is_unedited_template
    p = _CFG / "base_resume_bt.yaml"
    return p.exists() and not _is_unedited_template(p.read_text(encoding="utf-8"))


@pytest.mark.skipif(not _have_resumes, reason="personal résumé files not present")
def test_bt_resume_routing_ranker():
    from agents.ranker import _load_resume_summary
    bt = _load_resume_summary("behavioral_technician")
    ai = _load_resume_summary("ai_engineer")
    if _bt_resume_is_real():
        assert bt != ai
        assert "aba" in bt.lower() or "behavior" in bt.lower()
    else:
        assert bt == ai   # falls back rather than scoring a placeholder identity


@pytest.mark.skipif(not _have_resumes, reason="personal résumé files not present")
def test_bt_resume_routing_tailor():
    from agents.tailor import _load_resume_yaml
    bt = _load_resume_yaml("behavioral_technician")
    ai = _load_resume_yaml("ai_analyst")  # non-BT -> AI résumé
    if _bt_resume_is_real():
        assert bt != ai
        assert "behavior" in bt.lower()
    else:
        assert bt == ai


# --- prompt hygiene: no other candidate's nouns may live in this module ------
# Regression guard for the 2026-09-09 audit: RESUME_PROMPT_TEMPLATE shipped a
# worked example carrying a previous candidate's skills and certifications, and
# the model copied them into 158 of 197 résumés.

_FOREIGN_NOUNS = [
    "pandas", "numpy", "scikit-learn", "statsmodels", "tensorflow", "pytorch",
    "tableau desktop specialist", "cfa institute", "reed", "bcat",
    "quantitative economics", "matthew", "cromaz", "sfusd",
    "algorithmic paper trading",
]


@pytest.mark.parametrize("noun", _FOREIGN_NOUNS)
def test_prompt_templates_name_no_foreign_credentials(noun):
    """No concrete skill/school/certification may appear in the prompt text."""
    blob = " ".join([
        RESUME_PROMPT_TEMPLATE,
        COVER_LETTER_PROMPT_TEMPLATE,
        *[_archetype_guidance(a) for a in
          (None, "behavioral_technician", "ai_engineer", "ai_solutions_engineer",
           "ai_analyst", "ml_engineer", "data_analyst")],
    ]).lower()
    assert noun not in blob, f"{noun!r} leaked back into a tailor prompt"


def test_bt_route_rejects_unedited_template():
    """An unfilled base_resume_bt.yaml must never be used as source material."""
    from agents.tailor import _is_unedited_template, _resolve_resume_path
    assert _is_unedited_template("contact:\n  name: \"Jane Doe\"\n")
    assert not _is_unedited_template("contact:\n  name: \"Haidar Farhat\"\n")
    # Whatever the config state, the BT route never returns a template file.
    path = _resolve_resume_path("behavioral_technician")
    assert not _is_unedited_template(path.read_text(encoding="utf-8"))
