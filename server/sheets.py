"""Google Sheet application-tracker sync API.

Reads the user's tracker sheet and appends applied jobs to it, never
duplicating. Uses a service account (gspread) — see ``config/settings.yaml``
``google_sheets.*`` and the gitignored credentials file.

Degrades gracefully: if credentials are missing or the sheet isn't shared, the
endpoints return ``connected:false`` with a human-readable reason rather than
raising 500s, so the dashboard can render a setup prompt instead of an error.

gspread / google-auth are imported lazily inside ``_open_worksheet`` so simply
importing this module (at dashboard startup) never fails on a missing key.
"""

import sys
from pathlib import Path

import yaml
from fastapi import APIRouter, Request
from sqlalchemy import desc

# Make project root importable (mirrors server/autofill.py).
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.database import get_session
from db.models import Job, JobScore, Application, ApplicationStatus
from agents.sheet_sync import (
    APPLIED_STATUSES,
    status_label,
    format_pay,
    map_headers,
    default_header,
    canonical_header_map,
    build_row,
    compare,
)

router = APIRouter(prefix="/api/sheet")

PROJECT_ROOT = Path(__file__).parent.parent
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"


class SheetNotConnected(Exception):
    """Raised when the sheet can't be reached; carries a user-facing reason."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _load_config():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg = {}
    return cfg.get("google_sheets", {}) or {}


def _spreadsheet_url(spreadsheet_id):
    if not spreadsheet_id:
        return ""
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def _open_worksheet():
    """Return a gspread worksheet, or raise SheetNotConnected with a reason."""
    cfg = _load_config()
    if not cfg.get("enabled", False):
        raise SheetNotConnected("Google Sheets sync is disabled in settings.yaml.")

    spreadsheet_id = cfg.get("spreadsheet_id")
    if not spreadsheet_id:
        raise SheetNotConnected("No spreadsheet_id configured in settings.yaml.")

    creds_rel = cfg.get("credentials_path", "config/google_credentials.json")
    creds_path = PROJECT_ROOT / creds_rel
    if not creds_path.exists():
        raise SheetNotConnected(
            f"Service-account key not found at {creds_rel}. Add the JSON key there "
            "and share the sheet with the service account's email as Editor."
        )

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except Exception as e:  # pragma: no cover - deps are in requirements.txt
        raise SheetNotConnected(f"gspread/google-auth not importable: {e}")

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    try:
        creds = Credentials.from_service_account_file(str(creds_path), scopes=scopes)
        client = gspread.authorize(creds)
        sh = client.open_by_key(spreadsheet_id)
        worksheet_name = cfg.get("worksheet") or ""
        return sh.worksheet(worksheet_name) if worksheet_name else sh.sheet1
    except SheetNotConnected:
        raise
    except Exception as e:
        msg = str(e)
        if "PERMISSION_DENIED" in msg or "403" in msg:
            raise SheetNotConnected(
                "Permission denied — share the sheet with the service account's "
                "email (the client_email in the JSON key) as Editor."
            )
        if "404" in msg or "not found" in msg.lower():
            raise SheetNotConnected("Spreadsheet not found — check spreadsheet_id in settings.yaml.")
        raise SheetNotConnected(f"Could not open the sheet: {msg}")


def _applied_applications():
    """Applied-group applications serialized to the fields the sheet needs."""
    session = get_session()
    try:
        applied_enums = []
        for s in APPLIED_STATUSES:
            try:
                applied_enums.append(ApplicationStatus(s))
            except ValueError:
                pass

        rows = (
            session.query(Application, Job, JobScore)
            .join(Job, Application.job_id == Job.id)
            .outerjoin(JobScore, JobScore.job_id == Job.id)
            .filter(Application.status.in_(applied_enums))
            .order_by(desc(Application.date_applied).nullslast(), desc(Job.date_found))
            .all()
        )

        out = []
        for app_obj, job, score in rows:
            status_key = app_obj.status.value if hasattr(app_obj.status, "value") else str(app_obj.status)
            date_applied = app_obj.date_applied.strftime("%Y-%m-%d") if app_obj.date_applied else ""
            out.append({
                "id": app_obj.id,
                "company": job.company or "",
                "title": job.title or "",
                "location": job.location or "",
                "pay": format_pay(
                    salary_text=job.salary_text,
                    hourly_min=getattr(job, "hourly_min", None),
                    hourly_max=getattr(job, "hourly_max", None),
                    salary_min=job.salary_min,
                    salary_max=job.salary_max,
                    pay_period=getattr(job, "pay_period", None),
                ),
                "source": job.source or "",
                "status": status_key,
                "status_label": status_label(status_key),
                "fit": score.fit_score if score else None,
                "url": job.url or "",
                "date_applied": date_applied,
                "notes": (app_obj.notes or "").replace("\n", " ").strip(),
            })
        return out
    finally:
        session.close()


def _disconnected_payload(reason, url, extra=None):
    payload = {"connected": False, "reason": reason, "spreadsheet_url": url}
    if extra:
        payload.update(extra)
    return payload


@router.get("/health")
async def sheet_health():
    """Is the sheet reachable? Drives the connection dot + setup prompt."""
    cfg = _load_config()
    url = _spreadsheet_url(cfg.get("spreadsheet_id"))
    enabled = bool(cfg.get("enabled", False))
    try:
        ws = _open_worksheet()
    except SheetNotConnected as e:
        return _disconnected_payload(e.reason, url, {"enabled": enabled})

    try:
        values = ws.get_all_values()
    except Exception as e:
        return _disconnected_payload(f"Could not read the sheet: {e}", url, {"enabled": enabled})

    header = values[0] if values else []
    return {
        "connected": True,
        "reason": "",
        "spreadsheet_url": url,
        "worksheet": getattr(ws, "title", ""),
        "row_count": max(0, len(values) - 1),
        "headers": header,
        "header_map": map_headers(header),
        "has_headers": bool(header),
        "enabled": True,
    }


@router.get("/compare")
async def sheet_compare():
    """Diff applied jobs against the sheet; return new / in-sheet buckets."""
    cfg = _load_config()
    url = _spreadsheet_url(cfg.get("spreadsheet_id"))
    apps = _applied_applications()
    empty_counts = {"new": 0, "in_sheet": 0, "orphans": 0, "applied_total": len(apps)}

    try:
        ws = _open_worksheet()
    except SheetNotConnected as e:
        return _disconnected_payload(
            e.reason, url, {"new": [], "in_sheet": [], "counts": empty_counts}
        )

    try:
        values = ws.get_all_values()
    except Exception as e:
        return _disconnected_payload(
            f"Could not read the sheet: {e}", url, {"new": [], "in_sheet": [], "counts": empty_counts}
        )

    header = values[0] if values else []
    data_rows = values[1:] if len(values) > 1 else []
    hmap = map_headers(header) if header else {}
    result = compare(apps, data_rows, hmap)

    return {
        "connected": True,
        "reason": "",
        "spreadsheet_url": url,
        "worksheet": getattr(ws, "title", ""),
        "new": result["new"],
        "in_sheet": result["in_sheet"],
        "counts": {
            "new": len(result["new"]),
            "in_sheet": len(result["in_sheet"]),
            "orphans": result["orphans"],
            "applied_total": len(apps),
        },
        "headers": header,
        "header_map": hmap,
        "has_headers": bool(header),
    }


@router.post("/sync")
async def sheet_sync(request: Request):
    """Append the selected applied jobs to the sheet (skips ones already there)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    app_ids = body.get("app_ids")

    cfg = _load_config()
    url = _spreadsheet_url(cfg.get("spreadsheet_id"))

    try:
        ws = _open_worksheet()
    except SheetNotConnected as e:
        return _disconnected_payload(e.reason, url, {"added": 0, "skipped": 0, "errors": [e.reason]})

    apps = _applied_applications()
    if app_ids is not None:
        wanted = set(app_ids)
        apps = [a for a in apps if a["id"] in wanted]

    try:
        values = ws.get_all_values()
    except Exception as e:
        return _disconnected_payload(
            f"Could not read the sheet: {e}", url, {"added": 0, "skipped": 0, "errors": [str(e)]}
        )

    header = values[0] if values else []
    data_rows = values[1:] if len(values) > 1 else []

    # Empty sheet → write our canonical header first, then append under it.
    wrote_header = False
    if not header:
        header = default_header()
        try:
            ws.append_row(header, value_input_option="USER_ENTERED")
            wrote_header = True
        except Exception as e:
            return {"connected": True, "reason": f"Could not write the header row: {e}",
                    "added": 0, "skipped": 0, "spreadsheet_url": url, "errors": [str(e)]}
        hmap = canonical_header_map()
        data_rows = []
    else:
        hmap = map_headers(header)

    if not hmap:
        return {
            "connected": True,
            "reason": ("Couldn't match any of your sheet's headers to known fields "
                       f"({', '.join(h for h in header if h)}). Rename a column to e.g. "
                       "Company / Role / Date Applied / Link, or tell me your layout."),
            "added": 0, "skipped": 0, "spreadsheet_url": url,
            "errors": ["no_header_match"], "headers": header,
        }

    diff = compare(apps, data_rows, hmap)
    to_add = diff["new"]
    skipped = len(diff["in_sheet"])

    ncols = len(header)
    rows = [build_row(a, hmap, ncols) for a in to_add]
    added = 0
    errors = []
    if rows:
        try:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            added = len(rows)
        except Exception as e:
            errors.append(str(e))

    return {
        "connected": True,
        "reason": "",
        "added": added,
        "skipped": skipped,
        "wrote_header": wrote_header,
        "spreadsheet_url": url,
        "worksheet": getattr(ws, "title", ""),
        "added_ids": [a["id"] for a in to_add] if added else [],
        "errors": errors,
    }
