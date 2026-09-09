"""Controlled batch outreach API. Dry-run is the default; every send is claimed
in the database before SMTP opens.

The user asked for an "auto mail bomb". This module is controlled batch outreach
instead, and the difference is not politeness — it is the only version that works:

- Gmail (SMTP app password, utils/mailer.py) is the sender's *personal* mailbox.
  Unsolicited bulk mail to unverified, regex-scraped addresses produces bounces
  and spam complaints, which is exactly the signal Google uses to rate-limit or
  suspend an account. A suspended Gmail kills the whole JobPilot pipeline
  (auto-apply email, agents/email_reader, utils/bounce_watch), not just this
  feature.
- An identical body sent 200 times is the single strongest bulk-mail fingerprint.
  Per-job tailoring (its own CV + cover letter from agents/tailor.py) is therefore
  a deliverability mechanism, not a nicety.
- Addresses harvested from public LinkedIn posts are personal data. Mailing each
  one *once*, about *one specific job*, with materials actually written for that
  job, is a defensible personal job search. Mailing the same careers@ five times
  because five jobs matched is a bomb.

WHY THIS FILE OWNS THE GUARDRAILS RATHER THAN THE UI
----------------------------------------------------
`agents/auto_applier/runner` caps guessed addresses with a module-level dict, so
its real ceiling is `cap x processes x restarts` and a restart resets the day to
zero. `POST /api/application/{id}/email` bypasses the cap, is_safe_recipient and
the dead list entirely. Neither is reusable as a programmatic send path, so the
four guardrails below are re-implemented here and every one of them is
server-side:

  1. dedup      — `_claim()` INSERTs the UNIQUE dedup_key row and COMMITS it
                  *before* smtplib is imported into the call. A duplicate raises
                  IntegrityError and the send can never reach the network. It is
                  a database constraint, not an `if` someone can refactor away.
  2. daily cap  — `_remaining_today()` counts rows (status='sent',
                  sent_at >= UTC midnight), so it survives restarts and is shared
                  by the scheduler and dashboard processes. Re-read immediately
                  before *every* individual claim, never once per batch.
  3. dry-run    — preview is the default everywhere. A send needs an explicit
                  POST naming the row, `confirm == "SEND"`, and a preview_hash
                  matching the exact bytes being sent. A body the user never
                  previewed produces a 409.
  4. screening  — is_safe_recipient / bounce_watch dead list / OutreachSuppression
                  are checked at extraction time AND again inside `_send_one`,
                  after any hand-edit of `to`. A row failing any of them is
                  recorded as status='blocked' and never sent.

A blocked row must not consume the dedup key — the user may fix the cause
(prepare the missing CV, lift a suppression) and legitimately send later — so
blocked rows are written with a `<key>#b<nonce>` sentinel key. The canonical key
is `value.split("#", 1)[0]`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, nulls_last
from sqlalchemy.exc import IntegrityError

from db.database import get_session, record_status_change
from db.models import (Application, ApplicationStatus, Job, JobScore,
                       OutreachSend, OutreachSuppression)
from utils.bounce_watch import load_dead
from utils.company_names import normalize_company_name
from utils.mailer import (PREFERRED_LOCAL, is_safe_recipient, mailer_ready,
                          send_application_email)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/outreach", tags=["outreach"])

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# utils/linkedin_outreach.py is a sibling deliverable. Importing it eagerly at
# module scope would make `server/dashboard.py` fail to boot if that file is
# missing or half-written, taking the whole dashboard down with it — so the
# import is guarded and every entry point has a regex-only fallback below.
# The fallbacks are deliberately conservative: they find fewer addresses than
# the real extractor, never more.
# --------------------------------------------------------------------------
try:  # pragma: no cover - exercised by whichever half of the branch is live
    from utils import linkedin_outreach as _lo
except Exception as _e:  # ImportError, or a SyntaxError while it is being written
    _lo = None
    logger.info(f"[outreach] utils.linkedin_outreach unavailable ({_e}); "
                f"using built-in regex fallbacks")

# Bound at import so tests (and a later hot-fix) can monkeypatch these names on
# this module rather than reaching into utils.linkedin_outreach.
_lo_extract = (getattr(_lo, "extract_application_contact", None)
               or getattr(_lo, "extract_contact", None)) if _lo else None
_lo_dedup_key = getattr(_lo, "outreach_dedup_key", None) if _lo else None
_lo_build_body = getattr(_lo, "build_outreach_body", None) if _lo else None
_lo_subject_for = getattr(_lo, "subject_for", None) if _lo else None
_lo_is_suppressed = getattr(_lo, "is_suppressed", None) if _lo else None
_lo_scan_optouts = getattr(_lo, "scan_optouts", None) if _lo else None


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

#: mail.outreach defaults. Every value is a *ceiling*, so a missing or malformed
#: settings.yaml degrades to the safest configuration rather than an unlimited
#: one — `enabled: False` in particular means a broken config sends nothing.
OUTREACH_DEFAULTS: dict = {
    "enabled": False,
    "daily_cap": 8,
    "max_batch": 10,
    "max_per_company_per_day": 1,
    "per_recipient_cooldown_days": 30,
    "min_seconds_between_sends": 45,
    "require_attachments": True,
    "reply_to": "",
    "list_unsubscribe_header": True,
    "opt_out_line": True,
    "scan_optouts_hours": 168,
}

LINKEDIN_SOURCES = ("linkedin_unipile", "linkedin_post_unipile",
                    "linkedin_brightdata", "linkedin_post_brightdata")

#: Order matters — `blocked` is the FIRST failing reason, and the UI renders it
#: as the single thing standing between this row and a send.
BLOCK_ORDER = ("already_emailed", "suppressed", "dead_address", "unsafe_recipient",
               "cooldown", "company_capped", "no_recipient", "no_attachments")

_PREVIEW_BATCH_MAX = 25


def _cfg() -> dict:
    """settings.yaml as a dict, or {} if unreadable.

    A local copy of dashboard._load_settings rather than an import: this router
    must not depend on server.dashboard, which imports it.
    """
    try:
        with open(PROJECT_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"[outreach] could not read settings.yaml: {e}")
        return {}


def _outreach_cfg() -> dict:
    """mail.outreach merged over OUTREACH_DEFAULTS. Never raises."""
    raw = ((_cfg().get("mail") or {}).get("outreach") or {})
    out = dict(OUTREACH_DEFAULTS)
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if k in OUTREACH_DEFAULTS})
    return out


def _int(cfg: dict, key: str) -> int:
    try:
        return int(cfg.get(key, OUTREACH_DEFAULTS[key]))
    except (TypeError, ValueError):
        return int(OUTREACH_DEFAULTS[key])


# --------------------------------------------------------------------------
# Time. SQLite's DATETIME storage drops tzinfo, so rows read back are naive-UTC.
# Every comparison goes through _naive_utc so an in-session tz-aware value and a
# freshly-loaded naive one compare correctly.
# --------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _utc_midnight() -> datetime:
    now = _utc_now()
    return datetime(now.year, now.month, now.day)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _abs(path: str | None) -> Path | None:
    if not path:
        return None
    p = Path(path)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def _exists(path: str | None) -> bool:
    p = _abs(path)
    try:
        return bool(p and p.is_file())
    except OSError:
        return False


def _size(path: str | None) -> int | None:
    p = _abs(path)
    try:
        return p.stat().st_size if p and p.is_file() else None
    except OSError:
        return None


# --------------------------------------------------------------------------
# Extraction — thin adapters over utils.linkedin_outreach with regex fallbacks
# --------------------------------------------------------------------------

_BLANK_CONTACT = {
    "address": None, "candidates": [], "rejected": [], "channel": "unknown",
    "instructions": "", "subject_hint": "", "deadline_hint": "",
    "confidence": 0.0, "source": None, "obfuscated": False,
}


def _fallback_extract(text: str | None, *, company: str = "", job_title: str = "",
                      dead: set[str] | None = None, use_llm: bool = False) -> dict:
    """Regex-only application contact — the degraded path.

    Only used when utils.linkedin_outreach is not importable. It finds strictly
    fewer addresses than the real extractor (no de-obfuscation, no instruction
    reading) but applies the SAME two screens, so it can never surface an address
    the real one would have rejected.
    """
    out = dict(_BLANK_CONTACT)
    out["candidates"], out["rejected"] = [], []
    if not text:
        return out
    import re
    dead = dead if dead is not None else load_dead()
    seen, safe = set(), []
    for raw in re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text):
        addr = raw.lower().rstrip(".,;:)]}>\"'")
        if addr in seen:
            continue
        seen.add(addr)
        if not is_safe_recipient(addr):
            out["rejected"].append({"address": addr, "reason": "blocked_local"})
        elif addr in dead:
            out["rejected"].append({"address": addr, "reason": "dead"})
        else:
            safe.append(addr)
    norm_company = "".join(ch for ch in (company or "").lower() if ch.isalnum())

    def _rank(a: str) -> tuple[int, int]:
        local, _, domain = a.partition("@")
        pref = 0 if any(t in local for t in PREFERRED_LOCAL) else 1
        label = domain.split(".")[0] if domain else ""
        same = 0 if (norm_company and norm_company.startswith(label) and label) else 1
        return (pref, same)

    safe.sort(key=_rank)
    out["candidates"] = safe
    out["address"] = safe[0] if safe else None
    if out["address"]:
        out["channel"], out["confidence"], out["source"] = "email", 0.55, "regex"
        out["instructions"] = _sentence_around(text, out["address"])
    return out


def _sentence_around(text: str, address: str) -> str:
    """The sentence containing the address, capped — the regex-only baseline for
    'what did the poster actually ask for'."""
    low = text.find(address)
    if low < 0:
        return ""
    start = max(text.rfind(".", 0, low), text.rfind("\n", 0, low)) + 1
    end = len(text)
    for stop in (". ", "\n"):
        i = text.find(stop, low)
        if i != -1:
            end = min(end, i + 1)
    return " ".join(text[start:end].split())[:400]


def _normalize_contact(raw) -> dict:
    """Coerce whatever the extractor returned into the stable candidate shape.

    utils.linkedin_outreach is written concurrently and its two documented entry
    points disagree (`extract_application_contact` returns the full dict,
    `extract_contact` returns a 4-key dict or None), so nothing here indexes a
    key directly.
    """
    out = dict(_BLANK_CONTACT)
    out["candidates"], out["rejected"] = [], []
    if not isinstance(raw, dict):
        return out
    addr = (raw.get("address") or "").strip().lower() or None
    out["address"] = addr
    cands = raw.get("candidates")
    out["candidates"] = [str(a).lower() for a in cands] if isinstance(cands, list) else (
        [addr] if addr else [])
    rej = raw.get("rejected")
    out["rejected"] = [r for r in rej if isinstance(r, dict)] if isinstance(rej, list) else []
    ch = raw.get("channel")
    out["channel"] = str(ch) if ch else ("email" if addr else "unknown")
    for key, cap in (("instructions", 400), ("subject_hint", 200), ("deadline_hint", 120)):
        out[key] = str(raw.get(key) or "")[:cap]
    try:
        out["confidence"] = float(raw.get("confidence") or 0.0)
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    src = raw.get("source")
    out["source"] = str(src) if src else None
    out["obfuscated"] = bool(raw.get("obfuscated"))
    # A hand-off between two modules is exactly where a screen gets skipped:
    # re-run both here so the router never trusts an upstream address.
    if out["address"] and not is_safe_recipient(out["address"]):
        out["rejected"].append({"address": out["address"], "reason": "blocked_local"})
        out["address"] = None
    return out


def extract_application_contact(text: str | None, *, company: str = "",
                                job_title: str = "", dead: set[str] | None = None,
                                use_llm: bool = True) -> dict:
    """The router's single extraction entry point. Never raises."""
    fn = _lo_extract
    if fn is None:
        return _fallback_extract(text, company=company, job_title=job_title,
                                 dead=dead, use_llm=use_llm)
    try:
        try:
            raw = fn(text, company=company, job_title=job_title, dead=dead,
                     use_llm=use_llm)
        except TypeError:
            # The narrower `extract_contact(text, *, company=None)` contract.
            raw = fn(text, company=company)
    except Exception as e:
        logger.warning(f"[outreach] extraction failed, falling back to regex: {e}")
        return _fallback_extract(text, company=company, job_title=job_title,
                                 dead=dead, use_llm=False)
    return _normalize_contact(raw)


def outreach_dedup_key(company: str, job_title: str, external_id: str | None = None) -> str:
    """Identity of "this company, this job" — the value the UNIQUE column holds."""
    if _lo_dedup_key is not None:
        try:
            return str(_lo_dedup_key(company, job_title, external_id or ""))
        except Exception as e:
            logger.warning(f"[outreach] dedup key helper failed ({e}); using local hash")
    norm = lambda s: " ".join((s or "").lower().split())  # noqa: E731
    basis = f"{norm(company)}|{norm(job_title)}|{(external_id or '').strip().lower()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _canonical_key(value: str | None) -> str:
    """Strip the `#b<nonce>` sentinel a blocked row carries."""
    return (value or "").split("#", 1)[0]


def _blocked_key(dedup_key: str) -> str:
    """A unique, obviously-non-canonical key for a blocked row.

    A blocked row is not a send, so it must not burn the dedup key: the user can
    prepare the missing CV or lift the suppression and then legitimately send.
    """
    return f"{dedup_key}#b{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------------------
# Body / subject
# --------------------------------------------------------------------------

def _opt_out_line(company: str) -> str:
    return ("If you'd rather not receive messages like this, reply STOP and I "
            f"won't contact you or anyone at {company or 'your company'} again.")


def _fallback_body(*, sender_name: str, job_title: str, company: str, contact: dict,
                   links: dict | None = None, post_url: str = "",
                   opt_out_line: str = "") -> str:
    """Per-job body used when utils.linkedin_outreach is unavailable.

    Deliberately NOT utils.mailer.default_body: that one hardcodes a fixed
    self-description, which would make every message in a batch byte-identical
    apart from the title — the exact bulk fingerprint this feature exists to
    avoid. The opening sentence names where the job was seen, which is what makes
    the mail solicited-in-context rather than cold.
    """
    links = links or {}
    where = f" at {company}" if company else ""
    seen = f"I saw your post about the {job_title} role{where}"
    if post_url:
        seen += f" ({post_url})"
    parts = [f"Dear Hiring Team,\n\n{seen}, and I would like to apply."]
    instructions = (contact or {}).get("instructions") or ""
    if (contact or {}).get("subject_hint"):
        parts.append(f"Reference: {contact['subject_hint']}.")
    if instructions:
        parts.append("I have followed the instructions in the post.")
    parts.append("My resume and cover letter, both written for this role, are attached.")
    parts.append("Thank you for your time and consideration.")
    tail = []
    if links.get("linkedin"):
        tail.append(f"LinkedIn: {links['linkedin']}")
    if links.get("github"):
        tail.append(f"GitHub: {links['github']}")
    body = "\n\n".join(parts) + f"\n\nKind regards,\n{sender_name}"
    if tail:
        body += "\n" + "\n".join(tail)
    if opt_out_line:
        body += f"\n\n{opt_out_line}"
    return body + "\n"


def build_outreach_body(**kwargs) -> str:
    if _lo_build_body is not None:
        try:
            return str(_lo_build_body(**kwargs))
        except Exception as e:
            logger.warning(f"[outreach] body builder failed ({e}); using local body")
    return _fallback_body(**kwargs)


def subject_for(job_title: str, company: str, sender_name: str, contact: dict) -> str:
    """A subject line the post explicitly demanded wins verbatim — ignoring an
    explicit instruction is the fastest way into a spam filter."""
    if _lo_subject_for is not None:
        try:
            return str(_lo_subject_for(job_title, company, sender_name, contact))
        except Exception as e:
            logger.warning(f"[outreach] subject helper failed ({e}); using local subject")
    hint = ((contact or {}).get("subject_hint") or "").strip()
    if hint:
        return f"{hint} — {sender_name}" if sender_name else hint
    where = f" at {company}" if company else ""
    return f"{job_title}{where} — {sender_name}" if sender_name else f"{job_title}{where}"


def _profile() -> dict:
    """config/applicant_profile.yaml, or {} — never fatal to a preview."""
    try:
        from server.autofill import load_profile
        return load_profile() or {}
    except Exception as e:
        logger.warning(f"[outreach] profile unavailable: {e}")
        return {}


def _sender_email() -> str:
    try:
        from agents.email_reader import load_gmail_config
        return (load_gmail_config() or {}).get("address") or ""
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Ledger queries — the cap and the screens, all counted from rows
# --------------------------------------------------------------------------

def _sent_today(session) -> int:
    return int(session.query(func.count(OutreachSend.id))
               .filter(OutreachSend.status == "sent",
                       OutreachSend.sent_at >= _utc_midnight())
               .scalar() or 0)


def _remaining_today(session, cfg: dict) -> int:
    """Cap remaining, counted from the DB right now.

    Read again immediately before every individual claim — not once per batch —
    because two processes share this table and a batch takes minutes.
    """
    return max(0, _int(cfg, "daily_cap") - _sent_today(session))


def _company_sent_today(session, company_normalized: str) -> int:
    return int(session.query(func.count(OutreachSend.id))
               .filter(OutreachSend.status == "sent",
                       OutreachSend.company_normalized == (company_normalized or ""),
                       OutreachSend.sent_at >= _utc_midnight())
               .scalar() or 0)


def _last_send_to(session, recipient: str) -> datetime | None:
    row = (session.query(OutreachSend)
           .filter(OutreachSend.status == "sent",
                   OutreachSend.recipient == (recipient or "").lower())
           .order_by(OutreachSend.sent_at.desc()).first())
    return _naive_utc(row.sent_at) if row else None


def _cooldown_active(session, recipient: str, days: int) -> bool:
    """Stops five jobs at one company becoming five mails to the same careers@."""
    if days <= 0 or not recipient:
        return False
    last = _last_send_to(session, recipient)
    return bool(last and last >= _naive_utc(_utc_now()) - timedelta(days=days))


def _is_suppressed(session, address: str) -> bool:
    """Fail CLOSED: a STOP request outranks everything else in this module.

    Checks scope='address' on the full address AND scope='domain' on its domain,
    so "don't contact anyone at Acme" is one row rather than fifty. The
    utils.linkedin_outreach helper is consulted as well and can only ADD
    suppressions — it is never allowed to clear one found here.
    """
    addr = (address or "").strip().lower()
    if not addr:
        return False
    domain = addr.split("@", 1)[1] if "@" in addr else ""
    q = session.query(OutreachSuppression).filter(
        ((OutreachSuppression.scope == "address") & (OutreachSuppression.value == addr))
        | ((OutreachSuppression.scope == "domain") & (OutreachSuppression.value == domain)))
    if q.first() is not None:
        return True
    if _lo_is_suppressed is not None:
        try:
            return bool(_lo_is_suppressed(session, addr))
        except Exception as e:
            logger.warning(f"[outreach] suppression helper failed: {e}")
    return False


def _existing_send(session, dedup_key: str) -> OutreachSend | None:
    """The claimed/sent/failed row holding this key, if any.

    Blocked rows carry the `#b<nonce>` sentinel and are excluded by the exact
    match, which is the point: they do not stop a later legitimate send.
    """
    return (session.query(OutreachSend)
            .filter(OutreachSend.dedup_key == dedup_key).first())


# --------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------

@dataclass
class _Candidate:
    """Everything one send needs, resolved once and re-screened at send time."""

    application_id: int
    job_id: int
    title: str
    company: str
    company_normalized: str
    location: str
    url: str
    post_url: str
    source: str
    external_id: str
    fit_score: int | None
    dedup_key: str
    recipient: str | None
    recipient_source: str
    channel: str
    contact: dict = field(default_factory=dict)
    resume_path: str | None = None
    cover_letter_path: str | None = None
    subject: str = ""
    body: str = ""
    headers: dict = field(default_factory=dict)

    @property
    def materials_ready(self) -> bool:
        return _exists(self.resume_path) and _exists(self.cover_letter_path)


def _recipient_source_for(job_source: str, edited: bool) -> str:
    if edited:
        return "manual"
    return "post_text" if "post" in (job_source or "") else "job_description"


def _build_candidate(session, app_obj: Application, job: Job, score: JobScore | None,
                     *, use_llm: bool, dead: set[str] | None = None) -> _Candidate:
    contact = extract_application_contact(
        job.description, company=job.company or "", job_title=job.title or "",
        dead=dead, use_llm=use_llm)
    return _Candidate(
        application_id=app_obj.id, job_id=job.id,
        title=job.title or "", company=job.company or "",
        company_normalized=normalize_company_name(job.company) or "",
        location=job.location or "", url=job.url or "",
        post_url=job.url or "", source=job.source or "",
        external_id=str(job.source_id or ""),
        fit_score=score.fit_score if score else None,
        dedup_key=outreach_dedup_key(job.company or "", job.title or "",
                                     str(job.source_id or "")),
        recipient=contact.get("address"),
        recipient_source=_recipient_source_for(job.source or "", edited=False),
        channel=contact.get("channel") or "unknown",
        contact=contact,
        resume_path=app_obj.resume_path, cover_letter_path=app_obj.cover_letter_path,
    )


def _first_block(session, cand: _Candidate, cfg: dict, dead: set[str]) -> str | None:
    """The first failing guardrail, in BLOCK_ORDER. None means eligible.

    Advisory only — /candidates renders it. Nothing here is trusted at send time;
    `_send_one` re-runs every one of these checks against the DB.
    """
    checks = {
        "already_emailed": lambda: _existing_send(session, cand.dedup_key) is not None,
        "suppressed": lambda: bool(cand.recipient) and _is_suppressed(session, cand.recipient),
        "dead_address": lambda: bool(cand.recipient) and cand.recipient in dead,
        "unsafe_recipient": lambda: bool(cand.recipient) and not is_safe_recipient(cand.recipient),
        "cooldown": lambda: bool(cand.recipient) and _cooldown_active(
            session, cand.recipient, _int(cfg, "per_recipient_cooldown_days")),
        "company_capped": lambda: _company_sent_today(session, cand.company_normalized)
        >= _int(cfg, "max_per_company_per_day"),
        "no_recipient": lambda: not cand.recipient,
        "no_attachments": lambda: bool(cfg.get("require_attachments", True))
        and not cand.materials_ready,
    }
    for reason in BLOCK_ORDER:
        try:
            if checks[reason]():
                return reason
        except Exception as e:  # a broken screen must not read as "eligible"
            logger.error(f"[outreach] screen {reason} raised: {e}")
            return reason
    return None


@router.get("/candidates")
def list_candidates(limit: int = Query(50, ge=1, le=200),
                    include_sent: bool = False,
                    min_score: int = 0,
                    source: str = ",".join(LINKEDIN_SOURCES)) -> dict:
    """LinkedIn rows that publish an application address. Writes nothing.

    Extraction runs with use_llm=False here — fifty LLM calls to render a list is
    absurd; the model only runs on an individual preview.
    """
    sources = [s.strip() for s in (source or "").split(",") if s.strip()] or list(LINKEDIN_SOURCES)
    cfg = _outreach_cfg()
    session = get_session()
    try:
        dead = load_dead()
        rows = (session.query(Application, Job, JobScore)
                .join(Job, Application.job_id == Job.id)
                .outerjoin(JobScore, JobScore.job_id == Job.id)
                .filter(Job.source.in_(sources))
                .order_by(nulls_last(JobScore.fit_score.desc()), Job.date_found.desc())
                .limit(max(limit * 4, limit)).all())

        counts = {"total": 0, "eligible": 0, "no_recipient": 0,
                  "already_sent": 0, "no_materials": 0}
        out: list[dict] = []
        for app_obj, job, score in rows:
            counts["total"] += 1
            if min_score and (score is None or (score.fit_score or 0) < min_score):
                continue
            cand = _build_candidate(session, app_obj, job, score, use_llm=False, dead=dead)
            prior = _existing_send(session, cand.dedup_key)
            already_sent = prior is not None
            blocked = _first_block(session, cand, cfg, dead)
            if already_sent:
                counts["already_sent"] += 1
                if not include_sent:
                    continue
            if not cand.recipient:
                counts["no_recipient"] += 1
            if not cand.materials_ready:
                counts["no_materials"] += 1
            if blocked is None:
                counts["eligible"] += 1
            if len(out) >= limit:
                continue
            out.append({
                "application_id": cand.application_id, "job_id": cand.job_id,
                "title": cand.title, "company": cand.company,
                "location": cand.location, "url": cand.url, "post_url": cand.post_url,
                "source": cand.source, "fit_score": cand.fit_score,
                "recipient": cand.recipient,
                "recipient_source": cand.recipient_source if cand.recipient else None,
                "channel": cand.channel,
                "instructions": cand.contact.get("instructions", ""),
                "subject_hint": cand.contact.get("subject_hint", ""),
                "confidence": cand.contact.get("confidence", 0.0),
                "rejected": cand.contact.get("rejected", []),
                "materials_ready": cand.materials_ready,
                "resume_path": cand.resume_path,
                "cover_letter_path": cand.cover_letter_path,
                "dedup_key": cand.dedup_key,
                "already_sent": already_sent,
                "blocked": blocked,
                "eligible": blocked is None,
            })
        return {
            "candidates": out,
            "counts": counts,
            "cap": {"daily_cap": _int(cfg, "daily_cap"),
                    "sent_today": _sent_today(session),
                    "remaining": _remaining_today(session, cfg)},
        }
    finally:
        session.close()


# --------------------------------------------------------------------------
# Preview — the dry-run default
# --------------------------------------------------------------------------

def _preview_hash(dedup_key: str, to: str, subject: str, body: str) -> str:
    """One helper, used identically by preview and send.

    The send path recomputes it from the bytes it is about to send, so an edited
    body the user never previewed cannot match.
    """
    return hashlib.sha256(
        f"{dedup_key}|{to or ''}|{subject or ''}|{body or ''}".encode("utf-8")).hexdigest()


def _unsubscribe_headers(cfg: dict) -> dict[str, str]:
    """mailto: only. A one-click header we cannot service would be a lie."""
    sender = _sender_email()
    if cfg.get("list_unsubscribe_header", True) and sender:
        return {"List-Unsubscribe": f"<mailto:{sender}?subject=unsubscribe>"}
    return {}


def _render(cand: _Candidate, cfg: dict) -> None:
    """Fill subject/body/headers on the candidate. Pure; touches no DB, no SMTP."""
    profile = _profile()
    identity = profile.get("identity", {}) or {}
    sender_name = identity.get("full_name") or ""
    cand.subject = subject_for(cand.title, cand.company, sender_name, cand.contact)
    cand.body = build_outreach_body(
        sender_name=sender_name, job_title=cand.title, company=cand.company,
        contact=cand.contact, links=profile.get("links", {}) or {},
        post_url=cand.post_url,
        opt_out_line=_opt_out_line(cand.company) if cfg.get("opt_out_line", True) else "")
    cand.headers = _unsubscribe_headers(cfg)


def _load_candidate(session, application_id: int, *, use_llm: bool,
                    dead: set[str] | None = None) -> _Candidate:
    row = (session.query(Application, Job, JobScore)
           .join(Job, Application.job_id == Job.id)
           .outerjoin(JobScore, JobScore.job_id == Job.id)
           .filter(Application.id == application_id).first())
    if not row:
        raise HTTPException(status_code=404, detail="Application not found")
    app_obj, job, score = row
    return _build_candidate(session, app_obj, job, score, use_llm=use_llm, dead=dead)


def _preview_payload(session, cand: _Candidate, cfg: dict, dead: set[str]) -> dict:
    _render(cand, cfg)
    warnings: list[str] = []
    if cand.contact.get("obfuscated"):
        warnings.append("Address was obfuscated in the post and de-mangled — verify it.")
    if not cand.materials_ready:
        warnings.append("Tailored resume / cover letter missing — run Prepare first.")
    attachments = [
        {"name": Path(p).name, "path": p, "exists": _exists(p), "bytes": _size(p)}
        for p in (cand.resume_path, cand.cover_letter_path) if p
    ]
    return {
        "application_id": cand.application_id,
        "dedup_key": cand.dedup_key,
        "to": cand.recipient,
        "recipient_source": cand.recipient_source if cand.recipient else None,
        "reply_to": (cfg.get("reply_to") or "").strip() or _sender_email(),
        "subject": cand.subject,
        "body": cand.body,
        "attachments": attachments,
        "headers": cand.headers,
        "instructions": cand.contact.get("instructions", ""),
        "confidence": cand.contact.get("confidence", 0.0),
        "source": cand.contact.get("source"),
        "blocked": _first_block(session, cand, cfg, dead),
        "warnings": warnings,
        "preview_hash": _preview_hash(cand.dedup_key, cand.recipient or "",
                                      cand.subject, cand.body),
        "dry_run": True,
    }


@router.get("/preview/{application_id}")
def preview_one(application_id: int, use_llm: bool = True) -> dict:
    """Render the exact subject + body + attachments. WRITES NOTHING."""
    cfg = _outreach_cfg()
    session = get_session()
    try:
        dead = load_dead()
        cand = _load_candidate(session, application_id, use_llm=use_llm, dead=dead)
        return _preview_payload(session, cand, cfg, dead)
    finally:
        session.close()


class PreviewBatchRequest(BaseModel):
    application_ids: list[int] = Field(default_factory=list)
    use_llm: bool = True


@router.post("/preview")
def preview_batch(payload: PreviewBatchRequest) -> dict:
    """Preview many rows. Still writes nothing; LLM calls are serial, hence the cap."""
    ids = payload.application_ids or []
    if len(ids) > _PREVIEW_BATCH_MAX:
        raise HTTPException(status_code=422,
                            detail=f"at most {_PREVIEW_BATCH_MAX} previews per call")
    cfg = _outreach_cfg()
    session = get_session()
    try:
        dead = load_dead()
        previews, errors = [], []
        for aid in ids:
            try:
                cand = _load_candidate(session, aid, use_llm=payload.use_llm, dead=dead)
                previews.append(_preview_payload(session, cand, cfg, dead))
            except HTTPException as e:
                errors.append({"application_id": aid, "error": str(e.detail)})
            except Exception as e:
                logger.exception(f"[outreach] preview {aid} failed")
                errors.append({"application_id": aid, "error": str(e)})
        return {"previews": previews, "errors": errors}
    finally:
        session.close()


# --------------------------------------------------------------------------
# Send
# --------------------------------------------------------------------------

def _record_blocked(session, cand: _Candidate, reason: str) -> dict:
    """Write the ledger row for a refused send and return the per-row result.

    Uses the sentinel key so the block is auditable without consuming the dedup
    key — the cause may be fixable.
    """
    row = OutreachSend(
        dedup_key=_blocked_key(cand.dedup_key), company=cand.company,
        company_normalized=cand.company_normalized, job_title=cand.title,
        external_id=cand.external_id or None, post_url=cand.post_url or None,
        job_id=cand.job_id, application_id=cand.application_id,
        recipient=(cand.recipient or ""), recipient_source=cand.recipient_source,
        channel=cand.channel, subject=cand.subject or None, body=cand.body or None,
        resume_path=cand.resume_path, cover_letter_path=cand.cover_letter_path,
        provider=cand.source or None, status="blocked", block_reason=reason,
        dry_run=False,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:  # pragma: no cover - nonce collision
        session.rollback()
        return {"sent": False, "send_id": None, "blocked": reason,
                "dedup_key": cand.dedup_key, "error": None}
    logger.info(f"[outreach] blocked {cand.company!r}/{cand.title[:40]!r}: {reason}")
    return {"sent": False, "send_id": row.id, "blocked": reason,
            "dedup_key": cand.dedup_key, "to": cand.recipient, "error": None}


def _send_one(session, cand: _Candidate, cfg: dict) -> dict:
    """One tailored email, fully screened, claimed, then sent.

    Ordering is the whole point: every screen and the claim INSERT happen before
    send_application_email is called, so a refused row never opens SMTP.
    """
    # 1. caps — re-read from the DB right now, not from the batch's opening count
    if _remaining_today(session, cfg) <= 0:
        return _record_blocked(session, cand, "cap_reached")
    if _company_sent_today(session, cand.company_normalized) >= _int(cfg, "max_per_company_per_day"):
        return _record_blocked(session, cand, "company_capped")
    if not cand.recipient:
        return _record_blocked(session, cand, "no_recipient")
    if _cooldown_active(session, cand.recipient, _int(cfg, "per_recipient_cooldown_days")):
        return _record_blocked(session, cand, "cooldown")
    # 2. re-screen — the address may have been hand-edited in the UI since preview
    if not is_safe_recipient(cand.recipient):
        return _record_blocked(session, cand, "unsafe_recipient")
    if cand.recipient in load_dead():
        return _record_blocked(session, cand, "dead_address")
    if _is_suppressed(session, cand.recipient):
        return _record_blocked(session, cand, "suppressed")
    if cfg.get("require_attachments", True) and not cand.materials_ready:
        # Refusing here is what forbids an identical bulk body: no tailored
        # materials, no mail.
        return _record_blocked(session, cand, "no_attachments")

    # 3. CLAIM. The unique constraint is the dedup guarantee. Nothing sent yet.
    row = OutreachSend(
        dedup_key=cand.dedup_key, company=cand.company,
        company_normalized=cand.company_normalized, job_title=cand.title,
        external_id=cand.external_id or None, post_url=cand.post_url or None,
        job_id=cand.job_id, application_id=cand.application_id,
        recipient=cand.recipient, recipient_source=cand.recipient_source,
        channel=cand.channel, subject=cand.subject, body=cand.body,
        body_hash=_preview_hash(cand.dedup_key, cand.recipient, cand.subject, cand.body),
        resume_path=cand.resume_path, cover_letter_path=cand.cover_letter_path,
        provider=cand.source or None, status="claimed", dry_run=False,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        logger.info(f"[outreach] duplicate claim refused by the DB for "
                    f"{cand.company!r}/{cand.title[:40]!r}")
        return {"sent": False, "send_id": None, "blocked": "already_emailed",
                "dedup_key": cand.dedup_key, "to": cand.recipient, "error": None}

    # 4. ONLY NOW does SMTP open.
    return _deliver(session, row, cand, cfg)


def _deliver(session, row: OutreachSend, cand: _Candidate, cfg: dict) -> dict:
    """Step 4+5 for an already-claimed row. Shared by send and retry."""
    profile = _profile()
    identity = profile.get("identity", {}) or {}
    kwargs = dict(
        to_addr=cand.recipient, job_title=cand.title, company=cand.company,
        resume_path=str(_abs(cand.resume_path) or ""),
        cover_letter_path=str(_abs(cand.cover_letter_path) or ""),
        sender_name=identity.get("full_name") or "", body=cand.body,
        links=profile.get("links", {}) or {}, self_copy=False,
    )
    extra = dict(reply_to=(cfg.get("reply_to") or "").strip() or None,
                 extra_headers=cand.headers or None)
    try:
        res = send_application_email(**kwargs, **extra)
    except TypeError:
        # utils/mailer.py gains reply_to/extra_headers in the same feature branch;
        # tolerate the pre-change signature rather than failing the send.
        logger.info("[outreach] mailer has no reply_to/extra_headers yet")
        res = send_application_email(**kwargs)
    except Exception as e:
        logger.exception("[outreach] send raised")
        res = {"sent": False, "error": str(e)}
    res = res if isinstance(res, dict) else {"sent": False, "error": "mailer returned no result"}

    sent = bool(res.get("sent"))
    row.status = "sent" if sent else "failed"
    row.sent_at = _utc_now() if sent else None
    row.attachments = res.get("attachments")
    row.error = res.get("error")
    session.commit()

    if sent:
        _stamp_application(session, cand)
    return {"sent": sent, "send_id": row.id, "to": cand.recipient,
            "attachments": res.get("attachments") or [],
            "sent_at": _iso(_naive_utc(row.sent_at)),
            "remaining_today": _remaining_today(session, cfg),
            "blocked": None, "error": res.get("error")}


def _stamp_application(session, cand: _Candidate) -> None:
    """Pipeline bookkeeping — one write path, per db/database.record_status_change.

    The `emailed_at` stamp matters beyond the audit trail: the auto-applier
    refuses to email an application whose auto_apply_log already carries it, so
    writing it here is what stops a later auto-apply run double-mailing the same
    employer.
    """
    app_obj = session.query(Application).get(cand.application_id)
    if not app_obj:
        return
    try:
        record_status_change(session, app_obj, ApplicationStatus.APPLIED,
                             source="outreach", note=f"emailed {cand.recipient}")
        try:
            log = json.loads(app_obj.auto_apply_log or "{}")
            log = log if isinstance(log, dict) else {}
        except (ValueError, TypeError):
            log = {}
        log["emailed_at"] = _utc_now().isoformat()
        app_obj.auto_apply_log = json.dumps(log)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("[outreach] pipeline bookkeeping failed after a successful send")


class SendRequest(BaseModel):
    """`confirm` has no usable default on purpose — a send is never the default."""

    confirm: str = ""
    preview_hash: str = ""
    to: str | None = None
    subject: str | None = None
    body: str | None = None


def _require_confirm(confirm: str) -> None:
    if confirm != "SEND":
        raise HTTPException(status_code=422, detail='confirm must be exactly "SEND"')


def _require_enabled(cfg: dict) -> None:
    if not cfg.get("enabled"):
        raise HTTPException(status_code=403,
                            detail="mail.outreach.enabled is false — the screen is preview-only")


def _require_mailer() -> None:
    ok, why = mailer_ready()
    if not ok:
        raise HTTPException(status_code=400, detail=why or "mailer not configured")


def _apply_edits(cand: _Candidate, edits: SendRequest, cfg: dict) -> None:
    """Fold the human's edits in, then re-render whatever they did not touch."""
    _render(cand, cfg)
    if edits.to is not None:
        new_to = edits.to.strip().lower()
        if new_to != (cand.recipient or ""):
            cand.recipient = new_to or None
            cand.recipient_source = "manual"
    if edits.subject is not None:
        cand.subject = edits.subject
    if edits.body is not None:
        cand.body = edits.body


def _check_hash(cand: _Candidate, supplied: str) -> None:
    expected = _preview_hash(cand.dedup_key, cand.recipient or "", cand.subject, cand.body)
    if (supplied or "") != expected:
        raise HTTPException(status_code=409,
                            detail={"detail": "preview stale — re-preview before sending",
                                    "expected": expected})


@router.post("/send/{application_id}")
def send_one(application_id: int, payload: SendRequest) -> dict:
    """Send ONE previewed, tailored email.

    Blocked and failed outcomes are 200 with `sent: false` — a blocked row is a
    normal per-row outcome the UI renders, not a transport error.
    """
    cfg = _outreach_cfg()
    _require_confirm(payload.confirm)
    _require_enabled(cfg)
    _require_mailer()
    session = get_session()
    try:
        dead = load_dead()
        cand = _load_candidate(session, application_id, use_llm=False, dead=dead)
        _apply_edits(cand, payload, cfg)
        _check_hash(cand, payload.preview_hash)
        return _send_one(session, cand, cfg)
    finally:
        session.close()


class BatchItem(BaseModel):
    application_id: int
    preview_hash: str = ""
    to: str | None = None
    subject: str | None = None
    body: str | None = None


class BatchRequest(BaseModel):
    confirm: str = ""
    items: list[BatchItem] = Field(default_factory=list)


#: Batch state. A 10-message batch paced at 45 s takes ~7 minutes, so the request
#: returns immediately and the UI polls /status. Singleton by design — two clicks
#: must not start two batches.
_BATCH: dict = {"thread": None, "started_at": None, "total": 0, "done": 0,
                "results": [], "active": False}
_BATCH_LOCK = threading.Lock()


def _batch_worker(items: list[BatchItem], cfg: dict) -> None:
    pause = max(0, _int(cfg, "min_seconds_between_sends"))
    try:
        if _lo_scan_optouts is not None:
            try:
                _lo_scan_optouts(hours=_int(cfg, "scan_optouts_hours"))
            except Exception as e:
                logger.warning(f"[outreach] opt-out scan failed: {e}")
        for i, item in enumerate(items):
            session = get_session()
            try:
                if _remaining_today(session, cfg) <= 0:
                    # Stop the WHOLE batch the moment the cap is reached.
                    for rest in items[i:]:
                        _BATCH["results"].append(
                            {"application_id": rest.application_id, "sent": False,
                             "blocked": "cap_reached"})
                        _BATCH["done"] += 1
                    return
                cand = _load_candidate(session, item.application_id, use_llm=False)
                _apply_edits(cand, SendRequest(confirm="SEND", preview_hash=item.preview_hash,
                                               to=item.to, subject=item.subject,
                                               body=item.body), cfg)
                try:
                    _check_hash(cand, item.preview_hash)
                except HTTPException:
                    res = {"sent": False, "blocked": "preview_stale"}
                else:
                    res = _send_one(session, cand, cfg)
                res["application_id"] = item.application_id
                _BATCH["results"].append(res)
            except Exception as e:
                logger.exception(f"[outreach] batch item {item.application_id} failed")
                _BATCH["results"].append({"application_id": item.application_id,
                                          "sent": False, "error": str(e)})
            finally:
                _BATCH["done"] += 1
                session.close()
            if pause and i < len(items) - 1:
                time.sleep(pause)
    finally:
        _BATCH["active"] = False


@router.post("/send-batch")
def send_batch(payload: BatchRequest) -> dict:
    cfg = _outreach_cfg()
    _require_confirm(payload.confirm)
    _require_enabled(cfg)
    if len(payload.items) > _int(cfg, "max_batch"):
        raise HTTPException(status_code=422,
                            detail=f"at most {_int(cfg, 'max_batch')} rows per batch")
    if not payload.items:
        raise HTTPException(status_code=422, detail="items must not be empty")
    _require_mailer()
    with _BATCH_LOCK:
        if _BATCH["active"]:
            raise HTTPException(status_code=409, detail="a batch is already running")
        _BATCH.update(active=True, total=len(payload.items), done=0, results=[],
                      started_at=_iso(_naive_utc(_utc_now())))
        t = threading.Thread(target=_batch_worker, args=(list(payload.items), cfg),
                             name="jobpilot-outreach-batch", daemon=True)
        _BATCH["thread"] = t
        t.start()
    return {"started": True, "total": len(payload.items)}


@router.get("/batch")
def batch_status() -> dict:
    return {"active": bool(_BATCH["active"]), "total": _BATCH["total"],
            "done": _BATCH["done"], "started_at": _BATCH["started_at"],
            "results": list(_BATCH["results"])}


# --------------------------------------------------------------------------
# Status / ledger
# --------------------------------------------------------------------------

@router.get("/status")
def status() -> dict:
    cfg = _outreach_cfg()
    ok, why = mailer_ready()
    session = get_session()
    try:
        totals = {"sent": 0, "failed": 0, "blocked": 0, "claimed": 0}
        for st, n in (session.query(OutreachSend.status, func.count(OutreachSend.id))
                      .group_by(OutreachSend.status).all()):
            totals[str(st)] = int(n)
        suppressions = int(session.query(func.count(OutreachSuppression.id)).scalar() or 0)
        cap = {"daily_cap": _int(cfg, "daily_cap"),
               "sent_today": _sent_today(session),
               "remaining": _remaining_today(session, cfg),
               "max_per_company_per_day": _int(cfg, "max_per_company_per_day"),
               "per_recipient_cooldown_days": _int(cfg, "per_recipient_cooldown_days"),
               "max_batch": _int(cfg, "max_batch"),
               "min_seconds_between_sends": _int(cfg, "min_seconds_between_sends")}
        return {
            "mailer_ready": bool(ok), "mailer_reason": why or "",
            "enabled": bool(cfg.get("enabled")), "dry_run_default": True,
            "cap": cap,
            "batch": {"active": bool(_BATCH["active"]), "total": _BATCH["total"],
                      "done": _BATCH["done"], "started_at": _BATCH["started_at"]},
            "providers": _provider_rows(),
            "suppressions": suppressions,
            "dead_addresses": len(load_dead()),
            "totals": totals,
        }
    finally:
        session.close()


def _provider_rows() -> list[dict]:
    """LinkedIn provider health. Absent providers are reported, not raised —
    this router ships before the sources do."""
    try:
        from agents.scanner.providers import provider_health
        rows = provider_health(_cfg())
    except Exception as e:
        logger.info(f"[outreach] provider health unavailable: {e}")
        return []
    wanted = {"unipile_linkedin", "brightdata_linkedin"}
    return [r for r in rows if isinstance(r, dict) and r.get("name") in wanted]


def _send_row(row: OutreachSend, *, with_body: bool = False) -> dict:
    d = {"id": row.id, "company": row.company, "job_title": row.job_title,
         "recipient": row.recipient, "status": row.status,
         "block_reason": row.block_reason, "error": row.error,
         "subject": row.subject, "sent_at": _iso(_naive_utc(row.sent_at)),
         "created_at": _iso(_naive_utc(row.created_at)), "post_url": row.post_url,
         "dedup_key": _canonical_key(row.dedup_key),
         "application_id": row.application_id, "job_id": row.job_id}
    if with_body:
        d.update(body=row.body, body_hash=row.body_hash,
                 attachments=row.attachments or [],
                 resume_path=row.resume_path, cover_letter_path=row.cover_letter_path,
                 recipient_source=row.recipient_source, channel=row.channel,
                 provider=row.provider, external_id=row.external_id)
    return d


@router.get("/history")
def history(limit: int = Query(100, ge=1, le=1000), status: str = "") -> dict:
    session = get_session()
    try:
        q = session.query(OutreachSend)
        if status:
            q = q.filter(OutreachSend.status == status)
        rows = q.order_by(OutreachSend.id.desc()).limit(limit).all()
        return {"sends": [_send_row(r) for r in rows], "count": len(rows)}
    finally:
        session.close()


@router.get("/send/{send_id}")
def send_detail(send_id: int) -> dict:
    """The audit view — includes the EXACT body that was sent."""
    session = get_session()
    try:
        row = session.query(OutreachSend).get(send_id)
        if not row:
            raise HTTPException(status_code=404, detail="Send not found")
        return _send_row(row, with_body=True)
    finally:
        session.close()


class ConfirmRequest(BaseModel):
    confirm: str = ""


@router.post("/retry/{send_id}")
def retry_send(send_id: int, payload: ConfirmRequest) -> dict:
    """Re-run delivery for a FAILED row. 'sent' is terminal and 404s here."""
    cfg = _outreach_cfg()
    _require_confirm(payload.confirm)
    _require_enabled(cfg)
    _require_mailer()
    session = get_session()
    try:
        row = session.query(OutreachSend).get(send_id)
        if not row or row.status != "failed":
            raise HTTPException(status_code=404, detail="No failed send with that id")
        if _remaining_today(session, cfg) <= 0:
            return {"sent": False, "send_id": row.id, "blocked": "cap_reached", "error": None}
        if not is_safe_recipient(row.recipient) or _is_suppressed(session, row.recipient) \
                or row.recipient in load_dead():
            return {"sent": False, "send_id": row.id, "blocked": "unsafe_recipient",
                    "error": None}
        cand = _Candidate(
            application_id=row.application_id or 0, job_id=row.job_id or 0,
            title=row.job_title or "", company=row.company or "",
            company_normalized=row.company_normalized or "", location="",
            url=row.post_url or "", post_url=row.post_url or "",
            source=row.provider or "", external_id=row.external_id or "",
            fit_score=None, dedup_key=row.dedup_key, recipient=row.recipient,
            recipient_source=row.recipient_source or "manual",
            channel=row.channel or "email", contact={},
            resume_path=row.resume_path, cover_letter_path=row.cover_letter_path,
            subject=row.subject or "", body=row.body or "",
            # The body is replayed byte-for-byte from the ledger, but the
            # unsubscribe header is rebuilt from current config — it is
            # transport metadata, not part of what the user approved.
            headers=_unsubscribe_headers(cfg))
        row.status = "claimed"
        row.error = None
        session.commit()
        return _deliver(session, row, cand, cfg)
    finally:
        session.close()


# --------------------------------------------------------------------------
# Suppressions
# --------------------------------------------------------------------------

class SuppressRequest(BaseModel):
    value: str
    scope: str = "address"
    reason: str = "manual"


@router.post("/suppress")
def suppress(payload: SuppressRequest) -> dict:
    """Idempotent upsert. Adding a suppression is always allowed; removing one
    a human asked for is not (see DELETE below)."""
    value = (payload.value or "").strip().lower()
    scope = payload.scope if payload.scope in ("address", "domain") else "address"
    if not value:
        raise HTTPException(status_code=400, detail="value required")
    session = get_session()
    try:
        row = (session.query(OutreachSuppression)
               .filter_by(value=value, scope=scope).first())
        if row:
            return {"ok": True, "id": row.id, "created": False,
                    "value": row.value, "scope": row.scope}
        row = OutreachSuppression(value=value, scope=scope,
                                  reason=(payload.reason or "manual")[:200],
                                  source="manual")
        session.add(row)
        try:
            session.commit()
        except IntegrityError:  # concurrent insert — still idempotent
            session.rollback()
            row = session.query(OutreachSuppression).filter_by(value=value, scope=scope).first()
            return {"ok": True, "id": row.id if row else None, "created": False,
                    "value": value, "scope": scope}
        return {"ok": True, "id": row.id, "created": True, "value": value, "scope": scope}
    finally:
        session.close()


@router.get("/suppressions")
def list_suppressions(limit: int = Query(500, ge=1, le=2000)) -> dict:
    session = get_session()
    try:
        rows = (session.query(OutreachSuppression)
                .order_by(OutreachSuppression.id.desc()).limit(limit).all())
        return {"suppressions": [
            {"id": r.id, "value": r.value, "scope": r.scope, "reason": r.reason,
             "source": r.source, "created_at": _iso(_naive_utc(r.created_at))}
            for r in rows], "count": len(rows)}
    finally:
        session.close()


@router.delete("/suppress/{suppression_id}")
def unsuppress(suppression_id: int) -> dict:
    """Undo a suppression the user added by mistake.

    A row written by the reply scanner cannot be deleted here: a human replied
    STOP, and no UI click should be able to walk that back.
    """
    session = get_session()
    try:
        row = session.query(OutreachSuppression).get(suppression_id)
        if not row:
            raise HTTPException(status_code=404, detail="Suppression not found")
        if row.source == "reply_scan":
            raise HTTPException(status_code=403,
                                detail="a STOP reply cannot be removed from the dashboard")
        session.delete(row)
        session.commit()
        return {"ok": True}
    finally:
        session.close()


# --------------------------------------------------------------------------
# Background helpers — scan + prepare
# --------------------------------------------------------------------------

_SCAN = {"active": False}
_SCAN_LOCK = threading.Lock()


@router.post("/scan")
def scan_now() -> dict:
    """Run the LinkedIn providers once, now. Fire-and-forget, singleton."""
    with _SCAN_LOCK:
        if _SCAN["active"]:
            raise HTTPException(status_code=409, detail="a scan is already running")
        _SCAN["active"] = True

    def _work():
        try:
            from agents.scanner.aggregators import AggregatorScanner
            AggregatorScanner(_cfg()).run()
        except Exception:
            logger.exception("[outreach] LinkedIn scan failed")
        finally:
            _SCAN["active"] = False

    threading.Thread(target=_work, name="jobpilot-outreach-scan", daemon=True).start()
    return {"started": True}


_PREPARING: set[int] = set()


@router.post("/prepare/{application_id}")
def prepare(application_id: int) -> dict:
    """Ensure this row has its OWN tailored resume + cover letter.

    Poll /candidates for `materials_ready`; this returns immediately because
    scoring + tailoring are minutes of LLM work.
    """
    session = get_session()
    try:
        row = (session.query(Application, Job).join(Job, Application.job_id == Job.id)
               .filter(Application.id == application_id).first())
        if not row:
            raise HTTPException(status_code=404, detail="Application not found")
    finally:
        session.close()
    if application_id in _PREPARING:
        return {"started": False, "reason": "already preparing"}
    _PREPARING.add(application_id)

    def _work():
        s = get_session()
        try:
            app_obj, job = (s.query(Application, Job)
                            .join(Job, Application.job_id == Job.id)
                            .filter(Application.id == application_id).first())
            score = s.query(JobScore).filter(JobScore.job_id == job.id).first()
            if score is None:
                from agents.ranker import score_job
                score = score_job(job)
                s.add(score)
                s.commit()
            from agents.tailor import tailor_for_job
            res = tailor_for_job(job, score) or {}
            if res.get("resume_docx"):
                app_obj.resume_path = str(res["resume_docx"])
            if res.get("cover_letter_docx"):
                app_obj.cover_letter_path = str(res["cover_letter_docx"])
            record_status_change(s, app_obj, ApplicationStatus.MATERIALS_READY,
                                 source="outreach", note="tailored for outreach")
            s.commit()
        except Exception:
            logger.exception(f"[outreach] prepare {application_id} failed")
        finally:
            _PREPARING.discard(application_id)
            s.close()

    threading.Thread(target=_work, name=f"jobpilot-outreach-prep-{application_id}",
                     daemon=True).start()
    return {"started": True, "application_id": application_id}
