"""Runs tests/js/choice_matching.mjs under pytest so it can't rot unnoticed.

That script drives the SHIPPED extension JavaScript (content/fill.js and
service_worker.js) through the same REJECT/ACCEPT table as
tests/test_choice_matching_safety.py checks against the Python mapper. Both
paths fill the same forms — the online one via /api/autofill/plan, the offline
one when the backend is down — so they must agree on what a safe match is.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "js" / "choice_matching.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_extension_js_matcher_agrees_with_python():
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"\n{r.stdout}\n{r.stderr}"
