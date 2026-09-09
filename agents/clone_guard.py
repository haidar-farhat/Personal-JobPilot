"""Detects when a freshly tailored résumé is a near-copy of another job's résumé.

WHY THIS EXISTS
---------------
Measured across the 197 shipped sidecar JSONs in output/resumes on 2026-09-09:

  * one bullet — "Engineered backend services and REST APIs using Laravel and
    MySQL." — appeared verbatim in 163 of 197 résumés
  * 39% of all 2,408 generated bullets were byte-for-byte copies of a
    config/base_resume.yaml bullet; 70% were >=80% similar to one
  * 1.9% of all résumé PAIRS scored >=85% similar, the worst at 94.2%
    (Checkr "Solutions Engineer" vs Gusto "AI Engineer")

The cause was upstream — a near-greedy sampler decoding a prompt that barely
changes between jobs — and is fixed there. This module is the *detector* that
proves the fix works and catches regressions: it measures a draft against
recent drafts for OTHER jobs and, when they are too alike, hands the tailor a
concrete feedback block naming the shared phrases to rewrite.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It never blocks shipping. An under-differentiated résumé is still an HONEST
résumé, and refusing to ship one would trade a real problem (fabrication) for a
cosmetic one. After the tailor's last round the best draft ships, flagged.

It also exempts genuinely similar jobs: two "Senior Data Analyst" postings with
near-identical descriptions SHOULD produce near-identical résumés, and
penalising that would push the model toward inventing differences — the exact
failure this codebase is trying to eliminate.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Bumped when fingerprinting changes shape, to invalidate the on-disk cache.
ENGINE_VERSION = 1

_CACHE_NAME = ".fingerprints.json"


# ---------------------------------------------------------------------------
# text -> comparable shape
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace."""
    t = (text or "").lower()
    t = t.replace("–", "-").replace("—", "-").replace("’", "'")
    t = re.sub(r"[^a-z0-9+#./\- ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _bullet_text(bullet) -> str:
    if isinstance(bullet, dict):
        return str(bullet.get("text") or "")
    return str(bullet or "")


def bullets_of(resume_data: dict) -> list[str]:
    """Every bullet in the résumé, in render order."""
    out: list[str] = []
    for key in ("project_experience", "work_experience"):
        for entry in resume_data.get(key) or []:
            if not isinstance(entry, dict):
                continue
            for b in entry.get("bullets") or []:
                text = _bullet_text(b)
                if text:
                    out.append(text)
    return out


def entries_of(resume_data: dict) -> list[str]:
    """Identity of each selected entry — what the model CHOSE, not how it worded it."""
    out: list[str] = []
    for entry in resume_data.get("work_experience") or []:
        if isinstance(entry, dict):
            # Normalise the parts separately, then join with a separator _norm
            # would otherwise strip. Normalising the joined string collapses the
            # boundary, so ("Data Analyst", "Corp") and ("Data", "Analyst Corp")
            # would produce the same identity.
            out.append(f"{_norm(str(entry.get('title', '')))}"
                       f"@{_norm(str(entry.get('organization', '')))}")
    for entry in resume_data.get("project_experience") or []:
        if isinstance(entry, dict):
            out.append(_norm(str(entry.get("title", ""))))
    return [e for e in out if e and e != "@"]


def shingles(texts: list[str], n: int = 4) -> set[str]:
    """Word n-grams across all bullets.

    Shingles rather than whole-bullet equality because the interesting failure
    is a bullet lightly reworded between two jobs — "Built an AI-powered system
    automating job discovery" vs "Built an AI-powered platform automating job
    discovery" shares every 4-gram but no exact bullet.
    """
    out: set[str] = set()
    for text in texts:
        words = _norm(text).split()
        if len(words) < n:
            if words:
                out.add(" ".join(words))
            continue
        for i in range(len(words) - n + 1):
            out.add(" ".join(words[i:i + n]))
    return out


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


# ---------------------------------------------------------------------------
# fingerprints
# ---------------------------------------------------------------------------

@dataclass
class Fingerprint:
    """Everything needed to compare one résumé, without keeping its full text."""
    name: str                       # filename stem, e.g. "Gusto_AI_Engineer"
    shingles: set[str] = field(default_factory=set)
    bullets: set[str] = field(default_factory=set)   # normalised, for verbatim overlap
    entries: set[str] = field(default_factory=set)
    jd_terms: set[str] = field(default_factory=set)  # for the similar-JD exemption

    def as_json(self) -> dict:
        return {
            "name": self.name,
            "shingles": sorted(self.shingles),
            "bullets": sorted(self.bullets),
            "entries": sorted(self.entries),
            "jd_terms": sorted(self.jd_terms),
        }

    @staticmethod
    def from_json(d: dict) -> "Fingerprint":
        return Fingerprint(
            name=d.get("name", ""),
            shingles=set(d.get("shingles") or []),
            bullets=set(d.get("bullets") or []),
            entries=set(d.get("entries") or []),
            jd_terms=set(d.get("jd_terms") or []),
        )


def fingerprint(resume_data: dict, *, name: str = "", shingle_n: int = 4,
                jd_terms: set[str] | None = None) -> Fingerprint:
    bl = bullets_of(resume_data)
    return Fingerprint(
        name=name,
        shingles=shingles(bl, shingle_n),
        bullets={_norm(b) for b in bl if _norm(b)},
        entries=set(entries_of(resume_data)),
        jd_terms=set(jd_terms or ()),
    )


# ---------------------------------------------------------------------------
# corpus
# ---------------------------------------------------------------------------

def _resumes_dir(config: dict) -> Path:
    root = Path(__file__).resolve().parents[1]
    return root / (config.get("output", {}) or {}).get("resumes_dir", "output/resumes")


def load_clone_corpus(config: dict, exclude_base: str = "", limit: int = 40,
                      shingle_n: int = 4) -> list[Fingerprint]:
    """Fingerprints of the newest `limit` sidecar JSONs, excluding this job's own.

    A re-run of the SAME job must never be compared against its own previous
    output — that would flag every legitimate regeneration as a clone.

    Fingerprints are cached in output/resumes/.fingerprints.json keyed by
    (filename, mtime, ENGINE_VERSION). A corrupt or unreadable cache is a cache
    miss, never an error: this is an advisory signal and must not be able to
    break tailoring.
    """
    d = _resumes_dir(config)
    if not d.is_dir():
        return []
    exclude = f"{exclude_base}_resume.json" if exclude_base else None

    try:
        files = sorted(
            (p for p in d.glob("*_resume.json") if p.name != exclude),
            key=lambda p: p.stat().st_mtime, reverse=True)[:max(0, limit)]
    except OSError:
        return []

    cache_path = d / _CACHE_NAME
    cache: dict = {}
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        if raw.get("engine") == ENGINE_VERSION:
            cache = raw.get("entries") or {}
    except Exception:
        cache = {}

    out: list[Fingerprint] = []
    dirty = False
    for p in files:
        try:
            key = f"{p.name}:{int(p.stat().st_mtime)}"
        except OSError:
            continue
        hit = cache.get(key)
        if hit:
            out.append(Fingerprint.from_json(hit))
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        fp = fingerprint(data, name=p.stem.removesuffix("_resume"), shingle_n=shingle_n)
        out.append(fp)
        cache[key] = fp.as_json()
        dirty = True

    if dirty:
        try:
            live = {f"{p.name}:{int(p.stat().st_mtime)}" for p in files}
            cache_path.write_text(json.dumps(
                {"engine": ENGINE_VERSION,
                 "entries": {k: v for k, v in cache.items() if k in live}},
                ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass          # cache is an optimisation; never fatal
    return out


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------

@dataclass
class CloneVerdict:
    flagged: bool = False
    nearest: str = ""
    similarity: float = 0.0
    verbatim_bullet_ratio: float = 0.0
    entry_overlap: float = 0.0
    jd_similarity: float = 0.0
    exempt: bool = False              # the two JDs are themselves near-identical
    shared_shingles: list[str] = field(default_factory=list)
    shared_entries: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "flagged": self.flagged, "nearest": self.nearest,
            "similarity": round(self.similarity, 3),
            "verbatim_bullet_ratio": round(self.verbatim_bullet_ratio, 3),
            "entry_overlap": round(self.entry_overlap, 3),
            "jd_similarity": round(self.jd_similarity, 3),
            "exempt": self.exempt,
            "shared_shingles": self.shared_shingles[:12],
            "shared_entries": self.shared_entries[:5],
        }

    @property
    def feedback(self) -> str:
        """Prompt block telling the model exactly what to re-word. Empty if clean."""
        if not self.flagged:
            return ""
        lines = [
            f"## THIS DRAFT IS NEARLY IDENTICAL TO ANOTHER JOB'S RÉSUMÉ "
            f"(similarity {self.similarity:.2f} vs {self.nearest})",
        ]
        if self.shared_shingles:
            lines.append("These exact phrases appear in both — rewrite them in THIS "
                         "job's vocabulary or drop them:")
            lines += [f'  - "{s}"' for s in self.shared_shingles[:8]]
        if self.shared_entries:
            lines.append("These entries were chosen for both jobs even though the two "
                         "job descriptions differ. Swap at least one for an entry that "
                         "covers a requirement the other job did not have:")
            lines += [f"  - {e}" for e in self.shared_entries[:4]]
        lines.append("Do NOT add anything new — re-emphasise different ATTESTED evidence.")
        return "\n".join(lines)


def _cfg(config: dict) -> dict:
    return ((config.get("tailor") or {}).get("clone_guard") or {})


def clone_check(resume_data: dict, corpus: list[Fingerprint], config: dict,
                *, jd_terms: set[str] | None = None, name: str = "") -> CloneVerdict:
    """Compare a draft against the corpus. Never raises; never blocks."""
    cfg = _cfg(config)
    if not cfg.get("enabled", True) or not corpus:
        return CloneVerdict()

    n = int(cfg.get("shingle_n", 4))
    max_jac = float(cfg.get("max_shingle_jaccard", 0.60))
    max_verb = float(cfg.get("max_verbatim_bullet_ratio", 0.50))
    jd_exempt = float(cfg.get("jd_similarity_exempt", 0.70))

    me = fingerprint(resume_data, name=name, shingle_n=n, jd_terms=jd_terms)
    if not me.shingles:
        return CloneVerdict()

    verdict = CloneVerdict()
    for other in corpus:
        sim = jaccard(me.shingles, other.shingles)
        if sim <= verdict.similarity:
            continue
        verdict.similarity = sim
        verdict.nearest = other.name
        verdict.verbatim_bullet_ratio = (
            len(me.bullets & other.bullets) / len(me.bullets) if me.bullets else 0.0)
        verdict.entry_overlap = jaccard(me.entries, other.entries)
        verdict.jd_similarity = jaccard(me.jd_terms, other.jd_terms)
        shared = sorted(me.shingles & other.shingles, key=len, reverse=True)
        verdict.shared_shingles = shared[:12]
        verdict.shared_entries = sorted(me.entries & other.entries)

    # Two genuinely similar postings are ALLOWED to produce similar résumés.
    # Only exempt when we actually have JD terms on both sides to compare.
    if me.jd_terms and verdict.jd_similarity >= jd_exempt:
        verdict.exempt = True
        return verdict

    verdict.flagged = (verdict.similarity > max_jac
                       or verdict.verbatim_bullet_ratio > max_verb)
    if verdict.flagged:
        logger.warning(
            f"[clone_guard] draft is {verdict.similarity:.2f} similar to "
            f"{verdict.nearest} (verbatim bullets {verdict.verbatim_bullet_ratio:.0%}) "
            f"— requesting re-emphasis")
    return verdict
