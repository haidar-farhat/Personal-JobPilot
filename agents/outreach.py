"""Networking Outreach Drafter — draft "connect with company insiders" messages.

Given a job (its JD) plus the fit analysis already computed by the ranker
(archetype, fit score, key matches/gaps) and the candidate's résumé summary,
this asks the local Gemma model for DRAFT outreach the user can copy and send
THEMSELVES: a LinkedIn connection note, a recruiter email, a hiring-manager
email, talking points grounded in the résumé, and GENERIC role titles worth
contacting.

SAFETY: This DRAFTS text only. It never sends anything, never looks up real
people, and never invents names or contact details — only generic role titles
(e.g. "Engineering Manager, Platform"). Local only — one Ollama call per job on
127.0.0.1; no other network access.

Results are cached to `output/outreach/{job_id}_outreach.json` so re-opening a
job is instant; pass refresh=True to regenerate. The LinkedIn note is hard-capped
at 300 chars IN CODE (LinkedIn's connection-note limit) even if the model
overshoots.
"""

import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from agents.ranker import _load_resume_summary
from utils.ollama_client import generate_json

logger = logging.getLogger(__name__)

_OUTPUT_DIR = Path(__file__).parent.parent / "output" / "outreach"

# LinkedIn caps a connection request note at 300 characters. Enforced in code.
LINKEDIN_NOTE_LIMIT = 300
N_TALKING_POINTS = 5
N_ROLES = 5

OUTREACH_SYSTEM_PROMPT = (
    "You are an expert networking coach helping one specific candidate reach out "
    "to insiders at one specific company about one specific role. You draft warm, "
    "concise, professional outreach the candidate will send themselves. You NEVER "
    "invent real people's names or contact details — you only ever suggest GENERIC "
    "role titles worth contacting. Respond with valid JSON only."
)

OUTREACH_PROMPT_TEMPLATE = """Draft networking outreach for this candidate targeting this exact role.

## CANDIDATE PROFILE:
{resume_summary}

## ROLE:
Title: {job_title}
Company: {company}
Location: {location}
Detected archetype: {archetype}
Overall fit score: {fit_score}/100
Known strengths for this role: {key_matches}
Known gaps / risks for this role: {key_gaps}

## JOB DESCRIPTION (excerpt):
{job_description}

## INSTRUCTIONS:
- Draft outreach the candidate will COPY AND SEND THEMSELVES. You are writing drafts, not sending anything.
- NEVER fabricate a real person's name, email, phone, or handle. Do NOT address anyone by name.
  Use role-based greetings only (e.g. "Hi there," or "Hello [Recruiter name]," as a fill-in placeholder).
- For "roles_to_contact", list GENERIC role TITLES only (e.g. "Engineering Manager, Platform",
  "Technical Recruiter", "Senior Data Scientist on the team"). NEVER invent an actual named person.
- The LinkedIn note MUST be at most {linkedin_limit} characters (LinkedIn's hard limit) — warm, specific
  to this role/company, and worth accepting.
- Keep every email short (a few tight sentences), professional, and specific to THIS role and company.
- Ground the talking points and emails in the candidate's ACTUAL background above — name their real
  projects/experience (e.g. Trading Bot, JobPilot, Portfolio, Rithum, SFUSD/BT work). No generic filler.

Respond with EXACTLY this JSON shape:
{{
  "linkedin_note": "<connection-request note, <= {linkedin_limit} chars, warm and specific>",
  "recruiter_email": {{
    "subject": "<short, specific subject line>",
    "body": "<a few concise sentences to a recruiter; role-based greeting, no invented names>"
  }},
  "hiring_manager_email": {{
    "subject": "<short, specific subject line>",
    "body": "<a few concise sentences to the hiring manager; role-based greeting, no invented names>"
  }},
  "talking_points": ["<3-5 concrete reasons this candidate is a fit, grounded in their real background + the role>"],
  "roles_to_contact": ["<3-5 GENERIC role titles worth contacting; NEVER real names>"]
}}"""


def _clean_str(v, limit: int = 600) -> str:
    """Coerce a value to a trimmed, length-capped string."""
    if v is None:
        return ""
    return str(v).strip()[:limit]


def _clean_str_list(v, max_items: int, item_limit: int = 400) -> list:
    """Coerce a value to a list of non-empty trimmed strings."""
    if not isinstance(v, list):
        return []
    out = []
    for item in v:
        s = _clean_str(item, item_limit)
        if s:
            out.append(s)
    return out[:max_items]


# Defense-in-depth contact scrubbing. Names can't be regex-detected (the prompt
# enforces role-based greetings / bracketed placeholders), but a hallucinated
# email or phone CAN be — strip those in code so no fabricated contact detail can
# reach the UI even if the prompt regresses or the model is swapped.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)")
_LONGNUM_RE = re.compile(r"\d{7,}")


def _scrub_contacts(s: str) -> str:
    """Redact email addresses / phone numbers / long digit runs from model text."""
    if not s:
        return s
    s = _EMAIL_RE.sub("[email removed]", s)
    s = _PHONE_RE.sub("[phone removed]", s)
    s = _LONGNUM_RE.sub("[number removed]", s)
    return s


def _normalize_email(raw, subject_limit: int = 200, body_limit: int = 1500) -> dict:
    """Coerce an email value into a stable {subject, body} dict.

    Defensive against the model returning a plain string, a list, None, or a dict
    with missing/mis-typed keys instead of the expected {subject, body} object.
    """
    raw = raw if isinstance(raw, dict) else {}
    return {
        "subject": _scrub_contacts(_clean_str(raw.get("subject"), subject_limit)),
        "body": _scrub_contacts(_clean_str(raw.get("body"), body_limit)),
    }


def _normalize_outreach(raw: dict) -> dict:
    """Validate + clamp the model's raw JSON into the stable outreach contract.

    Pure (no I/O) so it can be unit-tested without Ollama. Always returns every
    key with a sane type, even if the model omitted or mis-typed fields. The
    LinkedIn note is hard-capped at LINKEDIN_NOTE_LIMIT chars here (belt-and-braces
    with the prompt) so an overshoot from the model can never leak through.
    """
    raw = raw if isinstance(raw, dict) else {}

    # Hard cap the LinkedIn note in code regardless of what the model returned,
    # then scrub any hallucinated email/phone (belt-and-braces with the prompt).
    linkedin_note = _scrub_contacts(_clean_str(raw.get("linkedin_note"), LINKEDIN_NOTE_LIMIT))[:LINKEDIN_NOTE_LIMIT]

    return {
        "linkedin_note": linkedin_note,
        "recruiter_email": _normalize_email(raw.get("recruiter_email")),
        "hiring_manager_email": _normalize_email(raw.get("hiring_manager_email")),
        "talking_points": _clean_str_list(raw.get("talking_points"), N_TALKING_POINTS),
        "roles_to_contact": _clean_str_list(raw.get("roles_to_contact"), N_ROLES),
    }


def generate_outreach(ctx: dict) -> dict:
    """Call the local model to build outreach drafts from a job-context dict.

    ctx keys: title, company, location, description, archetype, fit_score,
    key_matches (list), key_gaps (list). Raises on Ollama/JSON failure (the
    caller decides how to surface it); otherwise returns the normalized contract.
    """
    archetype = ctx.get("archetype")
    resume_summary = _load_resume_summary(archetype)

    key_matches = ctx.get("key_matches") or []
    key_gaps = ctx.get("key_gaps") or []
    if not isinstance(key_matches, list):
        key_matches = []
    if not isinstance(key_gaps, list):
        key_gaps = []
    fit_score = ctx.get("fit_score")

    prompt = OUTREACH_PROMPT_TEMPLATE.format(
        resume_summary=resume_summary,
        job_title=ctx.get("title") or "(untitled role)",
        company=ctx.get("company") or "(company)",
        location=ctx.get("location") or "Not specified",
        archetype=archetype or "unknown",
        fit_score=fit_score if fit_score is not None else "n/a",
        key_matches=", ".join(str(m) for m in key_matches) or "(not yet scored)",
        key_gaps=", ".join(str(g) for g in key_gaps) or "(not yet scored)",
        job_description=(ctx.get("description") or "")[:3000],
        linkedin_limit=LINKEDIN_NOTE_LIMIT,
    )

    raw = generate_json(prompt, system_prompt=OUTREACH_SYSTEM_PROMPT)
    outreach = _normalize_outreach(raw)
    outreach["role"] = ctx.get("title") or ""
    outreach["company"] = ctx.get("company") or ""
    return outreach


def _outreach_path(job_id: int) -> Path:
    return _OUTPUT_DIR / f"{int(job_id)}_outreach.json"


_LOCKS_GUARD = threading.Lock()
_JOB_LOCKS: dict = {}


def _job_lock(job_id: int) -> threading.Lock:
    """One lock per job_id so two tabs / a double-click share a single LLM call."""
    with _LOCKS_GUARD:
        lock = _JOB_LOCKS.get(job_id)
        if lock is None:
            lock = threading.Lock()
            _JOB_LOCKS[job_id] = lock
        return lock


def _has_content(data: dict) -> bool:
    """True if an outreach pack actually carries usable drafts.

    Used to decide whether a pack is worth serving from cache / writing to disk.
    A pack with a LinkedIn note OR any talking points OR any roles counts.
    """
    if not isinstance(data, dict):
        return False
    rec = data.get("recruiter_email")
    hm = data.get("hiring_manager_email")
    return bool(
        data.get("linkedin_note")
        or data.get("talking_points")
        or data.get("roles_to_contact")
        or (isinstance(rec, dict) and rec.get("body"))
        or (isinstance(hm, dict) and hm.get("body"))
    )


def _read_cache(path: Path):
    """Return a cached outreach dict that has real content, else None to regenerate.

    Requires actual content (not merely a present-but-empty pack) so a stale
    empty/failed pack is never served — it regenerates instead.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None  # normal cache miss (first open of this job) — not an error
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[outreach] cache unreadable ({path.name}), regenerating: {e}")
        return None
    if _has_content(data):
        data["cached"] = True
        return data
    return None


def _write_cache(path: Path, outreach: dict) -> None:
    """Write the outreach JSON atomically (temp file + os.replace) so a concurrent
    reader never sees a half-written file."""
    try:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_OUTPUT_DIR), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(outreach, ensure_ascii=False, indent=2))
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"[outreach] could not cache outreach ({path.name}): {e}")


def get_or_create_outreach(job_id: int, ctx: dict, refresh: bool = False) -> dict:
    """Return a cached outreach pack for job_id, generating + caching it if needed.

    Serializes generation per job_id (concurrent requests share one LLM call),
    writes the cache atomically, and never caches an empty/failed pack.
    """
    path = _outreach_path(job_id)
    if not refresh:
        cached = _read_cache(path)
        if cached is not None:
            return cached

    with _job_lock(int(job_id)):
        if not refresh:  # another thread may have generated it while we waited on the lock
            cached = _read_cache(path)
            if cached is not None:
                return cached
        outreach = generate_outreach(ctx)
        outreach["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        outreach["cached"] = False
        if _has_content(outreach):  # don't cache an empty/failed pack — let the next open retry
            _write_cache(path, outreach)
        return outreach
