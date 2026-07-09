# Companies Section, Application Trees & Advisor Tracking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reliable application tracking (event history + external-apply capture), a Companies tab with tailored profiles and full-pipeline trees, and a printable Advisor report for Vantage Point meetings.

**Architecture:** Two new SQLAlchemy tables (`companies`, `application_events`) + three new columns, all status writes funneled through one `record_status_change` helper, three new FastAPI routers following the existing `job_import` pattern, vanilla-JS additions to the single-file dashboard, and one extension widget button. LLM profile drafting reuses `utils/ollama_client.generate_json` as a FastAPI background task.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2 (SQLite), pytest (+ FastAPI TestClient, `live` marker for e2e), vanilla JS single-file dashboard (`server/static/index.html`), Chrome MV3 extension.

**Spec:** `docs/superpowers/specs/2026-07-08-companies-advisor-tracking-design.md`

---

## ⚠️ Repo cautions (read first)

1. **`config/settings.yaml` has an intentional uncommitted local edit** (Mac-only `model: "gemma4:e4b"` override). NEVER stage it. Always `git add` explicit paths — never `git add -A` / `git add .`.
2. New TABLES are auto-created by `Base.metadata.create_all` inside `db/database.get_engine()` — migrations only need `ALTER TABLE` for new COLUMNS + data backfill (house pattern, see `db/migrations/005_*.py`).
3. Unit tests that mutate data must NOT touch the real `jobpilot.db`. Use the tmp-engine fixture from Task 2 and monkeypatch `get_session` in the router module under test (pattern below).
4. Dashboard is one file: `server/static/index.html` (CSS ~lines 1–450, HTML ~450–530, JS ~530+). Match its style: `$()` helper, `esc()`, dark-theme CSS vars, `fetch` + `.then(r=>r.json())`.
5. Run tests from repo root with the venv: `venv/bin/python -m pytest ...`. E2e `live` tests need the dashboard running (`venv/bin/python -m server.dashboard`).

---

# Phase 1 — Capture (migration, events, applied-record, extension, quick-add)

### Task 1: Company-name normalizer

**Files:**
- Create: `utils/company_names.py`
- Test: `tests/test_company_names.py`

- [ ] **Step 1: Write the failing test**

```python
"""Company-name normalization — canonical keys for company matching."""

from utils.company_names import normalize_company_name


def test_strips_legal_suffixes_and_case():
    assert normalize_company_name("Chime Financial, Inc") == "chime"
    assert normalize_company_name("Chime Financial, Inc.") == "chime"
    assert normalize_company_name("Affirm") == "affirm"
    assert normalize_company_name("Affirm, Inc.") == "affirm"


def test_strips_punctuation_and_whitespace():
    assert normalize_company_name("  Plaid   Inc ") == "plaid"
    assert normalize_company_name("Intuit - Credit Karma") == "intuit credit karma"


def test_common_corporate_words_removed_only_as_suffix_tokens():
    # "financial" as a suffix token goes; embedded words stay intact
    assert normalize_company_name("Brex Financial") == "brex"
    assert normalize_company_name("Coinbase Global, Inc.") == "coinbase"
    # A company literally named a suffix word alone is preserved
    assert normalize_company_name("Financial") == "financial"


def test_empty_and_none_safe():
    assert normalize_company_name("") == ""
    assert normalize_company_name(None) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_company_names.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'utils.company_names'`

- [ ] **Step 3: Write the implementation**

```python
"""Normalize company names to a canonical matching key.

"Chime Financial, Inc" and "Chime" must land on the same key so jobs
link to Company rows regardless of which scanner/ATS spelled the name.
Used by: migration 006 backfill, scanner ingest, /api/applied/record,
and the Companies rollups.
"""

import re

# Tokens dropped when they appear as TRAILING tokens (repeatedly, so
# "Global, Inc." collapses fully). Never dropped from the front/middle.
_SUFFIX_TOKENS = {
    "inc", "incorporated", "llc", "ltd", "corp", "corporation", "co",
    "company", "financial", "technologies", "technology", "labs",
    "global", "holdings", "group", "plc",
}


def normalize_company_name(name: str | None) -> str:
    if not name:
        return ""
    s = name.casefold()
    s = re.sub(r"[^\w\s]", " ", s)          # punctuation -> space
    tokens = s.split()
    while len(tokens) > 1 and tokens[-1] in _SUFFIX_TOKENS:
        tokens.pop()
    return " ".join(tokens)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_company_names.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add utils/company_names.py tests/test_company_names.py
git commit -m "feat: company-name normalizer for company/job matching"
```

---

### Task 2: Models — `Company`, `ApplicationEvent`, new columns + shared tmp-DB test fixture

**Files:**
- Modify: `db/models.py` (append two model classes; add 2 columns to `Application`, 1 to `Job`)
- Create: `tests/conftest.py` (shared tmp-engine fixture — tests/ has none today; e2e has its own in `tests/e2e/conftest.py`, unaffected)
- Test: `tests/test_models_companies.py`

- [ ] **Step 1: Write the shared fixture** (`tests/conftest.py`)

```python
"""Shared unit-test fixtures: isolated tmp SQLite engine/session.

Unit tests must never touch the real jobpilot.db. `tmp_session` builds a
fresh file-backed SQLite DB per test with the full schema.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base


@pytest.fixture()
def tmp_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'test.db'}", echo=False)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def tmp_session(tmp_engine):
    Session = sessionmaker(bind=tmp_engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture()
def session_factory(tmp_engine):
    """For monkeypatching a router module's get_session."""
    Session = sessionmaker(bind=tmp_engine)
    return lambda: Session()
```

- [ ] **Step 2: Write the failing test** (`tests/test_models_companies.py`)

```python
"""Schema smoke tests for Company / ApplicationEvent and new columns."""

from datetime import datetime, timezone

from db.models import (
    Application,
    ApplicationEvent,
    ApplicationStatus,
    Company,
    Job,
)


def _mk_job(session, company="Chime Financial, Inc", url="https://x.test/1"):
    job = Job(title="AI/ML Engineer", company=company, url=url,
              source="test", dedup_hash=url)
    session.add(job)
    session.flush()
    return job


def test_company_row_roundtrip(tmp_session):
    c = Company(name="Chime", name_normalized="chime",
                careers_url="https://careers.chime.com",
                ats_platform="greenhouse", priority="high", status="target",
                overview_md="SF consumer fintech.", why_fit_md="Fits.",
                hiring_bar_md="1-2 YOE ok.", profile_source="seeded")
    tmp_session.add(c)
    tmp_session.commit()
    got = tmp_session.query(Company).filter_by(name_normalized="chime").one()
    assert got.suggested is False
    assert got.draft_status is None
    assert got.notes_md is None


def test_job_links_to_company(tmp_session):
    c = Company(name="Chime", name_normalized="chime")
    tmp_session.add(c)
    tmp_session.flush()
    job = _mk_job(tmp_session)
    job.company_id = c.id
    tmp_session.commit()
    assert tmp_session.query(Job).one().company_id == c.id


def test_application_event_rows(tmp_session):
    job = _mk_job(tmp_session)
    app = Application(job_id=job.id, status=ApplicationStatus.FOUND,
                      lead_source="Bianca / Vantage Point")
    tmp_session.add(app)
    tmp_session.flush()
    ev = ApplicationEvent(application_id=app.id,
                          occurred_at=datetime.now(timezone.utc),
                          from_status="found", to_status="applied",
                          source="dashboard")
    tmp_session.add(ev)
    tmp_session.commit()
    assert tmp_session.query(ApplicationEvent).count() == 1
    assert app.next_action is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_models_companies.py -v`
Expected: FAIL with `ImportError: cannot import name 'Company'`

- [ ] **Step 4: Add the models** — append to `db/models.py` (after `Application`, before `ScanLog`), and add columns.

Append these classes:

```python
class Company(Base):
    """A target company with a tailored profile (Companies tab)."""

    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(300), nullable=False, unique=True)
    name_normalized = Column(String(300), nullable=False)
    careers_url = Column(String(2000), nullable=True)
    ats_platform = Column(String(50), nullable=True)   # greenhouse|ashby|lever|workday|radancy|custom
    priority = Column(String(20), nullable=True)       # high | medium | low
    status = Column(String(20), default="target")      # target | watch | paused

    # Profile fields — LLM/seed-owned except notes_md (user-owned, never
    # written by the LLM or seed refresh).
    overview_md = Column(Text, nullable=True)
    why_fit_md = Column(Text, nullable=True)
    hiring_bar_md = Column(Text, nullable=True)
    notes_md = Column(Text, nullable=True)

    profile_source = Column(String(20), default="manual")  # seeded | llm | manual
    profile_backup = Column(JSON, nullable=True)   # pre-refresh snapshot (one-step undo)
    draft_status = Column(String(20), nullable=True)  # drafting | failed | None=idle
    suggested = Column(Boolean, default=False, nullable=False)
    last_refreshed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_companies_name_normalized", "name_normalized"),
    )

    def __repr__(self):
        return f"<Company(id={self.id}, name='{self.name}')>"


class ApplicationEvent(Base):
    """Historized status change — one row per transition (advisor timeline)."""

    __tablename__ = "application_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    occurred_at = Column(DateTime, nullable=False)
    from_status = Column(String(50), nullable=True)   # null for creation/backfill
    to_status = Column(String(50), nullable=False)
    note = Column(Text, nullable=True)
    # dashboard | extension | auto_applier | review_ui | quick_add | backfill
    source = Column(String(30), nullable=False)

    application = relationship("Application", back_populates="events")

    __table_args__ = (
        Index("idx_application_events_app_id", "application_id"),
        Index("idx_application_events_occurred", "occurred_at"),
    )

    def __repr__(self):
        return f"<ApplicationEvent(app_id={self.application_id}, to='{self.to_status}')>"
```

Add to `Application` (after the `notes` column):

```python
    lead_source = Column(String(100), nullable=True)   # "Bianca / Vantage Point", "scanner", ...
    next_action = Column(Text, nullable=True)
```

Add to `Application` relationships:

```python
    events = relationship("ApplicationEvent", back_populates="application",
                          order_by="ApplicationEvent.occurred_at")
```

Add to `Job` (after `extra_urls`):

```python
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
```

And to `Job.__table_args__` add: `Index("idx_jobs_company_id", "company_id"),`

- [ ] **Step 5: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_models_companies.py -v`
Expected: 3 passed

- [ ] **Step 6: Run the full unit suite to catch model regressions**

Run: `venv/bin/python -m pytest tests/ -m "not live" -q`
Expected: no new failures vs before the change (baseline first if unsure: `git stash && pytest && git stash pop`)

- [ ] **Step 7: Commit**

```bash
git add db/models.py tests/conftest.py tests/test_models_companies.py
git commit -m "feat: Company + ApplicationEvent models, lead_source/next_action/company_id columns"
```

---

### Task 3: Migration 006 — column adds + event backfill + company_id backfill

**Files:**
- Create: `db/migrations/006_add_companies_and_events.py`
- Test: `tests/test_migration_006.py`

New tables are created by `create_all`; this migration ALTERs existing tables and backfills.

- [ ] **Step 1: Write the failing test**

```python
"""Migration 006 — idempotent column adds + event/company_id backfill."""

import importlib
from datetime import datetime, timezone

from sqlalchemy import inspect

from db.models import (
    Application, ApplicationEvent, ApplicationStatus, Company, Job,
)

mig = importlib.import_module("db.migrations.006_add_companies_and_events")


def _seed_legacy_app(session, status, date_applied=None, response_date=None):
    job = Job(title="T", company="Chime Financial, Inc",
              url=f"https://x.test/{status.value}", source="test",
              dedup_hash=f"h-{status.value}")
    session.add(job)
    session.flush()
    app = Application(job_id=job.id, status=status,
                      date_applied=date_applied, response_date=response_date)
    session.add(app)
    session.commit()
    return app


def test_run_is_idempotent_and_backfills(tmp_engine, tmp_session, monkeypatch):
    monkeypatch.setattr(mig, "get_engine", lambda: tmp_engine)
    monkeypatch.setattr(mig, "get_session_factory", lambda: (lambda: tmp_session))

    applied_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    _seed_legacy_app(tmp_session, ApplicationStatus.APPLIED, date_applied=applied_at)
    tmp_session.add(Company(name="Chime", name_normalized="chime"))
    tmp_session.commit()

    r1 = mig.run()
    assert r1["events_backfilled"] >= 1
    assert r1["jobs_linked"] == 1

    ev = tmp_session.query(ApplicationEvent).one()
    assert ev.to_status == "applied"
    assert ev.source == "backfill"
    assert ev.occurred_at.replace(tzinfo=timezone.utc) == applied_at

    job = tmp_session.query(Job).one()
    company = tmp_session.query(Company).one()
    assert job.company_id == company.id

    # Second run: nothing double-backfilled
    r2 = mig.run()
    assert r2["events_backfilled"] == 0
    assert tmp_session.query(ApplicationEvent).count() == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_migration_006.py -v`
Expected: FAIL with `ModuleNotFoundError` for the migration module

- [ ] **Step 3: Write the migration**

```python
"""Companies & advisor tracking (spec 2026-07-08).

New TABLES (companies, application_events) come from Base.metadata.create_all
at engine init. This migration:
  1. ALTERs existing tables:  applications.lead_source, applications.next_action,
     jobs.company_id
  2. Backfills approximate ApplicationEvents from existing date fields
     (source="backfill") — only for applications with zero events.
  3. Links jobs.company_id by normalized company name.

Idempotent. Run from the project root:
    python -m db.migrations.006_add_companies_and_events
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.database import get_engine  # noqa: E402
from db.models import Application, ApplicationEvent, Company, Job  # noqa: E402
from utils.company_names import normalize_company_name  # noqa: E402


ALTERS = (
    ("applications", "lead_source", "VARCHAR(100)"),
    ("applications", "next_action", "TEXT"),
    ("jobs", "company_id", "INTEGER"),
)


def get_session_factory():
    return sessionmaker(bind=get_engine())


def _add_columns(engine) -> list[str]:
    inspector = inspect(engine)
    added = []
    with engine.begin() as conn:
        for table, col, coltype in ALTERS:
            existing = {c["name"] for c in inspector.get_columns(table)}
            if col not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
                added.append(f"{table}.{col}")
    return added


def _backfill_events(session) -> int:
    """Synthesize approximate events from legacy date fields (once)."""
    count = 0
    apps = session.query(Application).all()
    with_events = {
        row[0] for row in session.query(ApplicationEvent.application_id).distinct()
    }
    for app in apps:
        if app.id in with_events:
            continue
        stamps = []
        if app.date_applied:
            stamps.append((app.date_applied, "applied"))
        if app.response_date:
            stamps.append((app.response_date, "response_received"))
        if app.interview_date:
            stamps.append((app.interview_date, "interview"))
        current = app.status.value if app.status else "found"
        if current not in {s for _, s in stamps}:
            stamps.append((datetime.now(timezone.utc), current))
        prev = None
        for occurred_at, to_status in sorted(stamps, key=lambda t: t[0]):
            session.add(ApplicationEvent(
                application_id=app.id, occurred_at=occurred_at,
                from_status=prev, to_status=to_status, source="backfill"))
            prev = to_status
            count += 1
    return count


def _link_jobs(session) -> int:
    by_norm = {c.name_normalized: c.id for c in session.query(Company).all()}
    linked = 0
    for job in session.query(Job).filter(Job.company_id.is_(None)).all():
        cid = by_norm.get(normalize_company_name(job.company))
        if cid:
            job.company_id = cid
            linked += 1
    return linked


def run() -> dict:
    engine = get_engine()   # create_all makes the new tables
    added = _add_columns(engine)
    session = get_session_factory()()
    try:
        events = _backfill_events(session)
        linked = _link_jobs(session)
        session.commit()
    finally:
        session.close()
    return {"columns_added": added, "events_backfilled": events, "jobs_linked": linked}


if __name__ == "__main__":
    print(run())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_migration_006.py -v`
Expected: 1 passed

- [ ] **Step 5: Run the migration against the real DB**

Run: `venv/bin/python -m db.migrations.006_add_companies_and_events`
Expected: printed dict — `columns_added` lists 3 on first run, `events_backfilled` > 0 (existing applications), `jobs_linked` = 0 (no companies seeded yet — Task 11 reruns the linker).

- [ ] **Step 6: Commit**

```bash
git add db/migrations/006_add_companies_and_events.py tests/test_migration_006.py
git commit -m "feat: migration 006 — tracking columns + event/company backfill"
```

---

### Task 4: `record_status_change` helper

**Files:**
- Modify: `db/database.py` (append helper)
- Test: `tests/test_record_status_change.py`

- [ ] **Step 1: Write the failing test**

```python
"""record_status_change — single funnel for Application.status writes."""

from datetime import datetime, timezone

from db.database import record_status_change
from db.models import Application, ApplicationEvent, ApplicationStatus, Job


def _mk_app(session, status=ApplicationStatus.FOUND):
    job = Job(title="T", company="Plaid", url="https://x.test/1",
              source="test", dedup_hash="h1")
    session.add(job)
    session.flush()
    app = Application(job_id=job.id, status=status)
    session.add(app)
    session.flush()
    return app


def test_sets_status_dates_and_event(tmp_session):
    app = _mk_app(tmp_session)
    record_status_change(tmp_session, app, ApplicationStatus.APPLIED,
                         source="dashboard", note="via test")
    tmp_session.commit()
    assert app.status == ApplicationStatus.APPLIED
    assert app.date_applied is not None
    ev = tmp_session.query(ApplicationEvent).one()
    assert (ev.from_status, ev.to_status, ev.source, ev.note) == \
        ("found", "applied", "dashboard", "via test")


def test_interview_and_response_stamp_their_dates(tmp_session):
    app = _mk_app(tmp_session, ApplicationStatus.APPLIED)
    record_status_change(tmp_session, app, ApplicationStatus.RESPONSE_RECEIVED,
                         source="dashboard")
    assert app.response_date is not None
    record_status_change(tmp_session, app, ApplicationStatus.INTERVIEW,
                         source="dashboard")
    assert app.interview_date is not None
    tmp_session.commit()
    assert tmp_session.query(ApplicationEvent).count() == 2


def test_same_status_is_noop(tmp_session):
    app = _mk_app(tmp_session, ApplicationStatus.APPLIED)
    record_status_change(tmp_session, app, ApplicationStatus.APPLIED,
                         source="extension")
    tmp_session.commit()
    assert tmp_session.query(ApplicationEvent).count() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_record_status_change.py -v`
Expected: FAIL with `ImportError: cannot import name 'record_status_change'`

- [ ] **Step 3: Implement** — append to `db/database.py`:

```python
def record_status_change(session, application, new_status, *, source, note=None):
    """THE single write-path for Application.status.

    Sets the status, stamps the matching date field (preserving the logic
    previously inlined in dashboard.py / review_app.py / runner.py), and
    appends an ApplicationEvent. Same-status calls are no-ops so callers
    can be idempotent for free. Caller commits.
    """
    from datetime import datetime, timezone

    from db.models import ApplicationEvent, ApplicationStatus

    if application.status == new_status:
        return
    old = application.status.value if application.status else None
    application.status = new_status
    now = datetime.now(timezone.utc)
    if new_status == ApplicationStatus.APPLIED and not application.date_applied:
        application.date_applied = now
    elif new_status == ApplicationStatus.RESPONSE_RECEIVED and not application.response_date:
        application.response_date = now
    elif new_status == ApplicationStatus.INTERVIEW and not application.interview_date:
        application.interview_date = now
    session.add(ApplicationEvent(
        application_id=application.id, occurred_at=now,
        from_status=old, to_status=new_status.value,
        note=note, source=source))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_record_status_change.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add db/database.py tests/test_record_status_change.py
git commit -m "feat: record_status_change — historized single write-path for status"
```

---

### Task 5: Convert the three existing status write-sites + grep-guard

**Files:**
- Modify: `server/dashboard.py` (`api_update_status`, ~line 397)
- Modify: `agents/auto_applier/runner.py` (`_record_result`, ~line 167)
- Modify: `ui/review_app.py` (`update_application_status`, ~line 150)
- Test: `tests/test_status_write_guard.py`

- [ ] **Step 1: Write the failing grep-guard test**

```python
"""Guard: Application.status may only be assigned inside record_status_change.

Prevents new code from bypassing event history. Pattern-based; allowed
files are the helper itself and migrations (which run before events exist).
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ALLOWED = {"db/database.py", "db/migrations"}
PATTERN = re.compile(r"\.status\s*=\s*(ApplicationStatus|new_status|status_enum)")


def test_no_direct_status_assignments():
    offenders = []
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith(("venv/", "tests/", "output/")):
            continue
        if any(rel.startswith(a) for a in ALLOWED):
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PATTERN.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, (
        "Application.status assigned outside record_status_change:\n"
        + "\n".join(offenders))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_status_write_guard.py -v`
Expected: FAIL listing exactly three offenders (dashboard.py, runner.py, review_app.py)

- [ ] **Step 3: Convert `server/dashboard.py` `api_update_status`**

Replace this block inside the endpoint:

```python
        app_obj.status = status_enum
        if status_enum == ApplicationStatus.APPLIED:
            app_obj.date_applied = datetime.now(timezone.utc)
        elif status_enum == ApplicationStatus.INTERVIEW:
            app_obj.interview_date = datetime.now(timezone.utc)
```

with:

```python
        record_status_change(session, app_obj, status_enum, source="dashboard")
```

and extend the existing import line `from db.database import get_session, init_db` to:

```python
from db.database import get_session, init_db, record_status_change
```

- [ ] **Step 4: Convert `agents/auto_applier/runner.py` `_record_result`**

Replace:

```python
    if result.success and result.status == "submitted":
        app.status = ApplicationStatus.APPLIED
        app.date_applied = datetime.now(timezone.utc)
```

with:

```python
    if result.success and result.status == "submitted":
        record_status_change(session, app, ApplicationStatus.APPLIED,
                             source="auto_applier",
                             note=f"auto-apply {result.status}")
```

Add to that file's imports: `from db.database import record_status_change` (alongside its existing db imports).

- [ ] **Step 5: Convert `ui/review_app.py` `update_application_status`**

Replace:

```python
        if app:
            app.status = new_status
            if new_status == ApplicationStatus.APPLIED:
                app.date_applied = datetime.now(timezone.utc)
            session.commit()
```

with:

```python
        if app:
            record_status_change(session, app, new_status, source="review_ui")
            session.commit()
```

Add `record_status_change` to the file's `from db.database import ...` line.

- [ ] **Step 6: Run guard + full unit suite**

Run: `venv/bin/python -m pytest tests/test_status_write_guard.py tests/ -m "not live" -q`
Expected: guard passes; no new failures elsewhere (auto-applier unit tests exercise `_record_result` — they must still pass).

- [ ] **Step 7: Commit**

```bash
git add server/dashboard.py agents/auto_applier/runner.py ui/review_app.py tests/test_status_write_guard.py
git commit -m "refactor: route all status writes through record_status_change"
```

---

### Task 6: `POST /api/applied/record` endpoint

**Files:**
- Create: `server/applied.py`
- Modify: `server/dashboard.py` (mount router — after the existing `app.include_router(import_router)` at ~line 71)
- Test: `tests/test_applied_record.py`

- [ ] **Step 1: Write the failing test**

```python
"""/api/applied/record — match-or-create + idempotent applied marking."""

import pytest
from fastapi.testclient import TestClient

import server.applied as applied_mod
import server.dashboard as dash
from db.models import Application, ApplicationEvent, ApplicationStatus, Job

client = TestClient(dash.app)


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, session_factory):
    monkeypatch.setattr(applied_mod, "get_session", session_factory)


def _mk_job(session_factory, url="https://job-boards.greenhouse.io/affirm/jobs/7485068003"):
    s = session_factory()
    job = Job(title="SWE, Early Career", company="Affirm", url=url,
              source="greenhouse", dedup_hash="h-affirm-1")
    s.add(job)
    s.flush()
    app = Application(job_id=job.id, status=ApplicationStatus.SCORED)
    s.add(app)
    s.commit()
    jid = job.id
    s.close()
    return jid


def test_matches_existing_job_by_url(session_factory):
    jid = _mk_job(session_factory)
    r = client.post("/api/applied/record", json={
        "url": "https://job-boards.greenhouse.io/affirm/jobs/7485068003",
        "source": "extension"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["matched"] and not body["created"]
    s = session_factory()
    app = s.query(Application).filter_by(job_id=jid).one()
    assert app.status == ApplicationStatus.APPLIED
    assert app.lead_source == "extension"
    ev = s.query(ApplicationEvent).one()
    assert (ev.to_status, ev.source) == ("applied", "extension")
    s.close()


def test_idempotent_second_call(session_factory):
    _mk_job(session_factory)
    payload = {"url": "https://job-boards.greenhouse.io/affirm/jobs/7485068003",
               "source": "extension"}
    client.post("/api/applied/record", json=payload)
    r2 = client.post("/api/applied/record", json=payload)
    assert r2.json()["already"] is True
    s = session_factory()
    assert s.query(ApplicationEvent).count() == 1   # no duplicate event
    s.close()


def test_never_regresses_past_applied(session_factory):
    jid = _mk_job(session_factory)
    s = session_factory()
    app = s.query(Application).filter_by(job_id=jid).one()
    app.status = ApplicationStatus.INTERVIEW
    s.commit(); s.close()
    r = client.post("/api/applied/record", json={
        "url": "https://job-boards.greenhouse.io/affirm/jobs/7485068003",
        "source": "dashboard"})
    assert r.json()["already"] is True
    s = session_factory()
    assert s.query(Application).one().status == ApplicationStatus.INTERVIEW
    s.close()


def test_creates_minimal_record_when_no_match(session_factory):
    r = client.post("/api/applied/record", json={
        "url": "https://jobs.example.com/posting/123",
        "title": "Data Engineer", "company": "Brex",
        "lead_source": "Bianca / Vantage Point", "source": "dashboard"})
    body = r.json()
    assert body["ok"] and body["created"]
    s = session_factory()
    job = s.query(Job).one()
    assert (job.title, job.company) == ("Data Engineer", "Brex")
    app = s.query(Application).one()
    assert app.status == ApplicationStatus.APPLIED
    assert app.lead_source == "Bianca / Vantage Point"
    s.close()


def test_company_falls_back_to_hostname(session_factory):
    r = client.post("/api/applied/record", json={
        "url": "https://careers.chime.com/jobs/1/x", "source": "extension"})
    assert r.json()["created"]
    s = session_factory()
    assert s.query(Job).one().company == "careers.chime.com"
    s.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_applied_record.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.applied'`

- [ ] **Step 3: Implement `server/applied.py`**

```python
"""Record an externally-made application (extension button / dashboard quick-add).

POST /api/applied/record  {url, title?, company?, lead_source?, source}
  - match existing Job by exact URL
  - else create a minimal Job+Application directly (no scoring — the JD
    isn't available; the watchdog's ranking cycle skips description-less jobs)
  - mark APPLIED via record_status_change; idempotent — never regresses a
    status at/beyond APPLIED (applied/response/interview/rejected/...).
"""

import logging
from urllib.parse import urlparse

from fastapi import APIRouter
from pydantic import BaseModel

from db.database import get_session, record_status_change
from db.models import Application, ApplicationStatus, Company, Job
from utils.company_names import normalize_company_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/applied", tags=["applied"])

# Stages at-or-beyond APPLIED — recording again must be a no-op.
_AT_OR_PAST_APPLIED = {
    ApplicationStatus.APPLIED, ApplicationStatus.RESPONSE_RECEIVED,
    ApplicationStatus.INTERVIEW, ApplicationStatus.REJECTED,
    ApplicationStatus.NO_RESPONSE,
}


class AppliedPayload(BaseModel):
    url: str
    title: str = ""
    company: str = ""
    lead_source: str = ""
    source: str = "dashboard"   # "extension" | "dashboard"


def _fallback_company(url: str) -> str:
    return urlparse(url).hostname or "unknown"


@router.post("/record")
def record_applied(payload: AppliedPayload):
    session = get_session()
    try:
        job = session.query(Job).filter(Job.url == payload.url).first()
        created = False
        if job is None:
            company = payload.company.strip() or _fallback_company(payload.url)
            job = Job(
                title=payload.title.strip() or "(untitled — edit me)",
                company=company,
                url=payload.url,
                source="applied_manual",
                dedup_hash=payload.url,   # URL-keyed; scanner uses its own hash scheme
            )
            match = session.query(Company).filter(
                Company.name_normalized == normalize_company_name(company)).first()
            if match:
                job.company_id = match.id
            session.add(job)
            session.flush()
            created = True

        app = session.query(Application).filter_by(job_id=job.id).first()
        if app is None:
            app = Application(job_id=job.id, status=ApplicationStatus.FOUND)
            session.add(app)
            session.flush()

        if app.status in _AT_OR_PAST_APPLIED:
            session.commit()
            return {"ok": True, "already": True, "created": created,
                    "matched": not created, "job_id": job.id,
                    "status": app.status.value}

        record_status_change(session, app, ApplicationStatus.APPLIED,
                             source=payload.source)
        app.lead_source = payload.lead_source or payload.source
        session.commit()
        return {"ok": True, "already": False, "created": created,
                "matched": not created, "job_id": job.id, "status": "applied"}
    finally:
        session.close()
```

- [ ] **Step 4: Mount the router** — in `server/dashboard.py`, directly after `app.include_router(import_router)`:

```python
from server.applied import router as applied_router
app.include_router(applied_router)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_applied_record.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add server/applied.py server/dashboard.py tests/test_applied_record.py
git commit -m "feat: /api/applied/record — capture external applications"
```

---

### Task 7: Extension "Mark applied ✓" button

**Files:**
- Modify: `browser-extension/service_worker.js` (new `mark_applied` cmd in the `onMessage` switch, ~line 441)
- Modify: `browser-extension/content/widget.js` (button in panel HTML ~line 135 area + handler near `root.getElementById("go").onclick = run;` ~line 162)
- Modify: `browser-extension/manifest.json` (bump version 1.2.0 → 1.3.0)
- Create: `server/static/qa_applied.html` (QA fixture)

House rules (from repo history): widget handlers live in the isolated world; QA with real mouse clicks — synthetic main-world events don't cross the boundary; widget must survive host-node teardown (existing rebuild logic covers new buttons automatically since the panel re-renders whole).

- [ ] **Step 1: Add the service-worker command**

In `browser-extension/service_worker.js`, inside `chrome.runtime.onMessage.addListener`, add alongside the other `else if` branches:

```javascript
    else if (msg.cmd === "mark_applied") {
      try {
        const r = await fetch(`${BACKEND}/api/applied/record`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: msg.url, title: msg.title || "",
                                 company: msg.company || "",
                                 source: "extension" }),
        });
        sendResponse(r.ok ? await r.json() : { error: `HTTP ${r.status}` });
      } catch (e) { sendResponse({ error: String(e && e.message || e) }); }
    }
```

- [ ] **Step 2: Add the widget button**

In `browser-extension/content/widget.js`, in the panel template next to the Autofill button (`<button class="go" id="go">⚡ Autofill</button>`), add:

```html
<button class="go" id="applied" title="Record that you submitted this application in JobPilot">✓ Mark applied</button>
```

Near `root.getElementById("go").onclick = run;` add:

```javascript
    const appliedBtn = root.getElementById("applied");
    appliedBtn.onclick = async () => {
      appliedBtn.disabled = true;
      appliedBtn.textContent = "Recording…";
      const res = await send({ cmd: "mark_applied",
                               url: location.href, title: document.title });
      if (res && res.ok) {
        appliedBtn.textContent = res.already ? "✓ Already recorded" : "✓ Recorded";
      } else {
        appliedBtn.textContent = "⚠ Retry — backend offline?";
        appliedBtn.disabled = false;
        setTimeout(() => {
          if (root.getElementById("applied") === appliedBtn)
            appliedBtn.textContent = "✓ Mark applied";
        }, 4000);
      }
    };
```

(`send()` is the widget's existing `chrome.runtime.sendMessage` promise wrapper at ~line 60.)

- [ ] **Step 3: Bump manifest version** — in `browser-extension/manifest.json` change `"version": "1.2.0"` to `"version": "1.3.0"`.

- [ ] **Step 4: Create the QA fixture** — `server/static/qa_applied.html`:

```html
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>QA — Mark Applied</title></head>
<body>
<h1>QA fixture: Mark-applied widget button</h1>
<p>Load this page at http://127.0.0.1:7777/static/qa_applied.html with the
extension enabled. The JobPilot pill should appear (bottom-right). Open it,
click <b>✓ Mark applied</b> with a REAL mouse click, and verify:</p>
<ol>
  <li>Button shows "Recording…" then "✓ Recorded".</li>
  <li>A second click shows "✓ Already recorded".</li>
  <li><code>sqlite3 jobpilot.db "SELECT to_status, source FROM application_events ORDER BY id DESC LIMIT 1;"</code> → <code>applied|extension</code></li>
  <li>Delete the QA rows afterwards:
      <code>sqlite3 jobpilot.db "DELETE FROM application_events WHERE application_id IN (SELECT id FROM applications WHERE job_id IN (SELECT id FROM jobs WHERE source='applied_manual' AND url LIKE '%qa_applied%')); DELETE FROM applications WHERE job_id IN (SELECT id FROM jobs WHERE source='applied_manual' AND url LIKE '%qa_applied%'); DELETE FROM jobs WHERE source='applied_manual' AND url LIKE '%qa_applied%';"</code></li>
</ol>
<form><label>Decoy field <input type="text" name="email"></label></form>
</body>
</html>
```

- [ ] **Step 5: Manual QA** — reload the unpacked extension (chrome://extensions), start the dashboard (`venv/bin/python -m server.dashboard`), walk the fixture checklist above with real clicks.
Expected: all four checklist items pass.

- [ ] **Step 6: Commit**

```bash
git add browser-extension/service_worker.js browser-extension/content/widget.js browser-extension/manifest.json server/static/qa_applied.html
git commit -m "feat(extension): Mark-applied widget button -> /api/applied/record (v1.3.0)"
```

---

### Task 8: Dashboard quick-add ("Log apply")

**Files:**
- Modify: `server/static/index.html` — button in the hero controls (next to the `Sync to Sheet` button, ~line 502), modal + JS near the tracker modal code (~line 1165+)

- [ ] **Step 1: Add the button** — next to the Sync-to-Sheet control:

```html
<button class="btn" id="logApplyBtn" title="Record an application you made outside JobPilot (paste the job URL)">+ Log apply</button>
```

(match the Sync-to-Sheet button's classes exactly — inspect that element and reuse.)

- [ ] **Step 2: Add the modal markup** — after the existing `#trackerModal` markup:

```html
<div class="modal-backdrop" id="logApplyModal" hidden>
  <div class="modal">
    <h3>Log an application</h3>
    <p class="muted">Paste the job posting URL. If JobPilot already knows the job it just marks it applied; otherwise it creates a record you can edit.</p>
    <label>Job URL <input type="url" id="laUrl" placeholder="https://…" required></label>
    <label>Title <input type="text" id="laTitle" placeholder="(optional — filled from the posting if known)"></label>
    <label>Company <input type="text" id="laCompany" placeholder="(optional)"></label>
    <label>Lead source
      <select id="laSource">
        <option>Bianca / Vantage Point</option>
        <option>Self-found</option>
        <option>JobRight</option>
        <option>Simplify</option>
        <option>Referral</option>
      </select>
    </label>
    <div class="modal-actions">
      <button class="btn" id="laCancel">Cancel</button>
      <button class="btn primary" id="laSave">Record applied</button>
    </div>
  </div>
</div>
```

(reuse the tracker modal's CSS classes for `.modal`, `.modal-actions`; add small styles only if missing.)

- [ ] **Step 3: Add the JS**

```javascript
// ============================================================
// Quick-add: log an externally-made application
// ============================================================
$("#logApplyBtn").onclick = () => {
  $("#logApplyModal").hidden = false;
  $("#laSource").value = localStorage.getItem("jp_lead_source") || "Bianca / Vantage Point";
  $("#laUrl").focus();
};
$("#laCancel").onclick = () => { $("#logApplyModal").hidden = true; };
$("#laSave").onclick = async () => {
  const url = $("#laUrl").value.trim();
  if (!url) { $("#laUrl").focus(); return; }
  const lead = $("#laSource").value;
  localStorage.setItem("jp_lead_source", lead);
  $("#laSave").disabled = true;
  try {
    const r = await fetch("/api/applied/record", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ url, title: $("#laTitle").value.trim(),
                             company: $("#laCompany").value.trim(),
                             lead_source: lead, source: "dashboard" })});
    const d = await r.json();
    $("#logApplyModal").hidden = true;
    $("#laUrl").value = $("#laTitle").value = $("#laCompany").value = "";
    toast(d.already ? "Already recorded as applied"
          : d.created ? "Recorded — new job created (edit title/company in its drawer)"
          : "Recorded ✓");
    loadAll();          // existing full-refresh helper
  } catch (e) {
    toast("Backend error — application NOT recorded");
  } finally { $("#laSave").disabled = false; }
};
```

If the dashboard has no `toast()` helper, add the minimal one:

```javascript
function toast(msg){
  let t=$("#toast");
  if(!t){ t=document.createElement("div"); t.id="toast";
    t.style.cssText="position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:#111a33;color:#fff;padding:10px 18px;border-radius:10px;z-index:99;box-shadow:var(--shadow-lg);transition:opacity .3s";
    document.body.appendChild(t); }
  t.textContent=msg; t.style.opacity="1";
  clearTimeout(t._h); t._h=setTimeout(()=>t.style.opacity="0", 3200);
}
```

(check for an existing `loadAll` — the boot sequence at ~line 1041 fetches stats+applications; if it's wrapped in a named function use that name, otherwise wrap it into `loadAll()` and call it from boot.)

- [ ] **Step 4: Manual verify** — `venv/bin/python -m server.dashboard`, open http://127.0.0.1:7777, click **+ Log apply**, paste `https://job-boards.greenhouse.io/affirm/jobs/7485068003`, lead source "Bianca / Vantage Point", save.
Expected: toast, job appears in list as applied; `sqlite3 jobpilot.db "SELECT lead_source FROM applications ORDER BY id DESC LIMIT 1;"` → `Bianca / Vantage Point`. Delete the test rows (same cleanup SQL as Task 7 fixture, adjusting the URL LIKE).

- [ ] **Step 5: Commit**

```bash
git add server/static/index.html
git commit -m "feat(dashboard): quick-add modal to log external applications"
```

---

# Phase 2 — Companies tab + trees + seed

### Task 9: Companies read API (`GET /api/companies`, `GET /api/company/{id}`)

**Files:**
- Create: `server/companies.py`
- Modify: `server/dashboard.py` (mount router, after applied_router)
- Test: `tests/test_companies_api.py`

- [ ] **Step 1: Write the failing test**

```python
"""Companies API — rollups + pipeline tree."""

import pytest
from fastapi.testclient import TestClient

import server.companies as companies_mod
import server.dashboard as dash
from db.models import Application, ApplicationStatus, Company, Job

client = TestClient(dash.app)


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, session_factory):
    monkeypatch.setattr(companies_mod, "get_session", session_factory)


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_companies_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.companies'`

- [ ] **Step 3: Implement `server/companies.py`**

```python
"""Companies API — tailored profiles + per-company pipeline trees."""

import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from db.database import get_session
from db.models import Application, ApplicationStatus, Company, Job
from utils.company_names import normalize_company_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["companies"])

# Pipeline stage groups (spec §4).
STAGE_GROUPS = {
    "watching": {ApplicationStatus.FOUND, ApplicationStatus.SCORED,
                 ApplicationStatus.MATERIALS_READY, ApplicationStatus.QUEUED,
                 ApplicationStatus.APPROVED},
    "applied": {ApplicationStatus.APPLIED},
    "in_play": {ApplicationStatus.RESPONSE_RECEIVED, ApplicationStatus.INTERVIEW},
    "closed": {ApplicationStatus.REJECTED, ApplicationStatus.NO_RESPONSE,
               ApplicationStatus.NO_LONGER_AVAILABLE, ApplicationStatus.SKIPPED},
}
STAGE_ORDER = ("watching", "applied", "in_play", "closed")


def _stage_of(status: ApplicationStatus) -> str:
    for stage, members in STAGE_GROUPS.items():
        if status in members:
            return stage
    return "watching"


def _lazy_link(session) -> None:
    """Link any unlinked jobs to companies by normalized name.

    Runs at the top of both GET endpoints so jobs found by scanners AFTER
    seeding attach to their company without touching scanner code. Cheap:
    only scans company_id IS NULL rows (a few hundred max, local SQLite).
    """
    by_norm = {c.name_normalized: c.id
               for c in session.query(Company.name_normalized, Company.id).all()}
    dirty = False
    for job in session.query(Job).filter(Job.company_id.is_(None)).all():
        cid = by_norm.get(normalize_company_name(job.company))
        if cid:
            job.company_id = cid
            dirty = True
    if dirty:
        session.commit()


def _teaser(md: str | None) -> str:
    if not md:
        return ""
    return md.strip().splitlines()[0][:200]


def _company_dict(c: Company) -> dict:
    return {
        "id": c.id, "name": c.name, "careers_url": c.careers_url,
        "ats_platform": c.ats_platform, "priority": c.priority,
        "status": c.status, "suggested": c.suggested,
        "profile_source": c.profile_source, "draft_status": c.draft_status,
        "last_refreshed_at": c.last_refreshed_at.isoformat() if c.last_refreshed_at else None,
    }


@router.get("/companies")
def list_companies():
    session = get_session()
    try:
        _lazy_link(session)
        rows = (session.query(Job, Application)
                .outerjoin(Application, Application.job_id == Job.id)
                .all())
        counts: dict[int | None, dict] = defaultdict(
            lambda: {"watching": 0, "applied": 0, "in_play": 0, "closed": 0})
        last_activity: dict[int | None, datetime] = {}
        other_names: dict[str, str] = {}
        for job, app in rows:
            status = app.status if app else ApplicationStatus.FOUND
            counts[job.company_id][_stage_of(status)] += 1
            stamp = (app.date_applied or job.date_found) if app else job.date_found
            if stamp and (job.company_id not in last_activity or stamp > last_activity[job.company_id]):
                last_activity[job.company_id] = stamp
            if job.company_id is None:
                other_names.setdefault(normalize_company_name(job.company), job.company)

        companies = []
        for c in session.query(Company).order_by(Company.priority.desc().nullslast(),
                                                 Company.name).all():
            d = _company_dict(c)
            d["counts"] = counts.get(c.id, {"watching": 0, "applied": 0,
                                            "in_play": 0, "closed": 0})
            la = last_activity.get(c.id)
            d["last_activity"] = la.isoformat() if la else None
            d["why_fit_teaser"] = _teaser(c.why_fit_md)
            companies.append(d)

        # Unlinked jobs rolled up by normalized name so nothing is invisible.
        other = []
        if None in counts:
            for norm, display in sorted(other_names.items()):
                per = {"watching": 0, "applied": 0, "in_play": 0, "closed": 0}
                for job, app in rows:
                    if job.company_id is None and normalize_company_name(job.company) == norm:
                        per[_stage_of(app.status if app else ApplicationStatus.FOUND)] += 1
                other.append({"name": display, "counts": per})
        return {"companies": companies, "other": other}
    finally:
        session.close()


@router.get("/company/{company_id}")
def company_detail(company_id: int):
    session = get_session()
    try:
        _lazy_link(session)
        c = session.query(Company).get(company_id)
        if not c:
            raise HTTPException(status_code=404, detail="Company not found")
        d = _company_dict(c)
        d.update(overview_md=c.overview_md, why_fit_md=c.why_fit_md,
                 hiring_bar_md=c.hiring_bar_md, notes_md=c.notes_md)

        groups = {stage: [] for stage in STAGE_ORDER}
        rows = (session.query(Job, Application)
                .outerjoin(Application, Application.job_id == Job.id)
                .filter(Job.company_id == company_id)
                .order_by(Job.date_found.desc())
                .all())
        for job, app in rows:
            status = app.status if app else ApplicationStatus.FOUND
            entry = {
                "job_id": job.id, "app_id": app.id if app else None,
                "title": job.title, "url": job.url,
                "fit_score": job.score.fit_score if job.score else None,
                "status": status.value,
                "date_found": job.date_found.isoformat() if job.date_found else None,
                "date_applied": (app.date_applied.isoformat()
                                 if app and app.date_applied else None),
                "lead_source": app.lead_source if app else None,
                "next_action": app.next_action if app else None,
                "events": [
                    {"occurred_at": e.occurred_at.isoformat(),
                     "from": e.from_status, "to": e.to_status,
                     "source": e.source, "note": e.note}
                    for e in (app.events if app else [])
                ],
            }
            groups[_stage_of(status)].append(entry)
        d["tree"] = [{"stage": s, "jobs": groups[s]} for s in STAGE_ORDER]
        return d
    finally:
        session.close()
```

- [ ] **Step 4: Mount** — in `server/dashboard.py` after the applied router:

```python
from server.companies import router as companies_router
app.include_router(companies_router)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_companies_api.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add server/companies.py server/dashboard.py tests/test_companies_api.py
git commit -m "feat: companies read API — rollups + pipeline trees"
```

---

### Task 10: Companies write API (create / edit / delete; refresh stub)

**Files:**
- Modify: `server/companies.py`
- Test: `tests/test_companies_api.py` (append)

The LLM draft itself is Phase 3; `POST /api/companies` and `/refresh` set `draft_status` and call a `draft_profile_task` hook that Phase 3 fills in. Until then the hook is a no-op that clears `draft_status` (documented below), so Phase 2 ships without Ollama.

> Pydantic note: the repo's FastAPI stack is assumed pydantic v2 (`model_dump`). If `payload.model_dump(exclude_unset=True)` raises `AttributeError`, the repo is on v1 — use `payload.dict(exclude_unset=True)` instead (same semantics).

- [ ] **Step 1: Append failing tests**

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/python -m pytest tests/test_companies_api.py -v`
Expected: new tests FAIL with 405/404s (routes missing)

- [ ] **Step 3: Append to `server/companies.py`**

```python
class CompanyCreate(BaseModel):
    name: str
    careers_url: str = ""


class CompanyPatch(BaseModel):
    # all optional — patch semantics
    name: str | None = None
    careers_url: str | None = None
    ats_platform: str | None = None
    priority: str | None = None
    status: str | None = None
    overview_md: str | None = None
    why_fit_md: str | None = None
    hiring_bar_md: str | None = None
    notes_md: str | None = None
    next_action: str | None = None


_PROFILE_FIELDS = {"overview_md", "why_fit_md", "hiring_bar_md"}


def draft_profile_task(company_id: int) -> None:
    """Phase-3 hook (agents/company_profiler.py). Until the profiler lands,
    clear draft_status so cards don't hang in 'drafting'."""
    session = get_session()
    try:
        c = session.query(Company).get(company_id)
        if c and c.draft_status == "drafting":
            c.draft_status = None
            session.commit()
    finally:
        session.close()


@router.post("/companies")
def create_company(payload: CompanyCreate, background_tasks: BackgroundTasks):
    norm = normalize_company_name(payload.name)
    if not norm:
        raise HTTPException(status_code=400, detail="Company name required")
    session = get_session()
    try:
        if session.query(Company).filter_by(name_normalized=norm).first():
            raise HTTPException(status_code=409, detail="Company already exists")
        c = Company(name=payload.name.strip(), name_normalized=norm,
                    careers_url=payload.careers_url or None,
                    profile_source="manual", draft_status="drafting")
        session.add(c)
        # adopt any unlinked jobs that match
        session.flush()
        for job in session.query(Job).filter(Job.company_id.is_(None)).all():
            if normalize_company_name(job.company) == norm:
                job.company_id = c.id
        session.commit()
        background_tasks.add_task(draft_profile_task, c.id)
        return {"id": c.id, "draft_status": "drafting"}
    finally:
        session.close()


@router.patch("/company/{company_id}")
def patch_company(company_id: int, payload: CompanyPatch):
    session = get_session()
    try:
        c = session.query(Company).get(company_id)
        if not c:
            raise HTTPException(status_code=404, detail="Company not found")
        data = payload.model_dump(exclude_unset=True)
        for field, value in data.items():
            setattr(c, field, value)
        if data.keys() & _PROFILE_FIELDS:
            c.profile_source = "manual"
        if "name" in data:
            c.name_normalized = normalize_company_name(data["name"])
        session.commit()
        return {"ok": True}
    finally:
        session.close()


@router.delete("/company/{company_id}")
def delete_company(company_id: int):
    session = get_session()
    try:
        c = session.query(Company).get(company_id)
        if not c:
            raise HTTPException(status_code=404, detail="Company not found")
        for job in session.query(Job).filter(Job.company_id == company_id).all():
            job.company_id = None
        session.delete(c)
        session.commit()
        return {"ok": True}
    finally:
        session.close()


@router.post("/company/{company_id}/refresh")
def refresh_company(company_id: int, background_tasks: BackgroundTasks):
    session = get_session()
    try:
        c = session.query(Company).get(company_id)
        if not c:
            raise HTTPException(status_code=404, detail="Company not found")
        c.profile_backup = {"overview_md": c.overview_md,
                            "why_fit_md": c.why_fit_md,
                            "hiring_bar_md": c.hiring_bar_md,
                            "profile_source": c.profile_source}
        c.draft_status = "drafting"
        session.commit()
        background_tasks.add_task(draft_profile_task, company_id)
        return {"ok": True, "draft_status": "drafting"}
    finally:
        session.close()
```

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m pytest tests/test_companies_api.py -v`
Expected: all pass (create/patch/delete/duplicate/unlink + earlier 3)

- [ ] **Step 5: Commit**

```bash
git add server/companies.py tests/test_companies_api.py
git commit -m "feat: companies write API (create/edit/delete/refresh stub)"
```

---

### Task 11: Seed data + seeder script

**Files:**
- Create: `config/companies_seed.yaml` (content transcribed from `output/research/2026-07-08/*.md` — the authoritative source)
- Create: `scripts/seed_companies.py`
- Modify: `.gitignore` (add seed file under the personal-data block)
- Test: `tests/test_seed_companies.py`

- [ ] **Step 1: Add to `.gitignore`** under `# ─── Personal data` block:

```
config/companies_seed.yaml
```

- [ ] **Step 2: Write the failing test**

```python
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_seed_companies.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Write `scripts/seed_companies.py`**

```python
"""Seed/refresh the companies table from config/companies_seed.yaml.

Idempotent upsert keyed on normalized name. Companies whose profile_source
is "manual" are never overwritten (hand edits win). Re-runnable any time.

Run:  venv/bin/python scripts/seed_companies.py
"""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import get_session  # noqa: E402
from db.models import Company, Job  # noqa: E402
from utils.company_names import normalize_company_name  # noqa: E402

SEED_PATH = Path(__file__).resolve().parents[1] / "config" / "companies_seed.yaml"

PROFILE_FIELDS = ("careers_url", "ats_platform", "priority", "status",
                  "overview_md", "why_fit_md", "hiring_bar_md")


def load_seed() -> list[dict]:
    with open(SEED_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)["companies"]


def run(close: bool = True) -> dict:
    session = get_session()
    created = updated = skipped = 0
    try:
        for entry in load_seed():
            norm = normalize_company_name(entry["name"])
            c = session.query(Company).filter_by(name_normalized=norm).first()
            if c is None:
                c = Company(name=entry["name"], name_normalized=norm,
                            profile_source="seeded",
                            suggested=bool(entry.get("suggested", False)))
                session.add(c)
                created += 1
            elif c.profile_source == "manual":
                skipped += 1
                continue
            else:
                updated += 1
            for field in PROFILE_FIELDS:
                if field in entry:
                    setattr(c, field, entry[field])
        session.flush()
        # link any unlinked jobs to the (possibly new) companies
        by_norm = {c.name_normalized: c.id for c in session.query(Company).all()}
        for job in session.query(Job).filter(Job.company_id.is_(None)).all():
            cid = by_norm.get(normalize_company_name(job.company))
            if cid:
                job.company_id = cid
        session.commit()
    finally:
        if close:
            session.close()
    return {"created": created, "updated": updated, "skipped_manual": skipped}


if __name__ == "__main__":
    print(run())
```

- [ ] **Step 5: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_seed_companies.py -v`
Expected: 2 passed

- [ ] **Step 6: Author `config/companies_seed.yaml`**

Structure — one entry per company, all keys shown below. **Content source:** transcribe/condense from `output/research/2026-07-08/` — `SUMMARY.md` for priorities, per-company files for the three profile sections. Write all 9: Chime, Affirm, Plaid, Coinbase, Ramp, Brex, Intuit / Credit Karma, Stripe (status: watch), Jack & Jill (status: watch). Complete example of the required shape and depth (Chime, from `chime.md`):

```yaml
# Personal seed data — gitignored. Source: output/research/2026-07-08/*.md
companies:
  - name: "Chime"
    careers_url: "https://careers.chime.com"
    ats_platform: "greenhouse"
    priority: "high"
    status: "target"
    overview_md: |
      SF consumer fintech — "a financial technology company, not a bank"
      (banking via Bancorp/Stride). Fee-free checking/savings, Credit
      Builder, SpotMe, MyPay earned-wage access, Instant Loans. IPO'd
      June 2025 (Nasdaq: CHYM); ~1,500 employees, ~66 open reqs.
    why_fit_md: |
      Nearly every eng team sits on a risk / credit / deposits /
      experimentation problem — the econ + data + AI hybrid profile is a
      differentiator vs pure-CS applicants. Two Chime JDs explicitly name
      Claude Code/Cursor as desired skills. AI/ML Engineer (Trust & Safety)
      is the #1 application priority: 1–2 YOE incl. project experience,
      $125k–173k, SF hybrid.
    hiring_bar_md: |
      Transparent bands overlapping the $110–160k target. Backend org is
      Ruby-on-Rails heavy — frame FastAPI as "comparable framework".
      Titles undersell seniority (Deposits & Insights hides a tech-lead
      bar); read requirements, not titles. Almost everything SF 4d/wk
      onsite; exactly one US-remote eng req (Infrastructure, 2–4 YOE).
      TRAPS: Financial Platform = hard Go gate; Deposits & Insights =
      hidden senior bar.
    suggested: false
```

- [ ] **Step 7: Run the seeder + relink against the real DB**

Run: `venv/bin/python scripts/seed_companies.py`
Expected: `{'created': 9, 'updated': 0, 'skipped_manual': 0}`; rerun → `{'created': 0, 'updated': 9, ...}`

- [ ] **Step 8: Commit** (seed YAML is gitignored — commit script/test/gitignore only)

```bash
git add scripts/seed_companies.py tests/test_seed_companies.py .gitignore
git commit -m "feat: companies seeder (idempotent, manual-profiles protected)"
```

---

### Task 12: Companies tab UI (cards + detail + tree)

**Files:**
- Modify: `server/static/index.html`

The nav already contains `<a data-view="companies">Companies</a>` (line ~457) — today it's a dead link. Wire it for real.

- [ ] **Step 1: Add the view container** — after `<section id="boardView" hidden>` block:

```html
<section id="companiesView" hidden>
  <div class="board-head"><h2>Companies</h2>
    <div class="board-sub">Tailored targets — profiles, hiring bars, and your full pipeline at each.</div></div>
  <div id="companiesGrid" class="cgrid"></div>
  <div id="companyDetail" hidden></div>
  <div id="companiesOther"></div>
</section>
```

- [ ] **Step 2: Extend `showView`** — replace the function body additions so companies behaves like board:

```javascript
function showView(v){
  document.querySelectorAll(".topnav a").forEach(x=>x.classList.toggle("active", x.dataset.view===v));
  const board=v==="board", companies=v==="companies", advisor=v==="advisor";
  const hideMain=board||companies||advisor;
  const m=document.querySelector("main"); if(m) m.style.display=hideMain?"none":"";
  const hero=document.querySelector(".hero"); if(hero) hero.style.display=hideMain?"none":"";
  const rb=document.querySelector(".resultsbar"); if(rb) rb.style.display=hideMain?"none":"";
  $("#boardView").hidden=!board;
  $("#companiesView").hidden=!companies;
  const av=$("#advisorView"); if(av) av.hidden=!advisor;   // Task 16 adds it
  $("#insights").classList.toggle("open", v==="insights");
  if(board) renderBoard();
  if(companies) renderCompanies();
}
```

- [ ] **Step 3: Add CSS** (in the style block, near the `.bcard` board styles):

```css
/* Companies tab */
.cgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;padding:18px}
.ccard{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:16px;cursor:pointer}
.ccard:hover{border-color:var(--brand2)}
.ccard h3{margin:0 0 4px;font-size:16px}
.ccard .cmeta{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
.chip{font-size:11px;padding:2px 8px;border-radius:99px;background:#1b2440;color:#c5cee0}
.chip.hi{background:#3b2a10;color:#ffce6a}
.chip.sug{background:#10263b;color:#7cc5ff}
.ccounts{display:flex;gap:10px;font-size:12.5px;color:var(--muted);margin-top:8px}
.ccounts b{color:var(--text)}
.cteaser{font-size:13px;color:var(--muted);margin-top:8px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
/* detail + tree */
.cdetail{padding:18px;max-width:980px}
.cdetail .backlink{cursor:pointer;color:var(--brand2);font-weight:600}
.cprofile h4{margin:18px 0 6px}
.cprofile .pbody{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px;font-size:14px}
.ctree{margin-top:20px}
.cstage{margin:10px 0}
.cstage>summary{cursor:pointer;font-weight:700;padding:6px 0}
.cstage.applied>summary{color:#7ee2a8}
.cjob{display:flex;gap:10px;align-items:center;padding:8px 12px;border:1px solid var(--border);border-radius:10px;margin:6px 0;background:var(--panel)}
.cjob .jtitle{flex:1;font-weight:600}
.cjob .jscore{font-size:12px;color:var(--muted)}
.cjob .jstatus{font-size:11px;padding:2px 8px;border-radius:99px;background:#1b2440}
.cjob.applied .jstatus{background:#123822;color:#7ee2a8}
.jevents{font-size:12px;color:var(--muted);padding:4px 12px 8px 28px}
.jevents .approx{opacity:.7}
```

- [ ] **Step 4: Add the JS** (new section after the board code):

```javascript
// ============================================================
// Companies tab
// ============================================================
function mdLite(md){
  // headings/bold/links/bullets only — matches profile content; all input escaped first
  let h = esc(md||"");
  h = h.replace(/^### (.*)$/gm,"<h5>$1</h5>").replace(/^## (.*)$/gm,"<h4>$1</h4>");
  h = h.replace(/\*\*([^*]+)\*\*/g,"<b>$1</b>");
  h = h.replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g,'<a href="$2" target="_blank">$1</a>');
  h = h.replace(/^- (.*)$/gm,"<li>$1</li>").replace(/(<li>.*<\/li>\n?)+/g, m=>`<ul>${m}</ul>`);
  return h.split(/\n{2,}/).map(p=>p.match(/^<(h4|h5|ul)/)?p:`<p>${p.replace(/\n/g,"<br>")}</p>`).join("");
}

async function renderCompanies(){
  const grid=$("#companiesGrid"); $("#companyDetail").hidden=true; grid.style.display="";
  grid.innerHTML=`<div class="empty">Loading…</div>`;
  const d=await fetch("/api/companies").then(r=>r.json()).catch(()=>null);
  if(!d){ grid.innerHTML=`<div class="empty">Backend unreachable.</div>`; return; }
  grid.innerHTML=d.companies.map(c=>`
    <div class="ccard" data-cid="${c.id}">
      <h3>${esc(c.name)}</h3>
      <div class="cmeta">
        ${c.priority?`<span class="chip ${c.priority==="high"?"hi":""}">${esc(c.priority)}</span>`:""}
        ${c.ats_platform?`<span class="chip">${esc(c.ats_platform)}</span>`:""}
        ${c.suggested?`<span class="chip sug">suggested</span>`:""}
        ${c.draft_status==="drafting"?`<span class="chip">drafting…</span>`:""}
        ${c.draft_status==="failed"?`<span class="chip" style="background:#3b1010;color:#ff8a8a">draft failed</span>`:""}
      </div>
      <div class="ccounts">
        <span>👁 <b>${c.counts.watching}</b></span><span>📨 <b>${c.counts.applied}</b></span>
        <span>💬 <b>${c.counts.in_play}</b></span><span>🗄 <b>${c.counts.closed}</b></span>
      </div>
      <div class="cteaser">${esc(c.why_fit_teaser||"")}</div>
    </div>`).join("") || `<div class="empty">No companies yet — run scripts/seed_companies.py</div>`;
  $("#companiesOther").innerHTML = d.other.length ? `
    <div class="cdetail"><h4>Other companies in the pipeline</h4>
    ${d.other.map(o=>`<div class="cjob"><span class="jtitle">${esc(o.name)}</span>
      <span class="jscore">👁 ${o.counts.watching} · 📨 ${o.counts.applied} · 💬 ${o.counts.in_play} · 🗄 ${o.counts.closed}</span>
      <button class="chip trackco" data-name="${esc(o.name)}" title="Create a tracked company profile — its jobs link automatically and the LLM drafts the profile">+ track</button></div>`).join("")}</div>` : "";
  grid.querySelectorAll(".ccard").forEach(el=>el.onclick=()=>openCompany(+el.dataset.cid));
  $("#companiesOther").querySelectorAll(".trackco").forEach(btn=>btn.onclick=async(e)=>{
    e.stopPropagation();
    btn.disabled=true; btn.textContent="adding…";
    const r=await fetch("/api/companies",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name:btn.dataset.name})}).then(r=>r.json()).catch(()=>null);
    renderCompanies();   // re-render — the new card appears, jobs adopted server-side
  });
}

async function openCompany(cid){
  const det=$("#companyDetail"); $("#companiesGrid").style.display="none";
  $("#companiesOther").innerHTML=""; det.hidden=false;
  det.innerHTML=`<div class="cdetail"><div class="empty">Loading…</div></div>`;
  const c=await fetch(`/api/company/${cid}`).then(r=>r.json()).catch(()=>null);
  if(!c){ det.innerHTML=`<div class="cdetail"><div class="empty">Failed to load.</div></div>`; return; }
  const stageLabel={watching:"👁 Watching / scored",applied:"📨 Applied",in_play:"💬 In play",closed:"🗄 Closed"};
  det.innerHTML=`<div class="cdetail">
    <span class="backlink" id="cback">← All companies</span>
    <h2>${esc(c.name)}</h2>
    <div class="cmeta">
      ${c.ats_platform?`<span class="chip">${esc(c.ats_platform)}</span>`:""}
      ${c.careers_url?`<a class="chip" href="${esc(c.careers_url)}" target="_blank">careers ↗</a>`:""}
      <button class="chip" id="crefresh" title="Re-draft profile with the local LLM (your Notes are never touched)">↻ refresh profile</button>
    </div>
    <div class="cprofile">
      <h4>Overview</h4><div class="pbody">${mdLite(c.overview_md)||"<i>empty</i>"}</div>
      <h4>Why it fits you</h4><div class="pbody">${mdLite(c.why_fit_md)||"<i>empty</i>"}</div>
      <h4>Hiring bar</h4><div class="pbody">${mdLite(c.hiring_bar_md)||"<i>empty</i>"}</div>
      <h4>Notes (yours)</h4><div class="pbody" contenteditable="true" id="cnotes">${esc(c.notes_md||"")}</div>
    </div>
    <div class="ctree">
      ${c.tree.map(g=>`<details class="cstage ${g.stage}" ${g.jobs.length&&g.stage!=="closed"?"open":""}>
        <summary>${stageLabel[g.stage]} (${g.jobs.length})</summary>
        ${g.jobs.map(j=>`
          <div class="cjob ${g.stage==="applied"?"applied":""}">
            <span class="jtitle"><a href="${esc(j.url)}" target="_blank">${esc(j.title)}</a></span>
            ${j.fit_score!=null?`<span class="jscore">${j.fit_score}%</span>`:""}
            ${j.lead_source?`<span class="jscore">via ${esc(j.lead_source)}</span>`:""}
            <span class="jstatus">${esc(j.status)}</span>
          </div>
          ${j.events.length?`<div class="jevents">${j.events.map(e=>
            `<div class="${e.source==="backfill"?"approx":""}">${e.occurred_at.slice(0,10)} — ${e.from?esc(e.from)+" → ":""}${esc(e.to)}${e.source==="backfill"?" ≈":""}</div>`).join("")}</div>`:""}
        `).join("") || `<div class="empty">none</div>`}
      </details>`).join("")}
    </div></div>`;
  $("#cback").onclick=renderCompanies;
  $("#crefresh").onclick=async()=>{ await fetch(`/api/company/${cid}/refresh`,{method:"POST"}); openCompany(cid); };
  $("#cnotes").onblur=async()=>{ await fetch(`/api/company/${cid}`,{method:"PATCH",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({notes_md:$("#cnotes").innerText})}); };
}
```

- [ ] **Step 5: Manual verify** — start dashboard; Companies tab shows 9 seeded cards with live counts (Affirm/Chime should show real pipeline numbers if jobs exist); open Chime → profile renders, tree groups by stage, quick-added Task-8 test job appears under Applied with its event timeline; Notes edit persists on blur (reload page to confirm).

- [ ] **Step 6: Commit**

```bash
git add server/static/index.html
git commit -m "feat(dashboard): Companies tab — profile cards, detail view, pipeline trees"
```

---

# Phase 3 — LLM profiler

### Task 13: `agents/company_profiler.py`

**Files:**
- Create: `agents/company_profiler.py`
- Test: `tests/test_company_profiler.py`

- [ ] **Step 1: Write the failing test**

```python
"""Company profiler — LLM drafting with mocked Ollama + page fetch."""

import agents.company_profiler as prof
from db.models import Company


FAKE_PAGE = "<html><body><h1>Acme Careers</h1><p>We build fintech agents.</p></body></html>"
FAKE_JSON = {"overview_md": "Acme builds fintech agents.",
             "why_fit_md": "Agentic AI overlap.",
             "hiring_bar_md": "Mostly senior; SWE II at 2 YOE.",
             "ats_platform_guess": "greenhouse"}


def _mk_company(session, **kw):
    c = Company(name="Acme", name_normalized="acme",
                careers_url="https://acme.test/careers",
                draft_status="drafting", **kw)
    session.add(c)
    session.commit()
    return c


def test_draft_fills_profile(tmp_session, monkeypatch):
    monkeypatch.setattr(prof, "_fetch_page_text", lambda url: "Acme Careers. We build fintech agents.")
    monkeypatch.setattr(prof, "generate_json", lambda *a, **k: dict(FAKE_JSON))
    monkeypatch.setattr(prof, "get_session", lambda: tmp_session)
    c = _mk_company(tmp_session)
    prof.draft_profile(c.id)
    tmp_session.refresh(c)
    assert c.overview_md == "Acme builds fintech agents."
    assert c.profile_source == "llm"
    assert c.draft_status is None
    assert c.ats_platform == "greenhouse"
    assert c.last_refreshed_at is not None


def test_draft_failure_sets_failed(tmp_session, monkeypatch):
    monkeypatch.setattr(prof, "_fetch_page_text", lambda url: "text")
    def boom(*a, **k): raise RuntimeError("ollama down")
    monkeypatch.setattr(prof, "generate_json", boom)
    monkeypatch.setattr(prof, "get_session", lambda: tmp_session)
    c = _mk_company(tmp_session)
    prof.draft_profile(c.id)
    tmp_session.refresh(c)
    assert c.draft_status == "failed"
    assert c.overview_md is None          # nothing half-written


def test_notes_never_touched(tmp_session, monkeypatch):
    monkeypatch.setattr(prof, "_fetch_page_text", lambda url: "text")
    monkeypatch.setattr(prof, "generate_json", lambda *a, **k: dict(FAKE_JSON))
    monkeypatch.setattr(prof, "get_session", lambda: tmp_session)
    c = _mk_company(tmp_session, notes_md="MY NOTES")
    prof.draft_profile(c.id)
    tmp_session.refresh(c)
    assert c.notes_md == "MY NOTES"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_company_profiler.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `agents/company_profiler.py`**

```python
"""Draft a tailored company profile with the local LLM.

Input: careers-page text + the same resume summary the ranker uses.
Output: overview_md / why_fit_md / hiring_bar_md written onto the Company
row. Runs as a FastAPI background task (sync function, threadpool).
notes_md is user-owned and never written here.

v1 fetches with requests only (spec mentioned a Playwright fallback for
JS-rendered pages — deliberately deferred: a thin page yields a thin,
honest profile per the system prompt, and the user can hand-edit; revisit
if drafts for JS-heavy sites prove useless).
"""

import logging
import re
from datetime import datetime, timezone

import requests

from agents.ranker import _load_resume_summary
from db.database import get_session
from db.models import Company
from utils.ollama_client import generate_json

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) JobPilot/1.0"}
_MAX_PAGE_CHARS = 8000   # leave room in 16k ctx for resume + instructions

_SYSTEM = (
    "You write concise, honest company profiles for one specific job seeker. "
    "Never invent facts not present in the provided page text; if the page "
    "says little, say little. Markdown, plain claims, no hype.")

_PROMPT = """CAREERS PAGE TEXT for {name}:
---
{page}
---

THE CANDIDATE (profile summary):
---
{resume}
---

Write a JSON object with exactly these keys:
- "overview_md": 3-5 sentences — what the company does, size/stage if stated.
- "why_fit_md": 3-5 sentences — why THIS candidate's background maps to them.
- "hiring_bar_md": 2-4 sentences — seniority mix, experience bars, comp if shown.
- "ats_platform_guess": one of greenhouse|ashby|lever|workday|radancy|custom|unknown.
Only state what the page text supports."""


def _fetch_page_text(url: str) -> str:
    """Fetch + strip a careers page to plain-ish text (no new deps)."""
    r = requests.get(url, headers=_UA, timeout=20)
    r.raise_for_status()
    html = r.text
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_PAGE_CHARS]


def draft_profile(company_id: int) -> None:
    session = get_session()
    try:
        c = session.query(Company).get(company_id)
        if not c:
            return
        try:
            page = _fetch_page_text(c.careers_url) if c.careers_url else ""
            resume = _load_resume_summary()
            data = generate_json(
                _PROMPT.format(name=c.name, page=page or "(no page available)",
                               resume=resume),
                system_prompt=_SYSTEM)
            c.overview_md = data.get("overview_md") or c.overview_md
            c.why_fit_md = data.get("why_fit_md") or c.why_fit_md
            c.hiring_bar_md = data.get("hiring_bar_md") or c.hiring_bar_md
            guess = (data.get("ats_platform_guess") or "").strip().lower()
            if guess in {"greenhouse", "ashby", "lever", "workday", "radancy", "custom"} \
                    and not c.ats_platform:
                c.ats_platform = guess
            c.profile_source = "llm"
            c.draft_status = None
            c.last_refreshed_at = datetime.now(timezone.utc)
        except Exception as e:
            logger.error(f"[profiler] draft failed for {c.name}: {e}")
            c.draft_status = "failed"
        session.commit()
    finally:
        session.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_company_profiler.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add agents/company_profiler.py tests/test_company_profiler.py
git commit -m "feat: company_profiler — LLM-drafted tailored company profiles"
```

---

### Task 14: Wire the profiler into create/refresh

**Files:**
- Modify: `server/companies.py` (replace the `draft_profile_task` stub body)
- Test: `tests/test_companies_api.py` (append)

- [ ] **Step 1: Append failing test**

```python
def test_create_triggers_profiler(monkeypatch, session_factory):
    calls = []
    monkeypatch.setattr(companies_mod, "draft_profile", lambda cid: calls.append(cid))
    r = client.post("/api/companies", json={"name": "Mercury"})
    assert r.status_code == 200
    assert calls == [r.json()["id"]]
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/bin/python -m pytest tests/test_companies_api.py::test_create_triggers_profiler -v`
Expected: FAIL (`draft_profile` not an attribute of server.companies)

- [ ] **Step 3: Replace the stub** — in `server/companies.py`, delete the `draft_profile_task` function and change both `background_tasks.add_task(draft_profile_task, ...)` call sites to `background_tasks.add_task(draft_profile, ...)`, importing at top:

```python
from agents.company_profiler import draft_profile
```

- [ ] **Step 4: Run the whole companies + profiler suite**

Run: `venv/bin/python -m pytest tests/test_companies_api.py tests/test_company_profiler.py -v`
Expected: all pass

- [ ] **Step 5: Live smoke test** — with the dashboard + ollama running:
`curl -s -X POST http://127.0.0.1:7777/api/companies -H 'Content-Type: application/json' -d '{"name":"SoFi","careers_url":"https://www.sofi.com/careers/"}'`
Expected: `{"id":N,"draft_status":"drafting"}`; within ~1-2 min `GET /api/company/N` shows filled profile, `draft_status: null`, `profile_source: "llm"`. (gemma4:e4b at ~25 tok/s — be patient.) Delete the test company after: `curl -X DELETE http://127.0.0.1:7777/api/company/N`.

- [ ] **Step 6: Add Claude's ~5 suggested companies** using the live feature (this is the spec's "suggested companies" requirement): POST each of **SoFi, Mercury, Gusto, Block (Square), Upstart** via `/api/companies`, then mark them suggested:
`sqlite3 jobpilot.db "UPDATE companies SET suggested=1 WHERE name IN ('SoFi','Mercury','Gusto','Block (Square)','Upstart');"`
Review each drafted profile in the UI; hand-fix anything gemma got wrong (edits flip `profile_source` to manual, protecting them from seed reruns).

- [ ] **Step 7: Commit**

```bash
git add server/companies.py tests/test_companies_api.py
git commit -m "feat: wire company_profiler into create/refresh background tasks"
```

---

# Phase 4 — Advisor report

### Task 15: `GET /api/advisor/report`

**Files:**
- Create: `server/advisor.py`
- Modify: `server/dashboard.py` (mount router)
- Test: `tests/test_advisor_report.py`

- [ ] **Step 1: Write the failing test**

```python
"""Advisor report — since-date aggregation from application_events."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import server.advisor as advisor_mod
import server.dashboard as dash
from db.models import Application, ApplicationEvent, ApplicationStatus, Company, Job

client = TestClient(dash.app)


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, session_factory):
    monkeypatch.setattr(advisor_mod, "get_session", session_factory)


def _seed(session_factory):
    s = session_factory()
    c = Company(name="Plaid", name_normalized="plaid")
    s.add(c); s.flush()
    def ev(app_id, day, frm, to, source="dashboard"):
        s.add(ApplicationEvent(application_id=app_id,
                               occurred_at=datetime(2026, 7, day, tzinfo=timezone.utc),
                               from_status=frm, to_status=to, source=source))
    j1 = Job(title="SWE Backend", company="Plaid", url="https://p.test/1",
             source="test", dedup_hash="p1", company_id=c.id)
    s.add(j1); s.flush()
    a1 = Application(job_id=j1.id, status=ApplicationStatus.INTERVIEW,
                     lead_source="Bianca / Vantage Point")
    s.add(a1); s.flush()
    ev(a1.id, 2, "scored", "applied")
    ev(a1.id, 6, "applied", "response_received")
    ev(a1.id, 7, "response_received", "interview")
    j2 = Job(title="Old role", company="Plaid", url="https://p.test/2",
             source="test", dedup_hash="p2", company_id=c.id)
    s.add(j2); s.flush()
    a2 = Application(job_id=j2.id, status=ApplicationStatus.APPLIED)
    s.add(a2); s.flush()
    ev(a2.id, 1, None, "applied", source="backfill")   # before the window
    s.commit(); s.close()


def test_report_counts_and_sections(session_factory):
    _seed(session_factory)
    r = client.get("/api/advisor/report?since=2026-07-02")
    assert r.status_code == 200
    body = r.json()
    assert body["stats"] == {"applied": 1, "responses": 1, "interviews": 1,
                             "closed": 0}
    plaid = next(sec for sec in body["companies"] if sec["company"] == "Plaid")
    titles = [a["title"] for a in plaid["applications"]]
    assert "SWE Backend" in titles and "Old role" not in titles
    swe = next(a for a in plaid["applications"] if a["title"] == "SWE Backend")
    assert swe["lead_source"] == "Bianca / Vantage Point"
    assert [e["to"] for e in swe["events"]] == ["applied", "response_received", "interview"]


def test_report_bad_since_400(session_factory):
    assert client.get("/api/advisor/report?since=nonsense").status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_advisor_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.advisor'`

- [ ] **Step 3: Implement `server/advisor.py`**

```python
"""Advisor report — everything that happened since a given date.

Built for Vantage Point meetings: headline stats + per-company application
timelines, aggregated from application_events (the historized record).
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from db.database import get_session
from db.models import Application, ApplicationEvent, Job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/advisor", tags=["advisor"])

_RESPONSES = {"response_received"}
_INTERVIEWS = {"interview"}
_CLOSED = {"rejected", "no_response", "no_longer_available"}


@router.get("/report")
def advisor_report(since: str):
    try:
        since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="since must be YYYY-MM-DD")

    session = get_session()
    try:
        rows = (session.query(ApplicationEvent, Application, Job)
                .join(Application, ApplicationEvent.application_id == Application.id)
                .join(Job, Application.job_id == Job.id)
                .filter(ApplicationEvent.occurred_at >= since_dt.replace(tzinfo=None))
                .order_by(ApplicationEvent.occurred_at)
                .all())

        stats = {"applied": 0, "responses": 0, "interviews": 0, "closed": 0}
        by_app: dict[int, dict] = {}
        for ev, app, job in rows:
            if ev.to_status == "applied":
                stats["applied"] += 1
            elif ev.to_status in _RESPONSES:
                stats["responses"] += 1
            elif ev.to_status in _INTERVIEWS:
                stats["interviews"] += 1
            elif ev.to_status in _CLOSED:
                stats["closed"] += 1
            entry = by_app.setdefault(app.id, {
                "title": job.title, "company": job.company,
                "company_id": job.company_id, "url": job.url,
                "status": app.status.value, "lead_source": app.lead_source,
                "next_action": app.next_action,
                "date_applied": app.date_applied.isoformat() if app.date_applied else None,
                "events": []})
            entry["events"].append({
                "occurred_at": ev.occurred_at.isoformat(),
                "from": ev.from_status, "to": ev.to_status,
                "source": ev.source, "approx": ev.source == "backfill"})

        by_company: dict[str, list] = defaultdict(list)
        for entry in by_app.values():
            by_company[entry["company"]].append(entry)
        companies = [{"company": name, "applications": apps}
                     for name, apps in sorted(by_company.items())]
        return {"since": since, "generated_at": datetime.now(timezone.utc).isoformat(),
                "stats": stats, "companies": companies}
    finally:
        session.close()
```

- [ ] **Step 4: Mount** — in `server/dashboard.py` after the companies router:

```python
from server.advisor import router as advisor_router
app.include_router(advisor_router)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_advisor_report.py -v`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add server/advisor.py server/dashboard.py tests/test_advisor_report.py
git commit -m "feat: advisor report API — since-date event aggregation"
```

---

### Task 16: Advisor tab UI + print stylesheet

**Files:**
- Modify: `server/static/index.html` — nav link, view section, JS, print CSS

- [ ] **Step 1: Add nav link** — in the `<nav class="topnav">` after the Companies link:

```html
    <a data-view="advisor">Advisor</a>
```

- [ ] **Step 2: Add the view section** — after `#companiesView`:

```html
<section id="advisorView" hidden>
  <div class="board-head"><h2>Advisor report</h2>
    <div class="board-sub no-print">For Vantage Point check-ins — pick your last meeting date, review, print.</div></div>
  <div class="adv-controls no-print">
    <label>Since <input type="date" id="advSince"></label>
    <button class="btn" id="advRun">Run report</button>
    <button class="btn" id="advPrint">🖨 Print / PDF</button>
  </div>
  <div id="advBody"><div class="empty">Pick a date and run the report.</div></div>
</section>
```

- [ ] **Step 3: Add CSS** (screen styles near companies CSS, print block at the very end of the style element):

```css
/* Advisor tab */
.adv-controls{display:flex;gap:12px;align-items:center;padding:0 18px 12px}
.adv-stats{display:flex;gap:14px;padding:0 18px 14px}
.adv-stat{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:12px 20px;text-align:center}
.adv-stat b{font-size:22px;display:block}
.adv-co{padding:8px 18px}
.adv-co h3{margin:14px 0 6px}
.adv-app{border:1px solid var(--border);border-radius:10px;background:var(--panel);padding:10px 14px;margin:8px 0}
.adv-app .ahead{display:flex;gap:10px;align-items:baseline}
.adv-app .ahead .t{font-weight:700;flex:1}
.adv-ev{font-size:12.5px;color:var(--muted);margin-top:6px}
.adv-hdr{display:none}

@media print{
  body{background:#fff;color:#000}
  .hero,.topnav,.topicons,main,.resultsbar,#boardView,#companiesView,#insights,.no-print{display:none!important}
  #advisorView{display:block!important}
  #advisorView[hidden]{display:none!important}
  .adv-hdr{display:block;font-size:18px;font-weight:800;margin:0 0 4px}
  .adv-stat,.adv-app{border:1px solid #999;background:#fff;color:#000;break-inside:avoid}
  .adv-ev,.board-sub{color:#333}
  a{color:#000;text-decoration:none}
}
```

- [ ] **Step 4: Add the JS**

```javascript
// ============================================================
// Advisor report
// ============================================================
function advisorInit(){
  const saved=localStorage.getItem("jp_adv_since");
  $("#advSince").value = saved || new Date(Date.now()-14*864e5).toISOString().slice(0,10);
  $("#advRun").onclick=runAdvisorReport;
  $("#advPrint").onclick=()=>window.print();
}
async function runAdvisorReport(){
  const since=$("#advSince").value; if(!since) return;
  localStorage.setItem("jp_adv_since", since);
  $("#advBody").innerHTML=`<div class="empty">Building report…</div>`;
  const d=await fetch(`/api/advisor/report?since=${since}`).then(r=>r.json()).catch(()=>null);
  if(!d){ $("#advBody").innerHTML=`<div class="empty">Backend unreachable.</div>`; return; }
  $("#advBody").innerHTML=`
    <div class="adv-hdr">Matthew Cromaz — application report, ${esc(d.since)} → ${d.generated_at.slice(0,10)}</div>
    <div class="adv-stats">
      <div class="adv-stat"><b>${d.stats.applied}</b>applied</div>
      <div class="adv-stat"><b>${d.stats.responses}</b>responses</div>
      <div class="adv-stat"><b>${d.stats.interviews}</b>interviews</div>
      <div class="adv-stat"><b>${d.stats.closed}</b>closed</div>
    </div>
    ${d.companies.map(sec=>`<div class="adv-co"><h3>${esc(sec.company)}</h3>
      ${sec.applications.map(a=>`<div class="adv-app">
        <div class="ahead"><span class="t">${esc(a.title)}</span>
          <span class="jstatus">${esc(a.status)}</span>
          ${a.lead_source?`<span class="jscore">via ${esc(a.lead_source)}</span>`:""}</div>
        <div class="adv-ev">${a.events.map(e=>
          `${e.occurred_at.slice(0,10)} ${e.from?esc(e.from)+" → ":""}${esc(e.to)}${e.approx?" ≈":""}`).join(" · ")}</div>
        ${a.next_action?`<div class="adv-ev"><b>Next:</b> ${esc(a.next_action)}</div>`:""}
      </div>`).join("")}</div>`).join("") || `<div class="empty">Nothing in this window.</div>`}`;
}
advisorInit();
```

Also add to `showView`: on `advisor===true`, call `runAdvisorReport()` if `$("#advBody .adv-stats")` is absent (auto-run with the remembered date).

- [ ] **Step 5: Manual verify** — Advisor tab: pick 2026-07-01, Run; stats reflect real events (the backfilled ones show ≈); Print preview (⌘P) shows only the report, white background, header line present.

- [ ] **Step 6: Commit**

```bash
git add server/static/index.html
git commit -m "feat(dashboard): Advisor tab — since-date report with print-to-PDF"
```

---

### Task 17: E2E contract tests + final verification sweep

**Files:**
- Create: `tests/e2e/test_companies_advisor_contracts.py`
- Modify: `README.md` (document the two new tabs + quick-add + extension button, in the existing feature-list style)

- [ ] **Step 1: Write the live contract tests**

```python
"""Contract tests for companies/advisor/applied endpoints (live server)."""

import pytest
import requests

pytestmark = pytest.mark.live


def test_companies_list_shape(base_url):
    r = requests.get(f"{base_url}/api/companies", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert "companies" in body and "other" in body
    if body["companies"]:
        c = body["companies"][0]
        for key in ("id", "name", "counts", "why_fit_teaser", "draft_status"):
            assert key in c
        assert set(c["counts"]) == {"watching", "applied", "in_play", "closed"}


def test_company_detail_shape(base_url):
    companies = requests.get(f"{base_url}/api/companies", timeout=5).json()["companies"]
    if not companies:
        pytest.skip("no companies seeded")
    d = requests.get(f"{base_url}/api/company/{companies[0]['id']}", timeout=5).json()
    assert [g["stage"] for g in d["tree"]] == ["watching", "applied", "in_play", "closed"]
    for key in ("overview_md", "why_fit_md", "hiring_bar_md", "notes_md"):
        assert key in d


def test_advisor_report_shape(base_url):
    r = requests.get(f"{base_url}/api/advisor/report?since=2026-01-01", timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert set(body["stats"]) == {"applied", "responses", "interviews", "closed"}
    assert isinstance(body["companies"], list)


def test_advisor_report_validates_since(base_url):
    assert requests.get(f"{base_url}/api/advisor/report?since=bad", timeout=5).status_code == 400


def test_applied_record_requires_url(base_url):
    r = requests.post(f"{base_url}/api/applied/record", json={}, timeout=5)
    assert r.status_code == 422   # pydantic validation
```

- [ ] **Step 2: Run everything**

Run: `venv/bin/python -m pytest tests/ -m "not live" -q` → Expected: all pass.
Start the dashboard, then: `venv/bin/python -m pytest tests/e2e/test_companies_advisor_contracts.py -m live -v` → Expected: all pass/skip.

- [ ] **Step 3: Update README** — add to the feature list: Companies tab (tailored profiles + pipeline trees), Advisor report (print-to-PDF), external-apply capture (extension ✓ button + Log-apply modal), `application_events` history. Match the README's existing tone/format (it was updated in e4624fb — follow that structure).

- [ ] **Step 4: Final QA checklist (manual, real browser + extension)**

1. Quick-add one of Bianca's real leads (e.g. Chime AI/ML Engineer URL) with lead source "Bianca / Vantage Point" → appears under Chime's Applied branch.
2. Companies tab: 9 seeded + 5 suggested cards; open Chime — profile + tree render.
3. Change a status via the Board (drag) → company detail timeline gains the event.
4. Advisor: since = two weeks ago → the quick-added app + status change appear; ⌘P preview is clean.
5. Extension: on the Task-7 fixture, ✓ Mark applied → recorded (then clean the QA rows).

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/test_companies_advisor_contracts.py README.md
git commit -m "test: live contracts for companies/advisor/applied + README update"
```

---

## Post-plan notes for the executor

- After all phases: run `venv/bin/python scripts/seed_companies.py` once more (Task 11 seeds may have been added before migration backfill linked jobs); confirm `jobs_linked` in a rerun of migration 006 is 0 (everything linked).
- The watchdog/scheduler needs a restart to pick up server changes: `launchctl kickstart -k gui/$(id -u)/com.jobpilot.watchdog` (or stop/start the dashboard manually while developing).
- Remaining ideas deliberately NOT in scope (spec non-goals): email response scanning, Google-Sheet advisor sharing, scheduled profile refresh.
