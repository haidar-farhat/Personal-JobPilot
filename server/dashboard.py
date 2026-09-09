"""JobPilot Dashboard — FastAPI server with live SSE updates."""

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, desc

# Make parent importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.database import get_session, init_db, record_status_change
from db.models import (Job, JobScore, Application, ApplicationStatus, ScanLog,
                       AUTO_APPLY_PERMANENT_FAILURES)


BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
PROJECT_ROOT = BASE_DIR.parent

# Cache the archetype config (used for labels + auto-apply thresholds in the drawer)
_ARCHETYPES_CACHE: dict | None = None


def _archetypes() -> dict:
    global _ARCHETYPES_CACHE
    if _ARCHETYPES_CACHE is None:
        path = PROJECT_ROOT / "config" / "archetypes.yaml"
        try:
            with open(path, encoding="utf-8") as f:
                _ARCHETYPES_CACHE = yaml.safe_load(f)
        except FileNotFoundError:
            _ARCHETYPES_CACHE = {"archetypes": {}, "dimensions": []}
    return _ARCHETYPES_CACHE


def _archetype_label(key: str | None) -> str | None:
    if not key:
        return None
    arch = _archetypes().get("archetypes", {}).get(key)
    return arch.get("label") if arch else key


def _ats_summary(resume_path: str | None) -> dict | None:
    """Optimizer verdict the tailor saved for this résumé
    (output/optimizer/<base>_optimizer.json, same base name as the .docx)."""
    if not resume_path:
        return None
    try:
        base = Path(resume_path).stem.removesuffix("_resume")
        report = PROJECT_ROOT / "output" / "optimizer" / f"{base}_optimizer.json"
        if not report.exists():
            return None
        r = json.loads(report.read_text(encoding="utf-8"))
        return {"score": r.get("overall"), "keywords_missing": list(r.get("keywords_missing") or [])[:8]}
    except Exception:
        return None

app = FastAPI(title="JobPilot Dashboard", docs_url=None, redoc_url=None)


class _QuietLogs(logging.Filter):
    """Drop the two noise sources that buried real errors in dashboard.log
    (964 tracebacks by 2026-09-01): the Windows Proactor ConnectionResetError
    raised whenever an SSE client disconnects, and access lines for the two
    5-second pollers (SSE stats, watchdog health)."""

    def filter(self, record):
        exc = record.exc_info[1] if record.exc_info else None
        if isinstance(exc, ConnectionResetError):
            return False
        msg = record.getMessage()
        if record.name == "asyncio" and "_call_connection_lost" in msg:
            return False
        if record.name == "uvicorn.access" and ('"GET /api/stats ' in msg or '"GET /api/stream ' in msg):
            return False
        return True


for _name in ("asyncio", "uvicorn.access"):
    logging.getLogger(_name).addFilter(_QuietLogs())

# CORS — the server binds to 127.0.0.1 only; permissive origins let the
# browser-extension service worker call the autofill API.
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Browser-extension autofill API (profile / plan / health)
from server.autofill import router as autofill_router
app.include_router(autofill_router)

# Google Sheet application-tracker sync API (health / compare / sync)
from server.sheets import router as sheets_router
app.include_router(sheets_router)

# Agent-side job imports (MCP job-board connectors -> pipeline)
from server.job_import import router as import_router
app.include_router(import_router)

# Externally-made applications (extension button / dashboard quick-add)
from server.applied import router as applied_router
app.include_router(applied_router)

# Companies — tailored profiles + per-company pipeline trees
from server.companies import router as companies_router, fail_orphaned_drafts
app.include_router(companies_router)

# Advisor report — since-date event aggregation for Vantage Point meetings
from server.advisor import router as advisor_router
app.include_router(advisor_router)

# Extension features (company board scan / find-or-import for the tailor chain)
from server.extension_api import router as extension_router
app.include_router(extension_router)

# Gmail connector (read-only IMAP): ATS verification codes + "Sync from email" review queue
from server.gmail import router as gmail_router
app.include_router(gmail_router)

# Initialize DB
init_db()


@app.on_event("startup")
def _recover_orphaned_drafts():
    """Drafts run as in-process background tasks — any left 'drafting' when
    the server last stopped are orphans that will hang forever. Sweep them
    to 'failed' on boot so the UI offers retry instead of a stuck spinner."""
    fail_orphaned_drafts()


# ============================================================
# Helpers
# ============================================================

STATUS_META = {
    "found":              {"label": "FOUND",             "tone": "neutral",  "stage": 1},
    "scored":             {"label": "SCORED",            "tone": "neutral",  "stage": 2},
    "queued":             {"label": "QUEUED",            "tone": "info",     "stage": 3},
    "materials_ready":    {"label": "MATERIALS READY",   "tone": "info",     "stage": 4},
    "approved":           {"label": "APPROVED",          "tone": "accent",   "stage": 5},
    "applied":            {"label": "APPLIED",           "tone": "warning",  "stage": 6},
    "response_received":  {"label": "RESPONSE",          "tone": "info",     "stage": 7},
    "interview":          {"label": "INTERVIEW",         "tone": "success",  "stage": 8},
    "rejected":           {"label": "REJECTED",          "tone": "danger",   "stage": 9},
    "no_response":        {"label": "NO RESPONSE",       "tone": "muted",    "stage": 9},
    "no_longer_available": {"label": "NO LONGER AVAILABLE", "tone": "muted",  "stage": 9},
    "skipped":            {"label": "SKIPPED",           "tone": "muted",    "stage": 0},
}


def _serialize_application(app_obj, job, score):
    """Serialize an application record for the API."""
    # Calculate "days since applied" for follow-up reminders
    days_since_applied = None
    if app_obj.date_applied:
        delta = datetime.now(timezone.utc) - app_obj.date_applied.replace(tzinfo=timezone.utc)
        days_since_applied = delta.days

    # Age of the posting
    age_hours = None
    if job.date_found:
        found = job.date_found if job.date_found.tzinfo else job.date_found.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - found
        age_hours = int(delta.total_seconds() / 3600)

    resume_filename = Path(app_obj.resume_path).name if app_obj.resume_path else None
    cover_filename = Path(app_obj.cover_letter_path).name if app_obj.cover_letter_path else None

    status_key = app_obj.status.value if hasattr(app_obj.status, 'value') else str(app_obj.status)

    return {
        "id": app_obj.id,
        "job_id": job.id,
        "title": job.title,
        "company": job.company,
        "company_id": getattr(job, "company_id", None),
        "location": job.location or "",
        "salary_text": job.salary_text or "",
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "pay_period": getattr(job, "pay_period", None),
        "hourly_min": getattr(job, "hourly_min", None),
        "hourly_max": getattr(job, "hourly_max", None),
        "employment_type": getattr(job, "employment_type", None),
        "source": job.source,
        "url": job.url,
        "description": job.description or "",
        "date_found": job.date_found.isoformat() if job.date_found else None,
        "age_hours": age_hours,
        "is_remote": job.is_remote,
        "fit_score": score.fit_score if score else None,
        "key_matches": score.key_matches if score else [],
        "key_gaps": score.key_gaps if score else [],
        "ats_keywords": score.ats_keywords if score else [],
        "reasoning": score.reasoning if score else "",
        "recommended_action": score.recommended_action if score else None,
        # Multi-dimensional scoring (added when career-ops-style ranking landed)
        "archetype": getattr(score, "archetype", None) if score else None,
        "archetype_label": _archetype_label(getattr(score, "archetype", None)) if score else None,
        "archetype_confidence": getattr(score, "archetype_confidence", None) if score else None,
        "dimensions": getattr(score, "dimensions", None) if score else None,
        "dimension_weights": getattr(score, "dimension_weights", None) if score else None,
        # AI-forward signal (migration 004)
        "ai_intensity": getattr(score, "ai_intensity", None) if score else None,
        "ai_tools": getattr(score, "ai_tools", None) if score else [],
        "has_evaluation": bool(getattr(score, "evaluation_path", None)) if score else False,
        "status": status_key,
        "status_meta": STATUS_META.get(status_key, {"label": status_key.upper(), "tone": "neutral", "stage": 0}),
        "resume_path": app_obj.resume_path,
        "cover_letter_path": app_obj.cover_letter_path,
        "resume_filename": resume_filename,
        "cover_filename": cover_filename,
        "has_tailored_cv": bool(app_obj.resume_path),
        "has_resume_pdf": bool(app_obj.resume_path) and Path(app_obj.resume_path).with_suffix(".pdf").exists(),
        "ats": _ats_summary(app_obj.resume_path),
        "has_cover_letter": bool(app_obj.cover_letter_path),
        "date_applied": app_obj.date_applied.isoformat() if app_obj.date_applied else None,
        "days_since_applied": days_since_applied,
        "interview_date": app_obj.interview_date.isoformat() if app_obj.interview_date else None,
        "notes": app_obj.notes or "",
        # Auto-apply tracking
        "auto_applied": bool(getattr(app_obj, "auto_applied", False)),
        "auto_apply_status": getattr(app_obj, "auto_apply_status", None),
        "auto_apply_attempted_at": app_obj.auto_apply_attempted_at.isoformat()
            if getattr(app_obj, "auto_apply_attempted_at", None) else None,
        "next_action": getattr(app_obj, "next_action", None),
        # The bot hit a wall it can never clear (login, CAPTCHA…) and the role is
        # still waiting — a human has to submit it.
        "needs_manual_apply": getattr(app_obj, "auto_apply_status", None) in AUTO_APPLY_PERMANENT_FAILURES
            and status_key == "materials_ready",
    }


def _get_stats():
    """Get summary statistics."""
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        today = now.date()
        week_ago = now - timedelta(days=7)

        total_jobs = session.query(Job).count()
        jobs_today = session.query(Job).filter(func.date(Job.date_found) == today).count()
        jobs_this_week = session.query(Job).filter(Job.date_found >= week_ago).count()

        # Pipeline counts
        pipeline = {}
        for status in ApplicationStatus:
            count = session.query(Application).filter(Application.status == status).count()
            pipeline[status.value] = count

        avg_score = session.query(func.avg(JobScore.fit_score)).scalar() or 0
        high_matches = session.query(JobScore).filter(JobScore.fit_score >= 80).count()

        # Applications needing follow-up (applied 7+ days ago, no response)
        followup_cutoff = now - timedelta(days=7)
        needs_followup = (
            session.query(Application)
            .filter(Application.status == ApplicationStatus.APPLIED)
            .filter(Application.date_applied <= followup_cutoff)
            .count()
        )

        # Response rate
        applied_count = pipeline.get("applied", 0) + pipeline.get("interview", 0) + pipeline.get("rejected", 0) + pipeline.get("response_received", 0)
        response_count = pipeline.get("interview", 0) + pipeline.get("rejected", 0) + pipeline.get("response_received", 0)
        response_rate = round((response_count / applied_count * 100), 1) if applied_count > 0 else 0

        # Auto-apply counts (for the new bot-applied stat card)
        try:
            auto_applied_total = session.query(Application).filter(Application.auto_applied.is_(True)).count()
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            auto_applied_today = (
                session.query(Application)
                .filter(Application.auto_applied.is_(True))
                .filter(Application.auto_apply_attempted_at >= start_of_day)
                .count()
            )
        except Exception:
            auto_applied_total = 0
            auto_applied_today = 0

        # Pipeline liveness for the header strip (2026-09-01): when did the
        # scheduler last scan, and how many roles is the bot waiting on a human for?
        last_scan = session.query(func.max(ScanLog.timestamp)).scalar()
        last_scan_age_min = None
        if last_scan:
            if last_scan.tzinfo is None:
                last_scan = last_scan.replace(tzinfo=timezone.utc)
            last_scan_age_min = max(0, int((now - last_scan).total_seconds() // 60))
        needs_manual_apply = (
            session.query(Application)
            .filter(Application.auto_apply_status.in_(AUTO_APPLY_PERMANENT_FAILURES))
            .filter(Application.status == ApplicationStatus.MATERIALS_READY)
            .count()
        )

        return {
            "total_jobs": total_jobs,
            "jobs_today": jobs_today,
            "jobs_this_week": jobs_this_week,
            "pipeline": pipeline,
            "avg_score": round(avg_score, 1),
            "high_matches": high_matches,
            "needs_followup": needs_followup,
            "response_rate": response_rate,
            "pending_review": pipeline.get("materials_ready", 0) + pipeline.get("queued", 0) + pipeline.get("scored", 0),
            "auto_applied_total": auto_applied_total,
            "auto_applied_today": auto_applied_today,
            "last_scan_at": last_scan.isoformat() if last_scan else None,
            "last_scan_age_min": last_scan_age_min,
            "needs_manual_apply": needs_manual_apply,
            "last_update": now.isoformat(),
        }
    finally:
        session.close()


def _get_applications(status_filter=None, search=None, limit=2000):
    """Get applications with optional filtering.

    The limit was 500 until 2026-08-03. The UI filters facets (location, status,
    fit) client-side over whatever this returns, and the result set is ordered by
    fit_score — so once the corpus outgrew 500, any job below the cut was
    invisible to the location filter no matter what the user selected. Unscored
    jobs sort last (nullslast), so freshly-scanned out-of-metro roles were
    exactly the ones being hidden. 838 jobs serialise to ~4 MB in ~0.1s locally.
    """
    session = get_session()
    try:
        query = (
            session.query(Application, Job, JobScore)
            .join(Job, Application.job_id == Job.id)
            .outerjoin(JobScore, JobScore.job_id == Job.id)
        )

        if status_filter and status_filter != "all":
            if status_filter == "active":
                query = query.filter(Application.status.in_([
                    ApplicationStatus.MATERIALS_READY,
                    ApplicationStatus.QUEUED,
                    ApplicationStatus.APPROVED,
                    ApplicationStatus.APPLIED,
                    ApplicationStatus.SCORED,
                    ApplicationStatus.INTERVIEW,
                ]))
            elif status_filter == "applied":
                query = query.filter(Application.status.in_([
                    ApplicationStatus.APPLIED,
                    ApplicationStatus.INTERVIEW,
                    ApplicationStatus.RESPONSE_RECEIVED,
                    ApplicationStatus.REJECTED,
                    ApplicationStatus.NO_RESPONSE,
                ]))
            else:
                try:
                    status_enum = ApplicationStatus(status_filter)
                    query = query.filter(Application.status == status_enum)
                except ValueError:
                    pass

        if search:
            like = f"%{search}%"
            query = query.filter(
                (Job.title.ilike(like)) | (Job.company.ilike(like)) | (Job.location.ilike(like))
            )

        results = query.order_by(desc(JobScore.fit_score).nullslast(), desc(Job.date_found)).limit(limit).all()

        return [_serialize_application(a, j, s) for a, j, s in results]
    finally:
        session.close()


def _get_recent_activity(limit=15):
    """Get recent activity for the ticker."""
    session = get_session()
    try:
        recent_jobs = (
            session.query(Job)
            .order_by(desc(Job.date_found))
            .limit(limit)
            .all()
        )
        return [
            {
                "timestamp": j.date_found.isoformat() if j.date_found else "",
                "action": "FOUND",
                "title": j.title,
                "company": j.company,
                "source": j.source,
            }
            for j in recent_jobs
        ]
    finally:
        session.close()


def _get_scan_logs(limit=15):
    session = get_session()
    try:
        logs = session.query(ScanLog).order_by(desc(ScanLog.timestamp)).limit(limit).all()
        return [
            {
                "source": log.source,
                "timestamp": log.timestamp.isoformat() if log.timestamp else "",
                "jobs_found": log.jobs_found,
                "jobs_new": log.jobs_new,
                "errors": log.errors,
                "duration": log.duration_seconds,
            }
            for log in logs
        ]
    finally:
        session.close()


# ============================================================
# API Endpoints
# ============================================================
# Handlers that only do sync SQLite work are plain `def` on purpose: FastAPI
# runs those in its threadpool, so a slow query can't stall the event loop and
# freeze the SSE stream for every open tab (they were all `async def` until
# 2026-09-01). Handlers that await a threadpool call themselves stay async.

@app.get("/", response_class=HTMLResponse)
def index():
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/healthz")
def healthz():
    """Liveness probe — checks Ollama reachability + DB query + return ok."""
    from utils.ollama_client import check_ollama_health  # local import to avoid startup cost
    ollama_ok = False
    try:
        ollama_ok = check_ollama_health()
    except Exception:
        ollama_ok = False

    db_ok = False
    try:
        session = get_session()
        try:
            session.query(Job).limit(1).count()
            db_ok = True
        finally:
            session.close()
    except Exception:
        db_ok = False

    return {
        "ok": True,
        "ollama": "reachable" if ollama_ok else "unreachable",
        "db": "ok" if db_ok else "error",
    }


@app.get("/api/stats")
def api_stats():
    return _get_stats()


# ============================================================
# Agent preferences — Jack & Jill-style intake answers.
# The Home ranking blends these with save/skip feedback client-side.
# ============================================================
PREFS_PATH = PROJECT_ROOT / "config" / "agent_preferences.yaml"


def _clean_keyword_list(v):
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()][:30]


@app.get("/api/agent/preferences")
def get_agent_preferences():
    if PREFS_PATH.exists():
        try:
            return yaml.safe_load(PREFS_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
    return {}


@app.post("/api/agent/preferences")
async def set_agent_preferences(request: Request):
    body = await request.json()
    try:
        min_hourly = float(body.get("min_hourly")) if body.get("min_hourly") not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="min_hourly must be a number")
    prefs = {
        "love_keywords": _clean_keyword_list(body.get("love_keywords")),
        "avoid_keywords": _clean_keyword_list(body.get("avoid_keywords")),
        "locations": str(body.get("locations") or "").strip()[:200],
        "min_hourly": min_hourly,
        "notes": str(body.get("notes") or "").strip()[:2000],
    }
    # ponytail: whole-file overwrite, no merge — single-user tool
    PREFS_PATH.write_text(yaml.safe_dump(prefs, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"ok": True, **prefs}


@app.get("/api/applications")
def api_applications(status: str = None, search: str = None, limit: int = 2000):
    return _get_applications(status_filter=status, search=search, limit=limit)


@app.get("/api/application/{app_id}")
def api_application_detail(app_id: int):
    session = get_session()
    try:
        result = (
            session.query(Application, Job, JobScore)
            .join(Job, Application.job_id == Job.id)
            .outerjoin(JobScore, JobScore.job_id == Job.id)
            .filter(Application.id == app_id)
            .first()
        )
        if not result:
            raise HTTPException(status_code=404, detail="Application not found")
        app_obj, job, score = result
        return _serialize_application(app_obj, job, score)
    finally:
        session.close()


@app.post("/api/application/{app_id}/status")
async def api_update_status(app_id: int, request: Request):
    body = await request.json()
    new_status = body.get("status")
    notes = body.get("notes")

    try:
        status_enum = ApplicationStatus(new_status)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}")

    session = get_session()
    try:
        app_obj = session.query(Application).get(app_id)
        if not app_obj:
            raise HTTPException(status_code=404, detail="Application not found")

        record_status_change(session, app_obj, status_enum, source="dashboard")

        if notes is not None:
            app_obj.notes = notes

        session.commit()
        return {"success": True, "new_status": status_enum.value}
    finally:
        session.close()


@app.post("/api/application/{app_id}/email")
async def api_email_application(app_id: int, request: Request):
    """Email one application's resume + cover letter to ONE recipient you name.

    Human-in-the-loop on purpose: you supply the address (a recruiter who asked,
    or a careers inbox a posting actually names). Omit `to` for a self-copy.
    """
    body = await request.json() if await request.body() else {}
    to_addr = (body.get("to") or "").strip()
    note = (body.get("body") or "").strip() or None

    from server.autofill import load_profile
    from utils.mailer import send_application_email

    session = get_session()
    try:
        row = (session.query(Application, Job)
               .join(Job, Application.job_id == Job.id)
               .filter(Application.id == app_id).first())
        if not row:
            raise HTTPException(status_code=404, detail="Application not found")
        app_obj, job = row
        profile = load_profile()
        identity = profile.get("identity", {}) or {}
        res = send_application_email(
            to_addr=to_addr or identity.get("email", ""),
            job_title=job.title or "", company=job.company or "",
            resume_path=app_obj.resume_path, cover_letter_path=app_obj.cover_letter_path,
            sender_name=identity.get("full_name") or "", body=note,
            links=profile.get("links", {}), self_copy=not to_addr,
        )
        if not res.get("sent"):
            return JSONResponse(res, status_code=400)
        return res
    finally:
        session.close()


@app.get("/api/mail/health")
def api_mail_health():
    """Whether outbound email is configured (SMTP over the Gmail app password)."""
    try:
        from utils.mailer import mailer_ready
        with open(PROJECT_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
            cfg = (yaml.safe_load(f) or {}).get("mail", {}) or {}
        ready, why = mailer_ready()
        mode = ("forced:" + cfg["to"]) if cfg.get("to") else (
            "employer" if cfg.get("to_employer") else "off")
        if not cfg.get("to") and cfg.get("self_copy"):
            mode += "+self-copy"
        return {"ready": ready, "reason": why, "enabled": bool(cfg.get("enabled")),
                "to": cfg.get("to") or "", "to_employer": bool(cfg.get("to_employer")),
                "self_copy": bool(cfg.get("self_copy")), "mode": mode}
    except Exception as e:
        return {"ready": False, "reason": str(e)}


@app.get("/api/job-sources")
def api_job_sources():
    """Per-provider status for the Jobs screen source panel."""
    try:
        with open(PROJECT_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        from agents.scanner.providers import provider_health
        return {"providers": provider_health(config)}
    except Exception as e:
        logger.error(f"[job-sources] {e}")
        return {"providers": [], "error": str(e)}


@app.post("/api/job-sources/scan")
def api_job_sources_scan():
    """Run the aggregator providers once, now (the scheduler also does this)."""
    def _work():
        try:
            with open(PROJECT_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            from agents.scanner.aggregators import AggregatorScanner
            AggregatorScanner(config).run()
        except Exception:
            logger.exception("[job-sources] manual scan failed")

    threading.Thread(target=_work, name="jobpilot-source-scan", daemon=True).start()
    return {"started": True}


@app.post("/api/applications/bulk-status")
async def api_bulk_update_status(request: Request):
    """Move many applications to one status in a single round-trip.

    Backs the "Add all to agent queue" / "Add all to start" buttons — 100+
    single-row POSTs would be a request storm and could half-apply if the
    page navigates mid-flight.
    """
    body = await request.json()
    ids = body.get("ids") or []
    new_status = body.get("status")

    try:
        status_enum = ApplicationStatus(new_status)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="ids must be a non-empty list")

    session = get_session()
    try:
        rows = session.query(Application).filter(Application.id.in_(ids)).all()
        changed = 0
        for app_obj in rows:
            if app_obj.status == status_enum:
                continue  # already there — don't churn the status history
            record_status_change(session, app_obj, status_enum, source="dashboard_bulk")
            changed += 1
        session.commit()
        return {"success": True, "changed": changed, "requested": len(ids),
                "new_status": status_enum.value}
    finally:
        session.close()


@app.post("/api/application/{app_id}/notes")
async def api_update_notes(app_id: int, request: Request):
    body = await request.json()
    notes = body.get("notes", "")

    session = get_session()
    try:
        app_obj = session.query(Application).get(app_id)
        if not app_obj:
            raise HTTPException(status_code=404, detail="Application not found")
        app_obj.notes = notes
        session.commit()
        return {"success": True}
    finally:
        session.close()


@app.get("/api/scan-logs")
def api_scan_logs(limit: int = 15):
    return _get_scan_logs(limit)


@app.get("/api/activity")
def api_activity(limit: int = 15):
    return _get_recent_activity(limit)


@app.get("/api/file/{file_type}/{app_id}")
def api_file_download(file_type: str, app_id: int):
    """Download resume or cover letter .docx."""
    session = get_session()
    try:
        app_obj = session.query(Application).get(app_id)
        if not app_obj:
            raise HTTPException(status_code=404, detail="Application not found")

        if file_type == "resume" and app_obj.resume_path:
            path = Path(app_obj.resume_path)
        elif file_type == "resume_pdf" and app_obj.resume_path:
            path = Path(app_obj.resume_path).with_suffix(".pdf")   # Word-verified one-page export
        elif file_type == "cover" and app_obj.cover_letter_path:
            path = Path(app_obj.cover_letter_path)
        else:
            raise HTTPException(status_code=404, detail="File not found")

        if not path.exists():
            raise HTTPException(status_code=404, detail="File not found on disk")

        return FileResponse(
            path=str(path),
            filename=path.name,
            media_type=("application/pdf" if path.suffix.lower() == ".pdf" else
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        )
    finally:
        session.close()


@app.post("/api/application/{app_id}/open-link")
def api_open_link(app_id: int):
    """Open the application URL in the default browser."""
    session = get_session()
    try:
        app_obj = session.query(Application).get(app_id)
        if not app_obj:
            raise HTTPException(status_code=404, detail="Application not found")
        job = session.query(Job).get(app_obj.job_id)
        if job and job.url:
            webbrowser.open(job.url)
            return {"success": True, "url": job.url}
        raise HTTPException(status_code=404, detail="No URL available")
    finally:
        session.close()


@app.get("/api/application/{app_id}/evaluation")
def api_evaluation(app_id: int):
    """Return the 6-block markdown evaluation report for this application."""
    session = get_session()
    try:
        result = (
            session.query(Application, JobScore)
            .join(JobScore, JobScore.job_id == Application.job_id)
            .filter(Application.id == app_id)
            .first()
        )
        if not result:
            raise HTTPException(status_code=404, detail="No evaluation for this application")
        _, score = result
        eval_path = getattr(score, "evaluation_path", None)
        if not eval_path:
            raise HTTPException(status_code=404, detail="Evaluation not generated for this job")
        path = Path(eval_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Evaluation file missing on disk")
        return JSONResponse(
            content={
                "markdown": path.read_text(encoding="utf-8"),
                "path": str(path),
                "archetype": score.archetype,
                "archetype_label": _archetype_label(score.archetype),
                "fit_score": score.fit_score,
                "dimensions": score.dimensions,
                "dimension_weights": score.dimension_weights,
            }
        )
    finally:
        session.close()


@app.get("/api/application/{app_id}/interview")
async def api_interview_prep(app_id: int, refresh: bool = False):
    """Generate (or return cached) a role-specific interview-prep pack for this job.

    The LLM call runs in a threadpool so it never blocks the event loop / SSE.
    Degrades gracefully (ok:false) when Ollama is offline instead of 500-ing.
    """
    session = get_session()
    try:
        result = (
            session.query(Application, Job, JobScore)
            .join(Job, Application.job_id == Job.id)
            .outerjoin(JobScore, JobScore.job_id == Job.id)
            .filter(Application.id == app_id)
            .first()
        )
        if not result:
            raise HTTPException(status_code=404, detail="Application not found")
        app_obj, job, score = result
        job_id = job.id
        ctx = {
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "description": job.description or "",
            "archetype": getattr(score, "archetype", None) if score else None,
            "fit_score": getattr(score, "fit_score", None) if score else None,
            "key_matches": (getattr(score, "key_matches", None) or []) if score else [],
            "key_gaps": (getattr(score, "key_gaps", None) or []) if score else [],
        }
    finally:
        session.close()

    try:
        from agents.interview_prep import get_or_create_prep
        prep = await asyncio.to_thread(get_or_create_prep, job_id, ctx, refresh)
        prep["ok"] = True
        return JSONResponse(content=prep)
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)}, status_code=200)


@app.get("/api/application/{app_id}/outreach")
async def api_outreach(app_id: int, refresh: bool = False):
    """Generate (or return cached) networking/outreach DRAFTS for this job.

    Drafts a LinkedIn note + recruiter/hiring-manager emails + talking points that
    the user copies and sends themselves — it NEVER sends anything and does no
    people/contact lookup. LLM runs in a threadpool; degrades gracefully (ok:false)
    when Ollama is offline instead of 500-ing.
    """
    session = get_session()
    try:
        result = (
            session.query(Application, Job, JobScore)
            .join(Job, Application.job_id == Job.id)
            .outerjoin(JobScore, JobScore.job_id == Job.id)
            .filter(Application.id == app_id)
            .first()
        )
        if not result:
            raise HTTPException(status_code=404, detail="Application not found")
        app_obj, job, score = result
        job_id = job.id
        ctx = {
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "description": job.description or "",
            "archetype": getattr(score, "archetype", None) if score else None,
            "fit_score": getattr(score, "fit_score", None) if score else None,
            "key_matches": (getattr(score, "key_matches", None) or []) if score else [],
            "key_gaps": (getattr(score, "key_gaps", None) or []) if score else [],
        }
    finally:
        session.close()

    try:
        from agents.outreach import get_or_create_outreach
        pack = await asyncio.to_thread(get_or_create_outreach, job_id, ctx, refresh)
        pack["ok"] = True
        return JSONResponse(content=pack)
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)}, status_code=200)


@app.get("/api/application/{app_id}/followup")
async def api_followup(app_id: int, refresh: bool = False):
    """Generate (or return cached) a follow-up nudge DRAFT for a quiet application.

    Drafts a short status-check email + LinkedIn DM the user copies and sends
    themselves — it NEVER sends anything. LLM runs in a threadpool; degrades
    gracefully (ok:false) when Ollama is offline instead of 500-ing.
    """
    session = get_session()
    try:
        result = (
            session.query(Application, Job, JobScore)
            .join(Job, Application.job_id == Job.id)
            .outerjoin(JobScore, JobScore.job_id == Job.id)
            .filter(Application.id == app_id)
            .first()
        )
        if not result:
            raise HTTPException(status_code=404, detail="Application not found")
        app_obj, job, score = result
        job_id = job.id
        days_since_applied = None
        if app_obj.date_applied:
            delta = datetime.now(timezone.utc) - app_obj.date_applied.replace(tzinfo=timezone.utc)
            days_since_applied = delta.days
        ctx = {
            "title": job.title,
            "company": job.company,
            "description": job.description or "",
            "archetype": getattr(score, "archetype", None) if score else None,
            "fit_score": getattr(score, "fit_score", None) if score else None,
            "key_matches": (getattr(score, "key_matches", None) or []) if score else [],
            "status": app_obj.status.value if hasattr(app_obj.status, "value") else str(app_obj.status),
            "days_since_applied": days_since_applied,
        }
    finally:
        session.close()

    try:
        from agents.followup import get_or_create_followup
        pack = await asyncio.to_thread(get_or_create_followup, job_id, ctx, refresh)
        pack["ok"] = True
        return JSONResponse(content=pack)
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)}, status_code=200)


@app.get("/api/upskill")
async def api_upskill(refresh: bool = False):
    """Global skill-gap heatmap + learning plan aggregated from every scored job.

    The heatmap is deterministic (DB only); the learning-plan themes need Ollama
    and degrade to an `llm_error` note inside an ok:true response when it's down.
    """
    try:
        from agents.upskill import get_or_create_upskill
        report = await asyncio.to_thread(get_or_create_upskill, refresh)
        report["ok"] = True
        return JSONResponse(content=report)
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)}, status_code=200)


_TAILOR_LOCKS_GUARD = threading.Lock()
_TAILOR_LOCKS: dict = {}


def _tailor_lock(job_id):
    """One lock per job so concurrent tailor requests (two tabs, double-POST) don't
    double-run the LLM or race on the shared, deterministic .docx filename."""
    with _TAILOR_LOCKS_GUARD:
        lk = _TAILOR_LOCKS.get(job_id)
        if lk is None:
            lk = threading.Lock()
            _TAILOR_LOCKS[job_id] = lk
        return lk


@app.post("/api/application/{app_id}/tailor")
async def api_tailor_resume(app_id: int, cover_letter: int | None = None):
    """On-demand: generate a tailored one-page résumé (+ optional cover letter).

    ?cover_letter=0|1 overrides settings.yaml tailor.cover_letter_default.
    Runs the LLM in a threadpool; degrades gracefully (ok:false) on failure.
    Persists the file paths + bumps status to MATERIALS_READY from pre-materials states.
    """
    session = get_session()
    try:
        result = (
            session.query(Application, Job, JobScore)
            .join(Job, Application.job_id == Job.id)
            .outerjoin(JobScore, JobScore.job_id == Job.id)
            .filter(Application.id == app_id)
            .first()
        )
        if not result:
            raise HTTPException(status_code=404, detail="Application not found")
        app_obj, job, score = result
    finally:
        session.close()

    if score is None:
        return JSONResponse(
            content={"ok": False, "error": "Score this job first — tailoring needs the match analysis."},
            status_code=200,
        )

    try:
        from agents.tailor import tailor_for_job
        want_cover = None if cover_letter is None else bool(cover_letter)
        def _run_tailor():
            with _tailor_lock(job.id):   # serialize same-job tailoring (no dup LLM, no racing save)
                return tailor_for_job(job, score, cover_letter=want_cover)
        paths = await asyncio.to_thread(_run_tailor)
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": f"Tailoring failed: {e}"}, status_code=200)

    session = get_session()
    try:
        app_obj = session.query(Application).get(app_id)
        if not app_obj:
            raise HTTPException(status_code=404, detail="Application not found")
        app_obj.resume_path = paths["resume_docx"]
        if paths.get("cover_letter_docx"):        # a skipped letter keeps any earlier one
            app_obj.cover_letter_path = paths["cover_letter_docx"]
        if app_obj.status in (ApplicationStatus.FOUND, ApplicationStatus.SCORED, ApplicationStatus.QUEUED):
            record_status_change(session, app_obj, ApplicationStatus.MATERIALS_READY, source="dashboard")
        session.commit()
        new_status = app_obj.status.value
        cover_name = Path(app_obj.cover_letter_path).name if app_obj.cover_letter_path else None
    finally:
        session.close()

    return JSONResponse(content={
        "ok": True,
        "resume_filename": Path(paths["resume_docx"]).name,
        "pdf_filename": Path(paths["resume_pdf"]).name if paths.get("resume_pdf") else None,
        "pages": paths.get("pages"),
        "cover_filename": cover_name,
        "cover_generated": bool(paths.get("cover_letter_docx")),
        "ats_score": paths.get("optimizer_score"),
        "keywords_missing": paths.get("keywords_missing") or [],
        "status": new_status,
    })


@app.get("/api/archetypes")
def api_archetypes():
    """Return the archetype config so the drawer can render labels + auto-apply floors."""
    cfg = _archetypes()
    out = {}
    for key, arch in cfg.get("archetypes", {}).items():
        out[key] = {
            "label": arch.get("label", key),
            "description": arch.get("description", ""),
            "auto_apply_min_score": arch.get("auto_apply_min_score"),
        }
    return {"archetypes": out, "dimensions": cfg.get("dimensions", [])}


@app.post("/api/application/{app_id}/open-cv")
def api_open_cv(app_id: int):
    """Open the tailored CV in the default application (Word)."""
    session = get_session()
    try:
        app_obj = session.query(Application).get(app_id)
        if not app_obj or not app_obj.resume_path:
            raise HTTPException(status_code=404, detail="Resume not found")
        path = Path(app_obj.resume_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Resume file missing")
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])
        return {"success": True}
    finally:
        session.close()


# ============================================================
# JobRight-parity endpoints (2026-09-07): copilot Q&A, résumé list,
# profile page, agent run/status.
# ============================================================

_ASK_SYSTEM = ("You are JobPilot, a job-search copilot for one candidate. Answer the question about "
               "this specific job using the posting, the evaluation and the candidate's résumé summary. "
               "Be concrete and brief: markdown bullets, no preamble, no invented facts about the company.")


@app.post("/api/application/{app_id}/ask")
async def api_ask(app_id: int, request: Request):
    """Free-text copilot question about one job → local LLM (90 s cap)."""
    body = await request.json()
    question = str(body.get("question") or "").strip()[:2000]
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    session = get_session()
    try:
        result = (
            session.query(Application, Job, JobScore)
            .join(Job, Application.job_id == Job.id)
            .outerjoin(JobScore, JobScore.job_id == Job.id)
            .filter(Application.id == app_id)
            .first()
        )
        if not result:
            raise HTTPException(status_code=404, detail="Application not found")
        _, job, score = result
        title, company, desc = job.title, job.company, (job.description or "")[:6000]
        archetype = getattr(score, "archetype", None) if score else None
        eval_md = ""
        eval_path = getattr(score, "evaluation_path", None) if score else None
        if eval_path and Path(eval_path).exists():
            eval_md = Path(eval_path).read_text(encoding="utf-8")[:4000]
    finally:
        session.close()

    def _answer():
        from agents.ranker import _load_resume_summary
        from utils.ollama_client import generate_text
        try:
            resume = _load_resume_summary(archetype)[:3500]
        except Exception:
            resume = ""
        prompt = (f"JOB: {title} at {company}\n\nPOSTING:\n{desc}\n\n"
                  + (f"EVALUATION REPORT:\n{eval_md}\n\n" if eval_md else "")
                  + f"CANDIDATE RESUME SUMMARY:\n{resume}\n\nQUESTION: {question}\n\nANSWER (markdown):")
        return generate_text(prompt, _ASK_SYSTEM)

    try:
        answer = await asyncio.wait_for(asyncio.to_thread(_answer), timeout=90)
        return {"ok": True, "answer": (answer or "").strip()}
    except asyncio.TimeoutError:
        return JSONResponse(content={"ok": False, "error": "The local AI took longer than 90 s — try a shorter question."})
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)})


_BASE_RESUMES = [
    {"key": "ai", "file": "base_resume.yaml", "label": "AI / Data base résumé", "target": "AI, data & analytics roles"},
    {"key": "bt", "file": "base_resume_bt.yaml", "label": "Behavioral Tech base résumé", "target": "Behavioral Technician roles"},
]


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M")


@app.get("/api/resume/list")
def api_resume_list(limit: int = 20):
    """Resume page: uploaded PDF · base YAML résumés · newest tailored .docx (with their job)."""
    from server.autofill import resume_meta
    try:
        uploaded = resume_meta("ai")
    except Exception as e:
        uploaded = {"file": None, "error": str(e)}
    base = []
    for b in _BASE_RESUMES:
        p = PROJECT_ROOT / "config" / b["file"]
        if p.exists():
            st = p.stat()
            base.append({**b, "modified": _stamp(st.st_mtime), "created": _stamp(getattr(st, "st_birthtime", st.st_ctime))})
    out_dir = PROJECT_ROOT / "output" / "resumes"
    docs = sorted(out_dir.glob("*_resume.docx"), key=lambda p: p.stat().st_mtime, reverse=True) if out_dir.exists() else []
    newest = docs[:limit]
    by_path = {}
    session = get_session()
    try:
        names = [d.name for d in newest]
        rows = (session.query(Application, Job)
                .join(Job, Application.job_id == Job.id)
                .filter(Application.resume_path.isnot(None)).all())
        for app_obj, job in rows:
            if app_obj.resume_path and Path(app_obj.resume_path).name in names:
                by_path[Path(app_obj.resume_path).name] = (app_obj, job)
    finally:
        session.close()
    tailored = []
    for d in newest:
        st = d.stat()
        hit = by_path.get(d.name)
        ats = _ats_summary(str(d)) or {}
        tailored.append({
            "file": d.name, "modified": _stamp(st.st_mtime), "created": _stamp(getattr(st, "st_birthtime", st.st_ctime)),
            "has_pdf": d.with_suffix(".pdf").exists(), "ats_score": ats.get("score"),
            "app_id": hit[0].id if hit else None, "title": hit[1].title if hit else None, "company": hit[1].company if hit else None,
        })
    return {"uploaded": uploaded, "base": base, "tailored": tailored, "tailored_total": len(docs)}


@app.get("/api/resume/base/{key}")
def api_resume_base(key: str):
    """Render a base résumé YAML to .docx (cached next to the autofill's copy)."""
    b = next((x for x in _BASE_RESUMES if x["key"] == key), None)
    if not b:
        raise HTTPException(status_code=404, detail="Unknown base résumé")
    yaml_path = PROJECT_ROOT / "config" / b["file"]
    if not yaml_path.exists():
        raise HTTPException(status_code=404, detail="Base résumé YAML missing")
    from agents.tailor import _create_resume_docx, _load_config
    cache = PROJECT_ROOT / "output" / "resumes" / "_base"
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"base_resume_{key}.docx"
    if not out.exists() or out.stat().st_mtime < yaml_path.stat().st_mtime:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        _create_resume_docx(data, _load_config()).save(str(out))
    return FileResponse(str(out), filename=f"Matthew_Cromaz_{b['label'].split(' ')[0]}_base_resume.docx")


_PROFILE_KEYS = ("identity", "address", "links", "work_authorization", "experience", "education", "eeoc")
_RESUME_KEYS = ("education", "work_experience", "project_experience", "technical_skills", "certifications", "summary")


@app.get("/api/profile/full")
def api_profile_full():
    """Profile page. Never returns guardrails / essays / passwords / overrides."""
    prof = {}
    p = PROJECT_ROOT / "config" / "applicant_profile.yaml"
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        prof = {k: raw.get(k) for k in _PROFILE_KEYS if raw.get(k) is not None}
    r = PROJECT_ROOT / "config" / "base_resume.yaml"
    resume = {}
    if r.exists():
        raw = yaml.safe_load(r.read_text(encoding="utf-8")) or {}
        resume = {k: raw.get(k) for k in _RESUME_KEYS if raw.get(k) is not None}
    return {**prof, "resume": resume, "note": "Edit config/applicant_profile.yaml or config/base_resume.yaml to change"}


# ponytail: one module-level thread + dict; per-user tool, one run at a time is the point.
_AGENT = {"thread": None, "last_summary": None, "started_at": None,
          "autorun": {"active": False}}


def _agent_worker():
    try:
        from agents.auto_applier.runner import run_auto_apply
        _AGENT["last_summary"] = run_auto_apply()
    except Exception as e:  # runner is being rewritten by another agent — surface, never crash
        _AGENT["last_summary"] = {"error": str(e)}


def _autorun_worker():
    """One-button pipeline, run JOB BY JOB.

    For each job: queue it -> generate its resume + cover letter -> apply to
    it -> move on. The earlier version ran each phase across the whole list,
    so nothing was ever applied to until every resume existed (hours). This
    way the first application goes out within a minute or two.

    Cancel is checked between jobs, so Stop never interrupts a half-filled form.
    """
    from agents.auto_applier.runner import run_auto_apply
    from agents.tailor import tailor_for_job

    st = _AGENT["autorun"]
    totals = {"submitted": 0, "dry_run_count": 0, "failed_captcha": 0,
              "failed_other": 0, "total_attempted": 0}
    try:
        with open(PROJECT_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        # Work list: everything scored or already queued but lacking materials,
        # best fit first. Captured once so the list can't grow under us.
        session = get_session()
        try:
            rows = (session.query(Application.id)
                    .join(JobScore, JobScore.job_id == Application.job_id)
                    .filter(Application.status.in_([ApplicationStatus.SCORED,
                                                    ApplicationStatus.QUEUED]))
                    .filter(Application.resume_path.is_(None))
                    .order_by(JobScore.fit_score.desc())
                    .all())
            app_ids = [r[0] for r in rows]
        finally:
            session.close()

        st.update(phase="running", phase_done=0, phase_total=len(app_ids))

        for i, app_id in enumerate(app_ids, 1):
            if st.get("cancel"):
                st.update(phase="cancelled")
                return

            # ---- this job: fetch, queue, tailor --------------------------
            session = get_session()
            try:
                app_obj = session.query(Application).get(app_id)
                if app_obj is None:
                    continue
                job = session.query(Job).get(app_obj.job_id)
                score = session.query(JobScore).filter_by(job_id=app_obj.job_id).first()
                if not job or not score:
                    continue
                label = f"{job.title} @ {job.company}"
                st.update(phase_done=i, current=label, step="generating")

                if app_obj.status != ApplicationStatus.QUEUED:
                    record_status_change(session, app_obj, ApplicationStatus.QUEUED,
                                         source="autorun")
                    session.commit()

                paths = tailor_for_job(job, score)
                app_obj.resume_path = paths["resume_docx"]
                app_obj.cover_letter_path = paths["cover_letter_docx"]
                record_status_change(session, app_obj, ApplicationStatus.MATERIALS_READY,
                                     source="autorun")
                session.commit()
                st["tailored"] = st.get("tailored", 0) + 1
            except Exception as e:
                session.rollback()
                logger.error(f"[autorun] tailoring failed for app {app_id}: {e}")
                continue
            finally:
                session.close()

            # ---- this job: apply right away ------------------------------
            if st.get("cancel"):
                st.update(phase="cancelled")
                return
            st.update(step="applying")
            try:
                # only_app_id: apply to THIS job alone. Without it the pass
                # would sweep every eligible application, which is exactly the
                # batching this loop exists to avoid.
                res = run_auto_apply(config, only_app_id=app_id) or {}
                for k in totals:
                    totals[k] += res.get(k, 0) or 0
                _AGENT["last_summary"] = dict(totals)
            except Exception as e:
                logger.error(f"[autorun] apply pass failed after {label}: {e}")

        st.update(phase="done", current=None, step=None)
        _AGENT["last_summary"] = dict(totals)
    except Exception as e:
        logger.exception("[autorun] pipeline failed")
        st.update(phase="error", error=str(e))
        _AGENT["last_summary"] = {"error": str(e)}
    finally:
        st["active"] = False


@app.post("/api/agent/autorun")
def api_agent_autorun():
    """Start the full queue -> generate -> apply pipeline in one call."""
    t = _AGENT["thread"]
    if t and t.is_alive():
        return {"started": False, "reason": "already running"}
    _AGENT["autorun"] = {"active": True, "cancel": False, "phase": "starting",
                         "phase_done": 0, "phase_total": 0, "queued": 0, "tailored": 0}
    t = threading.Thread(target=_autorun_worker, name="jobpilot-autorun", daemon=True)
    _AGENT["thread"] = t
    _AGENT["started_at"] = datetime.now(timezone.utc).isoformat()
    t.start()
    return {"started": True}


@app.post("/api/agent/stop")
def api_agent_stop():
    """Ask a running auto-run to stop after the current batch."""
    st = _AGENT["autorun"]
    if not st.get("active"):
        return {"stopped": False, "reason": "not running"}
    st["cancel"] = True
    return {"stopped": True, "note": "finishing current batch, then halting"}


@app.get("/api/agent/status")
def api_agent_status():
    t = _AGENT["thread"]
    try:  # live per-job progress — also reflects the scheduler's own cycles
        from agents.auto_applier.runner import get_progress
        progress = get_progress()
    except Exception:
        progress = {"active": False}
    return {"running": bool(t and t.is_alive()) or bool(progress.get("active")),
            "progress": progress, "autorun": _AGENT["autorun"],
            "last_summary": _AGENT["last_summary"], "started_at": _AGENT["started_at"]}


@app.post("/api/agent/run")
def api_agent_run():
    t = _AGENT["thread"]
    if t and t.is_alive():
        return {"started": False, "reason": "already running"}
    t = threading.Thread(target=_agent_worker, name="jobpilot-agent", daemon=True)
    _AGENT["thread"] = t
    _AGENT["started_at"] = datetime.now(timezone.utc).isoformat()
    t.start()
    return {"started": True}


# ============================================================
# Server-Sent Events for live updates
# ============================================================

@app.get("/api/stream")
async def api_stream():
    """Server-Sent Events stream for live dashboard updates."""

    async def event_generator():
        last_total = -1
        last_applied_count = -1

        while True:
            try:
                # DB work off the event loop — this generator runs once per open
                # tab, forever; a sync query here stalled every other request.
                stats = await asyncio.to_thread(_get_stats)
                total = stats["total_jobs"]
                applied_count = stats["pipeline"].get("applied", 0)

                # Send stats every 5 seconds
                data = {
                    "type": "stats",
                    "payload": stats,
                }
                yield f"data: {json.dumps(data)}\n\n"

                # If job counts changed, also send updated applications
                if total != last_total or applied_count != last_applied_count:
                    applications = await asyncio.to_thread(_get_applications, limit=100)
                    data = {
                        "type": "applications",
                        "payload": applications[:100],
                    }
                    yield f"data: {json.dumps(data)}\n\n"

                    activity = await asyncio.to_thread(_get_recent_activity)
                    data = {
                        "type": "activity",
                        "payload": activity,
                    }
                    yield f"data: {json.dumps(data)}\n\n"

                last_total = total
                last_applied_count = applied_count

                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
                await asyncio.sleep(10)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ============================================================
# Entry point
# ============================================================

def main():
    import uvicorn
    # Ensure UTF-8 output on Windows
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    port = int(os.environ.get("JOBPILOT_PORT", 7777))
    host = os.environ.get("JOBPILOT_HOST", "127.0.0.1")
    print("")
    print("  +---- JobPilot Dashboard ------------------------+")
    print("  |                                                |")
    print(f"  |   Running at: http://{host}:{port}{' ' * (23 - len(host) - len(str(port)))}|")
    print("  |                                                |")
    print("  +------------------------------------------------+")
    print("")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
