"""Drop unused columns from the `applications` table.

Removes columns that the codebase no longer references:
- resume_pdf_path        : superseded by resume_path (we ship .docx, not .pdf)
- cover_letter_pdf_path  : superseded by cover_letter_path
- sheets_row_id          : Google Sheets export was never wired up

Idempotent: each DROP is guarded by a column-existence check so re-running is a no-op.
SQLite >= 3.35 (Python 3.10+ ships this) supports ALTER TABLE DROP COLUMN.

Run from the project root:
    python -m db.migrations.001_drop_unused_application_columns
"""

import sys
from pathlib import Path

# Make the project root importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import inspect, text  # noqa: E402

from db.database import get_engine  # noqa: E402


COLUMNS_TO_DROP = (
    "resume_pdf_path",
    "cover_letter_pdf_path",
    "sheets_row_id",
)
TABLE = "applications"


def run() -> dict:
    engine = get_engine()
    inspector = inspect(engine)
    existing = {col["name"] for col in inspector.get_columns(TABLE)}

    dropped: list[str] = []
    skipped: list[str] = []

    with engine.begin() as conn:
        for col in COLUMNS_TO_DROP:
            if col in existing:
                conn.execute(text(f'ALTER TABLE {TABLE} DROP COLUMN {col}'))
                dropped.append(col)
            else:
                skipped.append(col)

    return {"dropped": dropped, "skipped": skipped}


if __name__ == "__main__":
    result = run()
    print(f"dropped: {result['dropped']}")
    print(f"skipped (already gone): {result['skipped']}")
