"""Extract the application channel from a LinkedIn job post or hiring post.

Regex FIRST — an email address is a lexical object and an LLM is the wrong tool
for finding one. The LLM is used only to read the human INSTRUCTION around it
("email your CV to X with subject 'DevOps 2026'", "DM me", "apply on our site")
and only when instruction keywords are present.

HARD RULE: any address the LLM returns that does not appear VERBATIM in the
source text is discarded. The model may summarise instructions; it may never
mint a recipient. (Same principle as agents/outreach._scrub_contacts.)

Every candidate address is screened through utils.mailer.is_safe_recipient, so
accommodation / accessibility / compliance / legal / no-reply inboxes are never
returned — those channels exist for other purposes and people depend on them —
and through utils.bounce_watch.load_dead() so an address a previous send bounced
from is never offered again.

WHY THIS MODULE EXISTS AT ALL (the rationale the whole feature is built on)
--------------------------------------------------------------------------
The user asked for an "auto mail bomb". This builds **controlled batch
outreach** instead, and the difference is not politeness — it is the only
version that works:

- Gmail (SMTP app password, utils/mailer.py) is the sender's *personal*
  mailbox. Unsolicited bulk mail to unverified, regex-scraped addresses
  produces bounces and spam complaints, which is exactly the signal Google uses
  to rate-limit or suspend an account. A suspended Gmail kills the whole
  JobPilot pipeline (auto-apply email, agents/email_reader, utils/bounce_watch),
  not just this feature.
- An identical body sent 200 times is the single strongest bulk-mail
  fingerprint. **Per-job tailoring (its own CV + cover letter from
  agents/tailor.py) is therefore a deliverability mechanism, not a nicety.**
- Addresses harvested from public LinkedIn posts are personal data. Mailing
  each one **once**, about **one specific job**, with materials actually
  written for that job, is a defensible personal job search. Mailing the same
  careers@ five times because five jobs matched is a bomb.

Scope: pure extraction + screening. No network, no SMTP. One optional LLM call,
and only to *interpret instructions* — never to produce an address. The single
exception to "no DB" is scan_optouts(), which is the write side of the STOP
promise the outreach body makes and therefore has to persist rows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from utils.company_names import normalize_company_name
from utils.mailer import PREFERRED_LOCAL, is_safe_recipient

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)

APPLY_CHANNELS = ("email", "external_link", "linkedin_easy_apply", "dm", "unknown")
TRAILING_PUNCT = ".,;:)]}>\"'"

INSTRUCTION_HINTS = (
    "send your cv", "send your resume", "send cv", "send resume", "email your",
    "apply by email", "share your cv", "drop your cv", "dm me", "message me",
    "apply here", "apply at", "apply via", "subject line", "mention the role",
    "attach your", "forward your", "reach out to", "kindly send",
)
_DM_HINTS = ("dm me", "dm us", "message me", "message us", "send me a dm",
             "inbox me", "pm me", "drop me a message", "connect with me")

# Addresses that are an image filename, not a mailbox. utils.mailer already
# rejects these, but we want the distinct reason in `rejected` so the dashboard
# does not tell the user an inbox was "blocked" when it was never an inbox.
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")


# --------------------------------------------------------------- de-mangling

# Posters mangle addresses to dodge scrapers, so the mangled form is the COMMON
# form in hiring posts, not an edge case. Everything below runs before the email
# regex; nothing below is allowed to invent an "@" out of ordinary prose, which
# is why the bare " at " rule fires only when a literal "dot" follows it. The
# form "first.last at acme.com" is deliberately NOT handled: it is lexically
# indistinguishable from "apply at acme.com" and "visit www.acme.com at acme.io",
# and a false recipient is far more expensive here than a missed one.
_ZERO_WIDTH = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)

# NFKC folds the fullwidth forms (U+FF20 "@", U+FF0E "."); these are the
# lookalikes it leaves alone, plus the no-break spaces that hide a spaced-out
# address from a naive whitespace collapse. Written as escapes on purpose:
# a literal zero-width character in source is invisible to the next reader.
_LOOKALIKE = {
    "\ufe6b": "@",    # SMALL COMMERCIAL AT
    "\u3002": ".",    # IDEOGRAPHIC FULL STOP
    "\u2024": ".",    # ONE DOT LEADER
    "\u00b7": ".",    # MIDDLE DOT
    "\u30fb": ".",    # KATAKANA MIDDLE DOT
    "\u00a0": " ",    # NO-BREAK SPACE
    "\u202f": " ",    # NARROW NO-BREAK SPACE
    "\u2007": " ",    # FIGURE SPACE
}

_OBFUSCATIONS = (
    # (at) [at] {at} <at>
    (re.compile(r"\s*[\(\[\{<]\s*at\s*[\)\]\}>]\s*", re.I), "@"),
    # name-at-domain / name_at_domain (only between alphanumerics)
    (re.compile(r"(?<=[A-Za-z0-9])\s*[-_]\s*at\s*[-_]\s*(?=[A-Za-z0-9])", re.I), "@"),
    # " AT " only when a literal "dot" follows the domain — this is the guard
    # that keeps "apply at acme.com" out of the candidate list.
    (re.compile(r"\s+at\s+(?=[A-Za-z0-9.\-]+\s*[\(\[\{<]?\s*dot(?![a-z]))", re.I), "@"),
    # (dot) [dot] {dot} <dot>  and the bracketed literal  [.]  (.)  {.}
    (re.compile(r"\s*[\(\[\{<]\s*(?:dot|\.)\s*[\)\]\}>]\s*", re.I), "."),
    (re.compile(r"(?<=[A-Za-z0-9])\s*[-_]\s*dot\s*[-_]\s*(?=[A-Za-z0-9])", re.I), "."),
    (re.compile(r"(?<=[A-Za-z0-9])\s+dot\s+(?=[A-Za-z0-9])", re.I), "."),
)

# A spaced-out address ("hr @ acme . com") is only collapsed when the run ends
# on a real TLD. Without that anchor, "follow @acme . We are hiring" collapses
# into "follow@acme.we", which is a plausible-looking address and a wrong one.
_KNOWN_TLDS = (
    "com", "net", "org", "io", "co", "ai", "dev", "app", "me", "info", "biz",
    "edu", "gov", "int", "xyz", "online", "site", "cloud", "tech", "digital",
    "agency", "solutions", "consulting", "systems", "group", "world", "jobs",
    "careers", "email", "us", "uk", "de", "fr", "es", "it", "nl", "be", "ch",
    "at", "se", "no", "dk", "fi", "pl", "cz", "pt", "gr", "ro", "hu", "ie",
    "ru", "ua", "tr", "il", "ae", "sa", "eg", "ng", "ke", "za", "ma", "in",
    "pk", "bd", "lk", "cn", "hk", "tw", "jp", "kr", "sg", "my", "id", "th",
    "vn", "ph", "au", "nz", "ca", "mx", "br", "ar", "cl", "pe", "eu",
)
_SPACED_ADDR = re.compile(
    r"(?<![A-Za-z0-9._%+\-@])"
    r"([A-Za-z0-9][A-Za-z0-9._%+\-]*[A-Za-z0-9]|[A-Za-z0-9])"   # local part
    r"(\s*)@\s*"
    r"([A-Za-z0-9\-]+(?:\s*\.\s*[A-Za-z0-9\-]+)*\s*\.\s*(?:"
    + "|".join(sorted(_KNOWN_TLDS, key=len, reverse=True)) + r"))"
    r"(?![A-Za-z0-9])",
    re.I,
)


def _local_looks_like_address(local: str) -> bool:
    """Is this token plausibly a local-part rather than an English word?

    Only consulted when whitespace separates the token from the "@", i.e. the
    case where prose ("follow @acme . com") can masquerade as an address.
    """
    low = local.lower()
    return any(c in low for c in "._-+") or any(p in low for p in PREFERRED_LOCAL)


def _collapse_spaced(m: re.Match) -> str:
    local, gap, domain = m.group(1), m.group(2), m.group(3)
    if gap and not _local_looks_like_address(local):
        return m.group(0)
    return local + "@" + re.sub(r"\s+", "", domain)


def _deobfuscate(text: str) -> str:
    """Un-mangle scraper-dodging address forms. Pure; never widens prose."""
    s = unicodedata.normalize("NFKC", text or "")
    s = s.translate(_ZERO_WIDTH)
    for bad, good in _LOOKALIKE.items():
        s = s.replace(bad, good)
    for rx, rep in _OBFUSCATIONS:
        s = rx.sub(rep, s)
    return _SPACED_ADDR.sub(_collapse_spaced, s)


# --------------------------------------------------------------- screening

def _addresses(text: str) -> list[str]:
    """Lowercased, punctuation-trimmed, order-preserving unique addresses."""
    out: list[str] = []
    for raw in EMAIL_RE.findall(text or ""):
        a = raw.strip().lower().rstrip(TRAILING_PUNCT)
        if a and a not in out:
            out.append(a)
    return out


def _own_address() -> str:
    """The sender's own mailbox — never a useful application contact."""
    try:
        from agents.email_reader import load_gmail_config
        return (load_gmail_config().get("address") or "").strip().lower()
    except Exception:
        return ""


def _default_dead() -> set[str]:
    """bounce_watch.load_dead(), with the silent-corruption case made loud.

    load_dead() returns an EMPTY SET on JSONDecodeError/OSError, i.e. a corrupt
    dead list silently means "nothing is dead" and a previously bounced address
    becomes offerable again. That failure is acceptable there; here it is worth
    a WARNING, because this module is what hands the address to a send path.
    """
    try:
        from utils import bounce_watch
        dead = bounce_watch.load_dead()
        path = getattr(bounce_watch, "_DEAD_PATH", None)
        if not dead and path is not None and path.exists():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(
                    f"[linkedin_outreach] dead-address list exists but is unreadable "
                    f"({e}) — treating every address as live, which it may not be")
        return dead
    except Exception as e:
        logger.warning(f"[linkedin_outreach] could not load dead addresses: {e}")
        return set()


def _domain(addr: str) -> str:
    return addr.split("@", 1)[1].lower() if "@" in addr else ""


def _second_level(domain: str) -> str:
    parts = [p for p in domain.split(".") if p]
    if len(parts) >= 3 and len(parts[-2]) <= 3 and len(parts[-1]) <= 3:
        return parts[-3]          # acme.co.uk -> acme
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def _screen(addresses: list[str], dead: set[str], own: str) -> tuple[list[str], list[dict]]:
    safe: list[str] = []
    rejected: list[dict] = []
    for a in addresses:
        if a.endswith(_IMAGE_SUFFIXES):
            rejected.append({"address": a, "reason": "image"})
        elif not is_safe_recipient(a):
            rejected.append({"address": a, "reason": "blocked_local"})
        elif own and a == own:
            rejected.append({"address": a, "reason": "self"})
        elif a in dead:
            rejected.append({"address": a, "reason": "dead"})
        else:
            safe.append(a)
    return safe, rejected


def _rank(candidates: list[str], company: str) -> list[str]:
    """Recruiting-looking local-part first, then the company's own domain."""
    key = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    order = []
    for i, a in enumerate(candidates):
        local = a.split("@", 1)[0]
        preferred = any(p in local for p in PREFERRED_LOCAL)
        sl = _second_level(_domain(a))
        on_company = bool(key) and bool(sl) and (sl in key or key in sl)
        order.append((0 if preferred else 1, 0 if on_company else 1, i, a))
    return [a for *_, a in sorted(order)]


# --------------------------------------------------------------- instructions

def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|[\n\r]+", text or "")
    return [p.strip() for p in parts if p.strip()]


def _snippet(text: str, needle: str) -> str:
    """The sentence containing `needle` plus the one after it, <=400 chars."""
    if not needle:
        return ""
    low_needle = needle.lower()
    sents = _sentences(text)
    for i, s in enumerate(sents):
        if low_needle in s.lower():
            out = s if i + 1 >= len(sents) else f"{s} {sents[i + 1]}"
            return re.sub(r"\s+", " ", out).strip()[:400]
    return ""


_INSTRUCTION_SYSTEM = (
    "You read one LinkedIn post and report HOW the poster says to apply. You "
    "never invent an email address, a name, a URL or a deadline — you only "
    "report what the post literally says. If the post does not say, use an "
    "empty string. Respond with valid JSON only."
)

_INSTRUCTION_PROMPT = """Read this post and report the application instructions.

POST:
\"\"\"
{post}
\"\"\"

Respond with exactly this JSON and nothing else:
{{"channel":"email|external_link|linkedin_easy_apply|dm|unknown",
 "application_email":"",
 "subject_line_required":"",
 "deadline":"",
 "instructions":"one or two sentences, max 300 chars"}}
"""


def _clip(value, limit: int) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", str(value).strip())[:limit]


def _normalize_instructions(raw, allowed: set[str]) -> dict | None:
    """Clamp the model's raw JSON in code, mirroring agents/outreach._normalize_outreach.

    `allowed` is the set of addresses the REGEX found AND that survived the
    safety screen — not merely "addresses present in the text". A blocked inbox
    the model helpfully re-surfaces is still a blocked inbox.
    """
    if not isinstance(raw, dict):
        return None
    channel = str(raw.get("channel") or "").strip().lower()
    if channel not in APPLY_CHANNELS:
        channel = "unknown"

    email = str(raw.get("application_email") or "").strip().lower().rstrip(TRAILING_PUNCT)
    if email and (email not in allowed or not is_safe_recipient(email)):
        logger.warning(f"[linkedin_outreach] discarded LLM recipient not present "
                       f"in the post (or not safe): {email!r}")
        email = ""

    return {
        "channel": channel,
        "application_email": email,
        "instructions": _clip(raw.get("instructions"), 400),
        "subject_hint": _clip(raw.get("subject_line_required"), 200),
        "deadline_hint": _clip(raw.get("deadline"), 120),
    }


def _llm_instructions(text: str, address: str | None, allowed: set[str]) -> dict | None:
    """Optional. Any failure — no Ollama, no cloud key, bad JSON — returns None
    and the caller keeps the regex-only result."""
    try:
        from utils.ollama_client import generate_json      # imported INSIDE the function
        raw = generate_json(_INSTRUCTION_PROMPT.format(post=(text or "")[:3500]),
                            system_prompt=_INSTRUCTION_SYSTEM)
    except Exception as e:
        logger.info(f"[linkedin_outreach] instruction LLM unavailable: {e}")
        return None
    return _normalize_instructions(raw, allowed)


# --------------------------------------------------------------- public API

def _blank() -> dict:
    return {
        "address": None,
        "candidates": [],
        "rejected": [],
        "channel": "unknown",
        "instructions": "",
        "subject_hint": "",
        "deadline_hint": "",
        "confidence": 0.0,
        "source": None,
        "obfuscated": False,
    }


def extract_application_contact(
    text: str | None,
    *,
    company: str = "",
    job_title: str = "",
    dead: set[str] | None = None,
    use_llm: bool = True,
) -> dict:
    """Structured, screened application channel for one posting.

    Returns (stable shape, every key always present):
    {
      "address":            str | None,   # the ONE recipient, already screened
      "candidates":         [str],        # every safe address found, best first
      "rejected":           [{"address": str, "reason": "blocked_local"|"dead"|"image"|"self"}],
      "channel":            "email"|"external_link"|"linkedin_easy_apply"|"dm"|"unknown",
      "instructions":       str,          # <=400 chars, verbatim-derived or LLM summary
      "subject_hint":       str,          # e.g. "DevOps Engineer - 2026"
      "deadline_hint":      str,
      "confidence":         float,        # 0.0-1.0
      "source":             "regex"|"regex+llm"|None,
      "obfuscated":         bool,         # address was mangled and de-mangled
    }
    Never raises. An LLM failure degrades to the regex-only result.
    """
    res = _blank()
    if not text:
        return res

    try:
        norm = _deobfuscate(text)
        raw_found = set(_addresses(text))
        found = _addresses(norm)
        # "obfuscated" means de-mangling REVEALED something, not merely that a
        # character changed — a stray NBSP is not obfuscation.
        res["obfuscated"] = bool(set(found) - raw_found)

        if dead is None:
            dead = _default_dead()
        dead = {str(a).strip().lower() for a in dead}

        candidates, rejected = _screen(found, dead, _own_address())
        candidates = _rank(candidates, company)
        res["candidates"] = candidates
        res["rejected"] = rejected
        address = candidates[0] if candidates else None
        res["address"] = address

        low = norm.lower()
        if address:
            channel = "email"
        elif "easy apply" in low:
            channel = "linkedin_easy_apply"
        elif any("linkedin.com/feed" not in u.lower() for u in _URL_RE.findall(norm)):
            channel = "external_link"
        elif any(h in low for h in _DM_HINTS):
            channel = "dm"
        else:
            channel = "unknown"
        res["channel"] = channel

        if address:
            res["instructions"] = _snippet(norm, address)
            res["confidence"] = 0.55
        else:
            hint = next((h for h in INSTRUCTION_HINTS if h in low), "")
            res["instructions"] = _snippet(norm, hint)
            res["confidence"] = 0.3 if channel != "unknown" else 0.0
        res["source"] = "regex"

        if use_llm and (address or any(h in low for h in INSTRUCTION_HINTS)):
            llm = _llm_instructions(norm, address, set(candidates))
            if llm:
                res["source"] = "regex+llm"
                for key in ("instructions", "subject_hint", "deadline_hint"):
                    if llm[key]:
                        res[key] = llm[key]
                # Regex wins on the address, always. The LLM wins on channel
                # only where regex had no address to go on.
                if not address and llm["channel"] != "unknown":
                    res["channel"] = llm["channel"]
                res["confidence"] = 0.8 if llm["channel"] == channel else 0.6
    except Exception as e:                       # never raise into a scan loop
        logger.warning(f"[linkedin_outreach] extraction failed: {e}")
    return res


# --------------------------------------------------------------- dedup key

# utils.company_names.normalize_company_name drops the anglophone suffixes; the
# LinkedIn feed is worldwide, so these have to go too or "Acme GmbH" and "Acme"
# become two different sends. Stripped only as TRAILING tokens, and never when
# they are the whole name.
_EXTRA_SUFFIX_TOKENS = {
    "gmbh", "mbh", "ug", "kg", "kgaa", "ag", "se", "gbr", "ohg",
    "bv", "nv", "cv", "vof", "aps", "ivs", "oy", "oyj", "ab", "as", "asa",
    "sa", "sas", "sarl", "srl", "spa", "srls", "sl", "slu", "lda", "kft", "sro",
    "zoo", "doo", "dd", "ad", "pty", "pte", "sdn", "bhd", "llp", "lp", "plc",
    "limited", "gk", "kk", "fze", "fzco", "fzc", "dmcc", "wll",
}


def normalize_company_key(company: str | None) -> str:
    """Canonical, ASCII, suffix-free form of a company name.

    Reuses utils.company_names.normalize_company_name (the key the rest of
    JobPilot links Company rows on) and then strips the non-anglophone legal
    suffixes it does not know about. Accents are folded because "Sagué" and
    "Sague" are the same employer to a person and must be to the dedup key.
    """
    cur = unicodedata.normalize("NFKD", company or "").encode("ascii", "ignore").decode("ascii")
    prev = None
    # Alternating passes, because the two suffix tables interleave: normalize_
    # company_name stops at "GmbH" in "Sague Technologies GmbH", and dropping
    # only "gmbh" here would leave "technologies" that it would have taken.
    while cur != prev:
        prev = cur
        cur = normalize_company_name(cur)
        tokens = cur.split()
        while len(tokens) > 1 and tokens[-1] in _EXTRA_SUFFIX_TOKENS:
            tokens.pop()
        cur = " ".join(tokens)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", cur.lower())).strip()


def _normalize_title(title: str | None) -> str:
    s = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", s.lower())).strip()


def outreach_dedup_key(company: str, job_title: str, external_id: str = "") -> str:
    """The identity of "this company, this job".

    32 hex chars deliberately — jobs.dedup_hash is truncated to 16
    (utils/dedup.py) in a unique column, which is thin; this key gates an
    IRREVERSIBLE send (an email cannot be un-sent), so it gets the wider prefix.

    Normalisation is local rather than utils.dedup.normalize_text on purpose:
    this value backs a UNIQUE constraint on outreach_sends, so it must be frozen
    against changes made for job-dedup tuning, and it must not drag the db
    package into an import of a pure module.
    """
    basis = (f"{normalize_company_key(company)}|{_normalize_title(job_title)}"
             f"|{(external_id or '').strip().lower()}")
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------- composition

_DEFAULT_OPT_OUT = ("If you'd rather not receive messages like this, reply STOP "
                    "and I won't contact you or anyone at {company} again.")


def subject_for(job_title: str, company: str, sender_name: str, contact: dict) -> str:
    """contact['subject_hint'] wins verbatim when the post demanded a subject
    line — ignoring an explicit instruction is the fastest way into a filter."""
    hint = _clip((contact or {}).get("subject_hint"), 200)
    if hint:
        return hint
    title = (job_title or "Application").strip()
    company = (company or "").strip()
    where = f" at {company}" if company else ""
    return f"{title}{where} — {sender_name}".strip()


def build_outreach_body(*, sender_name: str, job_title: str, company: str,
                        contact: dict, links: dict | None = None,
                        post_url: str = "", opt_out_line: str = "",
                        cover_opening: str = "") -> str:
    """Per-job body. NOT utils.mailer.default_body — that one hardcodes a fixed
    self-description and would make every message in a batch byte-identical
    apart from the title, which is the exact bulk fingerprint this feature
    exists to avoid.

    `cover_opening` is the opening paragraph of the cover letter agents/tailor.py
    already wrote for THIS job; the caller passes it. Nothing here invents a
    claim about the sender — if the caller has no tailored text, a neutral line
    is used instead.
    """
    contact = contact or {}
    links = links or {}
    company = (company or "").strip()
    title = (job_title or "the role").strip()
    where = f" at {company}" if company else ""

    seen = f"I saw your post about the {title} role{where}"
    seen += f" ({post_url.strip()})." if (post_url or "").strip() else "."

    body = [f"Dear {'Hiring Team' if not company else company + ' team'},", "", seen, ""]

    substance = re.sub(r"\s+", " ", (cover_opening or "").strip())
    body.append(substance[:600] if substance else
                "I am applying for it, and I have attached materials written "
                "specifically for this role rather than a generic pack.")
    body.append("")

    hint = _clip(contact.get("subject_hint"), 200)
    if hint:
        body.append(f'The post asks for the reference "{hint}", which is on the '
                    f"subject line of this email.")
        body.append("")

    body.append("My tailored resume and cover letter for this role are attached.")
    body.append("")
    body.append("Thank you for your time and consideration.")
    body.append("")
    body.append(f"Kind regards,\n{sender_name}")

    tail = []
    if links.get("linkedin"):
        tail.append(f"LinkedIn: {links['linkedin']}")
    if links.get("github"):
        tail.append(f"GitHub: {links['github']}")
    if tail:
        body.append("\n".join(tail))

    line = (opt_out_line or _DEFAULT_OPT_OUT.format(company=company or "your company")).strip()
    if line:
        body.append("")
        body.append("--")
        body.append(line)
    return "\n".join(body).rstrip() + "\n"


# --------------------------------------------------------------- opt-outs

# Word-boundary anchored: "stopwatch" and "nonstop" are not opt-outs, and a
# substring test would suppress a real recruiter over the word "nonstop".
_OPTOUT_RE = re.compile(
    r"(?<![a-z0-9])"
    r"(?:stop|unsubscribe|opt[\s\-_]?out|remove\s+me|do\s+not\s+contact|"
    r"don'?t\s+contact|no\s+more\s+emails)"
    r"(?![a-z0-9])",
    re.I,
)


def is_optout(subject: str | None, body: str | None) -> bool:
    """Subject, or the first 300 chars of the body, asks us to stop."""
    return bool(_OPTOUT_RE.search(subject or "")
                or _OPTOUT_RE.search((body or "")[:300]))


def _sender_address(header: str) -> str:
    m = EMAIL_RE.search(header or "")
    return m.group(0).strip().lower().rstrip(TRAILING_PUNCT) if m else ""


def scan_optouts(hours: int = 168, limit: int = 60) -> dict:
    """Read recent replies for STOP / unsubscribe / "do not contact" and write
    OutreachSuppression rows. Read-only IMAP via agents.email_reader.recent_messages
    — nothing is marked read, moved, sent or deleted (same contract as
    utils/bounce_watch.scan_bounces). Never raises.

    FAILS CLOSED. An unreadable mailbox means "we do not know whether anyone
    opted out", not "nobody did", so the result carries ok=False and the send
    path must refuse to send on it. This is the opposite of
    bounce_watch.load_dead(), which fails open — a missed bounce costs a wasted
    email, a missed STOP costs a person who asked to be left alone.
    """
    result = {"scanned": 0, "new_suppressions": 0, "suppressed": [],
              "ok": False, "error": ""}
    try:
        from agents.email_reader import load_gmail_config, recent_messages
        cfg = load_gmail_config()
        own = (cfg.get("address") or "").strip().lower()
        if not own or not cfg.get("app_password"):
            result["error"] = "gmail not configured"
            return result
        since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))
        messages = recent_messages(since, limit=max(1, int(limit)))
    except Exception as e:
        logger.warning(f"[linkedin_outreach] opt-out scan could not read mail: {e}")
        result["error"] = str(e)
        return result

    result["scanned"] = len(messages)
    wanted: list[tuple[str, str]] = []
    for msg in messages:
        subject = msg.get("subject") or ""
        if not is_optout(subject, msg.get("text") or msg.get("body") or ""):
            continue
        addr = _sender_address(msg.get("from") or "")
        if not addr or addr == own:
            continue
        if not any(addr == a for a, _ in wanted):
            wanted.append((addr, subject))

    try:
        result["new_suppressions"] = 0
        result["suppressed"] = _write_suppressions(wanted)
        result["new_suppressions"] = len(result["suppressed"])
        result["ok"] = True
    except Exception as e:
        logger.warning(f"[linkedin_outreach] opt-out scan could not write rows: {e}")
        result["error"] = str(e)
    return result


def _write_suppressions(pairs: list[tuple[str, str]]) -> list[str]:
    """Persist address rows, plus a DOMAIN row where we have actually mailed
    that address about a company — that domain row is what makes the "or anyone
    at {company}" half of the opt-out line in the body true."""
    from db.database import get_session
    from db.models import OutreachSend, OutreachSuppression

    added: list[str] = []
    session = get_session()
    try:
        for addr, subject in pairs:
            values = [(addr, "address")]
            sent = (session.query(OutreachSend)
                    .filter(OutreachSend.recipient == addr).first())
            if sent is not None and (sent.company_normalized or "").strip():
                dom = _domain(addr)
                if dom:
                    values.append((dom, "domain"))
            for value, scope in values:
                exists = (session.query(OutreachSuppression)
                          .filter(OutreachSuppression.value == value,
                                  OutreachSuppression.scope == scope).first())
                if exists is not None:
                    continue
                session.add(OutreachSuppression(
                    value=value, scope=scope, source="reply_scan",
                    reason=f"reply: {(subject or 'STOP')[:80]}"))
                session.flush()
                added.append(value)
                logger.info(f"[linkedin_outreach] suppressed {scope} {value}")
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return added
