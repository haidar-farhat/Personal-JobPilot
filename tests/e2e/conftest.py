"""Shared fixtures for JobPilot end-to-end tests."""

import os
import sys
from pathlib import Path

import pytest
import requests


BASE_URL = os.environ.get("JOBPILOT_BASE_URL", "http://127.0.0.1:7777")


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session", autouse=True)
def _verify_server_is_live(request):
    """Skip the entire `live` marked suite when the dashboard isn't reachable."""
    if "live" not in request.config.getoption("-m", default=""):
        return
    try:
        r = requests.get(f"{BASE_URL}/api/stats", timeout=3)
        r.raise_for_status()
    except Exception as exc:
        pytest.exit(
            f"JobPilot dashboard not reachable at {BASE_URL} ({exc}). "
            f"Start it with `python -m server.dashboard` before running -m live tests.",
            returncode=2,
        )


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def db_path(repo_root) -> Path:
    return repo_root / "jobpilot.db"
