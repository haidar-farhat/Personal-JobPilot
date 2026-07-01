"""Interview Prep Simulator — generate a role-specific mock-interview pack.

Given a job (its JD) plus the fit analysis already computed by the ranker
(archetype, fit score, key matches/gaps), this asks the local Gemma model for a
focused prep pack: likely questions (behavioral / technical / role-specific),
how THIS candidate should answer each using their real background, points to
lead with, gaps to reframe, and smart questions to ask the interviewer.

Local only — one Ollama call per job. Results are cached to
`output/interview_prep/{job_id}_prep.json` so re-opening a job is instant; pass
refresh=True to regenerate. Never touches the network beyond Ollama on 127.0.0.1.
"""

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from agents.ranker import _load_resume_summary
from utils.ollama_client import generate_json

logger = logging.getLogger(__name__)

_OUTPUT_DIR = Path(__file__).parent.parent / "output" / "interview_prep"

QUESTION_TYPES = {"behavioral", "technical", "role-specific"}
N_QUESTIONS = 6

INTERVIEW_SYSTEM_PROMPT = (
    "You are an expert interview coach preparing one specific candidate for one "
    "specific role. You produce a realistic, tailored mock-interview prep pack "
    "grounded in the candidate's actual background. Respond with valid JSON only."
)

INTERVIEW_PROMPT_TEMPLATE = """Create an interview-prep pack for this candidate applying to this exact role.

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
- Generate {n_questions} likely interview questions for THIS role: a realistic mix of
  behavioral, technical, and role-specific questions (not all one type).
- Ground every "strategy" and every point in the candidate's ACTUAL background above —
  name their real projects/experience (e.g. Trading Bot, JobPilot, Portfolio, Rithum, SFUSD/BT work).
  No generic filler that could apply to any candidate.
- For each likely gap or risk, give a concrete, honest reframe the candidate can use if pressed.
- Suggest sharp questions the candidate should ASK the interviewer about this role/team/company.

Respond with EXACTLY this JSON shape:
{{
  "questions": [
    {{
      "question": "<the interview question, verbatim as it would be asked>",
      "type": "behavioral" | "technical" | "role-specific",
      "why": "<one line: why THIS role/company would ask it>",
      "strategy": "<how THIS candidate should answer, referencing their real background>",
      "points": ["<2-4 concrete talking points / STAR beats>"]
    }}
  ],
  "talking_points": ["<3-5 things the candidate should proactively work into the conversation>"],
  "gaps_to_address": [
    {{"gap": "<a likely concern an interviewer would probe>", "reframe": "<how to address it honestly and positively>"}}
  ],
  "questions_to_ask": ["<3-5 sharp, specific questions for the interviewer>"]
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


def _normalize_prep(raw: dict) -> dict:
    """Validate + clamp the model's raw JSON into the stable prep contract.

    Pure (no I/O) so it can be unit-tested without Ollama. Always returns every
    key with a sane type, even if the model omitted or mis-typed fields.
    """
    raw = raw if isinstance(raw, dict) else {}

    raw_qs = raw.get("questions")
    questions = []
    for q in (raw_qs if isinstance(raw_qs, list) else [])[:8]:
        if not isinstance(q, dict):
            continue
        text = _clean_str(q.get("question"), 400)
        if not text:
            continue
        qtype = _clean_str(q.get("type"), 30).lower().replace(" ", "-")
        if qtype not in QUESTION_TYPES:
            qtype = "role-specific"
        questions.append({
            "question": text,
            "type": qtype,
            "why": _clean_str(q.get("why"), 300),
            "strategy": _clean_str(q.get("strategy"), 800),
            "points": _clean_str_list(q.get("points"), 4),
        })

    raw_gaps = raw.get("gaps_to_address")
    gaps = []
    for g in (raw_gaps if isinstance(raw_gaps, list) else [])[:6]:
        if not isinstance(g, dict):
            continue
        gap = _clean_str(g.get("gap"), 300)
        if not gap:
            continue
        gaps.append({"gap": gap, "reframe": _clean_str(g.get("reframe"), 600)})

    return {
        "questions": questions,
        "talking_points": _clean_str_list(raw.get("talking_points"), 6),
        "gaps_to_address": gaps,
        "questions_to_ask": _clean_str_list(raw.get("questions_to_ask"), 6),
    }


def generate_interview_prep(ctx: dict) -> dict:
    """Call the local model to build a prep pack from a job-context dict.

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

    prompt = INTERVIEW_PROMPT_TEMPLATE.format(
        resume_summary=resume_summary,
        job_title=ctx.get("title") or "(untitled role)",
        company=ctx.get("company") or "(company)",
        location=ctx.get("location") or "Not specified",
        archetype=archetype or "unknown",
        fit_score=fit_score if fit_score is not None else "n/a",
        key_matches=", ".join(str(m) for m in key_matches) or "(not yet scored)",
        key_gaps=", ".join(str(g) for g in key_gaps) or "(not yet scored)",
        job_description=(ctx.get("description") or "")[:3000],
        n_questions=N_QUESTIONS,
    )

    raw = generate_json(prompt, system_prompt=INTERVIEW_SYSTEM_PROMPT)
    prep = _normalize_prep(raw)
    prep["role"] = ctx.get("title") or ""
    prep["company"] = ctx.get("company") or ""
    return prep


def _prep_path(job_id: int) -> Path:
    return _OUTPUT_DIR / f"{int(job_id)}_prep.json"


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


def _read_cache(path: Path):
    """Return a cached prep dict that has real questions, else None to regenerate.

    Requires truthy `questions` (not merely present) so a stale empty/failed pack
    is never served — it regenerates instead.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[interview_prep] cache unreadable ({path.name}), regenerating: {e}")
        return None
    if isinstance(data, dict) and data.get("questions"):
        data["cached"] = True
        return data
    return None


def _write_cache(path: Path, prep: dict) -> None:
    """Write the prep JSON atomically (temp file + os.replace) so a concurrent
    reader never sees a half-written file."""
    try:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_OUTPUT_DIR), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(prep, ensure_ascii=False, indent=2))
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"[interview_prep] could not cache prep ({path.name}): {e}")


def get_or_create_prep(job_id: int, ctx: dict, refresh: bool = False) -> dict:
    """Return a cached prep pack for job_id, generating + caching it if needed.

    Serializes generation per job_id (concurrent requests share one LLM call),
    writes the cache atomically, and never caches an empty/failed pack.
    """
    path = _prep_path(job_id)
    if not refresh:
        cached = _read_cache(path)
        if cached is not None:
            return cached

    with _job_lock(int(job_id)):
        if not refresh:  # another thread may have generated it while we waited on the lock
            cached = _read_cache(path)
            if cached is not None:
                return cached
        prep = generate_interview_prep(ctx)
        prep["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        prep["cached"] = False
        if prep.get("questions"):  # don't cache an empty/failed pack — let the next open retry
            _write_cache(path, prep)
        return prep
