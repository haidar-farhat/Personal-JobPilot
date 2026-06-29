"""Resume and cover letter tailoring engine using Gemma 4 via Ollama.

Output is one-page, Jake's-resume styled .docx — serif body, ALL CAPS bold
section headers with hairline rule beneath, tab-aligned two-column rows for
institution/location and degree/date, tight bullet spacing.
"""

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

from db.database import get_session
from db.models import Job, JobScore, Application, ApplicationStatus
from utils.ollama_client import generate_json, generate_text

logger = logging.getLogger(__name__)


# ============================================================
# One-page Jake's-style hard caps applied at render time.
# These match the canonical resume in
#   C:\Users\matth\Downloads\Resumes\Matthew_Cromaz_new_resume.docx
# Acts as a safety net even if Gemma over-generates.
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
- Education: list both schools. For Reed include the Senior Thesis line. For each school
  add ONE coursework line listing 5 relevant courses (no full sentences).
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
            "senior_thesis": "optional — one-sentence thesis description for Reed; omit for other schools",
            "coursework": "comma-separated list of 5 relevant courses"
        }}
    ],
    "skills": {{
        "Languages": "Python, SQL, R, JavaScript, HTML/CSS, C++",
        "Data & ML": "Pandas, NumPy, scikit-learn, Statsmodels, ...",
        "AI Engineering": "Claude API, OpenAI API, local LLMs (Ollama), ...",
        "Tools & Infrastructure": "Git/GitHub, FastAPI, Playwright, ..."
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
        {{"name": "Tableau Desktop Specialist", "year": "2025"}},
        {{"name": "CFA Institute Investment Foundations Certificate", "year": "2024"}}
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
3. Mentions the skills most relevant to THIS role per the ARCHETYPE GUIDANCE (e.g. behavioral/ABA + BCAT for BT roles; Claude/OpenAI APIs, agents, prompt engineering for AI roles; Python/SQL/econometrics/Tableau for analyst roles)
4. References the most relevant background per the ARCHETYPE GUIDANCE (behavioral experience for BT; AI-engineering projects for AI roles; MS Quantitative Economics + modeling for analyst roles)
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

    Behavioral Technician roles must lead with the BIA experience + BCAT (not the
    software/AI projects); AI roles lead with AI-engineering work.
    """
    a = (archetype or "").lower()
    if a == "behavioral_technician":
        return (
            "This is a Behavioral Technician / ABA role. LEAD with the Behavioral "
            "Technician (BIA) work_experience and the BCAT certification. Put "
            '"work_experience" BEFORE "project_experience" in section_order. Emphasize '
            "1:1 client sessions, behavioral data collection, treatment-plan implementation, "
            "reliability, and working with children/families. Include AT MOST one technical "
            "project, and only if it shows reliability or data rigor — do NOT lead with "
            "software/AI work for this role."
        )
    if a in ("ai_engineer", "ai_solutions_engineer", "ai_analyst"):
        return (
            "This is an AI-focused role. LEAD with the AI Engineering skills and the "
            "LLM/agentic projects (Algorithmic Paper Trading System, JobPilot). Emphasize "
            "Claude/OpenAI APIs, local LLMs, MCP, agents, prompt engineering, RAG, tool-use, "
            "and full-stack delivery (FastAPI, Next.js). Keep project_experience and skills near the top."
        )
    if a == "ml_engineer":
        return (
            "This is a production-ML role. Emphasize Python, ML tooling, data pipelines, and "
            "engineering rigor from the trading system; be honest about depth of production-ML experience."
        )
    return "Use the default ordering and emphasis; tailor bullets to the JD's keywords."


def _load_resume_yaml(archetype: str | None = None) -> str:
    """Raw résumé YAML fed to the tailoring prompt. BT roles use the SFUSD/
    behavioral résumé; everything else uses the AI/data résumé."""
    fname = "base_resume_bt.yaml" if archetype == "behavioral_technician" else "base_resume.yaml"
    base = Path(__file__).parent.parent / "config"
    path = base / fname
    if not path.exists():
        path = base / "base_resume.yaml"
    with open(path, encoding="utf-8") as f:
        return f.read()


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
            text = bullet["text"] if isinstance(bullet, dict) else bullet
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
            text = bullet["text"] if isinstance(bullet, dict) else bullet
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


def tailor_for_job(job: Job, job_score: JobScore) -> dict:
    """Generate tailored resume and cover letter for a specific job.

    Returns:
        Dict with paths: {resume_docx, cover_letter_docx}
    """
    config = _load_config()
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
        job_description=(job.description or "No description")[:3000],
        ats_keywords=", ".join(job_score.ats_keywords or []),
        key_matches=", ".join(job_score.key_matches or []),
        key_gaps=", ".join(job_score.key_gaps or []),
        archetype_guidance=guidance,
    )

    resume_data = generate_json(resume_prompt, system_prompt=RESUME_SYSTEM_PROMPT)

    # Generate cover letter
    logger.info(f"[tailor] Generating cover letter for: {job.title} at {job.company}")
    cover_prompt = COVER_LETTER_PROMPT_TEMPLATE.format(
        name=user.get("name", ""),
        email=user.get("email", ""),
        phone=user.get("phone", ""),
        candidate_location=user.get("location", ""),
        job_title=job.title,
        company=job.company,
        location=job.location or "Not specified",
        job_description=(job.description or "No description")[:3000],
        key_matches="\n".join(f"- {m}" for m in (job_score.key_matches or [])),
        archetype_guidance=guidance,
    )

    cover_text = generate_text(cover_prompt, system_prompt=COVER_LETTER_SYSTEM_PROMPT)

    # Create output directory
    base_dir = Path(__file__).parent.parent
    resumes_dir = base_dir / config.get("output", {}).get("resumes_dir", "output/resumes")
    covers_dir = base_dir / config.get("output", {}).get("cover_letters_dir", "output/cover_letters")
    resumes_dir.mkdir(parents=True, exist_ok=True)
    covers_dir.mkdir(parents=True, exist_ok=True)

    # Clean filename
    safe_company = "".join(c if c.isalnum() or c in "- " else "" for c in job.company).strip().replace(" ", "_")
    safe_title = "".join(c if c.isalnum() or c in "- " else "" for c in job.title).strip().replace(" ", "_")
    filename_base = f"{safe_company}_{safe_title}"[:80]

    # Save resume .docx
    resume_doc = _create_resume_docx(resume_data, config)
    resume_path = resumes_dir / f"{filename_base}_resume.docx"
    resume_doc.save(str(resume_path))

    # Save cover letter .docx
    cover_doc = _create_cover_letter_docx(cover_text, job, config)
    cover_path = covers_dir / f"{filename_base}_cover_letter.docx"
    cover_doc.save(str(cover_path))

    logger.info(f"[tailor] Materials saved: {resume_path.name}, {cover_path.name}")

    return {
        "resume_docx": str(resume_path),
        "cover_letter_docx": str(cover_path),
    }


def tailor_queued_jobs(config: dict) -> dict:
    """Generate materials for all queued (high-scoring) jobs.

    Returns:
        Dict with results: {tailored, errors}
    """
    session = get_session()
    tailored_count = 0
    errors = []

    try:
        # Find queued jobs that need materials
        queued_apps = (
            session.query(Application)
            .filter(Application.status == ApplicationStatus.QUEUED)
            .filter(Application.resume_path.is_(None))
            .limit(10)
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
                app.status = ApplicationStatus.MATERIALS_READY
                session.commit()

                tailored_count += 1

            except Exception as e:
                session.rollback()
                error_msg = f"Error tailoring for '{job.title}' at '{job.company}': {e}"
                logger.error(f"[tailor] {error_msg}")
                errors.append(error_msg)

    finally:
        session.close()

    logger.info(f"[tailor] Tailoring complete: {tailored_count} jobs processed")
    return {"tailored": tailored_count, "errors": errors}
