"""Pure, testable field mapping for the autofill browser extension.

No I/O. Given a scanned form field and the applicant profile, decide a value
deterministically; assemble a fill-plan, delegating essays / ambiguous fields
to an injectable callable (the API wires that to Ollama).

Field shape (input):  {id, label, name, type, options?, required?}
Result shape (output): {id, value, source, confidence, needs_review}
  source ∈ {"deterministic","llm","fallback","file","none"}
"""

from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Free-text field types that should be routed to the essay/LLM path.
ESSAY_TYPES = {"textarea"}


def normalize(s: str | None) -> str:
    """Lowercase, replace runs of non-alphanumerics with a single space, trim."""
    return _NON_ALNUM.sub(" ", (s or "").lower()).strip()


def _has_tok(norm: str, *words: str) -> bool:
    """True if any of *words is an exact whitespace token of *norm* (no substrings)."""
    toks = set(norm.split())
    return any(w in toks for w in words)


def _has_sub(norm: str, *subs: str) -> bool:
    """True if any of *subs appears as a substring of *norm* (use for distinctive terms)."""
    return any(s in norm for s in subs)


def _match_option(desired: str, options: list[str]) -> str | None:
    """Return the option that best matches *desired* (exact-normalized, then substring)."""
    nd = normalize(desired)
    for o in options:
        if normalize(o) == nd:
            return o
    # Substring tier: prefer the TIGHTEST match, not the first in list order —
    # "United States" must pick "United States of America", not the
    # alphabetically-earlier "United States Minor Outlying Islands".
    best = None
    for o in options:
        no = normalize(o)
        if nd and (nd in no or no in nd):
            d = abs(len(no) - len(nd))
            if best is None or d < best[0]:
                best = (d, o)
    return best[1] if best else None


_DECLINE_HINTS = ("decline", "do not wish", "dont wish", "prefer not", "not to answer",
                  "not wish", "choose not", "rather not", "not answer")

# How ATSes spell "Yes"/"No" on consent questions: "Confirmed", "I acknowledge",
# "No, I am not a current or former Government Official", …
_AFFIRM_HINTS = ("confirmed", "confirm", "i confirm", "i agree", "agree",
                 "i acknowledge", "acknowledge", "i accept", "accept",
                 "i understand", "i consent", "i have read")
_NEGATE_HINTS = ("not ", "i do not", "i dont", "i have not", "i am not",
                 "i havent", "never ", "none")

_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]

# Cross-vocabulary synonyms: some ATSes render "Man/Woman" for gender, spell out
# race categories differently, etc. Keys and values are normalize()d forms.
_CHOICE_SYNONYMS = {
    "male": ["man"],
    "female": ["woman"],
    "heterosexual": ["straight", "heterosexual straight"],
    "two or more races": ["two or more", "multiracial", "multiple races"],
}


def _match_choice(desired: str, options: list[str]) -> str | None:
    """EEO / single-choice matcher tolerant of verbose ATS option text.

    Handles the ways a short stored answer maps onto a long rendered option:
    "No" → "Not Hispanic or Latino"; "Yes" → "Yes, I Have A Disability…";
    "Decline to answer" → "I don't wish to answer"; "Male" → "Man"; and finally
    best token overlap ("Two or More Races" → "Two or More Races (Not Hispanic…)").
    """
    if not options:
        return desired
    nd = normalize(desired)
    cands = [nd] + _CHOICE_SYNONYMS.get(nd, [])
    for c in cands:                                      # exact (incl. synonyms)
        for o in options:
            if normalize(o) == c:
                return o
    if nd in ("decline to answer", "decline", "prefer not to say", "prefer not to answer",
              "i dont wish to answer", "do not wish to answer", "i do not wish to answer"):
        for o in options:
            if any(h in normalize(o) for h in _DECLINE_HINTS):
                return o
    if nd in ("yes", "no"):                              # leading-token yes/no
        for o in options:
            if normalize(o).split(" ")[:1] == [nd]:
                return o
        hints = _AFFIRM_HINTS if nd == "yes" else _NEGATE_HINTS
        for o in options:                                # "Confirmed" / "Not Hispanic or Latino"
            no = normalize(o)
            if any(no == h.strip() or no.startswith(h.strip() + " ") for h in hints):
                return o
    for c in cands:                                      # substring either way
        for o in options:
            no = normalize(o)
            if c and (c in no or no in c):
                return o
    dwords = [w for w in nd.split() if len(w) > 2]       # best token overlap
    best, best_n = None, 0
    for o in options:
        ow = set(normalize(o).split())
        n = sum(1 for w in dwords if w in ow)
        if n > best_n:
            best, best_n = o, n
    return best


def choose_archetype(title, page_text, archetypes, resume_pref: str = "auto") -> str | None:
    """Pick the archetype key driving résumé routing.

    resume_pref "ai" -> None (AI/data résumé), "bt" -> behavioral_technician.
    Otherwise keyword-match the title first, then the page text, against each
    archetype's ``keywords`` list (config order wins ties). None if no hit.
    """
    if resume_pref == "ai":
        return None
    if resume_pref == "bt":
        return "behavioral_technician"
    for hay in ((title or "").lower(), (page_text or "").lower()):
        if not hay:
            continue
        for key, arch in (archetypes or {}).items():
            for kw in arch.get("keywords", []):
                if kw and kw in hay:
                    return key
    return None


def map_standard_field(field: dict, profile: dict) -> dict | None:
    """Map a single field to a profile value deterministically, or None if unknown."""
    ftype = (field.get("type") or "text").lower()
    options = field.get("options") or []

    if ftype == "file":
        return {"value": None, "source": "file", "confidence": 1.0, "needs_review": True}

    nlabel = normalize(field.get("label") or "")
    nname = normalize(field.get("name") or "")
    norm = (nlabel + " " + nname).strip()
    if not norm:
        return None

    nsection = normalize(field.get("section") or "")

    ident = profile.get("identity", {})
    addr = profile.get("address", {})
    links = profile.get("links", {})
    wa = profile.get("work_authorization", {})
    exp = profile.get("experience", {})
    sal = profile.get("salary", {})
    ref = profile.get("referral", {})
    eeoc = profile.get("eeoc", {})
    edu = profile.get("education") or []
    e0 = edu[0] if edu else {}
    prefs = profile.get("preferences", {})

    def text(v):
        return {"value": v, "source": "deterministic", "confidence": 0.95,
                "needs_review": False} if v else None

    def option(desired, conf=0.9):
        if not desired:
            return None
        if not options:
            return {"value": desired, "source": "deterministic", "confidence": conf, "needs_review": False}
        m = _match_option(desired, options)
        if m is None:
            return {"value": desired, "source": "deterministic", "confidence": 0.5, "needs_review": True}
        return {"value": m, "source": "deterministic", "confidence": conf, "needs_review": False}

    def choice(desired, conf=0.9):
        """Like option() but with EEO-tolerant matching (verbose option text)."""
        if not desired:
            return None
        if not options:
            return {"value": desired, "source": "deterministic", "confidence": conf, "needs_review": False}
        m = _match_choice(desired, options)
        if m is None:
            return {"value": desired, "source": "deterministic", "confidence": 0.5, "needs_review": True}
        return {"value": m, "source": "deterministic", "confidence": conf, "needs_review": False}

    def yesno(flag):
        return choice("Yes" if flag else "No")

    def month_year(m, y):
        """Answer a date field: month/year <select>s (year lists are numeric,
        month lists aren't), split month/year text inputs, or one MM/YYYY box.
        Comboboxes (react-select) expose no options at plan time — answer with
        the month NAME / year string so the fill-side option matcher commits."""
        is_combo = bool(field.get("combo"))
        if options:
            if y and any((o or "").strip() == str(y) for o in options):
                return option(str(y))
            if m and not any((o or "").strip().isdigit() for o in options[:5] if o):
                return choice(_MONTH_NAMES[int(m) - 1])
            return None
        if _has_tok(norm, "year"):
            return text(str(y)) if y else None
        if _has_tok(norm, "month"):
            if not m:
                return None
            return choice(_MONTH_NAMES[int(m) - 1]) if is_combo else text(f"{int(m):02d}")
        return text(f"{int(m):02d}/{y}") if (m and y) else (text(str(y)) if y else None)

    name_set = {"name", "full name", "your name", "legal name", "full legal name",
                "preferred name", "applicant name", "candidate name"}

    # --- Identity ---
    if _has_tok(norm, "first", "given", "fname") and _has_tok(norm, "name"):
        return text(ident.get("first_name"))
    if (_has_tok(norm, "last", "surname", "family", "lname")) and _has_tok(norm, "name"):
        return text(ident.get("last_name"))
    if nlabel in name_set or nname in name_set:
        return text(ident.get("full_name"))
    # SMS/WhatsApp consent questions mention "email" and "telephone" in their
    # fine print ("If you select no, we will only communicate with you via
    # email and/or telephone calls") — decide them BEFORE the contact rules.
    if _has_sub(norm, "sms", "whatsapp", "text message"):
        return yesno(profile.get("preferences", {}).get("sms_opt_in", True))
    if _has_sub(norm, "email"):
        return text(ident.get("email"))
    # Workday-style "Country Phone Code" dropdowns want a country, not a number
    if _has_sub(norm, "country code", "country phone", "phone country", "phone code"):
        return option(addr.get("country")) if options else text(addr.get("country"))
    if _has_sub(norm, "phone device", "device type", "phone type"):
        return option("Mobile")
    # "Phone Extension" must stay EMPTY — it contains "phone", so decide it
    # before the phone rule (NVIDIA Workday got the full number typed into it)
    if _has_tok(norm, "extension", "ext"):
        return {"value": None, "source": "deterministic", "confidence": 1.0, "needs_review": False}
    if _has_sub(norm, "phone", "mobile", "telephone"):
        return text(ident.get("phone"))

    # --- Links ---
    if _has_sub(norm, "linkedin"):
        return text(links.get("linkedin"))
    if _has_sub(norm, "github"):
        return text(links.get("github"))
    if _has_sub(norm, "portfolio", "personal website", "personal site") or norm == "website" or norm == "url":
        return text(links.get("portfolio") or links.get("website"))

    # --- Work eligibility (sponsorship before generic auth) ---
    if _has_sub(norm, "sponsor"):
        return yesno(wa.get("requires_sponsorship", False))
    if (_has_sub(norm, "authorized", "authorization", "eligible", "permitted") and _has_sub(norm, "work")) \
            or _has_sub(norm, "work authorization", "legally authorized", "right to work"):
        return yesno(wa.get("authorized_to_work_us", True))
    if _has_sub(norm, "citizen"):
        return text(wa.get("citizenship"))

    # --- Relocation / on-site willingness (already in SF — yes to both) ---
    if _has_sub(norm, "relocat", "willing to move", "open to moving"):
        return yesno(prefs.get("willing_to_relocate", True))
    if _has_sub(norm, "in person", "onsite", "on site", "in office", "in the office",
                "hybrid", "commute") \
            and _has_sub(norm, "willing", "are you", "can you", "able to",
                         "comfortable", "open to"):
        return yesno(prefs.get("willing_onsite", True))

    # --- EEOC (checked before address: "ethnicity" contains the substring "city") ---
    # Order matters: the more specific identity questions come before "gender"
    # because e.g. "gender identity" contains the substring "gender".
    if _has_sub(norm, "transgender"):
        return choice(eeoc.get("transgender") or "No")
    if _has_sub(norm, "sexual orientation") or _has_tok(norm, "orientation"):
        return choice(eeoc.get("sexual_orientation"))
    if _has_sub(norm, "lgbtq", "lgbtqia", "lgbt"):
        return choice(eeoc.get("lgbtq"))
    if _has_sub(norm, "hispanic", "latino", "latinx"):
        return choice(eeoc.get("hispanic_latino"))
    if _has_sub(norm, "gender"):  # covers "gender" and "gender identity"
        return choice(eeoc.get("gender"))
    if _has_sub(norm, "race", "ethnicity"):
        # Checkbox "select all that apply" groups get the individual components
        # ("Asian; White") — fill.js checks one box per ";"-separated part.
        # Single-choice renders (radio/select/listbox) keep the umbrella answer.
        comps = eeoc.get("race_components") or []
        if ftype == "checkbox" and len(comps) > 1:
            return {"value": "; ".join(comps), "source": "deterministic",
                    "confidence": 0.9, "needs_review": False}
        return choice(eeoc.get("race_ethnicity"))
    if _has_sub(norm, "veteran", "protected veteran", "military"):
        return choice(eeoc.get("veteran_status"))
    if _has_sub(norm, "disability", "disabled"):
        return choice(eeoc.get("disability_status"))

    # --- Education section (school / degree / discipline / GPA / dates) ---
    # Fills entry 0 (most recent). Multi-entry forms are handled by the
    # Greenhouse/Workday engines, which own their sections so we never clash.
    in_education = "education" in nsection or _has_sub(norm, "education")
    # Token match, NOT substring: Coinbase's "were you referred … by a senior
    # leader at a prospective INSTITUTIONAL client?" must not become a school.
    if _has_tok(norm, "school", "university", "college", "institution", "institute") \
            and not _has_sub(norm, "high school"):
        v = e0.get("school")
        return (option(v) if options else text(v)) if v else None
    if _has_sub(norm, "discipline", "major", "field of study", "area of study", "concentration"):
        v = e0.get("discipline") or e0.get("field_of_study")
        return choice(v) if v else None
    if _has_tok(norm, "gpa") or _has_sub(norm, "grade point"):
        v = str(e0.get("gpa") or "").strip()
        return text(v) if v else None
    if _has_tok(norm, "degree") and not _has_sub(norm, "highest"):
        v = e0.get("degree") or exp.get("highest_education")
        if not v:
            return None
        return choice(v) if options else text(e0.get("degree_full") or v)
    if in_education and (_has_tok(norm, "end", "graduation") or _has_sub(norm, "date completed")):
        return month_year(e0.get("end_month"), e0.get("end_year"))

    # --- Employment / work-experience section (entry 0 = most recent role;
    #     multi-entry sections are owned by the Greenhouse/Workday engines) ---
    work_hist = profile.get("employment") or []
    w0 = work_hist[0] if work_hist else {}
    in_employment = _has_sub(nsection, "employment", "work experience", "work history", "experience") \
        or _has_sub(norm, "employment")
    if in_employment and w0:
        if _has_tok(norm, "company", "employer", "organization"):
            return option(w0.get("company")) if options else text(w0.get("company"))
        if _has_tok(norm, "title", "position", "role"):
            return text(w0.get("title"))
        if _has_sub(norm, "currently work", "current position", "i currently", "present"):
            return yesno(bool(w0.get("current")))
        if _has_tok(norm, "start", "from"):
            return month_year(w0.get("start_month"), w0.get("start_year"))
        if _has_tok(norm, "end", "to") and not w0.get("current"):
            return month_year(w0.get("end_month"), w0.get("end_year"))
        if _has_sub(norm, "responsibilities", "duties", "description"):
            return text(w0.get("description"))

    # --- Location (typeahead "Location (City)" / bare or "Current Location",
    #     plus phrasings that never say "location") ---
    if (_has_tok(norm, "location") and not _has_sub(norm, "office")) \
            or _has_sub(norm, "where are you located", "currently located", "where do you live",
                        "where are you based", "currently based", "currently reside",
                        "city of residence", "current city"):
        if options:
            # The list may be cities, states, OR countries ("Where are you
            # currently based?" with a country dropdown) — try each granularity.
            for cand in (addr.get("city"), addr.get("state_full"), addr.get("country")):
                if cand and _match_option(cand, options):
                    return option(cand)
            return option(addr.get("city"))
        return text(addr.get("location_line") or addr.get("city"))

    # --- Address ---
    # Line 2 (apt/suite) is optional and not ours to invent — deterministic no-fill.
    # Checked first: "street address line 2" also contains "street address".
    if _has_sub(norm, "address line 2", "address 2") or _has_tok(norm, "apt", "apartment", "suite"):
        return {"value": None, "source": "deterministic", "confidence": 1.0, "needs_review": False}
    if _has_sub(norm, "address line 1", "address 1", "street address") or _has_tok(norm, "street"):
        return text(addr.get("street"))
    if nlabel == "address" or _has_sub(norm, "home address", "mailing address",
                                       "current address", "residential address"):
        line = ", ".join(p for p in (addr.get("street"), addr.get("city"), addr.get("state")) if p)
        zipc = addr.get("postal_code")
        return text(f"{line} {zipc}".strip() if zipc else line)
    if _has_tok(norm, "city"):
        return option(addr.get("city")) if options else text(addr.get("city"))
    if _has_tok(norm, "state", "province"):
        # Prefer whichever of full-name / abbreviation matches the select's options.
        if options:
            for cand in (addr.get("state_full"), addr.get("state")):
                if cand and _match_option(cand, options):
                    return option(cand)
            return option(addr.get("state"))
        return text(addr.get("state"))
    if _has_tok(norm, "zip", "zipcode", "postal") or _has_sub(norm, "postal code", "zip code"):
        return text(addr.get("postal_code"))
    if _has_sub(norm, "country"):
        return option(addr.get("country")) if options else text(addr.get("country"))

    # --- Experience ---
    if _has_sub(norm, "years of experience") or (_has_sub(norm, "experience") and _has_tok(norm, "years")):
        yrs = exp.get("total_years")
        return text(str(yrs)) if yrs is not None else None
    if _has_sub(norm, "education", "degree"):
        return option(exp.get("highest_education")) if options else text(exp.get("highest_education"))
    if _has_sub(norm, "current employer", "current company") or (_has_sub(norm, "employer")):
        return text(exp.get("current_employer"))
    if _has_sub(norm, "current title", "current role") or (_has_tok(norm, "title") and _has_sub(norm, "current")):
        return text(exp.get("current_title"))

    # --- Compensation ---
    if _has_sub(norm, "salary", "compensation", "desired pay", "expected pay", "rate of pay"):
        return text(sal.get("preferred_text"))

    # --- Recurring consent / preference questions ---
    if _has_sub(norm, "worked at", "worked for", "worked here", "previously employed",
                "been employed by", "employed by", "former employee",
                "current or former employee"):
        if not prefs.get("worked_here_before", False) and options:
            for o in options:  # prefer explicit "I have not worked at …" over bare "No"
                if _has_sub(normalize(o), "not worked", "never worked", "have not worked"):
                    return {"value": o, "source": "deterministic", "confidence": 0.9, "needs_review": False}
        return yesno(prefs.get("worked_here_before", False))
    if _has_sub(norm, "18 years", "at least 18", "age of 18", "over 18", "minimum age"):
        return yesno(prefs.get("age_18_or_older", True))
    # Availability — "When can you start?" / "Earliest start date" / "Date
    # available". Employment/education section dates never reach here (their
    # section-gated rules run above).
    if _has_sub(norm, "when can you start", "when could you start", "when can you begin",
                "available to start", "earliest start", "start date", "date available",
                "availability date", "want to start working", "when do you want to start"):
        return text(prefs.get("earliest_start_date"))
    # Compliance screeners (Coinbase-style): government-official family,
    # conflict-of-interest, insider referral. Verbose option vocabularies
    # ("No, I am not a current or former Government Official") resolve via
    # the leading yes/no token in _match_choice / fill.js.
    if _has_sub(norm, "government official", "government agency", "public office"):
        if _has_sub(norm, "relative"):
            return yesno(prefs.get("relative_of_government_official", False))
        return yesno(prefs.get("government_official", False))
    if _has_sub(norm, "conflict of interest"):
        return yesno(prefs.get("conflict_of_interest", False))
    if _has_sub(norm, "were you referred", "referred to this position", "referred to this role"):
        return yesno(prefs.get("referred_by_insider", False))
    # "How do you use AI tools?" self-assessment — BEFORE the acknowledgement
    # rule ("I understand that {company} may USE AI TOOLS…" is an ack, this
    # is not: it asks how *you* use them).
    if _has_sub(norm, "how you use ai", "you use ai tools", "your use of ai"):
        v = prefs.get("ai_tools_usage")
        return choice(v) if v else None
    if _has_sub(norm, "privacy", "acknowledg", "consent", "agree to the", "i agree",
                "i understand", "confirm receipt", "i have read"):
        return yesno(prefs.get("privacy_acknowledged", True))

    # --- Source / referral ---
    if _has_sub(norm, "how did you hear", "referral", "referred by") or _has_tok(norm, "source"):
        return option(ref.get("default_source"))

    return None


def build_plan(fields, profile, archetype, resume_summary, essay_fn=None, max_essays: int = 4) -> dict:
    """Assemble the fill-plan: deterministic mapping first, essay_fn for the rest.

    essay_fn(field, context) -> str | None, where context = {archetype, resume}.
    """
    out = []
    llm_used = 0
    for f in fields:
        m = map_standard_field(f, profile)
        if m is not None:
            out.append({"id": f["id"], **m})
            continue

        ftype = (f.get("type") or "text").lower()
        label = (f.get("label") or "").strip()
        # Open-ended: textareas, question-marked labels, selects — and plain
        # text inputs whose label reads like a question ("Describe your biggest
        # accomplishment", "Why do you want to work here") rather than a field name.
        is_open = ftype in ESSAY_TYPES or label.endswith("?") or ftype == "select" \
            or (ftype == "text" and len(label.split()) >= 5)
        if essay_fn and is_open and llm_used < max_essays:
            try:
                val = essay_fn(f, {"archetype": archetype, "resume": resume_summary})
            except Exception:
                val = None
            if val:
                llm_used += 1
                out.append({"id": f["id"], "value": val, "source": "llm",
                            "confidence": 0.7, "needs_review": True})
                continue

        out.append({"id": f["id"], "value": None, "source": "none",
                    "confidence": 0.0, "needs_review": True})

    filled = sum(1 for o in out if o["value"])
    needs = sum(1 for o in out if o["needs_review"])
    return {"fields": out, "stats": {"filled": filled, "needs_review": needs, "llm_used": llm_used}}
