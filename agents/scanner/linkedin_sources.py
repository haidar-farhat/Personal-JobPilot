"""Free, no-API-key LinkedIn job sources.

WHY THIS EXISTS
---------------
LinkedIn has no public job-search API available to an individual (its Job
Posting API is a restricted partner programme), and the paid access layers
originally specced for this slot — Unipile and Bright Data — were dropped: both
bill per record and one of them drives the user's OWN authenticated LinkedIn
identity, which is an account-restriction risk that a personal job search should
not take. What is left is the *unauthenticated guest* surface LinkedIn serves to
logged-out browsers, which is what this module reads.

WHAT THAT REALLY MEANS — READ BEFORE ENABLING
---------------------------------------------
LinkedIn's User Agreement prohibits automated access, scraping and crawling.
These are undocumented internal endpoints with no stability guarantee: the class
names below are the markup LinkedIn happened to serve on 2026-09-09, and they
change without notice. LinkedIn actively blocks volume — an HTTP **999** is its
"you look automated" response, and 429/403 follow sustained traffic. So this
module is scoped as narrowly as it can be and still be useful:

  * PUBLIC postings only. No login, no cookie, no ``li_at`` session, no member
    or profile data — only what LinkedIn serves to an anonymous browser.
  * Personal use, low volume: a per-run request cap and a >= 2s delay between
    requests, both configurable upward only in the sense that lowering the delay
    below the floor is ignored.
  * It degrades, it does not fight. A 999 aborts the whole run immediately and
    latches ``ready=False`` on health(); 429/503 gets exactly one backed-off
    retry and then gives up for the cycle. Every failure path returns ``[]``.
  * DELIBERATELY OUT OF SCOPE: proxy rotation, IP or fingerprint evasion,
    CAPTCHA solving, login automation, and headless-browser impersonation. If
    LinkedIn blocks this machine, the correct response is to stop, not to hide.
  * The honest ``JobPilot/1.0`` User-Agent inherited from JobProvider is kept.
    Measured 2026-09-09: the guest endpoint returned HTTP 200 for that UA, for
    ``python-requests/2.32``, and for a Chrome UA alike — spoofing a browser buys
    nothing here, so we do not.

MEASURED ENDPOINT BEHAVIOUR (2026-09-09, from the user's machine)
------------------------------------------------------------------
  ``GET /jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=&location=&start=``
    200, ``text/html``, an ``<li>``-per-card fragment (no wrapper document),
    **10 cards per page**; ``start`` pages in steps of 10. A ``start`` past the
    end of the result set returns **400 with an empty body** — that is the
    end-of-results signal, not an error, and it is treated as such.
  ``GET /jobs-guest/jobs/api/jobPosting/{job_id}``
    200, a ~50 KB detail fragment carrying ``div.show-more-less-html__markup``
    (the description) and ``ul.description__job-criteria-list`` (seniority,
    employment type). This is a sixth the size of the public ``/jobs/view/``
    page, so it is the one used. It is still ONE REQUEST PER JOB, which is why
    it is behind the opt-in ``fetch_descriptions`` flag: a 50-result run with
    descriptions on is 55 requests and ~2.5 minutes of deliberate delay.

The providers here are NOT registered in providers.PROVIDERS by this module.
They are exported as ``LINKEDIN_PROVIDERS`` for the caller to merge, so that
providers.py stays a single-owner file.
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from agents.scanner.base import RawJob
from agents.scanner.providers import JobProvider, _clean_html, _iso, _is_remote

logger = logging.getLogger(__name__)

_GUEST_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
_GUEST_POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

_PAGE_SIZE = 10          # measured: the guest endpoint always returns 10 cards
_MIN_DELAY = 2.0         # hard floor between requests; config can raise, not lower
_TIMEOUT = 30
_BLOCK_STATUS = 999      # LinkedIn's "automated traffic" response

_URN_ID = re.compile(r"urn:li:jobPosting:(\d+)")


def _pick(node, *selectors: str) -> str:
    """First CSS selector that matches and yields non-empty text.

    LinkedIn has shipped at least two generations of card markup
    (``base-search-card__*`` today, ``job-result-card__*`` / ``result-card__*``
    before it) and A/B-tests new ones, so no single selector is safe to index
    directly. Every field is read through this with fallbacks and tolerates
    coming back empty.
    """
    for sel in selectors:
        try:
            found = node.select_one(sel)
        except Exception:                      # malformed selector on odd markup
            continue
        if found is None:
            continue
        text = found.get_text(" ", strip=True)
        if text:
            return re.sub(r"\s{2,}", " ", text)
    return ""


def _num(value) -> float | None:
    """Coerce to float, rejecting None and NaN.

    JobSpy hands back a pandas frame; its empty cells are float ``nan``, which
    is falsy-safe but compares unequal to itself and would otherwise be written
    straight into ``Job.salary_min``.
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _canonical_job_url(href: str, job_id: str) -> str:
    """The shareable posting URL, stripped of per-impression tracking.

    Card hrefs carry ``?position=&pageNum=&refId=&trackingId=`` which are unique
    per response — leaving them on would defeat the pipeline's URL-keyed dedup
    (aggregators.py keys on ``url.split("?")[0]``) and would leak this run's
    tracking ids into anything that later displays the link. The path itself
    (``/jobs/view/<slug>-<id>``) is the canonical, attributable form.
    """
    href = (href or "").strip()
    if href:
        parts = urlsplit(href)
        if parts.scheme and parts.netloc:
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return f"https://www.linkedin.com/jobs/view/{job_id}" if job_id else ""


class LinkedInPublicProvider(JobProvider):
    """LinkedIn's unauthenticated guest job-search fragment.

    No key, no login. Config (``job_sources.linkedin_public`` in settings.yaml):

      ``delay_seconds``       seconds between requests, floored at 2.0 (default 2.5)
      ``max_requests``        per-run HTTP cap including detail fetches (default 12)
      ``fetch_descriptions``  follow each card to its posting for the body text
                              — one extra request per job, off by default
      ``f_TPR``/``f_WT``/``f_E``/``f_JT``
                              LinkedIn's own guest filter params, passed through
                              verbatim (e.g. ``f_TPR=r604800`` for "past week").
                              Undocumented; a wrong value silently returns 0 rows.
    """

    name = "linkedin_public"
    label = "LinkedIn (public guest search)"
    attribution = "Public LinkedIn postings — canonical linkedin.com/jobs/view links preserved"

    # Filter params LinkedIn's own guest UI puts on this endpoint. Passed through
    # untouched because their value grammar is undocumented and version-specific.
    _PASSTHROUGH = ("f_TPR", "f_WT", "f_E", "f_JT", "f_SB2", "geoId")

    def __init__(self, config: dict):
        super().__init__(config)
        self.delay_seconds = max(_MIN_DELAY, float(self.config.get("delay_seconds", 2.5)))
        self.max_requests = max(1, int(self.config.get("max_requests", 12)))
        self.fetch_descriptions = bool(self.config.get("fetch_descriptions", False))
        self._requests_made = 0
        self._blocked = False
        self._block_reason = ""

    # -- health ----------------------------------------------------------
    def health(self) -> dict:
        """Same contract as JobProvider.health(), plus the standing caveat.

        ``ready`` is True on a fresh instance (there is nothing to configure),
        but the reason line always states that the source is unofficial, and it
        flips to False for the rest of the process once LinkedIn has blocked us
        — the dashboard needs to show *why* the source went quiet.
        """
        h = super().health()
        if self._blocked:
            h["ready"] = False
            h["reason"] = self._block_reason
        else:
            h["reason"] = "unofficial guest endpoint — may return 999/429 without notice"
        return h

    # -- transport -------------------------------------------------------
    def _pause(self) -> None:
        time.sleep(self.delay_seconds)

    def _budget_left(self) -> bool:
        if self._requests_made >= self.max_requests:
            if not self._block_reason:
                self._block_reason = f"per-run request cap reached ({self.max_requests})"
            logger.info(f"[{self.name}] {self._block_reason}")
            return False
        return True

    def _abort(self, reason: str) -> None:
        self._blocked = True
        self._block_reason = reason
        logger.warning(f"[{self.name}] {reason}")

    def _fetch(self, url: str, params: dict | None = None) -> str | None:
        """One rate-limited GET returning HTML text, or None on any refusal.

        Retry policy is deliberately meagre: exactly one backed-off retry for
        429/503 (transient throttling), and none at all for 999 — retrying a
        block is how a soft block becomes a hard one.
        """
        if self._blocked or not self._budget_left():
            return None

        for attempt in (0, 1):
            if self._requests_made:
                self._pause()
            self._requests_made += 1
            try:
                r = self.session.get(url, params=params, timeout=_TIMEOUT)
            except Exception as e:
                logger.warning(f"[{self.name}] request failed: {e}")
                return None

            status = getattr(r, "status_code", 0)
            if status == 200:
                return r.text or ""
            if status == _BLOCK_STATUS:
                self._abort("LinkedIn returned 999 (automated-traffic block) — "
                            "aborting this run, do not retry")
                return None
            if status == 400:
                # Measured: `start` past the end of the result set. End of data.
                logger.debug(f"[{self.name}] HTTP 400 — treating as end of results")
                return None
            if status in (429, 503) and attempt == 0:
                backoff = self.delay_seconds * 4
                logger.warning(f"[{self.name}] HTTP {status} — one retry in {backoff:.0f}s")
                time.sleep(backoff)
                if not self._budget_left():
                    return None
                continue
            if status in (429, 503):
                self._abort(f"HTTP {status} after retry — throttled, stopping this cycle")
                return None
            if status == 403:
                self._abort("HTTP 403 — guest access refused from this IP")
                return None
            logger.warning(f"[{self.name}] HTTP {status}")
            return None
        return None

    # -- parsing ---------------------------------------------------------
    def _parse_cards(self, html: str) -> list[RawJob]:
        """Turn one guest fragment into RawJobs.

        The fragment is a bare ``<li>`` sequence with no document wrapper, which
        lxml recovers from fine. Cards missing a title or a resolvable URL are
        dropped: RawJob requires both and the pipeline cannot dedup without a URL.
        """
        if not html or not html.strip():
            return []
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("div.base-card, div.base-search-card, li div.job-search-card, "
                            "div.job-result-card")
        out: list[RawJob] = []
        for card in cards:
            urn = card.get("data-entity-urn") or ""
            m = _URN_ID.search(urn if isinstance(urn, str) else "")
            job_id = m.group(1) if m else ""

            # Selector order matters and a comma-group would not honour it: the
            # first `a[href]` in document order is the card link today, but the
            # company link in the subtitle is a sibling one nested block away.
            href = ""
            for sel in ("a.base-card__full-link", "a.result-card__full-card-link", "a[href]"):
                link = card.select_one(sel)
                if link is not None and link.get("href"):
                    href = str(link["href"])
                    break
            url = _canonical_job_url(href, job_id)

            title = _pick(card,
                          "h3.base-search-card__title",
                          "h3.job-result-card__title",
                          "span.sr-only")
            if not title or not url:
                continue

            location = _pick(card,
                             "span.job-search-card__location",
                             "span.job-result-card__location")
            company = _pick(card,
                            "h4.base-search-card__subtitle a",
                            "h4.base-search-card__subtitle",
                            "a.result-card__subtitle-link",
                            "h4.result-card__subtitle")

            posted = None
            time_el = card.select_one("time[datetime]")
            if time_el is not None:
                posted = _iso(time_el.get("datetime"))

            out.append(RawJob(
                title=title,
                company=company,
                location=location,
                url=url,
                source=self.name,
                description="",
                salary_text=_pick(card, "span.job-search-card__salary-info",
                                  "span.job-result-card__salary-info"),
                source_id=job_id,
                date_posted=posted,
                is_remote=_is_remote(title, location),
            ))
        return out

    def _fetch_detail(self, job: RawJob) -> None:
        """Fill description + seniority in place from the guest posting fragment.

        Mutates rather than returns because a failed detail fetch must leave a
        usable RawJob behind — a posting with no body is still worth ranking on
        its title, and the alternative (dropping it) would silently shrink runs
        every time LinkedIn throttled the detail endpoint.
        """
        if not job.source_id:
            return
        html = self._fetch(_GUEST_POSTING.format(job_id=job.source_id))
        if not html:
            return
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception as e:
            logger.warning(f"[{self.name}] detail parse failed for {job.source_id}: {e}")
            return

        body = soup.select_one("div.show-more-less-html__markup, div.description__text")
        if body is not None:
            job.description = _clean_html(body.decode_contents())

        for item in soup.select("li.description__job-criteria-item"):
            head = _pick(item, "h3.description__job-criteria-subheader").lower()
            value = _pick(item, "span.description__job-criteria-text")
            if value and "seniority" in head:
                job.seniority_level = value
                break

        if job.description and not job.is_remote:
            job.is_remote = _is_remote(job.title, job.location, job.description[:400])

    # -- search ----------------------------------------------------------
    def search(self, query: str, location: str, limit: int) -> list[RawJob]:
        if self.missing_keys():          # belt-and-braces; this provider needs none
            return []

        params_base = {"keywords": query or "", "location": location or ""}
        for k in self._PASSTHROUGH:
            if self.config.get(k) not in (None, ""):
                params_base[k] = self.config[k]

        jobs: list[RawJob] = []
        seen: set[str] = set()
        start = 0
        while len(jobs) < limit and not self._blocked:
            html = self._fetch(_GUEST_SEARCH, params={**params_base, "start": start})
            if html is None:
                break
            page = self._parse_cards(html)
            if not page:
                break
            for rj in page:
                key = rj.url.lower()
                if key in seen:
                    continue
                seen.add(key)
                jobs.append(rj)
                if len(jobs) >= limit:
                    break
            # No short-page heuristic: a page can legitimately yield fewer than
            # _PAGE_SIZE RawJobs because malformed cards are dropped. Termination
            # comes from the measured end-of-data signals instead — a 400 (start
            # past the end) or an empty fragment both land in the breaks above —
            # plus `limit` and the per-run request cap.
            start += _PAGE_SIZE

        if self.fetch_descriptions:
            for rj in jobs:
                if self._blocked:
                    break
                self._fetch_detail(rj)

        logger.info(f"[{self.name}] {len(jobs)} jobs in {self._requests_made} request(s)")
        return jobs


class JobSpyProvider(JobProvider):
    """Optional adapter for the ``python-jobspy`` scraper package.

    JobSpy wraps LinkedIn, Indeed, Glassdoor, ZipRecruiter and Google Jobs
    behind one call and keeps its selectors current, which is worth far more
    than the hand-rolled parser above when it is installed. It is NOT a
    dependency of this project: it is imported inside search() so that a missing
    package costs a caught ImportError and an unready health row, never an
    import-time crash of the whole scanner package.

    It also pulls in pandas, which is deliberately absent from this venv. So the
    result is duck-typed rather than treated as a DataFrame, and every numeric
    cell goes through _num() to strip pandas' float NaNs before they reach the DB.

    Config (``job_sources.jobspy``): ``sites`` (default LinkedIn + Indeed),
    ``hours_old``, ``country_indeed``, ``fetch_descriptions``, ``proxies`` is
    NOT read — see the module docstring on scope.
    """

    name = "jobspy"
    label = "JobSpy (LinkedIn/Indeed/Glassdoor)"
    attribution = "via python-jobspy — canonical source links preserved"

    _INSTALL_HINT = "pip install python-jobspy"

    @staticmethod
    def _load():
        """Import ``scrape_jobs`` lazily. Returns None when unavailable."""
        try:
            from jobspy import scrape_jobs
        except Exception as e:                 # ImportError, or a broken install
            logger.info(f"[jobspy] unavailable: {e}")
            return None
        return scrape_jobs

    def health(self) -> dict:
        h = super().health()
        if self._load() is None:
            h["ready"] = False
            h["reason"] = self._INSTALL_HINT
        return h

    def search(self, query: str, location: str, limit: int) -> list[RawJob]:
        scrape_jobs = self._load()
        if scrape_jobs is None:
            logger.info(f"[{self.name}] skipped — {self._INSTALL_HINT}")
            return []

        sites = self.config.get("sites") or ["linkedin", "indeed"]
        kwargs = {
            "site_name": list(sites),
            "search_term": query or "",
            "location": location or "",
            "results_wanted": max(1, int(limit)),
            "linkedin_fetch_description": bool(self.config.get("fetch_descriptions", False)),
        }
        if self.config.get("hours_old"):
            kwargs["hours_old"] = int(self.config["hours_old"])
        if self.config.get("country_indeed"):
            kwargs["country_indeed"] = self.config["country_indeed"]

        try:
            result = scrape_jobs(**kwargs)
        except Exception as e:
            logger.warning(f"[{self.name}] scrape failed: {e}")
            return []

        rows = self._rows(result)
        out: list[RawJob] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            url = str(row.get("job_url") or row.get("job_url_direct") or "").strip()
            if not title or not url:
                continue
            site = str(row.get("site") or "").strip().lower()
            out.append(RawJob(
                title=title,
                company=str(row.get("company") or "").strip(),
                location=str(row.get("location") or "").strip(),
                url=url,
                source=f"jobspy_{site}" if site else self.name,
                description=_clean_html(row.get("description")),
                salary_min=_num(row.get("min_amount")),
                salary_max=_num(row.get("max_amount")),
                salary_text=str(row.get("interval") or "").strip(),
                source_id=str(row.get("id") or ""),
                date_posted=_iso(row.get("date_posted")),
                is_remote=bool(row.get("is_remote")) or _is_remote(title, str(row.get("location") or "")),
                seniority_level=str(row.get("job_level") or "").strip(),
            ))
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _rows(result) -> list:
        """Normalise JobSpy's return to a list of dicts without importing pandas.

        Current versions return a DataFrame; older ones and the test doubles
        return a plain list. ``to_dict("records")`` is the DataFrame path and is
        detected by duck-typing so pandas never has to be importable here.
        """
        if result is None:
            return []
        if isinstance(result, list):
            return result
        to_dict = getattr(result, "to_dict", None)
        if callable(to_dict):
            try:
                return list(to_dict("records"))
            except Exception as e:
                logger.warning(f"[jobspy] could not normalise result: {e}")
                return []
        try:
            return list(result)
        except TypeError:
            return []


LINKEDIN_PROVIDERS: dict[str, type[JobProvider]] = {
    p.name: p for p in (LinkedInPublicProvider, JobSpyProvider)
}
