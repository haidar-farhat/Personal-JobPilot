"""Auto-applier orchestrator.

Pulls candidate applications from the DB (status=MATERIALS_READY, score >= threshold,
ATS in allowlist), dispatches each one to the correct ATS-specific filler, and
records the outcome.

Public entry point:
    run_auto_apply(config: dict) -> dict

Returns a summary dict with counts: {submitted, dry_run, skipped_score, skipped_ats,
skipped_files, skipped_captcha, failed, total_attempted, daily_cap_hit}
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

from agents.auto_applier.base import ApplyResult, load_profile
from agents.auto_applier.mapper_engine import MapperApplier
from agents.auto_applier.workday import WorkdayAutoApplier
from db.database import get_session, record_status_change
from db.models import (Application, ApplicationStatus, Job, JobScore,
                       AUTO_APPLY_PERMANENT_FAILURES, SYSTEM_FAULTS)

logger = logging.getLogger(__name__)

# Live progress for the dashboard's Agent screen. Module-level so BOTH the
# scheduler's cycles and dashboard-triggered runs report through one place.
# Written only by run_auto_apply (one cycle at a time), read by the API.
_PROGRESS: dict = {"active": False, "done": 0, "total": 0, "current": None,
                   "started_at": None, "finished_at": None}


_LOCK_PATH = Path(__file__).resolve().parents[2] / "output" / "apply.lock"
_LOCK_STALE_SECONDS = 30 * 60


def apply_lock_holder() -> dict | None:
    """Who is currently running an apply cycle, or None.

    The dashboard and the scheduler are separate processes, so an in-memory
    flag cannot coordinate them — this is a lockfile. A stale lock (crashed
    run) expires rather than wedging the scheduler forever.
    """
    try:
        rec = json.loads(_LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    started = rec.get("started_at") or 0
    if time.time() - float(started) > _LOCK_STALE_SECONDS:
        return None                      # stale — treat as free
    return rec


def _acquire_apply_lock(owner: str) -> bool:
    if apply_lock_holder():
        return False
    try:
        _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LOCK_PATH.write_text(json.dumps(
            {"owner": owner, "pid": os.getpid(), "started_at": time.time()}),
            encoding="utf-8")
        return True
    except OSError:
        return True                      # never block applying over a lock problem


def _release_apply_lock() -> None:
    try:
        _LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _resolve_recipient(job, page_text: str, mail_cfg: dict,
                       own: str = "") -> tuple[str | None, str]:
    """(address, where_it_came_from). Each stage runs at most once — the old
    code re-ran the lookup just to label its own log line, which would mean a
    second network fetch now that later stages hit the web."""
    from utils.mailer import find_employer_recipient

    own = (own or "").strip().lower()

    def usable(addr):
        """Our OWN address is not an employer. The page is read after the bot
        has filled the form, so the applicant's email is sitting right there in
        it — without this guard the agent mails itself and calls it an
        application."""
        return addr and addr.strip().lower() != own

    hit = find_employer_recipient(getattr(job, "description", ""))
    if usable(hit):
        return hit, "description"
    hit = find_employer_recipient(page_text)
    if usable(hit):
        return hit, "live page"

    if not (mail_cfg.get("lookup_website") or mail_cfg.get("guess_addresses")):
        return None, ""
    try:
        from utils.bounce_watch import load_dead
        from utils.company_email import find_company_email
        rec = find_company_email(
            job,
            allow_crawl=bool(mail_cfg.get("lookup_website", True)),
            allow_guess=bool(mail_cfg.get("guess_addresses", False)),
            guess_locals=mail_cfg.get("guess_locals"),
            dead=load_dead(),
        )
    except Exception as e:
        logger.warning(f"[auto_apply] company-email lookup failed: {e}")
        return None, ""

    if not usable(rec.get("address")):
        return None, ""
    if rec.get("source") == "guess" and not _guess_quota_left(mail_cfg):
        logger.info("[auto_apply] daily guessed-address cap reached — not emailing")
        return None, ""
    where = "company site" if rec["source"] == "crawl" else "constructed address"
    if rec.get("source_url"):
        where += f" ({rec['source_url']})"
    return rec["address"], where


def _guess_quota_left(mail_cfg: dict) -> bool:
    """Cap on UNVERIFIED sends only. A constructed address can bounce, and
    repeatedly mailing dead boxes is what gets a sender flagged; addresses the
    employer actually published are trusted and uncapped."""
    cap = int(mail_cfg.get("max_guessed_per_day", 20) or 0)
    if cap <= 0:
        return False
    today = datetime.now(timezone.utc).date().isoformat()
    if _GUESS_SENT.get("day") != today:
        _GUESS_SENT.update(day=today, count=0)
    if _GUESS_SENT["count"] >= cap:
        return False
    _GUESS_SENT["count"] += 1
    return True


_GUESS_SENT: dict = {"day": None, "count": 0}


def _email_application(profile: dict, config: dict | None, job, app,
                       page_text: str = "") -> None:
    """Email the resume + cover letter for an application just submitted.

    Controlled by settings.yaml `mail:`. Recipient resolution, most trustworthy
    first — the first hit wins:

      1. the stored job description
      2. the live page as rendered (descriptions are often empty or truncated)
      3. the company website (an address the employer published itself)
      4. a constructed role address, if mail.guess_addresses is on

    Every candidate is screened by is_safe_recipient(), so accommodation,
    compliance and no-reply inboxes are never used at any stage.
    """
    mail_cfg = ((config or {}).get("mail") or {})
    if not mail_cfg.get("enabled"):
        # Say WHY. This used to return in silence, so a caller that forgot to
        # pass config looked identical to "mail is switched off" — and every
        # dashboard-started run applied without emailing for hours unnoticed.
        logger.info("[auto_apply] no email: %s",
                    "config not passed to run_auto_apply" if not config
                    else "mail.enabled is false")
        return

    from utils.mailer import find_employer_recipient, send_application_email

    identity = profile.get("identity", {}) or {}
    self_copy = True
    to_addr = (mail_cfg.get("to") or "").strip()

    # Email the employer when THEIR OWN posting publishes an application
    # address (screened: accommodation/compliance inboxes are never used).
    if not to_addr and mail_cfg.get("to_employer"):
        found, src = _resolve_recipient(job, page_text, mail_cfg,
                                        own=identity.get("email", ""))
        if found:
            to_addr, self_copy = found, False
            logger.info(f"[auto_apply] application address found in {src} -> {found}")

    if to_addr and to_addr != identity.get("email", ""):
        self_copy = False

    if not to_addr:
        # No address published by this employer. Mailing yourself is not an
        # application, so by default send nothing — the ATS form already
        # carried the résumé. Opt in with mail.self_copy for an archive.
        if not mail_cfg.get("self_copy"):
            return
        to_addr, self_copy = identity.get("email", ""), True
    if not to_addr:
        return

    res = send_application_email(
        to_addr=to_addr,
        job_title=job.title or "",
        company=job.company or "",
        resume_path=app.resume_path,
        cover_letter_path=app.cover_letter_path,
        sender_name=identity.get("full_name") or "",
        links=profile.get("links", {}),
        self_copy=self_copy,
    )
    if res.get("sent"):
        logger.info(f"[auto_apply] emailed package for '{job.title}' -> {res['to']}")
    else:
        logger.warning(f"[auto_apply] package email not sent: {res.get('error')}")


def get_progress() -> dict:
    """Snapshot of the current (or last) auto-apply cycle."""
    return dict(_PROGRESS)


def _progress_start(total: int) -> None:
    _PROGRESS.update(active=True, done=0, total=total, current=None,
                     started_at=datetime.now(timezone.utc).isoformat(), finished_at=None)


def _progress_step(done: int, label: str | None) -> None:
    _PROGRESS.update(done=done, current=label)


def _progress_end() -> None:
    _PROGRESS.update(active=False, current=None,
                     finished_at=datetime.now(timezone.utc).isoformat())

# Submit was clicked but never confirmed — retrying could double-apply.
NEVER_RETRY = AUTO_APPLY_PERMANENT_FAILURES | {"submitted_unverified"}


def _skip_after_failure(app: Application, now: datetime, cooldown_hours: float) -> bool:
    """True when the last attempt failed and must not be retried yet (or ever).

    2026-09-01: without this, the same login-walled job was re-attempted every
    25-minute cycle, tripped the 3-consecutive-failure halt, and starved every
    other candidate. A login wall or CAPTCHA is the same tomorrow — permanent.
    Everything else (timeouts, missing submit button) gets one retry per cooldown.
    """
    status = app.auto_apply_status or ""
    if status in NEVER_RETRY:
        return True
    if not status.startswith("failed_"):
        return False
    attempted = app.auto_apply_attempted_at
    if attempted is None:
        return False
    if attempted.tzinfo is None:
        attempted = attempted.replace(tzinfo=timezone.utc)
    return now - attempted < timedelta(hours=cooldown_hours)


# Maps a job's `source` prefix to the right applier class. Keys double as the
# ATS keys for guardrail lookups (allowed_ats_platforms / min_score_per_ats), so
# anything not listed here — SmartRecruiters, career pages — is "generic".
# 2026-09-07: every form-based ATS goes through the extension's mapper engine;
# only Workday keeps its multi-page wizard applier.
APPLIER_MAP = {
    "greenhouse": MapperApplier,
    "ashby": MapperApplier,
    "lever": MapperApplier,
    "workday": WorkdayAutoApplier,
    "generic": MapperApplier,
    "custom": MapperApplier,
}


def _applier_for_source(source: str | None):
    """Pick the right applier for a job. Falls back to the mapper engine."""
    if not source:
        return MapperApplier
    prefix = source.split(":", 1)[0].lower().strip()
    return APPLIER_MAP.get(prefix, MapperApplier)


def _ats_key_for_source(source: str | None) -> str:
    """Return the ATS key (e.g. 'greenhouse', 'workday') for guardrail lookups."""
    if not source:
        return "generic"
    prefix = source.split(":", 1)[0].lower().strip()
    return prefix if prefix in APPLIER_MAP else "generic"


def _min_score_for_ats(ats_key: str, profile: dict) -> int:
    """Return the per-ATS min_score (falls back to global)."""
    guardrails = profile.get("guardrails", {})
    per_ats = guardrails.get("min_score_per_ats", {}) or {}
    return per_ats.get(ats_key, guardrails.get("min_score", 75))


# ============================================================
# Daily cap helper
# ============================================================

def _count_auto_applies_today(session) -> int:
    """How many auto-applies have already happened today (UTC)?"""
    start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        session.query(Application)
        .filter(Application.auto_applied.is_(True))
        .filter(Application.auto_apply_attempted_at >= start_of_day)
        .count()
    )


# ============================================================
# Candidate selection
# ============================================================

def _passes_hourly_floor(archetype, hourly_min, hourly_max, min_hourly_map: dict | None) -> bool:
    """True if a role clears its archetype's hourly floor (or has no floor).

    Used to enforce the Behavioral Technician ≥ $30/hr rule: a BT role is only
    auto-apply eligible when a known hourly rate meets the floor. Unknown pay
    (None) for a floored archetype returns False — we never auto-submit a BT
    role whose pay we can't confirm is at/above the floor.
    """
    floor = (min_hourly_map or {}).get(archetype)
    if floor is None:
        return True
    rate = hourly_max if hourly_max is not None else hourly_min
    return rate is not None and rate >= float(floor)


def _candidates(session, profile: dict, config: dict | None = None, max_candidates: int = 50) -> list[tuple[Application, Job, JobScore]]:
    """Find applications eligible for auto-submit.

    Filters:
      * status == MATERIALS_READY
      * resume_path AND cover_letter_path both set + files exist on disk
      * job.source prefix in profile.guardrails.allowed_ats_platforms
      * score.fit_score >= per-ATS min_score (defaults to global guardrails.min_score)
      * archetype hourly floor met (BT: >= $30/hr; settings.comp.min_hourly_by_archetype)
      * not already auto-applied
      * last attempt didn't fail permanently, or failed retryably more than
        guardrails.retry_cooldown_hours ago (default 24)
    """
    guardrails = profile.get("guardrails", {})
    fallback_min = guardrails.get("min_score", 75)
    cooldown_hours = float(guardrails.get("retry_cooldown_hours", 24))
    now = datetime.now(timezone.utc)
    allowed_ats = set(guardrails.get("allowed_ats_platforms", ["greenhouse", "ashby", "lever"]))
    # BT track: per-archetype hourly floor (e.g. behavioral_technician: 30).
    min_hourly_map = (config or {}).get("comp", {}).get("min_hourly_by_archetype", {}) or {}

    # Pull every plausibly-eligible row, then filter ATS + per-ATS threshold in Python
    # (SQL would need an enum over ATS prefixes; the dataset is small enough to filter in app code)
    rows = (
        session.query(Application, Job, JobScore)
        .join(Job, Application.job_id == Job.id)
        .join(JobScore, JobScore.job_id == Job.id)
        .filter(Application.status == ApplicationStatus.MATERIALS_READY)
        .filter(Application.auto_applied.is_(False))
        .filter(JobScore.fit_score >= max(fallback_min - 10, 0))  # over-pull, threshold-filter below
        .filter(Application.resume_path.isnot(None))
        .filter(Application.cover_letter_path.isnot(None))
        .order_by(JobScore.fit_score.desc())
        .limit(max_candidates * 3)  # over-pull to allow per-ATS filtering
        .all()
    )

    eligible: list[tuple[Application, Job, JobScore]] = []
    for app, job, score in rows:
        if _skip_after_failure(app, now, cooldown_hours):
            continue
        ats_key = _ats_key_for_source(job.source)
        if ats_key not in allowed_ats:
            continue
        per_ats_min = _min_score_for_ats(ats_key, profile)
        if score.fit_score < per_ats_min:
            continue
        if not Path(app.resume_path).exists() or not Path(app.cover_letter_path).exists():
            continue
        if not _passes_hourly_floor(getattr(score, "archetype", None),
                                    getattr(job, "hourly_min", None),
                                    getattr(job, "hourly_max", None),
                                    min_hourly_map):
            logger.info(
                f"[auto_apply] skip '{job.title}' @ {job.company} — "
                f"below hourly floor for {getattr(score, 'archetype', None)} (or pay unconfirmed)"
            )
            continue
        eligible.append((app, job, score))
        if len(eligible) >= max_candidates:
            break
    return eligible


# ============================================================
# Result persistence
# ============================================================

def _record_result(session, app: Application, result: ApplyResult) -> None:
    """Persist the auto-apply outcome onto the Application row."""
    app.auto_applied = bool(result.success and result.status == "submitted")
    app.auto_apply_status = result.status
    app.auto_apply_log = result.to_log_json()
    app.auto_apply_attempted_at = datetime.now(timezone.utc)
    if result.status in AUTO_APPLY_PERMANENT_FAILURES:
        # Hand it to the human — the dashboard shows this as "Needs manual apply".
        reason = result.status.removeprefix("failed_").replace("_", " ")
        app.next_action = f"Apply manually — bot hit {reason}"
    elif result.status == "submitted_unverified":
        app.next_action = "Check email — bot clicked Submit but saw no confirmation; verify before re-applying"

    if result.success and result.status == "submitted":
        record_status_change(session, app, ApplicationStatus.APPLIED,
                             source="auto_applier",
                             note=f"auto-apply {result.status}")

    session.commit()


# ============================================================
# Main runner
# ============================================================

def run_auto_apply(config: dict | None = None, only_app_id: int | None = None,
                   owner: str = "scheduler", respect_lock: bool = False) -> dict:
    """Run one auto-apply cycle.

    Args:
        config: Optional pipeline-level config (reserved for future use).
        only_app_id: apply to just this one Application and stop. The auto-run
            pipeline uses it so each job is generated -> applied -> emailed
            before the next one starts, instead of applying to the whole
            eligible set in one pass.

    Returns:
        Summary dict with counts.
    """
    profile = load_profile()
    guardrails = profile.get("guardrails", {})

    if not guardrails.get("enabled", False):
        logger.info("[auto_apply] disabled in profile.guardrails — skipping")
        return {"enabled": False}

    # Stand down while an Agent-screen run is working: two cycles applying at
    # once produce interleaved, unpredictable ordering and two unrelated
    # progress counters on the same screen.
    if respect_lock:
        held = apply_lock_holder()
        if held:
            logger.info(f"[auto_apply] skipped — {held.get('owner')} run in progress")
            return {"skipped_locked": True, "holder": held.get("owner")}
    took_lock = _acquire_apply_lock(owner)

    daily_cap = guardrails.get("daily_cap", 10)
    max_failures = guardrails.get("max_consecutive_failures", 3)
    per_app_timeout = guardrails.get("per_app_timeout_seconds", 90)
    dry_run = bool(guardrails.get("dry_run", False))

    session = get_session()
    try:
        applied_today = _count_auto_applies_today(session)
        remaining_quota = max(0, daily_cap - applied_today)
        if remaining_quota == 0:
            logger.info(f"[auto_apply] daily cap of {daily_cap} already reached — skipping")
            if took_lock:
                _release_apply_lock()
            return {"daily_cap_hit": True, "applied_today": applied_today}

        candidates = _candidates(session, profile, config=config, max_candidates=remaining_quota * 2)
        if only_app_id is not None:
            candidates = [c for c in candidates if c[0].id == only_app_id][:1]
        if not candidates:
            logger.info("[auto_apply] no eligible candidates this cycle")
            if took_lock:
                _release_apply_lock()
            return {"total_attempted": 0, "submitted": 0}

        logger.info(
            f"[auto_apply] {len(candidates)} candidates; "
            f"daily quota remaining: {remaining_quota}; "
            f"dry_run={dry_run}"
        )
    finally:
        session.close()

    # Learn which addresses bounced since last cycle, so a bad constructed
    # address is dropped after one failure instead of being retried forever.
    if ((config or {}).get("mail") or {}).get("guess_addresses"):
        try:
            from utils.bounce_watch import scan_bounces
            b = scan_bounces()
            if b.get("new_dead"):
                logger.info(f"[auto_apply] bounce scan: {b['new_dead']} new dead address(es)")
        except Exception as e:
            logger.warning(f"[auto_apply] bounce scan failed: {e}")

    _progress_start(min(len(candidates), remaining_quota))
    summary = {
        "submitted": 0,
        "dry_run_count": 0,
        "failed_captcha": 0,
        "failed_other": 0,
        "total_attempted": 0,
    }
    consecutive_failures = 0
    submitted_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,  # set to False during debugging to watch it work
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            bypass_csp=True,  # MapperApplier injects the extension's scan.js/fill.js
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
        )
        # Reduce automation fingerprint
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        try:
            for app_data in candidates[:remaining_quota]:
                app, job, score = app_data
                if submitted_count >= remaining_quota:
                    break
                if consecutive_failures >= max_failures:
                    logger.warning(f"[auto_apply] {max_failures} consecutive failures — halting cycle")
                    break

                summary["total_attempted"] += 1
                _progress_step(summary["total_attempted"], f"{job.title} @ {job.company}")
                ats_key = _ats_key_for_source(job.source)
                applier_cls = _applier_for_source(job.source)
                applier = applier_cls(profile=profile, dry_run=dry_run)
                result = ApplyResult(success=False, status="pending")
                t0 = time.time()
                page = None
                page_text = ""      # live JD/confirmation text, harvested before close

                try:
                    logger.info(
                        f"[auto_apply] {ats_key}: {job.title} @ {job.company} "
                        f"(score={score.fit_score})"
                    )
                    page = context.new_page()
                    page.set_default_timeout(per_app_timeout * 1000)
                    page.goto(job.url, wait_until="domcontentloaded", timeout=20000)
                    # Grab the JD as rendered, before apply() navigates on to the
                    # form — "email your CV to ..." usually lives on this page.
                    try:
                        page_text = page.evaluate(
                            "(document.body ? document.body.innerText : '').slice(0, 200000)") or ""
                    except Exception:
                        page_text = ""
                    applier.apply(page, job, app.resume_path, app.cover_letter_path, result)
                except Exception as e:
                    result.success = False
                    if not result.status or result.status == "pending":
                        result.status = "failed_unknown"
                    result.message = f"Unhandled: {e}"
                    result.add_step("exception", str(e), success=False)
                    logger.exception(f"[auto_apply] error on job {job.id}")
                finally:
                    if page is not None:
                        # Read the rendered page before it goes: an application
                        # address is often printed in the JD itself and never
                        # makes it into the scraped `description` (ATS APIs
                        # truncate, and some boards omit the body entirely).
                        try:
                            page_text += "\n" + (page.evaluate(
                                "(document.body ? document.body.innerText : '').slice(0, 200000)") or "")
                        except Exception:
                            pass
                        try:
                            page.close()
                        except Exception:
                            pass
                    result.duration_seconds = time.time() - t0

                # Persist result and bookkeeping
                # New session per write so the auto-apply loop doesn't hold a long-lived txn
                fresh_session = get_session()
                try:
                    fresh_app = fresh_session.query(Application).get(app.id)
                    if fresh_app is not None:
                        _record_result(fresh_session, fresh_app, result)
                finally:
                    fresh_session.close()

                # Email the application package (self-copy record by default).
                # Never blocks or fails the apply cycle.
                if result.status in ("submitted", "submitted_unverified"):
                    try:
                        _email_application(profile, config, job, app, page_text=page_text)
                    except Exception as e:
                        logger.warning(f"[auto_apply] application email failed: {e}")

                if result.success and result.status == "submitted":
                    summary["submitted"] += 1
                    submitted_count += 1
                    consecutive_failures = 0
                elif result.status == "dry_run":
                    summary["dry_run_count"] += 1
                    consecutive_failures = 0
                elif result.status == "submitted_unverified":
                    # Probably went through — count it against today's quota.
                    summary["submitted_unverified"] = summary.get("submitted_unverified", 0) + 1
                    submitted_count += 1
                    consecutive_failures = 0
                elif result.status == "failed_captcha":
                    summary["failed_captcha"] += 1
                    consecutive_failures += 1
                else:
                    summary["failed_other"] += 1
                    # Only a SYSTEM fault counts toward the halt. A form the bot
                    # refused to fake (required fields, essay cap) is the bot
                    # working correctly and is the common case — counting those
                    # stopped every cycle after ~6 jobs.
                    if result.status in SYSTEM_FAULTS:
                        consecutive_failures += 1
                    else:
                        consecutive_failures = 0
                        summary["skipped_expected"] = summary.get("skipped_expected", 0) + 1

                # Polite gap between submissions so we don't look like a flood
                time.sleep(3)

        finally:
            if took_lock:
                _release_apply_lock()
            _progress_end()
            try:
                context.close()
                browser.close()
            except Exception:
                pass

    logger.info(f"[auto_apply] cycle complete: {summary}")
    return summary
