"""Company profiler — LLM drafting with mocked Ollama + page fetch."""

import agents.company_profiler as prof
from db.models import Company


FAKE_JSON = {"overview_md": "Acme builds fintech agents.",
             "why_fit_md": "Agentic AI overlap.",
             "hiring_bar_md": "Mostly senior; SWE II at 2 YOE.",
             "ats_platform_guess": "greenhouse"}


def _mk_company(session, **kw):
    c = Company(name="Acme", name_normalized="acme",
                careers_url="https://acme.test/careers",
                draft_status="drafting", **kw)
    session.add(c)
    session.commit()
    return c


def test_draft_fills_profile(tmp_session, monkeypatch):
    monkeypatch.setattr(prof, "_fetch_page_text", lambda url: "Acme Careers. We build fintech agents.")
    monkeypatch.setattr(prof, "generate_json", lambda *a, **k: dict(FAKE_JSON))
    monkeypatch.setattr(prof, "get_session", lambda: tmp_session)
    c = _mk_company(tmp_session)
    prof.draft_profile(c.id)
    tmp_session.refresh(c)
    assert c.overview_md == "Acme builds fintech agents."
    assert c.profile_source == "llm"
    assert c.draft_status is None
    assert c.ats_platform == "greenhouse"
    assert c.last_refreshed_at is not None


def test_draft_failure_sets_failed(tmp_session, monkeypatch):
    monkeypatch.setattr(prof, "_fetch_page_text", lambda url: "text")
    def boom(*a, **k): raise RuntimeError("ollama down")
    monkeypatch.setattr(prof, "generate_json", boom)
    monkeypatch.setattr(prof, "get_session", lambda: tmp_session)
    c = _mk_company(tmp_session)
    prof.draft_profile(c.id)
    tmp_session.refresh(c)
    assert c.draft_status == "failed"
    assert c.overview_md is None          # nothing half-written


def test_notes_never_touched(tmp_session, monkeypatch):
    monkeypatch.setattr(prof, "_fetch_page_text", lambda url: "text")
    monkeypatch.setattr(prof, "generate_json", lambda *a, **k: dict(FAKE_JSON))
    monkeypatch.setattr(prof, "get_session", lambda: tmp_session)
    c = _mk_company(tmp_session, notes_md="MY NOTES")
    prof.draft_profile(c.id)
    tmp_session.refresh(c)
    assert c.notes_md == "MY NOTES"


def test_page_fetch_failure_still_drafts_from_name(tmp_session, monkeypatch):
    """Careers page down != draft failure — the LLM drafts from name alone."""
    def fetch_boom(url): raise RuntimeError("timeout")
    captured = {}
    def fake_generate(prompt, system_prompt=""):
        captured["prompt"] = prompt
        return dict(FAKE_JSON)
    monkeypatch.setattr(prof, "_fetch_page_text", fetch_boom)
    monkeypatch.setattr(prof, "generate_json", fake_generate)
    monkeypatch.setattr(prof, "get_session", lambda: tmp_session)
    c = _mk_company(tmp_session)
    prof.draft_profile(c.id)
    tmp_session.refresh(c)
    assert c.draft_status is None
    assert "(no page available)" in captured["prompt"]


def test_bad_ats_guess_ignored_and_existing_ats_kept(tmp_session, monkeypatch):
    monkeypatch.setattr(prof, "_fetch_page_text", lambda url: "text")
    bad = dict(FAKE_JSON, ats_platform_guess="linkedin")   # not in whitelist
    monkeypatch.setattr(prof, "generate_json", lambda *a, **k: bad)
    monkeypatch.setattr(prof, "get_session", lambda: tmp_session)
    c = _mk_company(tmp_session, ats_platform="ashby")     # pre-set — must be kept
    prof.draft_profile(c.id)
    tmp_session.refresh(c)
    assert c.ats_platform == "ashby"
