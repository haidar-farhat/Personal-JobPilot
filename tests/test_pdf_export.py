"""Word-backed PDF export + the one-page trim loop (skips without Word)."""

import sys
from pathlib import Path

import pytest

import agents.pdf_export as pdf_export
from agents.pdf_export import WordExporter
from agents.tailor import _create_resume_docx, _fit_one_page, _trim_one_bullet

# the keys _create_resume_docx actually renders (the LLM sidecar uses these too)
P, W = "project_experience", "work_experience"


def test_trim_cuts_the_last_project_bullet_first():
    data = {P: [{"title": "A", "bullets": ["1", "2", "3"]}, {"title": "B", "bullets": ["x", "y"]}],
            W: [{"organization": "Co", "bullets": ["e1", "e2"]}]}
    t = _trim_one_bullet(data)
    assert t[P][1]["bullets"] == ["x"]
    assert data[P][1]["bullets"] == ["x", "y"], "input must not be mutated"


def test_trim_falls_through_to_experience_then_whole_project_then_none():
    data = {P: [{"bullets": ["a"]}, {"bullets": ["b"]}, {"bullets": ["c"]}],
            W: [{"bullets": ["e1", "e2"]}]}
    t1 = _trim_one_bullet(data)
    assert t1[W][0]["bullets"] == ["e1"]
    t2 = _trim_one_bullet(t1)
    assert len(t2[P]) == 2
    assert _trim_one_bullet(t2) is None


class _FakeWord:
    """Stands in for Word: reports 2 pages until one line has been cut."""
    pages = [2, 1]

    def __init__(self):
        self.exports = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    available = True

    def export(self, docx, pdf):
        Path(pdf).write_bytes(b"%PDF-fake")
        self.exports += 1
        return self.pages[min(self.exports, len(self.pages)) - 1]


def test_fit_one_page_trims_until_word_says_one_page(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_export, "WordExporter", _FakeWord)
    config = {"user": {"name": "Test Person", "email": "t@x.com", "phone": "555", "location": "Oakland, CA"}}
    data = {"section_order": [P, W],
            P: [{"title": "Proj", "tech_stack": "Python", "bullets": ["one", "two", "three"]}],
            W: [{"title": "Analyst", "organization": "Co", "dates": "2024", "location": "CA", "bullets": ["did"]}]}
    docx = tmp_path / "x_resume.docx"
    _create_resume_docx(data, config).save(str(docx))
    out, pdf, pages = _fit_one_page(data, config, docx)
    assert pages == 1 and pdf == docx.with_suffix(".pdf") and pdf.exists()
    assert out[P][0]["bullets"] == ["one", "two"], "one bullet cut, then Word said one page"
    assert data[P][0]["bullets"] == ["one", "two", "three"], "caller's data untouched"


@pytest.mark.skipif(sys.platform != "win32", reason="Word COM is Windows-only")
def test_word_export_reports_pages(tmp_path):
    from docx import Document
    d = Document()
    d.add_paragraph("one page")
    src = tmp_path / "x.docx"
    d.save(str(src))
    with WordExporter() as w:
        if not w.available:
            pytest.skip("Word not installed")
        pages = w.export(src, tmp_path / "x.pdf")
    assert pages == 1
    assert (tmp_path / "x.pdf").stat().st_size > 500
