"""Add auto-apply tracking columns to the `applications` table.

Adds:
- auto_applied         (bool)        : whether the bot submitted this application
- auto_apply_status    (str, nullable): "submitted", "failed", "skipped", "captcha", etc.
- auto_apply_log       (text, nullable): JSON-serialized log of what the bot did
- auto_apply_attempted_at (datetime, nullable): when the bot last tried

Idempotent: each ADD is guarded by a column-existence check.

Run from the project root:
    python -m db.migrations.002_add_auto_apply_columns
"""

import sys
from pathlib import Path

# Make the project root importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import inspect, text  # noqa: E402

from db.database import get_engine  # noqa: E402


COLUMNS_TO_ADD = (
    ("auto_applied", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("auto_apply_status", "VARCHAR(50)"),
    ("auto_apply_log", "TEXT"),
    ("auto_apply_attempted_at", "DATETIME"),
)
TABLE = "applications"


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
