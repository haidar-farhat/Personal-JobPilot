"""Credential-shaped fields never get a value from the generic planner.

Passwords, one-time codes and SSNs are the fields where a wrong fill is
worst: the account assist (content/account.js) owns passwords and OTP boxes
with the user's chosen ATS password / a Gmail code; SSN has no source at all.
Before this guard, an OTP box labelled "Enter the 6-digit verification code we
emailed you" (5+ words, type=text) was routed to the LLM as an essay.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from agents.autofill_mapper import build_plan, map_standard_field

PROFILE = {"identity": {"first_name": "Alex", "last_name": "Rivera", "full_name": "Alex Rivera",
                        "email": "alex@example.com", "phone": "4155550142"}}

CREDENTIAL_FIELDS = [
    {"id": "f0", "label": "Password", "name": "password", "type": "password"},
    {"id": "f1", "label": "Create a password with at least 8 characters", "name": "pw", "type": "text"},
    {"id": "f2", "label": "Enter the 6-digit verification code we emailed you", "name": "code", "type": "text"},
    {"id": "f3", "label": "Code", "name": "otp", "type": "text", "autocomplete": "one-time-code"},
    {"id": "f4", "label": "Social Security Number", "name": "ssn", "type": "text"},
    {"id": "f5", "label": "Security code", "name": "verify", "type": "text", "inputmode": "numeric"},
]


@pytest.mark.parametrize("field", CREDENTIAL_FIELDS, ids=lambda f: f["name"])
def test_mapper_never_fills_credentials(field):
    m = map_standard_field(field, PROFILE)
    assert m is not None and m["value"] is None, f"{field['label']!r} got {m}"


def test_build_plan_never_sends_credentials_to_the_llm():
    calls = []

    def essay(field, ctx):
        calls.append(field["label"])
        return "Some prose the LLM would type into the box"

    plan = build_plan(CREDENTIAL_FIELDS, PROFILE, None, "", essay_fn=essay)
    assert calls == [], f"LLM was asked to answer credential fields: {calls}"
    assert all(f["value"] is None for f in plan["fields"])


def test_email_next_to_a_password_still_fills():
    """The guard is narrow — a login form's email box is still the user's email."""
    m = map_standard_field({"id": "f9", "label": "Email Address", "name": "email", "type": "text"}, PROFILE)
    assert m["value"] == "alex@example.com"


SCRIPT = Path(__file__).resolve().parent / "js" / "credential_guard.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_offline_js_mapper_agrees():
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"\n{r.stdout}\n{r.stderr}"
