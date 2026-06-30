"""Contract tests for the Google Sheet sync API (FastAPI TestClient, no network).

The live gspread worksheet is replaced with an in-memory fake and the DB query
is monkeypatched, so these run fully offline and deterministically.
"""

from fastapi.testclient import TestClient

import server.dashboard as dash
import server.sheets as sheets

client = TestClient(dash.app)


class FakeWS:
    """Minimal stand-in for a gspread Worksheet."""

    def __init__(self, values, title="Sheet1"):
        self._values = [list(r) for r in values]
        self.title = title
        self.appended = []

    def get_all_values(self):
        return [list(r) for r in self._values]

    def append_row(self, row, value_input_option=None):
        self._values.append(list(row))
        self.appended.append(list(row))

    def append_rows(self, rows, value_input_option=None):
        for r in rows:
            self._values.append(list(r))
            self.appended.append(list(r))


APPS = [
    {"id": 1, "company": "Acme", "title": "AI Engineer", "location": "SF",
     "pay": "$120k", "source": "greenhouse", "status": "applied",
     "status_label": "Applied", "fit": 88, "url": "https://acme.com/jobs/1",
     "date_applied": "2026-06-30", "notes": ""},
    {"id": 2, "company": "Beta", "title": "ML Engineer", "location": "Remote",
     "pay": "", "source": "lever", "status": "interview",
     "status_label": "Interview", "fit": 72, "url": "https://beta.com/jobs/2",
     "date_applied": "2026-06-29", "notes": ""},
]


def test_health_unconfigured(monkeypatch):
    def boom():
        raise sheets.SheetNotConnected("no creds yet")
    monkeypatch.setattr(sheets, "_open_worksheet", boom)
    r = client.get("/api/sheet/health")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert "no creds" in body["reason"]


def test_compare_buckets(monkeypatch):
    # Sheet already has Acme (matches app 1 by URL); app 2 is new.
    ws = FakeWS([
        ["Company", "Role", "Date Applied", "Link"],
        ["Acme", "AI Engineer", "2026-06-30", "https://acme.com/jobs/1"],
    ])
    monkeypatch.setattr(sheets, "_open_worksheet", lambda: ws)
    monkeypatch.setattr(sheets, "_applied_applications", lambda: list(APPS))
    r = client.get("/api/sheet/compare")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert body["counts"]["in_sheet"] == 1
    assert body["counts"]["new"] == 1
    assert body["counts"]["applied_total"] == 2
    assert [a["id"] for a in body["new"]] == [2]


def test_sync_appends_only_new(monkeypatch):
    ws = FakeWS([
        ["Company", "Role", "Date Applied", "Link"],
        ["Acme", "AI Engineer", "2026-06-30", "https://acme.com/jobs/1"],
    ])
    monkeypatch.setattr(sheets, "_open_worksheet", lambda: ws)
    monkeypatch.setattr(sheets, "_applied_applications", lambda: list(APPS))
    r = client.post("/api/sheet/sync", json={"app_ids": [1, 2]})
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 1     # only Beta (app 2)
    assert body["skipped"] == 1   # Acme already present
    assert len(ws.appended) == 1
    # Row aligns to the user's 4-col layout: Company, Role, Date Applied, Link
    row = ws.appended[0]
    assert row[0] == "Beta"
    assert row[1] == "ML Engineer"
    assert row[3] == "https://beta.com/jobs/2"


def test_sync_writes_header_on_empty_sheet(monkeypatch):
    ws = FakeWS([])
    monkeypatch.setattr(sheets, "_open_worksheet", lambda: ws)
    monkeypatch.setattr(sheets, "_applied_applications", lambda: list(APPS))
    r = client.post("/api/sheet/sync", json={"app_ids": [1, 2]})
    assert r.status_code == 200
    body = r.json()
    assert body["wrote_header"] is True
    assert body["added"] == 2
    # First appended row is the canonical header, then the 2 data rows.
    assert ws.appended[0] == sheets.default_header()
    assert len(ws.appended) == 3
