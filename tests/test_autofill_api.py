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
