"""Job ranking engine — 3-stage pipeline borrowed from career-ops.

Pipeline:
  Stage 1: classify_archetype(job)        -> {archetype, confidence}
  Stage 2: score_dimensions(job, archetype) -> {dim: 0-100} × 10
  Stage 3: write_evaluation(job, ...)     -> markdown report on disk

The overall fit_score is the weighted mean of the 10 dimensions, where the
weights come from `config/archetypes.yaml` and depend on the detected archetype.

Each stage uses Gemma 4 31B locally via Ollama. Total ~3 LLM calls per job vs the
old 1 call, but each is more focused and produces structured/transparent output.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

import yaml

from db.database import get_session
from db.models import Job, JobScore, Application, ApplicationStatus
from utils.ollama_client import generate_json, generate_text

logger = logging.getLogger(__name__)

# ============================================================
# Config loading
# ============================================================

_ARCHETYPE_CONFIG_CACHE: dict | None = None


def _archetype_config() -> dict:
    """Load and cache the archetype config."""
    global _ARCHETYPE_CONFIG_CACHE
    if _ARCHETYPE_CONFIG_CACHE is None:
        config_path = Path(__file__).parent.parent / "config" / "archetypes.yaml"
        with open(config_path, encoding="utf-8") as f:
            _ARCHETYPE_CONFIG_CACHE = yaml.safe_load(f)
        # Validate weights sum to ~1.0 within each archetype
        for key, arch in _ARCHETYPE_CONFIG_CACHE["archetypes"].items():
            total = sum(arch["weights"].values())
            if abs(total - 1.0) > 0.01:
                logger.warning(f"[ranker] archetype '{key}' weights sum to {total:.3f}, expected ~1.0")
    return _ARCHETYPE_CONFIG_CACHE


def _load_resume_summary() -> str:
    """Load and format the resume for the ranking prompts."""
    config_path = Path(__file__).parent.parent / "config" / "base_resume.yaml"
    with open(config_path, encoding="utf-8") as f:
        resume = yaml.safe_load(f)

    sections = []

    sections.append("EDUCATION:")
    for edu in resume.get("education", []):
        sections.append(f"  - {edu.get('degree', '')}, {edu.get('institution', '')} ({edu.get('date', '')})")

    certs = resume.get("certifications", [])
    if certs:
        sections.append("\nCERTIFICATIONS:")
        for cert in certs:
            year = cert.get("year") or cert.get("status", "")
            sections.append(f"  - {cert.get('name', '')} ({year})")

    skills = resume.get("technical_skills", {})
    if skills:
        sections.append("\nTECHNICAL SKILLS:")
        # New canonical shape: dict of {category: comma-string}
        if isinstance(skills, dict):
            for label, content in skills.items():
                if isinstance(content, list):
                    content = ", ".join(content)
                sections.append(f"  {label}: {content}")

    sections.append("\nPROJECT EXPERIENCE:")
    for proj in resume.get("project_experience", []):
        title = proj.get("title", "")
        stack = proj.get("tech_stack") or proj.get("organization", "")
        sections.append(f"  {title} ({stack})")
        for bullet in proj.get("bullets", []):
            text = bullet["text"] if isinstance(bullet, dict) else bullet
            sections.append(f"    - {text}")

    sections.append("\nWORK EXPERIENCE:")
    for exp in resume.get("work_experience", []):
        sections.append(f"  {exp.get('title', '')} at {exp.get('organization', '')} ({exp.get('dates', '')})")
        for bullet in exp.get("bullets", []):
            text = bullet["text"] if isinstance(bullet, dict) else bullet
            sections.append(f"    - {text}")

    return "\n".join(sections)


# ============================================================
# Stage 1: Archetype classification
# ============================================================

ARCHETYPE_SYSTEM_PROMPT = (
    "You are a job classification expert. Given a job posting, you decide which "
    "archetype best describes the role. You MUST respond with valid JSON only."
)

ARCHETYPE_PROMPT_TEMPLATE = """Classify this job posting into ONE of the following archetypes.

## ARCHETYPES:
{archetype_list}

## JOB POSTING:
Title: {job_title}
Company: {company}
Description (first 1500 chars):
{job_description}

## INSTRUCTIONS:
- Pick the SINGLE archetype that best fits the role's day-to-day work
- If genuinely ambiguous OR none fit well, return "unknown"
- Confidence is 0.0-1.0

Respond with this exact JSON:
{{
    "archetype": "<one of: {archetype_keys}>",
    "confidence": <0.0-1.0>,
    "reasoning": "<one sentence>"
}}"""


def classify_archetype(job: Job) -> dict:
    """Stage 1 — classify the role.

    Returns: {"archetype": str, "confidence": float, "reasoning": str}
    """
    cfg = _archetype_config()
    archetypes = cfg["archetypes"]

    archetype_list = "\n".join(
        f"- {key}: {arch['label']} — {arch['description']}"
        for key, arch in archetypes.items()
    )
    archetype_keys = ", ".join(archetypes.keys())

    description = (job.description or "")[:1500]

    prompt = ARCHETYPE_PROMPT_TEMPLATE.format(
        archetype_list=archetype_list,
        archetype_keys=archetype_keys,
        job_title=job.title,
        company=job.company,
        job_description=description,
    )

    try:
        result = generate_json(prompt, system_prompt=ARCHETYPE_SYSTEM_PROMPT)
    except Exception as e:
        logger.warning(f"[ranker] archetype classification failed for '{job.title}': {e}")
        return {"archetype": "unknown", "confidence": 0.0, "reasoning": f"classification failed: {e}"}

    arch_key = str(result.get("archetype", "unknown")).strip().lower().replace(" ", "_")
    if arch_key not in archetypes:
        # Try keyword-fallback against the title
        title_lower = (job.title or "").lower()
        for key, arch in archetypes.items():
            if any(kw in title_lower for kw in arch.get("keywords", [])):
                arch_key = key
                break
        else:
            arch_key = "unknown"

    confidence = float(result.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))

    return {
        "archetype": arch_key,
        "confidence": confidence,
        "reasoning": str(result.get("reasoning", ""))[:300],
    }


# ============================================================
# Stage 2: Multi-dimensional scoring
# ============================================================

DIMENSION_SYSTEM_PROMPT = (
    "You are a job-fit scoring expert. You score a candidate across 10 specific "
    "dimensions, each from 0 to 100. You MUST respond with valid JSON only."
)

DIMENSION_PROMPT_TEMPLATE = """Score this candidate against this job across 10 dimensions, each 0-100.

## CANDIDATE PROFILE:
{resume_summary}

## JOB POSTING:
Title: {job_title}
Company: {company}
Location: {location}
Archetype: {archetype} ({archetype_label})

Description:
{job_description}

## DIMENSION DEFINITIONS:
- technical_fit       (0-100): how well candidate's tools/languages overlap with what the JD asks for
- level_match         (0-100): is this entry-level / IC1-IC2 (high=junior fit, low=too senior for candidate)
- comp_range          (0-100): is compensation in range / disclosed / good for SF Bay (50 if not stated)
- location_remote     (0-100): SF Bay or remote-friendly? (Bay Area=90+, remote-OK=80, anywhere-but-Bay=20)
- archetype_fit       (0-100): does the role's day-to-day match the {archetype} archetype
- skills_overlap      (0-100): how many of the JD's required skills appear in the CV verbatim or near-verbatim
- growth_signal       (0-100): does the role offer learning, mentorship, career trajectory
- company_reputation  (0-100): is the company known/respected/profitable/stable
- ats_keyword_density (0-100): density of ATS-friendly keywords from the JD that we could naturally inject into the resume
- gap_severity        (0-100): how SMALL are the gaps (high=few or minor gaps, low=large/disqualifying gaps)
                                 NOTE: this is INVERSE — high score means GOOD (small gaps).

## SCORING GUIDANCE:
- Be honest. A senior/staff role with 8+ yrs required should give level_match < 30.
- An ML Eng role for a junior MS Quant Econ candidate should give technical_fit ~50, gap_severity ~40.
- A data-analyst role at a Bay Area tech company should generally score well across the board for this candidate.

Respond with this exact JSON:
{{
    "dimensions": {{
        "technical_fit":       <int 0-100>,
        "level_match":         <int 0-100>,
        "comp_range":          <int 0-100>,
        "location_remote":     <int 0-100>,
        "archetype_fit":       <int 0-100>,
        "skills_overlap":      <int 0-100>,
        "growth_signal":       <int 0-100>,
        "company_reputation":  <int 0-100>,
        "ats_keyword_density": <int 0-100>,
        "gap_severity":        <int 0-100>
    }},
    "key_matches": ["<top 3-6 specific matches>"],
    "key_gaps":    ["<top 2-5 specific gaps>"],
    "ats_keywords":["<keywords from the JD to weave into the resume>"],
    "seniority_match": <true|false>,
    "reasoning": "<2-3 sentence summary>"
}}"""


def score_dimensions(job: Job, archetype: str, resume_summary: str) -> dict:
    """Stage 2 — score 10 dimensions.

    Returns: {dimensions, key_matches, key_gaps, ats_keywords, seniority_match, reasoning}
    """
    cfg = _archetype_config()
    arch = cfg["archetypes"].get(archetype, cfg["archetypes"]["unknown"])

    description = (job.description or "No description")[:3000]

    prompt = DIMENSION_PROMPT_TEMPLATE.format(
        resume_summary=resume_summary,
        job_title=job.title,
        company=job.company,
        location=job.location or "Not specified",
        archetype=archetype,
        archetype_label=arch.get("label", archetype),
        job_description=description,
    )

    result = generate_json(prompt, system_prompt=DIMENSION_SYSTEM_PROMPT)

    dims_raw = result.get("dimensions", {}) or {}
    expected = cfg["dimensions"]
    dims: dict[str, int] = {}
    for d in expected:
        v = dims_raw.get(d, 0)
        try:
            dims[d] = max(0, min(100, int(v)))
        except (ValueError, TypeError):
            dims[d] = 0

    return {
        "dimensions": dims,
        "key_matches": result.get("key_matches", [])[:8],
        "key_gaps": result.get("key_gaps", [])[:6],
        "ats_keywords": result.get("ats_keywords", [])[:12],
        "seniority_match": bool(result.get("seniority_match", True)),
        "reasoning": str(result.get("reasoning", ""))[:600],
    }


def compute_weighted_score(dimensions: dict, archetype: str) -> tuple[int, dict]:
    """Compute the weighted-mean overall score from per-dimension scores.

    Returns: (overall_int_0_100, weights_used)
    """
    cfg = _archetype_config()
    arch = cfg["archetypes"].get(archetype, cfg["archetypes"]["unknown"])
    weights = arch["weights"]

    weighted_sum = 0.0
    weight_total = 0.0
    for dim_name, w in weights.items():
        if dim_name in dimensions:
            weighted_sum += dimensions[dim_name] * w
            weight_total += w

    if weight_total == 0:
        return 0, weights

    overall = weighted_sum / weight_total
    return max(0, min(100, int(round(overall)))), weights


# ============================================================
# Stage 3: 6-block evaluation report
# ============================================================

EVAL_SYSTEM_PROMPT = (
    "You are a senior career coach writing a structured evaluation report for a "
    "specific job posting. Output is markdown ONLY — no JSON, no preamble."
)

EVAL_PROMPT_TEMPLATE = """Write a 6-block evaluation report for this job posting.

## CANDIDATE PROFILE:
{resume_summary}

## JOB POSTING:
Title: {job_title}
Company: {company}
Location: {location}
Detected archetype: {archetype} ({archetype_label})
Overall fit score: {overall_score}/100

## DIMENSION SCORES:
{dimension_lines}

## KEY MATCHES:
{key_matches_lines}

## KEY GAPS:
{key_gaps_lines}

## JOB DESCRIPTION (excerpt):
{job_description}

## INSTRUCTIONS:
Produce a markdown report with EXACTLY these 6 sections (use the exact headers):

### 1. Role Summary
3 bullets describing what the role actually wants day-to-day (not the marketing fluff).

### 2. CV Match Analysis
A short table or bullet list with each major requirement marked GREEN / YELLOW / RED.
Use the format: `- **Requirement** — STATUS: short note`.

### 3. Level Strategy
2-4 sentences on whether to position the candidate as IC1, IC2, mid-level, etc., based on the JD's seniority signals.

### 4. Comp Research
Best estimate of comp range for this title at this company in SF Bay (use generic ranges if unknown). One sentence on whether the disclosed range (if any) is fair.

### 5. Personalization Angles
2-3 specific hooks for the cover letter — things only THIS candidate could say about THIS company/role.

### 6. Interview Prep
3-5 likely behavioral or technical questions, each with a one-line strategy on how to answer using the candidate's projects (Trading Bot / JobPilot / Portfolio / Rithum).

Total length: 400-700 words. Be specific. No corporate cliches."""


def write_evaluation(
    job: Job,
    archetype: str,
    overall_score: int,
    dimensions: dict,
    key_matches: list,
    key_gaps: list,
    resume_summary: str,
    output_dir: Path,
) -> str | None:
    """Stage 3 — produce the 6-block evaluation markdown report on disk.

    Returns: path to the written .md file, or None if generation failed.
    """
    cfg = _archetype_config()
    arch = cfg["archetypes"].get(archetype, cfg["archetypes"]["unknown"])

    dim_lines = "\n".join(f"- {k}: {v}/100" for k, v in dimensions.items())
    matches_lines = "\n".join(f"- {m}" for m in key_matches) or "- (none surfaced)"
    gaps_lines = "\n".join(f"- {g}" for g in key_gaps) or "- (none surfaced)"

    description = (job.description or "")[:2500]

    prompt = EVAL_PROMPT_TEMPLATE.format(
        resume_summary=resume_summary,
        job_title=job.title,
        company=job.company,
        location=job.location or "Not specified",
        archetype=archetype,
        archetype_label=arch.get("label", archetype),
        overall_score=overall_score,
        dimension_lines=dim_lines,
        key_matches_lines=matches_lines,
        key_gaps_lines=gaps_lines,
        job_description=description,
    )

    try:
        markdown = generate_text(prompt, system_prompt=EVAL_SYSTEM_PROMPT)
    except Exception as e:
        logger.warning(f"[ranker] evaluation report failed for '{job.title}': {e}")
        return None

    # Prepend a metadata header
    header = (
        f"# {job.title} — {job.company}\n\n"
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}\n"
        f"- Archetype: **{arch.get('label', archetype)}**\n"
        f"- Overall fit: **{overall_score}/100**\n"
        f"- Location: {job.location or '—'}\n"
        f"- Source: {job.source}\n"
        f"- URL: {job.url}\n\n"
        "---\n\n"
    )

    safe_company = re.sub(r"[^A-Za-z0-9\- ]", "", job.company).strip().replace(" ", "_")
    safe_title = re.sub(r"[^A-Za-z0-9\- ]", "", job.title).strip().replace(" ", "_")
    filename = f"{safe_company}_{safe_title}_{job.id}_eval.md"[:140]

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    path.write_text(header + markdown.strip() + "\n", encoding="utf-8")
    return str(path)


# ============================================================
# Pre-filter (unchanged from previous ranker)
# ============================================================


def _pre_filter(job: Job, config: dict) -> bool:
    """Quick pre-filter before spending LLM tokens. True = score it, False = skip."""
    search_config = config.get("search", {})
    excluded = search_config.get("excluded_title_keywords", [])
    max_exp = search_config.get("max_experience_years", 5)

    title_lower = (job.title or "").lower()
    desc_lower = (job.description or "").lower()

    for kw in excluded:
        if kw.lower() in title_lower:
            return False

    exp_patterns = [
        r"(\d+)\+?\s*(?:years?|yrs?)\s*(?:of\s*)?(?:experience|exp)",
        r"(?:minimum|at least|requires?)\s*(\d+)\s*(?:years?|yrs?)",
    ]
    for pattern in exp_patterns:
        matches = re.findall(pattern, desc_lower)
        for match in matches:
            try:
                years = int(match)
                if years > max_exp:
                    return False
            except ValueError:
                continue

    return True


# ============================================================
# Public entry points
# ============================================================


def score_job(job: Job, write_eval_report: bool = True) -> JobScore:
    """Run all 3 stages on a single job and return an unsaved JobScore."""
    resume_summary = _load_resume_summary()

    # Stage 1
    classification = classify_archetype(job)
    archetype = classification["archetype"]
    archetype_conf = classification["confidence"]

    # Stage 2
    dim_result = score_dimensions(job, archetype, resume_summary)
    overall, weights = compute_weighted_score(dim_result["dimensions"], archetype)

    # Stage 3 (skipped for low-quality jobs to save tokens)
    eval_path: str | None = None
    if write_eval_report and overall >= 50:
        output_dir = Path(__file__).parent.parent / "output" / "evaluations"
        eval_path = write_evaluation(
            job=job,
            archetype=archetype,
            overall_score=overall,
            dimensions=dim_result["dimensions"],
            key_matches=dim_result["key_matches"],
            key_gaps=dim_result["key_gaps"],
            resume_summary=resume_summary,
            output_dir=output_dir,
        )

    # Recommended action — uses the archetype's own threshold for auto-apply hint
    cfg = _archetype_config()
    arch = cfg["archetypes"].get(archetype, cfg["archetypes"]["unknown"])
    auto_apply_floor = arch.get("auto_apply_min_score", 75)

    if overall >= auto_apply_floor:
        recommended_action = "apply"
    elif overall >= 50:
        recommended_action = "maybe"
    else:
        recommended_action = "skip"

    return JobScore(
        job_id=job.id,
        fit_score=overall,
        key_matches=dim_result["key_matches"],
        key_gaps=dim_result["key_gaps"],
        ats_keywords=dim_result["ats_keywords"],
        seniority_match=dim_result["seniority_match"],
        recommended_action=recommended_action,
        reasoning=dim_result["reasoning"],
        archetype=archetype,
        archetype_confidence=archetype_conf,
        dimensions=dim_result["dimensions"],
        dimension_weights=weights,
        evaluation_path=eval_path,
    )


def rank_new_jobs(config: dict) -> dict:
    """Score all unscored jobs in the database. Returns {scored, queued, skipped, errors}."""
    session = get_session()
    scoring_config = config.get("scoring", {})
    auto_queue_threshold = scoring_config.get("auto_queue_threshold", 60)

    scored_count = 0
    queued_count = 0
    skipped_count = 0
    errors = []

    try:
        unscored_jobs = (
            session.query(Job)
            .outerjoin(JobScore)
            .filter(JobScore.id.is_(None))
            .order_by(Job.date_found.desc())
            .limit(20)  # batch
            .all()
        )

        logger.info(f"[ranker] Found {len(unscored_jobs)} unscored jobs")

        for job in unscored_jobs:
            try:
                if not _pre_filter(job, config):
                    job_score = JobScore(
                        job_id=job.id,
                        fit_score=0,
                        recommended_action="skip",
                        reasoning="Pre-filtered: seniority or experience mismatch",
                        seniority_match=False,
                        archetype="unknown",
                        archetype_confidence=0.0,
                    )
                    session.add(job_score)

                    app = session.query(Application).filter_by(job_id=job.id).first()
                    if app:
                        app.status = ApplicationStatus.SKIPPED
                    session.commit()
                    skipped_count += 1
                    continue

                job_score = score_job(job)
                session.add(job_score)

                app = session.query(Application).filter_by(job_id=job.id).first()
                if app:
                    app.status = ApplicationStatus.SCORED
                    if job_score.fit_score >= auto_queue_threshold:
                        app.status = ApplicationStatus.QUEUED
                        queued_count += 1

                session.commit()
                scored_count += 1
                logger.info(
                    f"[ranker] {job.title} at {job.company}: "
                    f"archetype={job_score.archetype} score={job_score.fit_score} action={job_score.recommended_action}"
                )

            except Exception as e:
                session.rollback()
                error_msg = f"Error scoring '{job.title}' at '{job.company}': {e}"
                logger.error(f"[ranker] {error_msg}")
                errors.append(error_msg)

    finally:
        session.close()

    result = {
        "scored": scored_count,
        "queued": queued_count,
        "skipped": skipped_count,
        "errors": errors,
    }
    logger.info(
        f"[ranker] Ranking complete: {scored_count} scored, {queued_count} queued, {skipped_count} skipped"
    )
    return result
