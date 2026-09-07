"""Read-only Gmail access over IMAP for JobPilot.

Two jobs:
  * find_verification(): the code (or link) an ATS just emailed while the user
    creates an account — the extension fills it into the OTP box.
  * scan_job_mail(): confirmation / rejection / interview emails matched to
    open applications, returned as a REVIEW QUEUE (nothing is written here).

Auth is a Gmail App Password (Google Account → Security → 2-Step Verification
→ App passwords) in config/gmail.yaml (gitignored) or env vars. The mailbox is
selected read-only and fetched with BODY.PEEK, so nothing is ever marked read,
moved, or deleted. Only headers are pulled for the tracking scan; a body is
fetched only for mail that names a company we applied to or comes from an ATS.

# ponytail: IMAP + app password, not OAuth. google-auth-oauthlib is already
# installed if Google ever drops app passwords for personal accounts.
"""
from __future__ import annotations

import email
import html
import imaplib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
GMAIL_CFG = PROJECT_ROOT / "config" / "gmail.yaml"
IMAP_HOST = "imap.gmail.com"
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

SETUP_HELP = ("Create config/gmail.yaml with `address:` and `app_password:` — Google Account → "
              "Security → 2-Step Verification → App passwords (16 characters).")

ATS_DOMAINS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkday.com", "workday.com", "icims.com",
    "smartrecruiters.com", "taleo.net", "jobvite.com", "successfactors.com", "workable.com",
    "bamboohr.com", "rippling.com", "breezy.hr", "applytojob.com", "dayforce", "phenom",
    "oraclecloud.com", "paylocity.com", "linkedin.com", "indeed.com", "hire.com", "recruitee",
)


class GmailNotConnected(Exception):
    """Raised when the mailbox can't be reached; carries a user-facing reason."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------- connection

def load_gmail_config() -> dict:
    cfg = {}
    if GMAIL_CFG.exists():
        try:
            cfg = yaml.safe_load(GMAIL_CFG.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.warning(f"[gmail] config unreadable: {e}")
    pw = os.environ.get("JOBPILOT_GMAIL_APP_PASSWORD") or str(cfg.get("app_password") or "")
    return {
        "enabled": cfg.get("enabled", True),
        "address": os.environ.get("JOBPILOT_GMAIL_ADDRESS") or str(cfg.get("address") or ""),
        "app_password": pw.replace(" ", ""),   # Google displays it as 4 groups of 4
    }


def masked_address(addr: str) -> str:
    if "@" not in (addr or ""):
        return addr or ""
    user, dom = addr.split("@", 1)
    return (user[:2] + "…" if len(user) > 2 else user) + "@" + dom


def connect() -> imaplib.IMAP4_SSL:
    """Open INBOX read-only, or raise GmailNotConnected with a reason."""
    cfg = load_gmail_config()
    if not cfg["enabled"]:
        raise GmailNotConnected("Gmail is disabled in config/gmail.yaml (enabled: false).")
    if not cfg["address"] or not cfg["app_password"]:
        raise GmailNotConnected("Gmail not set up. " + SETUP_HELP)
    try:
        m = imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=20)
        m.login(cfg["address"], cfg["app_password"])
        m.select("INBOX", readonly=True)
        return m
    except imaplib.IMAP4.error as e:
        msg = str(e)
        if "AUTHENTICATIONFAILED" in msg.upper() or "invalid credentials" in msg.lower():
            raise GmailNotConnected("Gmail rejected the login — use an App Password (not your normal "
                                    "password) and check the address in config/gmail.yaml.")
        raise GmailNotConnected(f"IMAP error: {msg}")
    except OSError as e:
        raise GmailNotConnected(f"Could not reach {IMAP_HOST}: {e}")


def close(m) -> None:
    try:
        m.logout()
    except Exception:
        pass


# ---------------------------------------------------------------- parsing

def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hdr(v) -> str:
    if not v:
        return ""
    try:
        return str(make_header(decode_header(v))).replace("\n", " ").strip()
    except Exception:
        return str(v)


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(re.sub(r"[ \t]+", " ", s))


def _links(s: str) -> list[str]:
    return [html.unescape(u) for u in re.findall(r"""href=["']?(https?://[^"'\s>]+)""", s, flags=re.I)]


def _body(msg) -> tuple[str, list[str]]:
    plain, htm = [], []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        if str(part.get("Content-Disposition", "")).lower().startswith("attachment"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        (plain if ct == "text/plain" else htm).append(text)
    # Some senders ship a stub text/plain ("view in browser") with the code only
    # in the HTML part — keep whichever rendering carries more content.
    plain_text = "\n".join(plain).strip()
    html_text = _html_to_text("\n".join(htm)).strip() if htm else ""
    text = max(plain_text, html_text, key=len)
    links = _links("\n".join(htm)) + re.findall(r"https?://[^\s<>\"')]+", "\n".join(plain))
    return text, links


def parse_message(raw: bytes, uid: str = "") -> dict:
    msg = email.message_from_bytes(raw)
    try:
        received = _aware(parsedate_to_datetime(msg.get("Date")))
    except Exception:
        received = None
    text, links = _body(msg)
    return {"uid": uid, "from": _hdr(msg.get("From")), "subject": _hdr(msg.get("Subject")),
            "received_at": received, "text": text, "links": links}


def _parse_headers(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw)
    return {"from": _hdr(msg.get("From")), "subject": _hdr(msg.get("Subject"))}


def recent_messages(since: datetime, limit: int = 25, conn=None, prefilter=None) -> list[dict]:
    """Messages received at/after `since`, newest first.

    prefilter(from, subject) -> bool, when given, decides from the HEADERS alone
    whether the body is fetched at all — unrelated mail is never read.
    """
    since = _aware(since)
    m = conn or connect()
    try:
        if conn is not None:
            m.noop()   # nudge the server so a message that just landed is searchable
        d = (since - timedelta(days=1)).astimezone(timezone.utc)
        day = f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"
        _, data = m.uid("search", None, f"(SINCE {day})")
        uids = (data[0] or b"").split()
        out = []
        for uid in reversed(uids[-limit:]):
            if prefilter is not None:
                _, hp = m.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
                hraw = next((p[1] for p in hp if isinstance(p, tuple)), None)
                if not hraw:
                    continue
                h = _parse_headers(hraw)
                if not prefilter(h["from"], h["subject"]):
                    continue
            _, parts = m.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if not raw:
                continue
            msg = parse_message(raw, uid.decode())
            if msg["received_at"] and msg["received_at"] < since:
                continue
            out.append(msg)
        return out
    finally:
        if conn is None:
            close(m)


# ---------------------------------------------------------------- verification codes

_CODE_CTX = re.compile(r"code|verif|passcode|\bpin\b|one[- ]?time|\botp\b|security|confirm|token|authenticat", re.I)
_CODE_NUM = re.compile(r"(?<![\d\-.,/$#])(\d{4,8})(?![\d\-.,/%])")
_LINK_OK = re.compile(r"verif|confirm|activate|validate|token=|otp|passcode", re.I)
_LINK_BAD = re.compile(r"unsubscribe|privacy|terms|preferences|support|help|policy", re.I)


def extract_code(subject: str, text: str) -> str | None:
    """A 4–8 digit number that sits near a code/verify word; the subject wins."""
    for src in (subject or "", text or ""):
        for mt in _CODE_NUM.finditer(src):
            n = mt.group(1)
            if len(n) == 4 and 1900 <= int(n) <= 2100:   # a year, not a code
                continue
            ctx = src[max(0, mt.start() - 90): mt.end() + 90]
            if _CODE_CTX.search(ctx):
                return n
    return None


def extract_link(links: list[str] | None) -> str | None:
    for u in links or []:
        if _LINK_OK.search(u) and not _LINK_BAD.search(u):
            return u
    return None


_GENERIC_HOST = {"www", "www2", "com", "net", "org", "io", "co", "careers", "career", "jobs", "job",
                 "apply", "https", "http", "myworkdayjobs", "myworkdaysite", "wd1", "wd3", "wd5", "wd12",
                 "greenhouse", "boards", "lever", "icims", "smartrecruiters", "ashbyhq", "taleo",
                 "successfactors", "external", "en", "us", "login", "signin", "account"}
_ATS_FAMILY = ("workday", "greenhouse", "lever", "icims", "smartrecruiters", "ashby", "taleo",
               "successfactors", "oracle", "phenom", "jobvite", "workable", "bamboohr", "rippling")


def _hint_tokens(hint: str) -> list[str]:
    low = (hint or "").lower()
    toks = [t for t in re.split(r"[^a-z0-9]+", low) if len(t) >= 3 and t not in _GENERIC_HOST]
    return toks + [f for f in _ATS_FAMILY if f in low]


def find_verification(since: datetime, hint: str = "", messages=None, conn=None) -> dict | None:
    """Best verification code/link received after `since`. `hint` is the ATS
    hostname or company — mail naming it outranks anything else."""
    msgs = messages if messages is not None else recent_messages(since, limit=15, conn=conn)
    toks = _hint_tokens(hint)
    best, best_key = None, None
    for msg in msgs:
        code = extract_code(msg.get("subject", ""), msg.get("text", ""))
        link = None if code else extract_link(msg.get("links"))
        if not code and not link:
            continue
        if link and not _CODE_CTX.search(msg.get("subject", "") + " " + msg.get("text", "")[:1500]):
            continue
        hay = (msg.get("from", "") + " " + msg.get("subject", "")).lower()
        when = msg.get("received_at") or datetime.min.replace(tzinfo=timezone.utc)
        key = (any(t in hay for t in toks), bool(code), when)
        if best_key is None or key > best_key:
            best_key = key
            best = {"uid": msg.get("uid", ""), "code": code, "link": link,
                    "subject": msg.get("subject", ""), "from": msg.get("from", ""),
                    "received_at": when.isoformat() if msg.get("received_at") else None}
    return best


# ---------------------------------------------------------------- tracking scan

_REJECT = re.compile(
    r"unfortunately|not (be )?moving forward|decided (not )?to (move forward|proceed|pursue)|"
    r"other candidates|no longer (under consideration|being considered)|not selected|"
    r"will not be (moving|proceeding)|regret to inform|not the right fit|position has been filled|"
    r"pursue other (candidates|applicants)|we will not be", re.I)
_INTERVIEW_SUBJ = re.compile(r"\binterview\b|phone screen|next steps?|let's (chat|talk|connect)", re.I)
_INTERVIEW_STRONG = re.compile(
    r"invite you to (an |a )?(interview|call|conversation)|schedule (a|an|your|some) (call|interview|conversation|time|chat)|"
    r"phone screen|screening call|your availability|book a time|calendly\.com|would like to (meet|speak|talk)|"
    r"interview (with|on|is scheduled|has been scheduled)", re.I)
_CONFIRM = re.compile(
    r"thank(s| you) for (applying|your application|your interest|submitting)|"
    r"application (was |has been |is )?(received|submitted|complete)|we('ve| have) received your application|"
    r"successfully (applied|submitted)|your application (to|for) |application confirmation|you applied (to|for)", re.I)

KIND_TO_STATUS = {"confirmation": "applied", "interview": "interview", "rejection": "rejected"}
_RANK = {"applied": 1, "response_received": 2, "interview": 3}   # never propose a step backwards
TRACK_STATUSES = ("materials_ready", "queued", "approved", "applied", "response_received", "interview")
_SUFFIX = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|the|group|holdings|technologies|labs)\b")


def classify(subject: str, text: str) -> str | None:
    s, b = subject or "", (text or "")[:2500]
    if _REJECT.search(s) or _REJECT.search(b):
        return "rejection"
    if _INTERVIEW_SUBJ.search(s):
        return "interview"
    if _CONFIRM.search(s):
        return "confirmation"
    if _INTERVIEW_STRONG.search(b):
        return "interview"
    if _CONFIRM.search(b):
        return "confirmation"
    return None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())).strip()


def company_key(company: str) -> str:
    return re.sub(r"\s+", " ", _SUFFIX.sub(" ", _norm(company))).strip()


def _from_domain(sender: str) -> str:
    mt = re.search(r"@([\w.-]+)", sender or "")
    return mt.group(1).lower() if mt else ""


def header_matches_company(sender: str, subject: str, company: str) -> bool:
    key = company_key(company)
    if len(key) < 3:
        return False
    if key in _norm(sender + " " + subject):
        return True
    first = key.split(" ")[0]
    return len(first) >= 4 and first in _from_domain(sender).replace("-", "").replace(".", "")


def message_matches_company(msg: dict, company: str) -> bool:
    if header_matches_company(msg.get("from", ""), msg.get("subject", ""), company):
        return True
    key = company_key(company)
    return len(key) >= 3 and key in _norm(msg.get("text", "")[:3000])


def is_ats_sender(sender: str) -> bool:
    dom = _from_domain(sender)
    return any(a in dom for a in ATS_DOMAINS)


def open_applications() -> tuple[list[dict], set[str]]:
    """Trackable applications + the Gmail uids already applied to the board."""
    from db.database import get_session
    from db.models import Application, ApplicationEvent, ApplicationStatus, Job

    s = get_session()
    try:
        wanted = [ApplicationStatus(x) for x in TRACK_STATUSES]
        rows = (s.query(Application, Job).join(Job, Application.job_id == Job.id)
                .filter(Application.status.in_(wanted)).all())
        apps = [{"app_id": a.id, "company": j.company or "", "title": j.title or "",
                 "status": a.status.value} for a, j in rows]
        notes = s.query(ApplicationEvent.note).filter(ApplicationEvent.note.like("gmail-uid:%")).all()
        seen = {n.split(" ", 1)[0][len("gmail-uid:"):] for (n,) in notes if n}
        return apps, seen
    finally:
        s.close()


def propose(messages: list[dict], apps: list[dict], seen_uids=frozenset()) -> list[dict]:
    """Review-queue rows: one proposed status change per matched email."""
    out = []
    for msg in messages:
        if msg.get("uid") in seen_uids:
            continue
        kind = classify(msg.get("subject", ""), msg.get("text", ""))
        if not kind:
            continue
        cands = [a for a in apps if message_matches_company(msg, a["company"])]
        if not cands:
            continue
        hay = _norm(msg.get("subject", "") + " " + msg.get("text", "")[:3000])

        def title_hits(a):
            return sum(1 for w in set(_norm(a["title"]).split()) if len(w) > 3 and w in hay)

        app = max(cands, key=title_hits)
        proposed = KIND_TO_STATUS[kind]
        if proposed == app["status"]:
            continue
        if proposed != "rejected" and _RANK.get(proposed, 0) < _RANK.get(app["status"], 0):
            continue
        when = msg.get("received_at")
        out.append({"uid": msg.get("uid", ""), "app_id": app["app_id"], "company": app["company"],
                    "title": app["title"], "current_status": app["status"],
                    "proposed_status": proposed, "kind": kind,
                    "subject": msg.get("subject", ""), "from": msg.get("from", ""),
                    "received_at": when.isoformat() if when else None,
                    "snippet": re.sub(r"\s+", " ", msg.get("text", ""))[:220]})
    return out


def scan_job_mail(days: int = 7) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    apps, seen = open_applications()
    companies = [a["company"] for a in apps]

    def prefilter(sender, subject):
        return is_ats_sender(sender) or any(header_matches_company(sender, subject, c) for c in companies)

    msgs = recent_messages(since, limit=300, prefilter=prefilter)
    return propose(msgs, apps, seen)
