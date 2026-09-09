"""Scanner that fans out across the configured job providers.

Each provider is queried independently and its failures are contained, so an
unkeyed or down source never blocks the others. Results land in the same
RawJob -> dedup -> DB path every other scanner uses, so the rest of the
pipeline (ranker, tailor, agent) needs no changes.
"""

import logging

from agents.scanner.base import BaseScanner, RawJob
from agents.scanner.providers import enabled_providers

logger = logging.getLogger(__name__)


class AggregatorScanner(BaseScanner):
    """Runs every enabled provider across the configured keywords."""

    source_name = "aggregators"
    rate_limit_seconds = 1.0

    def __init__(self, config: dict):
        super().__init__(config)
        self.js_config = (config or {}).get("job_sources", {}) or {}

    def _should_skip_location(self, location: str) -> bool:
        """Bypass the US-only deny-list when international sourcing is on.

        The base gate exists to avoid paying for LLM calls on reqs the original
        (US-based) author couldn't take. These providers are here precisely FOR
        non-US coverage, so applying it would throw away the point of them.
        """
        if self.js_config.get("international", True):
            return False
        return super()._should_skip_location(location)

    def scan(self) -> list[RawJob]:
        providers = enabled_providers(self.config)
        if not providers:
            logger.info("[aggregators] no providers enabled in settings.yaml job_sources")
            return []

        queries = self.js_config.get("queries") or self.keywords or ["software engineer"]
        location = self.js_config.get("location", "")
        per_query = int(self.js_config.get("results_per_query", 50))

        seen: set[str] = set()
        jobs: list[RawJob] = []
        for p in providers:
            missing = p.missing_keys()
            if missing:
                logger.info(f"[aggregators] {p.name}: skipped — needs {', '.join(missing)}")
                continue
            found = 0
            for q in queries:
                try:
                    for rj in p.search(q, location, per_query):
                        # cheap in-run dedup; the DB hash still guards across runs
                        key = (rj.url or "").split("?")[0].lower()
                        if key in seen:
                            continue
                        seen.add(key)
                        jobs.append(rj)
                        found += 1
                except Exception as e:
                    logger.warning(f"[aggregators] {p.name} failed on '{q}': {e}")
            logger.info(f"[aggregators] {p.name}: {found} jobs")
        logger.info(f"[aggregators] {len(jobs)} jobs from {len(providers)} provider(s)")
        return jobs
