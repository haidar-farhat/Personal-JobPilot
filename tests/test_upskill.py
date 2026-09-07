"""Unit tests for the upskill analyzer — gap aggregation runs on the tmp SQLite
fixture (never the real DB); plan normalizer is pure; LLM is stubbed."""

from datetime import datetime, timezone

import agents.upskill as upskill_mod
from agents.upskill import (
    _norm_gap,
    _normalize_plan,
    collect_gap_stats,
    generate_upskill_report,
    get_or_create_upskill,
)
from db.models import Application, ApplicationStatus, Job, JobScore


# --------------------------------------------------------------------------- #
# _norm_gap / _normalize_plan — pure
# --------------------------------------------------------------------------- #

def test_norm_gap_collapses_case_space_punct():
    assert _norm_gap("  Production   ML.") == "production ml"
    assert _norm_gap("Kubernetes;") == _norm_gap("kubernetes")


def test_normalize_plan_full_valid():
    raw = {
        "summary": "Cloud is the biggest lever.",
        "themes": [{"theme": "Cloud & deployment", "skills": ["AWS", "Docker"],
                    "why": "5 of 8 postings", "priority": "HIGH",
                    "actions": ["Deploy JobPilot to AWS"], "time_estimate": "2 weekends"}],
        "quick_wins": ["Add SQL to resume"],
    }
    out = _normalize_plan(raw)
    assert out["themes"][0]["priority"] == "high"  # case-normalized
    assert out["themes"][0]["skills"] == ["AWS", "Docker"]
    assert out["quick_wins"] == ["Add SQL to resume"]


def test_meta_gaps_filtered_out(monkeypatch, tmp_session, session_factory):
    # Posting-quality complaints from the ranker must never appear as skill gaps.
    monkeypatch.setattr(upskill_mod, "get_session", session_factory)
    _seed(tmp_session, 1, [
        "No salary information provided for compensation scoring",
        "The job description is empty, making keyword matching impossible.",
        "Salary range not specified in the posting",
        "Kubernetes",
    ])
    stats = collect_gap_stats()
    assert [g["gap"] for g in stats["gaps"]] == ["Kubernetes"]


def test_normalize_plan_guards_bad_shapes():
    out = _normalize_plan({"themes": ["not a dict", {"no_theme_key": 1},
                                      {"theme": "X", "priority": "urgent!!"}],
                           "quick_wins": "nope"})
    assert len(out["themes"]) == 1
    assert out["themes"][0]["priority"] == "medium"  # unknown priority clamped
    assert out["quick_wins"] == []
    assert _normalize_plan(None)["themes"] == []


def test_normalize_plan_strips_markdown_bold():
    out = _normalize_plan({"summary": "**Big** lever", "quick_wins": ["**Do:** the thing"]})
    assert out["summary"] == "Big lever"
    assert out["quick_wins"] == ["Do: the thing"]


# --------------------------------------------------------------------------- #
# collect_gap_stats — tmp DB, deterministic
# --------------------------------------------------------------------------- #

def _seed(session, i, gaps, fit=70, applied=False):
    job = Job(title=f"Role {i}", company=f"Co {i}", url=f"http://x/{i}",
              source="test", dedup_hash=f"hash{i}")
    session.add(job)
    session.flush()
    session.add(JobScore(job_id=job.id, fit_score=fit, key_gaps=gaps))
    if applied:
        session.add(Application(job_id=job.id, status=ApplicationStatus.APPLIED,
                                date_applied=datetime.now(timezone.utc)))
    session.commit()


def test_collect_gap_stats_counts_weights_and_examples(monkeypatch, tmp_session, session_factory):
    monkeypatch.setattr(upskill_mod, "get_session", session_factory)
    _seed(tmp_session, 1, ["Kubernetes", "AWS"], fit=80, applied=True)
    _seed(tmp_session, 2, ["kubernetes.", "Spark"], fit=60)          # same gap, different casing
    _seed(tmp_session, 3, None)                                      # null gaps tolerated

    stats = collect_gap_stats()
    assert stats["totals"] == {"n_scored": 3, "n_applied": 1, "n_distinct_gaps": 3}
    top = stats["gaps"][0]
    assert _norm_gap(top["gap"]) == "kubernetes"    # merged across casing/punctuation
    assert top["count"] == 2 and top["applied_count"] == 1
    assert top["weight"] == 3                       # count + applied_count (applied weighs double)
    assert top["avg_fit"] == 70
    assert "Role 1 — Co 1" in top["examples"]


def test_collect_gap_stats_empty_db(monkeypatch, session_factory):
    monkeypatch.setattr(upskill_mod, "get_session", session_factory)
    stats = collect_gap_stats()
    assert stats["gaps"] == [] and stats["totals"]["n_scored"] == 0


# --------------------------------------------------------------------------- #
# generate_upskill_report / get_or_create — LLM stubbed, cache in tmp
# --------------------------------------------------------------------------- #

_FAKE_PLAN = {"summary": "s", "themes": [{"theme": "T", "priority": "high"}], "quick_wins": []}


def _stub(monkeypatch, tmp_path, gaps, llm=None, calls=None):
    monkeypatch.setattr(upskill_mod, "collect_gap_stats",
                        lambda limit=30: {"gaps": gaps, "totals": {"n_scored": len(gaps), "n_applied": 0,
                                                                   "n_distinct_gaps": len(gaps)}})
    def fake_llm(prompt, system_prompt=""):
        if calls is not None:
            calls.append(1)
        if isinstance(llm, Exception):
            raise llm
        return dict(llm or _FAKE_PLAN)
    monkeypatch.setattr(upskill_mod, "generate_json", fake_llm)
    monkeypatch.setattr(upskill_mod, "_load_resume_summary", lambda archetype=None: "RESUME")
    monkeypatch.setattr(upskill_mod, "_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(upskill_mod, "_REPORT_PATH", tmp_path / "report.json")


_GAP = {"gap": "AWS", "count": 3, "applied_count": 1, "avg_fit": 70, "examples": ["R — C"], "weight": 4}


def test_report_includes_heatmap_and_plan(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, [_GAP])
    r = generate_upskill_report()
    assert r["gap_stats"] == [_GAP]
    assert r["themes"][0]["theme"] == "T"


def test_report_survives_llm_failure(monkeypatch, tmp_path):
    # Ollama down → heatmap still served, themes empty, llm_error set.
    _stub(monkeypatch, tmp_path, [_GAP], llm=RuntimeError("ollama offline"))
    r = generate_upskill_report()
    assert r["gap_stats"] == [_GAP]
    assert r["themes"] == [] and "ollama offline" in r["llm_error"]


def test_no_gaps_skips_llm(monkeypatch, tmp_path):
    calls = []
    _stub(monkeypatch, tmp_path, [], calls=calls)
    r = generate_upskill_report()
    assert calls == [] and r["themes"] == []
    assert "No skill gaps" in r["summary"]


def test_get_or_create_caches_second_call(monkeypatch, tmp_path):
    calls = []
    _stub(monkeypatch, tmp_path, [_GAP], calls=calls)
    first = get_or_create_upskill()
    assert first["cached"] is False and len(calls) == 1
    second = get_or_create_upskill()
    assert second["cached"] is True and len(calls) == 1  # served from cache


def test_empty_report_not_cached(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, [])
    out = get_or_create_upskill()
    assert out["cached"] is False
    assert not (tmp_path / "report.json").exists()
