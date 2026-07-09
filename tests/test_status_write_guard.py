"""Guard: Application.status may only be assigned inside record_status_change.

Prevents new code from bypassing event history. Pattern-based; allowed
files are the helper itself and migrations (which run before events exist).
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ALLOWED = {"db/database.py", "db/migrations"}
PATTERN = re.compile(r"\.status\s*=\s*(ApplicationStatus|new_status|status_enum)")


def test_no_direct_status_assignments():
    offenders = []
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith(("venv/", "tests/", "output/")):
            continue
        if any(rel.startswith(a) for a in ALLOWED):
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PATTERN.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, (
        "Application.status assigned outside record_status_change:\n"
        + "\n".join(offenders))
