"""Add hourly-comp + employment-type columns to Job (Behavioral Technician track).

Adds (to `jobs`):
- pay_period      (str, nullable)  : hourly | annual | unknown
- hourly_min      (float, nullable)
- hourly_max      (float, nullable)
- employment_type (str, nullable)  : part_time | full_time | contract | per_diem | unknown

Idempotent — each ADD is guarded by a column-existence check.

Run from the project root:
    python -m db.migrations.005_add_hourly_and_emptype
"""

import sys
from pathlib import Path

# Make the project root importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import inspect, text  # noqa: E402

from db.database import get_engine  # noqa: E402


COLUMNS_TO_ADD = (
    ("pay_period", "VARCHAR(20)"),
    ("hourly_min", "FLOAT"),
    ("hourly_max", "FLOAT"),
    ("employment_type", "VARCHAR(20)"),
)
TABLE = "jobs"


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
