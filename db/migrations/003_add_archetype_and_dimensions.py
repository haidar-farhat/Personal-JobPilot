"""Add archetype + multi-dimensional scoring + evaluation report path to JobScore.

Adds (all to `job_scores`):
- archetype          (str, nullable)  : one of data_analyst, data_scientist, quantitative_analyst,
                                        ml_engineer, business_analyst, product_analyst, unknown
- archetype_confidence (float, nullable): LLM-reported 0.0-1.0 confidence in classification
- dimensions         (text, nullable)  : JSON of {dim_name: 0-100 score}
- dimension_weights  (text, nullable)  : JSON of {dim_name: weight} (the archetype's weights frozen at scoring time)
- evaluation_path    (str, nullable)   : path to the 6-block markdown evaluation file

Idempotent — each ADD is guarded by a column-existence check.

Run from the project root:
    python -m db.migrations.003_add_archetype_and_dimensions
"""

import sys
from pathlib import Path

# Make the project root importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import inspect, text  # noqa: E402

from db.database import get_engine  # noqa: E402


COLUMNS_TO_ADD = (
    ("archetype", "VARCHAR(50)"),
    ("archetype_confidence", "FLOAT"),
    ("dimensions", "TEXT"),            # JSON, stored as text in SQLite
    ("dimension_weights", "TEXT"),     # JSON, stored as text in SQLite
    ("evaluation_path", "VARCHAR(1000)"),
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
