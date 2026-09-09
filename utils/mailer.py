"""Outbound email for application packages (SMTP over the Gmail app password).

Sends a professional application email with the tailored resume and cover
letter attached. Two modes:

  self_copy   — a record of each application, sent to your own address. Safe to
                run unattended: you are the only recipient.
  direct      — sent to a real recipient YOU supply for a specific application
                (a recruiter who asked for materials, a careers@ address a
                posting explicitly names). Never bulk, never guessed.

Recipient discovery lives in utils/company_email.py and is layered by how much
the address can be trusted: published in the posting > published on the
company's own site > constructed (careers@domain). The constructed layer is
opt-in (`mail.guess_addresses`), capped per day, gated on the domain provably
belonging to that company, and fed by utils/bounce_watch.py so an address that
bounced is never used twice.

The screening below is what keeps any of that acceptable: an accessibility,
accommodation, compliance or no-reply inbox must NEVER receive an application,
whichever layer produced it. Those channels exist for other purposes and people
depend on them. Volume discipline matters for the same reason — bulk mail to
unverified addresses burns the sender's reputation and is regulated in several
jurisdictions, which is why constructed addresses are capped and published ones
are not.
"""

from __future__ import annotations

import logging
import mimetypes
import re
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path

from agents.email_reader import load_gmail_config

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465          # implicit TLS


# Addresses that appear in job postings but must NEVER receive an application.
# Mailing a CV to a disability-accommodation or compliance inbox is not just
# ineffective, it misuses a channel people rely on for legal requests.
BLOCKED_LOCAL = (
    # --- accessibility / accommodation, INCLUDING the ways employers misspell it.
    # This is not pedantry: Qualcomm's own live posting says "You may e-mail
    # disability-accomodations@qualcomm.com" — one 'm' — and the correctly
    # spelled entry did not match it, so the address passed the screen. The
    # person staffing that inbox handles accessibility requests from disabled
    # applicants; a CV arriving there is both useless and a misuse of a channel
    # people depend on.
    "accommodation", "accomodation", "acommodation", "accomadation",
    "accessibility", "accessable", "accessible", "ada", "disability",
    "disabilities", "disabled", "reasonableaccom",
    # --- legal / compliance / ethics channels
    "compliance", "legal", "privacy", "security", "abuse", "ethics",
    "whistleblow", "harassment", "eeo", "affirmativeaction", "grievance",
    # --- automated or wrong-audience inboxes
    "noreply", "no-reply", "donotreply", "postmaster", "unsubscribe",
    "press", "media", "investor", "support", "help", "billing",
    "sales", "marketing", "info", "webmaster",
)
# Local-parts that read as a real application/recruiting inbox.
PREFERRED_LOCAL = (
    "recruit", "recruiting", "talent", "career", "careers", "jobs", "job",
    "hiring", "hr", "apply", "application", "resume", "cv", "people",
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def is_safe_recipient(addr: str) -> bool:
    """False for inboxes that should not receive an unsolicited application.

    The local part is compared BOTH as written and with its separators removed,
    so "disability-accomodations", "disability.accommodations" and
    "disabilityaccommodations" are one address to this screen rather than three
    spellings that each need their own blocklist entry. Punctuation is the
    cheapest way for a real address to slip past a substring match, and the cost
    of one slipping past is a CV in an accessibility inbox.
    """
    addr = (addr or "").strip().lower()
    if "@" not in addr or addr.endswith((".png", ".jpg", ".gif", ".svg")):
        return False
    local = addr.split("@", 1)[0]
    squashed = re.sub(r"[^a-z0-9]", "", local)
    return not any(b in local or b.replace("-", "") in squashed
                   for b in BLOCKED_LOCAL)


def find_employer_recipient(description: str | None) -> str | None:
    """The best application address PUBLISHED IN the given text, or None.

    This function itself never guesses — it only reads addresses the employer
    actually printed. Constructing an address from a company name is a separate,
    opt-in path in utils/company_email.py with its own guards, kept apart so
    "the employer published this" and "we made this up" never get confused.
    """
    if not description:
        return None
    seen, safe = set(), []
    for raw in _EMAIL_RE.findall(description):
        a = raw.strip().lower().rstrip(".,;:)")
        if a in seen:
            continue
        seen.add(a)
        if is_safe_recipient(a):
            safe.append(a)
    if not safe:
        return None
    for a in safe:                       # prefer an obvious recruiting inbox
        if any(p in a.split("@", 1)[0] for p in PREFERRED_LOCAL):
            return a
    return safe[0]


def mailer_ready() -> tuple[bool, str]:
    cfg = load_gmail_config()
    if not cfg.get("address") or not cfg.get("app_password"):
        return False, "config/gmail.yaml needs address + app_password"
    return True, ""


def _attach(msg: EmailMessage, path: str | None) -> str | None:
    """Attach one file; returns its display name, or None when unusable."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        logger.warning(f"[mailer] attachment missing: {p}")
        return None
    ctype, _ = mimetypes.guess_type(p.name)
    maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
    msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    return p.name


def build_application_email(
    *, sender_name: str, sender_email: str, to_addr: str, job_title: str,
    company: str, body: str, resume_path: str | None = None,
    cover_letter_path: str | None = None, self_copy: bool = False,
) -> tuple[EmailMessage, list[str]]:
    msg = EmailMessage()
    prefix = "[Application record] " if self_copy else ""
    msg["Subject"] = f"{prefix}{job_title} — {sender_name}" if not company else \
                     f"{prefix}{job_title} at {company} — {sender_name}"
    msg["From"] = formataddr((sender_name, sender_email))
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Reply-To"] = sender_email
    msg.set_content(body)

    attached = [n for n in (_attach(msg, resume_path), _attach(msg, cover_letter_path)) if n]
    return msg, attached


def default_body(*, sender_name: str, job_title: str, company: str,
                 links: dict | None = None, self_copy: bool = False) -> str:
    """A plain, professional letter. No hype, no invented claims."""
    links = links or {}
    tail = []
    if links.get("linkedin"):
        tail.append(f"LinkedIn: {links['linkedin']}")
    if links.get("github"):
        tail.append(f"GitHub: {links['github']}")
    contact = ("\n" + "\n".join(tail)) if tail else ""

    if self_copy:
        return (
            f"Application record — {job_title}"
            f"{(' at ' + company) if company else ''}.\n\n"
            "The tailored resume and cover letter sent with this application are "
            "attached for your records.\n"
        )

    where = f" at {company}" if company else ""
    return (
        f"Dear Hiring Team,\n\n"
        f"I am writing to apply for the {job_title} role{where}. My resume and "
        f"cover letter are attached.\n\n"
        f"I am an AI engineer and full-stack software engineer working across "
        f"computer vision, LLM systems and automation, and I would welcome the "
        f"chance to discuss how that experience fits what your team is building.\n\n"
        f"Thank you for your time and consideration.\n\n"
        f"Kind regards,\n{sender_name}{contact}\n"
    )


def send_application_email(
    *, to_addr: str, job_title: str, company: str, resume_path: str | None,
    cover_letter_path: str | None, sender_name: str, body: str | None = None,
    links: dict | None = None, self_copy: bool = False,
) -> dict:
    """Send one application email. Returns {sent, to, attachments, error}."""
    ok, why = mailer_ready()
    if not ok:
        return {"sent": False, "error": why}

    cfg = load_gmail_config()
    sender_email = cfg["address"]
    to_addr = (to_addr or "").strip()
    if not to_addr or "@" not in to_addr:
        return {"sent": False, "error": f"invalid recipient: {to_addr!r}"}

    msg, attached = build_application_email(
        sender_name=sender_name, sender_email=sender_email, to_addr=to_addr,
        job_title=job_title, company=company,
        body=body or default_body(sender_name=sender_name, job_title=job_title,
                                  company=company, links=links, self_copy=self_copy),
        resume_path=resume_path, cover_letter_path=cover_letter_path,
        self_copy=self_copy,
    )
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.login(sender_email, cfg["app_password"])
            s.send_message(msg)
        logger.info(f"[mailer] sent '{job_title}' to {to_addr} ({len(attached)} attachment(s))")
        return {"sent": True, "to": to_addr, "attachments": attached}
    except smtplib.SMTPAuthenticationError:
        return {"sent": False, "error": "SMTP auth failed — check the Gmail app password"}
    except Exception as e:
        logger.error(f"[mailer] send failed: {e}")
        return {"sent": False, "error": str(e)}
