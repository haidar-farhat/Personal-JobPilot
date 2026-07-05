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
    for o in options:
        no = normalize(o)
        if nd and (nd in no or no in nd):
            return o
    return None


_DECLINE_HINTS = ("decline", "do not wish", "dont wish", "prefer not", "not to answer",
                  "not wish", "choose not", "rather not", "not answer")

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
        if nd == "no":                                   # "Not Hispanic or Latino"
            for o in options:
                if normalize(o).startswith("not "):
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

    ident = profile.get("identity", {})
    addr = profile.get("address", {})
    links = profile.get("links", {})
    wa = profile.get("work_authorization", {})
    exp = profile.get("experience", {})
    sal = profile.get("salary", {})
    ref = profile.get("referral", {})
    eeoc = profile.get("eeoc", {})

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

    name_set = {"name", "full name", "your name", "legal name", "full legal name",
                "preferred name", "applicant name", "candidate name"}

    # --- Identity ---
    if _has_tok(norm, "first", "given", "fname") and _has_tok(norm, "name"):
        return text(ident.get("first_name"))
    if (_has_tok(norm, "last", "surname", "family", "lname")) and _has_tok(norm, "name"):
        return text(ident.get("last_name"))
    if nlabel in name_set or nname in name_set:
        return text(ident.get("full_name"))
    if _has_sub(norm, "email"):
        return text(ident.get("email"))
    # Workday-style "Country Phone Code" dropdowns want a country, not a number
    if _has_sub(norm, "country code", "country phone", "phone country", "phone code"):
        return option(addr.get("country")) if options else text(addr.get("country"))
    if _has_sub(norm, "phone device", "device type", "phone type"):
        return option("Mobile")
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
    if (_has_sub(norm, "authorized", "authorization", "eligible") and _has_sub(norm, "work")) \
            or _has_sub(norm, "work authorization", "legally authorized"):
        return yesno(wa.get("authorized_to_work_us", True))
    if _has_sub(norm, "citizen"):
        return text(wa.get("citizenship"))

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
        return choice(eeoc.get("race_ethnicity"))
    if _has_sub(norm, "veteran", "protected veteran", "military"):
        return choice(eeoc.get("veteran_status"))
    if _has_sub(norm, "disability", "disabled"):
        return choice(eeoc.get("disability_status"))

    # --- Address ---
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

    # --- Source / referral ---
    if _has_sub(norm, "how did you hear", "referral", "referred by") or _has_tok(norm, "source"):
        return option(ref.get("default_source"))

    return None


def build_plan(fields, profile, archetype, resume_summary, essay_fn=None, max_essays: int = 3) -> dict:
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
        is_open = ftype in ESSAY_TYPES or label.endswith("?") or ftype == "select"
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
