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
_ATS_WHITELIST = {"greenhouse", "ashby", "lever", "workday", "radancy", "custom"}

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
    """Draft (or re-draft) one company's profile in place.

    Never raises: on any failure the row is marked draft_status="failed"
    with no partial field writes, so a bad draft never overwrites a good
    previous profile.
    """
    session = get_session()
    try:
        c = session.query(Company).get(company_id)
        if not c:
            return

        try:
            page = ""
            if c.careers_url:
                try:
                    page = _fetch_page_text(c.careers_url)
                except Exception as e:
                    logger.warning(f"[profiler] page fetch failed for {c.name}: {e} — drafting from name alone")

            resume = _load_resume_summary()
            data = generate_json(
                _PROMPT.format(name=c.name, page=page or "(no page available)",
                               resume=resume),
                system_prompt=_SYSTEM)

            # Field writes only happen once generate_json has succeeded — if
            # it raises, control jumps straight to `except` below and nothing
            # here runs, so a failed draft never half-overwrites a good
            # profile.
            c.overview_md = data.get("overview_md") or c.overview_md
            c.why_fit_md = data.get("why_fit_md") or c.why_fit_md
            c.hiring_bar_md = data.get("hiring_bar_md") or c.hiring_bar_md
            guess = (data.get("ats_platform_guess") or "").strip().lower()
            if guess in _ATS_WHITELIST and not c.ats_platform:
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
