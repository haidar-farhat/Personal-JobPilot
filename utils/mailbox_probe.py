"""Look harder for a PUBLISHED address, and prove a CONSTRUCTED one exists.

WHY THIS EXISTS
---------------
Measured on 40 sampled jobs from the real jobpilot.db, the resolver found an
address for 15 (38%). Of the 25 misses, 16 were `domain_unconfirmed` and 8
`no_candidate` — so the bottleneck is DISCOVERY, not the mailbox. Optum is the
clearest case: the resolver settled on optum.com, a domain that plainly exists
and runs a careers operation, and still returned `no_candidate`, because
utils.company_email.crawl_for_email fetches exactly five URLs ("", /careers,
/jobs, /contact, /about) and then gives up. A site that publishes its recruiting
inbox at /company/contact-us, or behind a "Work with us" link, is invisible to
it.

So this module does two things, in the order that matters:

  (A) deep_crawl_for_email — 15 candidate paths instead of 5, and then up to a
      few IN-PAGE LINKS whose text or href reads as careers/contact, so a
      non-standard path is still reachable. One host, a hard request budget, a
      delay between requests, an overall wall-clock deadline, and robots.txt
      Disallow honoured for every path it would fetch.

  (B) verify_mailbox — an SMTP RCPT TO probe that asks the receiving server
      whether a mailbox exists, WITHOUT ever sending DATA and therefore without
      sending any mail. Today a constructed address is only found out by
      bouncing: utils/bounce_watch.py learns it AFTER the fact, from a Gmail
      delivery-failure notice. Checking first is strictly better — the bounce
      avoided is one the sending reputation never pays for.

Discovery is tried first for a reason: an address the employer published is
worth more than any verdict about one we invented (utils/recipient.py CONFIDENCE
— company_site 0.75 vs constructed 0.4). Verification cannot promote a guess to
a published address; it can only stop the worst guesses.

WHAT VERIFICATION IS ACTUALLY WORTH — read this before trusting a result
-----------------------------------------------------------------------
Many large providers — notably Microsoft 365 and Google Workspace, which between
them host a large share of corporate mail — deliberately accept EVERY RCPT TO
and decide about the recipient afterwards. On such a domain a "valid" answer
would mean nothing, so this module probes a random, certainly-nonexistent local
part FIRST: if that is accepted the domain is a catch-all, and the result is
"catch_all" — never "valid".

What the caller should do with each status:

  valid      the server distinguished a real mailbox from a fake one and
             accepted this address. The strongest signal short of sending.
  catch_all  UNVERIFIED BUT PLAUSIBLE — the guess is neither proven nor
             disproven. Treat it exactly as a guess is treated today: usable if
             the caller's other gates pass, still subject to the
             constructed-address quota, still liable to bounce.
  invalid    the server explicitly refused this recipient (5xx). Do not send.
  unknown    greylisted, blocked, unreachable, timed out, or no MX. Says
             nothing; treat it as catch_all, i.e. as an unverified guess.

This REDUCES bounces, it does not eliminate them: a catch-all domain can still
reject after DATA, and a server that accepted the RCPT can still emit an
asynchronous bounce. utils/bounce_watch.py stays the backstop.

Failure is always into uncertainty. A refused connection, a timeout, a 4xx
(greylisting is extremely common on first contact) and an unparseable reply all
return "unknown", never "invalid" — a false "invalid" silently discards a good
address and nothing downstream would ever notice.

MANNERS
-------
Both halves are rate-limited and cached under output/, the same way
utils/company_email.py caches crawl results: repeatedly hitting one domain is
what gets a crawler blocked and a prober blacklisted. A verification result is
cached for CACHE_TTL_DAYS, except "unknown", which is never cached because it is
exactly the transient answer that deserves a retry.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import smtplib
import tempfile
import threading
import time
from html import unescape
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests

from utils.company_email import _TIMEOUT, _UA, _addresses_on
from utils.mailer import PREFERRED_LOCAL, is_safe_recipient

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
_CACHE_DIR = ROOT / "output" / "mailbox_probe"

# ---------------------------------------------------------------------------
# (A) crawl tuning
# ---------------------------------------------------------------------------

# utils.company_email._PATHS, then the paths its five misses actually used.
# Ordered by observed hit rate, because a budget that runs out spends itself on
# whatever came first.
DEEP_PATHS: tuple[str, ...] = (
    "", "/careers", "/jobs", "/contact", "/about",
    "/contact-us", "/company", "/about-us", "/careers/contact", "/hiring",
    "/team", "/people", "/work-with-us", "/join-us", "/jobs/apply",
)

# Link text / href fragments that promise a careers or contact page. Kept
# lowercase and matched as substrings against both, so "/en-us/Careers/" and
# "Work With Us" both qualify.
_LINK_HINTS: tuple[str, ...] = (
    "career", "job", "hiring", "hire", "recruit", "talent", "vacanc",
    "opening", "contact", "get in touch", "reach us", "join", "work with us",
    "work for us", "employment", "apply",
)
# Never spend a budgeted request on a file that cannot contain a mailto.
_SKIP_LINK_EXT = (".pdf", ".zip", ".doc", ".docx", ".png", ".jpg", ".jpeg",
                  ".gif", ".svg", ".webp", ".mp4", ".ics", ".xml", ".css", ".js")

_CRAWL_DELAY = 0.5          # seconds between requests to the one host
_CRAWL_DEADLINE = 30.0      # wall-clock ceiling for a whole domain
_DEFAULT_BUDGET = 8
_DEFAULT_MAX_LINKS = 4
_MAX_PAGE_CHARS = 300_000   # a marketing homepage can be megabytes of inlined JS

_ANCHOR_RE = re.compile(r"(?is)<a\b[^>]*?href\s*=\s*[\"']([^\"'#][^\"']*)[\"'][^>]*>(.*?)</a>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")

# ---------------------------------------------------------------------------
# (B) probe tuning
# ---------------------------------------------------------------------------

SMTP_PORT = 25
# The null sender is what a bounce uses, so it is the sender every MTA is
# obliged to accept; a probe from a real address looks like the start of a real
# delivery and is more likely to be greylisted.
MAIL_FROM = ""
CACHE_TTL_DAYS = 14
_PROBE_DELAY = 2.0          # seconds between probes against ONE mail host

# Seam for the tests: the whole SMTP conversation goes through this name, so a
# test can substitute a recorder and assert on every command sent. Nothing here
# ever calls .data() or .send_message() — see test_no_data_command_is_ever_sent.
_SMTP_CLASS = smtplib.SMTP

_STATUS_VALID = "valid"
_STATUS_INVALID = "invalid"
_STATUS_CATCH_ALL = "catch_all"
_STATUS_UNKNOWN = "unknown"

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}
_LAST_PROBE: dict[str, float] = {}   # mail host -> monotonic time of last probe
_MX_MEMO: dict[str, list[str]] = {}  # MX rarely changes mid-run (as company_email memoises)


def _key_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def _norm_domain(domain: str) -> str:
    """'https://WWW.Acme.com/careers' -> 'acme.com'."""
    host = (domain or "").strip().lower()
    host = re.sub(r"^[a-z]+://", "", host).split("/")[0].split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _session(session: requests.Session | None = None) -> requests.Session:
    if session is not None:
        return session
    s = requests.Session()
    s.headers.update({"User-Agent": _UA})
    return s


def _same_org(address: str, domain: str) -> bool:
    """Is this address ON the company's own domain (or a subdomain of it)?

    A company site prints other people's addresses — its ATS vendor, its press
    agency, a partner. Mailing one of those is worse than mailing nothing, so
    the same rule utils.company_email.crawl_for_email uses applies here.
    """
    host = address.split("@")[-1].lower()
    return host == domain or host.endswith("." + domain)


def _rank(address: str) -> int:
    """0 for a recruiting inbox, 1 for anything else. Lower sorts first."""
    local = address.split("@", 1)[0]
    return 0 if any(p in local for p in PREFERRED_LOCAL) else 1


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

def _robots_rules(domain: str, sess: requests.Session, *,
                  timeout: float = 5.0) -> tuple[list[str], list[str]]:
    """(disallow, allow) path prefixes that apply to us, from /robots.txt.

    A tiny parser rather than urllib.robotparser because that one opens its own
    connection with its own user agent and its own timeout, which would defeat
    the single-session, single-host, budgeted manners of the crawl.

    Precedence follows RFC 9309: a 4xx (including the usual 404) means "no rules
    published, crawl freely"; an explicit 5xx means the site is telling us to
    come back later, so nothing is fetched. A CONNECTION failure is treated as
    "no rules" — it is indistinguishable from a site with no robots.txt at all,
    and treating every flaky TLS handshake as a total ban would silently switch
    discovery off for exactly the small employers this module exists to reach.
    """
    try:
        r = sess.get(f"https://{domain}/robots.txt", timeout=timeout,
                     allow_redirects=True)
    except requests.RequestException:
        return [], []
    if r.status_code >= 500:
        return ["/"], []
    if r.status_code != 200 or not r.text:
        return [], []

    disallow: list[str] = []
    allow: list[str] = []
    applies = False
    # A blank line ends a group; consecutive User-agent lines share one group.
    for raw in (r.text[:100_000]).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            applies = False
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            agent = value.lower()
            applies = agent == "*" or agent in _UA.lower() or "jobpilot" in agent
        elif applies and field == "disallow" and value:
            disallow.append(value)
        elif applies and field == "allow" and value:
            allow.append(value)
    return disallow, allow


def _robots_allows(rules: tuple[list[str], list[str]], path: str) -> bool:
    """RFC 9309 longest-match: the most specific rule wins, Allow breaks ties."""
    disallow, allow = rules
    p = path or "/"
    hit = max((len(d) for d in disallow if p.startswith(d)), default=-1)
    if hit < 0:
        return True
    return max((len(a) for a in allow if p.startswith(a)), default=-1) >= hit


# ---------------------------------------------------------------------------
# (A) the deeper crawl
# ---------------------------------------------------------------------------

def _text_of(fragment: str, *, tags: bool = True) -> str:
    """Anchor inner HTML as plain text: '<span>Work&nbsp;with&nbsp;us</span>'."""
    text = _TAG_RE.sub(" ", fragment or "") if tags else (fragment or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _link_is_promising(href: str, text: str) -> bool:
    blob = f"{href} {text}".lower()
    return any(h in blob for h in _LINK_HINTS)


def _candidate_links(page_url: str, html_text: str, domain: str) -> list[str]:
    """Same-host URLs on this page whose text or href promises careers/contact.

    Order is page order, which on a real site puts the nav and the footer — the
    two places a careers link actually lives — at the ends and the body copy
    between them.
    """
    out: list[str] = []
    for href, inner in _ANCHOR_RE.findall(html_text or ""):
        text = _text_of(inner)
        if not _link_is_promising(href, text):
            continue
        url = urljoin(page_url, _text_of(href, tags=False))
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            continue
        host = (parsed.hostname or "").lower()
        host = host[4:] if host.startswith("www.") else host
        if host != domain and not host.endswith("." + domain):
            continue                       # one host, per the crawl contract
        if parsed.path.lower().endswith(_SKIP_LINK_EXT):
            continue
        clean = url.split("#")[0]
        if clean not in out:
            out.append(clean)
    return out


def deep_crawl_for_email(
    domain: str, *,
    session: requests.Session | None = None,
    budget: int = _DEFAULT_BUDGET,
    max_links: int = _DEFAULT_MAX_LINKS,
    delay: float = _CRAWL_DELAY,
    deadline: float = _CRAWL_DEADLINE,
    stop_on_preferred: bool = True,
) -> list[tuple[str, str]]:
    """Every safe address published on this company's own site, with its page.

    Returns [(address, source_url), ...] ranked so a recruiting inbox
    (PREFERRED_LOCAL) comes first, then page order. Empty when nothing is found.
    Provenance is part of the return value, not a log line, because the caller
    records the source URL alongside the address it sends to.

    Wider than utils.company_email.crawl_for_email in two ways: DEEP_PATHS tries
    fifteen paths instead of five, and after those it follows up to `max_links`
    in-page links whose text or href reads as careers/contact — which is the only
    way to reach a site that publishes its inbox at /en/company/get-in-touch.

    Manners, all enforced rather than documented: one host, at most `budget`
    page requests, `delay` seconds between them, a `deadline` second wall-clock
    ceiling for the whole domain, and robots.txt Disallow checked BEFORE a path
    is fetched (a disallowed path costs no request, so the budget is spent on
    pages we are welcome to read). The robots.txt fetch itself is one extra
    request outside the budget.

    `stop_on_preferred` returns as soon as a recruiting inbox has been found:
    finding careers@ on the homepage and then fetching seven more pages is rude
    and buys nothing. Set it False to collect everything the site publishes.

    Unlike crawl_for_email this returns NON-preferred addresses too (ranked
    last). It is a discovery function, not a decision: the caller still applies
    its own trust rules, and this way it can see that a domain publishes only
    bd@ instead of concluding the site publishes nothing.
    """
    domain = _norm_domain(domain)
    if not domain:
        return []

    sess = _session(session)
    stop_at = time.monotonic() + max(0.0, deadline)
    rules = _robots_rules(domain, sess)
    spent = 0
    last_request = 0.0

    found: dict[str, str] = {}      # address -> first URL it was seen on
    links: list[str] = []           # promising links, discovered as we go
    visited: set[str] = set()

    def out_of_time() -> bool:
        return time.monotonic() >= stop_at

    def have_preferred() -> bool:
        return any(_rank(a) == 0 for a in found)

    def fetch(url: str) -> str | None:
        nonlocal spent, last_request
        remaining = stop_at - time.monotonic()
        if remaining <= 0:
            return None
        wait = delay - (time.monotonic() - last_request)
        if last_request and wait > 0:
            time.sleep(min(wait, remaining))
        spent += 1
        last_request = time.monotonic()
        try:
            r = sess.get(url, timeout=min(_TIMEOUT, max(1.0, stop_at - last_request)),
                         allow_redirects=True)
        except requests.RequestException as e:
            logger.debug(f"[mailbox_probe] {url} unreachable: {e}")
            return None
        if r.status_code != 200 or not r.text:
            return None
        return r.text[:_MAX_PAGE_CHARS]

    def harvest(url: str, html_text: str) -> None:
        for addr in _addresses_on(html_text):
            if addr in found or not _same_org(addr, domain):
                continue
            if not is_safe_recipient(addr):
                continue               # accommodation / legal / no-reply inbox
            found[addr] = url

    # Reserve part of the budget for links discovered on the pages we fetch.
    # Without this the fixed list (longer than the default budget of 8) spends
    # every request before the follow loop runs, so link-following was dead code
    # and a careers page on a non-standard path — "/join-the-crew", "/life-here"
    # — was unreachable. Those are exactly the small employers most likely to
    # publish an address at all.
    reserve = min(max_links, max(0, budget // 3)) if max_links else 0
    path_budget = max(1, budget - reserve)

    def sweep(limit: int) -> None:
        """Fetch unvisited fixed paths until `limit` requests are spent."""
        nonlocal spent
        for path in DEEP_PATHS:
            if spent >= limit or out_of_time():
                break
            if not _robots_allows(rules, path or "/"):
                logger.debug(f"[mailbox_probe] robots.txt disallows {domain}{path}")
                continue
            url = f"https://{domain}{path}"
            if url in visited:
                continue
            visited.add(url)
            html_text = fetch(url)
            if html_text is None:
                continue
            harvest(url, html_text)
            if stop_on_preferred and have_preferred():
                return
            for link in _candidate_links(url, html_text, domain):
                if link not in visited and link not in links:
                    links.append(link)

    sweep(path_budget)

    followed = 0
    for link in links:
        if (spent >= budget or followed >= max_links or out_of_time()
                or (stop_on_preferred and have_preferred())):
            break
        if link in visited:
            continue
        if not _robots_allows(rules, urlparse(link).path or "/"):
            continue
        visited.add(link)
        followed += 1
        html_text = fetch(link)
        if html_text is None:
            continue
        harvest(link, html_text)

    # Anything the reserve did not need goes back to the fixed list, so a site
    # with no promising links still gets the full budget.
    if spent < budget and not (stop_on_preferred and have_preferred()):
        sweep(budget)

    hits = sorted(found.items(), key=lambda kv: _rank(kv[0]))
    logger.info(f"[mailbox_probe] {domain}: {len(hits)} address(es) in "
                f"{spent} request(s)")
    return hits


def deep_crawl_best(domain: str, **kwargs: Any) -> tuple[str, str] | None:
    """The single best published address, shaped like crawl_for_email's return.

    A drop-in for utils.company_email.crawl_for_email at the call site, except
    that it also returns a non-recruiting address when that is all the site
    publishes — the caller decides what to do with a `bd@`, this module does not
    hide it.
    """
    hits = deep_crawl_for_email(domain, **kwargs)
    return hits[0] if hits else None


# ---------------------------------------------------------------------------
# (B) mailbox verification
# ---------------------------------------------------------------------------

def _result(status: str, *, code: int | None, detail: str,
            mx: str | None) -> dict[str, Any]:
    """The one shape every path returns. Keys are API — the dashboard reads them."""
    return {"status": status, "code": code, "detail": detail, "mx": mx}


def _cache_path(address: str) -> Path:
    safe = re.sub(r"[^a-z0-9.@\-]", "_", (address or "").lower())
    return _CACHE_DIR / f"{safe}.json"


def _read_cache(address: str, *, ttl_days: int) -> dict[str, Any] | None:
    try:
        rec = json.loads(_cache_path(address).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None                      # unreadable counts as a miss
    if not isinstance(rec, dict) or rec.get("status") not in (
            _STATUS_VALID, _STATUS_INVALID, _STATUS_CATCH_ALL):
        return None
    age = time.time() - float(rec.get("checked_at") or 0)
    if age > ttl_days * 86400 or age < 0:
        return None
    return _result(rec["status"], code=rec.get("code"),
                   detail=rec.get("detail") or "", mx=rec.get("mx"))


def _write_cache(address: str, rec: dict[str, Any]) -> None:
    # An "unknown" is never cached: greylisting, a timeout and a blocked port
    # all produce it, and all three are worth retrying tomorrow.
    if rec.get("status") == _STATUS_UNKNOWN:
        return
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = dict(rec, address=address, checked_at=time.time())
        fd, tmp = tempfile.mkstemp(dir=str(_CACHE_DIR), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(tmp, _cache_path(address))
    except OSError as e:
        logger.warning(f"[mailbox_probe] could not cache {address}: {e}")


def mx_hosts(domain: str) -> list[str]:
    """MX hostnames for the domain, best preference first; [] when there are none.

    Mirrors utils.company_email.has_mx, including its nslookup fallback: on this
    machine dnspython times out where the system resolver answers immediately,
    and treating that as "no MX" once disabled address lookup entirely.
    """
    domain = _norm_domain(domain)
    if not domain:
        return []
    if domain in _MX_MEMO:
        return list(_MX_MEMO[domain])
    hosts: list[tuple[int, str]] = []
    try:
        import dns.resolver
        resolver = dns.resolver.Resolver()
        resolver.lifetime = resolver.timeout = 5
        for rdata in resolver.resolve(domain, "MX"):
            hosts.append((int(rdata.preference), str(rdata.exchange).rstrip(".")))
    except ImportError:
        pass
    except Exception as e:
        if type(e).__name__ in ("NXDOMAIN", "NoAnswer", "NoNameservers"):
            _MX_MEMO[domain] = []
            return []

    if not hosts:
        try:
            import subprocess
            out = subprocess.run(["nslookup", "-type=MX", domain], capture_output=True,
                                 text=True, timeout=8).stdout
            for m in re.finditer(r"mail exchanger\s*=\s*(\d+)?\s*,?\s*([A-Za-z0-9.\-]+)",
                                 out, re.I):
                hosts.append((int(m.group(1) or 10), m.group(2).rstrip(".")))
        except Exception:
            return []          # not memoised: a resolver hiccup deserves a retry
    ordered = [h for _, h in sorted(hosts) if h and h != "."]
    _MX_MEMO[domain] = ordered
    return list(ordered)


def _catch_all_probe_address(domain: str) -> str:
    """A local part no real mailbox can be. Random, so a server cannot learn it."""
    return f"jp-{random.randrange(16 ** 12):012x}-probe@{domain}"


def _classify(code: int | None) -> str:
    if code is None:
        return _STATUS_UNKNOWN
    if 200 <= code < 300:
        return _STATUS_VALID
    if 400 <= code < 500:
        # Greylisting ("try again later") lives here and is extremely common on
        # a first contact. Calling it invalid would throw away good addresses.
        return _STATUS_UNKNOWN
    if code >= 500:
        return _STATUS_INVALID
    return _STATUS_UNKNOWN


def _decode(reply: Any) -> str:
    if isinstance(reply, bytes):
        return reply.decode("utf-8", "replace").strip()
    return str(reply or "").strip()


def verify_mailbox(address: str, *, timeout: int = 10, refresh: bool = False,
                   ttl_days: int = CACHE_TTL_DAYS,
                   mail_from: str = MAIL_FROM) -> dict[str, Any]:
    """Does this mailbox exist? Ask the receiving server, without sending mail.

    Returns {"status", "code", "detail", "mx"} where status is one of
    "valid" | "invalid" | "catch_all" | "unknown". Read the module docstring
    before acting on it — on a Microsoft 365 or Google Workspace domain the
    honest answer is almost always "catch_all", which means "unverified but
    plausible", not "good".

    The conversation is MX lookup, connect, EHLO, MAIL FROM (null sender),
    RCPT TO a random nonexistent local part, RCPT TO the real address, QUIT.
    DATA is never sent, so no message is ever transmitted — this is the standard
    existence check, and it is the same exchange a sending MTA would open before
    deciding to deliver.

    The random probe comes FIRST and short-circuits everything: a server that
    accepts a made-up recipient accepts all of them, and a "valid" verdict from
    it would be a lie. Both RCPTs share one connection and one transaction, so
    the domain sees a single conversation rather than two.

    Every failure — connection refused, timeout, TLS error, 4xx greylisting,
    garbled reply — returns "unknown". Failing into uncertainty is the whole
    safety property: an address wrongly marked "invalid" is discarded silently
    and forever.
    """
    address = (address or "").strip().lower()
    if "@" not in address:
        return _result(_STATUS_UNKNOWN, code=None, detail="not an address", mx=None)
    domain = address.split("@", 1)[1]

    with _key_lock(address):
        if not refresh:
            cached = _read_cache(address, ttl_days=ttl_days)
            if cached is not None:
                return cached

        hosts = mx_hosts(domain)
        if not hosts:
            # No MX is not "invalid": the domain may be misconfigured today, or
            # our resolver may be the thing that is broken.
            return _result(_STATUS_UNKNOWN, code=None,
                           detail=f"no MX records for {domain}", mx=None)

        rec = _probe(hosts[0], address, timeout=timeout, mail_from=mail_from)
        _write_cache(address, rec)
        return rec


def _probe(host: str, address: str, *, timeout: int,
           mail_from: str) -> dict[str, Any]:
    """One SMTP conversation. Never sends DATA; never raises."""
    domain = address.split("@", 1)[1]
    smtp = None
    try:
        smtp = _SMTP_CLASS(host, SMTP_PORT, timeout=timeout)
        code, msg = smtp.ehlo()
        if code >= 400:
            code, msg = smtp.helo()
        if code >= 400:
            return _result(_STATUS_UNKNOWN, code=code,
                           detail=f"greeting refused: {_decode(msg)}", mx=host)

        code, msg = smtp.mail(mail_from)
        if code >= 400:
            # A server that will not take the null sender tells us nothing
            # about the mailbox.
            return _result(_STATUS_UNKNOWN, code=code,
                           detail=f"MAIL FROM refused: {_decode(msg)}", mx=host)

        decoy = _catch_all_probe_address(domain)
        code, msg = smtp.rcpt(decoy)
        if _classify(code) == _STATUS_VALID:
            return _result(_STATUS_CATCH_ALL, code=code,
                           detail=f"{host} accepted a random local part — every "
                                  f"RCPT is accepted, so this address is neither "
                                  f"proven nor disproven", mx=host)
        if _classify(code) == _STATUS_UNKNOWN:
            # The decoy was greylisted or throttled, so the real address would
            # be too, and a rejection of it would not mean "no such mailbox".
            return _result(_STATUS_UNKNOWN, code=code,
                           detail=f"catch-all probe inconclusive: {_decode(msg)}",
                           mx=host)

        code, msg = smtp.rcpt(address)
        status = _classify(code)
        detail = {
            _STATUS_VALID: "server distinguishes real mailboxes and accepted this one",
            _STATUS_INVALID: "server rejected this recipient",
            _STATUS_UNKNOWN: "temporary refusal (greylisting or throttling)",
        }[status]
        return _result(status, code=code, detail=f"{detail}: {_decode(msg)}", mx=host)

    except (smtplib.SMTPException, OSError, ValueError) as e:
        # Refused, timed out, dropped mid-conversation, port 25 blocked by the
        # local network — all of it is uncertainty, none of it is "invalid".
        return _result(_STATUS_UNKNOWN, code=None,
                       detail=f"{type(e).__name__}: {e}", mx=host)
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                try:
                    smtp.close()
                except Exception:
                    pass


def verify_many(addresses: Iterable[str], *, timeout: int = 10,
                delay: float = _PROBE_DELAY, refresh: bool = False,
                ttl_days: int = CACHE_TTL_DAYS) -> dict[str, dict[str, Any]]:
    """verify_mailbox over several addresses, politely.

    Consecutive probes against the SAME mail host are spaced by `delay` seconds;
    different hosts are not made to wait on each other, and a cache hit costs no
    delay at all. Probing one domain in a tight loop is exactly what earns a
    prober a place on a blocklist, and the resolver naturally produces runs of
    addresses at one company.

    Returns {address: result}, deduplicated, in first-seen order.
    """
    out: dict[str, dict[str, Any]] = {}
    for raw in addresses or ():
        address = str(raw or "").strip().lower()
        if not address or address in out:
            continue
        if "@" not in address:
            out[address] = _result(_STATUS_UNKNOWN, code=None,
                                   detail="not an address", mx=None)
            continue
        if not refresh:
            cached = _read_cache(address, ttl_days=ttl_days)
            if cached is not None:
                out[address] = cached
                continue
        host = (mx_hosts(address.split("@", 1)[1]) or [""])[0]
        last = _LAST_PROBE.get(host)
        if host and last is not None:
            wait = delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        out[address] = verify_mailbox(address, timeout=timeout, refresh=refresh,
                                      ttl_days=ttl_days)
        if host:
            _LAST_PROBE[host] = time.monotonic()
    return out


def cache_size() -> int:
    try:
        return len(list(_CACHE_DIR.glob("*.json")))
    except OSError:
        return 0


def clear_probe_cache() -> None:
    """Drop cached verdicts (tests, and a manual re-check of a whole domain)."""
    try:
        for p in _CACHE_DIR.glob("*.json"):
            p.unlink()
    except OSError as e:
        logger.warning(f"[mailbox_probe] could not clear cache: {e}")
    _LAST_PROBE.clear()
    _MX_MEMO.clear()
