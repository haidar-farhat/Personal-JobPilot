"""Company-site scanners — Workday, SmartRecruiters, Workable, and custom pages.

JobRight-style direct ingestion from company career sites, extending the
Greenhouse/Lever/Ashby ATS-API coverage. Deliberately excludes Indeed and
LinkedIn (ToS risk). Every source here is either a public JSON API that the
company's own careers frontend uses, or the company's own public careers page.

Config lives in config/target_companies.yaml:

    - name: "NVIDIA"
      ats_platform: "workday"
      workday:
        host: "nvidia.wd5.myworkdayjobs.com"
        tenant: "nvidia"
        site: "NVIDIAExternalCareerSite"

    - name: "Visa"
      ats_platform: "smartrecruiters"
      sr_company_id: "Visa"

    - name: "Hugging Face"
      ats_platform: "workable"
      workable_account: "huggingface"

    - name: "Lawrence Berkeley National Laboratory"
      ats_platform: "custom"
      career_url: "https://jobs.lbl.gov"

Defensive by design: any failure logs and returns what was gathered — never
raising into the scheduler sweep.
"""

import logging
import re
from pathlib import Path
from urllib.parse import urljoin

import yaml
from bs4 import BeautifulSoup

from agents.scanner.base import BaseScanner, RawJob

logger = logging.getLogger(__name__)


def _load_companies(platform: str, required_key: str | None = None) -> list[dict]:
    """Load target companies for one ats_platform from target_companies.yaml."""
    config_path = Path(__file__).parent.parent.parent / "config" / "target_companies.yaml"
    if not config_path.exists():
        return []
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out = []
    for c in data.get("companies", []):
        if c.get("ats_platform") != platform:
            continue
        if required_key and not c.get(required_key):
            continue
        out.append(c)
    return out


def _html_to_text(html: str, limit: int = 8000) -> str:
    if not html:
        return ""
    try:
        return BeautifulSoup(html, "lxml").get_text(separator="\n", strip=True)[:limit]
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)[:limit]


# ============================================================
# Workday — public CXS JSON API used by every myworkdayjobs.com site
# ============================================================

class WorkdayScanner(BaseScanner):
    """Scan Workday-hosted career sites via the public CXS JSON API."""

    source_name = "workday"
    rate_limit_seconds = 1.5
    PAGES = 3               # 3 x 20 newest postings per company per sweep
    MAX_DETAILS = 12        # cap detail fetches per company per sweep

    def __init__(self, config: dict):
        super().__init__(config)
        self.companies = _load_companies("workday", required_key="workday")

    def scan(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for company in self.companies:
            try:
                found = self._scan_company(company)
                jobs.extend(found)
                logger.info(f"[workday] {company['name']}: found {len(found)} relevant jobs")
            except Exception as e:
                logger.error(f"[workday] Error scanning {company['name']}: {e}")
        return jobs

    def _scan_company(self, company: dict) -> list[RawJob]:
        wd = company["workday"]
        host, tenant, site = wd["host"], wd["tenant"], wd["site"]
        base = f"https://{host}/wday/cxs/{tenant}/{site}"

        postings = []
        for page in range(self.PAGES):
            self._rate_limit()
            try:
                r = self.session.post(
                    f"{base}/jobs",
                    json={"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": ""},
                    timeout=30,
                )
                r.raise_for_status()
                chunk = r.json().get("jobPostings", [])
            except Exception as e:
                logger.warning(f"[workday] {company['name']} page {page} failed: {e}")
                break
            if not chunk:
                break
            postings.extend(chunk)

        jobs = []
        details_fetched = 0
        for p in postings:
            title = (p.get("title") or "").strip()
            path = p.get("externalPath") or ""
            if not title or not path or not self._title_is_relevant(title):
                continue
            location = (p.get("locationsText") or "").strip()

            description = ""
            salary_text = ""
            if details_fetched < self.MAX_DETAILS:
                detail = self._fetch_detail(base, path)
                if detail:
                    description = detail["description"]
                    location = detail.get("location") or location
                    salary_text = detail.get("salary_text", "")
                    details_fetched += 1

            jobs.append(RawJob(
                title=title,
                company=company["name"],
                location=location or company.get("location", ""),
                url=f"https://{host}/en-US/{site}{path}",
                source=self.source_name,
                source_id=path,
                description=description,
                salary_text=salary_text,
                is_remote="remote" in (title + " " + location).lower(),
            ))
        return jobs

    def _fetch_detail(self, base: str, path: str) -> dict | None:
        self._rate_limit()
        try:
            r = self.session.get(f"{base}{path}", timeout=30)
            r.raise_for_status()
            info = r.json().get("jobPostingInfo", {}) or {}
            return {
                "description": _html_to_text(info.get("jobDescription", "")),
                "location": (info.get("location") or "").strip(),
                "salary_text": (info.get("compensation") or "") if isinstance(info.get("compensation"), str) else "",
            }
        except Exception as e:
            logger.debug(f"[workday] detail fetch failed for {path}: {e}")
            return None


# ============================================================
# SmartRecruiters — public postings API (api.smartrecruiters.com)
# ============================================================

class SmartRecruitersScanner(BaseScanner):
    """Scan SmartRecruiters companies via their public postings API."""

    source_name = "smartrecruiters"
    rate_limit_seconds = 1.5
    MAX_DETAILS = 12

    def __init__(self, config: dict):
        super().__init__(config)
        self.companies = _load_companies("smartrecruiters", required_key="sr_company_id")

    def scan(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for company in self.companies:
            try:
                found = self._scan_company(company)
                jobs.extend(found)
                logger.info(f"[smartrecruiters] {company['name']}: found {len(found)} relevant jobs")
            except Exception as e:
                logger.error(f"[smartrecruiters] Error scanning {company['name']}: {e}")
        return jobs

    def _scan_company(self, company: dict) -> list[RawJob]:
        cid = company["sr_company_id"]
        resp = self._safe_request(
            f"https://api.smartrecruiters.com/v1/companies/{cid}/postings?limit=100"
        )
        if not resp:
            return []
        content = resp.json().get("content", [])

        jobs = []
        details_fetched = 0
        for p in content:
            title = (p.get("name") or "").strip()
            if not title or not self._title_is_relevant(title):
                continue
            loc = p.get("location") or {}
            location = ", ".join(x for x in (loc.get("city"), loc.get("region")) if x)
            posting_id = str(p.get("id") or "")

            description = ""
            apply_url = ""
            if details_fetched < self.MAX_DETAILS and posting_id:
                detail = self._fetch_detail(cid, posting_id)
                if detail:
                    description = detail["description"]
                    apply_url = detail.get("apply_url", "")
                    details_fetched += 1

            jobs.append(RawJob(
                title=title,
                company=company["name"],
                location=location or company.get("location", ""),
                url=apply_url or f"https://jobs.smartrecruiters.com/{cid}/{posting_id}",
                source=self.source_name,
                source_id=posting_id,
                description=description,
                is_remote=bool(loc.get("remote")) or "remote" in location.lower(),
            ))
        return jobs

    def _fetch_detail(self, cid: str, posting_id: str) -> dict | None:
        resp = self._safe_request(
            f"https://api.smartrecruiters.com/v1/companies/{cid}/postings/{posting_id}"
        )
        if not resp:
            return None
        try:
            data = resp.json()
            sections = (data.get("jobAd") or {}).get("sections") or {}
            parts = []
            for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
                sec = sections.get(key) or {}
                if sec.get("text"):
                    parts.append(_html_to_text(sec["text"], limit=4000))
            return {
                "description": "\n\n".join(parts)[:8000],
                "apply_url": data.get("applyUrl", ""),
            }
        except Exception as e:
            logger.debug(f"[smartrecruiters] detail parse failed for {posting_id}: {e}")
            return None


# ============================================================
# Workable — public widget API (apply.workable.com)
# ============================================================

class WorkableScanner(BaseScanner):
    """Scan Workable-hosted career sites via the public widget API."""

    source_name = "workable"
    rate_limit_seconds = 1.5

    def __init__(self, config: dict):
        super().__init__(config)
        self.companies = _load_companies("workable", required_key="workable_account")

    def scan(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for company in self.companies:
            try:
                found = self._scan_company(company)
                jobs.extend(found)
                logger.info(f"[workable] {company['name']}: found {len(found)} relevant jobs")
            except Exception as e:
                logger.error(f"[workable] Error scanning {company['name']}: {e}")
        return jobs

    def _scan_company(self, company: dict) -> list[RawJob]:
        slug = company["workable_account"]
        resp = self._safe_request(
            f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
        )
        if not resp:
            return []
        data = resp.json()
        jobs = []
        for p in data.get("jobs", []):
            title = (p.get("title") or "").strip()
            if not title or not self._title_is_relevant(title):
                continue
            city = (p.get("city") or "").strip()
            country = (p.get("country") or "").strip()
            location = ", ".join(x for x in (city, country) if x)
            jobs.append(RawJob(
                title=title,
                company=company["name"],
                location=location or company.get("location", ""),
                url=p.get("url") or p.get("application_url") or "",
                source=self.source_name,
                source_id=str(p.get("shortcode") or ""),
                description=_html_to_text(p.get("description", "")),
                is_remote=bool(p.get("telecommuting")) or "remote" in location.lower(),
            ))
        return jobs


# ============================================================
# Generic careers pages — Playwright, heuristic link harvesting
# ============================================================

# In-browser extraction for job detail pages. document.body.innerText drags in
# cookie-consent overlays, screen-reader helpers, and site navigation, so:
# remove that chrome from the DOM, then prefer an explicit main-content
# landmark, then fall back to descending into whichever element holds the bulk
# of the page text (the largest content block).
_PAGE_TEXT_JS = """() => {
    const KILL = [
        'nav', 'header', 'footer', 'aside', 'script', 'style', 'noscript', 'iframe',
        '[role="navigation"]', '[role="banner"]', '[role="contentinfo"]',
        '[role="dialog"]', '[role="alertdialog"]', '[aria-hidden="true"]',
        '#onetrust-consent-sdk', '#CybotCookiebotDialog', '#usercentrics-root',
        '.cky-consent-container', '.cc-window', '#cookie-law-info-bar',
        '[id*="cookie-banner" i]', '[class*="cookie-banner" i]',
        '[id*="cookie-consent" i]', '[class*="cookie-consent" i]',
    ];
    for (const sel of KILL) {
        try { document.querySelectorAll(sel).forEach(el => el.remove()); } catch (e) {}
    }
    const textLen = el => (el && el.innerText) ? el.innerText.trim().length : 0;
    let best = null;
    for (const sel of ['main', '[role="main"]', 'article', '#main-content', '#content', '.main-content']) {
        const el = document.querySelector(sel);
        if (textLen(el) > 200) { best = el; break; }
    }
    if (!best && document.body) {
        best = document.body;
        for (;;) {
            const next = [...best.children].find(c => textLen(c) >= textLen(best) * 0.7);
            if (next && textLen(next) > 200) best = next; else break;
        }
    }
    return textLen(best) ? best.innerText : (document.body ? document.body.innerText : '');
}"""

# Lines containing cookie-consent language, wherever they appear.
_COOKIE_PHRASE_RE = re.compile(
    r"(?i)\b(?:we value your privacy|we use cookies|use of cookies"
    r"|consent to (?:the use of )?cookies|accept all cookies"
    r"|cookie (?:policy|settings|preferences|notice|consent)"
    r"|do not sell my personal information)\b"
)

# Whole lines that are consent controls, accessibility helpers, or footer
# legalese — the short button/link labels innerText leaves behind.
_BOILERPLATE_LINE_RE = re.compile(
    r"(?i)^\s*(?:"
    r"accept(?: all)?|reject(?: all)?|decline(?: all)?|customi[sz]e|got it"
    r"|manage (?:preferences|settings|cookies)"
    r"|skip to (?:main )?content|skip to (?:footer|navigation|search)"
    r"|(?:press|use) .*screen[ -]?reader.*|accessibility screen[ -]?reader.*"
    r"|.*\|\s*new window"
    r"|privacy policy|terms of (?:use|service)|sitemap|back to (?:top|jobs)"
    r"|(?:©|copyright\b).*"
    r")\s*$"
)


def _looks_like_prose(line: str) -> bool:
    return len(line) >= 60 or len(line.split()) >= 8


def _strip_leading_nav(lines: list[str]) -> list[str]:
    """Drop the run of short link labels (menus, breadcrumbs, phone numbers)
    before the first real paragraph. Stops at anything prose-like or
    money-bearing so a lone comp line at the top survives; pages with no
    prose at all are left untouched."""
    for i, ln in enumerate(lines):
        if _looks_like_prose(ln) or "$" in ln:
            return lines[i:]
    return lines


def _clean_page_text(text: str, limit: int = 8000) -> str:
    """Line-level boilerplate cleanup for harvested page text."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    lines = [
        ln for ln in lines
        if not _COOKIE_PHRASE_RE.search(ln) and not _BOILERPLATE_LINE_RE.match(ln)
    ]
    return "\n".join(_strip_leading_nav(lines))[:limit]


def _extract_page_text(page) -> str:
    """Job-description text for the page currently loaded in Playwright."""
    try:
        raw = page.evaluate(_PAGE_TEXT_JS) or ""
    except Exception as e:
        logger.debug(f"[custom] main-content extraction failed, using body text: {e}")
        try:
            raw = page.evaluate("document.body ? document.body.innerText : ''") or ""
        except Exception:
            return ""
    return _clean_page_text(raw)


class GenericCareersScanner(BaseScanner):
    """Scan custom company careers pages (no known ATS API) via Playwright.

    Heuristic: load the careers page, harvest anchors whose text looks like a
    relevant job title, then visit up to MAX_DETAILS of them for description
    text. Best-effort by design — a layout change degrades to zero results,
    never an exception.
    """

    source_name = "custom"
    rate_limit_seconds = 2.5
    MAX_DETAILS = 8

    def __init__(self, config: dict):
        super().__init__(config)
        self.companies = _load_companies("custom", required_key="career_url")

    def scan(self) -> list[RawJob]:
        if not self.companies:
            return []
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:  # pragma: no cover — environment dependent
            logger.warning(f"[custom] Playwright unavailable, skipping: {e}")
            return []

        jobs: list[RawJob] = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                for company in self.companies:
                    try:
                        found = self._scan_company(page, company)
                        jobs.extend(found)
                        logger.info(f"[custom] {company['name']}: found {len(found)} relevant jobs")
                    except Exception as e:
                        logger.error(f"[custom] Error scanning {company['name']}: {e}")
                browser.close()
        except Exception as e:
            logger.error(f"[custom] Playwright session failed: {e}")
        return jobs

    def _scan_company(self, page, company: dict) -> list[RawJob]:
        url = company["career_url"]
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)

        anchors = page.evaluate(
            """() => [...document.querySelectorAll('a[href]')]
                 .map(a => ({text: (a.innerText || '').trim(), href: a.href}))
                 .filter(a => a.text && a.text.length > 4 && a.text.length < 120)"""
        )

        seen_hrefs = set()
        candidates = []
        for a in anchors:
            text, href = a["text"], a["href"]
            if href in seen_hrefs or not href.startswith("http"):
                continue
            # first line of the anchor text is usually the title
            title = text.split("\n")[0].strip()
            if not self._title_is_relevant(title) or self._should_skip_title(title):
                continue
            # job-detail-ish URLs only — skip nav/category links
            if not re.search(r"(job|position|opening|posting|requisition|req[_-]?\d|/r/|careers?/)", href, re.I):
                continue
            seen_hrefs.add(href)
            candidates.append((title, urljoin(url, href)))

        jobs = []
        for title, job_url in candidates[: self.MAX_DETAILS]:
            description = ""
            location = company.get("location", "")
            try:
                page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
                body = _extract_page_text(page)
                description = body[:8000]
                m = re.search(r"(?:location|based in)[:\s]+([^\n]{3,60})", body, re.I)
                if m:
                    location = m.group(1).strip()
            except Exception as e:
                logger.debug(f"[custom] detail fetch failed for {job_url}: {e}")
            jobs.append(RawJob(
                title=title,
                company=company["name"],
                location=location,
                url=job_url,
                source=self.source_name,
                source_id=job_url,
                description=description,
                is_remote="remote" in (title + " " + description[:400]).lower(),
            ))
        return jobs
