"""Portable semantic-form coverage for Simplify/Jobright-style workflows."""

from html.parser import HTMLParser
from pathlib import Path

from agents.autofill_mapper import map_standard_field


FIXTURE = Path(__file__).parent / "e2e" / "fixtures" / "common_semantic_form.html"

PROFILE = {
    "identity": {"first_name": "Alex", "last_name": "Rivera",
                 "full_name": "Alex Rivera", "email": "alex@example.com",
                 "phone": "4155550142"},
    "address": {"street": "10 Market St", "city": "Oakland", "state": "CA",
                "state_full": "California", "postal_code": "94601",
                "country": "United States"},
    "links": {"linkedin": "https://linkedin.com/in/alex", "portfolio": ""},
}


class _Controls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.controls = []

    def handle_starttag(self, tag, attrs):
        if tag in {"input", "select", "textarea"}:
            data = dict(attrs)
            self.controls.append({
                "id": data.get("id", ""), "label": data.get("id", ""),
                "name": data.get("name", data.get("id", "")),
                "type": data.get("type", tag),
                "autocomplete": data.get("autocomplete", ""),
                "inputmode": data.get("inputmode", ""),
                "options": ["California", "Nevada"] if data.get("id") == "region" else [],
            })


def _fixture_controls():
    parser = _Controls()
    parser.feed(FIXTURE.read_text(encoding="utf-8"))
    return {field["id"]: field for field in parser.controls}


def test_fixture_exercises_portable_application_semantics():
    fields = _fixture_controls()
    expected = {
        "first": "Alex", "last": "Rivera", "email": "alex@example.com",
        "phone": "4155550142", "street": "10 Market St", "city": "Oakland",
        "region": "California", "postal": "94601",
        "linkedin": "https://linkedin.com/in/alex",
    }
    assert {key: map_standard_field(fields[key], PROFILE)["value"] for key in expected} == expected


def test_aria_label_fixture_uses_multiple_idrefs():
    source = FIXTURE.read_text(encoding="utf-8")
    assert 'aria-labelledby="first-label identity-help"' in source
    scan = (FIXTURE.parents[3] / "browser-extension" / "content" / "scan.js").read_text(
        encoding="utf-8"
    )
    assert "lblBy.split(/\\s+/)" in scan


def test_autofill_does_not_record_or_submit_application():
    worker = (FIXTURE.parents[3] / "browser-extension" / "service_worker.js").read_text(
        encoding="utf-8"
    )
    autofill = worker.split("async function runAutofill", 1)[1].split(
        "// ---- Company scan", 1
    )[0]
    assert "/api/applied/record" not in autofill
    assert "manual_review: true" in autofill
    assert ".submit(" not in autofill
