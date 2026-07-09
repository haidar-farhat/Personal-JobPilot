"""Seeder — idempotent upsert, never clobbers manual profiles."""

import scripts.seed_companies as seeder
from db.models import Company


SAMPLE = [{
    "name": "Chime", "careers_url": "https://careers.chime.com",
    "ats_platform": "greenhouse", "priority": "high", "status": "target",
    "overview_md": "SF consumer fintech.", "why_fit_md": "Risk + ML fit.",
    "hiring_bar_md": "AI/ML Eng is 1-2 YOE.", "suggested": False,
}]


def test_seed_upserts_idempotently(tmp_session, monkeypatch):
    monkeypatch.setattr(seeder, "load_seed", lambda: SAMPLE)
    monkeypatch.setattr(seeder, "get_session", lambda: tmp_session)
    r1 = seeder.run(close=False)
    assert r1 == {"created": 1, "updated": 0, "skipped_manual": 0}
    r2 = seeder.run(close=False)
    assert r2 == {"created": 0, "updated": 1, "skipped_manual": 0}
    assert tmp_session.query(Company).count() == 1


def test_seed_never_clobbers_manual(tmp_session, monkeypatch):
    monkeypatch.setattr(seeder, "load_seed", lambda: SAMPLE)
    monkeypatch.setattr(seeder, "get_session", lambda: tmp_session)
    seeder.run(close=False)
    c = tmp_session.query(Company).one()
    c.profile_source = "manual"
    c.why_fit_md = "hand-tuned"
    tmp_session.commit()
    r = seeder.run(close=False)
    assert r["skipped_manual"] == 1
    assert tmp_session.query(Company).one().why_fit_md == "hand-tuned"
