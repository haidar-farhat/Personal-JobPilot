"""Find a company's application email when the posting doesn't publish one.

Resolution order, most trustworthy first — the first hit wins:

  1. crawl  — an address the employer PUBLISHED on its own site (/careers,
              /contact, ...). Provenance (the source URL) is recorded.
  2. guess  — a constructed role address (careers@domain) on a domain that has
              MX records. Opt-in, capped, and never used for an address a
              previous send bounced from.

Why no search API: Google's Custom Search JSON API is closed to new customers
and shuts down 2027-01-01, and Brave's free tier ended in Feb 2026. Testing a
search for a real company's recruiting address returned nothing usable, because
ATS-using companies deliberately publish no application inbox. A search
dependency would cost money and yield ~nothing, so the company's own site is
the only source worth crawling.

A guessed address is a genuine trade-off: MX proves the DOMAIN accepts mail, it
does not prove the MAILBOX exists, so some will bounce. utils/bounce_watch.py
closes that loop by learning dead addresses from Gmail and never reusing them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import requests

from utils.mailer import PREFERRED_LOCAL, is_safe_recipient

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
_CACHE_DIR = ROOT / "output" / "company_email"

# Same fetch manners as the job providers (agents/scanner/providers.py).
_UA = "JobPilot/1.0 (personal job-search agent)"
_TIMEOUT = 10
_MAX_PAGES = 5

# Hosts that are an applicant-tracking system, not the employer's own domain.
_ATS_HOSTS = ("greenhouse.io", "ashbyhq.com", "lever.co", "myworkdayjobs.com",
              "smartrecruiters.com", "workable.com", "workday.com", "icims.com",
              "jobvite.com", "bamboohr.com", "breezy.hr", "recruitee.com",
              "teamtailor.com", "rippling.com", "paylocity.com", "adp.com",
              "careerpuck.com", "eightfold.ai", "phenompeople.com", "avature.net",
              "successfactors.com", "taleo.net", "oraclecloud.com", "gem.com")
# Subdomains a careers site hangs off — strip to reach the corporate domain
# (careers.airbnb.com -> airbnb.com), which is where mail actually lands.
_CAREER_SUBS = ("careers", "career", "jobs", "job", "apply", "work", "boards",
                "job-boards", "hire", "hiring", "talent", "recruiting")
_TLDS = (".com", ".io", ".ai", ".co", ".tech", ".dev")
_PATHS = ("", "/careers", "/jobs", "/contact", "/about")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_MAILTO_RE = re.compile(r"mailto:([^\"'?>\s]+)", re.I)

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}
_MX_CACHE: dict[str, bool] = {}   # process-level memo: MX rarely changes mid-run


def _key_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


# ---------------------------------------------------------------------------
# cache (same contract as agents/interview_prep.py: atomic write, never cache
# a failure, unreadable file counts as a miss)
# ---------------------------------------------------------------------------

def _cache_path(domain: str) -> Path:
    safe = re.sub(r"[^a-z0-9.\-]", "_", (domain or "").lower())
    return _CACHE_DIR / f"{safe}.json"


def _read_cache(domain: str) -> dict | None:
    try:
        data = json.loads(_cache_path(domain).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) and "address" in data else None


def _write_cache(domain: str, rec: dict) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_CACHE_DIR), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, indent=2))
        os.replace(tmp, _cache_path(domain))
    except OSError as e:
        logger.warning(f"[company_email] could not cache {domain}: {e}")


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------

def has_mx(domain: str) -> bool:
    """True when the domain publishes MX records (i.e. can receive mail).

    Says nothing about whether a particular mailbox exists — see module docstring.
    """
    if not domain:
        return False
    cached = _MX_CACHE.get(domain)
    if cached is not None:
        return cached

    result = None
    try:
        import dns.resolver
        resolver = dns.resolver.Resolver()
        resolver.lifetime = resolver.timeout = 5
        result = bool(resolver.resolve(domain, "MX"))
    except ImportError:
        pass                       # not installed — nslookup below
    except Exception as e:
        # A definitive "no such domain / no MX record" is an answer. A timeout
        # or config problem is NOT: this machine's resolver times out under
        # dnspython while nslookup answers fine, and treating that as "no MX"
        # silently disabled the whole lookup.
        name = type(e).__name__
        if name in ("NXDOMAIN", "NoAnswer", "NoNameservers"):
            result = False

    if result is None:             # fall through to the system resolver
        try:
            import subprocess
            out = subprocess.run(["nslookup", "-type=MX", domain], capture_output=True,
                                 text=True, timeout=8).stdout.lower()
            result = "mail exchanger" in out
        except Exception:
            result = False

    _MX_CACHE[domain] = result
    return result


# ---------------------------------------------------------------------------
# domain derivation
# ---------------------------------------------------------------------------

def _ats_slug(url: str) -> str:
    """Company slug out of an ATS URL: .../doximity/jobs/123 -> doximity."""
    skip = {"jobs", "job", "careers", "career", "embed", "board", "boards",
            "job-board", "job-boards", "jobboard", "apply", "en", "us", "search",
            "openings", "positions", "vacancies", "listing", "listings"}
    for p in [p for p in urlparse(url or "").path.split("/") if p]:
        low = p.lower()
        if low in skip or low.isdigit():
            continue
        return p
    return ""


def company_domain(job) -> str | None:
    """The employer's own domain, or None.

    Job URLs mostly point at an ATS (job-boards.greenhouse.io/...), so the host
    is only usable when it isn't one; otherwise the slug or company name is
    tried against common TLDs and confirmed with an MX lookup.
    """
    return (company_domain_with_origin(job) or (None, None))[0]


def company_domain_with_origin(job) -> tuple[str, str] | None:
    """(domain, origin) where origin is "url" (from the posting's own host, so
    authoritative) or "derived" (constructed from a slug/name, so unproven)."""
    host = (urlparse(getattr(job, "url", "") or "").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    # careers.airbnb.com -> airbnb.com; the careers subdomain rarely takes mail.
    first, _, rest = host.partition(".")
    if first in _CAREER_SUBS and rest.count(".") >= 1:
        host = rest
    # A dedicated careers *domain* (pinterestcareers.com, instacart.careers) is
    # not where an application inbox lives either — fall through to the slug.
    looks_careers = any(t in host for t in ("career", "jobs", "recruit", "hiring"))
    if host and not looks_careers and not any(a in host for a in _ATS_HOSTS):
        return host, "url"   # authoritative: the employer's own posting URL

    candidates = []
    slug = _ats_slug(getattr(job, "url", "") or "")
    if slug:
        candidates.append(re.sub(r"[^a-z0-9\-]", "", slug.lower()))
    name = (getattr(job, "company", "") or "").lower()
    name = re.sub(r"[^a-z0-9]", "", name)
    if name and name not in candidates:
        candidates.append(name)

    for base in candidates:
        if len(base) < 2:
            continue
        for tld in _TLDS:
            if has_mx(base + tld):
                return base + tld, "derived"
    return None


# ---------------------------------------------------------------------------
# crawl + guess
# ---------------------------------------------------------------------------

def domain_belongs_to(domain: str, company: str) -> bool:
    """Does this domain actually belong to that company?

    A domain built from a slug or a company name is a GUESS: "Scale AI" gave
    scaleai.co and a Lyft board URL gave job-board.com, both real sites owned by
    other people. Mailing a CV to a stranger is worse than sending nothing, so a
    constructed domain must prove itself by naming the company on its homepage
    before it may be mailed.
    """
    tokens = [t for t in re.split(r"[^a-z0-9]+", (company or "").lower()) if len(t) > 2]
    if not tokens or not domain:
        return False
    try:
        r = _session().get(f"https://{domain}", timeout=_TIMEOUT, allow_redirects=True)
        if r.status_code != 200:
            return False
        blob = (r.text or "")[:60000].lower()
    except requests.RequestException:
        return False
    return all(t in blob for t in tokens[:2])


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _UA})
    return s


def _addresses_on(text: str) -> list[str]:
    found = []
    for raw in _MAILTO_RE.findall(text or "") + _EMAIL_RE.findall(text or ""):
        a = raw.strip().lower().rstrip(".,;:)\"'")
        if a and a not in found:
            found.append(a)
    return found


def crawl_for_email(domain: str) -> tuple[str, str] | None:
    """An address published on the company's own site, with its source URL.

    Same-domain addresses only, screened by is_safe_recipient() so
    accommodation / compliance / no-reply inboxes are never returned, and
    ranked so a recruiting inbox beats an incidental personal address.
    """
    if not domain:
        return None
    sess = _session()
    best: tuple[str, str] | None = None   # stays None: only recruiting inboxes qualify
    for path in _PATHS[:_MAX_PAGES]:
        url = f"https://{domain}{path}"
        try:
            r = sess.get(url, timeout=_TIMEOUT, allow_redirects=True)
            if r.status_code != 200 or not r.text:
                continue
        except requests.RequestException:
            continue
        for addr in _addresses_on(r.text):
            if not addr.endswith("@" + domain) and f".{domain}" not in addr.split("@")[-1]:
                continue           # someone else's address on their page
            if not is_safe_recipient(addr):
                continue
            local = addr.split("@", 1)[0]
            if any(p in local for p in PREFERRED_LOCAL):
                return addr, url   # an obvious recruiting inbox — stop here
    # Anything else on a company site is some other department: doximity.com
    # publishes bd@ (business development), which should not receive a CV.
    # Precision matters more than recall here — return nothing instead.
    return best


def guess_email(domain: str, locals_: tuple[str, ...] | list[str] | None = None,
                dead: set[str] | None = None) -> str | None:
    """A constructed role address on an MX-valid domain, or None.

    Returns ONE address — never a barrage to several guesses at one company.
    Known-dead addresses (learned from bounces) are skipped.
    """
    if not domain or not has_mx(domain):
        return None
    dead = dead or set()
    for local in (locals_ or ("careers", "jobs", "recruiting", "talent", "hr")):
        addr = f"{local}@{domain}"
        if addr in dead or not is_safe_recipient(addr):
            continue
        return addr
    return None


def find_company_email(job, *, allow_crawl: bool = True, allow_guess: bool = False,
                       guess_locals=None, dead: set[str] | None = None,
                       refresh: bool = False) -> dict:
    """Resolve an application address for this job's employer.

    Returns {address, source, domain, source_url}; source is "crawl", "guess"
    or None. Results are cached per domain — applying to six roles at one
    company must not crawl its site six times.
    """
    blank = {"address": None, "source": None, "domain": None, "source_url": None}
    found = company_domain_with_origin(job)
    if not found:
        return blank
    domain, origin = found

    with _key_lock(domain):
        if not refresh:
            cached = _read_cache(domain)
            if cached is not None:
                # A cached guess can go stale when a bounce marks it dead.
                if cached.get("source") == "guess" and cached.get("address") in (dead or set()):
                    cached = None
                if cached is not None:
                    cached["cached"] = True
                    return cached

        rec = {"address": None, "source": None, "domain": domain,
               "source_url": None, "domain_origin": origin}
        if allow_crawl:
            hit = crawl_for_email(domain)
            if hit:
                rec.update(address=hit[0], source="crawl", source_url=hit[1])

        if not rec["address"] and allow_guess:
            # A domain we constructed ourselves must first prove it belongs to
            # this employer — otherwise a near-miss like scaleai.co sends the
            # CV to a stranger. A domain taken from the posting URL is already
            # authoritative and skips the check.
            owned = origin == "url" or domain_belongs_to(domain, getattr(job, "company", ""))
            if not owned:
                logger.info(f"[company_email] {domain} not confirmed for "
                            f"'{getattr(job, 'company', '')}' — not guessing")
            else:
                guessed = guess_email(domain, guess_locals, dead)
                if guessed:
                    rec.update(address=guessed, source="guess")

        # A crawl result is durable; a miss might just be a transient network
        # failure, so only cache an actual finding.
        if rec["address"]:
            _write_cache(domain, rec)
        return rec


def cache_size() -> int:
    try:
        return len(list(_CACHE_DIR.glob("*.json")))
    except OSError:
        return 0
