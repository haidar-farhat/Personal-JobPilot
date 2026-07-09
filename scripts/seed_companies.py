"""Seed/refresh the companies table from config/companies_seed.yaml.

Idempotent upsert keyed on normalized name. Companies whose profile_source
is "manual" are never overwritten (hand edits win). Re-runnable any time.

Run:  venv/bin/python scripts/seed_companies.py
"""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.company_linking import link_unlinked_jobs  # noqa: E402
from db.database import get_session  # noqa: E402
from db.models import Company  # noqa: E402
from utils.company_names import normalize_company_name  # noqa: E402

SEED_PATH = Path(__file__).resolve().parents[1] / "config" / "companies_seed.yaml"

# intentionally NOT in PROFILE_FIELDS: suggested is a creation-time fact;
# notes_md is user-owned
PROFILE_FIELDS = ("careers_url", "ats_platform", "priority", "status",
                  "overview_md", "why_fit_md", "hiring_bar_md")


def load_seed() -> list[dict]:
    with open(SEED_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)["companies"]


def run(close: bool = True) -> dict:
    session = get_session()
    created = updated = skipped = skipped_drafting = 0
    try:
        for entry in load_seed():
            norm = normalize_company_name(entry["name"])
            c = session.query(Company).filter_by(name_normalized=norm).first()
            if c is None:
                c = Company(name=entry["name"], name_normalized=norm,
                            profile_source="seeded",
                            suggested=bool(entry.get("suggested", False)))
                session.add(c)
                created += 1
            elif c.profile_source == "manual":
                skipped += 1
                continue
            elif c.draft_status == "drafting":
                # A LLM draft is in flight (Task 14+): overwriting now would be
                # clobbered when the draft lands — skip, rerun the seeder later.
                skipped_drafting += 1
                continue
            else:
                updated += 1
                c.profile_source = "seeded"
            for field in PROFILE_FIELDS:
                if field in entry:
                    setattr(c, field, entry[field])
        session.flush()
        # link any unlinked jobs to the (possibly new) companies
        link_unlinked_jobs(session)
        session.commit()
    finally:
        # close() without commit discards the whole batch — intentional:
        # a bad seed entry must not half-apply.
        if close:
            session.close()
    return {"created": created, "updated": updated, "skipped_manual": skipped,
            "skipped_drafting": skipped_drafting}


if __name__ == "__main__":
    print(run())
