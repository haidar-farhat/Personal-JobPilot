"""Unit tests for the new archetype-routed multi-dimensional ranker.

These tests do NOT call Ollama — they verify the math + config + persistence layers.
LLM-touching functions (classify_archetype, score_dimensions, write_evaluation) are
covered by the live integration on real jobs in the pipeline; here we lock down the
deterministic surfaces.
"""

from pathlib import Path

import pytest
import yaml

from agents.ranker import (
    _archetype_config,
    compute_weighted_score,
)


# ----------------------------------------------------------------------
# Config integrity
# ----------------------------------------------------------------------


def test_archetype_yaml_loads():
    cfg = _archetype_config()
    assert "archetypes" in cfg
    assert "dimensions" in cfg
    assert len(cfg["dimensions"]) == 10


def test_each_archetype_has_complete_weights():
    cfg = _archetype_config()
    expected_dims = set(cfg["dimensions"])
    for key, arch in cfg["archetypes"].items():
        weights = arch["weights"]
        missing = expected_dims - set(weights.keys())
        extra = set(weights.keys()) - expected_dims
        assert not missing, f"archetype '{key}' missing dim weights: {missing}"
        assert not extra, f"archetype '{key}' has extra weights: {extra}"


def test_archetype_weights_sum_to_one():
    cfg = _archetype_config()
    for key, arch in cfg["archetypes"].items():
        total = sum(arch["weights"].values())
        assert abs(total - 1.0) < 0.01, f"archetype '{key}' weights sum to {total}, expected ~1.0"


def test_archetype_keys_are_safe_identifiers():
    cfg = _archetype_config()
    for key in cfg["archetypes"].keys():
        assert key.replace("_", "").isalnum(), f"archetype key '{key}' is not a safe identifier"
        assert key == key.lower(), f"archetype key '{key}' should be lowercase"


def test_unknown_archetype_present():
    """Critical fallback — must exist for graceful degradation."""
    cfg = _archetype_config()
    assert "unknown" in cfg["archetypes"]


def test_each_archetype_has_auto_apply_floor():
    cfg = _archetype_config()
    for key, arch in cfg["archetypes"].items():
        floor = arch.get("auto_apply_min_score")
        assert isinstance(floor, int), f"archetype '{key}' missing auto_apply_min_score"
        assert 0 < floor <= 100, f"archetype '{key}' auto_apply_min_score out of range: {floor}"


# ----------------------------------------------------------------------
# Weighted score math
# ----------------------------------------------------------------------


def test_weighted_score_all_perfect():
    perfect = {
        "technical_fit": 100, "level_match": 100, "comp_range": 100, "location_remote": 100,
        "archetype_fit": 100, "skills_overlap": 100, "growth_signal": 100, "company_reputation": 100,
        "ats_keyword_density": 100, "gap_severity": 100,
    }
    overall, weights = compute_weighted_score(perfect, "data_analyst")
    assert overall == 100
    assert sum(weights.values()) == pytest.approx(1.0, abs=0.01)


def test_weighted_score_all_zero():
    zero = {k: 0 for k in [
        "technical_fit", "level_match", "comp_range", "location_remote",
        "archetype_fit", "skills_overlap", "growth_signal", "company_reputation",
        "ats_keyword_density", "gap_severity",
    ]}
    overall, _ = compute_weighted_score(zero, "data_scientist")
    assert overall == 0


def test_weighted_score_archetype_routing_changes_result():
    """Same dim scores should produce different overall scores under different archetypes
    when the archetypes have different weights — confirms routing actually does something.

    Build a dim vector that maximally diverges between two archetypes:
    - quantitative_analyst: archetype_fit weight = 0.18 (highest in catalog)
    - ml_engineer:          archetype_fit weight = 0.10
    With tf=20 / archetype_fit=100 / rest=50, quant should score ~54 and ml ~48.
    """
    dims = {
        "technical_fit": 20, "level_match": 50, "comp_range": 50, "location_remote": 50,
        "archetype_fit": 100, "skills_overlap": 50, "growth_signal": 50, "company_reputation": 50,
        "ats_keyword_density": 50, "gap_severity": 50,
    }
    quant_score, _ = compute_weighted_score(dims, "quantitative_analyst")
    ml_score, _    = compute_weighted_score(dims, "ml_engineer")
    assert quant_score != ml_score, f"quant={quant_score} ml={ml_score} — routing not producing distinct results"
    # Quant should score higher because it weights archetype_fit much more heavily
    assert quant_score > ml_score


def test_weighted_score_unknown_archetype_uses_unknown_weights():
    dims = {k: 50 for k in [
        "technical_fit", "level_match", "comp_range", "location_remote",
        "archetype_fit", "skills_overlap", "growth_signal", "company_reputation",
        "ats_keyword_density", "gap_severity",
    ]}
    overall, weights = compute_weighted_score(dims, "totally_made_up_archetype")
    # Falls back to "unknown" archetype's weights — overall should still compute (≈50)
    assert 49 <= overall <= 51
    assert sum(weights.values()) == pytest.approx(1.0, abs=0.01)


def test_weighted_score_partial_dimensions():
    """If LLM returns fewer dimensions than expected, score still computes from what's there."""
    partial = {"technical_fit": 80, "level_match": 90}
    overall, _ = compute_weighted_score(partial, "data_analyst")
    # Should be some non-zero value (weighted mean of just those two dims, normalized to their weights)
    assert overall > 0


def test_weighted_score_clamps_to_0_100():
    # In case some dim got out of bounds before persistence
    weird = {k: 999 for k in [
        "technical_fit", "level_match", "comp_range", "location_remote",
        "archetype_fit", "skills_overlap", "growth_signal", "company_reputation",
        "ats_keyword_density", "gap_severity",
    ]}
    overall, _ = compute_weighted_score(weird, "data_analyst")
    assert 0 <= overall <= 100


# ----------------------------------------------------------------------
# Migration sanity
# ----------------------------------------------------------------------


def test_jobscore_model_has_new_columns():
    from db.models import JobScore
    cols = {c.name for c in JobScore.__table__.columns}
    assert "archetype" in cols
    assert "archetype_confidence" in cols
    assert "dimensions" in cols
    assert "dimension_weights" in cols
    assert "evaluation_path" in cols


def test_jobscore_has_ai_intensity_columns():
    """AI-forward signal columns (migration 004, 2026-06-29)."""
    from db.models import JobScore
    cols = {c.name for c in JobScore.__table__.columns}
    assert "ai_intensity" in cols
    assert "ai_tools" in cols


def test_score_dimensions_extracts_ai_intensity(monkeypatch):
    """score_dimensions parses ai_intensity + ai_tools from the LLM JSON
    (no Ollama — generate_json is monkeypatched)."""
    from agents import ranker
    from db.models import Job

    canned = {
        "dimensions": {d: 50 for d in ranker._archetype_config()["dimensions"]},
        "key_matches": [], "key_gaps": [], "ats_keywords": [],
        "seniority_match": True, "reasoning": "ok",
        "ai_intensity": 82, "ai_tools": ["LLM APIs", "RAG", "agents"],
    }
    monkeypatch.setattr(ranker, "generate_json", lambda *a, **k: canned)

    job = Job(title="AI Engineer", company="X", location="San Francisco",
              description="Build LLM agents", url="http://x", source="test",
              dedup_hash="hash-aieng")
    out = ranker.score_dimensions(job, "ai_engineer", "resume summary")
    assert out["ai_intensity"] == 82
    assert "RAG" in out["ai_tools"]


def test_score_dimensions_ai_intensity_missing_is_none(monkeypatch):
    """If the model omits ai_intensity, we store None (not 0) and an empty list."""
    from agents import ranker
    from db.models import Job

    canned = {
        "dimensions": {d: 40 for d in ranker._archetype_config()["dimensions"]},
        "reasoning": "no ai mention",
    }
    monkeypatch.setattr(ranker, "generate_json", lambda *a, **k: canned)
    job = Job(title="Warehouse Lead", company="Y", location="Oakland",
              description="forklift", url="http://y", source="test", dedup_hash="hash-wh")
    out = ranker.score_dimensions(job, "unknown", "resume summary")
    assert out["ai_intensity"] is None
    assert out["ai_tools"] == []


def test_archetype_yaml_valid_yaml():
    """Direct check the file is parseable — catches syntax regressions early."""
    path = Path(__file__).parent.parent / "config" / "archetypes.yaml"
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict)
    assert "archetypes" in data


def test_new_ai_and_bt_archetypes_present():
    """AI-track + BT-track archetypes added 2026-06-29 (see
    docs/superpowers/specs/2026-06-29-ai-roles-indeed-ui-design.md).
    The weight-sum / complete-weights / floor tests above validate them automatically.
    """
    cfg = _archetype_config()
    arch = cfg["archetypes"]
    for key in ["ai_engineer", "ai_solutions_engineer", "ai_analyst", "behavioral_technician"]:
        assert key in arch, f"missing archetype: {key}"
        assert arch[key].get("keywords"), f"{key} has no keywords"
        assert isinstance(arch[key].get("auto_apply_min_score"), int), f"{key} missing auto_apply_min_score"
