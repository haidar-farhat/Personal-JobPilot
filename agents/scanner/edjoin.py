"""EdJoin scanner — Bay Area school-district Behavioral Technician roles.

EdJoin (edjoin.org) is California's K-12 job board and the source for the
SFUSD-style BT/paraprofessional roles. Results are JS-rendered, so we drive a
headless Chromium via Playwright (already a project dependency), then filter the
statewide results down to the Bay Area.

Card DOM (verified 2026-06-29):
    div.job-contain
      a.card-job-title[href="/Home/JobPosting/{id}"]  -> title
      innerText lines: "{District} - {City}, {County}, CA", "Deadline: ...",
                       "$min - $max  Per Hour"

Defensive by design: any failure (Playwright missing, navigation timeout, DOM
change, anti-bot) logs and returns whatever was gathered (often []), never
raising into the scheduler sweep.
"""

import logging
from urllib.parse import quote

from agents.scanner.base import BaseScanner, RawJob

logger = logging.getLogger(__name__)

EDJOIN_BASE = "https://www.edjoin.org"

# EdJoin keyword searches to run (statewide; filtered to Bay Area below).
SEARCH_TERMS = (
    "behavior technician",
    "registered behavior technician",
    "behavior interventionist",
)

# Bay Area cities + counties used to keep only local roles.
BAY_AREA_TERMS = (
    "san francisco", "oakland", "berkeley", "san jose", "bay area",
    "alameda county", "san mateo", "contra costa", "marin county",
    "santa clara county", "daly city", "hayward", "fremont", "richmond",
    "san rafael", "redwood city", "south san francisco", "san leandro",
    "emeryville", "union city", "newark", "milpitas", "burlingame",
    "san bruno", "pacifica", "novato", "walnut creek", "concord",
)

# Title must look like a BT/ABA role (EdJoin search already scoped it, so this is
# just a light sanity gate — NOT the general scanner relevance filter).
_BT_TITLE_TERMS = ("behavior", "interventionist", "rbt", "aba", "paraprofessional")


class EdJoinScanner(BaseScanner):
    """Scan EdJoin for Bay Area Behavioral Technician roles via Playwright."""

    source_name = "edjoin"
    rate_limit_seconds = 2.0

    def scan(self) -> list[RawJob]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:  # pragma: no cover - environment dependent
            logger.warning(f"[edjoin] Playwright unavailable, skipping: {e}")
            return []

        jobs: list[RawJob] = []
        seen: set[str] = set()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    for term in SEARCH_TERMS:
                        jobs.extend(self._scan_term(browser, term, seen))
                finally:
                    browser.close()
        except Exception as e:
            logger.error(f"[edjoin] scan failed: {e}")
        logger.info(f"[edjoin] {len(jobs)} Bay Area BT roles")
        return jobs

    def _scan_term(self, browser, term: str, seen: set) -> list[RawJob]:
        url = f"{EDJOIN_BASE}/Home/Jobs?keywords={quote(term)}"
        page = browser.new_page()
        out: list[RawJob] = []
        try:
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            try:
                page.wait_for_selector("div.job-contain", timeout=15000)
            except Exception:
                logger.info(f"[edjoin] no results rendered for '{term}'")
                return out
            page.wait_for_timeout(1500)
            cards = page.eval_on_selector_all(
                "div.job-contain",
                """els => els.map(e => {
                    const a = e.querySelector('a.card-job-title');
                    return {
                        title: a ? (a.innerText||'').trim() : '',
                        href: a ? a.getAttribute('href') : '',
                        text: (e.innerText||'').trim()
                    };
                })""",
            )
        except Exception as e:
            logger.warning(f"[edjoin] term '{term}' failed: {e}")
            return out
        finally:
            page.close()

        for card in cards:
            raw = self._card_to_rawjob(card)
            if raw and raw.url not in seen:
                seen.add(raw.url)
                out.append(raw)
        return out

    @staticmethod
    def _card_to_rawjob(card: dict) -> RawJob | None:
        """Pure parser: EdJoin card dict -> RawJob, Bay-Area-filtered. None if not a fit."""
        title = (card.get("title") or "").strip()
        href = (card.get("href") or "").strip()
        text = (card.get("text") or "").strip()
        if not title or not href:
            return None

        title_l = title.lower()
        if not any(t in title_l for t in _BT_TITLE_TERMS):
            return None

        # District + location from the "District - City, County, CA" line.
        district, location = "", ""
        for line in (l.strip() for l in text.splitlines() if l.strip()):
            if " - " in line and (", ca" in line.lower() or "county" in line.lower()):
                district, _, location = line.partition(" - ")
                district, location = district.strip(), location.strip()
                break

        hay = f"{location} {text}".lower()
        if not any(t in hay for t in BAY_AREA_TERMS):
            return None

        url_abs = href if href.startswith("http") else EDJOIN_BASE + href

        # First line that looks like pay (drives hourly parsing in _store_job).
        salary_text = ""
        for line in (l.strip() for l in text.splitlines() if l.strip()):
            ll = line.lower()
            if "$" in line or "per hour" in ll or "hourly" in ll:
                salary_text = line
                break

        return RawJob(
            title=title,
            company=district or "School District (EdJoin)",
            location=location or "Bay Area, CA",
            url=url_abs,
            source="edjoin",
            source_id=href.rstrip("/").rsplit("/", 1)[-1],
            description=text,
            salary_text=salary_text,
        )
