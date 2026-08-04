"""Extension-facing features: company board scan + find-or-import a job.

POST /api/extension/company-scan  {url}
  - detect which ATS board the URL points at (or fetch the page and sniff for
    an embedded/linked board), pull every posting via the same per-ATS
    fetchers the scheduled scanners use, and rank them against the resume
    (strong_match_keywords + technical_skills from config/base_resume.yaml).

POST /api/extension/job  {url, title, company, description, location, ensure_score}
  - match an existing Job (same matchers as /api/applied/record) or create a
    minimal Job+Application, backfill the description if we have a better one,
    and (optionally) run the LLM ranker so the tailor endpoint can work.
    Returns {job_id, app_id} so the extension can chain into
    POST /api/application/{app_id}/tailor.
"""

import logging
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from db.database import get_session
from db.models import Application, ApplicationStatus, Company, Job, JobScore
from server.applied import AppliedPayload, _dedup_hash, _find_job
from utils.company_names import normalize_company_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/extension", tags=["extension"])

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _settings() -> dict:
    with open(PROJECT_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _pretty(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug or "").strip().title()


# ============================================================
# Board detection
# ============================================================

_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")


def detect_board(url: str):
    """(ats, slug) — or ("workday", {host,tenant,site}) — from a board URL."""
    try:
        p = urlparse(url)
    except Exception:
        return None
    host = (p.hostname or "").lower()
    seg = [s for s in p.path.split("/") if s]
    first = seg[0] if seg else ""
    if host.endswith("greenhouse.io"):
        if first == "embed":  # greenhouse.io/embed/job_board?for=<slug>
            m = re.search(r"for=([\w-]+)", p.query or "")
            return ("greenhouse", m.group(1)) if m else None
        return ("greenhouse", first) if first else None
    if host.endswith("lever.co"):
        return ("lever", first) if first else None
    if host.endswith("ashbyhq.com"):
        return ("ashby", first) if first else None
    if host == "apply.workable.com":
        return ("workable", first) if first else None
    if host.endswith(".workable.com") and host.count(".") == 2:
        return ("workable", host.split(".")[0])
    if host.endswith("smartrecruiters.com"):
        return ("smartrecruiters", first) if first else None
    if host.endswith("myworkdayjobs.com") or host.endswith("myworkdaysite.com"):
        site = next((s for s in seg if not _LOCALE.match(s)), "")
        if site:
            return ("workday", {"host": host, "tenant": host.split(".")[0], "site": site})
    return None


_SNIFF = [
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board\?[^\"'\s]*for=([\w-]+)", re.I)),
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([\w-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([\w-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w-]+)", re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([\w-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([\w-]+)", re.I)),
]
_SNIFF_JUNK = {"embed", "api", "v1", "j", "jobs", "job", "widget", "en", "wp-content"}


def sniff_board(html: str):
    """Find the most-referenced ATS board in a careers page's HTML."""
    votes: Counter = Counter()
    for ats, pat in _SNIFF:
        for slug in pat.findall(html or ""):
            if slug.lower() not in _SNIFF_JUNK:
                votes[(ats, slug)] += 1
    if not votes:
        return None
    return votes.most_common(1)[0][0]


def _fetch_board_jobs(ats: str, slug, config: dict):
    """One company's postings via the same fetchers the scheduled scanners use
    (title-relevance gate + Bay-Area/remote gate + RawJob normalization)."""
    name = _pretty(slug["tenant"] if isinstance(slug, dict) else slug)
    if ats == "greenhouse":
        from agents.scanner.career_pages import GreenhouseScanner
        return GreenhouseScanner(config)._scan_company({
            "name": name,
            "api_url": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
        })
    if ats == "lever":
        from agents.scanner.career_pages import LeverScanner
        return LeverScanner(config)._scan_company(
            {"name": name, "api_url": f"https://jobs.lever.co/{slug}"})
    if ats == "ashby":
        from agents.scanner.ashby import AshbyScanner
        return AshbyScanner(config)._scan_company({"name": name, "ashby_slug": slug})
    if ats == "workable":
        from agents.scanner.company_sites import WorkableScanner
        return WorkableScanner(config)._scan_company(
            {"name": name, "workable_account": slug})
    if ats == "smartrecruiters":
        from agents.scanner.company_sites import SmartRecruitersScanner
        return SmartRecruitersScanner(config)._scan_company(
            {"name": name, "sr_company_id": slug})
    if ats == "workday":
        from agents.scanner.company_sites import WorkdayScanner
        return WorkdayScanner(config)._scan_company({"name": name, "workday": slug})
    return []


# ============================================================
# Resume-relative ranking
# ============================================================

def _resume_terms() -> list[str]:
    """Skill/keyword vocabulary from config/base_resume.yaml."""
    try:
        with open(PROJECT_ROOT / "config" / "base_resume.yaml", encoding="utf-8") as f:
            resume = yaml.safe_load(f) or {}
    except OSError:
        return []
    terms: set[str] = set()
    for kw in resume.get("strong_match_keywords") or []:
        if isinstance(kw, str) and len(kw.strip()) >= 2:
            terms.add(kw.strip())
    for blob in (resume.get("technical_skills") or {}).values():
        for part in str(blob).split(","):
            part = part.strip()
            if len(part) >= 2:
                terms.add(part)
    return sorted(terms, key=str.lower)


def _term_pattern(term: str) -> re.Pattern:
    # lookarounds instead of \b so terms like "C++" and "Node.js" still anchor
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.I)


def score_roles(raw_jobs, resume_terms: list[str] | None = None,
                role_keywords: list[str] | None = None) -> list[dict]:
    """Rank RawJobs against the resume. Deterministic and explainable.

    ponytail: keyword heuristic — upgrade path is agents.ranker.score_job
    (local LLM) over the top N if this ever feels too coarse.
    """
    if resume_terms is None:
        resume_terms = _resume_terms()
    if role_keywords is None:
        role_keywords = _settings().get("search", {}).get("keywords", [])
    term_pats = [(t, _term_pattern(t)) for t in resume_terms]
    role_pats = [(k, _term_pattern(k)) for k in role_keywords if len(k.strip()) >= 3]

    out = []
    for rj in raw_jobs:
        title = rj.title or ""
        desc = rj.description or ""
        role_hit = next((k for k, p in role_pats if p.search(title)), None)
        title_terms = [t for t, p in term_pats if p.search(title)]
        desc_terms = [t for t, p in term_pats if p.search(desc)]
        matched = sorted(set(title_terms) | set(desc_terms), key=str.lower)
        score = 5
        if role_hit:
            score += 40
        if title_terms:
            score += 10
        score += min(40, 5 * len(set(desc_terms) - set(title_terms)))
        if rj.is_remote:
            score += 3
        out.append({
            "title": title,
            "company": rj.company,
            "url": rj.url,
            "location": rj.location,
            "is_remote": rj.is_remote,
            "score": min(98, score),
            "role_match": role_hit,
            "matched": matched[:12],
            "description": desc[:2500],
            "snippet": desc[:220],
        })
    out.sort(key=lambda j: j["score"], reverse=True)
    return out


class CompanyScanPayload(BaseModel):
    url: str = Field(min_length=1)
    limit: int = 15


@router.post("/company-scan")
def company_scan(payload: CompanyScanPayload):
    config = _settings()
    board = detect_board(payload.url)
    if board is None:
        try:
            r = requests.get(payload.url, timeout=15, headers={"User-Agent": _UA})
            r.raise_for_status()
        except requests.RequestException as e:
            return {"ok": False, "error": f"Could not fetch page: {e}"}
        board = sniff_board(r.text)
    if board is None:
        return {"ok": False, "error":
                "No supported job board found (Greenhouse / Lever / Ashby / "
                "Workable / SmartRecruiters / Workday). Try the company's "
                "careers page itself."}
    ats, slug = board
    try:
        raw = _fetch_board_jobs(ats, slug, config)
    except Exception as e:
        logger.error(f"[extension] board fetch failed for {ats}:{slug}: {e}")
        return {"ok": False, "error": f"Board fetch failed: {e}"}
    jobs = score_roles(raw)
    company = raw[0].company if raw else _pretty(
        slug["tenant"] if isinstance(slug, dict) else slug)
    return {"ok": True, "ats": ats, "company": company,
            "jobs_found": len(raw), "jobs": jobs[:payload.limit]}


# ============================================================
# Find-or-import a job (feeds the tailor + board features)
# ============================================================

class ExtensionJob(BaseModel):
    url: str = Field(min_length=1)
    title: str = ""
    company: str = ""
    description: str = ""
    location: str = ""
    # LLM scoring is slow (Ollama) — the tailor chain needs it, plain
    # save-to-board doesn't
    ensure_score: bool = True


@router.post("/job")
def save_job(payload: ExtensionJob):
    session = get_session()
    try:
        probe = AppliedPayload(url=payload.url, title=payload.title,
                               company=payload.company, source="extension")
        job = _find_job(session, probe)
        created = False
        if job is None:
            company = payload.company.strip() or (urlparse(payload.url).hostname or "unknown")
            job = Job(
                title=payload.title.strip() or "(untitled — edit me)",
                company=company,
                location=payload.location.strip(),
                description=payload.description.strip(),
                url=payload.url,
                source="extension",
                dedup_hash=_dedup_hash(payload.url),
            )
            match = session.query(Company).filter(
                Company.name_normalized == normalize_company_name(company)).first()
            if match:
                job.company_id = match.id
            session.add(job)
            try:
                session.flush()
                created = True
            except IntegrityError:
                session.rollback()
                job = session.query(Job).filter(
                    Job.dedup_hash == _dedup_hash(payload.url)).first()
                if job is None:
                    raise
        elif payload.description.strip() and not (job.description or "").strip():
            # backfill so scoring + tailoring have a JD to work from
            job.description = payload.description.strip()

        app = session.query(Application).filter_by(job_id=job.id).first()
        if app is None:
            app = Application(job_id=job.id, status=ApplicationStatus.FOUND)
            session.add(app)
            session.flush()

        scored, fit, score_error = False, None, ""
        existing = session.query(JobScore).filter_by(job_id=job.id).first()
        if existing is not None:
            scored, fit = True, existing.fit_score
        elif payload.ensure_score and (job.description or "").strip():
            try:
                from agents.ranker import score_job
                js = score_job(job)      # slow: local LLM (Ollama)
                session.add(js)
                scored, fit = True, js.fit_score
            except Exception as e:
                logger.error(f"[extension] scoring failed for job {job.id}: {e}")
                score_error = str(e)

        session.commit()
        resp = {"ok": True, "job_id": job.id, "app_id": app.id,
                "created": created, "scored": scored, "fit_score": fit}
        if score_error:
            resp["score_error"] = score_error
        return resp
    finally:
        session.close()
