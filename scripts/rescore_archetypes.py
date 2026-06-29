"""Backfill: re-score existing jobs with the updated archetypes + ai_intensity.

Re-runs the ranker (classify + dimension scoring, incl. ai_intensity) on jobs
that already have a JobScore and UPDATES that row in place — no duplicates.
Idempotent and resumable. Requires Ollama running (gemma4).

    python scripts/rescore_archetypes.py                 # all scored jobs
    python scripts/rescore_archetypes.py --limit 3       # smoke a few
    python scripts/rescore_archetypes.py --only-missing-ai   # only rows lacking ai_intensity
    python scripts/rescore_archetypes.py --ids 12,34,56
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import get_session  # noqa: E402
from db.models import Job, JobScore  # noqa: E402
from agents.ranker import score_job  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("rescore")

# Also tee progress to logs/rescore.log so a background run can be monitored.
_LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_fh = logging.FileHandler(_LOG_DIR / "rescore.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
logger.addHandler(_fh)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="only re-score the first N")
    ap.add_argument("--ids", type=str, default=None, help="comma-separated job ids")
    ap.add_argument("--only-missing-ai", action="store_true",
                    help="only rows where ai_intensity IS NULL")
    args = ap.parse_args()

    session = get_session()
    try:
        q = session.query(Job).join(JobScore, JobScore.job_id == Job.id)
        if args.ids:
            ids = [int(x) for x in args.ids.split(",") if x.strip()]
            q = q.filter(Job.id.in_(ids))
        if args.only_missing_ai:
            q = q.filter(JobScore.ai_intensity.is_(None))
        q = q.order_by(Job.id)
        if args.limit:
            q = q.limit(args.limit)
        jobs = q.all()

        logger.info(f"[rescore] {len(jobs)} jobs to re-score")
        done = 0
        for job in jobs:
            try:
                fresh = score_job(job, write_eval_report=False)
                existing = session.query(JobScore).filter_by(job_id=job.id).first()
                if not existing:
                    continue
                existing.fit_score = fresh.fit_score
                existing.archetype = fresh.archetype
                existing.archetype_confidence = fresh.archetype_confidence
                existing.dimensions = fresh.dimensions
                existing.dimension_weights = fresh.dimension_weights
                existing.ai_intensity = fresh.ai_intensity
                existing.ai_tools = fresh.ai_tools
                existing.key_matches = fresh.key_matches
                existing.key_gaps = fresh.key_gaps
                existing.ats_keywords = fresh.ats_keywords
                existing.recommended_action = fresh.recommended_action
                existing.reasoning = fresh.reasoning
                session.commit()
                done += 1
                logger.info(
                    f"[rescore] {done}/{len(jobs)} #{job.id} {(job.title or '')[:42]!r} "
                    f"-> {fresh.archetype} fit={fresh.fit_score} ai={fresh.ai_intensity}"
                )
            except Exception as e:
                session.rollback()
                logger.error(f"[rescore] job #{job.id} failed: {e}")
        logger.info(f"[rescore] DONE: {done}/{len(jobs)} re-scored")
    finally:
        session.close()


if __name__ == "__main__":
    main()
