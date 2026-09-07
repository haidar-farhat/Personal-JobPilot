#!/usr/bin/env python3
"""Export top unapplied ranked jobs from jobpilot.db into an application_batch.json
manifest for the autonomous-job-applications skill (codex-job-application-automation
format). Confirmation flags always start false; authorization happens at action time."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "jobpilot.db"
RESUME_DIR = ROOT / "output" / "resumes"


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


def find_resume(company: str, title: str) -> str | None:
    """Best-matching tailored resume: company-prefixed .docx with most title-token overlap."""
    comp = norm(company)
    title_tokens = set(norm(title).split("_"))
    best, best_score = None, 0
    for p in RESUME_DIR.glob("*_resume.docx"):
        name = norm(p.stem)
        if not name.startswith(comp):
            continue
        overlap = len(title_tokens & set(name.split("_")))
        if overlap > best_score or best is None:
            best, best_score = p, overlap
    return str(best) if best else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-score", type=float, default=70.0)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--company", help="only jobs at this company (substring match)")
    ap.add_argument("--out", default=str(ROOT / "output" / "application_batch.json"))
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    # applications has a row for every job (SCORED -> MATERIALS_READY -> APPLIED);
    # "unapplied" means the row is still in a pre-applied state.
    sql = """
        SELECT j.id, j.title, j.company, j.url, j.location, j.employment_type,
               s.fit_score, s.recommended_action, a.resume_path, a.auto_apply_status
        FROM jobs j
        JOIN (SELECT job_id, fit_score, recommended_action,
                     ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY scored_at DESC) rn
              FROM job_scores) s ON s.job_id = j.id AND s.rn = 1
        LEFT JOIN applications a ON a.job_id = j.id
        WHERE COALESCE(a.status, 'SCORED') IN ('SCORED', 'MATERIALS_READY')
          AND s.fit_score >= ?
          AND LOWER(COALESCE(s.recommended_action, '')) NOT LIKE '%skip%'
    """
    params: list = [args.min_score]
    if args.company:
        sql += " AND j.company LIKE ?"
        params.append(f"%{args.company}%")
    sql += " ORDER BY s.fit_score DESC LIMIT ?"
    params.append(args.limit)
    rows = con.execute(sql, params).fetchall()
    con.close()

    jobs = []
    for i, (jid, title, company, url, location, emp_type, score, action,
            tracked_resume, auto_status) in enumerate(rows, 1):
        resume = tracked_resume if tracked_resume and Path(tracked_resume).is_file() \
            else find_resume(company, title)
        notes = []
        if not resume:
            notes.append("no tailored resume found; tailor one before applying")
        if auto_status and auto_status.startswith("failed"):
            notes.append(f"headless auto-apply failed ({auto_status}); needs browser run")
        jobs.append({
            "priority": i,
            "company": company,
            "role": title,
            "job_id": f"JP-{jid}",
            "apply_url": url,
            "status": "staged",
            "location": location,
            "employment_type": emp_type,
            "fit_score": score,
            "recommended_action": action,
            "resume_path": resume,
            "notes": "; ".join(notes),
        })

    batch = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "confirmations": {
            "personal_data_transmission_confirmed": False,
            "final_submission_confirmed": False,
            "scope": None,
        },
        "application_policies": {
            "type_personal_data_only_after_action_time_confirmation": True,
            "require_separate_final_submit_confirmation": True,
            "account_creation_is_user_handoff": True,
            "captcha_mfa_identity_are_user_handoffs": True,
            "never_store_plaintext_passwords": True,
        },
        "jobs": jobs,
    }

    out = Path(args.out)
    out.write_text(json.dumps(batch, indent=2), encoding="utf-8")
    with_resume = sum(1 for j in jobs if j["resume_path"])
    print(f"OK: {len(jobs)} jobs staged ({with_resume} with tailored resumes) -> {out}")
    for j in jobs:
        print(f"  [{j['priority']}] {j['company']} - {j['role']} (fit {j['fit_score']:.0f})"
              + ("" if j["resume_path"] else "  [needs resume]"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
