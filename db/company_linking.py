"""Link unlinked Job rows to Company rows by normalized name.

Shared by server/companies.py (_lazy_link on reads) and
scripts/seed_companies.py — one matching rule, one place.
First-created company (lowest id) wins normalized-name collisions.
"""
from db.models import Company, Job
from utils.company_names import normalize_company_name


def link_unlinked_jobs(session) -> int:
    by_norm = {}
    for c in session.query(Company).order_by(Company.id).all():
        by_norm.setdefault(c.name_normalized, c.id)
    linked = 0
    for job in session.query(Job).filter(Job.company_id.is_(None)).all():
        cid = by_norm.get(normalize_company_name(job.company))
        if cid:
            job.company_id = cid
            linked += 1
    return linked
