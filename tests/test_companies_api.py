"""Companies API — rollups + pipeline tree."""

import pytest
from fastapi.testclient import TestClient

import agents.company_profiler as profiler_mod
import server.companies as companies_mod
import server.dashboard as dash
from db.models import Application, ApplicationStatus, Company, Job

client = TestClient(dash.app)


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, session_factory):
    monkeypatch.setattr(companies_mod, "get_session", session_factory)
    # companies_mod now schedules the REAL agents.company_profiler.draft_profile
    # as a background task, and TestClient runs background tasks synchronously
    # before the request call returns. Left unstubbed, every CRUD test in this
    # file (not just the one below that cares about the profiler) would fire a
    # real Ollama call and — worse — since these tmp-DB company ids start at 1
    # just like the real seeded companies, an unpatched draft_profile would
    # write LLM output straight over production rows in jobpilot.db. Default
    # to a no-op here (fast, offline-safe); test_create_triggers_profiler
    # below overrides this per-test to assert it's actually invoked. The
    # get_session patch is kept too as defense in depth for any test that
    # overrides draft_profile back to the real function.
    monkeypatch.setattr(companies_mod, "draft_profile", lambda cid: None)
    monkeypatch.setattr(profiler_mod, "get_session", session_factory)


@pytest.fixture()
def seeded(session_factory):
    s = session_factory()
    c = Company(name="Affirm", name_normalized="affirm", priority="high",
                status="target", ats_platform="greenhouse",
                why_fit_md="ML is the moat.\nMore detail here.")
    s.add(c); s.flush()
    for i, st in enumerate((ApplicationStatus.SCORED, ApplicationStatus.APPLIED,
                            ApplicationStatus.INTERVIEW, ApplicationStatus.REJECTED)):
        job = Job(title=f"Role {i}", company="Affirm", url=f"https://a.test/{i}",
                  source="test", dedup_hash=f"h{i}", company_id=c.id)
        s.add(job); s.flush()
        s.add(Application(job_id=job.id, status=st))
    # one unlinked job -> "other" rollup
    other = Job(title="Mystery", company="SoFi", url="https://o.test/1",
                source="test", dedup_hash="ho1")
    s.add(other); s.flush()
    s.add(Application(job_id=other.id, status=ApplicationStatus.FOUND))
    s.commit()
    cid = c.id
    s.close()
    return cid


def test_list_rollups(seeded):
    r = client.get("/api/companies")
    assert r.status_code == 200
    body = r.json()
    aff = next(c for c in body["companies"] if c["name"] == "Affirm")
    assert aff["counts"] == {"watching": 1, "applied": 1, "in_play": 1, "closed": 1}
    assert aff["why_fit_teaser"] == "ML is the moat."
    assert body["other"][0]["name"] == "SoFi"


def test_list_orders_high_priority_first(session_factory):
    """priority is a STRING — a plain desc() sorts medium > low > high.

    Insert in low/high/medium order to prove the result isn't insertion
    order either.
    """
    s = session_factory()
    s.add(Company(name="Lowco", name_normalized="lowco", priority="low"))
    s.add(Company(name="Highco", name_normalized="highco", priority="high"))
    s.add(Company(name="Midco", name_normalized="midco", priority="medium"))
    s.commit(); s.close()

    r = client.get("/api/companies")
    assert r.status_code == 200
    names = [c["name"] for c in r.json()["companies"]]
    assert names == ["Highco", "Midco", "Lowco"]


def test_detail_tree_groups_by_stage(seeded):
    r = client.get(f"/api/company/{seeded}")
    assert r.status_code == 200
    tree = r.json()["tree"]
    stages = {g["stage"]: [j["title"] for j in g["jobs"]] for g in tree}
    assert set(stages) == {"watching", "applied", "in_play", "closed"}
    assert stages["applied"] == ["Role 1"]


def test_detail_404(seeded):
    assert client.get("/api/company/99999").status_code == 404


def test_lazy_link_attaches_new_scanned_jobs(seeded, session_factory):
    """Jobs found by scanners AFTER seeding must attach on next read."""
    s = session_factory()
    job = Job(title="New scan find", company="Affirm, Inc.",   # normalized -> affirm
              url="https://a.test/new", source="greenhouse", dedup_hash="hnew")
    s.add(job); s.flush()
    s.add(Application(job_id=job.id, status=ApplicationStatus.FOUND))
    s.commit(); s.close()

    r = client.get("/api/companies")
    aff = next(c for c in r.json()["companies"] if c["name"] == "Affirm")
    assert aff["counts"]["watching"] == 2   # original SCORED + the new find
    s = session_factory()
    assert s.query(Job).filter_by(dedup_hash="hnew").one().company_id is not None
    s.close()


def test_create_edit_delete_company(session_factory):
    r = client.post("/api/companies", json={"name": "SoFi",
                                            "careers_url": "https://sofi.com/careers"})
    assert r.status_code == 200
    cid = r.json()["id"]

    r2 = client.patch(f"/api/company/{cid}", json={"notes_md": "call recruiter",
                                                   "priority": "high"})
    assert r2.status_code == 200
    s = session_factory()
    c = s.query(Company).get(cid)
    assert (c.notes_md, c.priority) == ("call recruiter", "high")
    s.close()

    # editing a profile field by hand flips profile_source to manual
    client.patch(f"/api/company/{cid}", json={"why_fit_md": "hand-written"})
    s = session_factory()
    assert s.query(Company).get(cid).profile_source == "manual"
    s.close()

    assert client.delete(f"/api/company/{cid}").status_code == 200
    s = session_factory()
    assert s.query(Company).get(cid) is None
    s.close()


def test_create_duplicate_name_conflicts(session_factory):
    client.post("/api/companies", json={"name": "SoFi"})
    r = client.post("/api/companies", json={"name": "SoFi Technologies, Inc."})
    assert r.status_code == 409   # same normalized name


def test_delete_unlinks_jobs(seeded, session_factory):
    client.delete(f"/api/company/{seeded}")
    s = session_factory()
    assert all(j.company_id is None for j in s.query(Job).all())
    s.close()


def test_create_adopts_matching_unlinked_jobs(session_factory):
    s = session_factory()
    job = Job(title="AE", company="SoFi Technologies", url="https://s.test/1",
              source="test", dedup_hash="hs1")
    s.add(job); s.commit(); s.close()
    r = client.post("/api/companies", json={"name": "SoFi"})
    cid = r.json()["id"]
    s = session_factory()
    assert s.query(Job).one().company_id == cid
    s.close()


def test_patch_rename_conflict_409(session_factory):
    sid = client.post("/api/companies", json={"name": "SoFi"}).json()["id"]
    cid = client.post("/api/companies", json={"name": "Chime"}).json()["id"]

    # renaming Chime to a variant that normalizes to "sofi" must conflict —
    # PATCH may not bypass the POST uniqueness guard
    r = client.patch(f"/api/company/{cid}", json={"name": "SoFi Technologies, Inc."})
    assert r.status_code == 409

    # blank / whitespace-only name rejected
    assert client.patch(f"/api/company/{cid}", json={"name": "   "}).status_code == 400

    # self-rename to a variant of its OWN name is allowed (exclude_id)
    r = client.patch(f"/api/company/{sid}", json={"name": "SoFi, Inc."})
    assert r.status_code == 200
    s = session_factory()
    c = s.query(Company).get(sid)
    assert (c.name, c.name_normalized) == ("SoFi, Inc.", "sofi")
    # Chime untouched by the failed renames
    assert s.query(Company).get(cid).name == "Chime"
    s.close()


def test_patch_invalid_priority_422(session_factory):
    cid = client.post("/api/companies", json={"name": "SoFi"}).json()["id"]
    assert client.patch(f"/api/company/{cid}",
                        json={"priority": "urgent"}).status_code == 422
    assert client.patch(f"/api/company/{cid}",
                        json={"status": "zombie"}).status_code == 422


def test_patch_blank_careers_url_clears(session_factory):
    cid = client.post("/api/companies",
                      json={"name": "SoFi",
                            "careers_url": "https://sofi.com/careers"}).json()["id"]
    r = client.patch(f"/api/company/{cid}", json={"careers_url": ""})
    assert r.status_code == 200
    s = session_factory()
    assert s.query(Company).get(cid).careers_url is None   # "" -> None, matches POST
    s.close()


def test_careers_url_scheme_guard(session_factory):
    """careers_url renders into an href — javascript: URIs must 422.

    The Task-14 LLM profiler will write this field from untrusted page
    content, so the scheme check has to live server-side, on BOTH write
    models (the dashboard's startsWith("http") render guard is only
    belt-and-braces).
    """
    cid = client.post("/api/companies", json={"name": "SoFi"}).json()["id"]
    r = client.patch(f"/api/company/{cid}",
                     json={"careers_url": "javascript:alert(1)"})
    assert r.status_code == 422
    r = client.patch(f"/api/company/{cid}",
                     json={"careers_url": "https://sofi.com/careers"})
    assert r.status_code == 200
    # POST inherits the same guard
    assert client.post("/api/companies",
                       json={"name": "Evil Co",
                             "careers_url": "javascript:alert(1)"}).status_code == 422


def test_refresh_reentrant_noop(session_factory):
    cid = client.post("/api/companies", json={"name": "SoFi"}).json()["id"]
    # first refresh works (stub clears draft_status synchronously under TestClient)
    assert client.post(f"/api/company/{cid}/refresh").status_code == 200

    # simulate a draft in flight with a meaningful backup already captured
    s = session_factory()
    c = s.query(Company).get(cid)
    c.draft_status = "drafting"
    c.profile_backup = {"sentinel": True}
    s.commit(); s.close()

    r = client.post(f"/api/company/{cid}/refresh")
    assert r.status_code == 200
    assert r.json()["draft_status"] == "drafting"
    s = session_factory()
    assert s.query(Company).get(cid).profile_backup == {"sentinel": True}   # NOT overwritten
    s.close()


def test_create_triggers_profiler(monkeypatch, session_factory):
    calls = []
    monkeypatch.setattr(companies_mod, "draft_profile", lambda cid: calls.append(cid))
    r = client.post("/api/companies", json={"name": "Mercury"})
    assert r.status_code == 200
    assert calls == [r.json()["id"]]


def test_startup_sweep_fails_orphaned_drafts(session_factory):
    s = session_factory()
    c = Company(name="Orphan", name_normalized="orphan", draft_status="drafting")
    s.add(c); s.commit(); cid = c.id; s.close()
    from server.companies import fail_orphaned_drafts
    n = fail_orphaned_drafts()
    assert n == 1
    s = session_factory()
    assert s.query(Company).get(cid).draft_status == "failed"
    s.close()
