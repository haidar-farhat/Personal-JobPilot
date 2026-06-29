"""Archetype-aware tailoring guidance + prompt-format integrity."""

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
