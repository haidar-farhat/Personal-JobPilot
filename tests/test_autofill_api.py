"""Contract tests for the autofill API (FastAPI TestClient; Ollama mocked)."""

from fastapi.testclient import TestClient

import server.dashboard as dash

client = TestClient(dash.app)


def test_health():
    r = client.get("/api/autofill/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "ollama_up" in body and "profile_loaded" in body


def test_profile_shape():
    r = client.get("/api/autofill/profile")
    assert r.status_code == 200
    body = r.json()
    assert "identity" in body and "email" in body["identity"]


def test_plan_deterministic_plus_essay(monkeypatch):
    # Stub the LLM so the test is deterministic + offline.
    monkeypatch.setattr("server.autofill.generate_text", lambda *a, **k: "DRAFT ANSWER")
    body = {
        "url": "http://x",
        "job_title": "Behavior Technician",
        "fields": [
            {"id": "f1", "label": "Email", "name": "email", "type": "email"},
            {"id": "f2", "label": "Why do you want this role?", "name": "why", "type": "textarea"},
        ],
    }
    r = client.post("/api/autofill/plan", json=body)
    assert r.status_code == 200
    d = r.json()
    byid = {f["id"]: f for f in d["fields"]}
    assert byid["f1"]["value"]  # email filled deterministically
    assert byid["f1"]["source"] == "deterministic"
    assert byid["f2"]["value"] == "DRAFT ANSWER"
    assert byid["f2"]["source"] == "llm"
    assert d["resume_used"] == "base_resume_bt.yaml"  # routed to BT résumé
    assert d["stats"]["filled"] >= 2


def test_arm_armed_disarm_roundtrip(monkeypatch):
    """Dashboard arms a job URL → extension finds it by host (www-insensitive) or
    by page-url prefix → consumes it → gone. Expired arms never come back."""
    import server.autofill as af
    monkeypatch.setattr(af, "_ARMED", {})

    assert client.get("/api/autofill/armed", params={"host": "boards.greenhouse.io"}).json() == {}

    r = client.post("/api/autofill/arm", json={"url": "https://www.boards.greenhouse.io/acme/jobs/42",
                                               "company": "Acme", "title": "Data Analyst"})
    assert r.status_code == 200
    rec = r.json()
    assert rec["host"] == "boards.greenhouse.io" and rec["company"] == "Acme" and rec["app_id"] is None

    # by host (any www/case), and by page url when the host is a redirect target
    assert client.get("/api/autofill/armed", params={"host": "www.Boards.Greenhouse.io"}).json()["title"] == "Data Analyst"
    assert client.get("/api/autofill/armed", params={
        "host": "apply.example.com",
        "url": "https://www.boards.greenhouse.io/acme/jobs/42/application?src=x"}).json()["company"] == "Acme"
    assert client.get("/api/autofill/armed", params={"host": "lever.co"}).json() == {}

    d = client.delete("/api/autofill/armed", params={"host": "boards.greenhouse.io"}).json()
    assert d == {"ok": True, "removed": True}
    assert client.get("/api/autofill/armed", params={"host": "boards.greenhouse.io"}).json() == {}

    # TTL: an old arm is swept on read
    client.post("/api/autofill/arm", json={"url": "https://jobs.lever.co/acme/1"})
    af._ARMED["jobs.lever.co"]["ts"] -= af._ARM_TTL + 1
    assert client.get("/api/autofill/armed", params={"host": "jobs.lever.co"}).json() == {}

    # bad input
    assert client.post("/api/autofill/arm", json={"url": ""}).status_code == 400
    assert client.post("/api/autofill/arm", json={"app_id": 999999999}).status_code == 404


def test_plan_resume_pref_ai_routes_ai(monkeypatch):
    monkeypatch.setattr("server.autofill.generate_text", lambda *a, **k: "X")
    body = {
        "job_title": "Behavior Technician",  # would be BT, but pref overrides
        "resume_pref": "ai",
        "fields": [{"id": "f1", "label": "Email", "name": "email", "type": "email"}],
    }
    r = client.post("/api/autofill/plan", json=body)
    assert r.status_code == 200
    assert r.json()["resume_used"] == "base_resume.yaml"
