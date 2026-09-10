"""Work out which internet domain an employer actually owns.

WHY THIS EXISTS
---------------
Measured on 40 real jobs from this database, the address resolver found a
recipient for 15 (38%). Of the 25 misses, **16 were `domain_unconfirmed`** — a
perfectly plausible `careers@<something>` had been constructed, but the domain
could not be shown to belong to that employer, so it was refused.

The obvious reading of that — "we cannot FIND the domain" — is wrong, and
measuring it said so: over 60 real jobs, `company_domain_with_origin` already
produced a domain for 57 (95%), and this module produces one for the same 57.
Discovery was never the problem. CONFIRMATION was. A domain guessed from a bare
company name is unprovable by construction, so the ownership check refuses it,
and the address dies with it.

So this module's job is not to find more domains. It is to find the SAME domain
from a source good enough that no proof is needed: over those 60 jobs it returns
an authoritative domain for 33, each of which now bypasses the ownership check
that was rejecting them.

Meanwhile this repository already holds better evidence and reads none of it:

  * `config/target_companies.yaml` — 97 hand-curated employers with career URLs.
  * `jobs.extra_urls` — populated on 746 rows, and its `source` field literally
    names the ATS slug: `{"source": "greenhouse:anthropic", "url": ...}`.
  * the `companies` table — `Company.careers_url`, seeded by scripts/seed_companies.py.

This module layers those sources, most trustworthy first, and reports WHICH one
answered so the caller can decide how much to trust it.

AUTHORITATIVE vs NOT
--------------------
`authoritative=True` means "this domain is the employer's, no further proof
needed", and the caller may skip the homepage ownership check. That claim is
made ONLY for a domain a human curated or one the employer served the posting
from. Everything inferred — an ATS slug, a name guess — is returned with
`authoritative=False` and still has to pass `domain_confidently_belongs_to`.
Getting this backwards is how `careers@linkedin.com` once became the answer for
every company in the database.

A CAREERS DOMAIN IS NOT A MAIL DOMAIN
-------------------------------------
`kaiserpermanentejobs.org` is a real curated career URL, but mail does not land
there. Such domains are returned with `is_careers_domain=True` so a caller can
crawl them for a published address while refusing to construct `careers@` on
them.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from utils.company_email import _ATS_HOSTS, _BOARD_HOSTS, _CAREER_SUBS, has_mx
from utils.company_names import normalize_company_name

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
TARGETS_PATH = ROOT / "config" / "target_companies.yaml"

#: Subdomains to strip beyond _CAREER_SUBS before treating a host as the mail
#: domain. A curated career_url often points at a marketing or docs subdomain.
_EXTRA_SUBS = ("about", "www2", "info", "go", "get", "corporate", "company")

#: Tried in order when guessing a domain from a bare name. `.com` first because
#: it is overwhelmingly the most likely, then the tech TLDs, then the country
#: ones for markets this search actually covers — a Lebanese employer is far
#: more likely to sit on `.com.lb` or `.lb` than on `.dev`, and without them a
#: Beirut company can never have an address constructed for it at all.
_TLDS = (".com", ".io", ".ai", ".co", ".net", ".org", ".tech", ".dev",
         ".com.lb", ".lb", ".me", ".ae", ".sa", ".com.tr", ".eu")

#: A domain label containing one of these is a CAREERS domain, not the
#: employer's mail domain — kaiserpermanentejobs.org is a real example.
_CAREERS_WORDS = ("jobs", "careers", "career", "hiring", "recruiting", "talent")

#: extra_urls carries {"source": "greenhouse:anthropic", ...}; the part after
#: the colon is the employer's slug on that ATS.
_SOURCE_SLUG_RE = re.compile(r"^([a-z0-9_]+):([A-Za-z0-9][A-Za-z0-9._-]*)$")

#: Slug position in an ATS URL path, by host fragment.
_ATS_PATH_SLUG = {
    "greenhouse.io": 0,          # boards.greenhouse.io/<slug>/jobs/123
    "lever.co": 0,               # jobs.lever.co/<slug>/<id>
    "ashbyhq.com": 0,            # jobs.ashbyhq.com/<slug>/<id>
    "smartrecruiters.com": 0,
    "workable.com": 0,
    "breezy.hr": 0,
    "recruitee.com": 0,
    "teamtailor.com": 0,
}
#: Hosts where the slug is the leftmost label, not a path segment.
_ATS_SUBDOMAIN = ("myworkdayjobs.com", "workday.com", "icims.com", "jobvite.com",
                  "bamboohr.com", "applytojob.com", "hire.lever.co")

ORIGINS = ("companies_table", "target_yaml", "posting_host", "ats_slug",
           "extra_urls", "name_guess")
AUTHORITATIVE_ORIGINS = frozenset({"companies_table", "target_yaml", "posting_host"})


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def normalize_name(name: str | None) -> str:
    """Comparable form of a company name. Reuses the project's own normaliser."""
    return normalize_company_name(name) or ""


def domain_label(domain: str) -> str:
    """The registrable label: 'acme' from 'www.acme.co.uk'."""
    host = (domain or "").lower().strip().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host.split(".")[0] if host else ""


def is_board_or_ats(host: str) -> bool:
    """Is this a job board or an ATS rather than an employer's own domain?

    A board host is the more dangerous of the two to miss: every LinkedIn
    posting shares linkedin.com, so treating it as an employer domain collapses
    every company onto one inbox.
    """
    h = (host or "").lower()
    return any(b in h for b in _BOARD_HOSTS) or any(a in h for a in _ATS_HOSTS)


def is_careers_domain(domain: str) -> bool:
    """True for a dedicated careers domain, where application mail does not land."""
    label = domain_label(domain)
    return any(w in label for w in _CAREERS_WORDS) and label not in _CAREERS_WORDS


def _corporate_host(url: str) -> str | None:
    """The employer's own host from a URL, or None when it is a board/ATS/careers sub."""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    if is_board_or_ats(host):
        return None
    # careers.acme.com -> acme.com; the careers subdomain rarely takes mail.
    # "about" is stripped too but is NOT in _CAREER_SUBS: measured, GitLab's
    # curated career_url is about.gitlab.com, and mail addressed there does not
    # reach them. A marketing subdomain is the same problem as a careers one.
    first, _, rest = host.partition(".")
    if (first in _CAREER_SUBS or first in _EXTRA_SUBS) and rest.count(".") >= 1:
        host = rest
    return host or None


# ---------------------------------------------------------------------------
# layer 1-2: curated sources
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def load_target_domains() -> dict[str, str]:
    """normalised company name -> domain, from config/target_companies.yaml.

    The file uses `career_url` (singular), unlike Company.careers_url. Cached
    for the process: 97 entries re-parsed per job would be pure waste.
    """
    out: dict[str, str] = {}
    try:
        raw = yaml.safe_load(TARGETS_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.debug(f"[company_domain] target_companies.yaml unreadable: {e}")
        return out
    entries = raw.get("companies") if isinstance(raw, dict) else raw
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        key = normalize_name(entry.get("name"))
        url = entry.get("career_url") or entry.get("careers_url") or ""
        host = _corporate_host(url)
        if key and host:
            out.setdefault(key, host)
    return out


def _from_companies_table(company: str, session) -> str | None:
    """Company.careers_url for a name-normalised match."""
    if session is None or not company:
        return None
    try:
        from db.models import Company
        key = normalize_name(company)
        row = (session.query(Company)
               .filter(Company.name_normalized == key).first())
        if row is None:
            return None
        return _corporate_host(row.careers_url or "")
    except Exception as e:                       # pragma: no cover - defensive
        logger.debug(f"[company_domain] companies lookup failed: {e}")
        return None


# ---------------------------------------------------------------------------
# layer 3: the ATS slug
# ---------------------------------------------------------------------------

def domain_from_ats_url(url: str) -> tuple[str, str] | None:
    """(employer_slug, ats_name) from an ATS posting URL, or None.

    An ATS URL names the EMPLOYER — boards.greenhouse.io/acme is Acme's board,
    not Greenhouse's. That makes the slug a far better domain candidate than a
    company name, because it is what the employer chose to call itself.
    """
    try:
        parts = urlparse(url or "")
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None

    for ats in _ATS_SUBDOMAIN:
        if host.endswith(ats):
            label = host[: -len(ats)].strip(".").split(".")[0]
            if label and label not in ("www", "jobs", "boards", "careers", "hire"):
                return label, ats
    segments = [seg for seg in parts.path.split("/") if seg]
    for frag, idx in _ATS_PATH_SLUG.items():
        if frag in host and len(segments) > idx:
            slug = segments[idx]
            if slug and slug.lower() not in ("jobs", "job", "embed", "search"):
                return slug, frag
    return None


def _slugs_from_extra_urls(extra_urls: Any) -> list[tuple[str, str]]:
    """Every (slug, ats) an extra_urls blob names.

    The `source` field is the richest signal in the database and nothing read it
    before: "greenhouse:anthropic" states the employer's slug outright.
    """
    out: list[tuple[str, str]] = []
    rows = extra_urls
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except (ValueError, TypeError):
            return out
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        m = _SOURCE_SLUG_RE.match(str(row.get("source") or "").strip())
        if m:
            out.append((m.group(2), m.group(1)))
        hit = domain_from_ats_url(str(row.get("url") or ""))
        if hit:
            out.append(hit)
    return out


def _slug_to_domain(slug: str, *, allow_network: bool) -> str | None:
    """An MX-having domain built from an employer slug, or None."""
    base = re.sub(r"[^a-z0-9-]", "", (slug or "").lower())
    if len(base) < 2:
        return None
    if not allow_network:
        return f"{base}.com"
    for tld in _TLDS:
        if has_mx(base + tld):
            return base + tld
    return None


# ---------------------------------------------------------------------------
# the resolver
# ---------------------------------------------------------------------------

def _blank(reason: str) -> dict:
    return {"domain": None, "origin": None, "authoritative": False,
            "is_careers_domain": False, "candidates": [], "reason": reason}


def resolve_domain(job, *, session=None, allow_network: bool = True) -> dict:
    """Which domain does this job's employer own?

    Returns {domain, origin, authoritative, is_careers_domain, candidates, reason}.
    `candidates` lists every layer that produced something, in trust order, so a
    caller that rejects the winner can try the next instead of giving up.

    Never raises: a domain lookup failing must degrade address discovery, not
    break the pipeline that calls it.
    """
    company = getattr(job, "company", "") or ""
    candidates: list[dict] = []

    def add(domain: str | None, origin: str, why: str) -> None:
        if not domain:
            return
        host = domain.lower().strip().rstrip(".")
        if not host or is_board_or_ats(host):
            return
        if any(c["domain"] == host for c in candidates):
            return
        candidates.append({"domain": host, "origin": origin, "why": why})

    try:
        add(_from_companies_table(company, session), "companies_table",
            "Company.careers_url for this employer")
        add(load_target_domains().get(normalize_name(company)), "target_yaml",
            "curated in config/target_companies.yaml")
        add(_corporate_host(getattr(job, "url", "") or ""), "posting_host",
            "the employer served the posting from this host")

        for slug, ats in _slugs_from_extra_urls(getattr(job, "extra_urls", None)):
            add(_slug_to_domain(slug, allow_network=allow_network), "ats_slug",
                f"employer slug {slug!r} on {ats}")
        hit = domain_from_ats_url(getattr(job, "url", "") or "")
        if hit:
            add(_slug_to_domain(hit[0], allow_network=allow_network), "ats_slug",
                f"employer slug {hit[0]!r} on {hit[1]}")

        for row in (getattr(job, "extra_urls", None) or []):
            if isinstance(row, dict):
                add(_corporate_host(str(row.get("url") or "")), "extra_urls",
                    "a non-board URL recorded for this job")

        name_key = re.sub(r"[^a-z0-9]", "", normalize_name(company))
        if len(name_key) >= 2:
            if allow_network:
                for tld in _TLDS:
                    if has_mx(name_key + tld):
                        add(name_key + tld, "name_guess",
                            "company name plus a TLD that has MX")
                        break
            else:
                add(name_key + ".com", "name_guess", "company name plus .com")
    except Exception as e:                       # pragma: no cover - defensive
        logger.debug(f"[company_domain] resolve failed for {company!r}: {e}")

    if not candidates:
        return _blank("no domain candidate from any source")

    candidates.sort(key=lambda c: ORIGINS.index(c["origin"])
                    if c["origin"] in ORIGINS else len(ORIGINS))
    best = candidates[0]
    return {
        "domain": best["domain"],
        "origin": best["origin"],
        "authoritative": best["origin"] in AUTHORITATIVE_ORIGINS,
        "is_careers_domain": is_careers_domain(best["domain"]),
        "candidates": candidates,
        "reason": "",
    }
