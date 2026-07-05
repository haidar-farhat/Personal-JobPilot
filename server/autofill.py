"""Autofill API for the JobPilot browser extension.

Reuses the ranker's archetype config + résumé loader and the Ollama client.
The applicant profile is loaded directly here (a tiny YAML read) to keep the
web server decoupled from the Playwright-heavy ``auto_applier`` package.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml
from fastapi import APIRouter
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


class AutofillRequest(BaseModel):
    url: str | None = ""
    job_title: str | None = ""
    company: str | None = ""
    page_text: str | None = ""
    resume_pref: str | None = "auto"
    fields: list[FieldSpec]


@router.get("/profile")
def get_profile():
    """The applicant profile JSON, for the extension to cache + standard-fill."""
    return load_profile()


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


@router.get("/history")
def get_history(resume_pref: str = "auto"):
    """Work/education/skills as structured entries for multi-entry ATS wizards."""
    profile = load_profile()
    with open(_resume_yaml_path(resume_pref), encoding="utf-8") as f:
        resume = yaml.safe_load(f) or {}

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


@router.get("/resume_file")
def resume_file(resume_pref: str = "auto", company: str = "", job_title: str = ""):
    """Best résumé file for the extension to attach: the tailored .docx for a
    matching application if one exists, otherwise a rendered base résumé."""
    # 1. tailored materials for this company/title, newest first. Without a
    #    company to match on, skip straight to the base résumé — the newest
    #    tailored file could be for a totally unrelated role.
    if company:
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
                return FileResponse(best, filename=best.name)
        finally:
            s.close()
      except Exception as e:
        logger.warning(f"[autofill] tailored-resume lookup failed: {e}")

    # 2. rendered base résumé (cached; re-rendered when the YAML changes)
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


@router.get("/health")
def health():
    try:
        profile_loaded = bool(load_profile())
    except Exception:
        profile_loaded = False
    return {"ok": True, "ollama_up": check_ollama_health(), "profile_loaded": profile_loaded}


def _draft_answer(field: dict, profile: dict, resume_summary: str, job_title: str, company: str) -> str | None:
    """Agentic draft for an open-ended/ambiguous field, with fallback templates."""
    label = (field.get("label") or field.get("name") or "this question").strip()
    nl = label.lower()
    fbs = profile.get("fallback_essays", {})
    fb = None
    if "strength" in nl:
        fb = fbs.get("greatest_strength")
    elif "weak" in nl:
        fb = fbs.get("weakness")
    elif "role" in nl or "position" in nl:
        fb = fbs.get("why_role")
    elif "company" in nl or "why" in nl:
        fb = fbs.get("why_company")
    if fb and "{company}" in fb:
        fb = fb.replace("{company}", company or "your team")

    opts = field.get("options")
    prompt = (
        "You are the job candidate filling out an application. Answer the question "
        "concisely (max 120 words), first person, grounded ONLY in the résumé below. "
        "Do not invent facts.\n\n"
        f"QUESTION: {label}\n"
        + (f"PICK ONE OF THESE OPTIONS, returning its exact text: {opts}\n" if opts else "")
        + f"\nJOB: {job_title or ''} at {company or ''}\n\nRÉSUMÉ:\n{resume_summary[:2000]}"
    )
    try:
        return generate_text(prompt) or fb
    except Exception as e:  # Ollama down / timeout → fallback template (or None)
        logger.warning(f"[autofill] essay draft failed for {label!r}: {e}")
        return fb


@router.post("/plan")
def plan(req: AutofillRequest):
    profile = load_profile()
    archetypes = _archetype_config().get("archetypes", {})
    archetype = choose_archetype(req.job_title, req.page_text, archetypes, req.resume_pref or "auto")
    resume_summary = _load_resume_summary(archetype)
    archetype_label = (archetypes.get(archetype, {}).get("label") if archetype else None) or "AI / Data résumé"

    def essay_fn(field, ctx):
        return _draft_answer(field, profile, resume_summary, req.job_title or "", req.company or "")

    fields = [f.model_dump() for f in req.fields]
    result = build_plan(fields, profile, archetype, resume_summary, essay_fn=essay_fn)
    return {
        "archetype": archetype or "ai_default",
        "archetype_label": archetype_label,
        "resume_used": "base_resume_bt.yaml" if archetype == "behavioral_technician" else "base_resume.yaml",
        **result,
    }
