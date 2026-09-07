"""Follow-up Nudge Drafter — draft a status-check for an application gone quiet.

The dashboard already flags applications applied 7+ days ago with no response
("⏰ Follow up" pill). This drafts the actual nudge: a short, polite follow-up
email plus a LinkedIn DM variant the user copies and sends THEMSELVES.

SAFETY (same contract as outreach.py): DRAFTS only. Never sends anything, never
looks up real people, never invents names or contact details. Only reaffirms
claims already in the submitted materials (résumé summary / key matches) — no
new claims. Local only — one Ollama call per job on 127.0.0.1.

Results are cached to `output/followups/{job_id}_followup.json`; pass
refresh=True to regenerate.
"""

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from agents.outreach import _clean_str, _clean_str_list, _normalize_email, _scrub_contacts
from agents.ranker import _load_resume_summary
from utils.ollama_client import generate_json

logger = logging.getLogger(__name__)

_OUTPUT_DIR = Path(__file__).parent.parent / "output" / "followups"

# LinkedIn DMs have no hard 300-char cap like connection notes, but a nudge
# should stay short. Enforced in code.
LINKEDIN_DM_LIMIT = 800
N_TIPS = 3

FOLLOWUP_SYSTEM_PROMPT = (
    "You are an expert career coach drafting a follow-up nudge for one candidate "
    "on one specific job application that has gone quiet. You draft short, warm, "
    "professional messages the candidate will copy and send themselves. You NEVER "
    "invent real people's names or contact details, and you NEVER introduce claims "
    "that are not in the candidate's profile below — a follow-up only reaffirms "
    "what was already submitted. Respond with valid JSON only."
)

FOLLOWUP_PROMPT_TEMPLATE = """Draft a follow-up nudge for this candidate's quiet application.

## CANDIDATE PROFILE (already submitted to this company — the ONLY claims you may reference):
{resume_summary}

## APPLICATION:
Role: {job_title}
Company: {company}
Applied: {applied_line}
Current stage: {status}
Overall fit score: {fit_score}/100
Strengths already highlighted in the application: {key_matches}

## JOB DESCRIPTION (excerpt, for context only):
{job_description}

## INSTRUCTIONS:
- Draft messages the candidate will COPY AND SEND THEMSELVES. You are writing drafts, not sending.
- NEVER fabricate a real person's name, email, phone, or handle. Role-based greetings only
  (e.g. "Hi there," or "Hello [Recruiter name]," as a fill-in placeholder).
- Tone: warm, brief, confident — never apologetic or desperate. 3-5 sentences max per message.
- Structure: reference the role and roughly when they applied, reaffirm 1-2 SPECIFIC fit points
  from the profile above, add one genuine note of continued interest, close with ONE clear low-
  pressure ask (a status check or a short call). No new claims, no pressure, no guilt.
- The LinkedIn DM must be at most {dm_limit} characters and even more casual than the email.
- "tips" are 2-3 one-line pointers on sending it well (best channel, timing, what to do if this
  nudge also goes quiet).

Respond with EXACTLY this JSON shape:
{{
  "followup_email": {{
    "subject": "<short subject referencing the role, e.g. 'Following up — <role> application'>",
    "body": "<3-5 sentence follow-up email; role-based greeting; no invented names>"
  }},
  "linkedin_message": "<casual DM variant, <= {dm_limit} chars>",
  "tips": ["<2-3 one-line sending tips>"]
}}"""


def _normalize_followup(raw: dict) -> dict:
    """Validate + clamp the model's raw JSON into the stable follow-up contract.

    Pure (no I/O) so it can be unit-tested without Ollama. The LinkedIn DM is
    hard-capped at LINKEDIN_DM_LIMIT chars in code, and every free-text field is
    scrubbed of hallucinated emails/phones (belt-and-braces with the prompt).
    """
    raw = raw if isinstance(raw, dict) else {}
    dm = _scrub_contacts(_clean_str(raw.get("linkedin_message"), LINKEDIN_DM_LIMIT))[:LINKEDIN_DM_LIMIT]
    return {
        "followup_email": _normalize_email(raw.get("followup_email")),
        "linkedin_message": dm,
        "tips": [_scrub_contacts(t) for t in _clean_str_list(raw.get("tips"), N_TIPS, 200)],
    }


def generate_followup(ctx: dict) -> dict:
    """Call the local model to draft a follow-up nudge from a job-context dict.

    ctx keys: title, company, description, archetype, fit_score, key_matches,
    status, days_since_applied. Raises on Ollama/JSON failure (the caller
    decides how to surface it); otherwise returns the normalized contract.
    """
    resume_summary = _load_resume_summary(ctx.get("archetype"))

    key_matches = ctx.get("key_matches") or []
    if not isinstance(key_matches, list):
        key_matches = []
    fit_score = ctx.get("fit_score")

    days = ctx.get("days_since_applied")
    if isinstance(days, int) and days >= 0:
        applied_line = f"about {days} days ago, no response yet"
    else:
        applied_line = "a while ago, no response yet"

    prompt = FOLLOWUP_PROMPT_TEMPLATE.format(
        resume_summary=resume_summary,
        job_title=ctx.get("title") or "(untitled role)",
        company=ctx.get("company") or "(company)",
        applied_line=applied_line,
        status=ctx.get("status") or "applied",
        fit_score=fit_score if fit_score is not None else "n/a",
        key_matches=", ".join(str(m) for m in key_matches) or "(not recorded)",
        job_description=(ctx.get("description") or "")[:1500],
        dm_limit=LINKEDIN_DM_LIMIT,
    )

    raw = generate_json(prompt, system_prompt=FOLLOWUP_SYSTEM_PROMPT)
    pack = _normalize_followup(raw)
    pack["role"] = ctx.get("title") or ""
    pack["company"] = ctx.get("company") or ""
    pack["days_since_applied"] = days if isinstance(days, int) else None
    return pack


def _followup_path(job_id: int) -> Path:
    return _OUTPUT_DIR / f"{int(job_id)}_followup.json"


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
    """True if a follow-up pack actually carries a usable draft."""
    if not isinstance(data, dict):
        return False
    email = data.get("followup_email")
    return bool(
        data.get("linkedin_message")
        or (isinstance(email, dict) and email.get("body"))
    )


def _read_cache(path: Path):
    """Return a cached follow-up dict that has real content, else None to regenerate."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None  # normal cache miss (first open of this job) — not an error
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[followup] cache unreadable ({path.name}), regenerating: {e}")
        return None
    if _has_content(data):
        data["cached"] = True
        return data
    return None


def _write_cache(path: Path, pack: dict) -> None:
    """Write the follow-up JSON atomically (temp file + os.replace) so a concurrent
    reader never sees a half-written file."""
    try:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_OUTPUT_DIR), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(pack, ensure_ascii=False, indent=2))
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"[followup] could not cache follow-up ({path.name}): {e}")


def get_or_create_followup(job_id: int, ctx: dict, refresh: bool = False) -> dict:
    """Return a cached follow-up pack for job_id, generating + caching it if needed.

    Serializes generation per job_id (concurrent requests share one LLM call),
    writes the cache atomically, and never caches an empty/failed pack.
    """
    path = _followup_path(job_id)
    if not refresh:
        cached = _read_cache(path)
        if cached is not None:
            return cached

    with _job_lock(int(job_id)):
        if not refresh:  # another thread may have generated it while we waited on the lock
            cached = _read_cache(path)
            if cached is not None:
                return cached
        pack = generate_followup(ctx)
        pack["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        pack["cached"] = False
        if _has_content(pack):  # don't cache an empty/failed pack — let the next open retry
            _write_cache(path, pack)
        return pack
