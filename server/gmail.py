"""Gmail connector API — verification codes for ATS account creation and an
email-driven status review queue.

Mirrors server/sheets.py: never a 500; ``connected:false`` plus a
human-readable reason so the dashboard/extension can show a setup prompt.
"""

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Request

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents import email_reader as er  # noqa: E402

router = APIRouter(prefix="/api/gmail")


def _disconnected(reason, **extra):
    return {"connected": False, "reason": reason, **extra}


@router.get("/health")
def gmail_health():
    cfg = er.load_gmail_config()
    configured = bool(cfg["address"] and cfg["app_password"])
    try:
        m = er.connect()
    except er.GmailNotConnected as e:
        return _disconnected(e.reason, address=er.masked_address(cfg["address"]), configured=configured)
    er.close(m)
    return {"connected": True, "reason": "", "address": er.masked_address(cfg["address"]), "configured": True}


@router.get("/code")
def gmail_code(since: str = "", hint: str = "", wait: int = 45):
    """Poll (≤90 s) for a verification code/link received after `since` (ISO)."""
    try:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00")) if since else None
    except ValueError:
        since_dt = None
    since_dt = since_dt or datetime.now(timezone.utc) - timedelta(minutes=10)
    wait = max(0, min(int(wait), 90))
    try:
        m = er.connect()
    except er.GmailNotConnected as e:
        return _disconnected(e.reason, ok=False, found=False)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                hit = er.find_verification(since_dt, hint, conn=m)
            except Exception as e:
                return _disconnected(f"Gmail read failed: {e}", ok=False, found=False)
            if hit:
                return {"ok": True, "connected": True, "found": True, **hit}
            left = deadline - time.monotonic()
            if left <= 0:
                return {"ok": True, "connected": True, "found": False, "code": None, "link": None}
            time.sleep(min(4.0, left))
    finally:
        er.close(m)


@router.get("/scan")
def gmail_scan(days: int = 7):
    """Review queue of proposed status changes from recent job mail. Nothing is applied."""
    days = max(1, min(int(days), 30))
    try:
        proposals = er.scan_job_mail(days=days)
    except er.GmailNotConnected as e:
        return _disconnected(e.reason, proposals=[], days=days)
    except Exception as e:
        return _disconnected(f"Gmail scan failed: {e}", proposals=[], days=days)
    return {"connected": True, "reason": "", "proposals": proposals, "days": days}


@router.post("/apply")
async def gmail_apply(request: Request):
    """Commit the review rows the user ticked: [{uid, app_id, status, subject}]."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    items = body.get("items") or []

    from db.database import get_session, record_status_change
    from db.models import Application, ApplicationStatus

    s = get_session()
    applied, errors = [], []
    try:
        for it in items:
            try:
                app = s.get(Application, int(it["app_id"]))
                status = ApplicationStatus(it["status"])
            except Exception as e:
                errors.append(f"{it}: {e}")
                continue
            if not app:
                errors.append(f"application {it.get('app_id')} not found")
                continue
            note = f"gmail-uid:{it.get('uid', '')} · {(it.get('subject') or '')[:120]}"
            record_status_change(s, app, status, source="email", note=note)
            applied.append(app.id)
        s.commit()
    finally:
        s.close()
    return {"ok": True, "applied": applied, "errors": errors}
