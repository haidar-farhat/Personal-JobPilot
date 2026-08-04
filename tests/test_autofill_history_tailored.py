"""History endpoint must mirror the ATTACHED résumé (2026-07-22): the
tailored sidecar JSON when one exists for this company/role, else the routed
base YAML — so Workday experience panels match the résumé word-for-word."""

import json

from server import autofill

TAILORED = {
    "work_experience": [
        {"title": "AI Engineer", "organization": "Acme AI", "location": "San Francisco, CA",
         "dates": "June 2026 – Present",
         "bullets": ["Shipped LLM pipelines", "Cut inference cost 40%"]},
    ],
}


def test_history_serves_tailored_sidecar(tmp_path, monkeypatch):
    docx = tmp_path / "acme_resume.docx"
    docx.write_bytes(b"stub")
    docx.with_suffix(".json").write_text(json.dumps(TAILORED), encoding="utf-8")
    monkeypatch.setattr(autofill, "_tailored_resume_path", lambda c, t="": docx)
    h = autofill.get_history(company="acme", job_title="ai engineer")
    assert [w["title"] for w in h["work"]] == ["AI Engineer"]
    assert h["work"][0]["company"] == "Acme AI"
    assert h["work"][0]["description"] == "Shipped LLM pipelines\nCut inference cost 40%"
    assert h["work"][0]["current"] is True


def test_history_falls_back_to_base_without_company():
    h = autofill.get_history()
    titles = [w["title"] for w in h["work"]]
    assert "Behavioral Technician" in titles
    # reverse-chron guard: the current BIA role precedes the ended Rithum one
    assert titles.index("Behavioral Technician") < \
           titles.index("Economic Consultant (Graduate Capstone)")


def test_history_ignores_unreadable_sidecar(tmp_path, monkeypatch):
    docx = tmp_path / "acme_resume.docx"
    docx.write_bytes(b"stub")
    docx.with_suffix(".json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(autofill, "_tailored_resume_path", lambda c, t="": docx)
    h = autofill.get_history(company="acme")
    assert any(w["title"] == "Behavioral Technician" for w in h["work"])  # base fallback
