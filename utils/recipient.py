"""One recipient resolver, shared by the auto-applier and the Mail Agent.

WHY THIS EXISTS
---------------
Two code paths resolved an application address and they DISAGREED, so the same
job could be mailable by one agent and blocked by the other:

  * agents/auto_applier/runner._resolve_recipient — reads the posting first
    (utils.mailer.find_employer_recipient), then utils.company_email, then
    screens with is_safe_recipient and a DAILY CAP that applies to CONSTRUCTED
    addresses only. Published addresses are uncapped, because the employer
    printed them itself; a constructed address can bounce, and repeatedly
    mailing dead boxes is what gets a sender flagged.
  * server/outreach_mail._company_fallback_recipient — the same gathering, but
    it then refuses EVERY constructed address whose domain did not come from
    the employer's own posting URL. That is why the Mail Agent blocks nearly
    everything: LinkedIn-sourced rows never carry an employer URL, so the
    fallback rejected them all.

The blanket refusal was not paranoia, it was a measured defect it could not fix
any other way. utils.company_email.domain_belongs_to accepts a domain when the
company name appears anywhere on its homepage, and for a SHORT GENERIC name
that is satisfied by coincidence. Verified on real rows in jobpilot.db:

    domain_belongs_to("moab.com",   "Moab")   -> True   # a Utah travel site
    domain_belongs_to("garage.com", "Garage") -> True   # a stranger's site

Both are strangers. A CV sent to a stranger is worse than no CV sent, and it is
also the failure the sender's reputation pays for.

So this module keeps the runner's ORDER and its constructed-address cap, and
replaces the blanket refusal with `domain_confidently_belongs_to` — an
ownership test strong enough that the Mail Agent can safely use the same
gathering the regular agent uses. The rule, in one line: a name distinctive
enough that a mention cannot be coincidence passes on a plain mention; a short
or common-word name must be named as an EMPLOYER (a careers/jobs context) or
own the page's identity (<title>/og:site_name), and a parked or placeholder
page never passes at all.

MEASURED
--------
On a 20-case table built from company names in the `jobs` table of the real
jobpilot.db plus the known false positives above (tests/test_recipient.py), this
rule scores 20/20 where `domain_belongs_to` scores 13/20. All seven of the old
rule's errors are the same error — accepting a stranger's domain: moab.com/Moab,
garage.com/Garage, cape.com/Cape, air.com/Air, campfire.com/Campfire,
scaleai.co/"Scale AI" and salt.com/Salt. It has no false NEGATIVES, and neither
does this one: ramp.com/Ramp, hex.tech/Hex, vanta.com/Vanta, axon.com/Axon,
notion.so/Notion and brex.com/Brex are short names on their real sites and all
still pass.

The cost is strictness, paid where it is cheap. Of the 291 distinct companies in
that table, 147 (50%) are distinctive enough to pass on a plain mention and 144
(49%) must produce a title or careers signal — which a real employer's homepage
always has, and a squatter's never does.

WHAT THIS MODULE DOES NOT DO
----------------------------
It never sends, never writes, and never increments a counter. `resolve` is a
pure decision over the inputs it is handed (its only I/O is the lookup it
delegates to utils.company_email, which caches). The quota is passed IN, so the
caller owns the counting — see `guessed_sent_today` for the DB-backed count
that should be passed.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# tuning constants
# ---------------------------------------------------------------------------

# Confidence, high to low. A posting address was printed by the employer for
# exactly this purpose; a crawled one was published by the employer but for
# some purpose; a constructed one is our own invention.
CONFIDENCE = {"posting": 1.0, "company_site": 0.75, "constructed": 0.4}

# Same fetch manners as utils/company_email.py and agents/scanner/providers.py.
_UA = "JobPilot/1.0 (personal job-search agent)"
_TIMEOUT = 10

# A single-token company name only proves ownership by itself when it is long
# enough to be coined rather than borrowed. "Moab" (4), "Cape" (4) and
# "Garage" (6) fall under this floor; "Doximity" (8) and "Anthropic" (9) clear
# it. Chosen from the real `jobs` rows: no company name below 8 characters in
# that table is coincidence-proof, and the shortest coined ones are 8.
_SINGLE_TOKEN_DISTINCTIVE_LEN = 8

# Below this much visible text there is nothing to reason about — an empty
# shell, a JS-only app, or an error page rendered with status 200.
_MIN_PAGE_CHARS = 200

# Legal furniture, dropped before judging distinctiveness.
_LEGAL_SUFFIXES = frozenset("""
inc incorporated llc lp llp ltd limited plc corp corporation company co
gmbh ag sa nv bv srl spa oy ab as pty pte kk kft doo sarl the
""".split())

# Industry filler: real words, but they identify a SECTOR, not a company, so a
# name made only of these is not distinctive.
_GENERIC_BIZ = frozenset("""
ai ml api app apps cloud data digital tech technologies technology software
systems solutions services service consulting consultancy group holdings
holding partners ventures capital labs lab studio studios works agency media
network networks global international worldwide industries enterprises
enterprise energy health healthcare medical financial finance bank insurance
security analytics platform platforms interactive innovations innovation
robotics logistics automation intelligence research development brands
""".split())

# Common English words that are also popular company names. A name built only
# from these is a word anyone's homepage may contain by accident, so it has to
# earn ownership with a stronger signal than a plain mention. This list only
# ever makes the check STRICTER: a real company whose name is here still
# passes on a <title> or careers-context match, which every real company site
# has. Curated, not a dictionary — a missing word costs strictness, never
# correctness, for names of >= 8 characters.
_COMMON_WORDS = frozenset("""
about above air alpha anchor apple arc arch arrow atlas aurora axis
banana base basin beacon bear bell bench best beta bird black block blue board
boat bolt book boost border box brain branch brand bread bridge bright brook
brush bubble buffer build bunch cabin cable camp campfire canvas cape car card
care cargo carrot cash castle catch cause cell center chain chair chalk chance
change channel chapter charge chart check cherry chief circle city clarity
class clean clear cliff climb clock closet cloud coach coast code coffee coin
color comet common compass core corner cotton course court cover craft crane
creek crest cross crowd crown cube cup current curve cycle daily dairy dance
dawn deck deep delta demand desk diamond dial dime direct dish dock dollar
door dot double dove down draft dream drive drop dust eagle earth east echo
edge egg eight elder element elm ember empire engine equal escape estate ever
exact fable face fact fair falcon fall family farm fast feather feed fence
fern field figure film filter fine finish fire first fish five flag flame
flash fleet flint float flock floor flow flower focus fold forest forge form
fort forward foundry fountain four fox frame free fresh friend front frost
fruit fuel full fusion future garage garden gate gather gear gem gift give
glass globe glow gold golden good grace grade grain grand granite grape graph
grass gravel gray great green grid grove guard guide gulf habit half hammer
hand happy harbor hard hare harvest hatch haven hawk hay head health hearth
heart heat hedge helm help hen herb hero hex high hill hive hold hollow home
honey hook hope horizon horse house human hunt ice idea image impact index
indigo iron island ivory ivy jade jam jet join journey joy jump junction
juniper keen keep kettle key kind king kite knot label lake lamp land lane
lantern large last latch laurel lava lawn layer leaf leap learn ledge left
legacy lemon lens level lever liberty life lift light lily lime line linear
link lion list little live local lock lodge log logic loop lotus loud love
lucky lumber lunar lush machine magnet main major make mango manor maple
marble march mark market marsh mason mast match meadow measure medal melon
mercury merit mesa metal meter middle midnight mile milk mill mind mine mint
mirror mission mist mobile modal model modern moment money monitor moon moose
morning motion motor mount mountain move music narrow native nature near neat
nectar needle nest net never new next night nine noble node noon north note
notion nova novel oak oasis object ocean octave office olive omega onward
open opera orange orbit orchard order origin otter outer oval owl oxide
pacific pack page paint palm panda paper parade parcel park pass patch path
patrol pattern peach peak pear pearl pebble pen pepper perch perfect pier
pilot pine pink pioneer pitch pivot pixel place plain plane plant plate play
plaza pledge plum plus pocket point polar pond pool poplar port post pouch
power prairie present press pretty prime prism prize proof proper proud pulse
pure purple push puzzle quarry quartz quest quick quiet quill radar radiant
radius raft rail rain raise rally ramp ranch range rapid raven reach read
ready real reason rebel record red reed reef relay remote render rescue
reserve resolve rest return ridge rift right rise river road robin rock
rocket rogue roll roof room root rope rose round route rover row royal ruby
rule run rural rush sable saddle safe sage sail saint salt sand sapphire
satin save scale scarlet scene scope score scout screen sea seal search
season seat second secret sector seed sense sequoia serve seven shade shadow
shale shape share sharp sheet shell shield shift shine ship shore short
shovel side sight sign signal silk silver simple single site six sketch sky
slate sleep slide slope small smart smith smoke snow social soft solar solid
song sonic sound source south space span spark speed sphere spice spike
spirit split spoke spring sprout spruce square stack staff stage stair stamp
stand star start state station steady steel stem step stern stick still stone
stop storm story stove straight strand stream street strong studio study
style sugar summit sun sunny sure surf surge swan sweep sweet swift swing
table tack tale talent tall tandem tangent target teal team tempo ten tender
tent terra thread three thrive thunder tide tiger timber time tin tiny title
today token tone tool tooth top torch total touch tower town track trade
trail train transit travel tread treat tree trend triangle tribe trim triple
true trunk trust truth tulip tumble tune tunnel turn twin twist union unit
unity upper urban valley value vantage vault vector velvet venture verse
vessel vine violet vision vista vital vivid voice volt vote voyage wagon
walk wall walnut wander warm wash watch water wave wax way wealth weather
weave wedge welcome well west whale wheat wheel whisper white wide wild will
willow wind window wing winter wire wise wish wolf wonder wood word work
world worth wren write yard yarn year yellow yield young zenith zero zone
""".split())

# A page that exists to sell the domain, or one nobody has put a site on. Both
# are common for short .com names — exactly the names this check has to be
# careful about — and neither is ever an employer.
_PARKED_MARKERS = (
    "domain is for sale", "domain for sale", "buy this domain",
    "this domain may be for sale", "interested in this domain",
    "the domain name", "domain name is available", "make an offer",
    "inquire about this domain", "checking your browser",
    "hugedomains", "afternic", "sedo", "dan.com", "namecheap parking",
    "godaddy", "domain parking", "parked domain", "parkingcrew",
    "this webpage was generated by the domain owner",
    "under construction", "future home of", "welcome to nginx",
    "apache2 ubuntu default page", "it works!", "default web page",
    "index of /", "site not found", "there is nothing here yet",
    "register your domain", "domain registrar", "web hosting",
)
# "coming soon" is on plenty of real product pages, so it only reads as a
# placeholder when there is nothing else on the page.
_THIN_PARKED_MARKERS = ("coming soon", "launching soon", "stay tuned")

# Hiring language strong enough to say "this organisation employs people",
# distinct from the mere word "careers" which a tourism site can carry too.
_HIRING_PHRASES = (
    "we're hiring", "we are hiring", "now hiring", "join our team",
    "join the team", "open positions", "open roles", "current openings",
    "job openings", "view openings", "see open roles", "work with us",
    "come work", "life at", "our team is growing", "careers at",
    "jobs at", "browse jobs", "view all jobs", "open jobs",
)
# A bare "Careers" / "Jobs" nav or footer link is the commonest employer marker
# on a company homepage, and the phrase list above misses it because it demands
# "careers at". Kept SEPARATE and weaker: on its own it means little (a careers
# ADVICE site says "careers" constantly), but alongside the site owning the
# company's name it is what distinguishes an employer from a coincidence.
# Verified against the live moab.com, which contains none of these.
_CAREERS_LINK = (
    ">careers<", "careers |", "| careers", "/careers", "careers page",
    ">jobs<", "jobs |", "| jobs", "/jobs", "work here", "join us",
)
# Ordinary furniture of an organisation's own website.
_COMPANY_FURNITURE = (
    "privacy policy", "terms of service", "terms of use", "contact us",
    "our team", "our mission", "our customers", "our products", "our company",
    "about us", "leadership", "newsroom", "press", "investors", "©",
    "all rights reserved",
)

# Stable refusal reasons. The dashboard renders these, so they are API.
REASON_NO_CANDIDATE = "no_candidate"
REASON_LOOKUP_DISABLED = "lookup_disabled"
REASON_LOOKUP_FAILED = "lookup_failed"
REASON_UNSAFE = "unsafe_recipient"
REASON_DEAD = "dead_address"
REASON_OWN_ADDRESS = "own_address"
REASON_DOMAIN_UNCONFIRMED = "domain_unconfirmed"
REASON_QUOTA = "guess_quota_exhausted"

# A DB failure must not read as "quota free". See guessed_sent_today.
_QUOTA_FAILSAFE = 10 ** 6

# Ownership verdicts are memoised per process: six roles at one company must
# not fetch its homepage six times. Cleared by clear_ownership_cache().
_OWNERSHIP_MEMO: dict[tuple[str, str], bool] = {}


# ---------------------------------------------------------------------------
# name / domain normalisation (pure)
# ---------------------------------------------------------------------------

def _norm_key(s: str) -> str:
    """Lowercase alphanumerics only — 'Xcel Energy' and 'xcel-energy' agree."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def company_tokens(company: str) -> list[str]:
    """Content tokens of a company name, legal furniture removed.

    'Aventis Solutions, Inc.' -> ['aventis', 'solutions']; 'The Garage Co' ->
    ['garage'].
    """
    raw = [t for t in re.split(r"[^a-z0-9]+", (company or "").lower()) if t]
    kept = [t for t in raw if t not in _LEGAL_SUFFIXES]
    return kept or raw


def is_distinctive_name(company: str) -> bool:
    """True when a plain mention of this name on a page means something.

    Distinctive: at least one token that is neither a common English word nor
    sector filler. A SINGLE token must additionally be long enough to be coined
    rather than borrowed — 'Moab' and 'Garage' are not, 'Doximity' is. This is
    the whole difference between the measured false positives and the real
    matches, so it is deliberately conservative: a wrongly non-distinctive name
    is not rejected, it is merely asked for a stronger signal.
    """
    tokens = company_tokens(company)
    if not tokens:
        return False
    meaningful = [t for t in tokens
                  if len(t) >= 4 and t not in _COMMON_WORDS and t not in _GENERIC_BIZ]
    if not meaningful:
        return False
    if len(tokens) == 1:
        return len(tokens[0]) >= _SINGLE_TOKEN_DISTINCTIVE_LEN
    return True


def domain_label(domain: str) -> str:
    """The registrable label: 'www.hex.tech' -> 'hex', 'foo.co.uk' -> 'foo'."""
    host = (domain or "").strip().lower().rstrip(".")
    host = re.sub(r"^https?://", "", host).split("/")[0].split("@")[-1]
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return parts[0] if parts else ""
    two_part_suffixes = {"co.uk", "org.uk", "ac.uk", "com.au", "co.nz", "co.jp",
                         "co.in", "com.br", "com.mx", "co.za", "com.sg"}
    if len(parts) >= 3 and ".".join(parts[-2:]) in two_part_suffixes:
        return parts[-3]
    return parts[-2]


def _name_pattern(company: str) -> str:
    """Regex matching the company name with flexible separators, or ''.

    'Xcel Energy' matches 'xcel energy', 'xcel-energy' and 'xcelenergy'.
    """
    tokens = company_tokens(company)
    if not tokens:
        return ""
    spaced = r"[\s\-_/.]{0,3}".join(re.escape(t) for t in tokens)
    joined = re.escape("".join(tokens))
    if joined == spaced:
        return rf"\b{spaced}\b"
    return rf"\b(?:{spaced}|{joined})\b"


# ---------------------------------------------------------------------------
# page reading (pure)
# ---------------------------------------------------------------------------

def _strip_tags(html_text: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html_text or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _meta_content(html_text: str, prop: str) -> str:
    """The content of <meta property="og:site_name" ...>, either attribute order."""
    esc = re.escape(prop)
    for pat in (rf'(?is)<meta[^>]+(?:property|name)\s*=\s*["\']{esc}["\'][^>]*?content\s*=\s*["\']([^"\']*)',
                rf'(?is)<meta[^>]+content\s*=\s*["\']([^"\']*)["\'][^>]*?(?:property|name)\s*=\s*["\']{esc}["\']'):
        m = re.search(pat, html_text or "")
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def _title(html_text: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html_text or "")
    return html.unescape(_strip_tags(m.group(1))).strip() if m else ""


def _identity_segment(title: str) -> str:
    """The part of a <title> that names the site.

    'Datadog | Cloud Monitoring as a Service' -> 'Datadog'. Only separators
    that genuinely divide a title are used; a bare hyphen inside a word
    ('e-commerce') is left alone.
    """
    return re.split(r"\s*(?:\||–|—|·|:|»|>|\s-\s)\s*", (title or "").strip())[0].strip()


def ownership_evidence(domain: str, company: str, page_text: str) -> dict[str, Any]:
    """Every signal `domain_confidently_belongs_to` weighs, as a dict.

    Separated out so the decision is inspectable — a rejection can be explained
    in a log line, and the tests assert on the signals rather than only on the
    verdict. Pure: no network, no state.
    """
    raw = page_text or ""
    visible = _strip_tags(raw)
    low = visible.lower()
    title = _title(raw)
    og_site = _meta_content(raw, "og:site_name")
    og_title = _meta_content(raw, "og:title")
    key = _norm_key(company)
    pattern = _name_pattern(company)

    parked = any(m in low for m in _PARKED_MARKERS) or (
        len(low) < 600 and any(m in low for m in _THIN_PARKED_MARKERS))

    mentioned = bool(pattern) and bool(re.search(pattern, low))
    identity = bool(key) and key in {_norm_key(_identity_segment(title)),
                                     _norm_key(og_site),
                                     _norm_key(_identity_segment(og_title))}

    employer_context = False
    if pattern:
        # Deliberately NOT "about NAME" or "welcome to NAME": a tourism site
        # says both about a town, which is exactly the moab.com false positive
        # this check exists to reject. Every pattern below is about EMPLOYMENT,
        # and every one of them needs a connector so "jobs in Moab" (a job
        # board for a place) cannot read as "jobs at Moab" (an employer).
        ctx = (
            rf"(?:careers?|jobs?|hiring|openings?|internships?|employment|"
            rf"recruiting)[\s,\-–—]{{0,3}}(?:at|with|@)[\s,\-–—]{{0,3}}{pattern}",
            rf"(?:life|work|working|join|team|intern)[\s,\-–—]{{0,3}}"
            rf"(?:at|with)[\s,\-–—]{{0,3}}{pattern}",
            rf"join[\s,\-–—]{{0,3}}(?:the[\s]{{0,3}})?{pattern}[\s,\-–—]{{0,3}}team",
            rf"{pattern}(?:'s|s')?[\s,\-–—]{{0,3}}"
            rf"(?:careers?|jobs?|is hiring|are hiring|open roles?|"
            rf"open positions?|talent team|recruiting team|is looking for)",
        )
        employer_context = any(re.search(p, low) for p in ctx)

    return {
        "domain": (domain or "").lower(),
        "company": company or "",
        "distinctive": is_distinctive_name(company),
        "parked": parked,
        "thin": len(low) < _MIN_PAGE_CHARS,
        "mentioned": mentioned,
        "identity_match": identity,
        "employer_context": employer_context,
        "domain_is_name": bool(key) and domain_label(domain) == key,
        "hiring_signal": any(p in low for p in _HIRING_PHRASES),
        "careers_link": any(p in low for p in _CAREERS_LINK),
        "company_furniture": sum(1 for p in _COMPANY_FURNITURE if p in low) >= 2,
        "title": title,
        "og_site_name": og_site,
    }


def _verdict(ev: dict[str, Any]) -> tuple[bool, str]:
    """(owned, why) from gathered evidence. Pure — the rule itself."""
    if ev["parked"]:
        return False, "parked_or_placeholder"
    if ev["thin"]:
        return False, "page_too_thin"
    if not ev["mentioned"]:
        return False, "name_absent_from_page"
    if ev["distinctive"]:
        return True, "distinctive_name_mentioned"
    # Short or common-word name: a mention is worth nothing on its own.
    #
    # Nor is owning the page identity, on its own. Measured against the LIVE
    # site: moab.com's <title> is exactly "MOAB", so identity_match fires — but
    # it is a Utah tourism site, not the employer "Moab". Any site named after a
    # common word has that word as its title, so for a non-distinctive name the
    # identity signal has to be corroborated by something that says EMPLOYER:
    # hiring language, or the ordinary furniture of a company site. moab.com has
    # neither (hiring_signal False), so it is now correctly refused.
    # company_furniture is NOT enough corroboration: "Contact us / Privacy
    # policy" is on essentially every website, and moab.com has it. Only a
    # hiring or employer signal separates "a site named MOAB" from "the employer
    # Moab", and that is precisely the distinction that decides whether a CV
    # goes to a stranger.
    if ev["identity_match"] and (ev["hiring_signal"] or ev["employer_context"]
                                 or ev["careers_link"]):
        return True, "site_identity_matches_name"
    if ev["employer_context"]:
        return True, "named_as_employer"
    if ev["domain_is_name"] and ev["hiring_signal"] and ev["company_furniture"]:
        return True, "domain_equals_name_with_hiring_signals"
    return False, "generic_name_without_ownership_signal"


def domain_confidently_belongs_to(domain: str, company: str, *,
                                  page_text: str | None = None) -> bool:
    """Does this domain really belong to that company? Strictly.

    Replaces utils.company_email.domain_belongs_to, which asks only whether the
    name appears on the homepage. That is trivially true by coincidence for a
    short generic name — measured: moab.com/"Moab" and garage.com/"Garage" both
    passed it, and both are strangers.

    The rule:
      * a parked, for-sale, registrar or placeholder page NEVER qualifies (the
        common fate of short .com names, and the easiest way to mail a squatter);
      * a DISTINCTIVE name ('Aventis Solutions', 'Xcel Energy', 'Doximity')
        qualifies on a plain mention — the coincidence rate is negligible;
      * a SHORT or COMMON-WORD name ('Moab', 'Garage', 'Cape', 'Air',
        'Campfire') must do one of: own the page identity (<title> or
        og:site_name IS the name) AND read as an employer site, be named as an
        employer ('Careers at Ramp',
        'Join Hex'), or have the domain equal the name AND the page carry both
        hiring language and ordinary company furniture.

    `page_text` is the homepage HTML. Pass it to keep this pure — tests always
    do. When it is None the homepage is fetched once and the verdict memoised
    per (domain, company); any network failure is a refusal, never a pass.
    """
    domain = (domain or "").strip().lower()
    if not domain or not (company or "").strip():
        return False

    if page_text is None:
        cache_key = (domain, _norm_key(company))
        if cache_key in _OWNERSHIP_MEMO:
            return _OWNERSHIP_MEMO[cache_key]
        page_text = _fetch_homepage(domain)
        if page_text is None:
            # Unreachable is not innocent-until-proven: refuse, and do not
            # memoise, so a transient outage is retried next cycle.
            logger.info(f"[recipient] {domain} unreachable — ownership unproven")
            return False
        owned, why = _verdict(ownership_evidence(domain, company, page_text))
        _OWNERSHIP_MEMO[cache_key] = owned
        logger.info(f"[recipient] ownership {domain} / {company!r}: "
                    f"{'yes' if owned else 'NO'} ({why})")
        return owned

    owned, why = _verdict(ownership_evidence(domain, company, page_text))
    logger.debug(f"[recipient] ownership {domain} / {company!r}: {owned} ({why})")
    return owned


def ownership_reason(domain: str, company: str, page_text: str) -> str:
    """The `_verdict` explanation string, for logs and the dashboard."""
    return _verdict(ownership_evidence(domain, company, page_text))[1]


def clear_ownership_cache() -> None:
    """Drop memoised ownership verdicts (tests, and a manual re-check)."""
    _OWNERSHIP_MEMO.clear()


def _fetch_homepage(domain: str) -> str | None:
    """The homepage HTML, or None on any failure. The only I/O in this file."""
    try:
        import requests
        sess = requests.Session()
        sess.headers.update({"User-Agent": _UA})
        r = sess.get(f"https://{domain}", timeout=_TIMEOUT, allow_redirects=True)
        if r.status_code != 200:
            return None
        return (r.text or "")[:200000]
    except Exception as e:                       # network, TLS, DNS, import
        logger.debug(f"[recipient] homepage fetch failed for {domain}: {e}")
        return None


# ---------------------------------------------------------------------------
# the shared quota, counted from the database
# ---------------------------------------------------------------------------

def guessed_sent_today(session=None) -> int:
    """How many CONSTRUCTED addresses were mailed since UTC midnight.

    agents/auto_applier/runner._guess_quota_left counts in a module-level dict
    (`_GUESS_SENT`). That counter is PER PROCESS and caps nothing globally: the
    scheduler and the dashboard are separate processes, each gets its own fresh
    zero, and a restart resets it — so the "20 guessed sends a day" cap has
    never actually been 20 a day. Counting the OutreachSend ledger instead makes
    the cap real, shared between both agents, and durable across restarts —
    the same reasoning as the outreach daily cap (see db.models.OutreachSend).

    Counts only rows that were actually SENT; a claimed/failed/blocked row never
    reached a mailbox and must not consume the quota.

    Fails CLOSED: if the ledger cannot be read this returns a number no cap can
    exceed, so a database problem blocks constructed sends rather than
    unblocking them. Published addresses are unaffected — they are never capped.
    """
    own_session = session is None
    try:
        from db.models import OutreachSend
        if own_session:
            from db.database import get_session
            session = get_session()
        now = datetime.now(timezone.utc)
        midnight = datetime(now.year, now.month, now.day)   # naive UTC, as stored
        from sqlalchemy import func
        return int(session.query(func.count(OutreachSend.id))
                   .filter(OutreachSend.status == "sent",
                           OutreachSend.recipient_source.in_(("constructed",)),
                           OutreachSend.sent_at >= midnight)
                   .scalar() or 0)
    except Exception as e:
        logger.error(f"[recipient] guessed-send count failed, failing closed: {e}")
        return _QUOTA_FAILSAFE
    finally:
        if own_session and session is not None:
            try:
                session.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# the resolver
# ---------------------------------------------------------------------------

def _blank(reason: str) -> dict[str, Any]:
    """The stable shape. Every key is always present, whatever happened."""
    return {"address": None, "source": None, "domain": None, "source_url": None,
            "domain_origin": None, "confidence": 0.0, "rejected": [],
            "reason": reason}


def _addr_domain(address: str) -> str | None:
    return address.split("@", 1)[1].lower() if "@" in (address or "") else None


def _as_dead_set(dead: Iterable[str] | None) -> set[str]:
    return {str(a).strip().lower() for a in (dead or ()) if str(a).strip()}


def resolve(job, *, mail_cfg: dict, dead: set[str] | None = None,
            allow_guess: bool | None = None,
            sent_today_guessed: int = 0) -> dict:
    """The one recipient decision both agents make.

    Order, most trustworthy first — the same order the auto-applier runner uses:

      1. an address the employer PUBLISHED IN THE POSTING
         (utils.mailer.find_employer_recipient) -> source "posting"
      2. utils.company_email.find_company_email -> "company_site" (an address
         published on the company's own site) or "constructed" (careers@domain,
         built by us)

    Every candidate is then screened, and EVERY refusal is recorded in
    `rejected` so the dashboard can say why a job is unmailable instead of
    showing an empty box:

      * utils.mailer.is_safe_recipient — accommodation, legal, compliance,
        privacy, no-reply and press inboxes NEVER receive a CV. Applied to
        every source including a published one: an employer printing
        `accommodations@` in its posting is not inviting an application there.
      * the `dead` set — an address a previous send bounced from.
      * CONSTRUCTED addresses only: `domain_confidently_belongs_to`, and then
        the daily guessed-address cap (mail.max_guessed_per_day vs
        `sent_today_guessed`). A published or crawled address is NEVER
        quota-limited — the cap exists because a constructed address can bounce.

    Args:
        job: anything with .description, .company and .url (a db.models.Job).
        mail_cfg: the `mail` block of config/settings.yaml. Reads
            lookup_website, guess_addresses, guess_locals, max_guessed_per_day
            and (optionally) address — our own sending address, which is never
            a valid employer recipient.
        dead: bounced addresses, from utils.bounce_watch.load_dead().
        allow_guess: overrides mail_cfg["guess_addresses"] when not None.
        sent_today_guessed: constructed sends already made today — pass
            guessed_sent_today(). Nothing here increments it; resolve decides,
            the caller counts.

    Returns the stable shape documented in the module tests: address, source,
    domain, source_url, domain_origin, confidence, rejected, reason. `reason`
    explains an address of None, and is the refusal of the HIGHEST-TRUST
    candidate that was turned down, because that is the one worth explaining.
    """
    mail_cfg = mail_cfg or {}
    dead_set = _as_dead_set(dead)
    own = str(mail_cfg.get("address") or mail_cfg.get("from_address") or "").strip().lower()
    rejected: list[dict[str, str]] = []

    def refuse(address: str, reason: str) -> None:
        logger.info(f"[recipient] refusing {address!r}: {reason}")
        rejected.append({"address": address, "reason": reason})

    def finish(reason: str) -> dict[str, Any]:
        out = _blank(rejected[0]["reason"] if rejected else reason)
        out["rejected"] = rejected
        return out

    def screen_common(address: str) -> str | None:
        """The screens every source faces, whatever its provenance."""
        if own and address == own:
            return REASON_OWN_ADDRESS
        if not _is_safe(address):
            return REASON_UNSAFE
        if address in dead_set:
            return REASON_DEAD
        return None

    # ---- 1. the posting itself -------------------------------------------
    description = getattr(job, "description", "") or ""
    posting = _find_in_posting(description)
    if posting:
        posting = posting.strip().lower()
        bad = screen_common(posting)
        if bad:
            refuse(posting, bad)
        else:
            return {"address": posting, "source": "posting",
                    "domain": _addr_domain(posting), "source_url": None,
                    "domain_origin": "posting",
                    "confidence": CONFIDENCE["posting"],
                    "rejected": rejected, "reason": ""}
    else:
        # find_employer_recipient screens unsafe locals INSIDE itself, so a
        # posting whose only address is accommodations@ or legal@ comes back
        # as a bare None and the dashboard could only say "no address found" —
        # which is wrong and unactionable. Re-screen the raw addresses here so
        # the refusal is recorded with its reason. This changes no decision:
        # such an address was never going to be used.
        for addr in _addresses_in(description):
            bad = screen_common(addr)
            if bad:
                refuse(addr, bad)

    # ---- 2. the company itself -------------------------------------------
    allow_crawl = bool(mail_cfg.get("lookup_website", True))
    if allow_guess is None:
        allow_guess = bool(mail_cfg.get("guess_addresses", False))
    allow_guess = bool(allow_guess)
    if not (allow_crawl or allow_guess):
        return finish(REASON_LOOKUP_DISABLED)

    try:
        from utils.company_email import find_company_email
        rec = find_company_email(
            job,
            allow_crawl=allow_crawl,
            allow_guess=allow_guess,
            guess_locals=mail_cfg.get("guess_locals"),
            dead=dead_set,
        ) or {}
    except Exception as e:
        logger.warning(f"[recipient] company-email lookup failed for "
                       f"{getattr(job, 'company', '')!r}: {e}")
        return finish(REASON_LOOKUP_FAILED)

    address = str(rec.get("address") or "").strip().lower()
    if not address:
        return finish(REASON_NO_CANDIDATE)

    source = "company_site" if rec.get("source") in ("crawl", "website") else "constructed"
    domain = (rec.get("domain") or _addr_domain(address) or "").lower() or None
    origin = rec.get("domain_origin")

    bad = screen_common(address)
    if bad:
        refuse(address, bad)
        return finish(bad)

    if source == "constructed":
        # A domain taken from the employer's OWN posting URL is authoritative
        # and needs no proof. Anything we built from a slug or a bare company
        # name must prove itself — this is the check that used to let
        # moab.com/"Moab" through, and the reason the Mail Agent refused
        # constructed addresses wholesale instead.
        if origin != "url" and not domain_confidently_belongs_to(
                domain or "", getattr(job, "company", "") or ""):
            refuse(address, REASON_DOMAIN_UNCONFIRMED)
            return finish(REASON_DOMAIN_UNCONFIRMED)

        cap = _int(mail_cfg.get("max_guessed_per_day", 20))
        if cap <= 0 or int(sent_today_guessed or 0) >= cap:
            refuse(address, REASON_QUOTA)
            return finish(REASON_QUOTA)

    return {"address": address, "source": source, "domain": domain,
            "source_url": rec.get("source_url"), "domain_origin": origin,
            "confidence": CONFIDENCE[source], "rejected": rejected, "reason": ""}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _is_safe(address: str) -> bool:
    from utils.mailer import is_safe_recipient
    return bool(is_safe_recipient(address))


def _find_in_posting(description: str) -> str | None:
    from utils.mailer import find_employer_recipient
    return find_employer_recipient(description)


# Same pattern utils.mailer uses to read addresses out of a posting.
_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _addresses_in(text: str) -> list[str]:
    """Every address printed in the text, lowercased, in order, deduplicated."""
    out: list[str] = []
    for raw in _ADDRESS_RE.findall(text or ""):
        addr = raw.strip().lower().rstrip(".,;:)\"'")
        if addr and addr not in out:
            out.append(addr)
    return out
