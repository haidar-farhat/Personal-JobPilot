"""Autofill API for the JobPilot browser extension.

Reuses the ranker's archetype config + résumé loader and the Ollama client.
The applicant profile is loaded directly here (a tiny YAML read) to keep the
web server decoupled from the Playwright-heavy ``auto_applier`` package.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from agents.autofill_mapper import build_plan, choose_archetype
from agents.ranker import _archetype_config, _load_resume_summary
from utils.ollama_client import check_ollama_health, generate_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/autofill", tags=["autofill"])

_PROFILE_PATH = Path(__file__).parent.parent / "config" / "applicant_profile.yaml"
_profile_cache: dict | None = None


def load_profile() -> dict:
    """Load + cache config/applicant_profile.yaml (the user's standard answers)."""
    global _profile_cache
    if _profile_cache is None:
        with open(_PROFILE_PATH, encoding="utf-8") as f:
            _profile_cache = yaml.safe_load(f) or {}
    return _profile_cache


class FieldSpec(BaseModel):
    id: str
    label: str | None = ""
    name: str | None = ""
    type: str | None = "text"
    options: list[str] | None = None
    required: bool | None = False
    section: str | None = ""   # nearest heading/legend — disambiguates e.g. education dates
    combo: bool | None = False  # scan marks React-Select/autocomplete inputs
    autocomplete: str | None = ""  # browser-standard semantic hint
    inputmode: str | None = ""
    automation_id: str | None = ""  # stable Workday/vendor semantic hook


class AutofillRequest(BaseModel):
    url: str | None = ""
    job_title: str | None = ""
    company: str | None = ""
    page_text: str | None = ""
    resume_pref: str | None = "auto"
    fields: list[FieldSpec]


@router.get("/profile")
def get_profile():
    """The applicant profile JSON, for the extension to cache + standard-fill.
    Enriched with structured employment entries so section engines and the
    offline mapper can fill work-history forms."""
    prof = dict(load_profile())
    try:
        prof["employment"] = _load_work_entries("auto")
    except Exception as e:
        logger.warning(f"[autofill] employment enrichment failed: {e}")
    return prof


# ---------------------------------------------------------------------------
# Structured history for wizard-style ATSes (Workday "My Experience" step):
# repeating work/education entries, a flat skills list, and links.
# ---------------------------------------------------------------------------

_MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}
_MONTHS.update({m[:3].lower(): i for m, i in [(k.capitalize(), v) for k, v in _MONTHS.items()]})
_MONTHS["sept"] = 9


def _month_year(s: str) -> tuple[int | None, int | None]:
    """'June 2026' -> (6, 2026). Tolerates abbreviations and stray punctuation."""
    m = re.search(r"([A-Za-z]+)\.?\s+(\d{4})", s or "")
    if not m:
        y = re.search(r"(\d{4})", s or "")
        return None, int(y.group(1)) if y else None
    return _MONTHS.get(m.group(1).lower()[:4].rstrip(".")) or _MONTHS.get(m.group(1).lower()[:3]), int(m.group(2))


def _parse_date_range(dates: str) -> dict:
    """'June 2026 - Present' / 'March 2025 - June 2025' -> start/end month-year."""
    parts = re.split(r"\s*[-–—]\s*", dates or "", maxsplit=1)
    sm, sy = _month_year(parts[0] if parts else "")
    current = len(parts) > 1 and bool(re.search(r"present|current|now", parts[1], re.I))
    em, ey = (None, None) if (current or len(parts) < 2) else _month_year(parts[1])
    return {"start_month": sm, "start_year": sy, "end_month": em, "end_year": ey, "current": current}


def _bullet_str(b) -> str:
    if isinstance(b, dict):
        for k in ("text", "bullet", "content", "value", "description"):
            if b.get(k):
                return str(b[k])
        return ""
    return str(b or "")


def _resume_yaml_path(pref: str | None) -> Path:
    base = Path(__file__).parent.parent / "config"
    fname = "base_resume_bt.yaml" if (pref or "").lower() == "bt" else "base_resume.yaml"
    path = base / fname
    return path if path.exists() else base / "base_resume.yaml"


def _split_degree(degree: str) -> tuple[str, str]:
    """'M.S., Quantitative Economics' -> ('M.S.', 'Quantitative Economics')."""
    parts = re.split(r",\s*| in ", degree or "", maxsplit=1)
    return (parts[0].strip(), parts[1].strip() if len(parts) > 1 else "")


_DEGREE_LABELS = {
    "m.s.": "Master of Science", "ms": "Master of Science", "m.sc.": "Master of Science",
    "b.a.": "Bachelor of Arts", "ba": "Bachelor of Arts",
    "b.s.": "Bachelor of Science", "bs": "Bachelor of Science",
    "m.a.": "Master of Arts", "ma": "Master of Arts",
    "mba": "Master of Business Administration", "ph.d.": "Doctorate", "phd": "Doctorate",
}


def _work_entries_from(resume: dict) -> list[dict]:
    """Structured work-history entries from a résumé dict (base YAML or a
    tailored sidecar — both share the title/organization/dates/bullets shape)."""
    work = []
    for w in resume.get("work_experience", []) or []:
        d = _parse_date_range(w.get("dates", ""))
        work.append({
            "title": w.get("title", ""),
            "company": w.get("organization") or w.get("company", ""),
            "location": w.get("location", ""),
            "description": "\n".join(filter(None, (_bullet_str(b) for b in w.get("bullets", []) or []))),
            **d,
        })
    return work


def _load_work_entries(resume_pref: str = "auto") -> list[dict]:
    """Structured work-history entries from the routed base résumé YAML."""
    with open(_resume_yaml_path(resume_pref), encoding="utf-8") as f:
        return _work_entries_from(yaml.safe_load(f) or {})


def _load_history_resume(resume_pref: str, company: str, job_title: str) -> dict:
    """The résumé dict the history entries must mirror: the tailored sidecar
    JSON for this company/role when one exists (so wizard panels match the
    attached tailored .docx), else the routed base YAML."""
    if company:
        p = _tailored_resume_path(company, job_title)
        side = p.with_suffix(".json") if p else None
        if side and side.exists():
            try:
                return json.loads(side.read_text(encoding="utf-8")) or {}
            except Exception as e:
                logger.warning(f"[autofill] tailored sidecar unreadable ({side.name}): {e}")
    with open(_resume_yaml_path(resume_pref), encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@router.get("/history")
def get_history(resume_pref: str = "auto", company: str = "", job_title: str = ""):
    """Work/education/skills as structured entries for multi-entry ATS wizards."""
    profile = load_profile()
    resume = _load_history_resume(resume_pref, company, job_title)

    work = _work_entries_from(resume)

    education = []
    for e in resume.get("education", []) or []:
        short, field = _split_degree(e.get("degree", ""))
        _, year = _month_year(e.get("date", ""))
        education.append({
            "school": e.get("institution", ""),
            "degree": short,
            "degree_label": _DEGREE_LABELS.get(short.lower().replace(" ", ""), short),
            "field_of_study": field,
            "location": e.get("location", ""),
            "end_year": year,
            "gpa": e.get("gpa", ""),
        })

    skills: list[str] = []
    raw_skills = resume.get("technical_skills") or resume.get("skills") or {}
    if isinstance(raw_skills, dict):
        for v in raw_skills.values():
            skills.extend(s.strip() for s in str(v).split(",") if s.strip())
    elif isinstance(raw_skills, list):
        skills.extend(str(s).strip() for s in raw_skills)
    seen: set[str] = set()
    skills = [s for s in skills if not (s.lower() in seen or seen.add(s.lower()))][:25]

    langs = resume.get("languages") or []
    return {
        "work": work,
        "education": education,
        "skills": skills,
        "languages": langs,
        "links": profile.get("links", {}),
        "identity": profile.get("identity", {}),
        "eeoc": profile.get("eeoc", {}),
        "work_authorization": profile.get("work_authorization", {}),
        "address": profile.get("address", {}),
        "availability": profile.get("availability", {}),
    }


_BASE_DOCX_CACHE = Path(__file__).parent.parent / "output" / "autofill"


def _tailored_resume_path(company: str, job_title: str = "") -> Path | None:
    """Tailored .docx for the best-matching application at this company, or
    None. Without a company to match on there's nothing safe to return — the
    newest tailored file could be for a totally unrelated role."""
    if not company:
        return None
    try:
        from db.database import get_session
        from db.models import Application, Job

        s = get_session()
        try:
            q = (s.query(Application, Job).join(Job, Application.job_id == Job.id)
                 .filter(Application.resume_path.isnot(None))
                 .filter(Job.company.ilike(f"%{company.strip()}%")))
            rows = q.order_by(Application.id.desc()).all()
            GENERIC = {"senior", "junior", "staff", "lead", "principal", "sr", "jr",
                       "i", "ii", "iii", "iv", "the", "of", "and", "a", "an"}
            jt = set(re.findall(r"[a-z]+", (job_title or "").lower())) - GENERIC
            best, best_score = None, 0
            for app, job in rows:
                p = Path(app.resume_path)
                if not p.is_absolute():
                    p = Path(__file__).parent.parent / p
                if not p.exists():
                    continue
                if not jt:  # no title to match — newest tailored file for this company
                    best = p
                    break
                tt = set(re.findall(r"[a-z]+", (job.title or "").lower())) - GENERIC
                score = len(jt & tt)
                if score > best_score:  # rows are newest-first, so ties keep the newest
                    best, best_score = p, score
            # a same-company résumé tailored to an unrelated role is worse than
            # the base résumé — require at least one meaningful title word
            if best is not None and (not jt or best_score >= 1):
                return best
        finally:
            s.close()
    except Exception as e:
        logger.warning(f"[autofill] tailored-resume lookup failed: {e}")
    return None


def _attach_format() -> str:
    try:
        from agents.tailor import _load_config
        return str((_load_config().get("tailor") or {}).get("attach_format", "pdf")).lower()
    except Exception:
        return "pdf"


def _resume_path_for_app(app_id) -> Path | None:
    """The exact tailored résumé for ONE application, by id.

    When the dashboard arms a job it knows precisely which application this is,
    so there is no need to fuzzy-match a company name the extension guessed off
    the page — that guess is empty on custom career sites and silently fell
    back to the generic base résumé, attaching an untailored CV to a job the
    tailor had already optimised for.
    """
    try:
        app_id = int(app_id)
    except (TypeError, ValueError):
        return None
    try:
        from db.database import get_session
        from db.models import Application
        s = get_session()
        try:
            app = s.query(Application).get(app_id)
            if not app or not app.resume_path:
                return None
            p = Path(app.resume_path)
            if not p.is_absolute():
                p = Path(__file__).parent.parent / p
            return p if p.exists() else None
        finally:
            s.close()
    except Exception as e:
        logger.warning(f"[autofill] resume lookup for app {app_id} failed: {e}")
        return None


@router.get("/resume_file")
def resume_file(resume_pref: str = "auto", company: str = "", job_title: str = "",
                app_id: str = ""):
    """Best résumé file for the extension to attach: the tailored .docx for a
    matching application if one exists, otherwise a rendered base résumé.

    app_id wins when supplied — it names the application exactly, so an armed
    job always gets ITS optimised CV rather than a name-matched guess.
    """
    tailored = _resume_path_for_app(app_id) or _tailored_resume_path(company, job_title)
    if tailored is not None:
        # The Word-verified one-page PDF when the tailor produced one — ATS
        # parsers and recruiters both prefer it (settings tailor.attach_format).
        pdf = tailored.with_suffix(".pdf")
        if pdf.exists() and _attach_format() != "docx":
            return FileResponse(pdf, filename=pdf.name)
        return FileResponse(tailored, filename=tailored.name)

    # 2. user-provided résumé file (the polished PDF), if configured in the profile.
    #    Attach under its ORIGINAL filename (what the widget pill shows) — the
    #    file on the form must be recognizably the one the user uploaded.
    try:
        prof = load_profile()
        rf = prof.get("resume_files") or {}
        key = "bt" if (resume_pref or "").lower() == "bt" else "ai"
        rel = rf.get(key) or rf.get("ai")
        if rel:
            p = Path(rel)
            if not p.is_absolute():
                p = Path(__file__).parent.parent / p
            if p.exists():
                return FileResponse(p, filename=_resume_serve_name(p))
    except Exception as e:
        logger.warning(f"[autofill] configured resume_files lookup failed: {e}")

    # 3. rendered base résumé (cached; re-rendered when the YAML changes)
    try:
        from agents.tailor import _create_resume_docx, _load_config

        yaml_path = _resume_yaml_path(resume_pref)
        _BASE_DOCX_CACHE.mkdir(parents=True, exist_ok=True)
        out = _BASE_DOCX_CACHE / f"base_resume_{'bt' if 'bt' in yaml_path.stem else 'ai'}.docx"
        if not out.exists() or out.stat().st_mtime < yaml_path.stat().st_mtime:
            with open(yaml_path, encoding="utf-8") as f:
                resume_data = yaml.safe_load(f) or {}
            doc = _create_resume_docx(resume_data, _load_config())
            doc.save(str(out))
        identity = load_profile().get("identity", {})
        nice = (identity.get("full_name") or "resume").replace(" ", "_") + "_Resume.docx"
        return FileResponse(out, filename=nice)
    except Exception as e:
        logger.error(f"[autofill] base résumé render failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=404)


def _resume_target(resume_pref: str) -> Path | None:
    """The on-disk file the profile serves for this pref, or None if unset."""
    rf = load_profile().get("resume_files") or {}
    key = "bt" if (resume_pref or "").lower() == "bt" else "ai"
    rel = rf.get(key)
    if not rel:
        return None
    p = Path(rel)
    return p if p.is_absolute() else Path(__file__).parent.parent / p


def _resume_serve_name(p: Path) -> str:
    """Filename the ATS sees: the uploaded file's original name (what the
    widget pill shows), falling back to the on-disk name."""
    side = p.with_suffix(p.suffix + ".meta.json")
    if side.exists():
        try:
            name = (json.loads(side.read_text(encoding="utf-8")).get("original_name") or "").strip()
            if name:
                return name
        except Exception:
            pass
    return p.name


@router.get("/resume_meta")
def resume_meta(resume_pref: str = "auto"):
    """What the autofill would attach for this pref (before any per-company
    tailored override): file, display name, and upload provenance for the
    widget's résumé pill."""
    p = _resume_target(resume_pref)
    identity = load_profile().get("identity", {})
    nice = (identity.get("full_name") or "resume").replace(" ", "_") + "_Resume"
    if p and p.exists():
        meta = {}
        side = p.with_suffix(p.suffix + ".meta.json")
        if side.exists():
            try:
                meta = json.loads(side.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        return {"source": "profile_pdf", "file": p.name, "serve_name": _resume_serve_name(p),
                "original_name": meta.get("original_name") or p.name,
                "uploaded_at": meta.get("uploaded_at") or datetime.fromtimestamp(
                    p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "size": p.stat().st_size,
                "note": "a company-matched tailored résumé still overrides this"}
    return {"source": "rendered_base", "file": None, "serve_name": nice + ".docx",
            "original_name": None, "uploaded_at": None, "size": None,
            "note": "no PDF configured — the base résumé YAML is rendered to .docx"}


class ResumeUpload(BaseModel):
    resume_pref: str = "ai"
    filename: str = ""
    b64: str


@router.post("/resume_upload")
def resume_upload(req: ResumeUpload):
    """Replace the configured résumé PDF from the widget ('swap it out as I
    iterate my résumé'). Overwrites the profile-configured path only — never an
    arbitrary location — and records provenance in a .meta.json sidecar."""
    p = _resume_target(req.resume_pref)
    if p is None:
        return JSONResponse({"error": "no resume_files path configured for this pref in "
                                      "applicant_profile.yaml — set it first"}, status_code=400)
    try:
        data = base64.b64decode(req.b64)
    except Exception:
        return JSONResponse({"error": "invalid base64 payload"}, status_code=400)
    if not data.startswith(b"%PDF"):
        return JSONResponse({"error": "only PDF files are supported here"}, status_code=400)
    if len(data) > 15 * 1024 * 1024:
        return JSONResponse({"error": "file too large (>15MB)"}, status_code=400)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(p)
    side = p.with_suffix(p.suffix + ".meta.json")
    side.write_text(json.dumps({
        "original_name": (req.filename or "").strip()[:120] or p.name,
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "size": len(data)}), encoding="utf-8")
    logger.info(f"[autofill] résumé replaced via widget: {p.name} <- "
                f"{req.filename!r} ({len(data)} bytes)")
    return {"ok": True, **resume_meta(req.resume_pref)}


def _cover_path_for_app(app_id) -> Path | None:
    """The exact tailored cover letter for ONE application, by id."""
    try:
        app_id = int(app_id)
    except (TypeError, ValueError):
        return None
    try:
        from db.database import get_session
        from db.models import Application
        s = get_session()
        try:
            app = s.query(Application).get(app_id)
            if not app or not app.cover_letter_path:
                return None
            p = Path(app.cover_letter_path)
            if not p.is_absolute():
                p = Path(__file__).parent.parent / p
            return p if p.exists() else None
        finally:
            s.close()
    except Exception as e:
        logger.warning(f"[autofill] cover lookup for app {app_id} failed: {e}")
        return None


@router.get("/cover_letter_file")
def cover_letter_file(company: str = "", job_title: str = "", app_id: str = ""):
    """Tailored cover letter for a matching application, if one exists.
    No generic fallback — a wrong-company letter is worse than none.

    app_id names the application exactly (used when the dashboard armed it).
    """
    exact = _cover_path_for_app(app_id)
    if exact is not None:
        return FileResponse(exact, filename=exact.name)
    if not company:
        return JSONResponse({"error": "no company context"}, status_code=404)
    try:
        from db.database import get_session
        from db.models import Application, Job

        s = get_session()
        try:
            q = (s.query(Application, Job).join(Job, Application.job_id == Job.id)
                 .filter(Application.cover_letter_path.isnot(None))
                 .filter(Job.company.ilike(f"%{company.strip()}%")))
            rows = q.order_by(Application.id.desc()).all()
            GENERIC = {"senior", "junior", "staff", "lead", "principal", "sr", "jr",
                       "i", "ii", "iii", "iv", "the", "of", "and", "a", "an"}
            jt = set(re.findall(r"[a-z]+", (job_title or "").lower())) - GENERIC
            best, best_score = None, 0
            for app, job in rows:
                p = Path(app.cover_letter_path)
                if not p.is_absolute():
                    p = Path(__file__).parent.parent / p
                if not p.exists():
                    continue
                if not jt:
                    best = p
                    break
                tt = set(re.findall(r"[a-z]+", (job.title or "").lower())) - GENERIC
                score = len(jt & tt)
                if score > best_score:
                    best, best_score = p, score
            if best is not None and (not jt or best_score >= 1):
                return FileResponse(best, filename=best.name)
        finally:
            s.close()
    except Exception as e:
        logger.warning(f"[autofill] cover-letter lookup failed: {e}")
    return JSONResponse({"error": "no tailored cover letter for this company"}, status_code=404)


# ---------------------------------------------------------------------------
# "APPLY WITH AUTOFILL" arming. The dashboard arms the job's host right before
# it opens the link; the extension asks whether the page it landed on is armed
# and, if so, runs the fill by itself (once). Filling is still not submitting.
# ponytail: module dict + 15-min TTL — one process, a hint, not a record.
# ---------------------------------------------------------------------------
_ARMED: dict[str, dict] = {}
_ARM_TTL = 15 * 60


def _arm_host(host_or_url: str) -> str:
    """'https://www.Boards.Greenhouse.io/x' or 'www.boards.greenhouse.io' -> 'boards.greenhouse.io'."""
    s = (host_or_url or "").strip()
    h = (urlparse(s).hostname if "://" in s else s.split(":")[0]).lower() if s else ""
    return h[4:] if h.startswith("www.") else h


class ArmRequest(BaseModel):
    app_id: int | None = None
    url: str | None = ""
    company: str | None = ""
    title: str | None = ""


@router.post("/arm")
def arm(req: ArmRequest):
    url, company, title = req.url or "", req.company or "", req.title or ""
    if req.app_id is not None:
        from db.database import get_session
        from db.models import Application, Job

        s = get_session()
        try:
            row = (s.query(Job).join(Application, Application.job_id == Job.id)
                   .filter(Application.id == req.app_id).first())
            if row is None:
                return JSONResponse({"error": f"application {req.app_id} not found"}, status_code=404)
            url, company, title = row.url or url, row.company or company, row.title or title
        finally:
            s.close()
    host = _arm_host(url)
    if not host:
        return JSONResponse({"error": "no url to arm"}, status_code=400)
    rec = {"app_id": req.app_id, "url": url, "host": host, "company": company,
           "title": title, "ts": time.time()}
    _ARMED[host] = rec
    return rec


def arm_for_app(app_id: int) -> dict:
    """Arm autofill for one application — the in-process form of POST /arm,
    so a server-side caller need not round-trip through HTTP."""
    return arm(ArmRequest(app_id=app_id))


@router.get("/armed")
def armed(host: str = "", url: str = ""):
    """The un-expired arm for this host (or whose url is a prefix of the page url), else {}."""
    now = time.time()
    for h in [h for h, r in _ARMED.items() if now - r["ts"] > _ARM_TTL]:
        _ARMED.pop(h, None)
    rec = _ARMED.get(_arm_host(host or url))
    if rec is None and url:
        rec = next((r for r in _ARMED.values()
                    if r["url"] and url.startswith(r["url"].split("?")[0].rstrip("/"))), None)
    return rec or {}


@router.delete("/armed")
def disarm(host: str = ""):
    return {"ok": True, "removed": _ARMED.pop(_arm_host(host), None) is not None}


@router.get("/health")
def health():
    try:
        profile_loaded = bool(load_profile())
    except Exception:
        profile_loaded = False
    from utils.ollama_client import llm_status
    st = llm_status()
    return {"ok": True, "ollama_up": check_ollama_health(),
            "llm_provider": st["provider"], "llm_model": st["model"],
            "fallback_llm": st["openai_ready"], "profile_loaded": profile_loaded}


def _draft_answer(field: dict, profile: dict, resume_summary: str, job_title: str,
                  company: str, page_text: str = "") -> str | None:
    """Agentic draft for an open-ended/ambiguous field: grounded in the résumé,
    the page's job description, and the profile's essay_facts. Falls back to
    the fallback_essays templates when the LLM is unavailable."""
    label = (field.get("label") or field.get("name") or "this question").strip()
    nl = label.lower()
    fbs = profile.get("fallback_essays", {})
    fb = None
    if "strength" in nl:
        fb = fbs.get("greatest_strength")
    elif "weak" in nl:
        fb = fbs.get("weakness")
    elif "accomplish" in nl or "achievement" in nl or "proud" in nl:
        fb = (profile.get("essay_facts") or {}).get("biggest_accomplishment")
    elif "role" in nl or "position" in nl:
        fb = fbs.get("why_role")
    elif "company" in nl or "why" in nl:
        fb = fbs.get("why_company")
    if fb and "{company}" in fb:
        fb = fb.replace("{company}", company or "your team")

    facts = profile.get("essay_facts") or {}
    facts_block = "\n".join(f"- {k.replace('_', ' ')}: {v}" for k, v in facts.items() if v)
    jd = re.sub(r"\s+", " ", (page_text or "")).strip()[:1800]

    opts = field.get("options")
    prompt = (
        "You ARE the applicant, writing your own answer on your own job "
        "application. Write in the FIRST PERSON: 'I', 'my', 'me'.\n"
        "NEVER write about yourself in the third person and NEVER use your "
        "own name — write 'I am 24' and never 'Haidar is 24'.\n"
        "NEVER explain where the answer came from or how it was worked out — "
        "write 'I graduated in 2025', not 'based on the graduation date on "
        "the resume'. No preamble, no quotes, no meta-commentary: return only "
        "the answer text, exactly as it should appear in the form field.\n"
        "Be concise: 60-120 words, or a single short line when the question "
        "wants a short factual answer. Ground every claim ONLY in the RESUME "
        "and KNOWN FACTS below — never invent employers, dates, or numbers. "
        "When the JOB DESCRIPTION is relevant (e.g. 'why do you want to work "
        "here'), connect your actual experience to what the role needs.\n\n"
        f"QUESTION: {label}\n"
        + (f"PICK ONE OF THESE OPTIONS, returning its exact text: {opts}\n" if opts else "")
        + f"\nJOB: {job_title or ''} at {company or ''}\n"
        + (f"\nJOB DESCRIPTION (excerpt):\n{jd}\n" if jd else "")
        + (f"\nKNOWN FACTS:\n{facts_block}\n" if facts_block else "")
        + f"\nRESUME:\n{resume_summary[:2000]}"
    )
    try:
        answer = generate_text(prompt) or fb
        full_name = ((profile.get("identity") or {}).get("full_name") or "").strip()
        return _to_first_person(answer, full_name)
    except Exception as e:  # Ollama down / timeout → fallback template (or None)
        logger.warning(f"[autofill] essay draft failed for {label!r}: {e}")
        return fb


# The model drifts into describing the applicant ("Haidar is 24 years old
# based on his graduation date") even when told to answer in first person.
# Repairing the common shapes is cheaper and far more reliable than
# re-prompting, and it runs on the fallback templates too.
_META_PREFIX = re.compile(
    r"^\s*(based on[^,.]{0,60}[,.]\s*|according to[^,.]{0,60}[,.]\s*|"
    r"as (?:shown|stated|indicated|per)[^,.]{0,60}[,.]\s*)", re.I)


def _to_first_person(text: str, full_name: str = "") -> str:
    """Rewrite third-person self-reference into first person.

    The answer goes into a form field under the applicant's own name, so
    "Haidar is 24" reads as though somebody else filled it in.
    """
    if not text:
        return text
    out = text.strip().strip('"').strip()
    out = _META_PREFIX.sub("", out)

    names = [n for n in ([full_name] + full_name.split()) if len(n) > 2]
    for n in sorted(set(names), key=len, reverse=True):
        esc = re.escape(n)
        out = re.sub(rf"\b{esc}'s\b", "my", out)
        out = re.sub(rf"\b{esc}\s+is\b", "I am", out)
        out = re.sub(rf"\b{esc}\s+was\b", "I was", out)
        out = re.sub(rf"\b{esc}\s+has\b", "I have", out)
        out = re.sub(rf"\b{esc}\s+had\b", "I had", out)
        out = re.sub(rf"\b{esc}\b", "I", out)

    for pat, rep in (
        (r"\bthe (?:candidate|applicant)'s\b", "my"),
        (r"\bthe (?:candidate|applicant) is\b", "I am"),
        (r"\bthe (?:candidate|applicant) has\b", "I have"),
        (r"\bthe (?:candidate|applicant) was\b", "I was"),
        (r"\bthe (?:candidate|applicant)\b", "I"),
    ):
        out = re.sub(pat, rep, out, flags=re.I)

    # Only fix pronouns once a first-person subject is present, so a sentence
    # genuinely about someone else (a referrer, a manager) is left alone.
    if re.search(r"\bI\b", out):
        out = re.sub(r"\b(?:his|her|their)\b", "my", out, flags=re.I)
        out = re.sub(r"\bhe\s+is\b|\bshe\s+is\b|\bthey\s+are\b", "I am", out, flags=re.I)

    out = re.sub(r"\s{2,}", " ", out).strip()
    return (out[:1].upper() + out[1:]) if out else out


class LearnRequest(BaseModel):
    label: str
    value: str
    type: str | None = "text"
    name: str | None = ""
    section: str | None = ""
    options: list[str] | None = None
    automation_id: str | None = ""
    autocomplete: str | None = ""
    host: str | None = ""
    company: str | None = ""


def load_learned() -> dict:
    """{key: value} for every active learned answer, INCLUDING its aliases.

    Aliases let a rephrased question reuse the same answer. They resolve to the
    identical value, so the planner needs no extra logic — a paraphrase is just
    another key pointing at the same string.
    """
    from db.database import get_session
    from db.models import LearnedAnswer, LearnedAnswerAlias
    session = get_session()
    try:
        rows = session.query(LearnedAnswer).filter(LearnedAnswer.status == "active").all()
        by_id = {r.id: r.value for r in rows if r.value}
        out = {r.key: r.value for r in rows if r.key and r.value}
        for a in session.query(LearnedAnswerAlias).filter(
                LearnedAnswerAlias.status == "active").all():
            val = by_id.get(a.answer_id)
            if val and a.key and a.key not in out:
                out[a.key] = val
        return out
    except Exception as e:
        logger.warning(f"[autofill] learned answers unavailable: {e}")
        return {}
    finally:
        session.close()


_ALIAS_TRIED: set = set()   # keys we already asked about this process


def propose_aliases(unmatched: list[dict]) -> None:
    """Ask the local LLM whether an unanswered question is a rephrasing of one
    we already know, and record the match as an alias for next time.

    Runs in a background thread AFTER the plan is returned — /plan is already
    latency-sensitive and this must never make a page fill slower. The payoff
    lands on the NEXT application, which is exactly when it is wanted.
    """
    from agents.autofill_mapper import learned_key
    from db.database import get_session
    from db.models import LearnedAnswer, LearnedAnswerAlias
    from utils.ollama_client import generate_json

    session = get_session()
    try:
        known = session.query(LearnedAnswer).filter(LearnedAnswer.status == "active").all()
        if not known:
            return
        catalogue = [{"id": r.id, "question": r.label} for r in known][:60]

        for field in unmatched:
            label = (field.get("label") or "").strip()
            key = learned_key(label)
            if not key or key in _ALIAS_TRIED:
                continue
            _ALIAS_TRIED.add(key)
            if session.query(LearnedAnswerAlias).filter(LearnedAnswerAlias.key == key).first():
                continue
            if session.query(LearnedAnswer).filter(LearnedAnswer.key == key).first():
                continue

            prompt = (
                "Decide whether a NEW job-application question is just a "
                "REWORDING of one of the KNOWN questions - that is, the "
                "applicant would give the SAME answer to both.\n\n"
                "EXAMPLES\n"
                "Known: 'What is your highest level of education?'\n"
                "New: 'Which degree have you completed?' -> MATCH\n"
                "New: 'What did you study?' -> NO MATCH (subject, not level)\n"
                "Known: 'Are you willing to relocate?'\n"
                "New: 'Would you move for this role?' -> MATCH\n"
                "New: 'Are you willing to travel?' -> NO MATCH\n\n"
                "Same words do not mean the same question. Different topic "
                "means NO MATCH, however similar the wording.\n\n"
                'Reply ONLY with JSON: {"id": <known id or null>, "same": true|false}\n\n'
                f"NEW QUESTION: {label}\n\nKNOWN QUESTIONS:\n"
                + "\n".join(f'{c["id"]}: {c["question"]}' for c in catalogue)
            )
            try:
                res = generate_json(prompt) or {}
            except Exception as e:
                logger.debug(f"[autofill] alias proposal failed: {e}")
                continue

            aid = res.get("id")
            if not aid or not any(c["id"] == aid for c in catalogue):
                continue        # no candidate, or a hallucinated id

            # Second, focused pass. Measured on this model: picking the right
            # candidate from a list is reliable, but its yes/no verdict in the
            # same breath is not — it answered "same: false" for a genuine
            # rewording. Asked about ONE pair on its own it scored 4/5 with no
            # false positives, and the misses are conservative: a miss just
            # means the LLM drafts an answer as before, whereas a false
            # positive would silently answer a DIFFERENT question.
            target = next(c["question"] for c in catalogue if c["id"] == aid)
            try:
                verdict = generate_json(
                    "Two job-application questions. Would the applicant write "
                    "the SAME answer in both boxes?\n\n"
                    f"A: {target}\nB: {label}\n\n"
                    'Answer JSON only: {"same": true} if they ask for the same '
                    'information, {"same": false} if they ask about different '
                    "things."
                ) or {}
            except Exception as e:
                logger.debug(f"[autofill] alias confirmation failed: {e}")
                continue
            if not verdict.get("same"):
                continue
            session.add(LearnedAnswerAlias(key=key, label=label, answer_id=int(aid),
                                           origin="llm", status="active"))
            session.commit()
            logger.info(f"[autofill] learned that {label!r} means the same as answer {aid}")
    except Exception as e:
        logger.warning(f"[autofill] alias proposal aborted: {e}")
    finally:
        session.close()


@router.post("/learned")
def save_learned(req: LearnRequest):
    """Remember an answer the user typed into a field autofill could not fill.

    The policy gate runs HERE as well as in the extension, so a bad value never
    reaches disk even if the page sent something unexpected.
    """
    from agents.autofill_mapper import (is_learnable_field, is_learnable_value,
                                        learned_key)
    from db.database import get_session
    from db.models import LearnedAnswer

    field = {"label": req.label, "name": req.name, "type": req.type,
             "section": req.section, "automation_id": req.automation_id,
             "autocomplete": req.autocomplete}
    ok, why = is_learnable_field(field)
    if not ok:
        return {"saved": False, "reason": why}
    ok, why = is_learnable_value(req.value)
    if not ok:
        return {"saved": False, "reason": why}

    key = learned_key(req.label)
    session = get_session()
    try:
        row = session.query(LearnedAnswer).filter(LearnedAnswer.key == key).first()
        if row:
            changed = (row.value or "").strip() != req.value.strip()
            row.value = req.value
            row.label = req.label
            row.updated_at = datetime.now(timezone.utc)
            if changed:
                # The user overrode what we replayed — count it, so a
                # repeatedly-corrected answer is visible rather than silently
                # re-applied on every future application.
                row.corrections = (row.corrections or 0) + 1
        else:
            row = LearnedAnswer(
                key=key, label=req.label, value=req.value, field_type=req.type,
                options_seen=req.options or None, section=req.section or None,
                origin_host=req.host or None, origin_company=req.company or None)
            session.add(row)
        session.commit()
        logger.info(f"[autofill] learned: {key!r} -> {req.value[:40]!r}")
        return {"saved": True, "key": key}
    except Exception as e:
        session.rollback()
        logger.warning(f"[autofill] could not save learned answer: {e}")
        return {"saved": False, "reason": str(e)}
    finally:
        session.close()


@router.get("/learned")
def list_learned():
    """Everything remembered, newest first — so a wrong answer can be found."""
    from db.database import get_session
    from db.models import LearnedAnswer
    session = get_session()
    try:
        rows = (session.query(LearnedAnswer)
                .order_by(LearnedAnswer.updated_at.desc()).limit(500).all())
        return {"answers": [{
            "id": r.id, "label": r.label, "value": r.value, "status": r.status,
            "type": r.field_type, "times_used": r.times_used,
            "corrections": r.corrections, "company": r.origin_company,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        } for r in rows]}
    finally:
        session.close()


@router.delete("/learned/{answer_id}")
def forget_learned(answer_id: int):
    """Forget one answer outright."""
    from db.database import get_session
    from db.models import LearnedAnswer
    session = get_session()
    try:
        row = session.query(LearnedAnswer).get(answer_id)
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        session.delete(row)
        session.commit()
        return {"forgotten": True}
    finally:
        session.close()


@router.post("/plan")
def plan(req: AutofillRequest):
    profile = dict(load_profile())
    try:  # employment entries let the mapper fill work-history sections —
        # from the tailored sidecar when one exists for this company/role,
        # so inline experience blocks match the attached résumé
        profile["employment"] = _work_entries_from(
            _load_history_resume(req.resume_pref or "auto", req.company or "", req.job_title or ""))
    except Exception as e:
        logger.warning(f"[autofill] employment enrichment failed: {e}")
    archetypes = _archetype_config().get("archetypes", {})
    archetype = choose_archetype(req.job_title, req.page_text, archetypes, req.resume_pref or "auto")
    resume_summary = _load_resume_summary(archetype)
    archetype_label = (archetypes.get(archetype, {}).get("label") if archetype else None) or "AI / Data résumé"

    def essay_fn(field, ctx):
        return _draft_answer(field, profile, resume_summary, req.job_title or "",
                             req.company or "", req.page_text or "")

    fields = [f.model_dump() for f in req.fields]
    result = build_plan(fields, profile, archetype, resume_summary,
                        essay_fn=essay_fn, learned=load_learned())

    # Questions nothing could answer are candidates for "this is the same
    # question I already answered, worded differently". Asking the LLM about
    # them happens on a background thread AFTER this response is returned:
    # /plan blocks a page fill, and the benefit lands on the NEXT application.
    try:
        # Includes fields the LLM *guessed* at: a guess is precisely where a
        # remembered answer should have won, so those are the best candidates
        # for "I have already answered this, worded differently".
        unresolved = {o["id"] for o in result.get("fields", [])
                      if o.get("source") in ("none", "llm", None)}
        pending = [f.model_dump() if hasattr(f, "model_dump") else dict(f)
                   for f in req.fields if f.id in unresolved]
        if pending:
            threading.Thread(target=propose_aliases, args=(pending,),
                             name="jpaf-alias", daemon=True).start()
    except Exception as e:
        logger.debug(f"[autofill] alias scheduling skipped: {e}")

    return {
        "archetype": archetype or "ai_default",
        "archetype_label": archetype_label,
        "resume_used": "base_resume_bt.yaml" if archetype == "behavioral_technician" else "base_resume.yaml",
        **result,
    }
