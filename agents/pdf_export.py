"""docx → PDF through Microsoft Word (COM), returning Word's own page count so
the tailor can VERIFY "one page" instead of guessing from bullet caps.

Windows + Word only. Everywhere else (or when Word misbehaves) every call
degrades to None and callers keep shipping the .docx.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

WD_FORMAT_PDF = 17
WD_STATISTIC_PAGES = 2


class WordExporter:
    """One Word instance for several exports — each launch costs ~2 s."""

    def __init__(self):
        self._word = None
        self._com = None

    def __enter__(self):
        if sys.platform != "win32":
            return self
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()   # we run inside asyncio.to_thread / the scheduler thread
            self._com = pythoncom
            self._word = win32com.client.DispatchEx("Word.Application")
            self._word.Visible = False
            self._word.DisplayAlerts = 0
        except Exception as e:
            logger.warning(f"[pdf] Word unavailable — shipping .docx only: {e}")
            self._word = None
        return self

    def __exit__(self, *exc):
        try:
            if self._word is not None:
                self._word.Quit()
        except Exception:
            pass
        try:
            if self._com is not None:
                self._com.CoUninitialize()
        except Exception:
            pass
        self._word = None

    @property
    def available(self) -> bool:
        return self._word is not None

    def export(self, docx_path, pdf_path) -> int | None:
        """Write the PDF and return Word's page count for the document, or None."""
        if self._word is None:
            return None
        try:
            doc = self._word.Documents.Open(str(Path(docx_path).resolve()), ReadOnly=True,
                                            AddToRecentFiles=False)
            try:
                pages = int(doc.ComputeStatistics(WD_STATISTIC_PAGES))
                doc.ExportAsFixedFormat(OutputFileName=str(Path(pdf_path).resolve()),
                                        ExportFormat=WD_FORMAT_PDF)
            finally:
                doc.Close(0)
            return pages
        except Exception as e:
            logger.warning(f"[pdf] export failed for {docx_path}: {e}")
            return None


def docx_to_pdf(docx_path, pdf_path) -> int | None:
    with WordExporter() as w:
        return w.export(docx_path, pdf_path)
