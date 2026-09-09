"""Deterministic metadata extraction for postings the scrapers left blank.

WHY THIS EXISTS
---------------
Measured 2026-09-09 against jobpilot.db: all 106 `linkedin_public` rows carry
employment_type='unknown', seniority_level=NULL, is_remote=0, pay_period=
'unknown' and no salary of any kind. The Outreach screen's filters therefore
have nothing to filter on. The LinkedIn guest scraper fills title, company,
location, url and source_id and nothing else, so the enrichment has to come out
of the text that IS there.

The same audit found the harder half of the problem: **all 106 of those rows
also have description = '' (zero characters)**. The guest detail endpoint that
would supply a body is throttled, and `_fetch_detail` in
agents/scanner/linkedin_sources.py deliberately leaves the RawJob intact when it
fails. So for this corpus the title and the location string are the *only*
evidence, and any field that can only be read out of a description resolves
0/106 today. That is a scraper finding, not an extractor failure — this module
is written so it starts paying off the moment descriptions land, and reports
honestly (returns None) until then.

That claim is measured, not hoped for. Over the original 106 empty-bodied rows
this module resolves is_remote 106/106 (all False), seniority 5/106, work_mode
2/106 and comp 0/106. Over the 27 linkedin_public rows that had acquired a real
description a few hours later, the same code resolves salary 19/27, seniority
13/27 and work_mode 14/27 — and over the 223 rows from other sources that carry
descriptions, salary 42/223, seniority 81/223 and work_mode 105/223. The
extractor is not the bottleneck; the empty description is.

WHY NOT REUSE agents.scanner.providers._is_remote
-------------------------------------------------
That helper is `"remote" in blob or "anywhere" in blob`. Over a title+location
pair it is adequate; over a description it inverts the answer on the two most
common phrasings in real postings — "this role is **not** remote" and a boilerplate
"we have a **remote-first culture**" paragraph on an on-site req both make it
return True. Anything reading a description needs the graded detector below.
utils.comp.parse_hourly and parse_employment_type ARE reused: the former whole,
the latter title-only (its description path is a bare `"contract" in text`
substring test, which fires on "contract negotiation" and "government
contracts").

DESIGN CONSTRAINT
-----------------
A wrong filter value is worse than a missing one: a user who filters
work_mode=remote and gets an on-site job stops trusting the filter, whereas a
missing value just leaves the row where it was. Every detector here therefore
returns None on ambiguity, and enrich() emits only the keys it could actually
decide. Two consequences that look like under-reach but are deliberate:

* A plain city location with no other evidence proves the job is **not remote**
  (is_remote=False) but cannot separate on-site from hybrid, so work_mode stays
  None rather than guessing 'onsite'.
* Seniority is read from the TITLE first and only falls back to tightly scoped
  description phrases. A description saying "you will work with senior
  stakeholders" must not make the row senior.

No LLM, no network, no I/O — same input, same output, always.
"""

from __future__ import annotations

import logging
import re

from utils.comp import parse_hourly, parse_employment_type

logger = logging.getLogger(__name__)

# Values the UI filter can offer. Exported so the dropdown and the extractor
# cannot drift apart.
WORK_MODES: tuple[str, ...] = ("remote", "hybrid", "onsite")

# Canonical seniority ladder, lowest to highest. 'manager' is deliberately
# absent: "Product Manager" and "Program Manager" are job functions, not rungs,
# and there is no way to tell them from "Engineering Manager" by title alone.
SENIORITY_LEVELS: tuple[str, ...] = (
    "intern", "entry", "junior", "mid", "senior", "staff",
    "principal", "lead", "director", "executive",
)

EMPLOYMENT_TYPES: tuple[str, ...] = (
    "internship", "part_time", "full_time", "contract", "per_diem",
)

# Placeholders a scanner writes when it could not determine the field. Treated
# as "not yet known" so enrichment is allowed to fill them.
_UNSET_STRINGS = frozenset({"", "unknown", "none", "null", "n/a", "na", "-"})

# Annual comp sanity window. Below 20k is an hourly rate, a stipend or a
# per-diem; above 1M is funding, revenue or contract value, not a salary.
_ANNUAL_MIN = 20_000.0
_ANNUAL_MAX = 1_000_000.0
# Hourly sanity window; below 5 is a typo, above 500 is a project fee.
_HOURLY_MIN = 5.0
_HOURLY_MAX = 500.0


def _norm(s: str | None) -> str:
    """Lowercase and flatten the punctuation that varies between renderings."""
    t = (s or "").lower()
    t = t.replace("–", "-").replace("—", "-").replace("−", "-")
    t = t.replace("’", "'").replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()


def _is_set(value: object) -> bool:
    """True when a scanner already put a real value in this field."""
    if value is None:
        return False
    if isinstance(value, str):
        return _norm(value) not in _UNSET_STRINGS
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return True


# ---------------------------------------------------------------------------
# work mode / remote
# ---------------------------------------------------------------------------

# The role is explicitly NOT remote. Checked before everything else, because a
# posting that bothers to say this usually says "remote" three more times.
_NOT_REMOTE_RE = re.compile(
    r"\bno\s+remote\b"
    r"|\bnot\s+(?:a\s+)?remote\b"
    r"|\bnon-remote\b"
    r"|\bremote\s+(?:work\s+)?(?:is\s+)?not\s+(?:available|an\s+option|offered|permitted)\b"
    r"|\bthis\s+(?:role|position|job)\s+is\s+not\s+remote\b"
)

# A day count is capped at 4: "5 days a week in the office" is on-site, not
# hybrid. The reversed word order ("work onsite 4 days per week", jobpilot.db
# job 804) is as common as the forward one.
_HYBRID_RE = re.compile(
    r"\bhybrid\b"
    r"|\b[1-4]\s*(?:days?|x)\s*(?:per|a|/)\s*week\s+(?:in|on[\s-]?site|at\s+the\s+office)\b"
    r"|\b[1-4]\s+days?\s+(?:a\s+week\s+)?in\s+(?:the\s+)?office\b"
    r"|\b(?:on[\s-]?site|in\s+(?:the\s+)?office|in[\s-]?person)\s+[1-4]\s*days?"
    r"\s*(?:per|a|/)\s*week\b"
)

# "onsite"/"in-person" also describe INTERVIEWS, which say nothing about where
# the work happens; both are excluded by lookahead.
_ONSITE_RE = re.compile(
    r"\bon[\s-]?site\b(?!\s+interview)"
    r"|\bin[\s-]?office\b"
    r"|\bin[\s-]?person\b(?!\s+interview)"
    r"|\bfully\s+in[\s-]?person\b"
    r"|\b(?:5|five)\s+days?\s+(?:a\s+week\s+)?in\s+(?:the\s+)?office\b"
    r"|\bin\s+(?:the\s+)?office\s+(?:5|6|7|five)\s*days?\b"
)

# Remote assertions that are scoped to THE ROLE. Every pattern names the job or
# the work; none of them can be satisfied by a culture blurb. That is what keeps
# "remote-first culture", "remote-friendly", "our remote team" and "distributed
# team" — none of which promise this req is remote — out of the result.
_REMOTE_RE = re.compile(
    r"\b(?:100%|fully|entirely|completely)\s+remote\b"
    r"|\b(?:this\s+)?(?:role|position|job|opportunity)\s+is\s+(?:fully\s+)?remote\b"
    r"|\bremote\s+(?:role|position|job|opportunity)\b"
    r"|\bwork\s+from\s+(?:home|anywhere)\b"
    r"|\bwork\s+remotely\b"
    r"|\bremotely\s+from\s+anywhere\b"
    r"|\btelecommut\w*\b"
    r"|\bwfh\b"
    r"|\bremote\s+eligible\b"
    r"|\bopen\s+to\s+remote\s+candidates\b"
)

# Bare markers, for the SHORT fields only. LinkedIn writes these into the title
# and the location itself: "AI Engineer - On Site", "Remote", "Remote (US)",
# "Austin, TX (Remote)", "United States - Remote". A lone "remote" is trusted
# here and nowhere else — a title or location is a handful of words an author
# chose, so the word cannot be incidental the way it is in prose.
_SHORT_REMOTE_RE = re.compile(r"\bremote\b|\banywhere\b|\bworldwide\b|\bwork\s+from\s+home\b")
_SHORT_HYBRID_RE = re.compile(r"\bhybrid\b")
_SHORT_ONSITE_RE = re.compile(r"\bon[\s-]?site\b|\bin[\s-]?office\b|\bin[\s-]?person\b")


def _mode_from_short_field(text: str) -> str | None:
    """Work mode asserted by a title or location, or None."""
    if not text:
        return None
    if _NOT_REMOTE_RE.search(text):
        return "onsite"
    # Hybrid outranks remote: a hybrid posting normally advertises the remote
    # days ("Remote/Hybrid"), so checking remote first would mislabel it.
    if _SHORT_HYBRID_RE.search(text):
        return "hybrid"
    if _SHORT_ONSITE_RE.search(text):
        return "onsite"
    if _SHORT_REMOTE_RE.search(text):
        return "remote"
    return None


# "On-site" in prose is very often about the CUSTOMER's site, not the
# employer's. Audited against jobpilot.db 2026-09-09: of the first five
# descriptions this module labelled onsite, three were travel clauses ("travel
# up to 20-40% to work on-site with customers", "spend at least 25% of your time
# onsite") and one was a negation ("there is no minimum in-office qualification
# requirement"). All four are exactly the wrong filter value this module exists
# to avoid, so an onsite match inside a window naming any of these is dropped.
_ONSITE_DISQUALIFY_RE = re.compile(
    r"\bcustomers?\b|\bclients?\b|\bpartners?\b|\btravel\w*\b|\bvisit\w*\b"
    r"|%\s*of\s+your\s+time|\bno\s+minimum\b|\bnot\s+required\b|\bthere\s+is\s+no\b"
    r"|\bno\s+in[\s-]?office\b|\boptional\b"
    # An onsite marker in a sentence that also offers flexibility is listing the
    # options an employer supports, not stating this req's mode: jobpilot.db job
    # 510 reads "work personas (flexible, remote, or required in office) are
    # categories assigned to employees". Dropping a genuinely on-site role that
    # happens to advertise flexible hours is the safe direction to be wrong in.
    r"|\bflexible\b"
)

# Benefits vocabulary binds TIGHTLY to the marker ("in-office perks: lunch,
# snacks", "commuter benefits (in-office & US only)" — jobpilot.db 438-442, 453),
# so it gets a narrow window. A wide one would also swallow the legitimate
# "Remote-friendly perks. This position is on-site 5 days a week."
_ONSITE_BENEFITS_RE = re.compile(
    r"\bperks?\b|\bsnacks?\b|\blunch\b|\bmeals?\b|\bbenefits?\b|\binsurance\b"
    r"|\bgym\b|\bcommuter\b|\bcatered\b|\bstipend\b|\breimbursement\b"
    r"|\bamenit\w*\b|\ballowance\b|\bset[\s-]?up\b|\bwellness\b"
)


# Sentence / bullet boundaries. Benefits lists are bullets, and real postings
# bullet them with emoji as often as with punctuation (jobpilot.db job 453), so
# any non-ASCII character ends a segment too.
_SEGMENT_BREAK_RE = re.compile(r"[.;!?\n•|]|[^\x00-\x7f]")


def _segment_around(text: str, start: int, end: int, *, span: int = 120) -> str:
    """The bullet or sentence containing text[start:end]."""
    left, right = max(0, start - span), min(len(text), end + span)
    before = text[left:start]
    breaks = [m.end() for m in _SEGMENT_BREAK_RE.finditer(before)]
    if breaks:
        left += breaks[-1]
    after = _SEGMENT_BREAK_RE.search(text, end, right)
    if after:
        right = after.start()
    return text[left:right]


def _onsite_in_prose(text: str) -> bool:
    """True only for an onsite marker that is about where THIS job is done."""
    for match in _ONSITE_RE.finditer(text):
        wide = text[max(0, match.start() - 90):match.end() + 60]
        if _ONSITE_DISQUALIFY_RE.search(wide):
            continue
        if _ONSITE_BENEFITS_RE.search(_segment_around(text, match.start(), match.end())):
            continue
        return True
    return False


def _mode_from_prose(text: str) -> str | None:
    """Work mode asserted by a description body, or None if it asserts nothing."""
    if not text:
        return None
    if _NOT_REMOTE_RE.search(text):
        return "onsite"
    if _HYBRID_RE.search(text):
        return "hybrid"
    if _REMOTE_RE.search(text):
        return "remote"
    if _onsite_in_prose(text):
        return "onsite"
    return None


def _location_names_a_place(location: str) -> bool:
    """True when the location string identifies somewhere physical.

    "Miami, FL" and "San Francisco Bay Area" do; "", "Remote" and "Anywhere"
    do not. Used as the evidence that a job is not remote when nothing else
    speaks.
    """
    loc = _norm(location)
    if len(loc) < 3 or not re.search(r"[a-z]", loc):
        return False
    stripped = re.sub(r"\bremote\b|\banywhere\b|\bworldwide\b|\bhybrid\b|\bon[\s-]?site\b",
                      " ", loc)
    stripped = re.sub(r"[^a-z]+", "", stripped)
    return len(stripped) >= 3


def detect_work_mode(title: str | None, location: str | None,
                     description: str | None = "") -> str | None:
    """'remote' | 'hybrid' | 'onsite', or None when the posting does not say.

    Evidence order is title, then location, then description — the first two are
    short fields an author chose deliberately ("AI Engineer - On Site"), while a
    description is long prose where an incidental mention is likely.

    The description is still consulted for the common trap where the location
    names a head office but the body says the role is fully remote; a plain city
    with no mode marker asserts nothing here, so the description decides.

    A bare city with no description evidence returns None on purpose: it rules
    remote out (see detect_remote) but cannot separate on-site from hybrid.
    """
    t, loc, desc = _norm(title), _norm(location), _norm(description)

    for short in (t, loc):
        mode = _mode_from_short_field(short)
        if mode:
            return mode

    mode = _mode_from_prose(desc)
    if mode and mode == "remote" and _location_names_a_place(loc):
        logger.debug("[job_enrich] description overrides city location %r -> remote", loc)
    return mode


def detect_remote(title: str | None, location: str | None,
                  description: str | None = "") -> bool | None:
    """Value for the existing boolean is_remote column, or None if undecidable.

    Hybrid maps to False: the column exists so a user can filter for jobs with
    no office requirement, and a hybrid role has one.
    """
    mode = detect_work_mode(title, location, description)
    if mode is not None:
        return mode == "remote"
    if _location_names_a_place(location or ""):
        return False
    return None


# ---------------------------------------------------------------------------
# employment type
# ---------------------------------------------------------------------------

_TITLE_INTERN_RE = re.compile(r"\bintern(?:ship)?\b")  # \b keeps "Internal" out

# Description phrasings that are scoped to the posting rather than to the
# company's legal department. utils.comp.parse_employment_type is NOT used on
# descriptions because its bare `"contract" in text` test also matches
# "contract negotiation" and "manages vendor contracts".
_DESC_EMPLOYMENT_RES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bper[\s-]?diem\b"), "per_diem"),
    (re.compile(r"\bthis\s+is\s+an?\s+(?:paid\s+)?internship\b"
                r"|\binternship\s+(?:program|position|role|opportunity)\b"), "internship"),
    (re.compile(r"\bpart[\s-]?time\s+(?:position|role|opportunity|employment|hours)\b"
                r"|\bthis\s+is\s+an?\s+part[\s-]?time\b"), "part_time"),
    (re.compile(r"\bcontract[\s-]?to[\s-]?hire\b"
                r"|\b\d{1,2}[\s-]?month\s+contract\b"
                r"|\bcontract\s+(?:position|role|assignment|engagement)\b"
                r"|\bthis\s+is\s+an?\s+contract\b"
                r"|\b(?:w2|c2c|1099)\s+contract\b"), "contract"),
    (re.compile(r"\btemporary\s+(?:position|role|assignment)\b"), "contract"),
    (re.compile(r"\bfull[\s-]?time\s+(?:position|role|opportunity|employment)\b"
                r"|\bthis\s+is\s+an?\s+full[\s-]?time\b"), "full_time"),
)

# "Employment type: Full-time" — LinkedIn's own criteria block, and the ATS
# boards that copy its layout.
_EMPLOYMENT_KV_RE = re.compile(r"\bemployment\s+type\s*[:\-]\s*([a-z][a-z\s/-]{1,20})")

_EMPLOYMENT_LABELS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"per[\s-]?diem"), "per_diem"),
    (re.compile(r"intern"), "internship"),
    (re.compile(r"part[\s-]?time"), "part_time"),
    (re.compile(r"contract|temporary|temp\b"), "contract"),
    (re.compile(r"full[\s-]?time|permanent"), "full_time"),
)


def normalize_employment_type(label: str | None) -> str | None:
    """Map an external employment-type label onto EMPLOYMENT_TYPES."""
    text = _norm(label)
    if text in _UNSET_STRINGS:
        return None
    if text in EMPLOYMENT_TYPES:
        return text
    for pattern, value in _EMPLOYMENT_LABELS:
        if pattern.search(text):
            return value
    return None


def detect_employment_type(title: str | None, description: str | None = "") -> str | None:
    """part_time | full_time | contract | per_diem | internship, or None.

    None rather than the 'unknown' sentinel utils.comp returns, so a caller can
    tell "the extractor declined" from "the extractor decided nothing applies".
    """
    t = _norm(title)
    if _TITLE_INTERN_RE.search(t):
        return "internship"

    # Titles are curated, so comp's substring matching is safe there.
    from_title = parse_employment_type(t, "")
    if from_title != "unknown":
        return from_title

    desc = _norm(description)
    if not desc:
        return None

    m = _EMPLOYMENT_KV_RE.search(desc)
    if m:
        value = normalize_employment_type(m.group(1))
        if value:
            return value

    for pattern, value in _DESC_EMPLOYMENT_RES:
        if pattern.search(desc):
            return value
    return None


# ---------------------------------------------------------------------------
# seniority
# ---------------------------------------------------------------------------

# Highest rung first: "Senior Staff Engineer" is staff, not senior.
_TITLE_SENIORITY_RES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:vp|svp|evp|cto|cio|ceo|cpo)\b|\bvice\s+president\b"
                r"|\bchief\s+\w+\s+officer\b|\bhead\s+of\b"), "executive"),
    (re.compile(r"\bdirector\b"), "director"),
    (re.compile(r"\bprincipal\b"), "principal"),
    (re.compile(r"\bstaff\b"), "staff"),
    # "Lead Generation Specialist" is sales, not a lead engineer.
    (re.compile(r"\blead\b(?!\s+gen)"), "lead"),
    (re.compile(r"\b(?:senior|sr\.?|snr)\b"), "senior"),
    (re.compile(r"\bmid[\s-]?level\b"), "mid"),
    (re.compile(r"\b(?:junior|jr\.?)\b"), "junior"),
    (re.compile(r"\bentry[\s-]?level\b|\bnew\s+grad(?:uate)?\b"), "entry"),
    (re.compile(r"\bintern(?:ship)?\b"), "intern"),
)

# Trailing roman numeral: "AI Engineer II", "Machine Learning Engineer III",
# "AI/ML Engineer II" — 22 of the corpus's title forms use this ladder.
# A bare trailing "I" is skipped: it is far more often a truncation artifact
# than a level. III maps to mid, not senior, because the III rung is
# company-defined and mid is the rung that cannot be wrong by two steps.
# A numeric "Level 4" is ignored for the same reason without the safe floor —
# 4 means principal at one employer and new-grad at the next.
_TITLE_NUMERAL_RE = re.compile(r"\b(ii|iii|iv|v)\b\s*$|\b(ii|iii|iv|v)\b\s*[,(\-]")
_NUMERAL_LEVELS = {"ii": "mid", "iii": "mid", "iv": "senior", "v": "senior"}

# Description fallbacks, each scoped so an incidental mention cannot fire.
# "you will work with senior stakeholders" and "partner with senior engineers"
# match none of them: every pattern requires either an explicit "-level" suffix,
# a key/value criteria line, or a hiring verb plus an article.
_DESC_SENIORITY_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(principal|staff|senior|mid|junior|entry)[\s-]level\b"),
    re.compile(r"\b(?:as|hiring|seeking|looking\s+for|for)\s+an?\s+"
               r"(principal|staff|senior|junior|lead)\b"),
)

_DESC_SENIORITY_KV_RE = re.compile(r"\bseniority(?:\s+level)?\s*[:\-]\s*([a-z][a-z\s-]{1,24})")

# Years-of-experience requirement, the last resort. Only counted when the
# sentence is stating a REQUIREMENT — "our founders have 15+ years of
# experience" must not make the req executive.
_YEARS_RE = re.compile(
    r"\b(?:minimum\s+(?:of\s+)?|at\s+least\s+|require[sd]?\s+|must\s+have\s+)"
    r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?years?\s+(?:of\s+)?"
    r"(?:[a-z]+\s+){0,3}experience\b"
)

_EXTERNAL_SENIORITY_LABELS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"intern"), "intern"),
    (re.compile(r"entry|new\s+grad"), "entry"),
    (re.compile(r"junior"), "junior"),
    # LinkedIn's "Mid-Senior level" is its default bucket for everything above
    # entry, so it maps to the LOWER rung. A genuinely senior req says Senior in
    # the title, and the title outranks this.
    (re.compile(r"mid[\s-]senior|associate|mid"), "mid"),
    (re.compile(r"senior"), "senior"),
    (re.compile(r"staff"), "staff"),
    (re.compile(r"principal"), "principal"),
    (re.compile(r"\blead\b"), "lead"),
    (re.compile(r"director"), "director"),
    (re.compile(r"executive|vice\s+president|\bvp\b|chief"), "executive"),
)


def normalize_seniority(label: str | None) -> str | None:
    """Map an external seniority label onto SENIORITY_LEVELS.

    Handles the strings already stored by other code paths — LinkedIn's
    "Mid-Senior level" (agents/scanner/linkedin_sources.py) and JobRight's
    seniority column — so an existing value can be compared without a
    vocabulary clash.
    """
    text = _norm(label)
    if text in _UNSET_STRINGS:
        return None
    if text in SENIORITY_LEVELS:
        return text
    for pattern, value in _EXTERNAL_SENIORITY_LABELS:
        if pattern.search(text):
            return value
    return None


def _years_to_level(years: int) -> str | None:
    if years <= 1:
        return "entry"
    if years <= 4:
        return "mid"
    if years <= 15:
        return "senior"
    return None  # >15 years in a job ad is almost always company boilerplate


def detect_seniority(title: str | None, description: str | None = "") -> str | None:
    """A SENIORITY_LEVELS value, or None.

    The title is authoritative and is fully exhausted (words, then the roman
    numeral ladder) before the description is looked at.
    """
    t = _norm(title)
    for pattern, level in _TITLE_SENIORITY_RES:
        if pattern.search(t):
            return level

    m = _TITLE_NUMERAL_RE.search(t)
    if m:
        return _NUMERAL_LEVELS[(m.group(1) or m.group(2))]

    desc = _norm(description)
    if not desc:
        return None

    kv = _DESC_SENIORITY_KV_RE.search(desc)
    if kv:
        level = normalize_seniority(kv.group(1))
        if level:
            return level

    for pattern in _DESC_SENIORITY_RES:
        m = pattern.search(desc)
        if m:
            level = normalize_seniority(m.group(1))
            if level:
                return level

    m = _YEARS_RE.search(desc)
    if m:
        return _years_to_level(int(m.group(1)))
    return None


# ---------------------------------------------------------------------------
# compensation
# ---------------------------------------------------------------------------

# One money amount: "$120,000", "USD 120000", "120k", "$150K".
_AMOUNT = r"(?:\$\s*|usd\s*|us\$\s*)?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{2,3}(?:\.\d+)?\s*k|\d{4,7})"
_ANNUAL_RANGE_RE = re.compile(
    _AMOUNT + r"\s*(?:-|to|and)\s*" + _AMOUNT, re.IGNORECASE)
_ANNUAL_SINGLE_RE = re.compile(_AMOUNT, re.IGNORECASE)

# A match must carry its own currency/scale evidence. "120000 - 150000" with no
# $, no USD and no k is as likely to be two row counts as a salary band.
_HAS_CURRENCY_RE = re.compile(r"\$|usd|\dk\b|\d\s*k\b", re.IGNORECASE)

# Units that prove the number is not an annual figure.
_NON_ANNUAL_UNIT_RE = re.compile(
    r"^\s*(?:/|per\s+|an?\s+)?\s*(?:hr|hour|hourly|mo|month|monthly|wk|week|weekly|day|daily)\b",
    re.IGNORECASE,
)

# Money in a description is very often not pay. 80 chars of left context is
# enough to catch the sentence subject.
_NOT_PAY_CONTEXT_RE = re.compile(
    r"\b(?:raised|raising|funding|funded|valuation|valued|revenue|arr|mrr|budget|"
    r"grant|award|loan|investment|invested|seed|series\s+[a-e]|market\s+cap|"
    r"savings|saved|cost|costs|deal|deals|contract\s+value|portfolio|aum|"
    r"transactions?|processed|processing)\b",
    re.IGNORECASE,
)

# Annual markers, required before a SINGLE amount in long prose is believed.
_ANNUAL_MARKER_RE = re.compile(
    r"\b(?:salary|salaries|base\s+pay|base|compensation|comp\b|ote|per\s+year|"
    r"/\s*yr|/\s*year|annually|annualized|annual|per\s+annum|pay\s+range|"
    r"pay\s+band)\b",
    re.IGNORECASE,
)

# "Competitive salary", "salary commensurate with experience", "DOE" — a salary
# word with no number. Documented here because it is the single most common
# comp string in real postings and must yield nothing at all.
_NO_NUMBER_COMP_RE = re.compile(
    r"\bcompetitive\s+(?:salary|compensation|pay)\b"
    r"|\bcommensurate\s+with\s+experience\b"
    r"|\bdepend(?:s|ing)?\s+on\s+experience\b"
    r"|\bdoe\b",
    re.IGNORECASE,
)


# Where an hourly unit sits in the text. utils.comp.parse_hourly owns the
# grammar; this only locates candidates so each one can be context-checked.
# No left word boundary: "8hr/day" must be found, precisely so it can be
# REJECTED below.
_HOURLY_UNIT_RE = re.compile(r"(?:/\s*hr|/\s*hour|per\s*hour|an\s*hour|hourly|hr)\b",
                             re.IGNORECASE)

# parse_hourly is a pay parser being pointed at a whole job description, where
# an "hours" number is far more often a SCHEDULE than a wage. Measured on
# jobpilot.db job 562: "Monday - Friday, with basic 8hr/day work requirement"
# parsed as $8.00/hour. An hourly figure in prose is only believed when the
# sentence is about money.
_HOURLY_PAY_CONTEXT_RE = re.compile(
    r"\$|\busd\b|\bpays?\b|\brate\b|\bwage\b|\bsalary\b|\bcompensation\b"
    r"|\bearn\b|\bhiring\s+range\b|\bpay\s+range\b|\bstipend\b",
    re.IGNORECASE,
)
# A unit followed by another period is a schedule, not a rate: "8hr/day".
_HOURLY_TRAILING_SCHEDULE_RE = re.compile(
    r"^\s*(?:/|per\s+|a\s+)\s*(?:day|week|wk|shift|month|mo)\b", re.IGNORECASE)


def _hourly_from(text: str) -> tuple[float, float] | None:
    """Hourly pay in `text`, or None. Grammar delegated to utils.comp."""
    for unit in _HOURLY_UNIT_RE.finditer(text):
        if _HOURLY_TRAILING_SCHEDULE_RE.match(text[unit.end():unit.end() + 12]):
            continue
        window = text[max(0, unit.start() - 80):unit.end()]
        if not _HOURLY_PAY_CONTEXT_RE.search(window):
            continue
        rate = parse_hourly(window)
        if rate and _HOURLY_MIN <= rate[0] <= _HOURLY_MAX and rate[1] <= _HOURLY_MAX:
            return rate
    return None


def _amount_value(raw: str | None) -> float | None:
    """Parse one captured amount into dollars, or None if out of the sane band."""
    if not raw:
        return None
    s = raw.strip().lower().replace(",", "").replace(" ", "")
    try:
        if s.endswith("k"):
            value = float(s[:-1]) * 1000.0
        else:
            value = float(s)
    except ValueError:
        return None
    return value if _ANNUAL_MIN <= value <= _ANNUAL_MAX else None


def _annual_from(text: str, *, comp_field: bool) -> tuple[float, float, str] | None:
    """(min, max, matched text) for an annual figure, or None."""
    for match in _ANNUAL_RANGE_RE.finditer(text):
        span = match.group(0)
        if not _HAS_CURRENCY_RE.search(span):
            continue
        if _NON_ANNUAL_UNIT_RE.match(text[match.end():match.end() + 16]):
            continue
        if _NOT_PAY_CONTEXT_RE.search(text[max(0, match.start() - 80):match.start()]):
            continue
        lo, hi = _amount_value(match.group(1)), _amount_value(match.group(2))
        if lo is None or hi is None:
            continue
        if lo > hi:
            lo, hi = hi, lo
        return (lo, hi, span.strip())

    # A single amount in long prose needs an explicit annual marker nearby; in a
    # dedicated salary field the field itself is the marker.
    for match in _ANNUAL_SINGLE_RE.finditer(text):
        span = match.group(0)
        if not _HAS_CURRENCY_RE.search(span):
            continue
        if _NON_ANNUAL_UNIT_RE.match(text[match.end():match.end() + 16]):
            continue
        window = text[max(0, match.start() - 80):match.end() + 40]
        if _NOT_PAY_CONTEXT_RE.search(text[max(0, match.start() - 80):match.start()]):
            continue
        if not comp_field and not _ANNUAL_MARKER_RE.search(window):
            continue
        value = _amount_value(match.group(1))
        if value is None:
            continue
        return (value, value, span.strip())
    return None


def _scan_comp(text: str, *, comp_field: bool) -> dict:
    """Comp fields readable from one piece of text; {} when it says nothing."""
    if not text:
        return {}
    if _NO_NUMBER_COMP_RE.search(text) and not re.search(r"\d", text):
        return {}

    hourly = _hourly_from(text)
    if hourly:
        return {
            "pay_period": "hourly",
            "hourly_min": hourly[0],
            "hourly_max": hourly[1],
        }

    annual = _annual_from(text, comp_field=comp_field)
    if annual:
        lo, hi, span = annual
        return {
            "pay_period": "annual",
            "salary_min": lo,
            "salary_max": hi,
            "salary_text": span[:200],
        }
    return {}


def detect_comp(description: str | None, salary_text: str | None = None) -> dict:
    """Compensation readable from the posting, or {} when there is none.

    Keys, when present: pay_period ('hourly'|'annual'), hourly_min/hourly_max,
    salary_min/salary_max, salary_text. Hourly detection is delegated to
    utils.comp.parse_hourly so the Behavioral Technician track and this module
    can never disagree about what "$28-$35 an hour" means.

    salary_text is the authoritative field and is read first; only if it yields
    nothing is the description searched, and there a lone dollar figure must sit
    beside an annual marker ("base salary", "per year") to count.
    """
    found = _scan_comp(_norm(salary_text), comp_field=True)
    if found:
        found.setdefault("salary_text", (salary_text or "").strip()[:200])
        return found
    return _scan_comp(_norm(description), comp_field=False)


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------

def enrich(title: str | None, location: str | None, description: str | None = "",
           *, existing: dict | None = None) -> dict:
    """Every Job/RawJob field this posting's text supports, and nothing else.

    The return value is safe to dict-update onto a RawJob or to pass as keyword
    updates to a Job row: keys are omitted entirely when the extractor could not
    decide, and when `existing` already holds a real value for them.

    `existing` treats '', 'unknown', 'n/a' and 0 as "never determined", because
    that is literally what the scanners write when they give up. is_remote gets
    one extra rule: the column defaults to False in db/models.py, so a stored
    False is indistinguishable from "never asked" and may be overwritten — but a
    stored True is a positive claim by a scanner that had the LinkedIn workplace
    flag, and is never overwritten by inference from text.

    work_mode has no column yet; it is returned for the finer Outreach filter and
    callers that do not want it can drop the key.
    """
    have = existing or {}
    out: dict = {}

    def offer(key: str, value: object) -> None:
        if value is None:
            return
        if _is_set(have.get(key)):
            return
        out[key] = value

    offer("work_mode", detect_work_mode(title, location, description))
    offer("is_remote", detect_remote(title, location, description))
    offer("seniority_level", detect_seniority(title, description))
    offer("employment_type", detect_employment_type(title, description))

    comp = detect_comp(description, have.get("salary_text"))
    for key, value in comp.items():
        offer(key, value)

    return out
