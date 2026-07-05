"""Resume/cover-letter optimizer — JD-relative, evidence-based scoring.

Scoring approach adapted from HackerRank's hiring-agent
(github.com/interviewstreet/hiring-agent):

  * Every category score REQUIRES cited evidence — no evidence, no points.
  * Hard category caps are enforced in Python, never trusted to the LLM.
  * Deductions are an itemized ledger with a total cap.
  * Deterministic facts (keyword hit/miss) are computed in Python and handed
    to the LLM as ground truth — the model interprets, it doesn't count.

Where hiring-agent scores a CANDIDATE for a fixed role, this scores a
TAILORED RESUME + COVER LETTER against a specific job description, and emits
actionable feedback (missing keywords, weak bullets, concrete improvements)
that the tailor uses to regenerate once when the score is below threshold.
"""

import json
import logging
import re
from pathlib import Path

from utils.ollama_client import generate_json

logger = logging.getLogger(__name__)

# category -> max points (sums to 100)
CATEGORY_MAX = {
    "keyword_coverage": 35,      # JD/ATS keywords present, naturally used
    "requirement_alignment": 30, # JD must-haves demonstrated by bullets
    "impact_quantification": 20, # numbers, scale, outcomes in bullets
    "role_narrative": 15,        # emphasis/ordering tells THIS role's story
}
MAX_DEDUCTIONS = 15


OPTIMIZER_SYSTEM_PROMPT = (
    "You are a strict ATS and recruiting expert auditing application materials "
    "against one specific job description. You are SCORING, not summarizing. "
    "Every score requires concrete cited evidence from the materials — if you "
    "cannot cite evidence, the score must be low. Never award points for "
    "keyword stuffing, fabricated-sounding claims, or generic filler. "
    "You MUST respond with valid JSON only."
)

OPTIMIZER_PROMPT_TEMPLATE = """Audit this tailored resume and cover letter against the job description.

## TARGET JOB
Title: {job_title}
Company: {company}

## JOB DESCRIPTION
{job_description}

## KEYWORD GROUND TRUTH (computed programmatically — do NOT recount)
Keywords already present in the resume: {keywords_present}
Keywords MISSING from the resume: {keywords_missing}

## TAILORED RESUME (text rendering)
{resume_text}

## COVER LETTER
{cover_text}

## SCORING CATEGORIES (hard caps — never exceed)
1. keyword_coverage (0-35): How well the resume covers the JD's important keywords,
   weighted by importance to THIS role, and how naturally they are integrated.
   Use the keyword ground truth above. Stuffed/unnatural usage scores lower.
2. requirement_alignment (0-30): For each explicit requirement in the JD, is there a
   bullet or section demonstrating it with specifics? Cite which requirements are
   covered and which are not.
3. impact_quantification (0-20): Do bullets show measurable outcomes (numbers,
   scale, time, money, users)? Vague activity-listing scores low.
4. role_narrative (0-15): Does the overall emphasis and section ordering tell a
   coherent story for THIS role? Does the cover letter open specific to
   {company}, map qualifications to requirements, and avoid cliches?

## DEDUCTIONS (0-15 total, itemized)
- Keyword stuffing or unnatural phrasing
- Bullets irrelevant to this role occupying prime space
- Generic cover-letter filler ("I am writing to express...", "team player")
- Claims that don't appear grounded in the resume's own content

## OUTPUT — EXACTLY this JSON structure, nothing else:
{{
  "scores": {{
    "keyword_coverage": {{"score": 0, "max": 35, "evidence": "cite specifics"}},
    "requirement_alignment": {{"score": 0, "max": 30, "evidence": "cite specifics"}},
    "impact_quantification": {{"score": 0, "max": 20, "evidence": "cite specifics"}},
    "role_narrative": {{"score": 0, "max": 15, "evidence": "cite specifics"}}
  }},
  "deductions": {{"total": 0, "reasons": "itemized, or 'none'"}},
  "missing_keywords": ["most important JD terms still missing, max 8"],
  "weak_bullets": ["verbatim quote of up to 3 weakest bullets"],
  "improvements": ["concrete rewrite instruction 1", "instruction 2", "instruction 3"],
  "cover_letter_notes": "1-2 sentences on the cover letter specifically"
}}"""


# ------------------------------------------------------------------
# Deterministic helpers
# ------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+#./ -]", " ", (s or "").lower())


def keyword_presence(keywords: list[str], text: str) -> tuple[list[str], list[str]]:
    """Split keywords into (present, missing) by normalized containment."""
    hay = " " + re.sub(r"\s+", " ", _norm(text)) + " "
    present, missing = [], []
    for kw in keywords or []:
        k = re.sub(r"\s+", " ", _norm(kw)).strip()
        if not k:
            continue
        (present if k in hay else missing).append(kw)
    return present, missing


def render_resume_text(resume_data: dict) -> str:
    """Render the tailor's resume JSON to plain text for scoring."""
    lines = []
    for edu in resume_data.get("education", []) or []:
        lines.append(f"{edu.get('degree', '')} — {edu.get('institution', '')} ({edu.get('date', '')})")
        if edu.get("coursework"):
            cw = edu["coursework"]
            lines.append("Coursework: " + (", ".join(cw) if isinstance(cw, list) else str(cw)))
        if edu.get("senior_thesis"):
            lines.append(f"Thesis: {edu['senior_thesis']}")
    skills = resume_data.get("skills") or {}
    if isinstance(skills, dict):
        for label, content in skills.items():
            if isinstance(content, list):
                content = ", ".join(str(c) for c in content)
            lines.append(f"{label}: {content}")
    for proj in resume_data.get("project_experience", []) or []:
        lines.append(f"PROJECT: {proj.get('title', '')} ({proj.get('tech_stack', '')})")
        for b in proj.get("bullets", []) or []:
            text = b.get("text") if isinstance(b, dict) else b
            if text:
                lines.append(f"  - {text}")
    for exp in resume_data.get("work_experience", []) or []:
        lines.append(f"EXPERIENCE: {exp.get('title', '')} at {exp.get('organization', '')} ({exp.get('dates', '')})")
        for b in exp.get("bullets", []) or []:
            text = b.get("text") if isinstance(b, dict) else b
            if text:
                lines.append(f"  - {text}")
    for cert in resume_data.get("certifications", []) or []:
        if isinstance(cert, dict):
            lines.append(f"CERT: {cert.get('name', '')} ({cert.get('year', '')})")
        else:
            lines.append(f"CERT: {cert}")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Scoring
# ------------------------------------------------------------------

def score_materials(job, job_score, resume_text: str, cover_text: str) -> dict:
    """Score tailored materials against the JD. Returns a report dict.

    All caps and totals are enforced HERE in Python, hiring-agent style — the
    LLM's numbers are inputs, not the verdict.
    """
    keywords = list(job_score.ats_keywords or []) if job_score is not None else []
    present, missing = keyword_presence(keywords, resume_text)

    prompt = OPTIMIZER_PROMPT_TEMPLATE.format(
        job_title=job.title,
        company=job.company,
        job_description=(job.description or "No description")[:3000],
        keywords_present=", ".join(present) or "(none)",
        keywords_missing=", ".join(missing) or "(none)",
        resume_text=resume_text[:6000],
        cover_text=(cover_text or "")[:2500],
    )
    raw = generate_json(prompt, system_prompt=OPTIMIZER_SYSTEM_PROMPT)

    # --- deterministic post-processing: caps, floors, clamped totals ---
    scores = {}
    total = 0
    raw_scores = raw.get("scores") or {}
    for cat, cap in CATEGORY_MAX.items():
        entry = raw_scores.get(cat) or {}
        try:
            val = float(entry.get("score", 0))
        except (TypeError, ValueError):
            val = 0.0
        evidence = str(entry.get("evidence") or "").strip()
        if not evidence:      # no evidence, no points — hiring-agent rule
            val = min(val, cap * 0.3)
        val = max(0.0, min(val, cap))
        scores[cat] = {"score": round(val, 1), "max": cap, "evidence": evidence}
        total += val

    try:
        deductions = float((raw.get("deductions") or {}).get("total", 0))
    except (TypeError, ValueError):
        deductions = 0.0
    deductions = max(0.0, min(deductions, MAX_DEDUCTIONS))

    overall = max(0.0, min(100.0, total - deductions))

    return {
        "overall": round(overall, 1),
        "scores": scores,
        "deductions": {
            "total": round(deductions, 1),
            "reasons": str((raw.get("deductions") or {}).get("reasons", "")),
        },
        "keywords_present": present,
        "keywords_missing": missing,
        "missing_keywords": [str(k) for k in (raw.get("missing_keywords") or [])][:8],
        "weak_bullets": [str(b) for b in (raw.get("weak_bullets") or [])][:3],
        "improvements": [str(i) for i in (raw.get("improvements") or [])][:5],
        "cover_letter_notes": str(raw.get("cover_letter_notes", "")),
    }


def feedback_block(report: dict) -> str:
    """Render an optimizer report as prompt feedback for one tailor retry."""
    lines = [
        "## OPTIMIZER FEEDBACK ON YOUR PREVIOUS ATTEMPT "
        f"(scored {report['overall']}/100 — improve it):",
    ]
    missing = report.get("missing_keywords") or report.get("keywords_missing") or []
    if missing:
        lines.append("Work these missing JD keywords in NATURALLY (only where honest): "
                     + ", ".join(missing[:8]))
    for wb in report.get("weak_bullets", []):
        lines.append(f"Weak bullet to rewrite with concrete impact: \"{wb}\"")
    for imp in report.get("improvements", []):
        lines.append(f"- {imp}")
    return "\n".join(lines)


def save_report(report: dict, filename_base: str, config: dict) -> str:
    """Persist the optimizer report next to the other generated materials."""
    base_dir = Path(__file__).parent.parent
    out_dir = base_dir / config.get("output", {}).get("optimizer_dir", "output/optimizer")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{filename_base}_optimizer.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)
