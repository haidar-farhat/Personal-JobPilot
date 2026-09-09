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

from db.database import get_session, record_status_change
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


_SETTINGS_CACHE: dict | None = None


def _settings() -> dict:
    """Load and cache settings.yaml (target metros + comp floor for scoring)."""
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is None:
        config_path = Path(__file__).parent.parent / "config" / "settings.yaml"
        with open(config_path, encoding="utf-8") as f:
            _SETTINGS_CACHE = yaml.safe_load(f) or {}
    return _SETTINGS_CACHE


def _target_locations_text() -> str:
    """Comma-joined target metros for the location_remote prompt."""
    locs = (_settings().get("search", {}) or {}).get("locations", []) or []
    return ", ".join(str(loc) for loc in locs) or "Remote (US)"


def _salary_floor(archetype: str | None = None) -> int:
    """Full-time base-salary floor used to anchor comp_range scoring.

    The $85k default is the AI/data track's "can I afford to live alone" number.
    The trainable tracks (2026-08-11: technician / sales / ops-trainee) are
    judged against their own floor — $62,400 = $30/hr × 2080 — otherwise every
    $70k data-center-tech posting scores as underpaid against a number that was
    never meant for it.
    """
    comp = _settings().get("comp", {}) or {}
    by_arch = comp.get("min_annual_by_archetype", {}) or {}
    if archetype and archetype in by_arch:
        return int(by_arch[archetype])
    return int(comp.get("min_annual_full_time", 85000))


def _load_resume_summary(archetype: str | None = None) -> str:
    """Load and format the resume for the ranking prompts.

    Behavioral Technician roles score against the behavioral résumé
    (base_resume_bt.yaml); every other archetype uses the AI/data résumé.

    Resolution is shared with agents.tailor so the résumé a job is SCORED
    against is always the one it would be TAILORED from. It also inherits the
    unedited-template guard: scoring against a placeholder résumé silently
    produced fit scores for a fictional candidate ("M.S. in Quantitative
    Economics", "AWS Certified Cloud Practitioner") on every BT posting.
    """
    from agents.tailor import _resolve_resume_path
    with open(_resolve_resume_path(archetype), encoding="utf-8") as f:
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
- Pick the SINGLE archetype that best fits the role's day-to-day work.
- ALWAYS prefer a concrete archetype. Titles are often non-obvious — map the role to its
  closest archetype by the ACTUAL work described (e.g. a software/engineering role centered on
  LLM/GenAI/agents -> ai_engineer; an analytics role that runs on AI tooling -> ai_analyst).
- If two archetypes both fit, pick the stronger one. Do NOT retreat to "unknown" just because a
  title is unusual or spans two archetypes.
- Use "unknown" ONLY as a last resort, when the role genuinely matches NONE of the archetypes.
- Confidence is 0.0-1.0

Respond with this exact JSON:
{{
    "archetype": "<one of: {archetype_keys}>",
    "confidence": <0.0-1.0>,
    "reasoning": "<one sentence>"
}}"""


def _match_archetype_by_title(title: str | None, archetypes: dict) -> str | None:
    """Keyword-match a job title against each archetype's ``keywords`` list.

    Returns the first archetype key with a substring hit in the (lower-cased)
    title — config order, so earlier archetypes win ties — or None if nothing
    matches. The "unknown" archetype has an empty keyword list, so it is never
    selected here.
    """
    title_lower = (title or "").lower()
    if not title_lower:
        return None
    for key, arch in archetypes.items():
        if any(kw in title_lower for kw in arch.get("keywords", [])):
            return key
    return None


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
    confidence = float(result.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))

    # The local LLM over-uses "unknown" and occasionally returns an out-of-set key
    # or a low-confidence guess. In any of those cases, try to rescue the routing
    # with a verbatim title-keyword match BEFORE accepting "unknown". Note "unknown"
    # is itself a valid key, so it must be checked explicitly (not just `not in`).
    if arch_key not in archetypes or arch_key == "unknown" or confidence < 0.5:
        kw_match = _match_archetype_by_title(job.title, archetypes)
        if kw_match:
            arch_key = kw_match
            confidence = max(confidence, 0.6)  # a verbatim title hit is a strong signal
        elif arch_key not in archetypes:
            # Out-of-set key with no keyword rescue → fall back to unknown.
            arch_key = "unknown"
        # Otherwise keep arch_key: a real low-confidence archetype stays as the
        # LLM's guess rather than being downgraded to "unknown".

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
Compensation: {compensation}
Archetype: {archetype} ({archetype_label})

Description:
{job_description}

## DIMENSION DEFINITIONS:
- technical_fit       (0-100): how well candidate's tools/languages overlap with what the JD asks for
- level_match         (0-100): is this entry-level / IC1-IC2 (high=junior fit, low=too senior for candidate)
- comp_range          (0-100): does this pay enough to live ALONE in the job's OWN metro? (50 if not stated)
                                 Judge against local cost of living, NOT against SF norms. The candidate's
                                 floor is ${salary_floor:,}/yr base. $95k in Tucson or Nashville is a strong
                                 score (~85); the same $95k in San Francisco barely clears rent (~45).
                                 A disclosed range at/above the floor for its metro should score 75+.
- location_remote     (0-100): can the candidate afford to live alone there, and does it fit the target list?
                                 STEP 1 — ELIGIBILITY FIRST, before anything else. The candidate can only
                                 work in the UNITED STATES. If the role is based outside the US, score 5 —
                                 no matter what else the posting says. "Remote" does NOT override this:
                                 "Remote - India", "Canada - Remote", "Remote-Vietnam" and
                                 "Remote, UAE" are all 5, because the remote work is not US-based.
                                 Only score a remote role highly when it is explicitly US-eligible.
                                 Careful: a US state or city that merely resembles a country name is
                                 still the US — "Indiana" is not India, "Dublin, OH" is not Ireland.
                                 STEP 2 — only for US-based roles, grade affordability:
                                 US-remote / remote-first = 95 (best case — decouples the job from the move).
                                 Hybrid or onsite in a TARGET METRO (listed below) = 85.
                                 Onsite in another affordable US metro not on the list = 65.
                                 Onsite in a high-cost US metro (SF Bay, NYC, Seattle, Boston, San Diego,
                                 LA, DC) = 40 — the role may be good but rent eats an entry salary alive.
                                 TARGET METROS: {target_locations}
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
- A data-analyst or analytics role that is US-remote, or onsite in one of the target metros above,
  should generally score well across the board for this candidate — that is the sweet spot.
- The candidate currently lives in San Francisco but is actively relocating to a cheaper metro.
  Do NOT treat a non-Bay location as a negative; treat an unaffordable one as the negative.

## AI-FORWARD SIGNAL (separate from the 10 dimensions above):
- ai_intensity (0-100): how central is BUILDING WITH or USING AI/LLM tooling to THIS role's day-to-day?
  100 = core AI/LLM/GenAI engineering (build LLM apps, agents, RAG); 60-90 = heavy daily AI-tool use (Copilot, ChatGPT, internal LLM tools); 20-50 = some AI exposure; 0-15 = no AI involvement.
- ai_tools: the specific AI tools/tech the JD names (e.g. "LLM APIs", "RAG", "LangChain", "Copilot", "agents", "fine-tuning"); [] if none.

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
    "reasoning": "<2-3 sentence summary>",
    "ai_intensity": <int 0-100>,
    "ai_tools": ["<AI tools/tech the role builds with or uses; [] if none>"]
}}"""


def score_dimensions(job: Job, archetype: str, resume_summary: str) -> dict:
    """Stage 2 — score 10 dimensions.

    Returns: {dimensions, key_matches, key_gaps, ats_keywords, seniority_match, reasoning}
    """
    cfg = _archetype_config()
    arch = cfg["archetypes"].get(archetype, cfg["archetypes"]["unknown"])

    description = (job.description or "No description")[:3000]

    # Surface compensation to the LLM so comp_range isn't scored blind (matters
    # most for the BT track, where pay is hourly and the whole point is >= $30/hr).
    if getattr(job, "pay_period", None) == "hourly" and job.hourly_min is not None:
        if job.hourly_max and job.hourly_max != job.hourly_min:
            compensation = f"${job.hourly_min:.0f}-${job.hourly_max:.0f}/hr"
        else:
            compensation = f"${job.hourly_min:.0f}/hr"
    elif job.salary_text:
        compensation = job.salary_text
    elif job.salary_min:
        compensation = f"${job.salary_min:,.0f}+"
    else:
        compensation = "Not stated"

    prompt = DIMENSION_PROMPT_TEMPLATE.format(
        resume_summary=resume_summary,
        job_title=job.title,
        company=job.company,
        location=job.location or "Not specified",
        compensation=compensation,
        archetype=archetype,
        archetype_label=arch.get("label", archetype),
        job_description=description,
        target_locations=_target_locations_text(),
        salary_floor=_salary_floor(archetype),
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

    # AI-forward signal (stored alongside the weighted dims, not part of the rubric)
    ai_raw = result.get("ai_intensity", None)
    try:
        ai_intensity = max(0, min(100, int(ai_raw))) if ai_raw is not None else None
    except (ValueError, TypeError):
        ai_intensity = None
    ai_tools = result.get("ai_tools") or []
    if not isinstance(ai_tools, list):
        ai_tools = []
    ai_tools = [str(t)[:60] for t in ai_tools][:12]

    return {
        "dimensions": dims,
        "key_matches": result.get("key_matches", [])[:8],
        "key_gaps": result.get("key_gaps", [])[:6],
        "ats_keywords": result.get("ats_keywords", [])[:12],
        "seniority_match": bool(result.get("seniority_match", True)),
        "reasoning": str(result.get("reasoning", ""))[:600],
        "ai_intensity": ai_intensity,
        "ai_tools": ai_tools,
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
    # Stage 1
    classification = classify_archetype(job)
    archetype = classification["archetype"]
    archetype_conf = classification["confidence"]

    # Load the résumé matching this archetype (BT -> SFUSD/behavioral résumé)
    resume_summary = _load_resume_summary(archetype)

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
        ai_intensity=dim_result.get("ai_intensity"),
        ai_tools=dim_result.get("ai_tools"),
    )


def rank_new_jobs(config: dict) -> dict:
    """Score all unscored jobs in the database. Returns {scored, queued, skipped, errors}."""
    session = get_session()
    scoring_config = config.get("scoring", {})
    auto_queue_threshold = scoring_config.get("auto_queue_threshold", 60)
    # Batch size per 15-min cycle. Was hardcoded at 20, which was fine while the
    # scanners' Bay-Area allow-list kept intake tiny. Removing that filter
    # (2026-08-03) raised intake ~5x, so 20/cycle no longer keeps up. Keep this
    # under ~45: each job costs 2-3 Ollama calls (~18s), and the cycle must
    # finish inside its 15-minute interval.
    batch_size = int(scoring_config.get("rank_batch_size", 40))

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
            .limit(batch_size)
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
                        record_status_change(session, app, ApplicationStatus.SKIPPED, source="ranker")
                    session.commit()
                    skipped_count += 1
                    continue

                job_score = score_job(job)
                session.add(job_score)

                app = session.query(Application).filter_by(job_id=job.id).first()
                if app:
                    record_status_change(session, app, ApplicationStatus.SCORED, source="ranker")
                    if job_score.fit_score >= auto_queue_threshold:
                        record_status_change(session, app, ApplicationStatus.QUEUED, source="ranker")
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
