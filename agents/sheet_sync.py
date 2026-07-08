"""Pure, network-free logic for syncing applied jobs to a Google Sheet.

No gspread / IO lives here — only normalization, adaptive header mapping,
dedup matching, and row building. ``server/sheets.py`` wires this to the live
worksheet. Keeping it pure makes the tricky parts (matching, header mapping)
fully unit-testable offline.
"""

import re

# Application statuses that count as "things I've applied for" — mirrors the
# dashboard's existing "applied" filter group.
APPLIED_STATUSES = ["applied", "interview", "response_received", "rejected", "no_response"]

# Human-friendly status labels for the sheet (title-case, unlike the dashboard's
# uppercase pills).
STATUS_LABELS = {
    "found": "Found",
    "scored": "Scored",
    "queued": "Queued",
    "materials_ready": "Materials Ready",
    "approved": "Approved",
    "applied": "Applied",
    "response_received": "Response Received",
    "interview": "Interview",
    "rejected": "Rejected",
    "no_response": "No Response",
    "no_longer_available": "No Longer Available",
    "skipped": "Skipped",
}

# (field_key, header_label, [synonyms]) — order defines the canonical sheet layout
# written to an empty sheet, and the priority when matching a field to a column.
CANONICAL_COLUMNS = [
    ("date_applied", "Date Applied", ["date applied", "applied", "date", "applied on", "application date"]),
    ("company",      "Company",      ["company", "employer", "organization", "organisation", "org"]),
    ("title",        "Title",        ["title", "role", "position", "job title", "job", "job role"]),
    ("location",     "Location",     ["location", "city", "place", "where", "area"]),
    ("pay",          "Pay",          ["pay", "salary", "comp", "compensation", "rate", "pay rate", "wage"]),
    ("source",       "Source",       ["source", "via", "site", "board", "job board", "platform"]),
    ("status",       "Status",       ["status", "stage", "result", "outcome", "progress"]),
    ("fit",          "Fit",          ["fit", "score", "fit score", "match", "match score"]),
    ("url",          "URL",          ["url", "link", "job link", "posting", "application link", "apply link", "job url", "listing"]),
    ("notes",        "Notes",        ["notes", "note", "comments", "comment", "remarks"]),
]

_FIELD_KEYS = [c[0] for c in CANONICAL_COLUMNS]


def normalize(s):
    """Lowercase, strip punctuation to spaces, collapse whitespace."""
    if not s:
        return ""
    s = re.sub(r"[^a-z0-9]+", " ", str(s).lower())
    return re.sub(r"\s+", " ", s).strip()


def normalize_url(url):
    """Canonicalize a URL for matching: drop scheme + leading www + trailing
    slash, lowercase. Query string is preserved — it's the identity for many
    ATS postings (e.g. ?gh_jid=123)."""
    if not url:
        return ""
    u = str(url).strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def name_key(company, title):
    """Normalized company+title identity, or '' if both are empty."""
    c, t = normalize(company), normalize(title)
    if not c and not t:
        return ""
    return f"{c} :: {t}"


def status_label(status_key):
    """Human label for the sheet's Status column."""
    if status_key in STATUS_LABELS:
        return STATUS_LABELS[status_key]
    return (status_key or "").replace("_", " ").title()


def _tokens(s):
    return normalize(s).split()


def _phrase_match(syn_tokens, hdr_tokens):
    """True if syn_tokens appear as a consecutive run inside hdr_tokens.

    Token-based (not substring) so 'date' never matches 'candidate'.
    """
    n = len(syn_tokens)
    if n == 0:
        return False
    for i in range(len(hdr_tokens) - n + 1):
        if hdr_tokens[i:i + n] == syn_tokens:
            return True
    return False


def map_headers(header_row):
    """Map JobPilot field keys -> 0-based column index in the user's sheet.

    Each sheet column maps to at most one field; the first field (in canonical
    order) whose label/synonym matches the header wins. Unmappable headers are
    ignored. Returns ``{field_key: col_index}``.
    """
    mapping = {}
    used = set()
    for idx, raw in enumerate(header_row or []):
        ht = _tokens(raw)
        if not ht:
            continue
        for field, label, syns in CANONICAL_COLUMNS:
            if field in used:
                continue
            candidates = list(syns) + [label]
            if any(_phrase_match(_tokens(s), ht) for s in candidates):
                mapping[field] = idx
                used.add(field)
                break
    return mapping


def default_header():
    """Canonical header row — written only when the sheet has no headers."""
    return [c[1] for c in CANONICAL_COLUMNS]


def canonical_header_map():
    """Field -> column index for the canonical (default) header layout."""
    return {field: i for i, field in enumerate(_FIELD_KEYS)}


def build_row(app, header_map, ncols):
    """Build a cell list of length ``ncols`` for ``app``, placing each field's
    value at ``header_map[field]`` and leaving unmapped columns blank."""
    row = [""] * ncols
    fit = app.get("fit")
    values = {
        "date_applied": app.get("date_applied") or "",
        "company": app.get("company") or "",
        "title": app.get("title") or "",
        "location": app.get("location") or "",
        "pay": app.get("pay") or "",
        "source": app.get("source") or "",
        "status": app.get("status_label") or "",
        "fit": "" if fit is None else str(fit),
        "url": app.get("url") or "",
        "notes": app.get("notes") or "",
    }
    for field, idx in header_map.items():
        if 0 <= idx < ncols:
            row[idx] = values.get(field, "")
    return row


def _cell(row, idx):
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return row[idx]


def compare(apps, data_rows, header_map):
    """Diff applied applications against existing sheet rows.

    ``data_rows`` are the worksheet rows *excluding* the header. Matching: an
    app is "in_sheet" if its canonicalized URL equals a sheet row's URL, or its
    normalized company+title equals a sheet row's. Returns
    ``{new, in_sheet, orphans}`` where new/in_sheet are the app dicts and
    orphans is the count of sheet rows with no JobPilot match.
    """
    url_idx = header_map.get("url")
    company_idx = header_map.get("company")
    title_idx = header_map.get("title")

    sheet_urls, sheet_names, row_keys = set(), set(), []
    for r in data_rows or []:
        uk = normalize_url(_cell(r, url_idx))
        nk = name_key(_cell(r, company_idx), _cell(r, title_idx))
        if uk:
            sheet_urls.add(uk)
        if nk:
            sheet_names.add(nk)
        row_keys.append((uk, nk))

    app_urls, app_names = set(), set()
    new, in_sheet = [], []
    for a in apps or []:
        uk = normalize_url(a.get("url"))
        nk = name_key(a.get("company"), a.get("title"))
        if uk:
            app_urls.add(uk)
        if nk:
            app_names.add(nk)
        present = (uk and uk in sheet_urls) or (nk and nk in sheet_names)
        (in_sheet if present else new).append(a)

    orphans = 0
    for uk, nk in row_keys:
        if not (uk or nk):
            continue
        matched = (uk and uk in app_urls) or (nk and nk in app_names)
        if not matched:
            orphans += 1

    return {"new": new, "in_sheet": in_sheet, "orphans": orphans}


def format_pay(salary_text=None, hourly_min=None, hourly_max=None,
               salary_min=None, salary_max=None, pay_period=None):
    """Human pay string: prefer salary_text, then hourly range, then annual range."""
    if salary_text:
        return str(salary_text)

    def money(x):
        return f"${int(round(x)):,}" if x is not None else None

    if hourly_min or hourly_max:
        parts = [p for p in (money(hourly_min), money(hourly_max)) if p]
        return f"{' – '.join(parts)}/hr" if parts else ""
    if salary_min or salary_max:
        parts = [p for p in (money(salary_min), money(salary_max)) if p]
        return " – ".join(parts) if parts else ""
    return ""
