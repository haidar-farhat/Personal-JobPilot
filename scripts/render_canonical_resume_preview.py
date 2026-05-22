"""Render a preview of the tailor's output using the base_resume.yaml directly.

This SKIPS the Gemma 4 step entirely — it just runs the rendering layer of
agents/tailor.py against the canonical resume data so we can visually verify
that the layout matches Matthew's new canonical resume PDF.

Run:
    venv/Scripts/python.exe scripts/render_canonical_resume_preview.py

Output:
    output/resumes/_PREVIEW_canonical_render.docx
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from agents.tailor import _create_resume_docx  # noqa: E402


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    settings = _load_yaml(ROOT / "config" / "settings.yaml")
    resume = _load_yaml(ROOT / "config" / "base_resume.yaml")

    # Reshape the YAML into the same dict that Gemma 4 returns.
    # This mirrors the "resume_data" shape consumed by _create_resume_docx().
    resume_data = {
        "section_order": ["education", "skills", "project_experience", "work_experience", "certifications"],
        "education": resume.get("education", []),
        "skills": resume.get("technical_skills", {}),
        "project_experience": resume.get("project_experience", []),
        "work_experience": resume.get("work_experience", []),
        "certifications": resume.get("certifications", []),
    }

    doc = _create_resume_docx(resume_data, settings)

    out_dir = ROOT / "output" / "resumes"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "_PREVIEW_canonical_render.docx"
    doc.save(str(out_path))
    print(f"Rendered preview -> {out_path}")


if __name__ == "__main__":
    main()
