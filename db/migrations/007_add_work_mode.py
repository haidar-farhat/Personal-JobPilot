"""Add `work_mode` to Job — the three-way remote/hybrid/onsite the boolean can't hold.

`is_remote` is a Boolean with a False default, so a stored False is ambiguous:
it means "we determined this is not remote" and "nobody ever looked" equally,
and it cannot express hybrid at all. Measured over 223 rows with real
descriptions, hybrid is the LARGEST group — 62 hybrid vs 23 remote vs 20 onsite —
so collapsing it into the boolean loses the most common answer.

`work_mode` is NULL when undetermined, which is the distinction the filter needs:
"onsite" and "not established" must not look the same in a facet count.

Idempotent — the ADD is guarded by a column-existence check.

Run from the project root:
    python -m db.migrations.007_add_work_mode
"""

import sys
from pathlib import Path

# Make the project root importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import inspect, text  # noqa: E402

from db.database import get_engine  # noqa: E402


COLUMNS_TO_ADD = (
    ("work_mode", "VARCHAR(20)"),
)
TABLE = "jobs"
INDEXES = (
    ("idx_jobs_work_mode", "work_mode"),
)


def run() -> dict:
    engine = get_engine()
    inspector = inspect(engine)
    existing = {col["name"] for col in inspector.get_columns(TABLE)}
    existing_indexes = {ix["name"] for ix in inspector.get_indexes(TABLE)}

    added: list[str] = []
    skipped: list[str] = []
    indexed: list[str] = []

    with engine.begin() as conn:
        for col_name, col_type in COLUMNS_TO_ADD:
            if col_name in existing:
                skipped.append(col_name)
            else:
                conn.execute(text(f'ALTER TABLE {TABLE} ADD COLUMN {col_name} {col_type}'))
                added.append(col_name)
        # create_all does not add an index to a table that already exists, so
        # the index is created here explicitly (same wrinkle as migration 006).
        for ix_name, col_name in INDEXES:
            if ix_name not in existing_indexes:
                conn.execute(text(f'CREATE INDEX {ix_name} ON {TABLE} ({col_name})'))
                indexed.append(ix_name)

    return {"added": added, "skipped": skipped, "indexed": indexed}


if __name__ == "__main__":
    result = run()
    print(f"added: {result['added']}")
    print(f"skipped (already present): {result['skipped']}")
    print(f"indexes created: {result['indexed']}")
