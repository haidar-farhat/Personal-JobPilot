"""Import the JobRight "Applied" history into JobPilot as APPLIED applications.

    venv\\Scripts\\python scripts\\import_jobright_applied.py [--dry-run]

Reads config/jobright_applied_export.json (exported 2026-09-07 from
jobright.ai /swan/job/applied/jobs-v3). For each row:
  - reuse an existing Job when title+company already match (any source),
    otherwise insert a Job with source="jobright" whose URL is the stable
    JobRight info page (the ATS apply links are embed URLs without ids);
  - make sure an Application exists and is at least APPLIED, stamping
    date_applied from JobRight's applyTime and writing the event through
    record_status_change(source="jobright_import") — idempotent, never
    regresses a status already at/beyond APPLIED.
No scoring, no LLM, no network.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.database import get_session, record_status_change  # noqa: E402
from db.models import Application, ApplicationStatus, Job  # noqa: E402
from server.applied import _AT_OR_PAST_APPLIED, _match_by_title_company  # noqa: E402

EXPORT = ROOT / "config" / "jobright_applied_export.json"


def main(dry_run: bool) -> None:
    data = json.loads(EXPORT.read_text(encoding="utf-8"))
    cols = data["cols"]
    session = get_session()
    created = reused = marked = already = 0
    try:
        for row in data["rows"]:
            r = dict(zip(cols, row))
            url = f"https://jobright.ai/jobs/info/{r['jobId']}"
            applied_at = datetime.fromtimestamp(r["applyTime_ms"] / 1000, tz=timezone.utc)
            job = session.query(Job).filter(Job.url == url).first() or \
                _match_by_title_company(session, r["title"], r["company"])
            if job is None:
                job = Job(
                    title=r["title"], company=r["company"], location=r["location"] or None,
                    salary_text=r["salary"] or None, url=url, source="jobright",
                    source_id=r["jobId"], seniority_level=r["seniority"] or None,
                    is_remote=(r["workModel"] == "Remote"),
                    employment_type={"Full-time": "full_time", "Internship": "contract"}.get(r["employment"], "unknown"),
                    dedup_hash=hashlib.sha256(url.encode()).hexdigest(),
                    date_found=applied_at,
                )
                session.add(job)
                session.flush()
                created += 1
            else:
                reused += 1
            app = session.query(Application).filter_by(job_id=job.id).first()
            if app is None:
                app = Application(job_id=job.id, status=ApplicationStatus.FOUND)
                session.add(app)
                session.flush()
            if app.status in _AT_OR_PAST_APPLIED:
                already += 1
            else:
                note = "Imported from JobRight" + (" (applied by JobRight Agent)" if r["by_agent"] else "")
                record_status_change(session, app, ApplicationStatus.APPLIED,
                                     source="jobright_import", note=note)
                app.date_applied = applied_at
                app.lead_source = app.lead_source or "JobRight"
                marked += 1
        if dry_run:
            session.rollback()
        else:
            session.commit()
    finally:
        session.close()
    print(f"{'DRY RUN — ' if dry_run else ''}jobs created={created} reused={reused} "
          f"marked APPLIED={marked} already applied={already} total rows={len(data['rows'])}")


if __name__ == "__main__":
    main("--dry-run" in sys.argv)
