"""Tests for the deterministic JD-coverage engine.

Every test loads a committed fixture résumé written to tmp_path rather than
`config/base_resume.yaml`: the real file is gitignored, so a test that read it
would pass here and fail on a clean checkout, and it would also make results
depend on whatever the candidate last edited.

The assertions below encode the framing decisions, not just the code:
presence-not-frequency, contiguous phrases, directional aliasing, and the rule
that a skills grid is a claim while a bullet is evidence.
"""

from __future__ import annotations

import textwrap
import types

import pytest

from agents import ats_engine as ats


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

BASE_RESUME_YAML = textwrap.dedent("""\
    contact:
      name: "Test Candidate"
      email: "candidate@example.com"
      location: "Springfield, IL"

    education:
      - institution: "State University"
        degree: "B.S. in Computer Science"
        location: "Springfield, IL"
        date: "2019"
        coursework:
          - "Algorithms"
          - "Databases"
          - "Machine Learning"

    work_experience:
      - title: "Full-Stack Developer"
        organization: "Carepool"
        location: "Remote"
        dates: "Jan 2024 - Jan 2025"
        bullets:
          - "Engineered backend services and REST APIs using Laravel and MySQL."
          - "Optimized database queries and resolved production issues."
      - title: "Software Engineer"
        organization: "Northwind Logistics"
        location: "Chicago, IL"
        dates: "Mar 2021 - Jun 2022"
        bullets:
          - "Built Python data pipelines that processed 40,000 shipment records daily."
          - "Automated regression checks with Playwright across three web applications."

    project_experience:
      - title: "JobPilot"
        tech_stack: "Python, FastAPI, SQLAlchemy, Ollama, Playwright"
        bullets:
          - "Built a local RAG pipeline over job postings using Ollama for inference."
          - "Shipped a FastAPI dashboard that tracks 500+ applications end to end."
      - title: "Transcript Reader"
        tech_stack: "Python, PyTorch"
        bullets:
          - "Applied Natural Language Processing to parse 1,200 academic transcripts."

    skills:
      Languages:
        - "Python"
        - "PHP"
        - "JavaScript"
        - "SQL"
      "AI / ML":
        - "Machine Learning"
        - "RAG"
        - "Natural Language Processing"
        - "Prompt Engineering"
      Tools:
        - "Docker"
        - "Playwright"
        - "FastAPI"
        - "Laravel"
        - "MySQL"
        - "Ollama"

    certifications:
      - name: "AWS Certified Cloud Practitioner"
        year: "2023"
    """)


@pytest.fixture(scope="module")
def base_path(tmp_path_factory) -> str:
    p = tmp_path_factory.mktemp("ats") / "base_resume_fixture.yaml"
    p.write_text(BASE_RESUME_YAML, encoding="utf-8")
    return str(p)


@pytest.fixture(scope="module")
def base(base_path) -> dict:
    return ats.load_base_resume(base_path)


class FakeJob:
    """Duck-typed stand-in for db.models.Job — analyze_jd only reads attributes."""

    def __init__(self, title="", company="", description=""):
        self.title = title
        self.company = company
        self.description = description


def _kw(term: str, weight: float = 1.0, count: int = 1,
        section: str = "requirements") -> ats.Keyword:
    return ats.Keyword(term=term, display=term, weight=weight, count=count,
                       section=section, n=term.count(" ") + 1, is_technical=True)


def _req(rid: str, terms, *, kind: str = "must", section: str = "requirements",
         weight: float = 1.0, text: str | None = None) -> ats.Requirement:
    return ats.Requirement(id=rid, text=text or f"Requirement {rid}", kind=kind,
                           section=section, weight=weight, terms=tuple(terms))


def _analysis(*, title: str = "", keywords=(), requirements=()) -> ats.JDAnalysis:
    return ats.JDAnalysis(job_title=title, company="Acme",
                          sections={"unknown": ""},
                          requirements=list(requirements),
                          keywords=list(keywords), hints=[], jd_hash="0" * 40)


def _resume(bullets=(), skills=None, titles=("Engineer",)):
    return {
        "work_experience": [{
            "title": titles[0], "organization": "Acme", "location": "Remote",
            "dates": "Jan 2024 - Present", "bullets": list(bullets),
        }],
        "skills": skills or {},
    }


JD_SECTIONED = textwrap.dedent("""\
    Requirements:
    - Must have professional Python experience shipping production services.
    - Proven experience designing REST APIs and relational database schemas.
    - Build Machine Learning systems that rank results for customers.

    Nice to have:
    - Familiarity with Kubernetes is a plus.
    - Exposure to Rust would be great.

    Benefits:
    - Competitive salary range, 401(k) match and paid time off.
    - We offer a generous benefits package and a remote work stipend.
    """)


# --------------------------------------------------------------------------
# 1.3  Text primitives
# --------------------------------------------------------------------------

def test_normalize_preserves_technical_punctuation():
    out = ats.normalize("C++ and C# with Node.js, scikit-learn and CI/CD")
    for term in ("c++", "c#", "node.js", "scikit-learn", "ci/cd"):
        assert term in out.split(), f"{term!r} did not survive normalize(): {out!r}"


def test_normalize_folds_unicode_and_is_idempotent():
    raw = "Palantir’s “mixed—reality” team – hiring"
    once = ats.normalize(raw)
    assert once == ats.normalize(once)
    assert "’" not in once and "—" not in once
    # A curly apostrophe must not split a possessive into two tokens.
    assert "palantirs" in once.split()


def test_normalize_strips_sentence_punctuation_from_word_edges():
    assert ats.normalize("things go awry. Node.js rocks!") == "things go awry node.js rocks"


def test_tokens_keeps_compound_whole_and_also_splits_it():
    toks = ats.tokens("scikit-learn for cross-functional teams")
    assert "scikit-learn" in toks          # survives whole
    assert "cross" in toks and "functional" in toks   # also yields its parts


def test_content_tokens_drop_stopwords_and_naively_depluralise():
    out = ats.content_tokens("The engineers built the pipelines and the analysis")
    assert "the" not in out and "and" not in out
    assert "engineer" in out and "pipeline" in out
    assert "analysis" in out               # -is words are never stripped


def test_ngrams_discard_stopword_boundaries():
    grams = {g for g, _i in ats.ngrams(ats.tokens("machine learning of the data"), 3)}
    assert "machine learning" in grams
    assert not any(g.split()[0] in ats.STOPWORDS or g.split()[-1] in ats.STOPWORDS
                   for g in grams)


def test_contains_phrase_requires_contiguity():
    hay = ats.normalize("Applied machine translation and deep learning models")
    assert ats.contains_phrase(hay, "deep learning")
    assert not ats.contains_phrase(hay, "machine learning")
    # hyphenation must not decide the outcome either way
    assert ats.contains_phrase(ats.normalize("cross-functional work"), "cross functional")
    assert ats.contains_phrase(ats.normalize("cross functional work"), "cross-functional")


# --------------------------------------------------------------------------
# 1.4  Section splitting
# --------------------------------------------------------------------------

def test_split_jd_sections_finds_requirements_and_nice_to_have():
    sections = ats.split_jd_sections(JD_SECTIONED)
    assert set(sections) == {"requirements", "nice_to_have", "benefits"}
    assert "Python" in sections["requirements"]
    assert "Kubernetes" in sections["nice_to_have"]


def test_split_jd_sections_headingless_jd_is_unknown():
    jd = "We build software for logistics companies and want a strong engineer."
    assert ats.split_jd_sections(jd) == {"unknown": jd}


def test_split_jd_sections_empty_jd_never_raises():
    assert ats.split_jd_sections("") == {"unknown": ""}
    assert ats.split_jd_sections(None) == {"unknown": ""}


def test_a_bullet_starting_must_have_is_not_a_heading():
    """`must[- ]haves?` is a heading cue; "Must have 3+ years..." is a bullet."""
    sections = ats.split_jd_sections(
        "Requirements:\n- Must have professional Python experience today.\n")
    assert "Python" in sections["requirements"]


# --------------------------------------------------------------------------
# 1.6  Keyword extraction
# --------------------------------------------------------------------------

def test_section_weighting_ranks_requirements_above_nice_to_have(base_path):
    kws = {k.term: k for k in ats.extract_keywords(JD_SECTIONED, base_resume_path=base_path)}
    assert kws["python"].section == "requirements"
    assert kws["kubernetes"].section == "nice_to_have"
    assert kws["python"].weight > kws["kubernetes"].weight
    ratio = kws["python"].weight / kws["kubernetes"].weight
    assert ratio == pytest.approx(
        ats.SECTION_WEIGHTS["requirements"] / ats.SECTION_WEIGHTS["nice_to_have"],
        rel=1e-4)


def test_keyword_frequency_saturates(base_path):
    def kube_jd(n: int) -> str:
        lines = [f"- Deploy production services with Kubernetes in region {i}."
                 for i in range(n)]
        return "Requirements:\n" + "\n".join(lines) + "\n"

    def weight(n: int) -> float:
        kws = {k.term: k for k in ats.extract_keywords(kube_jd(n), top_n=200,
                                                        base_resume_path=base_path)}
        assert kws["kubernetes"].count == n
        return kws["kubernetes"].weight

    w1, w3, w30 = weight(1), weight(3), weight(30)
    assert w1 < w3, "a term seen three times must beat a term seen once"
    assert w3 == w30, "repetition past 3 occurrences must add nothing (BM25 k1 cap)"


def test_keyword_phrase_beats_its_unigram(base_path):
    phrase_jd = "Requirements:\n- Build Machine Learning systems for ranking.\n"
    unigram_jd = "Requirements:\n- We invest in Learning and mentoring for engineers.\n"

    phrase = {k.term: k for k in ats.extract_keywords(phrase_jd, top_n=200,
                                                      base_resume_path=base_path)}
    unigram = {k.term: k for k in ats.extract_keywords(unigram_jd, top_n=200,
                                                       base_resume_path=base_path)}
    assert phrase["machine learning"].count == unigram["learning"].count == 1
    assert phrase["machine learning"].weight > unigram["learning"].weight

    # ...and in the phrase's own posting the unigram is pruned away entirely.
    assert "learning" not in phrase
    assert "machine" not in phrase


def test_low_signal_unigrams_rejected(base_path):
    jd = textwrap.dedent("""\
        Requirements:
        - Experience with Kubernetes on a fast paced team is required.
        - You are passionate about clean code and enjoy cross functional work.
        - Proven experience designing REST APIs for a growing business.

        Nice to have:
        - Exposure to Rust would be great for this role.
        """)
    terms = {k.term for k in ats.extract_keywords(jd, top_n=40, base_resume_path=base_path)}
    for junk in ("team", "work", "experience", "passionate", "role", "business"):
        assert junk not in terms, f"{junk!r} is generic HR vocabulary, not a JD term"
    assert "kubernetes" in terms
    assert terms & {"rest api", "rest apis"}, "real technical terms must survive"


def test_boilerplate_is_never_a_keyword(base_path):
    jd = textwrap.dedent("""\
        Requirements:
        - We are an equal opportunity employer and consider applicants regardless of race.
        - The compensation range for this position is competitive with market rates.
        """)
    terms = {k.term for k in ats.extract_keywords(jd, top_n=200, base_resume_path=base_path)}
    for phrase in ("equal opportunity", "compensation range", "regardless of race"):
        assert phrase not in terms


def test_seed_terms_are_advisory_not_authoritative(base_path):
    kws = ats.extract_keywords(JD_SECTIONED, top_n=200,
                               seed_terms=["Snowflake", "python"],
                               base_resume_path=base_path)
    by_term = {k.term: k for k in kws}
    assert by_term["snowflake"].count == 0
    assert by_term["snowflake"].section == "unknown"
    # A seed the JD never used must not outrank a term the JD actually wrote.
    assert by_term["snowflake"].weight < by_term["python"].weight
    # A seed already present keeps its earned weight rather than being doubled.
    plain = {k.term: k for k in ats.extract_keywords(JD_SECTIONED, top_n=200,
                                                     base_resume_path=base_path)}
    assert by_term["python"].weight == plain["python"].weight


def test_extract_keywords_is_deterministic(base_path):
    runs = [ats.extract_keywords(JD_SECTIONED, base_resume_path=base_path)
            for _ in range(5)]
    assert all(r == runs[0] for r in runs)


# --------------------------------------------------------------------------
# 1.5  Requirement extraction
# --------------------------------------------------------------------------

MODALITY_JD = textwrap.dedent("""\
    Requirements:
    - Design and ship backend services in Python.
    - Experience with Kubernetes is preferred.
    - Must have 3+ years of Python and SQL.

    Preferred:
    - Work with Terraform across cloud environments.
    - Familiarity with Rust is a plus.
    """)


def _find(reqs, needle):
    for r in reqs:
        if needle.lower() in r.text.lower():
            return r
    raise AssertionError(f"no requirement containing {needle!r} in "
                         f"{[r.text for r in reqs]}")


def test_requirement_modality(base_path):
    reqs = ats.extract_requirements(MODALITY_JD, base_resume_path=base_path)

    assert _find(reqs, "Must have 3+ years").kind == "must"          # explicit MUST cue
    assert _find(reqs, "Familiarity with Rust").kind == "nice"       # explicit NICE cue
    assert _find(reqs, "Design and ship backend").kind == "must"     # section default
    assert _find(reqs, "Work with Terraform").kind == "nice"         # Preferred: default
    # An in-text NICE cue beats the section default: over-calling a must-have
    # inflates the denominator and makes an honest résumé look worse.
    assert _find(reqs, "Kubernetes is preferred").kind == "nice"


def test_requirement_weight_is_section_times_modality(base_path):
    reqs = ats.extract_requirements(MODALITY_JD, base_resume_path=base_path)
    must = _find(reqs, "Must have 3+ years")
    nice = _find(reqs, "Familiarity with Rust")
    assert must.weight == pytest.approx(
        ats.SECTION_WEIGHTS["requirements"] * ats.MUST_WEIGHT)
    assert nice.weight == pytest.approx(
        ats.SECTION_WEIGHTS["nice_to_have"] * ats.NICE_WEIGHT)
    assert must.weight > nice.weight


def test_boilerplate_never_becomes_a_requirement(base_path):
    jd = textwrap.dedent("""\
        Requirements:
        We are an equal opportunity employer and all qualified applicants will
        receive consideration for employment regardless of race, colour or religion.
        All offers of employment are contingent upon a successful background check.
        The compensation range is competitive and the benefits package includes 401(k).
        """)
    assert ats.extract_requirements(jd, base_resume_path=base_path) == []


def test_requirements_are_ranked_and_capped(base_path):
    reqs = ats.extract_requirements(JD_SECTIONED, max_requirements=2,
                                    base_resume_path=base_path)
    assert [r.id for r in reqs] == ["R1", "R2"]
    assert reqs[0].weight >= reqs[1].weight
    assert all(r.terms for r in reqs), "a requirement with no terms is prose"


def test_requirement_terms_are_distinct_concepts(base_path):
    """Five overlapping windows of one noun phrase would make `need` unmatchable."""
    req = _find(ats.extract_requirements(MODALITY_JD, base_resume_path=base_path),
                "Must have 3+ years")
    seen: set[str] = set()
    for term in req.terms:
        parts = set(term.split())
        assert not (parts & seen), f"{req.terms} overlap on {parts & seen}"
        seen |= parts
    assert "python" in req.terms or "sql" in req.terms


# --------------------------------------------------------------------------
# 1.2  Alias direction
# --------------------------------------------------------------------------

def test_alias_expansion_is_directional(base_path):
    attested = ats.attested_vocabulary(base_path)

    # The fixture résumé says MySQL and never says PostgreSQL.
    assert "mysql" in attested
    assert "sql" in attested, "MySQL truthfully attests SQL"
    assert "postgresql" not in attested, "MySQL must never attest PostgreSQL"

    analysis_sql = _analysis(keywords=[_kw("sql")])
    covered = ats.coverage(analysis_sql,
                           _resume(bullets=["Optimized MySQL queries for reporting."]),
                           base={})
    assert [k.term for k in covered.keywords.covered] == ["sql"]

    analysis_mysql = _analysis(keywords=[_kw("mysql")])
    not_covered = ats.coverage(analysis_mysql,
                               _resume(bullets=["Wrote SQL reports for finance."]),
                               base={})
    assert [k.term for k in not_covered.keywords.missing] == ["mysql"]


def test_symmetric_aliases_attest_each_other():
    long_form = ats.coverage(_analysis(keywords=[_kw("large language model")]),
                             _resume(bullets=["Shipped an LLM assistant for support."]),
                             base={})
    assert long_form.keywords.covered, "llm must attest large language model"

    short_form = ats.coverage(_analysis(keywords=[_kw("llm")]),
                              _resume(bullets=["Tuned a large language model in house."]),
                              base={})
    assert short_form.keywords.covered, "large language model must attest llm"


def test_no_alias_group_is_also_a_one_way_pair():
    """A term cannot be both a synonym and a specialisation of the same term."""
    for src, implied in ats.TERM_IMPLIES.items():
        group = ats.ALIAS_TABLE.get(src, frozenset({src}))
        assert not (group & implied), (
            f"{src!r} both aliases and implies {sorted(group & implied)}")


# --------------------------------------------------------------------------
# 1.7  Acronym pairing
# --------------------------------------------------------------------------

def test_expansion_hint_only_for_attested_pairs(base_path):
    attested = ats.attested_vocabulary(base_path)
    hints = ats.expansion_hints([_kw("nlp"), _kw("ocr")], attested)
    by_form = {h.jd_form: h for h in hints}

    assert by_form["nlp"].recommended == "natural language processing (NLP)"
    assert by_form["nlp"].other_form == "natural language processing"
    # The fixture résumé has no OCR anywhere, so recommending it would be an
    # instruction to fabricate.
    assert "ocr" not in by_form


def test_expansion_hints_emit_one_hint_per_pair(base_path):
    attested = ats.attested_vocabulary(base_path)
    hints = ats.expansion_hints(
        [_kw("nlp"), _kw("natural language processing")], attested)
    assert len(hints) == 1


# --------------------------------------------------------------------------
# 1.8  Coverage
# --------------------------------------------------------------------------

def test_coverage_counts_each_term_once_and_flags_stuffing():
    analysis = _analysis(keywords=[_kw("python"), _kw("rest api")])
    once = _resume(bullets=["Built Python REST API services for payments."])
    many = _resume(bullets=["Built Python REST API services for payments."]
                   + [f"Refactored Python helper module {i}." for i in range(7)])

    cov_once = ats.coverage(analysis, once, base={})
    cov_many = ats.coverage(analysis, many, base={})

    assert cov_once.keywords.fraction == cov_many.keywords.fraction == 1.0
    assert cov_once.keywords.covered_weight == cov_many.keywords.covered_weight
    assert cov_once.keywords.overused == []
    assert cov_many.keywords.overused == [("python", 8)]


def test_coverage_fraction_is_weighted_not_counted():
    analysis = _analysis(keywords=[_kw("python", weight=3.0), _kw("rust", weight=1.0)])
    cov = ats.coverage(analysis, _resume(bullets=["Wrote Python services."]), base={})
    assert cov.keywords.fraction == pytest.approx(0.75)


def test_coverage_requires_bullet_context():
    """Naming a skill in a grid is a claim; a bullet is evidence."""
    analysis = _analysis(requirements=[_req("R1", ("kubernetes", "terraform"))])

    skills_only = {
        "work_experience": [{"title": "Engineer", "organization": "Acme",
                             "dates": "Jan 2024 - Present",
                             "bullets": ["Wrote onboarding documentation."]}],
        "skills": {"Infrastructure": "Kubernetes, Terraform"},
    }
    in_bullets = {
        "work_experience": [{"title": "Engineer", "organization": "Acme",
                             "dates": "Jan 2024 - Present",
                             "bullets": ["Operated Kubernetes clusters and "
                                         "Terraform modules in production."]}],
        "skills": {"Infrastructure": "Kubernetes, Terraform"},
    }

    partial = ats.coverage(analysis, skills_only, base={}).requirements[0]
    assert partial.status == "partial"
    assert set(partial.matched_terms) == {"kubernetes", "terraform"}

    covered = ats.coverage(analysis, in_bullets, base={}).requirements[0]
    assert covered.status == "covered"
    assert any(m.startswith("bullet:") for m in covered.matched_in)


def test_requirement_missing_when_nothing_matches():
    analysis = _analysis(requirements=[_req("R1", ("kubernetes",))])
    rc = ats.coverage(analysis, _resume(bullets=["Wrote Python services."]),
                      base={}).requirements[0]
    assert rc.status == "missing" and rc.matched_terms == []


def test_requirement_fraction_gives_partial_half_credit():
    analysis = _analysis(requirements=[
        _req("R1", ("python",), weight=1.0),        # covered in a bullet
        _req("R2", ("kubernetes",), weight=1.0),    # skills only -> partial
        _req("R3", ("rust",), weight=1.0),          # missing
    ])
    resume = {
        "work_experience": [{"title": "Engineer", "organization": "Acme",
                             "dates": "Jan 2024 - Present",
                             "bullets": ["Built Python services."]}],
        "skills": {"Infrastructure": "Kubernetes"},
    }
    cov = ats.coverage(analysis, resume, base={})
    assert [r.status for r in cov.requirements] == ["covered", "partial", "missing"]
    assert cov.requirement_fraction == pytest.approx((1.0 + 0.5) / 3.0)


def test_phrase_integrity():
    """A phrase split over two lines is not a phrase hit — it is a break."""
    analysis = _analysis(keywords=[_kw("machine learning")])
    split = _resume(bullets=["Applied machine", "learning to ranking models."])
    whole = _resume(bullets=["Applied machine learning to ranking models."])

    broken = ats.coverage(analysis, split, base={})
    assert broken.keywords.covered == []
    assert broken.keywords.phrase_breaks == ["machine learning"]
    assert broken.keywords.fraction == 0.0

    intact = ats.coverage(analysis, whole, base={})
    assert [k.term for k in intact.keywords.covered] == ["machine learning"]
    assert intact.keywords.phrase_breaks == []


def test_title_alignment_is_literal_token_overlap():
    resume = {"work_experience": [
        {"title": "Full Stack Developer", "organization": "Acme",
         "dates": "Jan 2024 - Present", "bullets": ["Shipped features."]}]}
    exact = ats.coverage(_analysis(title="Full Stack Developer"), resume, base={})
    assert exact.title_alignment == pytest.approx(1.0)

    partial = ats.coverage(_analysis(title="Senior Backend Developer"), resume, base={})
    assert partial.title_alignment == pytest.approx(1 / 3)

    assert ats.coverage(_analysis(title=""), resume, base={}).title_alignment == 0.0


def test_quantified_fraction_rewards_front_loaded_numbers():
    analysis = _analysis()
    plain = _resume(bullets=["Improved reliability of the ingest pipeline."])
    assert ats.coverage(analysis, plain, base={}).quantified_fraction == 0.0

    front = _resume(bullets=["Cut p99 latency 40% across the ingest pipeline."])
    cov = ats.coverage(analysis, front, base={})
    assert cov.quantified_fraction == pytest.approx(1.0)   # 1 quantified + front-load, clamped


def test_keyword_wall_detects_a_skills_heavy_resume():
    wall = {
        "work_experience": [{"title": "Engineer", "organization": "Acme",
                             "dates": "Jan 2024 - Present",
                             "bullets": ["Shipped features."]}],
        "skills": {"Everything": ", ".join(f"tool{i}" for i in range(60))},
    }
    balanced = {
        "work_experience": [{"title": "Engineer", "organization": "Acme",
                             "dates": "Jan 2024 - Present",
                             "bullets": ["Built Python services that processed "
                                         "40,000 shipment records every day for "
                                         "three regional logistics customers."] * 4}],
        "skills": {"Languages": "Python, SQL"},
    }
    assert ats.coverage(_analysis(), wall, base={}).keyword_wall is True
    assert ats.coverage(_analysis(), balanced, base={}).keyword_wall is False


def test_flags_report_gaps_and_unparseable_dates(base):
    resume = {"work_experience": [
        {"title": "Engineer", "organization": "Acme", "dates": "Summer",
         "bullets": ["Shipped features."]}]}
    flags = ats.coverage(_analysis(), resume, base=base).flags
    assert "docx:no_tables" in flags and "docx:standard_headings" in flags
    assert any(f.startswith("dates:unparseable:Acme") for f in flags)
    # Northwind ends Jun 2022, Carepool starts Jan 2024 -> a 19-month gap.
    assert any(f.startswith("gap:>6mo") and "Northwind Logistics" in f for f in flags)


def test_coverage_never_crashes_on_an_empty_resume():
    cov = ats.coverage(_analysis(keywords=[_kw("python")]), None, base={})
    assert cov.keywords.fraction == 0.0
    assert cov.requirement_fraction == 0.0
    assert cov.quantified_fraction == 0.0


def test_coverage_accepts_plain_text_for_the_legacy_call():
    cov = ats.coverage(_analysis(keywords=[_kw("python")]),
                       "EXPERIENCE\nBuilt Python services for payments.", base={})
    assert [k.term for k in cov.keywords.covered] == ["python"]


# --------------------------------------------------------------------------
# 1.9  analyze_jd
# --------------------------------------------------------------------------

def test_analyze_jd_end_to_end(base_path):
    job = FakeJob("Machine Learning Engineer", "Acme", JD_SECTIONED)
    analysis = ats.analyze_jd(job, base_resume_path=base_path)
    assert analysis.job_title == "Machine Learning Engineer"
    assert set(analysis.sections) == {"requirements", "nice_to_have", "benefits"}
    assert analysis.requirements and analysis.keywords
    assert len(analysis.jd_hash) == 40
    assert all(r.id == f"R{i}" for i, r in enumerate(analysis.requirements, start=1))


def test_analyze_jd_with_empty_description_falls_back_to_title(base_path, monkeypatch):
    def boom(job):
        raise AssertionError("no LLM call is permitted when use_llm=False")

    monkeypatch.setattr(ats, "_llm_requirement_seed", boom)

    job = FakeJob("Senior Geospatial Data Engineer", "Acme", "")
    analysis = ats.analyze_jd(job, use_llm=False, base_resume_path=base_path)

    assert analysis.requirements, "a title-only posting must still yield requirements"
    assert all(r.section == "unknown" for r in analysis.requirements)
    assert all(r.kind == "must" for r in analysis.requirements)
    assert "geospatial" in analysis.requirements[0].terms


def test_analyze_jd_degrades_on_a_20_char_description(base_path, monkeypatch):
    monkeypatch.setattr(ats, "_llm_requirement_seed",
                        lambda job: (_ for _ in ()).throw(AssertionError("no LLM")))
    job = FakeJob("Data Center Technician", "Cologix", "Come work with us!")
    analysis = ats.analyze_jd(job, use_llm=False, base_resume_path=base_path)
    assert analysis.requirements
    assert all(r.section == "unknown" for r in analysis.requirements)


def test_analyze_jd_tolerates_a_null_description(base_path):
    job = FakeJob("Backend Engineer", "Acme", None)
    analysis = ats.analyze_jd(job, base_resume_path=base_path)
    assert analysis.sections == {"unknown": ""}
    assert analysis.requirements and analysis.keywords == []


def test_llm_seed_is_only_reached_for_a_title_only_posting(base_path, monkeypatch):
    """The optional assist may fire on a title-only JD, never on a real one."""
    calls: list[str] = []
    monkeypatch.setattr(ats, "_llm_requirement_seed",
                        lambda job: calls.append(job.title) or ["Playwright"])

    ats.analyze_jd(FakeJob("QA Engineer", "Acme", JD_SECTIONED), use_llm=True,
                   base_resume_path=base_path)
    assert calls == [], "a full posting must be analysed without the model"

    analysis = ats.analyze_jd(FakeJob("QA Engineer", "Acme", ""), use_llm=True,
                              base_resume_path=base_path)
    assert calls == ["QA Engineer"]
    # ...and the seed is re-verified by the deterministic path, not trusted.
    assert "playwright" in {t for r in analysis.requirements for t in r.terms}


def test_llm_seed_failure_leaves_the_title_only_path_intact(base_path, monkeypatch):
    import utils.ollama_client as oc

    def boom(*a, **k):
        raise ConnectionError("ollama down")

    monkeypatch.setattr(oc, "generate_json", boom)
    analysis = ats.analyze_jd(FakeJob("QA Engineer", "Acme", ""), use_llm=True,
                              base_resume_path=base_path)
    assert analysis.requirements, "a dead model must not empty the analysis"


def test_analyze_jd_merges_ranker_seeds_into_the_title_fallback(base_path):
    score = types.SimpleNamespace(ats_keywords=["Playwright", "Docker"])
    analysis = ats.analyze_jd(FakeJob("QA Engineer", "Acme", ""), score,
                              use_llm=False, base_resume_path=base_path)
    all_terms = {t for r in analysis.requirements for t in r.terms}
    assert {"playwright", "docker"} <= all_terms


def test_analyze_jd_is_deterministic(base_path):
    job = FakeJob("Machine Learning Engineer", "Acme", JD_SECTIONED)
    first = ats.analyze_jd(job, base_resume_path=base_path)
    second = ats.analyze_jd(job, base_resume_path=base_path)
    assert first == second
    assert first.jd_hash == second.jd_hash
    assert [k.term for k in first.keywords] == [k.term for k in second.keywords]


def test_coverage_is_deterministic(base_path):
    job = FakeJob("Machine Learning Engineer", "Acme", JD_SECTIONED)
    analysis = ats.analyze_jd(job, base_resume_path=base_path)
    resume = _resume(bullets=["Built Machine Learning ranking services in Python.",
                              "Designed REST APIs backed by MySQL."],
                     skills={"Languages": "Python, SQL"})
    reports = [ats.coverage(analysis, resume, base={}) for _ in range(10)]
    first = reports[0]
    for r in reports[1:]:
        assert r.keywords.fraction == first.keywords.fraction
        assert [k.term for k in r.keywords.covered] == \
               [k.term for k in first.keywords.covered]
        assert [(rc.requirement.id, rc.status) for rc in r.requirements] == \
               [(rc.requirement.id, rc.status) for rc in first.requirements]
        assert r.flags == first.flags


# --------------------------------------------------------------------------
# 1.10  Entry ranking and selection
# --------------------------------------------------------------------------

SELECTION_ENTRIES = [
    {"title": "Backend Engineer", "organization": "A", "dates": "Jan 2024 - Present",
     "bullets": ["Built REST APIs with Laravel and MySQL for customer integrations."]},
    {"title": "ML Engineer", "organization": "B", "dates": "Jan 2023 - Dec 2023",
     "bullets": ["Trained computer vision models on satellite imagery."]},
    {"title": "Data Engineer", "organization": "C", "dates": "Jan 2022 - Dec 2022",
     "bullets": ["Built geospatial pipelines over GIS datasets."]},
    {"title": "Solutions Engineer", "organization": "D", "dates": "Jan 2021 - Dec 2021",
     "bullets": ["Ran customer integrations and SQL debugging sessions."]},
    {"title": "Automation Engineer", "organization": "E", "dates": "Jan 2020 - Dec 2020",
     "bullets": ["Automated browser flows with Playwright nightly."]},
    {"title": "Support Engineer", "organization": "F", "dates": "Jan 2019 - Dec 2019",
     "bullets": ["Handled escalations and wrote onboarding documentation."]},
]

SOLUTIONS_ANALYSIS = _analysis(
    title="Solutions Engineer",
    keywords=[_kw("rest api"), _kw("sql"), _kw("customer integrations")],
    requirements=[_req("R1", ("rest api",)), _req("R2", ("customer integrations",))])

VISION_ANALYSIS = _analysis(
    title="Computer Vision Engineer",
    keywords=[_kw("computer vision"), _kw("geospatial")],
    requirements=[_req("R1", ("computer vision",)), _req("R2", ("geospatial",))])


def test_score_entry_records_hits_and_bullet_scores():
    es = ats.score_entry(SELECTION_ENTRIES[0], "work_experience", "W1",
                         SOLUTIONS_ANALYSIS)
    assert es.key == "W1" and es.kind == "work_experience"
    assert "rest api" in es.keyword_hits
    assert "R1" in es.requirement_hits
    assert len(es.bullet_scores) == 1 and es.bullet_scores[0] > 0
    assert es.entry is SELECTION_ENTRIES[0], "the base entry must not be copied or edited"


def test_score_entries_applies_recency_to_work_only():
    ranked = ats.score_entries(SELECTION_ENTRIES, "work_experience", SOLUTIONS_ANALYSIS)
    flat = [ats.score_entry(e, "work_experience", f"W{i+1}", SOLUTIONS_ANALYSIS)
            for i, e in enumerate(SELECTION_ENTRIES)]
    assert ranked[0].raw == pytest.approx(round(flat[0].raw * 1.10, 6))
    assert ranked[3].raw == pytest.approx(flat[3].raw)   # neither of the top two


def test_select_entries_is_jd_dependent():
    a = ats.select_entries(
        ats.score_entries(SELECTION_ENTRIES, "work_experience", SOLUTIONS_ANALYSIS), 3)
    b = ats.select_entries(
        ats.score_entries(SELECTION_ENTRIES, "work_experience", VISION_ANALYSIS), 3)
    assert {e.key for e in a} != {e.key for e in b}
    assert {"W1", "W4"} <= {e.key for e in a}     # REST/SQL/customer integrations
    assert {"W2", "W3"} <= {e.key for e in b}     # computer vision / geospatial


def test_select_entries_is_deterministic():
    scores = ats.score_entries(SELECTION_ENTRIES, "work_experience", SOLUTIONS_ANALYSIS)
    runs = [[e.key for e in ats.select_entries(scores, 3)] for _ in range(20)]
    assert all(r == runs[0] for r in runs)


def test_select_entries_never_returns_fewer_than_available():
    scores = ats.score_entries(SELECTION_ENTRIES[:2], "work_experience", VISION_ANALYSIS)
    assert len(ats.select_entries(scores, 5)) == 2
    # An entry with raw == 0 is still eligible: an empty section is worse.
    zero = ats.score_entries([SELECTION_ENTRIES[5]], "work_experience", VISION_ANALYSIS)
    assert zero[0].raw == 0.0
    assert len(ats.select_entries(zero, 1)) == 1


def _covered_requirements(selected) -> set[str]:
    return {rid for es in selected for rid in es.requirement_hits}


def test_greedy_beats_top_k_on_coverage():
    words = ["alpha", "bravo", "charlie", "delta", "echo",
             "foxtrot", "golf", "hotel", "india"]
    reqs = [_req(f"R{i+1}", (w,)) for i, w in enumerate(words)]
    analysis = _analysis(requirements=reqs)

    def entry(name, covers):
        return {"title": name, "organization": name, "dates": "Jan 2020 - Dec 2020",
                "bullets": [" ".join(covers)]}

    entries = [
        entry("E1", ["alpha", "bravo", "charlie", "delta"]),
        entry("E2", ["alpha", "bravo", "charlie", "echo"]),
        entry("E3", ["alpha", "bravo", "charlie", "foxtrot"]),
        entry("E4", ["golf", "hotel", "india"]),
    ]
    scores = [ats.score_entry(e, "project_experience", f"P{i+1}", analysis)
              for i, e in enumerate(entries)]

    top_k = sorted(scores, key=lambda s: (-s.raw, s.key))[:3]
    greedy = ats.select_entries(scores, 3)

    n_top, n_greedy = len(_covered_requirements(top_k)), len(_covered_requirements(greedy))
    assert n_top == 6 and n_greedy == 8
    assert n_greedy >= n_top


def test_select_bullets_covers_distinct_requirements():
    analysis = _analysis(requirements=[_req("R1", ("alpha",)), _req("R2", ("bravo",))])
    entry = {"title": "E", "organization": "E", "dates": "2020",
             "bullets": ["alpha alpha alpha", "alpha again", "bravo once", "nothing here"]}
    es = ats.score_entry(entry, "project_experience", "P1", analysis)
    picked = ats.select_bullets(es, 2)
    assert picked == sorted(picked), "indices come back in résumé order"
    assert {"R1", "R2"} == {rid for i in picked for rid in es.bullet_requirement_hits[i]}


def test_select_bullets_returns_min_k_when_everything_scores_zero():
    analysis = _analysis(keywords=[_kw("zulu")])
    entry = {"title": "E", "bullets": ["nothing relevant", "also nothing"]}
    es = ats.score_entry(entry, "project_experience", "P1", analysis)
    assert es.bullet_scores == [0.0, 0.0]
    assert ats.select_bullets(es, 1) == [0]
    assert ats.select_bullets(es, 0, min_k=1) == [0]
    assert ats.select_bullets(
        ats.score_entry({"title": "E", "bullets": []}, "project_experience", "P1",
                        analysis), 3) == []


def test_swap_weakest_replaces_the_least_useful_entry():
    scores = ats.score_entries(SELECTION_ENTRIES, "work_experience", SOLUTIONS_ANALYSIS)
    selected = ats.select_entries(scores, 3)
    pool = [s for s in scores if s.key not in {e.key for e in selected}]

    swapped = ats.swap_weakest(selected, pool, SOLUTIONS_ANALYSIS)
    assert swapped is not None
    assert len(swapped) == len(selected)
    assert {e.key for e in swapped} != {e.key for e in selected}

    assert ats.swap_weakest(selected, [], SOLUTIONS_ANALYSIS) is None
    assert ats.swap_weakest([], pool, SOLUTIONS_ANALYSIS) is None


def test_swap_weakest_is_deterministic():
    scores = ats.score_entries(SELECTION_ENTRIES, "work_experience", SOLUTIONS_ANALYSIS)
    selected = ats.select_entries(scores, 3)
    pool = [s for s in scores if s.key not in {e.key for e in selected}]
    runs = [[e.key for e in ats.swap_weakest(selected, pool, SOLUTIONS_ANALYSIS)]
            for _ in range(10)]
    assert all(r == runs[0] for r in runs)


# --------------------------------------------------------------------------
# Framing guarantees
# --------------------------------------------------------------------------

def test_module_never_calls_the_llm_on_the_deterministic_path(base_path, monkeypatch):
    import utils.ollama_client as oc

    monkeypatch.setattr(oc, "generate_json", lambda *a, **k: pytest.fail(
        "the coverage engine must be pure — no LLM call on any default path"))

    job = FakeJob("Machine Learning Engineer", "Acme", JD_SECTIONED)
    analysis = ats.analyze_jd(job, base_resume_path=base_path)
    ats.coverage(analysis, _resume(bullets=["Built Python services."]), base={})
    ats.extract_requirements(JD_SECTIONED, base_resume_path=base_path)


def test_the_number_is_never_called_an_ats_score():
    """No ATS computes a résumé match percentage, so nothing here may claim one."""
    import dataclasses

    banned = {"ats_score", "match_score", "match_percentage", "ats_match"}
    for obj in vars(ats).values():
        if dataclasses.is_dataclass(obj):
            names = {f.name for f in dataclasses.fields(obj)}
            assert not (names & banned), f"{obj.__name__} exposes {names & banned}"
    assert "not an ats score" in (ats.coverage.__doc__ or "").lower()
    assert "jd coverage" in (ats.__doc__ or "").lower()


def test_hidden_text_is_declared_permanently_out_of_scope():
    doc = (ats.__doc__ or "").lower()
    assert "out of scope permanently" in doc
    assert "a human reader of the document cannot see" in doc
