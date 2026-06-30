"""Autofill API for the JobPilot browser extension.

Reuses the ranker's archetype config + résumé loader and the Ollama client.
The applicant profile is loaded directly here (a tiny YAML read) to keep the
web server decoupled from the Playwright-heavy ``auto_applier`` package.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from fastapi import APIRouter
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
