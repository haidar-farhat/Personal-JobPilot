"""Backfill work_mode / seniority / employment_type / comp on existing jobs.

New scans enrich as they ingest (agents/scanner/base.py), but rows already in
the database were written before that existed and have employment_type
'unknown', seniority_level NULL and no compensation — which is what makes the
dashboard's filters render empty controls.

ONLY ADDS. Every write goes through utils.job_enrich.enrich(existing=...), which
omits any key already holding a real value, so a field a scanner set
authoritatively is never overwritten. The one deliberate exception is
`is_remote`: it is a Boolean defaulting to False, so a stored False cannot be
told apart from "never determined" and may be replaced; a stored True is a
positive claim and is kept.

    python scripts/backfill_job_enrichment.py --dry-run
    python scripts/backfill_job_enrichment.py
    python scripts/backfill_job_enrichment.py --source linkedin_public
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.database import get_session          # noqa: E402
from db.models import Job                    # noqa: E402
from utils.job_enrich import enrich          # noqa: E402


FIELDS = ("work_mode", "is_remote", "seniority_level", "employment_type",
          "pay_period", "salary_min", "salary_max", "salary_text",
          "hourly_min", "hourly_max")


def _existing(job: Job) -> dict:
    """What the row already asserts, so enrich() does not overwrite it."""
    return {
        "is_remote": job.is_remote,
        "seniority_level": job.seniority_level,
        # 'unknown' is the scanner's placeholder, not a determination.
        "employment_type": job.employment_type if job.employment_type not in (None, "", "unknown") else None,
        "pay_period": job.pay_period if job.pay_period not in (None, "", "unknown") else None,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "hourly_min": job.hourly_min,
        "hourly_max": job.hourly_max,
        "work_mode": getattr(job, "work_mode", None),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--source", help="limit to one Job.source value")
    ap.add_argument("--limit", type=int, default=0, help="cap rows processed (0 = all)")
    args = ap.parse_args()

    session = get_session()
    try:
        q = session.query(Job)
        if args.source:
            q = q.filter(Job.source == args.source)
        if args.limit:
            q = q.limit(args.limit)
        jobs = q.all()

        filled = Counter()
        changed_rows = 0
        no_description = 0

        for job in jobs:
            if not (job.description or "").strip():
                no_description += 1
            try:
                patch = enrich(job.title or "", job.location or "",
                               job.description or "", existing=_existing(job))
            except Exception as e:
                print(f"  ! job {job.id}: {type(e).__name__}: {e}")
                continue
            patch = {k: v for k, v in patch.items() if k in FIELDS}
            if not patch:
                continue
            # Skip a patch whose only content restates the is_remote default.
            if set(patch) == {"is_remote"} and patch["is_remote"] == bool(job.is_remote):
                continue
            changed_rows += 1
            for k, v in patch.items():
                filled[k] += 1
                if not args.dry_run:
                    setattr(job, k, v)

        if not args.dry_run:
            session.commit()

        scope = f"source={args.source}" if args.source else "all sources"
        print(f"\n{'DRY RUN — ' if args.dry_run else ''}{len(jobs)} job(s) examined ({scope})")
        print(f"  rows with no description : {no_description}"
              f"   <-- nothing but title/location to read")
        print(f"  rows updated             : {changed_rows}")
        if filled:
            print("\n  fields filled:")
            for k, n in filled.most_common():
                print(f"    {k:18s} {n}")
        else:
            print("\n  nothing to fill.")
        if args.dry_run:
            print("\n  (dry run — no changes written)")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
