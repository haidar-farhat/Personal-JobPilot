"""Retire jobs whose location the non-US gate rejects.

The scan-time gate (BaseScanner._should_skip_location) stops new foreign reqs
from entering the pipeline, but jobs scanned BEFORE that gate existed are still
sitting in the board — and they score well enough to occupy the top of the
fit-ranked dashboard. Being ineligible to work in a country is a hard
disqualifier, not a small scoring penalty, and location_remote only carries
~8-10% of the composite, so scoring alone cannot push them out.

This marks them NO_LONGER_AVAILABLE, deliberately NOT skipped. The UI's
buildAffinity() treats `skipped` as a NEGATIVE training signal for agent
ranking, so bulk-skipping 140+ foreign reqs would teach the ranker that their
archetypes and companies are disliked — but they were rejected for geography,
not for the role. NO_LONGER_AVAILABLE carries the right meaning ("real job, not
obtainable by you"), lands in the same closed column on the Board, and is
excluded from the Home list.

It does NOT delete anything: the Job and JobScore rows stay, and the status
change goes through record_status_change so it lands in the ApplicationEvent
log and can be reversed.

    python scripts/skip_non_us_jobs.py --dry-run     # report only (default)
    python scripts/skip_non_us_jobs.py --apply
    python scripts/skip_non_us_jobs.py --apply --include-applied

Applied/interviewing rows are left alone unless --include-applied is passed —
if Matthew already applied somewhere, that history is not ours to rewrite.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.scanner.base import BaseScanner  # noqa: E402
from db.database import get_session, record_status_change  # noqa: E402
from db.models import Application, ApplicationStatus, Job  # noqa: E402

# Statuses representing real-world activity we must not overwrite.
PROTECTED = {
    ApplicationStatus.APPLIED,
    ApplicationStatus.INTERVIEW,
    ApplicationStatus.RESPONSE_RECEIVED,
    ApplicationStatus.REJECTED,
    ApplicationStatus.NO_RESPONSE,
}


class _Gate(BaseScanner):
    """BaseScanner is abstract; we only want its location predicate."""

    source_name = "maintenance"

    def scan(self):
        return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    ap.add_argument("--include-applied", action="store_true",
                    help="also retire jobs already applied to / interviewing")
    args = ap.parse_args()

    cfg_path = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    gate = _Gate(config)
    if not gate.excluded_locations:
        print("settings.yaml search.excluded_locations is empty — nothing to do.")
        return

    session = get_session()
    try:
        rows = (
            session.query(Application, Job)
            .join(Job, Application.job_id == Job.id)
            .all()
        )

        targets, protected_hits = [], []
        for app, job in rows:
            if not gate._should_skip_location(job.location or ""):
                continue
            if app.status == ApplicationStatus.NO_LONGER_AVAILABLE:
                continue
            if app.status in PROTECTED and not args.include_applied:
                protected_hits.append((app, job))
                continue
            targets.append((app, job))

        print(f"non-US jobs to retire : {len(targets)}")
        if protected_hits:
            print(f"protected (applied/interviewing, left alone): {len(protected_hits)}")
        if targets:
            print("  by status:", dict(Counter(a.status.value for a, _ in targets)))
            print("  sample:")
            for app, job in targets[:10]:
                print(f"    {job.id:5d}  {(job.location or '')[:38]:40s} {job.title[:38]}")

        if not args.apply:
            print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
            return

        for app, job in targets:
            record_status_change(
                session, app, ApplicationStatus.NO_LONGER_AVAILABLE,
                source="skip_non_us_jobs",
                note=f"Outside US hiring scope: {job.location!r}",
            )
        session.commit()
        print(f"\nDONE: {len(targets)} marked NO_LONGER_AVAILABLE "
              f"(reversible — see ApplicationEvent log).")
    finally:
        session.close()


if __name__ == "__main__":
    main()
