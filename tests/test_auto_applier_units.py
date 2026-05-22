"""Unit tests for the auto-applier — pure logic, no browser, no live server."""

import sys
from pathlib import Path

import pytest

# Make project root importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ============================================================
# Dispatch tests
# ============================================================

class TestDispatch:
    """The runner picks the right applier for each ATS source identifier."""

    def test_greenhouse_source_maps_to_greenhouse_applier(self):
        from agents.auto_applier.runner import _applier_for_source
        from agents.auto_applier.greenhouse import GreenhouseAutoApplier
        assert _applier_for_source("greenhouse:anthropic") is GreenhouseAutoApplier

    def test_ashby_source_maps_to_ashby_applier(self):
        from agents.auto_applier.runner import _applier_for_source
        from agents.auto_applier.ashby import AshbyAutoApplier
        assert _applier_for_source("ashby:replit") is AshbyAutoApplier

    def test_lever_source_maps_to_lever_applier(self):
        from agents.auto_applier.runner import _applier_for_source
        from agents.auto_applier.lever import LeverAutoApplier
        assert _applier_for_source("lever:netflix") is LeverAutoApplier

    def test_workday_source_maps_to_workday_applier(self):
        from agents.auto_applier.runner import _applier_for_source
        from agents.auto_applier.workday import WorkdayAutoApplier
        assert _applier_for_source("workday:kaiser") is WorkdayAutoApplier

    def test_custom_source_maps_to_generic_applier(self):
        from agents.auto_applier.runner import _applier_for_source
        from agents.auto_applier.generic import GenericAutoApplier
        assert _applier_for_source("custom:apple") is GenericAutoApplier

    def test_unknown_source_falls_back_to_generic(self):
        from agents.auto_applier.runner import _applier_for_source
        from agents.auto_applier.generic import GenericAutoApplier
        assert _applier_for_source("brand_new_ats:foo") is GenericAutoApplier

    def test_none_source_falls_back_to_generic(self):
        from agents.auto_applier.runner import _applier_for_source
        from agents.auto_applier.generic import GenericAutoApplier
        assert _applier_for_source(None) is GenericAutoApplier


# ============================================================
# Per-ATS threshold tests
# ============================================================

class TestPerAtsThresholds:

    def test_greenhouse_min_score_75(self):
        from agents.auto_applier.runner import _min_score_for_ats
        from agents.auto_applier.base import reload_profile
        assert _min_score_for_ats("greenhouse", reload_profile()) == 75

    def test_workday_min_score_80(self):
        from agents.auto_applier.runner import _min_score_for_ats
        from agents.auto_applier.base import reload_profile
        assert _min_score_for_ats("workday", reload_profile()) == 80

    def test_generic_min_score_85(self):
        from agents.auto_applier.runner import _min_score_for_ats
        from agents.auto_applier.base import reload_profile
        assert _min_score_for_ats("generic", reload_profile()) == 85

    def test_unknown_ats_falls_back_to_global_min(self):
        from agents.auto_applier.runner import _min_score_for_ats
        from agents.auto_applier.base import reload_profile
        # Unknown ATS keys should fall back to the global guardrails.min_score (75)
        assert _min_score_for_ats("never_heard_of_this", reload_profile()) == 75


# ============================================================
# Essay-question classifier tests
# ============================================================

class TestEssayClassifier:
    """The classifier picks the right fallback template based on question text."""

    def test_why_company_questions(self):
        from agents.auto_applier.base import _classify_essay_question
        # Plain "company"/"us"/"team" phrasings — no company name needed
        for q in (
            "Why do you want to work for our company?",
            "Tell us about your interest in our company",
            "Why us?",
            "Why this company?",
            "Why are you interested in joining our team?",
        ):
            assert _classify_essay_question(q) == "why_company", f"failed: {q!r}"

        # When the question names the company directly, classifier needs the
        # company name to disambiguate from a why_role question.
        assert _classify_essay_question(
            "Why are you interested in Anthropic?", company="Anthropic"
        ) == "why_company"

    def test_why_role_questions(self):
        from agents.auto_applier.base import _classify_essay_question
        for q in (
            "Why this role?",
            "Why are you applying for this position?",
            "Why interested in this position?",
        ):
            assert _classify_essay_question(q) == "why_role", f"failed: {q!r}"

    def test_strength_questions(self):
        from agents.auto_applier.base import _classify_essay_question
        assert _classify_essay_question("What is your greatest strength?") == "greatest_strength"
        assert _classify_essay_question("What are you good at?") == "greatest_strength"

    def test_weakness_questions(self):
        from agents.auto_applier.base import _classify_essay_question
        assert _classify_essay_question("What is your greatest weakness?") == "weakness"
        assert _classify_essay_question("Describe an area you'd like to improve") == "weakness"

    def test_unrecognized_falls_back_to_why_role(self):
        from agents.auto_applier.base import _classify_essay_question
        assert _classify_essay_question("What is your favorite color?") == "why_role"


# ============================================================
# Profile + fallback essays
# ============================================================

class TestFallbackEssays:
    """Fallback templates exist and are non-trivial."""

    def test_all_required_fallbacks_present(self):
        from agents.auto_applier.base import reload_profile
        p = reload_profile()
        fallbacks = p.get("fallback_essays", {})
        for key in ("why_company", "why_role", "greatest_strength", "weakness"):
            assert key in fallbacks, f"missing fallback essay: {key}"
            assert len(fallbacks[key]) > 50, f"fallback {key} suspiciously short"

    def test_fallback_used_when_ollama_unreachable(self, monkeypatch):
        """If Ollama is down, draft_essay_answer falls back to the rendered template."""
        from agents.auto_applier import base

        # Force the health check to report unhealthy
        monkeypatch.setattr("utils.ollama_client.check_ollama_health", lambda: False)

        class FakeJob:
            company = "Anthropic"
            title = "Data Analyst"
            description = ""

        answer = base.draft_essay_answer(
            "Why do you want to work for our company?",
            FakeJob(),
            base.reload_profile(),
        )
        # Should produce the rendered fallback (with company substituted)
        assert "Anthropic" in answer
        assert len(answer) > 80


# ============================================================
# Guardrails sanity
# ============================================================

class TestGuardrails:

    def test_workday_and_generic_in_allowed_list(self):
        from agents.auto_applier.base import reload_profile
        guards = reload_profile()["guardrails"]
        allowed = set(guards["allowed_ats_platforms"])
        assert "workday" in allowed
        assert "generic" in allowed or "custom" in allowed

    def test_max_essays_cap_set(self):
        from agents.auto_applier.base import reload_profile
        guards = reload_profile()["guardrails"]
        assert isinstance(guards["max_essays_per_app"], int)
        assert 1 <= guards["max_essays_per_app"] <= 10

    def test_bail_on_captcha_is_true(self):
        """We never solve CAPTCHAs — guardrail must stay enabled."""
        from agents.auto_applier.base import reload_profile
        guards = reload_profile()["guardrails"]
        assert guards["bail_on_captcha"] is True
