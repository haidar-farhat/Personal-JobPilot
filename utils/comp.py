"""Compensation + employment-type parsing.

Used by the Behavioral Technician track, where pay is quoted hourly (e.g.
"$28–$35 an hour") rather than as an annual salary. parse_hourly returns an
(min, max) hourly tuple ONLY when an explicit hourly unit is present, so annual
salaries never get misread as hourly.
"""

import re

# Range: "$28 - $35 an hour" / "$28–$35/hr" / "24 to 34 hourly"
_HOURLY_RANGE_RE = re.compile(
    r"\$?\s*(\d{1,3}(?:\.\d{1,2})?)\s*(?:-|–|—|to)\s*\$?\s*(\d{1,3}(?:\.\d{1,2})?)"
    r"\s*(?:/\s*hr|/\s*hour|per\s*hour|an\s*hour|hourly|hr\b)",
    re.IGNORECASE,
)

# Single: "$30/hr" / "$32.50 per hour" / "30/hr"
_HOURLY_SINGLE_RE = re.compile(
    r"\$?\s*(\d{1,3}(?:\.\d{1,2})?)\s*(?:/\s*hr|/\s*hour|per\s*hour|an\s*hour|hourly|hr\b)",
    re.IGNORECASE,
)


def parse_hourly(text: str | None) -> tuple[float, float] | None:
    """Parse an hourly pay rate from free text.

    Returns (min, max) as floats, or None if no explicit hourly rate is found.
    Annual salaries (e.g. "$120,000/yr") return None by design.
    """
    if not text:
        return None
    s = str(text)

    m = _HOURLY_RANGE_RE.search(s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return (lo, hi)

    m = _HOURLY_SINGLE_RE.search(s)
    if m:
        v = float(m.group(1))
        return (v, v)

    return None


def parse_employment_type(title: str | None, description: str | None = "") -> str:
    """Classify employment type from title + description.

    Returns one of: part_time | full_time | contract | per_diem | unknown.
    Priority order favors the more specific/flexible arrangements first.
    """
    t = f"{title or ''} {description or ''}".lower()
    if "per diem" in t or "per-diem" in t:
        return "per_diem"
    if "part-time" in t or "part time" in t or "part-time/" in t:
        return "part_time"
    if "contract" in t or "contractor" in t or "temporary" in t or "temp position" in t:
        return "contract"
    if "full-time" in t or "full time" in t:
        return "full_time"
    return "unknown"
