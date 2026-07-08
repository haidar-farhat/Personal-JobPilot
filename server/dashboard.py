"""JobPilot Dashboard — FastAPI server with live SSE updates."""

import asyncio
import json
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

from db.database import get_session, init_db
from db.models import Job, JobScore, Application, ApplicationStatus, ScanLog


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

app = FastAPI(title="JobPilot Dashboard", docs_url=None, redoc_url=None)

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

# Initialize DB
init_db()


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
            "last_update": now.isoformat(),
        }
    finally:
        session.close()


def _get_applications(status_filter=None, search=None, limit=500):
    """Get applications with optional filtering."""
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

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/healthz")
async def healthz():
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
async def api_stats():
    return _get_stats()


@app.get("/api/applications")
async def api_applications(status: str = None, search: str = None, limit: int = 500):
    return _get_applications(status_filter=status, search=search, limit=limit)


@app.get("/api/application/{app_id}")
async def api_application_detail(app_id: int):
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

        app_obj.status = status_enum
        if status_enum == ApplicationStatus.APPLIED:
            app_obj.date_applied = datetime.now(timezone.utc)
        elif status_enum == ApplicationStatus.INTERVIEW:
            app_obj.interview_date = datetime.now(timezone.utc)

        if notes is not None:
            app_obj.notes = notes

        session.commit()
        return {"success": True, "new_status": status_enum.value}
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
async def api_scan_logs(limit: int = 15):
    return _get_scan_logs(limit)


@app.get("/api/activity")
async def api_activity(limit: int = 15):
    return _get_recent_activity(limit)


@app.get("/api/file/{file_type}/{app_id}")
async def api_file_download(file_type: str, app_id: int):
    """Download resume or cover letter .docx."""
    session = get_session()
    try:
        app_obj = session.query(Application).get(app_id)
        if not app_obj:
            raise HTTPException(status_code=404, detail="Application not found")

        if file_type == "resume" and app_obj.resume_path:
            path = Path(app_obj.resume_path)
        elif file_type == "cover" and app_obj.cover_letter_path:
            path = Path(app_obj.cover_letter_path)
        else:
            raise HTTPException(status_code=404, detail="File not found")

        if not path.exists():
            raise HTTPException(status_code=404, detail="File not found on disk")

        return FileResponse(
            path=str(path),
            filename=path.name,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    finally:
        session.close()


@app.post("/api/application/{app_id}/open-link")
async def api_open_link(app_id: int):
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
async def api_evaluation(app_id: int):
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
async def api_tailor_resume(app_id: int):
    """On-demand: generate a tailored one-page résumé + cover letter for this job.

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
        def _run_tailor():
            with _tailor_lock(job.id):   # serialize same-job tailoring (no dup LLM, no racing save)
                return tailor_for_job(job, score)
        paths = await asyncio.to_thread(_run_tailor)
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": f"Tailoring failed: {e}"}, status_code=200)

    session = get_session()
    try:
        app_obj = session.query(Application).get(app_id)
        if not app_obj:
            raise HTTPException(status_code=404, detail="Application not found")
        app_obj.resume_path = paths["resume_docx"]
        app_obj.cover_letter_path = paths["cover_letter_docx"]
        if app_obj.status in (ApplicationStatus.FOUND, ApplicationStatus.SCORED, ApplicationStatus.QUEUED):
            app_obj.status = ApplicationStatus.MATERIALS_READY
        session.commit()
        new_status = app_obj.status.value
    finally:
        session.close()

    return JSONResponse(content={
        "ok": True,
        "resume_filename": Path(paths["resume_docx"]).name,
        "cover_filename": Path(paths["cover_letter_docx"]).name,
        "status": new_status,
    })


@app.get("/api/archetypes")
async def api_archetypes():
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
async def api_open_cv(app_id: int):
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
                stats = _get_stats()
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
                    applications = _get_applications(limit=100)
                    data = {
                        "type": "applications",
                        "payload": applications[:100],
                    }
                    yield f"data: {json.dumps(data)}\n\n"

                    activity = _get_recent_activity()
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
