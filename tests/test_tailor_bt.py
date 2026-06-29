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
    assert "bcat" in g
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


@pytest.mark.skipif(not _have_resumes, reason="personal résumé files not present")
def test_bt_resume_routing_ranker():
    from agents.ranker import _load_resume_summary
    bt = _load_resume_summary("behavioral_technician")
    ai = _load_resume_summary("ai_engineer")
    assert bt != ai
    assert "aba" in bt.lower() or "behavior" in bt.lower()


@pytest.mark.skipif(not _have_resumes, reason="personal résumé files not present")
def test_bt_resume_routing_tailor():
    from agents.tailor import _load_resume_yaml
    bt = _load_resume_yaml("behavioral_technician")
    ai = _load_resume_yaml("ai_analyst")  # non-BT -> AI résumé
    assert bt != ai
    assert "behavior" in bt.lower()
