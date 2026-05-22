"""One-shot script to update the 'Autonomous Job Search Agent' section in
Matthew's real resume to reflect what JobPilot actually is — not the
LinkedIn/Indeed/Computer Use/Google Sheets fiction the original draft had.

Run from project root:
    python scripts/update_resume_jobpilot_section.py
"""

from __future__ import annotations

from pathlib import Path
from copy import deepcopy

from docx import Document
from docx.oxml.ns import qn

RESUME_PATH = Path(r"C:\Users\matth\Downloads\Resumes\Matthew_Cromaz_Resume iteration 2 (edited).docx")
OUTPUT_PATH = Path(r"C:\Users\matth\Downloads\Resumes\Matthew_Cromaz_Resume_v3_jobpilot_accurate.docx")


# What JobPilot actually is (verified against the codebase + git history)
NEW_PROJECT_NAME = "Autonomous Job Search Agent (JobPilot)"
NEW_TECH_STACK = "Python, FastAPI, Playwright, Ollama (Gemma 4 27B), SQLAlchemy, APScheduler"

NEW_BULLETS = [
    "Built a local-first autonomous job pipeline that scans Greenhouse / Lever / Ashby ATS APIs every 30 minutes, "
    "scores each posting 0–100 against my profile using a local Gemma 4 27B model via Ollama, and generates "
    "ATS-optimized .docx resumes and cover letters tailored per role.",

    "Implemented a Playwright auto-submitter for Greenhouse, Ashby, Lever, Workday, and a heuristic generic-ATS "
    "fallback, with strict guardrails (per-ATS score thresholds, daily caps, CAPTCHA + login-wall bail, max-essay "
    "caps, full audit logs persisted to SQLite) so bad submissions never reach recruiters.",

    "Built a live FastAPI + Server-Sent-Events dashboard (cinematic glass UI, animated nebula backdrop) showing "
    "the full scan→score→tailor→submit pipeline in real time, with 54 passing pytest + Playwright e2e tests "
    "covering API contracts, dashboard render, SSE stream, DB integrity, and applier dispatch logic.",
]


def main():
    if not RESUME_PATH.exists():
        raise SystemExit(f"Resume not found at {RESUME_PATH}")

    doc = Document(str(RESUME_PATH))

    # 1. Locate the project header paragraph by its bold "Autonomous Job Search Agent" run
    project_idx = None
    for i, p in enumerate(doc.paragraphs):
        text = p.text.strip()
        if text.startswith("Autonomous Job Search Agent"):
            project_idx = i
            break

    if project_idx is None:
        raise SystemExit("Could not find 'Autonomous Job Search Agent' header in resume")

    print(f"Found project header at paragraph index {project_idx}: {doc.paragraphs[project_idx].text[:80]}")

    # 2. Identify the bullet paragraphs that follow until we hit the next bold header
    #    (style usually 'List Paragraph')
    bullet_indices: list[int] = []
    j = project_idx + 1
    while j < len(doc.paragraphs):
        p = doc.paragraphs[j]
        style_name = p.style.name if p.style else ""
        # Stop when we reach the next non-list paragraph that has bold content (next project / section)
        if style_name != "List Paragraph":
            break
        bullet_indices.append(j)
        j += 1

    print(f"Found {len(bullet_indices)} existing bullet paragraphs (indices: {bullet_indices})")

    # 3. Capture the styling of an existing bullet so we can clone for new ones
    if not bullet_indices:
        raise SystemExit("No existing bullet paragraphs found to use as styling template")
    template_bullet_idx = bullet_indices[0]
    template_bullet = doc.paragraphs[template_bullet_idx]
    bullet_style = template_bullet.style

    # Capture font properties from the first run of the template bullet
    template_run = template_bullet.runs[0] if template_bullet.runs else None
    template_font_name = template_run.font.name if template_run else None
    template_font_size = template_run.font.size if template_run else None

    # 4. Replace the project header paragraph's runs with the new project name + tech stack
    header_para = doc.paragraphs[project_idx]
    # Strip all existing runs
    for run in list(header_para.runs):
        run._element.getparent().remove(run._element)

    name_run = header_para.add_run(NEW_PROJECT_NAME)
    name_run.bold = True
    if template_font_name:
        name_run.font.name = template_font_name

    tab_run = header_para.add_run("\t")

    stack_run = header_para.add_run(NEW_TECH_STACK)
    stack_run.italic = True
    if template_font_name:
        stack_run.font.name = template_font_name

    print(f"Updated header to: {header_para.text[:100]}")

    # 5. Replace bullet content
    # Strategy: edit the existing bullet paragraphs in-place where possible,
    # then add or remove bullets to match NEW_BULLETS count.
    for idx, bullet_text in enumerate(NEW_BULLETS):
        if idx < len(bullet_indices):
            # Reuse existing bullet paragraph
            target = doc.paragraphs[bullet_indices[idx]]
            for run in list(target.runs):
                run._element.getparent().remove(run._element)
            new_run = target.add_run(bullet_text)
            if template_font_name:
                new_run.font.name = template_font_name
            if template_font_size:
                new_run.font.size = template_font_size
            print(f"  Updated bullet {idx + 1}: {bullet_text[:80]}…")
        else:
            # Need to add a new bullet — clone the styling of template_bullet
            template_xml = deepcopy(template_bullet._element)
            # Remove all runs from the cloned paragraph
            for r in list(template_xml.findall(qn("w:r"))):
                template_xml.remove(r)
            # Insert it after the last current bullet (or after project header if no bullets)
            anchor_idx = bullet_indices[-1] if bullet_indices else project_idx
            anchor = doc.paragraphs[anchor_idx]._element
            anchor.addnext(template_xml)
            # Now create a new Paragraph wrapper around the inserted xml so we can use the API
            from docx.text.paragraph import Paragraph
            new_para = Paragraph(template_xml, anchor.getparent())
            new_run = new_para.add_run(bullet_text)
            if template_font_name:
                new_run.font.name = template_font_name
            if template_font_size:
                new_run.font.size = template_font_size
            bullet_indices.append(anchor_idx + (idx - len(bullet_indices) + 1))  # rough tracking
            print(f"  Added bullet {idx + 1}: {bullet_text[:80]}…")

    # 6. If there are extra existing bullets beyond what we need, remove them
    excess = len(bullet_indices) - len(NEW_BULLETS)
    if excess > 0:
        for old_idx in bullet_indices[len(NEW_BULLETS):]:
            old_para = doc.paragraphs[old_idx]
            old_para._element.getparent().remove(old_para._element)
            print(f"  Removed extra bullet at index {old_idx}")

    # 7. Save back to disk (write to new file so we don't fight a Word lock)
    try:
        doc.save(str(RESUME_PATH))
        print(f"\nSaved updated resume in place: {RESUME_PATH}")
    except PermissionError:
        doc.save(str(OUTPUT_PATH))
        print(f"\n[note] Source file was locked (open in Word). Wrote new copy to:\n  {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
