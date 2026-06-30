# Google Sheet Tracker Sync — Design

**Date:** 2026-06-30
**Status:** Approved (auth method chosen: service account)

## Goal

A dashboard feature to **compare** the jobs Matthew has applied to against his
personal tracker Google Sheet and **add** the ones that aren't there yet — never
creating duplicates. One-directional push (JobPilot → Sheet) plus a read-only
diff so he can see what's already logged.

Target sheet:
`https://docs.google.com/spreadsheets/d/1ljhaUbcWmiPgejBcz43OdNy4vJCxtR5ZpE_manKd-80/edit`

## Why this shape

The repo was already scaffolded for Sheets: `gspread` is installed, `google-auth*`
are in `requirements.txt`, and `.gitignore` lists `config/google_credentials.json`.
The integration was never built. This feature completes that scaffold using a
**service account** — fully automatic two-way access (read to compare, append to
add), no OAuth consent popups, secret key file gitignored.

## Architecture

Mirrors the existing autofill router pattern (`server/autofill.py` +
`app.include_router(...)` in `server/dashboard.py`). Two layers:

### 1. Pure logic — `agents/sheet_sync.py` (no network, fully unit-tested)

- `normalize(s)` — lowercase, collapse whitespace, strip punctuation.
- `CANONICAL_COLUMNS` — ordered list of `(field_key, header_label, synonyms)` for
  the columns JobPilot knows how to produce: Date Applied, Company, Title,
  Location, Pay, Source, Status, Fit, URL, Notes.
- `map_headers(sheet_header_row) -> {field_key: column_index}` — adaptively maps
  JobPilot's fields onto the user's *existing* sheet headers by normalized
  synonym match. Each sheet column maps to at most one field; first match wins.
- `default_header() -> list[str]` — the canonical header row, written only when
  the sheet is empty.
- `row_key(company, title, url) -> tuple` — dedup identity: `(url_norm,
  company_title_norm)`. Two records match if URLs are equal **or** company+title
  are equal.
- `build_row(app, header_order) -> list[str]` — produce a cell list aligned to the
  sheet's column order, filling only mapped columns, blanks elsewhere.
- `compare(apps, sheet_rows, header_map) -> {new, in_sheet, orphans}` — diff
  applied applications against sheet rows.

### 2. API + gspread glue — `server/sheets.py` (`APIRouter(prefix="/api/sheet")`)

- `_load_config()` — read `google_sheets:` from `config/settings.yaml`.
- `_open_worksheet()` — gspread service-account client → worksheet, or raise a
  typed `SheetNotConnected` with a human reason (missing creds / not shared /
  bad id). Never crashes the dashboard.
- `_applied_applications()` — own DB query (no import of `dashboard.py`, avoiding
  a circular import — same precedent as `autofill.py`), returns applied-group
  apps serialized to the fields above.

Endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/sheet/health` | `{connected, reason, spreadsheet_url, worksheet, row_count, header_map}` |
| GET | `/api/sheet/compare` | `{connected, new:[...], in_sheet:[...], orphans:[...], counts}` |
| POST | `/api/sheet/sync` | body `{app_ids:[...]}` → append rows; `{added, skipped, errors}` |

All three degrade gracefully when creds are absent: `health` returns
`connected:false` with a reason; `compare`/`sync` return HTTP 200 with
`connected:false` so the UI can show a setup prompt instead of erroring.

### 3. Dashboard UI — `server/static/index.html`

- A **Tracker** button in the header (next to existing controls).
- Opens a modal: connection dot + reason, "Open sheet ↗" link, a list of
  applied jobs each badged **✓ In sheet** / **+ New** (new pre-checked), select-all,
  and **"Add N selected →"**. On success, synced rows flip to ✓.
- A small "N in sheet but not in JobPilot" line (orphans) for awareness.
- When not connected, the modal shows the service-account setup steps + the
  config path, instead of the list.

## Data mapping

| JobPilot field | Sheet column (canonical) | Source |
|---|---|---|
| date_applied (or today) | Date Applied | `Application.date_applied` |
| company | Company | `Job.company` |
| title | Title | `Job.title` |
| location | Location | `Job.location` |
| pay | Pay | `salary_text`, else `$min–$max/hr` (hourly), else `$min–$max` |
| source | Source | `Job.source` |
| status label | Status | `STATUS_META[...].label`, title-cased |
| fit_score | Fit | `JobScore.fit_score` |
| url | URL | `Job.url` (USER_ENTERED → auto-links) |
| notes | Notes | `Application.notes` |

"Things I just applied for" = status in **applied, interview, response_received,
rejected, no_response** (the dashboard's existing "applied" filter group).

## Config

Add to `config/settings.yaml` (committed — only a non-secret ID/URL):

```yaml
google_sheets:
  enabled: true
  spreadsheet_id: "1ljhaUbcWmiPgejBcz43OdNy4vJCxtR5ZpE_manKd-80"
  worksheet: ""                                  # "" = first worksheet
  credentials_path: "config/google_credentials.json"
```

The secret service-account key lives at `config/google_credentials.json`
(already gitignored). Never committed.

## Matching / dedup rules

- Read the whole worksheet once (`get_all_values`). Row 0 = headers.
- Build a set of existing keys from sheet rows (using mapped URL + Company/Title).
- An applied app is **in_sheet** if its `row_key` collides with any sheet key.
- Append is idempotent: re-running sync never re-adds an already-present row.
- Sheet rows with no JobPilot match = **orphans** (count surfaced, not modified).

## Testing

- `tests/test_sheet_sync.py` — pure logic: normalize, header mapping (incl.
  synonyms + alien headers), `row_key` matching (url-equal, company/title-equal),
  `build_row` alignment, `compare` buckets, `default_header`.
- `tests/test_sheets_api.py` — FastAPI `TestClient` with gspread monkeypatched:
  health-when-unconfigured (`connected:false`), compare buckets, sync appends
  only selected + skips existing. No network.

## Out of scope (YAGNI)

- Editing/updating existing sheet rows (append-only v1).
- Pulling sheet edits back into the JobPilot DB.
- Multiple sheets / multi-tab routing.
- Auto-sync on status change (manual, button-triggered v1).

## Guardrails preserved

- Data stays local; only the user's own sheet is written, via his own service
  account. No third-party services.
- Secret key gitignored; spreadsheet ID (non-secret) in committed settings.
- Append-only — never deletes or overwrites the user's existing rows.
