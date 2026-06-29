"""Add the AI-forward signal columns to JobScore.

Adds (to `job_scores`):
- ai_intensity (int, nullable)  : 0-100, how central building-with / using AI tooling is to the role
- ai_tools     (text, nullable) : JSON list of AI tools/tech the JD mentions

Idempotent — each ADD is guarded by a column-existence check.

Run from the project root:
    python -m db.migrations.004_add_ai_intensity
"""

import sys
from pathlib import Path

# Make the project root importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import inspect, text  # noqa: E402

from db.database import get_engine  # noqa: E402


COLUMNS_TO_ADD = (
    ("ai_intensity", "INTEGER"),
    ("ai_tools", "TEXT"),   # JSON, stored as text in SQLite
)
TABLE = "job_scores"


def run() -> dict:
    engine = get_engine()
    inspector = inspect(engine)
    existing = {col["name"] for col in inspector.get_columns(TABLE)}

    added: list[str] = []
    skipped: list[str] = []

    with engine.begin() as conn:
        for col_name, col_type in COLUMNS_TO_ADD:
            if col_name in existing:
                skipped.append(col_name)
            else:
                conn.execute(text(f'ALTER TABLE {TABLE} ADD COLUMN {col_name} {col_type}'))
                added.append(col_name)

    return {"added": added, "skipped": skipped}


if __name__ == "__main__":
    result = run()
    print(f"added: {result['added']}")
    print(f"skipped (already present): {result['skipped']}")
