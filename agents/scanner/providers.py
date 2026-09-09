"""Provider-based job aggregation layer.

LinkedIn has no public job-search API for this use case (its Job Posting API is
a restricted partner programme), so job discovery is spread across a set of
independent providers instead. Each one:

  * carries its own credentials + config
  * has request timeouts and a per-call cap
  * normalises results into RawJob (the pipeline's existing schema)
  * preserves the canonical source URL for attribution
  * reports health
  * FAILS INDEPENDENTLY — one provider being down or unkeyed never stops the rest

Keys come from environment variables so nothing secret lands in settings.yaml
(which is git-tracked). A provider with no key reports "needs key" and is
skipped rather than erroring.

Greenhouse and Lever are deliberately absent: this project already scans them
directly (agents/scanner/career_pages.py) against config/target_companies.yaml.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

import requests

from agents.scanner.base import RawJob

logger = logging.getLogger(__name__)

_TIMEOUT = 25
_UA = "JobPilot/1.0 (personal job-search agent)"


def _clean_html(text: str | None) -> str:
    """Strip tags from provider descriptions — the ranker wants plain text."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _iso(value) -> datetime | None:
    """Parse the assorted date shapes providers return; None when unusable."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    s = str(value).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _is_remote(*fields: str) -> bool:
    blob = " ".join(f or "" for f in fields).lower()
    return "remote" in blob or "anywhere" in blob or "worldwide" in blob


class JobProvider:
    """One job source. Subclasses implement search()."""

    name = "provider"
    label = "Provider"
    requires_key: tuple[str, ...] = ()      # env var names
    attribution: str | None = None          # shown in the UI when required

    def __init__(self, config: dict):
        self.config = config or {}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _UA})

    # -- credentials -----------------------------------------------------
    def key(self, env_name: str) -> str:
        return (os.environ.get(env_name) or "").strip()

    def missing_keys(self) -> list[str]:
        return [k for k in self.requires_key if not self.key(k)]

    def health(self) -> dict:
        missing = self.missing_keys()
        return {
            "name": self.name, "label": self.label, "ready": not missing,
            "missing_keys": missing, "attribution": self.attribution,
            "reason": ("set " + ", ".join(missing)) if missing else "",
        }

    # -- fetching --------------------------------------------------------
    def search(self, query: str, location: str, limit: int) -> list[RawJob]:
        raise NotImplementedError

    def _get(self, url: str, **kw) -> dict | list | None:
        try:
            r = self.session.get(url, timeout=_TIMEOUT, **kw)
            if r.status_code != 200:
                logger.warning(f"[{self.name}] HTTP {r.status_code}")
                return None
            return r.json()
        except Exception as e:
            logger.warning(f"[{self.name}] request failed: {e}")
            return None


class JobicyProvider(JobProvider):
    """Remote jobs, no key. Attribution + canonical URL required by their terms."""

    name = "jobicy"
    label = "Jobicy (remote)"
    attribution = "Jobs by Jobicy — canonical links preserved"

    def search(self, query: str, location: str, limit: int) -> list[RawJob]:
        params = {"count": min(limit, 200)}
        if query:
            params["tag"] = query
        data = self._get("https://jobicy.com/api/v2/remote-jobs", params=params)
        items = (data or {}).get("jobs", []) if isinstance(data, dict) else []
        out = []
        for j in items:
            title = j.get("jobTitle") or j.get("title") or ""
            url = j.get("url") or ""
            if not title or not url:
                continue
            out.append(RawJob(
                title=title,
                company=j.get("companyName") or "",
                location=j.get("jobGeo") or "Remote",
                url=url,
                source="jobicy",
                description=_clean_html(j.get("jobExcerpt") or j.get("jobDescription")),
                source_id=str(j.get("id") or ""),
                date_posted=_iso(j.get("pubDate")),
                is_remote=True,
                salary_min=j.get("annualSalaryMin") or None,
                salary_max=j.get("annualSalaryMax") or None,
            ))
        return out


class RemotiveProvider(JobProvider):
    """Remote jobs, no key. NOTE: their terms delay public API data 24h and
    forbid re-posting listings to third-party job platforms. Read-only use in a
    personal agent is fine; do not republish."""

    name = "remotive"
    label = "Remotive (remote, 24h delayed)"
    attribution = "Jobs by Remotive — do not republish to other job platforms"

    def search(self, query: str, location: str, limit: int) -> list[RawJob]:
        params = {"limit": min(limit, 100)}
        if query:
            params["search"] = query
        data = self._get("https://remotive.com/api/remote-jobs", params=params)
        out = []
        for j in (data or {}).get("jobs", []) if isinstance(data, dict) else []:
            if not j.get("title") or not j.get("url"):
                continue
            out.append(RawJob(
                title=j["title"],
                company=j.get("company_name") or "",
                location=j.get("candidate_required_location") or "Remote",
                url=j["url"],
                source="remotive",
                description=_clean_html(j.get("description")),
                salary_text=j.get("salary") or "",
                source_id=str(j.get("id") or ""),
                date_posted=_iso(j.get("publication_date")),
                is_remote=True,
            ))
        return out


class ArbeitnowProvider(JobProvider):
    """Europe-heavy board, no key. Good non-US coverage."""

    name = "arbeitnow"
    label = "Arbeitnow (Europe)"

    def search(self, query: str, location: str, limit: int) -> list[RawJob]:
        data = self._get("https://www.arbeitnow.com/api/job-board-api")
        q = (query or "").lower()
        out = []
        for j in (data or {}).get("data", []) if isinstance(data, dict) else []:
            title = j.get("title") or ""
            if q and q not in title.lower() and q not in (j.get("description") or "").lower():
                continue
            if not j.get("url"):
                continue
            out.append(RawJob(
                title=title,
                company=j.get("company_name") or "",
                location=j.get("location") or "",
                url=j["url"],
                source="arbeitnow",
                description=_clean_html(j.get("description")),
                source_id=str(j.get("slug") or ""),
                date_posted=_iso(j.get("created_at")),
                is_remote=bool(j.get("remote")),
            ))
            if len(out) >= limit:
                break
        return out


class JSearchProvider(JobProvider):
    """RapidAPI aggregator spanning LinkedIn/Indeed/Glassdoor/ZipRecruiter and
    other public sources — the broadest single feed. Needs a RapidAPI key.

    Use per the provider's current terms: an aggregator's right to expose
    source data is its own, and does not make the underlying boards' data
    unrestricted.
    """

    name = "jsearch"
    label = "JSearch (aggregator)"
    requires_key = ("JOBPILOT_RAPIDAPI_KEY",)

    def search(self, query: str, location: str, limit: int) -> list[RawJob]:
        if self.missing_keys():
            return []
        q = f"{query} in {location}" if location else query
        data = self._get(
            "https://jsearch.p.rapidapi.com/search",
            params={"query": q or "software engineer", "num_pages": 1},
            headers={"X-RapidAPI-Key": self.key("JOBPILOT_RAPIDAPI_KEY"),
                     "X-RapidAPI-Host": "jsearch.p.rapidapi.com"},
        )
        out = []
        for j in (data or {}).get("data", []) if isinstance(data, dict) else []:
            url = j.get("job_apply_link") or j.get("job_google_link")
            if not j.get("job_title") or not url:
                continue
            city, country = j.get("job_city"), j.get("job_country")
            out.append(RawJob(
                title=j["job_title"],
                company=j.get("employer_name") or "",
                location=", ".join(x for x in (city, country) if x) or "",
                url=url,
                source="jsearch",
                description=_clean_html(j.get("job_description")),
                source_id=str(j.get("job_id") or ""),
                date_posted=_iso(j.get("job_posted_at_timestamp")),
                is_remote=bool(j.get("job_is_remote")),
                salary_min=j.get("job_min_salary"),
                salary_max=j.get("job_max_salary"),
            ))
            if len(out) >= limit:
                break
        return out


class AdzunaProvider(JobProvider):
    """International aggregator (20+ countries), structured salary data.
    Free tier is rate-limited (25/min, 250/day), so keep call counts low."""

    name = "adzuna"
    label = "Adzuna (international)"
    requires_key = ("JOBPILOT_ADZUNA_APP_ID", "JOBPILOT_ADZUNA_APP_KEY")

    def search(self, query: str, location: str, limit: int) -> list[RawJob]:
        if self.missing_keys():
            return []
        country = (self.config.get("country") or "gb").lower()
        params = {
            "app_id": self.key("JOBPILOT_ADZUNA_APP_ID"),
            "app_key": self.key("JOBPILOT_ADZUNA_APP_KEY"),
            "results_per_page": min(limit, 50),
            "content-type": "application/json",
        }
        if query:
            params["what"] = query
        if location:
            params["where"] = location
        data = self._get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", params=params)
        out = []
        for j in (data or {}).get("results", []) if isinstance(data, dict) else []:
            if not j.get("title") or not j.get("redirect_url"):
                continue
            loc = (j.get("location") or {}).get("display_name") or ""
            out.append(RawJob(
                title=j["title"],
                company=(j.get("company") or {}).get("display_name") or "",
                location=loc,
                url=j["redirect_url"],
                source="adzuna",
                description=_clean_html(j.get("description")),
                source_id=str(j.get("id") or ""),
                date_posted=_iso(j.get("created")),
                is_remote=_is_remote(j.get("title"), loc),
                salary_min=j.get("salary_min"),
                salary_max=j.get("salary_max"),
            ))
        return out


class JoobleProvider(JobProvider):
    """International aggregator. Each regional domain needs its own key and the
    free tier is capped at 500 lifetime requests — secondary source only."""

    name = "jooble"
    label = "Jooble (international)"
    requires_key = ("JOBPILOT_JOOBLE_KEY",)

    def search(self, query: str, location: str, limit: int) -> list[RawJob]:
        if self.missing_keys():
            return []
        try:
            r = self.session.post(
                f"https://jooble.org/api/{self.key('JOBPILOT_JOOBLE_KEY')}",
                json={"keywords": query or "", "location": location or ""},
                timeout=_TIMEOUT,
            )
            data = r.json() if r.status_code == 200 else {}
        except Exception as e:
            logger.warning(f"[jooble] request failed: {e}")
            return []
        out = []
        for j in (data or {}).get("jobs", []):
            if not j.get("title") or not j.get("link"):
                continue
            out.append(RawJob(
                title=j["title"],
                company=j.get("company") or "",
                location=j.get("location") or "",
                url=j["link"],
                source="jooble",
                description=_clean_html(j.get("snippet")),
                salary_text=j.get("salary") or "",
                source_id=str(j.get("id") or ""),
                date_posted=_iso(j.get("updated")),
                is_remote=_is_remote(j.get("title"), j.get("location")),
            ))
            if len(out) >= limit:
                break
        return out


class USAJobsProvider(JobProvider):
    """US federal postings. Needs an email + API key registered with USAJOBS."""

    name = "usajobs"
    label = "USAJOBS (US federal)"
    requires_key = ("JOBPILOT_USAJOBS_EMAIL", "JOBPILOT_USAJOBS_KEY")

    def search(self, query: str, location: str, limit: int) -> list[RawJob]:
        if self.missing_keys():
            return []
        params = {"ResultsPerPage": min(limit, 500)}
        if query:
            params["Keyword"] = query
        if location:
            params["LocationName"] = location
        data = self._get(
            "https://data.usajobs.gov/api/search",
            params=params,
            headers={"Host": "data.usajobs.gov",
                     "User-Agent": self.key("JOBPILOT_USAJOBS_EMAIL"),
                     "Authorization-Key": self.key("JOBPILOT_USAJOBS_KEY")},
        )
        items = ((data or {}).get("SearchResult", {}) or {}).get("SearchResultItems", [])
        out = []
        for row in items:
            d = row.get("MatchedObjectDescriptor", {}) or {}
            if not d.get("PositionTitle") or not d.get("PositionURI"):
                continue
            pay = (d.get("PositionRemuneration") or [{}])[0]
            out.append(RawJob(
                title=d["PositionTitle"],
                company=d.get("OrganizationName") or "",
                location=(d.get("PositionLocationDisplay") or ""),
                url=d["PositionURI"],
                source="usajobs",
                description=_clean_html((d.get("UserArea", {}).get("Details", {}) or {}).get("JobSummary")
                                        or d.get("QualificationSummary")),
                source_id=str(d.get("PositionID") or ""),
                date_posted=_iso(d.get("PublicationStartDate")),
                is_remote=_is_remote(d.get("PositionLocationDisplay")),
                salary_min=float(pay["MinimumRange"]) if pay.get("MinimumRange") else None,
                salary_max=float(pay["MaximumRange"]) if pay.get("MaximumRange") else None,
            ))
        return out


PROVIDERS: dict[str, type[JobProvider]] = {
    p.name: p for p in (
        JSearchProvider, AdzunaProvider, JoobleProvider, JobicyProvider,
        ArbeitnowProvider, RemotiveProvider, USAJobsProvider,
    )
}


# Free, no-key LinkedIn sources live in their own module (they scrape public
# pages rather than calling a paid API, and carry their own rate limiting and
# block detection). Merged so enabled_providers/provider_health treat them
# exactly like every other provider.
#
# Registered LAZILY, not at import time: linkedin_sources imports JobProvider
# from THIS module, so an import-time merge here is a cycle. It happens to work
# when providers is imported first and fails with "partially initialized
# module" when linkedin_sources is — i.e. silent, order-dependent breakage.
_LINKEDIN_MERGED = False


def _ensure_linkedin_providers() -> None:
    """Merge the LinkedIn providers into PROVIDERS exactly once."""
    global _LINKEDIN_MERGED
    if _LINKEDIN_MERGED:
        return
    _LINKEDIN_MERGED = True
    try:
        from agents.scanner.linkedin_sources import LINKEDIN_PROVIDERS
        PROVIDERS.update(LINKEDIN_PROVIDERS)
    except Exception as e:          # pragma: no cover - defensive
        logger.warning(f"[providers] LinkedIn sources unavailable: {e}")


def enabled_providers(config: dict) -> list[JobProvider]:
    """Instantiate the providers switched on in settings.yaml `job_sources`."""
    _ensure_linkedin_providers()
    cfg = (config or {}).get("job_sources", {}) or {}
    out = []
    for name, cls in PROVIDERS.items():
        pcfg = cfg.get(name) or {}
        if pcfg.get("enabled"):
            out.append(cls(pcfg))
    return out


def provider_health(config: dict) -> list[dict]:
    """Status of every known provider, for the dashboard's source panel."""
    _ensure_linkedin_providers()
    cfg = (config or {}).get("job_sources", {}) or {}
    rows = []
    for name, cls in PROVIDERS.items():
        pcfg = cfg.get(name) or {}
        h = cls(pcfg).health()
        h["enabled"] = bool(pcfg.get("enabled"))
        rows.append(h)
    return rows
