"""Provenance gate: nothing reaches a .docx that isn't in the base résumé.

WHY THIS EXISTS
---------------
On 2026-09-09 an audit of the 197 shipped sidecar JSONs in output/resumes found
that 158 of them (80%) claimed Pandas, NumPy, scikit-learn and Statsmodels, 152
claimed TensorFlow, 87 claimed PyTorch, 3 claimed a "Tableau Desktop Specialist"
certification and 1 claimed Reed College / MS Quantitative Economics. None of
those appear anywhere in config/base_resume.yaml.

The source was not the model inventing freely — it was the worked JSON example
inside RESUME_PROMPT_TEMPLATE, which carried a PREVIOUS candidate's real skills
and certifications. A 7B model decoding at temperature 0.2 / top_k 20 copies a
concrete example in preference to reading the supplied YAML. Separately, the
behavioral-technician route reads config/base_resume_bt.yaml, which was never
filled in, so it fed the model "Jane Doe / Your University" and one résumé
carrying "Your University" shipped to a real employer.

Prompt wording alone cannot prevent this: "never fabricate" was already in the
prompt (twice) while all of the above was happening. The only reliable control
is a deterministic gate on the OUTPUT, which is what this module is. It runs
after generation and before rendering, and it is not optional.

CONTRACT
--------
Every skill token, certification, institution, degree, employer and project
title in the generated résumé must appear in the base résumé YAML. Anything
that does not is dropped and recorded. The gate only ever REMOVES content — it
never invents a replacement — so a stripped résumé is short and true rather
than full and false.

Bullet prose is deliberately NOT vocabulary-filtered: rewriting a real bullet in
the job description's words is the entire point of tailoring, and a token filter
there would block legitimate reframing. Bullets are instead checked for
*fabricated specifics* — named technologies absent from the base résumé, and
quantified claims with no counterpart in the source bullet.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# Words that are structure, not claims — never treated as a skill needing proof.
_STOPWORDS = frozenset("""
a an and or the of to in for with using via on at by from as is are was were be
been being this that these those it its their our my your his her they we you i
built designed developed implemented created led managed improved increased
reduced delivered shipped owned drove ran wrote maintained supported worked
across into over under between during within through more most less least
than then so such other others including include includes included etc
new use used uses user users team teams project projects system systems
data time work role years year experience
""".split())

# Structural placeholders from the shipped example configs. Their presence in a
# generated résumé means an unedited template was used as source material.
TEMPLATE_PLACEHOLDERS = (
    "jane doe", "john doe", "your university", "your.email@example.com",
    "555-555-5555", "your-handle", "city, state", "your handle",
    "school name", "degree name", "org name", "project title", "role title",
    "tailored bullet", "graduation date", "date range", "tech1", "tech2",
)


def _norm(s: str) -> str:
    """Lowercase, collapse punctuation that varies between renderings."""
    s = (s or "").lower()
    s = s.replace("–", "-").replace("—", "-").replace("’", "'")
    return re.sub(r"\s+", " ", s).strip()


# Singular words ending in -is, which a naive plural rule would corrupt
# ("analysis" -> "analysi"). Words like "apis" and "uris" are genuine plurals
# of acronyms and MUST still be trimmed, so a blanket -is guard is wrong.
_IS_SINGULARS = frozenset({
    "analysis", "basis", "thesis", "axis", "diagnosis", "synthesis",
    "hypothesis", "genesis", "crisis", "emphasis", "parenthesis", "prognosis",
})


def _singular(t: str) -> str:
    """Crude depluraliser, enough to stop 'API' failing against 'APIs'.

    Trims a trailing 's' only where it is unlikely to belong to the word:
    '-ss' and '-us' endings are left alone ('css', 'status'), as are the
    known -is singulars above. 'aws' is 3 chars and so falls under the length
    floor.
    """
    if (len(t) > 3 and t.endswith("s")
            and not t.endswith(("ss", "us"))
            and t not in _IS_SINGULARS):
        return t[:-1]
    return t


def _tokens(s: str) -> set[str]:
    """Content tokens of a string, stopwords removed.

    Keeps '+' and '#' so 'c++' and 'c#' survive, and keeps dots so 'node.js'
    stays whole. A hyphenated compound contributes BOTH the whole token and its
    parts, so the base résumé's 'OpenAI-compatible APIs' covers a generated
    'OpenAI API' — the same claim in different packaging — while leaving a
    genuinely absent 'scikit-learn' unmatched.
    """
    raw = re.findall(r"[a-z0-9][a-z0-9+#.\-]*", _norm(s))
    out: set[str] = set()
    for tok in raw:
        tok = tok.strip(".-")
        if not tok or tok in _STOPWORDS:
            continue
        out.add(_singular(tok))
        if "-" in tok:
            out.update(_singular(p) for p in tok.split("-") if p and p not in _STOPWORDS)
    return out


def _flatten(node: Any, out: list[str]) -> None:
    """Collect every scalar in a nested dict/list into out."""
    if isinstance(node, dict):
        for k, v in node.items():
            out.append(str(k))
            _flatten(v, out)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _flatten(v, out)
    elif node is not None:
        out.append(str(node))


def base_vocabulary(base_resume: dict) -> set[str]:
    """Every token the candidate can legitimately claim, from the base résumé."""
    scalars: list[str] = []
    _flatten(base_resume, scalars)
    vocab: set[str] = set()
    for s in scalars:
        vocab |= _tokens(s)
    return vocab


def _phrase_supported(phrase: str, vocab: set[str]) -> bool:
    """True when every content token of `phrase` appears in the base résumé.

    Multi-word skills ("computer vision") are supported only if BOTH words are
    present, which is what stops "machine learning" being smuggled in on the
    strength of an unrelated "learning".
    """
    toks = _tokens(phrase)
    if not toks:
        return False
    return toks <= vocab


def _split_skills(value: Any) -> list[str]:
    """Skill lines arrive as 'A, B, C' strings or as lists; normalise to a list."""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


class GroundingReport:
    """What the gate removed, for logging and for the dashboard."""

    def __init__(self) -> None:
        self.removed_skills: list[str] = []
        self.removed_certs: list[str] = []
        self.removed_education: list[str] = []
        self.removed_entries: list[str] = []
        self.flagged_bullets: list[dict] = []
        self.template_leaks: list[str] = []

    @property
    def clean(self) -> bool:
        return not (self.removed_skills or self.removed_certs or self.removed_education
                    or self.removed_entries or self.template_leaks)

    @property
    def total_removed(self) -> int:
        return (len(self.removed_skills) + len(self.removed_certs)
                + len(self.removed_education) + len(self.removed_entries))

    def as_dict(self) -> dict:
        return {
            "clean": self.clean,
            "total_removed": self.total_removed,
            "removed_skills": self.removed_skills,
            "removed_certs": self.removed_certs,
            "removed_education": self.removed_education,
            "removed_entries": self.removed_entries,
            "flagged_bullets": self.flagged_bullets,
            "template_leaks": self.template_leaks,
        }

    def summary(self) -> str:
        if self.clean and not self.flagged_bullets:
            return "grounded: no unsupported claims"
        bits = []
        if self.template_leaks:
            bits.append(f"{len(self.template_leaks)} TEMPLATE PLACEHOLDER(S)")
        if self.removed_skills:
            bits.append(f"{len(self.removed_skills)} skill(s)")
        if self.removed_certs:
            bits.append(f"{len(self.removed_certs)} cert(s)")
        if self.removed_education:
            bits.append(f"{len(self.removed_education)} education entr(ies)")
        if self.removed_entries:
            bits.append(f"{len(self.removed_entries)} experience entr(ies)")
        if self.flagged_bullets:
            bits.append(f"{len(self.flagged_bullets)} bullet(s) flagged")
        return "stripped " + ", ".join(bits)


def detect_template_leaks(resume_data: dict) -> list[str]:
    """Placeholder strings from an unedited example config that reached output."""
    scalars: list[str] = []
    _flatten(resume_data, scalars)
    blob = _norm(" | ".join(scalars))
    return [p for p in TEMPLATE_PLACEHOLDERS if p in blob]


def enforce(resume_data: dict, base_resume: dict) -> tuple[dict, GroundingReport]:
    """Strip every claim not supported by the base résumé.

    Returns (cleaned_copy, report). Pure — the input dict is not mutated.
    """
    import copy

    data = copy.deepcopy(resume_data or {})
    report = GroundingReport()
    vocab = base_vocabulary(base_resume or {})

    if not vocab:
        # No base résumé means nothing can be verified. Refuse rather than
        # pass everything through — an empty vocabulary must not read as
        # "all claims approved".
        raise ValueError("grounding: base résumé produced an empty vocabulary")

    report.template_leaks = detect_template_leaks(data)

    # ---- skills: drop unsupported tokens, drop emptied categories ----------
    skills = data.get("skills")
    if isinstance(skills, dict):
        cleaned: dict[str, str] = {}
        for label, value in skills.items():
            kept = []
            for item in _split_skills(value):
                if _phrase_supported(item, vocab):
                    kept.append(item)
                else:
                    report.removed_skills.append(f"{label}: {item}")
            if kept:
                cleaned[label] = ", ".join(kept)
        data["skills"] = cleaned

    # ---- certifications: name must appear in the base résumé --------------
    certs = data.get("certifications")
    if isinstance(certs, list):
        kept_certs = []
        for cert in certs:
            name = cert.get("name", "") if isinstance(cert, dict) else str(cert)
            if _phrase_supported(name, vocab):
                kept_certs.append(cert)
            else:
                report.removed_certs.append(str(name))
        data["certifications"] = kept_certs

    # ---- education: institution and degree must both be real --------------
    edu = data.get("education")
    if isinstance(edu, list):
        kept_edu = []
        for e in edu:
            if not isinstance(e, dict):
                continue
            inst, degree = e.get("institution", ""), e.get("degree", "")
            if _phrase_supported(inst, vocab) and _phrase_supported(degree, vocab):
                kept_edu.append(e)
            else:
                report.removed_education.append(f"{degree} — {inst}".strip(" —"))
        data["education"] = kept_edu

    # ---- experience / projects: the ENTRY identity must be real -----------
    # Bullet prose is reframing and stays; the employer, title and project name
    # are facts and must match the base résumé.
    for key, fields in (("work_experience", ("organization", "title")),
                        ("project_experience", ("title",))):
        entries = data.get(key)
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if all(_phrase_supported(entry.get(f, ""), vocab) for f in fields):
                kept.append(entry)
            else:
                label = " / ".join(str(entry.get(f, "")) for f in fields).strip(" /")
                report.removed_entries.append(f"{key}: {label}")
        data[key] = kept

    # ---- bullets: flag fabricated specifics, don't silently delete --------
    # A bullet is prose the model is meant to rewrite, so it is reported rather
    # than removed; the caller decides whether to regenerate.
    for key in ("work_experience", "project_experience"):
        for entry in data.get(key) or []:
            if not isinstance(entry, dict):
                continue
            for bullet in entry.get("bullets") or []:
                text = bullet if isinstance(bullet, str) else bullet.get("text", "")
                # Match on the normalised form but REPORT the surface form, so a
                # reviewer sees "kubernetes", not the stemmed "kubernete".
                unsupported = sorted({
                    surface for surface in re.findall(r"[a-z0-9][a-z0-9+#.\-]*", _norm(text))
                    if (s := surface.strip(".-")) and s not in _STOPWORDS
                    and _singular(s) not in vocab
                    and not s.replace(".", "").isdigit()
                })
                if unsupported:
                    report.flagged_bullets.append({
                        "entry": str(entry.get("title") or entry.get("organization") or ""),
                        "text": text,
                        "unsupported_terms": unsupported[:10],
                    })

    return data, report


def enforce_and_log(resume_data: dict, base_resume: dict, *, label: str = "") -> dict:
    """enforce() with a log line. Returns the cleaned résumé data."""
    cleaned, report = enforce(resume_data, base_resume)
    prefix = f"[grounding]{(' ' + label) if label else ''}"
    if report.template_leaks:
        logger.error(f"{prefix} TEMPLATE PLACEHOLDERS in generated résumé: "
                     f"{report.template_leaks} — the source config is unedited")
    if not report.clean:
        logger.warning(f"{prefix} {report.summary()}")
        for s in report.removed_skills:
            logger.warning(f"{prefix}   dropped skill: {s}")
        for c in report.removed_certs:
            logger.warning(f"{prefix}   dropped certification: {c}")
        for e in report.removed_education:
            logger.warning(f"{prefix}   dropped education: {e}")
        for x in report.removed_entries:
            logger.warning(f"{prefix}   dropped entry: {x}")
    else:
        logger.info(f"{prefix} {report.summary()}")
    cleaned["_grounding"] = report.as_dict()
    return cleaned
