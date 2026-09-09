"""Resume and cover letter tailoring engine using Gemma 4 via Ollama.

Output is one-page, Jake's-resume styled .docx — serif body, ALL CAPS bold
section headers with hairline rule beneath, tab-aligned two-column rows for
institution/location and degree/date, tight bullet spacing.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import yaml
from docx import Document
from docx.shared import Pt, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from db.database import get_session, record_status_change
from db.models import Job, JobScore, Application, ApplicationStatus
from agents.grounding import enforce_and_log
from utils.ollama_client import generate_json, generate_text

logger = logging.getLogger(__name__)


# ============================================================
# One-page Jake's-style hard caps applied at render time.
# Acts as a safety net even if the model over-generates.
#
# The candidate's real content lives ONLY in config/base_resume.yaml. Nothing
# in this module may name a concrete skill, employer, school or certification:
# a worked example carrying a previous candidate's data used to sit in
# RESUME_PROMPT_TEMPLATE below, and the model copied it into 158 of 197 shipped
# résumés (Pandas/NumPy/scikit-learn/Statsmodels, plus a Tableau and a CFA
# certification the candidate does not hold). Keep every example value in this
# file structural — "<from the résumé above>", never a real noun.
# agents/grounding.py enforces this on the output as well.
# ============================================================
MAX_PROJECTS = 3          # entries shown in TECHNICAL PROJECTS
MAX_EXPERIENCE = 3        # entries shown in EXPERIENCE
MAX_BULLETS_PER_ENTRY = 3 # bullets per project / experience entry
MIN_BULLETS_PER_ENTRY = 1 # allow short entries (e.g., portfolio site = 1 bullet)


RESUME_SYSTEM_PROMPT = (
    "You are an expert resume writer creating ATS-optimized one-page resumes "
    "in Jake Gutierrez's classic LaTeX-resume style. Strict density: every "
    "bullet is a single line that fits within ~22 words. You MUST respond with "
    "valid JSON only — no prose, no markdown."
)

RESUME_PROMPT_TEMPLATE = """Create a tailored ONE-PAGE resume for this specific job application.

## BASE RESUME DATA (use only this — never fabricate):
{resume_yaml}

## TARGET JOB:
Title: {job_title}
Company: {company}
Location: {location}

## JOB DESCRIPTION:
{job_description}

## ATS KEYWORDS TO INCLUDE NATURALLY:
{ats_keywords}

## KEY MATCHES FROM ANALYSIS:
{key_matches}

## KEY GAPS:
{key_gaps}

## ARCHETYPE GUIDANCE (overrides the default ordering/emphasis below where relevant):
{archetype_guidance}

## ONE-PAGE CONSTRAINTS (strict — output will be truncated if you exceed):
- Pick the TOP 3 most-relevant projects (no more, fewer is fine)
- Pick the TOP 3 most-relevant experience entries (no more, fewer is fine)
- MAX 3 bullets per project or experience entry (1-2 is fine for less-relevant entries)
- Each bullet: 18-32 words, may wrap to two lines for the most important entries
- NO professional summary section (Jake's style omits it)
- Education: list every school in the résumé above, exactly as written there. Include a
  thesis line ONLY where the résumé above supplies one. For each school add ONE coursework
  line listing up to 5 courses, chosen from that school's coursework in the résumé above
  (no full sentences, invent nothing).
- Skills: use the SAME category labels that appear in the candidate's résumé above
  (3-4 grouped lines). Do NOT invent or substitute category names.
- Certifications: list each cert with its year — they will be joined into one
  pipe-separated line at render time

## INSTRUCTIONS:
1. Default section order is Education / Technical Skills / Technical Projects / Experience / Certifications.
   Only deviate if the role strongly favors a different order (e.g., heavy ML role → Skills above Projects is fine).
2. Rewrite bullets to emphasize the JD's keywords/requirements naturally
3. Be honest — only rephrase what's in the base resume, never invent
4. Keep it to 1 page (~450-550 words total content)

Respond with this exact JSON structure:
{{
    "section_order": ["education", "skills", "project_experience", "work_experience", "certifications"],
    "education": [
        {{
            "institution": "school name",
            "degree": "degree name",
            "date": "graduation date",
            "location": "city, state",
            "senior_thesis": "include ONLY if the résumé above gives this school a thesis; otherwise omit the key",
            "coursework": "comma-separated list of 5 relevant courses"
        }}
    ],
    "skills": {{
        "<category label copied from the résumé above>": "<only skills listed under that same category in the résumé above, comma-separated, most JD-relevant first>",
        "<second category label from the résumé above>": "<only skills listed under it>"
    }},
    "project_experience": [
        {{
            "title": "project title",
            "tech_stack": "Tech1, Tech2, Tech3",
            "bullets": ["tailored bullet 1", "tailored bullet 2"]
        }}
    ],
    "work_experience": [
        {{
            "title": "role title",
            "organization": "org name",
            "dates": "date range",
            "location": "city, state",
            "bullets": ["tailored bullet 1", "tailored bullet 2"]
        }}
    ],
    "certifications": [
        {{"name": "<certification name EXACTLY as written in the résumé above>", "year": "<its year, or empty string>"}}
    ]
}}"""


COVER_LETTER_SYSTEM_PROMPT = """You are an expert cover letter writer. You create professional, genuine cover letters that demonstrate specific interest in the company and role. Do not be generic or use cliches. Be specific about WHY this candidate fits THIS role at THIS company."""

COVER_LETTER_PROMPT_TEMPLATE = """Write a tailored cover letter for this job application.

## CANDIDATE:
Name: {name}
Email: {email}
Phone: {phone}
Location: {candidate_location}

## TARGET JOB:
Title: {job_title}
Company: {company}
Location: {location}

## JOB DESCRIPTION:
{job_description}

## CANDIDATE'S KEY MATCHING QUALIFICATIONS:
{key_matches}

## ARCHETYPE GUIDANCE (tailor the emphasis to this):
{archetype_guidance}

## INSTRUCTIONS:
Write a 3-4 paragraph cover letter that:
1. Opens with specific interest in {company} and the {job_title} role (NOT generic "I am writing to express interest")
2. Maps 2-3 of the candidate's strongest qualifications to specific job requirements
3. Mentions the skills most relevant to THIS role per the ARCHETYPE GUIDANCE, drawn ONLY from the candidate's qualifications listed above — never a tool, framework or credential not listed there
4. References the most relevant background per the ARCHETYPE GUIDANCE, naming only real employers, projects, schools and credentials from the qualifications above
5. Closes with enthusiasm and a clear call to action
6. Keeps a professional but genuine tone — avoid corporate cliches
7. Total length: 250-350 words

Write the cover letter as plain text (no JSON). Just the letter body, no headers or formatting."""


def _load_config():
    config_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _archetype_guidance(archetype: str | None) -> str:
    """Archetype-specific tailoring guidance injected into the resume + cover prompts.

    Guidance describes WHICH KIND of material to lead with and in what order —
    never a concrete project, employer or certification. Naming real nouns here
    is how a previous candidate's BCAT certification, trading-bot project and
    Reed College thesis ended up being copied into this candidate's résumés; the
    model treats any concrete noun in the instructions as content to emit.
    Selection of specific entries is the model's job, from base_resume.yaml only.
    """
    a = (archetype or "").lower()
    if a == "behavioral_technician":
        return (
            "This is a Behavioral Technician / ABA role. Put \"work_experience\" BEFORE "
            "\"project_experience\" in section_order and lead with any direct behavioral, "
            "care, teaching or client-facing work the résumé above contains. Emphasize 1:1 "
            "client sessions, behavioral data collection, treatment-plan implementation, "
            "reliability and working with children/families — but ONLY where the résumé "
            "above actually evidences them. Surface any behavioral or care certification "
            "the résumé lists, using its exact name. Include AT MOST one technical project, "
            "and only if it demonstrates reliability or data rigor."
        )
    if a in ("ai_engineer", "ai_solutions_engineer", "ai_analyst"):
        return (
            "This is an AI-focused role. Keep \"skills\" and \"project_experience\" near the "
            "top of section_order and lead with the résumé's most substantial AI/LLM work. "
            "Emphasize the AI capabilities the résumé above actually lists — model APIs, "
            "local inference, agents, retrieval, prompt engineering, tool calling, evaluation "
            "— together with the delivery stack it names. Use the JD's vocabulary for those "
            "capabilities where it differs from the résumé's wording, but never claim a tool "
            "or framework the résumé does not list."
        )
    if a == "ml_engineer":
        return (
            "This is a production-ML role. Emphasize the résumé's strongest engineering "
            "rigor — data flow, pipelines, evaluation, deployment and the languages it "
            "lists. Be honest about depth: describe the ML work the résumé evidences, and "
            "do not imply production-ML scale it does not claim."
        )
    return "Use the default ordering and emphasis; tailor bullets to the JD's keywords."


# Markers of an unedited config/base_resume*.example.yaml copy. A résumé built
# from one of these is not a weaker résumé, it is a different person's: a
# behavioral-technician posting at Centria Autism received one carrying
# "Your University" and "City, State" because base_resume_bt.yaml had never
# been filled in and the BT route trusted it on existence alone.
_TEMPLATE_MARKERS = ("Jane Doe", "John Doe", "Your University",
                     "your.email@example.com", "555-555-5555", "your-handle")


def _is_unedited_template(text: str) -> bool:
    return any(m.lower() in text.lower() for m in _TEMPLATE_MARKERS)


def _resolve_resume_path(archetype: str | None = None) -> Path:
    """Which base résumé file backs this archetype.

    Existence is not enough — an unedited example copy must never be used as
    source material, so it falls back to the primary résumé.
    """
    base = Path(__file__).parent.parent / "config"
    primary = base / "base_resume.yaml"
    if archetype != "behavioral_technician":
        return primary
    bt = base / "base_resume_bt.yaml"
    if not bt.exists():
        return primary
    try:
        if _is_unedited_template(bt.read_text(encoding="utf-8")):
            logger.error(
                "[tailor] config/base_resume_bt.yaml is still the unedited example "
                "(placeholder identity) — falling back to base_resume.yaml. Fill it "
                "in or delete it; a résumé built from it would carry another name.")
            return primary
    except OSError:
        return primary
    return bt


def _load_resume_yaml(archetype: str | None = None) -> str:
    """Raw résumé YAML fed to the tailoring prompt."""
    return _resolve_resume_path(archetype).read_text(encoding="utf-8")


def _load_resume_data(archetype: str | None = None) -> dict:
    """Parsed base résumé — the ground truth agents/grounding.py checks against."""
    return yaml.safe_load(_load_resume_yaml(archetype)) or {}


_BODY_FONT = "Garamond"          # ATS-friendly serif that reads close to Jake's LaTeX template
_BODY_FALLBACK = "Times New Roman"
_BODY_SIZE_PT = 10               # tightened from 10.5 to fit one page with 3 projects + 3 jobs
_NAME_SIZE_PT = 22               # large centered name like the canonical resume
_CONTACT_SIZE_PT = 9.5
_SECTION_SIZE_PT = 11            # small-caps section headers
_BULLET_SIZE_PT = 10

# Tab stop position — must equal page_width - left_margin - right_margin.
# Letter is 8.5"; with 0.45" left/right margins, content width = 7.6".
_RIGHT_TAB_INCHES = 7.6


def _set_run_font(run, size_pt: float, *, bold=False, italic=False, underline=False, small_caps=False):
    """Apply the resume's body font + size to a run."""
    run.font.name = _BODY_FONT
    # python-docx leaves East Asian font separate; set it too so Word doesn't fall back
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), _BODY_FONT)
    rfonts.set(qn("w:hAnsi"), _BODY_FONT)
    rfonts.set(qn("w:cs"), _BODY_FONT)
    run.font.size = Pt(size_pt)
    run.bold = bold
    run.italic = italic
    if underline:
        run.underline = True
    if small_caps:
        # <w:smallCaps w:val="1"/> — typographic small caps (real, not ALL CAPS hack)
        sc = OxmlElement("w:smallCaps")
        sc.set(qn("w:val"), "1")
        rpr.append(sc)


def _add_bottom_border(paragraph, *, size_eighths=6, color="000000"):
    """Draw a thin horizontal rule under a paragraph (Jake's section divider)."""
    pPr = paragraph._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size_eighths))
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), color)
    pBdr.append(bottom)
    pPr.append(pBdr)


def _set_right_tab(paragraph):
    """Add a single right-aligned tab stop at the right margin so two-col rows align."""
    paragraph.paragraph_format.tab_stops.add_tab_stop(
        Inches(_RIGHT_TAB_INCHES), WD_TAB_ALIGNMENT.RIGHT
    )


def _tight(paragraph, *, before=0, after=0, line=1.15):
    """Minimal vertical spacing — Jake's-style density."""
    paragraph.paragraph_format.space_before = Pt(before)
    paragraph.paragraph_format.space_after = Pt(after)
    paragraph.paragraph_format.line_spacing = line


def _section_header(doc: Document, text: str):
    """Title-case bold section header with real small-caps + hairline rule below.
    Matches the canonical resume's LaTeX \\section appearance."""
    p = doc.add_paragraph()
    _tight(p, before=4, after=0)
    # Keep the header with the next paragraph so a section never breaks across pages
    p.paragraph_format.keep_with_next = True
    # First letter is naturally larger than the small-caps that follow.
    # We render the whole word with small-caps applied; the first letter still
    # renders as a full-size capital because it was already uppercase.
    run = p.add_run(text)
    _set_run_font(run, _SECTION_SIZE_PT, bold=False, small_caps=True)
    _add_bottom_border(p)
    return p


def _two_col(doc: Document, left: str, right: str, *, left_bold=False, italic=False, size=_BODY_SIZE_PT):
    """Two-column row: left text + tab + right text (Jake's bread and butter)."""
    p = doc.add_paragraph()
    _tight(p, before=0, after=0, line=1.08)
    _set_right_tab(p)
    lr = p.add_run(left)
    _set_run_font(lr, size, bold=left_bold, italic=italic)
    p.add_run("\t")
    rr = p.add_run(right)
    _set_run_font(rr, size, italic=italic)
    return p


def _bullet(doc: Document, text: str):
    """Tight hanging-indent bullet point."""
    p = doc.add_paragraph()
    _tight(p, before=0, after=1, line=1.08)
    p.paragraph_format.left_indent = Inches(0.20)
    p.paragraph_format.first_line_indent = Inches(-0.20)
    run = p.add_run(f"• {text}")
    _set_run_font(run, _BULLET_SIZE_PT)
    return p


def _create_resume_docx(resume_data: dict, config: dict) -> Document:
    """Render Gemma's tailored content into a one-page Jake's-style .docx."""
    doc = Document()

    # Tighten the default style so anything we don't override still looks right
    normal = doc.styles["Normal"]
    normal.font.name = _BODY_FONT
    normal.font.size = Pt(_BODY_SIZE_PT)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(0)

    # Margins: 0.3" top/bottom, 0.45" left/right -> 7.6" usable width
    for section in doc.sections:
        section.top_margin = Inches(0.3)
        section.bottom_margin = Inches(0.3)
        section.left_margin = Inches(0.45)
        section.right_margin = Inches(0.45)

    user = config.get("user", {})

    # ---- Header: name + contact line (matches canonical resume exactly) ----
    name_para = doc.add_paragraph()
    name_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _tight(name_para, before=0, after=2)
    name_run = name_para.add_run(user.get("name", "Matthew Cromaz"))
    _set_run_font(name_run, _NAME_SIZE_PT, bold=False)

    contact_para = doc.add_paragraph()
    contact_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _tight(contact_para, before=0, after=2)

    # Contact bits in canonical order. Underline the link-like bits to match
    # the rendered LaTeX \href{} appearance in the canonical PDF.
    location = user.get("location", "San Francisco, CA")
    phone = user.get("phone", "")
    email = user.get("email", "")
    linkedin = user.get("linkedin", "")
    github = user.get("github", "")
    sep = "  |  "

    def _add(text, *, underline=False):
        if not text:
            return False
        r = contact_para.add_run(text)
        _set_run_font(r, _CONTACT_SIZE_PT, underline=underline)
        return True

    parts = []
    if location:
        parts.append(("plain", location))
    if phone:
        parts.append(("plain", phone))
    if email:
        parts.append(("link", email))
    if linkedin:
        parts.append(("link", linkedin))
    if github:
        parts.append(("link", github))

    for i, (kind, txt) in enumerate(parts):
        if i > 0:
            sep_run = contact_para.add_run(sep)
            _set_run_font(sep_run, _CONTACT_SIZE_PT)
        _add(txt, underline=(kind == "link"))

    # ---- Sections (skip 'summary' — Jake's style omits it) ----
    section_order = resume_data.get("section_order", [
        "education", "skills", "project_experience", "work_experience", "certifications"
    ])
    section_order = [s for s in section_order if s != "summary"]

    for section_name in section_order:
        if section_name == "education":
            _render_education(doc, resume_data.get("education", []))
        elif section_name == "skills":
            _render_skills(doc, resume_data.get("skills", {}))
        elif section_name == "project_experience":
            _render_projects(doc, resume_data.get("project_experience", []))
        elif section_name == "work_experience":
            _render_experience(doc, resume_data.get("work_experience", []))
        elif section_name == "certifications":
            _render_certifications(doc, resume_data.get("certifications", []))

    return doc


def _render_education(doc, items):
    if not items:
        return
    _section_header(doc, "Education")
    for i, edu in enumerate(items[:2]):  # max 2 schools
        row1 = _two_col(
            doc,
            edu.get("institution", ""),
            edu.get("location", ""),
            left_bold=True,
        )
        if i > 0:
            row1.paragraph_format.space_before = Pt(2)
        _two_col(
            doc,
            edu.get("degree", ""),
            edu.get("date", ""),
            italic=True,
        )
        # Senior Thesis line (Reed College only, typically) — bullet with bold prefix
        thesis = edu.get("senior_thesis") or edu.get("thesis")
        if thesis:
            _bullet_with_prefix(doc, "Senior Thesis:", thesis)

        # Coursework — accept either a string (already comma-joined) or a list.
        # Render as a bulleted line with bold "Coursework:" prefix.
        coursework = edu.get("coursework")
        if coursework is None:
            coursework = edu.get("highlights")  # legacy field name
        if coursework:
            if isinstance(coursework, list):
                coursework_text = ", ".join(coursework[:6])
            else:
                coursework_text = str(coursework)
            _bullet_with_prefix(doc, "Coursework:", coursework_text)


def _bullet_with_prefix(doc, prefix: str, text: str):
    """Bullet line with a bold prefix — used for 'Senior Thesis:' and 'Coursework:'."""
    p = doc.add_paragraph()
    _tight(p, before=0, after=1, line=1.08)
    p.paragraph_format.left_indent = Inches(0.20)
    p.paragraph_format.first_line_indent = Inches(-0.20)
    bullet_run = p.add_run("• ")
    _set_run_font(bullet_run, _BULLET_SIZE_PT)
    prefix_run = p.add_run(f"{prefix} ")
    _set_run_font(prefix_run, _BULLET_SIZE_PT, bold=True)
    text_run = p.add_run(text)
    _set_run_font(text_run, _BULLET_SIZE_PT)


def _render_skills(doc, skills):
    if not skills:
        return
    _section_header(doc, "Technical Skills")

    # Skills can be either:
    #   1. New canonical dict: {Languages: "...", Data & ML: "...", AI Engineering: "...", Tools & Infrastructure: "..."}
    #   2. Legacy dict: {languages: [...], tools: [...], libraries: [...]}
    if not isinstance(skills, dict):
        return

    if any(k in skills for k in ("languages", "tools", "libraries")):
        # legacy shape
        entries = []
        if skills.get("languages"):
            entries.append(("Languages", ", ".join(skills["languages"])))
        if skills.get("tools"):
            entries.append(("Tools", ", ".join(skills["tools"])))
        if skills.get("libraries"):
            entries.append(("Libraries", ", ".join(skills["libraries"])))
    else:
        # Preserve canonical 4-category order if present
        canonical_order = ["Languages", "Data & ML", "AI Engineering", "Tools & Infrastructure"]
        ordered_keys = [k for k in canonical_order if k in skills]
        for k in skills.keys():
            if k not in ordered_keys:
                ordered_keys.append(k)
        entries = []
        for k in ordered_keys:
            v = skills[k]
            entries.append((k, v if isinstance(v, str) else ", ".join(v)))

    for label, content in entries[:4]:  # cap at 4 skill lines
        if not content:
            continue
        p = doc.add_paragraph()
        _tight(p, before=0, after=1, line=1.08)
        bold_run = p.add_run(f"{label}: ")
        _set_run_font(bold_run, _BODY_SIZE_PT, bold=True)
        text_run = p.add_run(content)
        _set_run_font(text_run, _BODY_SIZE_PT)


def _bullet_text(bullet) -> str:
    """Extract bullet text from a string or a dict of any common shape.

    The LLM is asked for plain strings but smaller models sometimes echo
    dicts like {"text": ...} / {"bullet": ...} — accept them all.
    """
    if isinstance(bullet, dict):
        for key in ("text", "bullet", "content", "value", "description"):
            if bullet.get(key):
                return str(bullet[key])
        for v in bullet.values():
            if isinstance(v, str) and v.strip():
                return v
        return ""
    return str(bullet) if bullet else ""


def _render_projects(doc, items):
    if not items:
        return
    _section_header(doc, "Technical Projects")
    for proj in items[:MAX_PROJECTS]:
        title = proj.get("title", "")
        stack = proj.get("tech_stack", "") or proj.get("organization", "")
        # Inline header row (matches canonical resume): **Bold Title** | *italic stack*
        p = doc.add_paragraph()
        _tight(p, before=2, after=0, line=1.08)
        title_run = p.add_run(title)
        _set_run_font(title_run, _BODY_SIZE_PT, bold=True)
        if stack:
            sep_run = p.add_run(" | ")
            _set_run_font(sep_run, _BODY_SIZE_PT)
            stack_run = p.add_run(stack)
            _set_run_font(stack_run, _BODY_SIZE_PT, italic=True)
        # Bullets — accept either list of strings or list of {text, keywords} dicts
        for bullet in proj.get("bullets", [])[:MAX_BULLETS_PER_ENTRY]:
            text = _bullet_text(bullet)
            if text:
                _bullet(doc, text)


def _render_experience(doc, items):
    if not items:
        return
    _section_header(doc, "Experience")
    for i, exp in enumerate(items[:MAX_EXPERIENCE]):
        org = exp.get("organization", "")
        loc = exp.get("location", "")
        title = exp.get("title", "")
        dates = exp.get("dates", "")
        # Row 1: bold org <tab> location  (small gap before subsequent entries)
        row1 = _two_col(doc, org, loc, left_bold=True)
        if i > 0:
            row1.paragraph_format.space_before = Pt(2)
        # Row 2: italic title <tab> italic dates
        _two_col(doc, title, dates, italic=True)
        # Bullets — accept either list of strings or list of {text, keywords} dicts
        for bullet in exp.get("bullets", [])[:MAX_BULLETS_PER_ENTRY]:
            text = _bullet_text(bullet)
            if text:
                _bullet(doc, text)


def _render_certifications(doc, items):
    if not items:
        return
    _section_header(doc, "Certifications")
    p = doc.add_paragraph()
    _tight(p, before=0, after=0, line=1.08)

    # Items can be:
    #   - list of strings ("Tableau Desktop Specialist (2025)")
    #   - list of {name, year} dicts
    # Render as: **Name** (year) | **Name** (year) | ... — bolded names, plain years
    normalized: list[tuple[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            normalized.append((str(item.get("name", "")).strip(), str(item.get("year", "")).strip()))
        else:
            # string form: try to split a trailing "(YYYY)"
            s = str(item).strip()
            year = ""
            if s.endswith(")") and "(" in s:
                head, _, tail = s.rpartition("(")
                if tail.rstrip(")").strip().isdigit():
                    s = head.strip()
                    year = tail.rstrip(")").strip()
            normalized.append((s, year))

    for i, (name, year) in enumerate(normalized):
        if i > 0:
            sep_run = p.add_run(" | ")
            _set_run_font(sep_run, _BODY_SIZE_PT)
        name_run = p.add_run(name)
        _set_run_font(name_run, _BODY_SIZE_PT, bold=True)
        if year:
            year_run = p.add_run(f" ({year})")
            _set_run_font(year_run, _BODY_SIZE_PT)


def _create_cover_letter_docx(cover_text: str, job: Job, config: dict) -> Document:
    """Render a one-page cover letter that visually matches the resume's typography."""
    doc = Document()

    # Match resume font for visual consistency across the application packet
    normal = doc.styles["Normal"]
    normal.font.name = _BODY_FONT
    normal.font.size = Pt(11)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.15

    for section in doc.sections:
        section.top_margin = Inches(0.7)
        section.bottom_margin = Inches(0.7)
        section.left_margin = Inches(0.8)
        section.right_margin = Inches(0.8)

    user = config.get("user", {})

    # Letterhead — keep the same name typography as the resume
    name_p = doc.add_paragraph()
    _tight(name_p, before=0, after=2)
    name_run = name_p.add_run(user.get("name", ""))
    _set_run_font(name_run, 16, bold=True)

    contact_p = doc.add_paragraph()
    _tight(contact_p, before=0, after=2)
    contact_bits = [
        user.get("email", ""),
        user.get("phone", ""),
        user.get("location", ""),
        user.get("linkedin", ""),
    ]
    contact_run = contact_p.add_run("  |  ".join(b for b in contact_bits if b))
    _set_run_font(contact_run, 10)

    date_p = doc.add_paragraph(datetime.now().strftime("%B %d, %Y"))
    _tight(date_p, before=8, after=12)

    # Body — paragraphs separated by blank lines in cover_text
    paragraphs = [p.strip() for p in cover_text.strip().split("\n\n") if p.strip()]
    for para_text in paragraphs:
        p = doc.add_paragraph()
        _tight(p, before=0, after=8, line=1.18)
        run = p.add_run(para_text)
        _set_run_font(run, 11)

    # Closing
    closing_p = doc.add_paragraph()
    _tight(closing_p, before=8, after=2)
    _set_run_font(closing_p.add_run("Sincerely,"), 11)
    sig_p = doc.add_paragraph()
    _tight(sig_p, before=0, after=0)
    _set_run_font(sig_p.add_run(user.get("name", "")), 11)

    return doc


def _trim_one_bullet(data: dict) -> dict | None:
    """One cut, least valuable first: the last bullet of the last rendered
    project / experience entry that still has more than MIN_BULLETS_PER_ENTRY,
    then a whole trailing project. None when nothing safe is left. Pure."""
    import copy
    d = copy.deepcopy(data)
    # keys are the ones _create_resume_docx renders, not the prompt's prose names
    for key, cap in (("project_experience", MAX_PROJECTS), ("work_experience", MAX_EXPERIENCE)):
        for entry in reversed((d.get(key) or [])[:cap]):
            bullets = entry.get("bullets") or []
            if len(bullets) > MIN_BULLETS_PER_ENTRY:
                entry["bullets"] = bullets[:-1]
                return d
    if len(d.get("project_experience") or []) > 2:
        d["project_experience"] = d["project_experience"][:-1]
        return d
    return None


def _fit_one_page(resume_data: dict, config: dict, resume_path: Path):
    """Export the .docx to PDF and, while Word says it spills past page 1, cut
    one line and re-render (≤6 cuts). Returns (data, pdf_path|None, pages|None).
    Without Word the .docx ships as-is — exactly the pre-PDF behavior."""
    from agents.pdf_export import WordExporter
    pdf_path = resume_path.with_suffix(".pdf")
    data, pages = resume_data, None
    with WordExporter() as word:
        if not word.available:
            return data, None, None
        for cut in range(7):
            pages = word.export(resume_path, pdf_path)
            if pages is None:
                return data, None, None
            if pages <= 1:
                if cut:
                    logger.info(f"[tailor] fit to one page after trimming {cut} line(s)")
                return data, pdf_path, pages
            trimmed = _trim_one_bullet(data)
            if trimmed is None:
                logger.warning(f"[tailor] still {pages} pages with nothing safe left to trim")
                return data, pdf_path, pages
            data = trimmed
            _create_resume_docx(data, config).save(str(resume_path))
    return data, pdf_path, pages


def _jd_cap(config: dict) -> int:
    """How much job description reaches the prompt (settings.yaml tailor.jd_max_chars).

    Was hard-coded [:3000]. Across the stored corpus that discarded roughly
    467k characters of description — the very material the resume is meant to
    be tailored against — while num_ctx sat half unused.
    """
    return int((config.get("tailor") or {}).get("jd_max_chars", 12000))


def _jd_terms(job: Job, job_score: JobScore) -> set[str]:
    """Content terms describing THIS job, for the clone guard's JD-similarity exemption.

    Two near-identical postings should be allowed near-identical résumés;
    penalising that would push the model toward inventing differences. Built
    from the scored keywords plus the title so it works even when the
    description is missing — which is the case for 51% of stored jobs.
    """
    import re as _re
    blob = " ".join([
        job.title or "",
        " ".join(job_score.ats_keywords or []) if job_score is not None else "",
        " ".join(job_score.key_matches or []) if job_score is not None else "",
    ]).lower()
    return {t for t in _re.findall(r"[a-z0-9][a-z0-9+#.\-]{2,}", blob)
            if t not in ("and", "the", "for", "with", "senior", "junior")}


def _draft_rank(report: dict, clone) -> float:
    """Ordering key for "is this draft better than the one we have?".

    Coverage dominates, with a fixed penalty for a draft that merely restates
    another job's résumé. The penalty is small enough that a clearly better-
    covering draft still wins: differentiation is a tie-breaker, not a veto.
    """
    score = float(report.get("overall") or 0.0)
    return score - (4.0 if getattr(clone, "flagged", False) else 0.0)


def tailor_for_job(job: Job, job_score: JobScore, *, cover_letter: bool | None = None,
                   max_rounds: int | None = None) -> dict:
    """Generate a tailored, page-count-verified one-page résumé (+ optional
    cover letter) for a specific job.

    cover_letter: None → settings.yaml tailor.cover_letter_default (True).

    Returns:
        {resume_docx, resume_pdf|None, pages|None, cover_letter_docx|None,
         optimizer_score, optimizer_report, keywords_missing}
    """
    config = _load_config()
    if cover_letter is None:
        cover_letter = bool((config.get("tailor") or {}).get("cover_letter_default", True))
    archetype = getattr(job_score, "archetype", None)
    resume_yaml = _load_resume_yaml(archetype)
    user = config.get("user", {})
    guidance = _archetype_guidance(archetype)

    # Generate tailored resume
    logger.info(f"[tailor] Generating resume for: {job.title} at {job.company}")
    resume_prompt = RESUME_PROMPT_TEMPLATE.format(
        resume_yaml=resume_yaml,
        job_title=job.title,
        company=job.company,
        location=job.location or "Not specified",
        job_description=(job.description or "No description")[:_jd_cap(config)],
        ats_keywords=", ".join(job_score.ats_keywords or []),
        key_matches=", ".join(job_score.key_matches or []),
        key_gaps=", ".join(job_score.key_gaps or []),
        archetype_guidance=guidance,
    )

    resume_data = generate_json(resume_prompt, system_prompt=RESUME_SYSTEM_PROMPT,
                                profile="tailor_resume")

    # Provenance gate — strips any claim not evidenced by the base résumé.
    # Runs BEFORE the optimizer so the audit scores the résumé that will
    # actually ship, not a richer one that briefly existed in memory.
    base_resume_data = _load_resume_data(archetype)
    resume_data = enforce_and_log(resume_data, base_resume_data,
                                  label=f"{job.title} @ {job.company}")

    # Generate cover letter (optional)
    cover_text = ""
    if cover_letter:
        logger.info(f"[tailor] Generating cover letter for: {job.title} at {job.company}")
        cover_prompt = COVER_LETTER_PROMPT_TEMPLATE.format(
            name=user.get("name", ""),
            email=user.get("email", ""),
            phone=user.get("phone", ""),
            candidate_location=user.get("location", ""),
            job_title=job.title,
            company=job.company,
            location=job.location or "Not specified",
            job_description=(job.description or "No description")[:_jd_cap(config)],
            key_matches="\n".join(f"- {m}" for m in (job_score.key_matches or [])),
            archetype_guidance=guidance,
        )
        cover_text = generate_text(cover_prompt, system_prompt=COVER_LETTER_SYSTEM_PROMPT,
                                   profile="tailor_cover")

    # Clean filename
    safe_company = "".join(c if c.isalnum() or c in "- " else "" for c in job.company).strip().replace(" ", "_")
    safe_title = "".join(c if c.isalnum() or c in "- " else "" for c in job.title).strip().replace(" ", "_")
    filename_base = f"{safe_company}_{safe_title}"[:80]

    # --- Iterate to target: score, improve, keep the best -------------------
    # Was a single conditional retry behind `enabled: false`, i.e. off entirely.
    # Now: up to max_rounds attempts, stopping early once target_score is met.
    # Two independent feedback signals steer each retry —
    #   * the optimizer's coverage audit (what the JD asks for and we missed)
    #   * the clone guard (what we said identically on a DIFFERENT job)
    # Both only ever ask for RE-EMPHASIS of attested material; neither can
    # introduce a claim, and every draft still passes the provenance gate.
    opt_cfg = (config.get("tailor", {}) or {}).get("optimizer", {}) or {}
    optimizer_report = None
    clone_verdict = None
    rounds_used = 1
    if opt_cfg.get("enabled", True):
        try:
            from agents.resume_optimizer import (
                score_materials, render_resume_text, feedback_block, save_report,
            )
            from agents.clone_guard import clone_check, load_clone_corpus

            cg_cfg = (config.get("tailor", {}) or {}).get("clone_guard", {}) or {}
            corpus = []
            if cg_cfg.get("enabled", True):
                try:
                    corpus = load_clone_corpus(
                        config, exclude_base=filename_base,
                        limit=int(cg_cfg.get("corpus_size", 40)),
                        shingle_n=int(cg_cfg.get("shingle_n", 4)))
                except Exception as e:      # advisory signal — never fatal
                    logger.debug(f"[tailor] clone corpus unavailable: {e}")

            jd_terms = _jd_terms(job, job_score)
            target = float(opt_cfg.get("target_score", 85))
            floor = float(opt_cfg.get("min_score", 70))
            if max_rounds is None:
                max_rounds = int(opt_cfg.get("max_rounds", 3))
            max_rounds = max(1, max_rounds)

            best_data = resume_data
            best_report = score_materials(
                job, job_score, render_resume_text(resume_data), cover_text)
            best_clone = clone_check(resume_data, corpus, config,
                                     jd_terms=jd_terms, name=filename_base)
            logger.info(f"[tailor] round 1/{max_rounds}: coverage "
                        f"{best_report['overall']}/100"
                        + (f", clone {best_clone.similarity:.2f} vs {best_clone.nearest}"
                           if best_clone.flagged else ""))

            for rnd in range(2, max_rounds + 1):
                if best_report["overall"] >= target and not best_clone.flagged:
                    break                      # good enough and differentiated
                rounds_used = rnd
                feedback = feedback_block(best_report)
                if best_clone.flagged:
                    feedback += "\n\n" + best_clone.feedback
                try:
                    cand = generate_json(resume_prompt + "\n\n" + feedback,
                                         system_prompt=RESUME_SYSTEM_PROMPT,
                                         profile="tailor_resume")
                except Exception as e:
                    logger.warning(f"[tailor] round {rnd} generation failed: {e}")
                    break
                cand = enforce_and_log(cand, base_resume_data,
                                       label=f"round {rnd} {job.title} @ {job.company}")
                cand_report = score_materials(
                    job, job_score, render_resume_text(cand), cover_text)
                cand_clone = clone_check(cand, corpus, config,
                                         jd_terms=jd_terms, name=filename_base)
                logger.info(f"[tailor] round {rnd}/{max_rounds}: coverage "
                            f"{cand_report['overall']}/100"
                            + (f", clone {cand_clone.similarity:.2f}"
                               if cand_clone.flagged else ", differentiated"))
                # A clone-flagged draft is penalised but not disqualified: an
                # under-differentiated résumé is still honest, and refusing to
                # ship one would be a worse trade than shipping it flagged.
                if _draft_rank(cand_report, cand_clone) > _draft_rank(best_report, best_clone):
                    best_data, best_report, best_clone = cand, cand_report, cand_clone

            resume_data, optimizer_report, clone_verdict = best_data, best_report, best_clone
            optimizer_report["rounds"] = rounds_used
            optimizer_report["clone"] = clone_verdict.as_dict()
            if optimizer_report["overall"] < floor:
                logger.warning(f"[tailor] shipping below the {floor} floor at "
                               f"{optimizer_report['overall']}/100 after {rounds_used} "
                               f"round(s) — the honest ceiling for this JD may be lower")
            optimizer_report["_saved_to"] = save_report(optimizer_report, filename_base, config)
        except Exception as e:
            logger.warning(f"[tailor] optimizer pass failed (materials still generated): {e}")

    # Create output directory
    base_dir = Path(__file__).parent.parent
    resumes_dir = base_dir / config.get("output", {}).get("resumes_dir", "output/resumes")
    covers_dir = base_dir / config.get("output", {}).get("cover_letters_dir", "output/cover_letters")
    resumes_dir.mkdir(parents=True, exist_ok=True)
    covers_dir.mkdir(parents=True, exist_ok=True)

    # Save resume .docx, then VERIFY one page through Word (PDF export) and trim
    # a line at a time until it fits — the bullet caps are a guess, the page
    # count is the truth. A stale PDF from an earlier run must never survive a
    # failed export, so it goes first.
    resume_doc = _create_resume_docx(resume_data, config)
    resume_path = resumes_dir / f"{filename_base}_resume.docx"
    resume_path.with_suffix(".pdf").unlink(missing_ok=True)
    resume_doc.save(str(resume_path))
    try:
        resume_data, pdf_path, pages = _fit_one_page(resume_data, config, resume_path)
    except Exception as e:
        logger.warning(f"[tailor] PDF / one-page pass failed (docx still shipped): {e}")
        pdf_path, pages = None, None

    # Sidecar JSON of the structured content (post-trim) — /api/autofill/history
    # serves THESE entries for Workday-style wizards so the filled experience
    # panels match the attached tailored résumé word-for-word.
    try:
        resume_path.with_suffix(".json").write_text(
            json.dumps(resume_data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[tailor] sidecar JSON save failed: {e}")

    # Save cover letter .docx (optional)
    cover_path = None
    if cover_letter:
        cover_doc = _create_cover_letter_docx(cover_text, job, config)
        cover_path = covers_dir / f"{filename_base}_cover_letter.docx"
        cover_doc.save(str(cover_path))

    logger.info("[tailor] Materials saved: " + resume_path.name
                + (f", {pdf_path.name} ({pages} page)" if pdf_path else " (no PDF — Word unavailable)")
                + (f", {cover_path.name}" if cover_path else ""))

    return {
        "resume_docx": str(resume_path),
        "resume_pdf": str(pdf_path) if pdf_path else None,
        "pages": pages,
        "cover_letter_docx": str(cover_path) if cover_path else None,
        "optimizer_score": optimizer_report["overall"] if optimizer_report else None,
        "optimizer_report": optimizer_report.get("_saved_to") if optimizer_report else None,
        "keywords_missing": list(optimizer_report.get("keywords_missing") or [])[:8] if optimizer_report else [],
    }


def tailor_queued_jobs(config: dict, on_each=None) -> dict:
    """Generate materials for all queued (high-scoring) jobs.

    Args:
        on_each: optional callback(done_in_batch, label) fired after each job,
            so a caller can show per-job progress instead of per-batch.

    Returns:
        Dict with results: {tailored, errors}
    """
    session = get_session()
    tailored_count = 0
    errors = []

    try:
        # Find queued jobs that need materials
        # Join JobScore: an unscored job can't be tailored (no archetype, no
        # dimensions), and without this join a batch of unscored rows at the
        # head of the queue starves tailoring forever.
        queued_apps = (
            session.query(Application)
            .join(JobScore, JobScore.job_id == Application.job_id)
            .filter(Application.status == ApplicationStatus.QUEUED)
            .filter(Application.resume_path.is_(None))
            .order_by(JobScore.fit_score.desc())
            .limit(int((config.get("tailor") or {}).get("batch_size", 10)))
            .all()
        )

        logger.info(f"[tailor] Found {len(queued_apps)} jobs needing materials")

        for app in queued_apps:
            try:
                job = session.query(Job).get(app.job_id)
                score = session.query(JobScore).filter_by(job_id=app.job_id).first()

                if not job or not score:
                    continue

                paths = tailor_for_job(job, score)

                app.resume_path = paths["resume_docx"]
                app.cover_letter_path = paths["cover_letter_docx"]
                record_status_change(session, app, ApplicationStatus.MATERIALS_READY, source="tailor")
                session.commit()

                tailored_count += 1
                if on_each:
                    try:
                        on_each(tailored_count, f"{job.title} @ {job.company}")
                    except Exception:
                        pass   # progress reporting must never break tailoring

            except Exception as e:
                session.rollback()
                error_msg = f"Error tailoring for '{job.title}' at '{job.company}': {e}"
                logger.error(f"[tailor] {error_msg}")
                errors.append(error_msg)

    finally:
        session.close()

    logger.info(f"[tailor] Tailoring complete: {tailored_count} jobs processed")
    return {"tailored": tailored_count, "errors": errors}
