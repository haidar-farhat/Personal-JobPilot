"""Upskill Analyzer — aggregate skill gaps across every scored job into a
prioritized heatmap + learning plan.

Pattern ported from MadsLorentzen/ai-job-search's /upskill command, adapted to
JobPilot: instead of re-reading postings, it mines the gap analysis the ranker
already computed (JobScore.key_gaps) across the whole pipeline.

Two stages, following resume_optimizer's philosophy (deterministic facts in
Python, interpretation in the model):
  1. collect_gap_stats() — pure DB aggregation: normalize + count every gap,
     weight gaps that appear in jobs actually applied to, keep example roles.
  2. One Gemma call clusters the top gaps into learning themes with concrete
     actions and time estimates. Local only (Ollama on 127.0.0.1) — so study
     resources are named from model knowledge, not web-searched.

The report is global (not per-job), cached to `output/upskill/report.json`;
pass refresh=True to regenerate. If Ollama is down the deterministic heatmap
is still returned (themes empty, `llm_error` set) — the numbers don't need a
model.
"""

import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from agents.outreach import _clean_str, _clean_str_list
from agents.ranker import _load_resume_summary
from db.database import get_session
from db.models import Application, Job, JobScore
from utils.ollama_client import generate_json

logger = logging.getLogger(__name__)

_OUTPUT_DIR = Path(__file__).parent.parent / "output" / "upskill"
_REPORT_PATH = _OUTPUT_DIR / "report.json"

MAX_GAPS = 30          # heatmap rows kept
LLM_GAPS = 20          # top rows handed to the model
MAX_THEMES = 8
PRIORITIES = {"high", "medium", "low"}

# Statuses that mean "I actually applied" — gaps in these jobs weigh double.
_APPLIED_STATUSES = {"applied", "response_received", "interview", "rejected", "no_response"}

UPSKILL_SYSTEM_PROMPT = (
    "You are an expert career coach building a focused upskilling plan for one "
    "specific candidate from hard data about which skills their target jobs "
    "keep asking for that they lack. You cluster related gaps into learning "
    "themes and give concrete, honest, achievable steps. You never pad the plan "
    "with skills the data doesn't show. Respond with valid JSON only."
)

UPSKILL_PROMPT_TEMPLATE = """Build an upskilling plan for this candidate from their real skill-gap data.

## CANDIDATE PROFILE:
{resume_summary}

## SKILL-GAP HEATMAP (computed from {n_scored} scored job postings; do NOT recount):
Each line: gap | seen in N postings (A of them applied-to) | avg fit of those postings | example roles
{gap_lines}

## INSTRUCTIONS:
- Cluster related gaps into 3-{max_themes} learning THEMES (e.g. several cloud gaps -> one
  "Cloud & deployment" theme). Every theme must trace back to gaps in the data above.
- Prioritize by the numbers: gaps that appear often — especially in applied-to postings — come first.
- For each theme give 2-4 CONCRETE actions the candidate can actually do (a specific course topic,
  a small buildable project that fits their existing background, a certification worth having),
  plus a realistic time estimate (e.g. "2 weekends", "3-4 weeks of evenings").
- "quick_wins": 2-4 gaps closable in under a week (e.g. a resume reframe of experience they already
  have, a one-day tutorial) — only if genuinely quick.
- "summary": 2-3 sentences: the single biggest lever, stated plainly.

Respond with EXACTLY this JSON shape:
{{
  "summary": "<2-3 sentence overview>",
  "themes": [
    {{
      "theme": "<short theme name>",
      "skills": ["<the gap strings from the data this theme covers>"],
      "why": "<one line: which roles keep asking for it, per the data>",
      "priority": "high" | "medium" | "low",
      "actions": ["<2-4 concrete steps>"],
      "time_estimate": "<realistic estimate>"
    }}
  ],
  "quick_wins": ["<2-4 fast closable gaps, or fewer if the data doesn't support them>"]
}}"""


def _norm_gap(s: str) -> str:
    """Normalize a gap string for grouping: lowercase, collapse spaces, strip trailing punctuation."""
    s = re.sub(r"\s+", " ", str(s)).strip().strip(".;,").lower()
    return s


# The ranker's key_gaps mixes real candidate gaps with complaints about the
# POSTING's data quality ("No salary information provided for compensation
# scoring", "The job description is empty..."). Those aren't learnable skills —
# drop them so they can't dominate the heatmap.
# ponytail: keyword blocklist, swap for a scorer-side fix if more variants appear
_META_GAP_RE = re.compile(
    r"job description|description is (empty|missing|brief|short|vague)"
    r"|(salary|compensation|pay)\b.*\b(information|range|scoring|assessment|provided|specified|missing)"
    r"|no (salary|compensation|pay|description)"
    r"|not (specified|provided|mentioned|listed) in"
    r"|unable to (assess|determine|evaluate)"
    r"|insufficient (information|detail|data)"
    r"|keyword matching",
    re.IGNORECASE,
)


def _is_meta_gap(norm: str) -> bool:
    """True for pseudo-gaps about the posting's data quality, not the candidate."""
    return bool(_META_GAP_RE.search(norm))


def collect_gap_stats(limit: int = MAX_GAPS) -> dict:
    """Aggregate JobScore.key_gaps across the pipeline. Deterministic, no LLM.

    Returns {"gaps": [...], "totals": {...}} where each gap row is
    {gap, count, applied_count, avg_fit, examples, weight} sorted by weight
    (count + applied_count — a gap in a job you applied to counts double).
    """
    session = get_session()
    try:
        rows = (
            session.query(JobScore, Job, Application)
            .join(Job, JobScore.job_id == Job.id)
            .outerjoin(Application, Application.job_id == Job.id)
            .all()
        )
        buckets: dict = {}
        n_scored = 0
        n_applied = 0
        for score, job, app in rows:
            n_scored += 1
            status = app.status.value if (app is not None and hasattr(app.status, "value")) else None
            applied = bool(
                (app is not None and app.date_applied is not None) or status in _APPLIED_STATUSES
            )
            if applied:
                n_applied += 1
            gaps = score.key_gaps if isinstance(score.key_gaps, list) else []
            for raw_gap in gaps:
                key = _norm_gap(raw_gap)
                if not (3 <= len(key) <= 100) or _is_meta_gap(key):
                    continue
                b = buckets.get(key)
                if b is None:
                    b = buckets[key] = {
                        "gap": str(raw_gap).strip()[:100],  # first-seen casing as display form
                        "count": 0, "applied_count": 0, "_fit_sum": 0, "_fit_n": 0,
                        "examples": [],
                    }
                b["count"] += 1
                if applied:
                    b["applied_count"] += 1
                if score.fit_score is not None:
                    b["_fit_sum"] += score.fit_score
                    b["_fit_n"] += 1
                if len(b["examples"]) < 3:
                    ex = f"{job.title} — {job.company}"
                    if ex not in b["examples"]:
                        b["examples"].append(ex)
    finally:
        session.close()

    gaps = []
    for b in buckets.values():
        gaps.append({
            "gap": b["gap"],
            "count": b["count"],
            "applied_count": b["applied_count"],
            "avg_fit": round(b["_fit_sum"] / b["_fit_n"]) if b["_fit_n"] else None,
            "examples": b["examples"],
            "weight": b["count"] + b["applied_count"],
        })
    gaps.sort(key=lambda g: (-g["weight"], -g["count"], g["gap"]))
    return {
        "gaps": gaps[:limit],
        "totals": {"n_scored": n_scored, "n_applied": n_applied, "n_distinct_gaps": len(gaps)},
    }


def _plain(v, limit: int) -> str:
    """_clean_str minus markdown bold — the UI renders plain text."""
    return _clean_str(v, limit).replace("**", "")


def _plain_list(v, max_items: int, item_limit: int) -> list:
    return [s.replace("**", "") for s in _clean_str_list(v, max_items, item_limit)]


def _normalize_plan(raw: dict) -> dict:
    """Validate + clamp the model's raw JSON into the stable plan contract.

    Pure (no I/O) so it can be unit-tested without Ollama.
    """
    raw = raw if isinstance(raw, dict) else {}
    themes = []
    raw_themes = raw.get("themes")
    for t in (raw_themes if isinstance(raw_themes, list) else [])[:MAX_THEMES]:
        if not isinstance(t, dict):
            continue
        name = _plain(t.get("theme"), 100)
        if not name:
            continue
        priority = _clean_str(t.get("priority"), 20).lower()
        if priority not in PRIORITIES:
            priority = "medium"
        themes.append({
            "theme": name,
            "skills": _plain_list(t.get("skills"), 8, 100),
            "why": _plain(t.get("why"), 300),
            "priority": priority,
            "actions": _plain_list(t.get("actions"), 4, 300),
            "time_estimate": _plain(t.get("time_estimate"), 60),
        })
    return {
        "summary": _plain(raw.get("summary"), 600),
        "themes": themes,
        "quick_wins": _plain_list(raw.get("quick_wins"), 4, 200),
    }


def _gap_lines(gaps: list) -> str:
    lines = []
    for g in gaps[:LLM_GAPS]:
        fit = f"{g['avg_fit']}" if g.get("avg_fit") is not None else "n/a"
        ex = "; ".join(g.get("examples") or [])
        lines.append(f"- {g['gap']} | {g['count']} postings ({g['applied_count']} applied) | avg fit {fit} | {ex}")
    return "\n".join(lines)


def generate_upskill_report() -> dict:
    """Build the full report: deterministic heatmap + LLM learning plan.

    The heatmap always succeeds (pure DB); if the LLM step fails the report
    still returns with empty themes and `llm_error` set, so the dashboard can
    show the numbers regardless of Ollama being up.
    """
    stats = collect_gap_stats()
    report = {
        "gap_stats": stats["gaps"],
        "totals": stats["totals"],
        "summary": "",
        "themes": [],
        "quick_wins": [],
    }
    if not stats["gaps"]:
        report["summary"] = "No skill gaps recorded yet — score some jobs first."
        return report

    try:
        prompt = UPSKILL_PROMPT_TEMPLATE.format(
            resume_summary=_load_resume_summary(None),
            n_scored=stats["totals"]["n_scored"],
            gap_lines=_gap_lines(stats["gaps"]),
            max_themes=MAX_THEMES,
        )
        raw = generate_json(prompt, system_prompt=UPSKILL_SYSTEM_PROMPT)
        report.update(_normalize_plan(raw))
    except Exception as e:
        logger.warning(f"[upskill] learning-plan LLM step failed (heatmap still served): {e}")
        report["llm_error"] = str(e)
    return report


_LOCK = threading.Lock()  # single global report — one lock is enough


def _read_cache():
    """Return the cached report if it has real gap rows, else None to regenerate."""
    try:
        data = json.loads(_REPORT_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None  # normal cache miss (first run) — not an error
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[upskill] cache unreadable, regenerating: {e}")
        return None
    if isinstance(data, dict) and data.get("gap_stats"):
        data["cached"] = True
        return data
    return None


def _write_cache(report: dict) -> None:
    """Write the report JSON atomically (temp file + os.replace)."""
    try:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_OUTPUT_DIR), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(report, ensure_ascii=False, indent=2))
        os.replace(tmp, _REPORT_PATH)
    except OSError as e:
        logger.warning(f"[upskill] could not cache report: {e}")


def get_or_create_upskill(refresh: bool = False) -> dict:
    """Return the cached upskill report, generating + caching it if needed.

    Cache persists until the user hits Regenerate — the heatmap only shifts
    when a batch of new jobs gets scored, so auto-invalidation isn't worth it.
    """
    if not refresh:
        cached = _read_cache()
        if cached is not None:
            return cached

    with _LOCK:
        if not refresh:  # another thread may have generated it while we waited on the lock
            cached = _read_cache()
            if cached is not None:
                return cached
        report = generate_upskill_report()
        report["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        report["cached"] = False
        if report.get("gap_stats"):  # don't cache an empty report — let the next open retry
            _write_cache(report)
        return report
