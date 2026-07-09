"""Normalize company names to a canonical matching key.

"Chime Financial, Inc" and "Chime" must land on the same key so jobs
link to Company rows regardless of which scanner/ATS spelled the name.
Used by: migration 006 backfill, scanner ingest, /api/applied/record,
and the Companies rollups.
"""

import re

# Tokens dropped when they appear as TRAILING tokens (repeatedly, so
# "Global, Inc." collapses fully). Never dropped from the front/middle.
_SUFFIX_TOKENS = {
    "inc", "incorporated", "llc", "ltd", "corp", "corporation", "co",
    "company", "financial", "technologies", "technology", "labs",
    "global", "holdings", "group", "plc",
}


def normalize_company_name(name: str | None) -> str:
    if not name:
        return ""
    s = name.casefold()
    s = re.sub(r"[^\w\s]", " ", s)          # punctuation -> space
    tokens = s.split()
    while len(tokens) > 1 and tokens[-1] in _SUFFIX_TOKENS:
        tokens.pop()
    return " ".join(tokens)
