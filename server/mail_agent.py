"""Mail Agent — a queue of jobs to email, and a worker that makes them sendable.

WHY THIS EXISTS
---------------
`server/outreach_mail.py`'s /candidates screen only surfaces rows that ALREADY
have both a tailored CV and an address the posting published. Measured on the
live jobpilot.db that is 0 of 843 jobs, so the Outreach screen renders an empty
list and the user cannot act on anything at all. The two missing pieces are not
the same kind of missing:

  - the address is missing because employers who recruit through LinkedIn's
    apply flow have no reason to print an inbox (12 of 12 live "AI Engineer"
    postings printed none), and
  - the CV is missing because agents/tailor.tailor_for_job takes ~3.5 minutes
    per job at max_rounds 3, so it can never run inside a request.

This module inverts the screen. The user queues ANY application; a background
worker does the preparation — generate the materials, find a recipient, render
the preview — and only rows that survive all of it become `ready`.
Sending is a separate, explicit step that reuses outreach_mail's send path
unchanged, so every guardrail (dedup INSERT before SMTP, daily cap counted from
OutreachSend, suppression, dead list, is_safe_recipient) applies here too.

THE ORDER OF THE PREPARE STEPS IS A DELIBERATE COST TRADE
---------------------------------------------------------
`mail.outreach.materials_first` decides it, and it defaults to TRUE:

    materials_first=True   score -> tailor CV + cover letter -> gather recipient
                           -> preview -> ready
    materials_first=False  score -> gather recipient -> tailor -> preview -> ready

The cost is real and measured: agents.tailor.tailor_for_job takes ~3.5 minutes
per job at max_rounds 3, and "no address" is the common case (0 of 843 live rows
had both an address and a CV), so tailoring first spends roughly 3.5 minutes on
every row that turns out to be unmailable. The user chose to pay it, for two
reasons that the cheaper order cannot buy back:

  - a tailored CV is useful even with no inbox to send it to — it is what they
    attach when applying by hand through the employer's own portal, and
  - it decouples "we have materials" from "we found an address". A row blocked
    on `no_recipient` keeps its resume_path and cover_letter_path, so the bounce
    recheck below — or a later re-run after the company site starts publishing a
    careers address — only has to find an address, never to re-tailor.

materials_first=False restores the pure cost guard for a bulk run over hundreds
of rows, where 49 hours of compute to learn what a regex knows is the wrong
trade. For the same reason the extractor's optional LLM de-obfuscation pass is
off by default here (`use_llm=False`): one model call per row to learn "this
posting has no address" is spend with no artifact to show for it, unlike the
tailoring. A user who wants it can pass use_llm=True for a small batch.

Two classes of check still run BEFORE tailoring in either order, because they
cost nothing and genuinely mean "never mail this": an existing ledger row for
this company+role (`already_emailed`), and a recipient already stored on the row
that is suppressed, dead or unsafe. Neither can be fixed by generating a CV.

WHERE THE ADDRESS COMES FROM
----------------------------
The posting's own text first (a regex over the description), then the shared
resolver `utils.recipient.resolve`, which owns crawling the employer's site,
constructing a role address, MX-checking it and rationing the constructed ones.
It is imported defensively: this router is imported at dashboard startup, so a
missing or half-written sibling must degrade to `outreach_mail`'s in-repo
fallback rather than take the whole dashboard down. Its `reason` is stored on
the row so a block reads "domain belongs to linkedin.com, not the employer"
instead of the generic "no_recipient".

BOUNCE RECHECK
--------------
A constructed address passes an MX check without proving the mailbox exists, so
some bounce. /recheck reads those bounces (utils.bounce_watch, read-only IMAP),
and every queue row whose recipient is now on the dead list is re-resolved to a
DIFFERENT address and returned to `ready` — reusing the materials, never
re-tailoring. This pays off across rows rather than within one: careers@acme.com
is typically the resolved address for EVERY queued job at Acme, so one bounce
invalidates all of them at once and one recheck repairs all of them at once.
`mail.outreach.max_retries` (default 2) stops a company with a permanently
broken mail server from being retried forever.

WHAT THIS MODULE DELIBERATELY DOES NOT OWN
------------------------------------------
No screening, body-building, hashing, capping or delivery logic is reimplemented
here — all of it is called on `server.outreach_mail`. Those functions are reached
through the module object (`om.<name>`) rather than bound at import, so a
monkeypatch in a test — or a hot-fix at runtime — takes effect for this router
too instead of silently missing a stale local reference.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Iterable

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from db.database import get_session
from db.models import Application, Job, JobScore, MailQueueItem
from server import outreach_mail as om
from server.outreach_mail import _exists, _int, _iso, _naive_utc, _utc_now
# Not guarded: server.outreach_mail already imports utils.bounce_watch at module
# level, so a broken bounce_watch has taken this router down one import earlier.
# Bound here rather than inside /recheck so tests have one honest patch point.
from utils.bounce_watch import mark_dead, scan_bounces
from utils.company_names import normalize_company_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mail-agent", tags=["mail-agent"])

# --------------------------------------------------------------------------
# utils.linkedin_outreach is a sibling deliverable that may be missing or
# half-written. server/dashboard.py imports this router at startup, so an
# eager import here would take the whole dashboard down with it — guarded for
# exactly the reason outreach_mail guards the same import. Only the opt-out
# scan is used from it, and its absence simply means the scan is skipped.
# --------------------------------------------------------------------------
try:  # pragma: no cover - exercised by whichever half of the branch is live
    from utils import linkedin_outreach as _lo
except Exception as _e:
    _lo = None
    logger.info(f"[mail_agent] utils.linkedin_outreach unavailable ({_e}); "
                f"opt-out scanning is skipped")

_lo_scan_optouts = getattr(_lo, "scan_optouts", None) if _lo else None

# --------------------------------------------------------------------------
# utils.recipient is the shared address resolver (crawl the employer's site,
# construct a role address, MX-check it, ration the constructed ones). Same
# startup guard, same reason. Its absence is not fatal: the resolution falls
# back to outreach_mail._company_fallback_recipient, which is the same
# utils/company_email path with the older, stricter rule that refused every
# constructed address. Bound as module-level names so a test patches one symbol.
# --------------------------------------------------------------------------
try:  # pragma: no cover - exercised by whichever half of the branch is live
    from utils import recipient as _rc
except Exception as _e:
    _rc = None
    logger.info(f"[mail_agent] utils.recipient unavailable ({_e}); falling back "
                f"to outreach_mail's company resolver")

_resolve_recipient = getattr(_rc, "resolve", None) if _rc else None
_guessed_sent_today = getattr(_rc, "guessed_sent_today", None) if _rc else None

# --------------------------------------------------------------------------
# agents.tailor pulls python-docx and the LLM client. Guarded for the same
# startup reason, and bound at module level rather than imported inside the
# worker so tests have one honest patch point — `mail_agent.tailor_for_job` —
# whose call count proves the cost guard above actually holds.
# --------------------------------------------------------------------------
try:  # pragma: no cover - exercised by whichever half of the branch is live
    from agents.tailor import tailor_for_job
except Exception as _e:
    tailor_for_job = None
    logger.warning(f"[mail_agent] agents.tailor unavailable ({_e}); "
                   f"preparation cannot generate missing materials")


#: MailQueueItem.status values, in lifecycle order. `blocked` and `failed` are
#: terminal for a run but not for the row: the user can fix the cause (lift a
#: suppression, let the cap roll over) and re-queue.
STATUSES = ("queued", "preparing", "ready", "sending", "sent", "blocked", "failed")

#: Block reasons that mean "this row cannot become sendable by trying again
#: today". `cooldown` and `company_capped` are absent on purpose — they expire,
#: and the send path re-screens them anyway, so a row that only trips those is
#: still worth previewing.
HARD_BLOCKS = frozenset({"already_emailed", "suppressed", "dead_address",
                         "unsafe_recipient", "no_recipient", "no_attachments",
                         "no_alternate_address", "retries_exhausted"})

#: Block reasons a human may NEVER override. Narrower than HARD_BLOCKS on
#: purpose, because the two sets answer different questions: HARD_BLOCKS asks
#: "can this row become sendable by trying again today" (about the ROW), this
#: one asks "may a human override this at all" (about someone ELSE).
#:
#:   suppressed       a person asked not to be contacted; overriding it mails
#:                    someone who explicitly opted out.
#:   unsafe_recipient the only address found is an accommodation / legal /
#:                    compliance / no-reply inbox. Those channels exist for other
#:                    purposes and people depend on them.
#:   already_emailed  one message per company per role, ever. Overriding it sends
#:                    a duplicate to a real person, and the ledger's UNIQUE
#:                    dedup_key would refuse the send anyway — so allowing it
#:                    would only buy a confusing failure several minutes later.
#:
#: Everything else — no_recipient, no_attachments, no_alternate_address,
#: retries_exhausted, dead_address, cap_reached, cooldown, company_capped, and
#: status `failed` — is a circumstance, not a person saying no, and the user may
#: retry it once the circumstance changes.
NEVER_UNBLOCKABLE = frozenset({"suppressed", "unsafe_recipient", "already_emailed"})

#: Why each of those is refused, as one line for the card and the 409 body.
_UNBLOCK_REFUSALS = {
    "suppressed": "someone there asked not to be contacted",
    "unsafe_recipient": "the address is an accommodations/legal/no-reply inbox",
    "already_emailed": "this company and role were already emailed once",
}

#: Statuses with nothing to un-block. `sent` is the one that matters: its dedup
#: key is spent and returning it to the queue would set up a second email about
#: one role, which is the same harm `already_emailed` exists to prevent.
_UNBLOCK_STATUS_REFUSALS = {"sent": "already sent", "sending": "a send is in flight",
                            "preparing": "being prepared right now",
                            "ready": "not blocked", "queued": "not blocked"}

#: How the shared resolver names its sources vs. what the outreach ledger and
#: the UI already call them. Mapped rather than stored raw so `recipient_source`
#: means one thing across both routers and the OutreachSend column.
#: "constructed" in particular must survive the round trip verbatim: the
#: resolver's own daily guess quota counts OutreachSend rows by exactly that
#: recipient_source, so renaming it here would silently un-cap guessed sends.
_SOURCE_ALIASES = {"crawl": "company_site", "website": "company_site",
                   "company": "company_site", "guess": "constructed",
                   "guessed": "constructed", "posting": "post_text"}

#: Prepare is one row at a time and each row can take minutes; a bulk add of the
#: whole database would otherwise start a run measured in days.
_PREPARE_MAX = 50


# --------------------------------------------------------------------------
# Worker state. Mirrors server/dashboard._AGENT: a module-level dict, a daemon
# thread, a singleton guard and a /status endpoint the UI polls. Cancellation is
# a cooperative flag read between rows — never a thread kill, which would leave a
# half-written .docx and a row stuck in `preparing`.
# --------------------------------------------------------------------------

_WORKER: dict[str, Any] = {
    "thread": None, "active": False, "cancel": False, "phase": "idle",
    "done": 0, "total": 0, "started_at": None, "current": None, "error": None,
    "prepared": 0, "blocked": 0, "failed": 0,
}
_WORKER_LOCK = threading.Lock()

#: Last /recheck outcome, surfaced on /status so the UI can say "3 bounced, 2
#: re-addressed" without keeping its own copy. Counters describe the LAST run,
#: not a running total: a total would be indistinguishable from a stuck run.
_RECHECK: dict[str, Any] = {"last_recheck": None, "bounced": 0, "retried": 0,
                            "blocked": 0, "scanned": 0, "new_dead": 0}


def _running() -> bool:
    t = _WORKER["thread"]
    return bool(_WORKER["active"]) and bool(t and t.is_alive())


# --------------------------------------------------------------------------
# Row loading / serialisation
# --------------------------------------------------------------------------

def _rows(session, *, ids: Iterable[int] | None = None, status: str | None = None):
    """Queue rows joined to everything a card needs, oldest first.

    The join goes through Application.job_id rather than MailQueueItem.job_id:
    the queue column is a convenience copy that is nullable, and a row whose
    job link is only on the Application must still render.
    """
    q = (session.query(MailQueueItem, Application, Job, JobScore)
         .join(Application, MailQueueItem.application_id == Application.id)
         .outerjoin(Job, Application.job_id == Job.id)
         .outerjoin(JobScore, JobScore.job_id == Job.id))
    if ids is not None:
        q = q.filter(MailQueueItem.id.in_(list(ids)))
    if status:
        q = q.filter(MailQueueItem.status == status)
    return q.order_by(MailQueueItem.added_at.asc(), MailQueueItem.id.asc()).all()


def _counts_by_status(session) -> dict[str, int]:
    """Every lifecycle status present as a key, zeros included — a UI that reads
    `counts["ready"]` must not KeyError on an empty queue."""
    out = {s: 0 for s in STATUSES}
    for status, n in (session.query(MailQueueItem.status,
                                    func.count(MailQueueItem.id))
                      .group_by(MailQueueItem.status).all()):
        out[status or "queued"] = int(n or 0)
    return out


def _item_dict(item: MailQueueItem, app_obj: Application | None,
               job: Job | None, score: JobScore | None) -> dict:
    body = item.body or ""
    return {
        "id": item.id,
        "application_id": item.application_id,
        "job_id": item.job_id or (job.id if job else None),
        "status": item.status,
        "added_from": item.added_from,
        "title": (job.title if job else "") or "",
        "company": (job.company if job else "") or "",
        "location": (job.location if job else "") or "",
        "url": (job.url if job else "") or "",
        "source": (job.source if job else "") or "",
        "fit_score": score.fit_score if score else None,
        "archetype": score.archetype if score else None,
        "recipient": item.recipient,
        "recipient_source": item.recipient_source,
        "subject": item.subject,
        "body": body,
        "body_preview": body[:400],
        "preview_hash": item.preview_hash,
        "resume_path": item.resume_path or (app_obj.resume_path if app_obj else None),
        "cover_letter_path": (item.cover_letter_path
                              or (app_obj.cover_letter_path if app_obj else None)),
        "materials_ready": bool(_exists(item.resume_path)
                                and _exists(item.cover_letter_path)),
        "block_reason": item.block_reason,
        "error": item.error,
        "attempts": item.attempts or 0,
        "outreach_send_id": item.outreach_send_id,
        "added_at": _iso(_naive_utc(item.added_at)),
        "prepared_at": _iso(_naive_utc(item.prepared_at)),
        "sent_at": _iso(_naive_utc(item.sent_at)),
    }


def _cap(session, cfg: dict) -> dict:
    return {"daily_cap": _int(cfg, "daily_cap"),
            "sent_today": om._sent_today(session),
            "remaining": om._remaining_today(session, cfg)}


# --------------------------------------------------------------------------
# Queue management
# --------------------------------------------------------------------------

class QueueRequest(BaseModel):
    application_ids: list[int] = Field(default_factory=list)
    added_from: str = "manual"


@router.post("/queue")
def add_to_queue(payload: QueueRequest) -> dict:
    """Add applications to the mail queue. Adding a queued row again is a no-op.

    Idempotency is the UNIQUE constraint on mail_queue.application_id, not a
    SELECT-then-INSERT: two dashboard tabs clicking "Queue all" at the same
    moment both pass a pre-check and both insert. Each row is therefore
    committed on its own so one IntegrityError rolls back one row rather than
    the whole batch.
    """
    added, skipped, already_sent, missing = 0, 0, 0, 0
    session = get_session()
    try:
        for aid in payload.application_ids or []:
            row = (session.query(Application, Job)
                   .outerjoin(Job, Application.job_id == Job.id)
                   .filter(Application.id == aid).first())
            if row is None:
                missing += 1
                continue
            app_obj, job = row
            # An application already emailed for this company+role can never be
            # sent again (the ledger's UNIQUE dedup_key forbids it), so queuing
            # it would only ever produce a `blocked` card.
            if job is not None:
                key = om.outreach_dedup_key(job.company or "", job.title or "",
                                            str(job.source_id or ""))
                if om._existing_send(session, key) is not None:
                    already_sent += 1
                    continue
            session.add(MailQueueItem(
                application_id=aid, job_id=app_obj.job_id, status="queued",
                added_from=(payload.added_from or "manual")[:20]))
            try:
                session.commit()
                added += 1
            except IntegrityError:
                session.rollback()
                skipped += 1
        counts = _counts_by_status(session)
        logger.info(f"[mail_agent] queue add: {added} added, {skipped} already queued, "
                    f"{already_sent} already emailed, {missing} unknown")
        return {"added": added, "skipped": skipped, "already_sent": already_sent,
                "not_found": missing, "counts_by_status": counts,
                "total": sum(counts.values())}
    finally:
        session.close()


@router.get("/queue")
def list_queue(status: str | None = Query(None),
               limit: int = Query(200, ge=1, le=1000)) -> dict:
    cfg = om._outreach_cfg()
    session = get_session()
    try:
        items = [_item_dict(*r) for r in _rows(session, status=status)[:limit]]
        counts = _counts_by_status(session)
        return {"items": items, "counts_by_status": counts,
                "total": sum(counts.values()), "cap": _cap(session, cfg),
                "sending_enabled": bool(cfg.get("enabled"))}
    finally:
        session.close()


@router.delete("/queue/{item_id}")
def remove_from_queue(item_id: int) -> dict:
    session = get_session()
    try:
        item = session.query(MailQueueItem).filter(MailQueueItem.id == item_id).first()
        if item is None:
            raise HTTPException(status_code=404, detail="queue item not found")
        session.delete(item)
        session.commit()
        return {"removed": 1, "counts_by_status": _counts_by_status(session)}
    finally:
        session.close()


class ClearRequest(BaseModel):
    status: str | None = None


@router.post("/queue/clear")
def clear_queue(payload: ClearRequest) -> dict:
    """Empty the queue, or just one status. Never touches OutreachSend.

    Clearing `sent` rows removes the CARD, not the send: the ledger row is what
    stops a re-send, and it is immutable by design.
    """
    if payload.status and payload.status not in STATUSES:
        raise HTTPException(status_code=422,
                            detail=f"status must be one of {', '.join(STATUSES)}")
    session = get_session()
    try:
        q = session.query(MailQueueItem)
        if payload.status:
            q = q.filter(MailQueueItem.status == payload.status)
        removed = q.delete(synchronize_session=False)
        session.commit()
        logger.info(f"[mail_agent] cleared {removed} row(s) "
                    f"({payload.status or 'all statuses'})")
        return {"removed": int(removed or 0),
                "counts_by_status": _counts_by_status(session)}
    finally:
        session.close()


# --------------------------------------------------------------------------
# Prepare — the background worker
# --------------------------------------------------------------------------

def _mark(session, item: MailQueueItem, status: str, *, reason: str | None = None,
          error: str | None = None) -> None:
    item.status = status
    item.block_reason = reason
    if error is not None:
        item.error = error
    session.commit()


def _ensure_materials(session, item: MailQueueItem, app_obj: Application,
                      job: Job, score: JobScore | None) -> tuple[str | None, str | None]:
    """Existing tailored materials, or freshly generated ones.

    Reuse is checked with is_file(), not with a non-null column: output/ is
    routinely pruned and a path pointing at a deleted .docx would sail through a
    null check and then fail at attachment time, after the ledger row was
    already claimed.

    tailor_for_job requires a JobScore for its ATS keywords. Unscored jobs are
    given a transient, UNSAVED zero score rather than being blocked — a résumé
    tailored from the job description alone is still far better than the generic
    base, and writing a fake score row would corrupt the scoring pipeline.
    """
    resume = item.resume_path or app_obj.resume_path
    cover = item.cover_letter_path or app_obj.cover_letter_path
    if _exists(resume) and _exists(cover):
        logger.info(f"[mail_agent] reusing existing materials for app {app_obj.id}")
        return resume, cover
    if tailor_for_job is None:
        raise RuntimeError("agents.tailor is unavailable; cannot generate materials")

    logger.info(f"[mail_agent] tailoring materials for {job.title!r} at {job.company!r}")
    _WORKER["current"] = {"queue_id": item.id, "title": job.title,
                          "company": job.company, "step": "tailoring"}
    stand_in = score or JobScore(job_id=job.id, fit_score=0, key_matches=[],
                                 key_gaps=[], ats_keywords=[])
    result = tailor_for_job(job, stand_in, cover_letter=True) or {}
    resume = result.get("resume_docx") or resume
    cover = result.get("cover_letter_docx") or cover
    # Both records get the paths: Application is what the rest of the pipeline
    # (auto-apply, autofill history) reads, the queue row is what the send path
    # attaches, and they must not be able to drift apart.
    app_obj.resume_path = resume
    app_obj.cover_letter_path = cover
    item.resume_path = resume
    item.cover_letter_path = cover
    session.commit()
    return resume, cover


def _mail_settings() -> dict:
    """The `mail:` block of settings.yaml, for the shared resolver's mail_cfg.

    Read through outreach_mail rather than re-opening the file so both routers
    always see the same knobs (lookup_website, guess_addresses, guess_locals).
    """
    try:
        return (om._cfg().get("mail") or {}) if isinstance(om._cfg(), dict) else {}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[mail_agent] could not read mail settings: {e}")
        return {}


def _guessed_today() -> int:
    """Constructed addresses already sent to today — the resolver's quota input."""
    if _guessed_sent_today is None:
        return 0
    try:
        return int(_guessed_sent_today() or 0)
    except Exception as e:
        logger.warning(f"[mail_agent] guessed_sent_today failed: {e}; assuming 0")
        return 0


def _resolver_reason(rec: dict) -> str:
    """The resolver's refusal, as one line for the card.

    `reason` alone is a token ("domain_unconfirmed"); `rejected` carries the
    address each token was about, and that is what makes the block readable —
    "domain_unconfirmed" says nothing, "acme.io: domain_unconfirmed" says which
    guess was thrown away and lets the user judge it.
    """
    reason = str(rec.get("reason") or "").strip()
    rejected = rec.get("rejected")
    if isinstance(rejected, (list, tuple)) and rejected:
        detail = "; ".join(
            f"{(r or {}).get('address', '?')}: {(r or {}).get('reason', '?')}"
            for r in rejected[:5] if isinstance(r, dict))
        if detail:
            return f"{reason or 'no_address_found'} ({detail})"[:2000]
    return reason or "no_address_found"


def _resolve_address(job: Job, dead: set[str]) -> dict:
    """An address for the COMPANY, when the posting itself publishes none.

    Returns {"address", "source", "reason"}. `reason` is the resolver's own
    explanation and is what the UI shows instead of the generic "no_recipient":
    "domain_origin=name, not the posting URL" is actionable, "no_recipient" is
    not. It is populated on both outcomes — a resolver that DID find an address
    still reports how, e.g. which quota the guess came out of.

    A constructed (careers@domain) address is no longer refused here. The old
    rule in outreach_mail refused every one of them because a bare company name
    could match a stranger's domain; rationing that risk — MX check, domain
    provenance, a daily quota — is now the resolver's job, and this router
    treats it exactly as the regular agent does. The screens below still apply.
    """
    if _resolve_recipient is not None:
        try:
            rec = _resolve_recipient(job, mail_cfg=_mail_settings(), dead=dead,
                                     sent_today_guessed=_guessed_today()) or {}
            addr = str(rec.get("address") or "").strip()
            src = str(rec.get("source") or "").strip().lower()
            return {"address": addr or None,
                    "source": (_SOURCE_ALIASES.get(src, src) or "company_site")[:30],
                    "reason": _resolver_reason(rec) if not addr else None}
        except Exception as e:
            # A sibling module raising must not fail the row: fall through to
            # the in-repo path, which is the same crawler with an older rule.
            logger.warning(f"[mail_agent] utils.recipient.resolve failed for "
                           f"{job.company!r}: {e}; using the in-repo fallback")
    addr, src = om._company_fallback_recipient(job, dead)
    return {"address": addr or None, "source": (src or "company_site")[:30],
            "reason": None if addr else "no_address_found"}


def _screen_address(session, address: str | None, dead: set[str]) -> str | None:
    """The three screens that mean "never mail this address". None means usable.

    One helper for all three callers — the cheap pre-tailor gate, prepare's
    post-resolution check and the bounce retry — so a retry can never pass a
    screen the first attempt failed.
    """
    addr = (address or "").strip()
    if not addr:
        return None
    if om._is_suppressed(session, addr):
        return "suppressed"
    if addr.lower() in dead:
        return "dead_address"
    if not om.is_safe_recipient(addr):
        return "unsafe_recipient"
    return None


def _gather_recipient(session, app_obj: Application, job: Job,
                      score: JobScore | None, *, dead: set[str], use_llm: bool,
                      exclude: str | None = None) -> tuple[om._Candidate, str | None]:
    """The candidate, with whatever address can be found. Returns (cand, reason).

    `reason` is None when an address was found, and otherwise the resolver's
    explanation for why there is none.

    `exclude` is the address a bounce just retired. The dead set already filters
    it out of both the post-text extractor and the resolver; comparing again is
    belt-and-braces against a resolver that ignores its `dead` argument, because
    handing back the address that just bounced would loop forever.
    """
    cand = om._build_candidate(session, app_obj, job, score, use_llm=use_llm,
                               dead=dead, resolve_company=False)
    ex = (exclude or "").strip().lower()
    if cand.recipient and cand.recipient.strip().lower() == ex:
        cand.recipient = None
    if cand.recipient:
        return cand, None

    res = _resolve_address(job, dead)
    addr = (res.get("address") or "").strip()
    if not addr or addr.lower() == ex:
        return cand, (res.get("reason") or "no_address_found")
    cand.recipient = addr
    cand.recipient_source = res.get("source") or "company_site"
    cand.contact = {**(cand.contact or {}), "address": addr,
                    "fallback": cand.recipient_source}
    return cand, None


def _prepare_one(session, item_id: int, cfg: dict, dead: set[str],
                 use_llm: bool) -> str:
    """Take one queued row as far as it can go. Returns the resulting status.

    Each step short-circuits: the moment a row cannot become sendable it is
    written as `blocked` with the reason, and the steps below it never run. The
    ORDER of the two middle steps is `mail.outreach.materials_first` — see the
    module docstring for the ~3.5 min/job trade it decides.
    """
    row = _rows(session, ids=[item_id])
    if not row:
        return "missing"
    item, app_obj, job, score = row[0]
    if job is None:
        _mark(session, item, "blocked", reason="no_job")
        return "blocked"

    item.status = "preparing"
    item.error = None
    item.attempts = (item.attempts or 0) + 1
    session.commit()

    # 1. THE FREE HARD BLOCKS, before either expensive step. Both mean "this row
    #    can never be mailed", and neither is fixed by generating a CV.
    dedup_key = om.outreach_dedup_key(job.company or "", job.title or "",
                                      str(job.source_id or ""))
    if om._existing_send(session, dedup_key) is not None:
        _mark(session, item, "blocked", reason="already_emailed")
        return "blocked"
    known = _screen_address(session, item.recipient, dead)
    if known:
        _mark(session, item, "blocked", reason=known)
        return "blocked"

    materials_first = bool(cfg.get("materials_first", True))
    resume = cover = None

    # 2. MATERIALS, if the user chose to pay for them up front. A row that ends
    #    up with no address still keeps them: the .docx is what they attach when
    #    applying by hand, and the recheck below never has to re-tailor.
    if materials_first:
        resume, cover = _ensure_materials(session, item, app_obj, job, score)

    # 3. RECIPIENT. The posting's own text first, then the shared resolver.
    _WORKER["current"] = {"queue_id": item.id, "title": job.title,
                          "company": job.company, "step": "recipient"}
    cand, reason = _gather_recipient(session, app_obj, job, score, dead=dead,
                                     use_llm=use_llm)
    if reason:
        # block_reason stays the machine-readable class the UI filters on;
        # `error` carries the resolver's precise sentence for the card.
        _mark(session, item, "blocked", reason="no_recipient", error=reason)
        return "blocked"
    item.recipient = cand.recipient
    item.recipient_source = cand.recipient_source
    session.commit()

    blocked = _screen_address(session, cand.recipient, dead)
    if blocked:
        _mark(session, item, "blocked", reason=blocked)
        return "blocked"

    # 4. Materials last, when the cost guard is on: nothing is generated until
    #    the row has an address that survived every screen above.
    if not materials_first:
        resume, cover = _ensure_materials(session, item, app_obj, job, score)
    return _finalize(session, item, cand, job, cfg, dead, resume, cover)


def _finalize(session, item: MailQueueItem, cand: om._Candidate, job: Job,
              cfg: dict, dead: set[str], resume: str | None,
              cover: str | None) -> str:
    """Attachment check, preview, `ready`. Shared by prepare and by the retry.

    The stored subject/body/hash ARE what /send transmits — the row the user
    approves is the row that goes out — so a re-addressed row MUST come back
    through here: its old preview named the address that bounced, and its old
    hash would refuse the send it is being repaired for.
    """
    cand.resume_path, cand.cover_letter_path = resume, cover
    if cfg.get("require_attachments", True) and not cand.materials_ready:
        _mark(session, item, "blocked", reason="no_attachments")
        return "blocked"

    _WORKER["current"] = {"queue_id": item.id, "title": job.title,
                          "company": job.company, "step": "preview"}
    payload = om._preview_payload(session, cand, cfg, dead)
    blocked = payload.get("blocked")
    if blocked in HARD_BLOCKS:
        _mark(session, item, "blocked", reason=blocked)
        return "blocked"
    item.subject = payload.get("subject")
    item.body = payload.get("body")
    item.preview_hash = payload.get("preview_hash")
    item.resume_path = resume
    item.cover_letter_path = cover
    item.prepared_at = _utc_now()
    _mark(session, item, "ready")
    logger.info(f"[mail_agent] ready: {job.title!r} at {job.company!r} -> {cand.recipient}")
    return "ready"


def _prepare_worker(cfg: dict, limit: int, use_llm: bool) -> None:
    """Prepare every queued row, one at a time.

    A failing row is recorded and the run continues: the whole point of a queue
    is that one unreachable company site does not strand the other forty rows.
    Progress is written after every row so /status is live rather than a
    before/after pair.
    """
    st = _WORKER
    try:
        session = get_session()
        try:
            ids = [r[0].id for r in _rows(session, status="queued")][:limit]
        finally:
            session.close()
        st.update(total=len(ids), done=0, phase="preparing")
        dead = om.load_dead()

        for item_id in ids:
            if st["cancel"]:
                st["phase"] = "cancelled"
                logger.info(f"[mail_agent] cancelled after {st['done']}/{st['total']}")
                return
            session = get_session()
            try:
                outcome = _prepare_one(session, item_id, cfg, dead, use_llm)
                if outcome == "ready":
                    st["prepared"] += 1
                elif outcome == "blocked":
                    st["blocked"] += 1
            except Exception as e:
                logger.exception(f"[mail_agent] preparing queue row {item_id} failed")
                st["failed"] += 1
                try:
                    session.rollback()
                    item = (session.query(MailQueueItem)
                            .filter(MailQueueItem.id == item_id).first())
                    if item is not None:
                        _mark(session, item, "failed", reason="prepare_error",
                              error=str(e)[:2000])
                except Exception:  # pragma: no cover - defensive
                    logger.exception("[mail_agent] could not record the failure")
            finally:
                st["done"] += 1
                session.close()
        st["phase"] = "done"
    except Exception as e:  # pragma: no cover - the run itself, not one row
        logger.exception("[mail_agent] prepare run failed")
        st.update(phase="error", error=str(e))
    finally:
        st["active"] = False
        st["current"] = None


class PrepareRequest(BaseModel):
    """`use_llm` is off by default — see the module docstring's cost guard."""

    limit: int = _PREPARE_MAX
    use_llm: bool = False


@router.post("/prepare")
def start_prepare(payload: PrepareRequest | None = None) -> dict:
    """Start preparing queued rows in the background.

    Sending being disabled is NOT checked here: preparing is not sending, and a
    user with mail.outreach.enabled=false still wants the previews to read.
    """
    payload = payload or PrepareRequest()
    with _WORKER_LOCK:
        if _running():
            raise HTTPException(status_code=409, detail="a prepare run is already active")
        cfg = om._outreach_cfg()
        session = get_session()
        try:
            queued = _counts_by_status(session).get("queued", 0)
        finally:
            session.close()
        limit = max(1, min(int(payload.limit or _PREPARE_MAX), _PREPARE_MAX))
        _WORKER.update(active=True, cancel=False, phase="starting", done=0,
                       total=min(queued, limit), current=None, error=None,
                       prepared=0, blocked=0, failed=0,
                       started_at=_iso(_naive_utc(_utc_now())))
        t = threading.Thread(target=_prepare_worker, args=(cfg, limit, payload.use_llm),
                             name="jobpilot-mail-agent", daemon=True)
        _WORKER["thread"] = t
        t.start()
    logger.info(f"[mail_agent] prepare started over {min(queued, limit)} queued row(s)")
    return {"started": True, "total": min(queued, limit)}


@router.post("/stop")
def stop_prepare() -> dict:
    """Cooperative cancel — the flag is read between rows, never mid-row.

    Killing the thread would leave a half-written .docx on disk and a row stuck
    in `preparing`, which the next run would then skip forever.
    """
    if not _running():
        return {"stopped": False, "reason": "not running"}
    _WORKER["cancel"] = True
    return {"stopped": True, "note": "finishing the current job, then halting"}


@router.get("/status")
def status() -> dict:
    cfg = om._outreach_cfg()
    session = get_session()
    try:
        return {
            "active": _running(),
            "phase": _WORKER["phase"],
            "done": _WORKER["done"],
            "total": _WORKER["total"],
            "current": _WORKER["current"],
            "prepared": _WORKER["prepared"],
            "blocked": _WORKER["blocked"],
            "failed": _WORKER["failed"],
            "error": _WORKER["error"],
            "counts_by_status": _counts_by_status(session),
            "cap": _cap(session, cfg),
            "started_at": _WORKER["started_at"],
            "sending_enabled": bool(cfg.get("enabled")),
            # Last bounce recheck, so the UI can show "2 bounced, 1 re-addressed"
            # next to the queue instead of tracking it itself.
            "last_recheck": _RECHECK["last_recheck"],
            "bounced": _RECHECK["bounced"],
            "retried": _RECHECK["retried"],
        }
    finally:
        session.close()


# --------------------------------------------------------------------------
# Bounce recheck — the second address for a row whose first one bounced
# --------------------------------------------------------------------------

def _scan_bounces(hours: int, limit: int) -> dict:
    """utils.bounce_watch.scan_bounces, which must never fail the request.

    It already swallows its own IMAP errors, but it is read from a sibling
    module and this endpoint's real work — re-resolving rows against the dead
    list on disk — is still worth doing when the scan itself found nothing.
    """
    try:
        return dict(scan_bounces(hours=hours, limit=limit) or {})
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[mail_agent] bounce scan failed: {e}")
        return {"scanned": 0, "new_dead": 0, "error": str(e)}


def _retry_row(session, item: MailQueueItem, app_obj: Application, job: Job | None,
               score: JobScore | None, cfg: dict, dead: set[str]) -> str:
    """Re-address one row whose recipient bounced. Returns the new status.

    Materials are REUSED, never regenerated: the CV was tailored to the JOB, not
    to the mailbox, so re-tailoring after a bounce would spend ~3.5 minutes to
    produce the same document. This is the whole reason prepare now generates
    materials before it looks for an address.

    Two refusals come before anything is cleared, because both are permanent:
    a suppressed address is never retried at all (the human asked us to stop
    mailing them — finding a second inbox at the same company is the opposite of
    honouring that), and a row that has already spent its
    `mail.outreach.max_retries` stops for good, so a company with a broken mail
    server is not re-resolved on every recheck forever. `attempts` counts
    ATTEMPTS, so the first prepare is attempt 1 and the retries spent so far are
    attempts - 1.
    """
    old = (item.recipient or "").strip()
    if om._is_suppressed(session, old):
        _mark(session, item, "blocked", reason="suppressed",
              error=f"{old} is suppressed; not retried")
        return "blocked"
    retries_done = max(0, (item.attempts or 0) - 1)
    max_retries = _int(cfg, "max_retries")
    if retries_done >= max_retries:
        _mark(session, item, "blocked", reason="retries_exhausted",
              error=f"{retries_done} retry/retries already spent "
                    f"(mail.outreach.max_retries={max_retries})")
        return "blocked"
    if job is None:
        _mark(session, item, "blocked", reason="no_job")
        return "blocked"

    # Clear the dead address AND the preview that named it. Leaving the old
    # subject/body/hash in place would leave a row whose stored bytes address a
    # mailbox that does not exist — and whose hash would then refuse the send.
    item.status = "preparing"
    item.recipient = None
    item.recipient_source = None
    item.subject = None
    item.body = None
    item.preview_hash = None
    item.block_reason = None
    item.error = None
    item.attempts = (item.attempts or 0) + 1
    session.commit()

    cand, reason = _gather_recipient(session, app_obj, job, score, dead=dead,
                                     use_llm=False, exclude=old)
    if reason or not cand.recipient:
        _mark(session, item, "blocked", reason="no_alternate_address",
              error=reason or f"no address other than {old}")
        return "blocked"

    item.recipient = cand.recipient
    item.recipient_source = cand.recipient_source
    session.commit()
    blocked = _screen_address(session, cand.recipient, dead)
    if blocked:
        # Stored before screening on purpose: the card should show WHICH second
        # address was found and why it was refused.
        _mark(session, item, "blocked", reason=blocked)
        return "blocked"

    resume = item.resume_path or app_obj.resume_path
    cover = item.cover_letter_path or app_obj.cover_letter_path
    status = _finalize(session, item, cand, job, cfg, dead, resume, cover)
    if status == "ready":
        logger.info(f"[mail_agent] re-addressed {job.company!r}: {old} bounced -> "
                    f"{cand.recipient} (attempt {item.attempts})")
    return status


class RecheckRequest(BaseModel):
    """Defaults match utils.bounce_watch.scan_bounces' own."""

    hours: int = Field(48, ge=1, le=2160)
    limit: int = Field(40, ge=1, le=500)


@router.post("/recheck")
def recheck_bounces(payload: RecheckRequest | None = None) -> dict:
    """Scan for bounces, then re-address every queue row that just went dead.

    This pays off ACROSS rows rather than within one. A resolved company address
    (careers@acme.com) is typically the recipient of every queued job at that
    company, so one bounce invalidates all of them and one recheck repairs all
    of them — which is why the row set is "recipient is on the dead list" rather
    than "this row bounced".

    Nothing is sent here. A repaired row goes back to `ready` and waits for the
    user, and the send it eventually gets is a NEW send through /send: every
    guard re-runs and the OutreachSend ledger records the attempt, because a
    retry to a different mailbox is not a replay of the first message.

    One boundary is deliberate and worth naming: a row that ALREADY sent is not
    retried. Its company+role dedup key is spent, the ledger's UNIQUE constraint
    would refuse a second claim, and quietly re-addressing it would be a second
    email about one role. Such a row keeps its `sent` status and gets
    block_reason "bounced" so the card stops claiming a clean delivery. Freeing
    that key is the ledger's business (outreach_mail), not this endpoint's.
    """
    payload = payload or RecheckRequest()
    if _running():
        # A prepare run is writing the same rows. Two writers would race on
        # status and one would overwrite the other's recipient.
        raise HTTPException(status_code=409,
                            detail="a prepare run is active; stop it first")
    cfg = om._outreach_cfg()
    scan = _scan_bounces(payload.hours, payload.limit)
    dead = {a.lower() for a in om.load_dead()}

    results: list[dict] = []
    bounced = retried = blocked = failed = 0
    session = get_session()
    try:
        for item, app_obj, job, score in _rows(session):
            addr = (item.recipient or "").strip()
            if not addr or addr.lower() not in dead:
                continue
            if item.status in ("preparing", "sending"):
                continue  # mid-flight elsewhere; never rewrite it underneath
            bounced += 1
            # Idempotent, and it makes the row's own address permanent on the
            # dead list even when the scan learned it from another mailbox.
            try:
                mark_dead(addr)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"[mail_agent] could not record {addr} as dead: {e}")
            dead.add(addr.lower())

            if item.status == "sent":
                # History, not work: the ledger holds this send and its dedup
                # key is spent, so there is no retry to make. Recording the
                # bounce on the card is still the honest thing — rewriting the
                # row's status would destroy the record that it went out.
                if item.block_reason != "bounced":
                    item.block_reason = "bounced"
                    item.error = f"delivery to {addr} bounced"
                    session.commit()
                results.append({"id": item.id, "application_id": item.application_id,
                                "bounced_from": addr, "status": "sent",
                                "recipient": item.recipient,
                                "block_reason": "bounced"})
                continue
            try:
                status = _retry_row(session, item, app_obj, job, score, cfg, dead)
            except Exception as e:
                logger.exception(f"[mail_agent] recheck of queue row {item.id} failed")
                session.rollback()
                _mark(session, item, "failed", reason="recheck_error",
                      error=str(e)[:2000])
                status = "failed"
            retried += int(status == "ready")
            blocked += int(status == "blocked")
            failed += int(status == "failed")
            results.append({"id": item.id, "application_id": item.application_id,
                            "bounced_from": addr, "status": status,
                            "recipient": item.recipient,
                            "block_reason": item.block_reason})

        _RECHECK.update(last_recheck=_iso(_naive_utc(_utc_now())), bounced=bounced,
                        retried=retried, blocked=blocked,
                        scanned=int(scan.get("scanned") or 0),
                        new_dead=int(scan.get("new_dead") or 0))
        logger.info(f"[mail_agent] recheck: {bounced} row(s) on a dead address, "
                    f"{retried} re-addressed, {blocked} blocked, {failed} failed")
        return {"scanned": _RECHECK["scanned"], "new_dead": _RECHECK["new_dead"],
                "scan_error": scan.get("error"), "bounced": bounced,
                "retried": retried, "blocked": blocked, "failed": failed,
                "last_recheck": _RECHECK["last_recheck"], "results": results,
                "counts_by_status": _counts_by_status(session),
                "cap": _cap(session, cfg)}
    finally:
        session.close()


# --------------------------------------------------------------------------
# Send
# --------------------------------------------------------------------------

def _candidate_from_row(item: MailQueueItem, app_obj: Application, job: Job,
                        score: JobScore | None, cfg: dict) -> om._Candidate:
    """The prepared row, as the candidate outreach_mail's send path expects.

    Nothing is re-derived from the posting: the recipient, subject and body are
    read back exactly as they were written at prepare time, because those are
    the bytes the user previewed and approved.
    """
    return om._Candidate(
        application_id=item.application_id, job_id=job.id,
        title=job.title or "", company=job.company or "",
        company_normalized=normalize_company_name(job.company) or "",
        location=job.location or "", url=job.url or "", post_url=job.url or "",
        source=job.source or "", external_id=str(job.source_id or ""),
        fit_score=score.fit_score if score else None,
        dedup_key=om.outreach_dedup_key(job.company or "", job.title or "",
                                        str(job.source_id or "")),
        recipient=item.recipient, recipient_source=item.recipient_source or "post_text",
        channel="email", contact={},
        resume_path=item.resume_path or app_obj.resume_path,
        cover_letter_path=item.cover_letter_path or app_obj.cover_letter_path,
        subject=item.subject or "", body=item.body or "",
        headers=om._unsubscribe_headers(cfg))


class SendRequest(BaseModel):
    """`confirm` has no usable default — a send is never the default."""

    ids: list[int] = Field(default_factory=list)
    confirm: str = ""


@router.post("/send")
def send_ready(payload: SendRequest) -> dict:
    """Send prepared rows. Only `ready` rows, only with confirm == "SEND".

    Deliberately synchronous and bounded by mail.outreach.max_batch rather than
    threaded like /prepare: the caller is a human clicking a button on rows they
    just read, and the daily cap plus max_batch already bound the volume. The
    per-message pacing worker lives in outreach_mail for the unattended path.

    Per-row outcomes are 200 with sent:false — a blocked row is a normal result
    the UI renders next to the card, not a transport error.
    """
    cfg = om._outreach_cfg()
    om._require_confirm(payload.confirm)
    om._require_enabled(cfg)
    ids = payload.ids or []
    if not ids:
        raise HTTPException(status_code=422, detail="ids must not be empty")
    if len(ids) > _int(cfg, "max_batch"):
        raise HTTPException(status_code=422,
                            detail=f"at most {_int(cfg, 'max_batch')} rows per send")
    om._require_mailer()

    results: list[dict] = []
    session = get_session()
    try:
        if _lo_scan_optouts is not None:
            try:
                _lo_scan_optouts(hours=_int(cfg, "scan_optouts_hours"))
            except Exception as e:
                logger.warning(f"[mail_agent] opt-out scan failed: {e}")

        found = {r[0].id: r for r in _rows(session, ids=ids)}
        for qid in ids:
            row = found.get(qid)
            if row is None:
                results.append({"id": qid, "sent": False, "refused": "not_found"})
                continue
            item, app_obj, job, score = row
            if item.status != "ready":
                # The preview-first rule is enforced by this column. A row that
                # was never prepared has no reviewed body, so there is nothing a
                # human could have approved.
                results.append({"id": qid, "sent": False, "refused": "not_ready",
                                "status": item.status})
                continue
            if job is None:
                results.append({"id": qid, "sent": False, "refused": "no_job"})
                continue

            cand = _candidate_from_row(item, app_obj, job, score, cfg)
            expected = om._preview_hash(cand.dedup_key, cand.recipient or "",
                                        cand.subject, cand.body)
            if expected != (item.preview_hash or ""):
                # The stored bytes no longer hash to the value written at
                # prepare time, i.e. something edited the row behind the
                # preview. Re-prepare rather than send an unreviewed body.
                _mark(session, item, "blocked", reason="preview_stale")
                results.append({"id": qid, "sent": False, "refused": "preview_stale",
                                "expected": expected})
                continue

            item.status = "sending"
            session.commit()
            try:
                res = om._send_one(session, cand, cfg)
            except Exception as e:
                logger.exception(f"[mail_agent] send failed for queue row {qid}")
                session.rollback()
                _mark(session, item, "failed", reason="send_error", error=str(e)[:2000])
                results.append({"id": qid, "sent": False, "error": str(e)})
                continue

            if res.get("sent"):
                item.status = "sent"
                item.sent_at = _utc_now()
                item.block_reason = None
                item.error = None
            elif res.get("blocked"):
                item.status = "blocked"
                item.block_reason = res["blocked"]
            else:
                item.status = "failed"
                item.block_reason = None
                item.error = res.get("error")
            item.outreach_send_id = res.get("send_id")
            session.commit()
            results.append({"id": qid, "application_id": item.application_id,
                            "to": cand.recipient, **res})

        sent = sum(1 for r in results if r.get("sent"))
        logger.info(f"[mail_agent] send: {sent}/{len(results)} delivered")
        return {"results": results, "sent": sent,
                "counts_by_status": _counts_by_status(session),
                "cap": _cap(session, cfg)}
    finally:
        session.close()
