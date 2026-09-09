"""Report which base-résumé bullets are costing you ATS coverage, and why.

WHY THIS EXISTS
---------------
The tailor can only restate what config/base_resume.yaml already contains. It
cannot honestly add a metric that is not there. Measured 2026-09-09 on the real
file: 40 source bullets, median 12 words, 5 of 40 in the 18-30 word target, and
**2 of 40 containing any number at all**.

That is a hard ceiling on two of the four scoring categories:

  * impact_quantification (20 points) — scores numbers, scale and outcomes.
    With 5% of source bullets carrying a number, most of those 20 points are
    unreachable no matter how good the tailoring gets.
  * requirement_alignment (30 points) — a 12-word generic bullet demonstrates
    far less than a 25-word specific one.

So the highest-value remaining action is not code: it is adding real numbers and
real specifics to the base résumé. This script says exactly which bullets to fix
and what is missing from each, ranked by how much it is likely costing.

It is READ-ONLY. It never edits the résumé — the facts have to come from you,
because inventing them is precisely what this codebase now refuses to do.

Usage:
    python scripts/audit_base_resume.py
    python scripts/audit_base_resume.py --resume config/base_resume_bt.yaml
    python scripts/audit_base_resume.py --csv out.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import statistics
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TARGET_MIN, TARGET_MAX = 18, 30

# A real metric, not an incidental digit. "9th-grade" and "Python 3" are not
# achievements; "100+ students", "40% faster", "$2M" are.
METRIC_RE = re.compile(
    r"(?<![\w.])(?:"
    r"\d{1,3}(?:,\d{3})+"                      # 1,000
    r"|\$\s?\d+(?:\.\d+)?\s?[kmb]?"            # $2M
    r"|\d+(?:\.\d+)?\s?%"                      # 40%
    r"|\d+(?:\.\d+)?\s?[kmb]\b"                # 10k
    r"|\d+\s?\+"                               # 100+
    r"|\d+(?:\.\d+)?\s?x\b"                    # 3x
    r"|\d+(?:\.\d+)?\s?(?:ms|s|sec|seconds|min|minutes|hours|hrs|days|weeks|months)\b"
    r"|\d{2,}"                                 # any 2+ digit count
    r")", re.I)

# Verbs that describe activity without asserting an outcome.
WEAK_VERBS = ("worked on", "helped", "assisted", "participated", "involved in",
              "responsible for", "supported", "contributed to", "was part of")

# Openers that spend words before saying anything.
FILLER_OPENERS = ("designed and developed", "developed and implemented",
                  "built and deployed", "created and maintained",
                  "designed and implemented", "developed and maintained")


def has_metric(text: str) -> bool:
    # Exclude ordinals like "9th" and bare years like "2024" used as dates.
    cleaned = re.sub(r"\b\d+(?:st|nd|rd|th)\b", " ", text or "")
    cleaned = re.sub(r"\b(?:19|20)\d{2}\b", " ", cleaned)
    return bool(METRIC_RE.search(cleaned))


def issues(text: str) -> list[str]:
    out, words = [], len((text or "").split())
    if words < TARGET_MIN:
        out.append(f"too short ({words}w, target {TARGET_MIN}-{TARGET_MAX})")
    elif words > TARGET_MAX:
        out.append(f"too long ({words}w)")
    if not has_metric(text):
        out.append("no metric")
    low = (text or "").lower()
    for v in WEAK_VERBS:
        if low.startswith(v) or f" {v}" in low:
            out.append(f'weak verb: "{v}"')
            break
    for f in FILLER_OPENERS:
        if low.startswith(f):
            out.append(f'filler opener: "{f}"')
            break
    return out


def cost(text: str) -> int:
    """Rough ranking weight — how much this bullet is likely leaving on the table."""
    c = 0
    words = len((text or "").split())
    if not has_metric(text):
        c += 3
    if words < TARGET_MIN:
        c += 2 + (TARGET_MIN - words) // 6
    low = (text or "").lower()
    if any(low.startswith(v) or f" {v}" in low for v in WEAK_VERBS):
        c += 2
    if any(low.startswith(f) for f in FILLER_OPENERS):
        c += 1
    return c


def collect(resume: dict) -> list[dict]:
    rows = []
    for section in ("work_experience", "project_experience"):
        for entry in resume.get(section) or []:
            label = (entry.get("organization") or entry.get("title") or "?")
            title = entry.get("title") or ""
            for i, b in enumerate(entry.get("bullets") or []):
                text = b if isinstance(b, str) else str(b.get("text", ""))
                if not text.strip():
                    continue
                rows.append({
                    "section": section, "entry": label, "role": title, "index": i,
                    "words": len(text.split()), "has_metric": has_metric(text),
                    "issues": "; ".join(issues(text)), "cost": cost(text),
                    "text": text,
                })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resume", default="config/base_resume.yaml")
    ap.add_argument("--csv", help="also write the full table here")
    ap.add_argument("--top", type=int, default=12, help="bullets to print (default 12)")
    args = ap.parse_args()

    path = ROOT / args.resume if not Path(args.resume).is_absolute() else Path(args.resume)
    if not path.exists():
        print(f"not found: {path}")
        return 1
    resume = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = collect(resume)
    if not rows:
        print("no bullets found")
        return 1

    wc = [r["words"] for r in rows]
    metric_n = sum(1 for r in rows if r["has_metric"])
    in_range = sum(1 for r in rows if TARGET_MIN <= r["words"] <= TARGET_MAX)

    print(f"\n{path.name} — {len(rows)} bullets\n" + "=" * 74)
    print(f"  median length        {statistics.median(wc):.0f} words   (target {TARGET_MIN}-{TARGET_MAX})")
    print(f"  in target range      {in_range}/{len(rows)}  ({in_range / len(rows) * 100:.0f}%)")
    print(f"  carrying a metric    {metric_n}/{len(rows)}  ({metric_n / len(rows) * 100:.0f}%)"
          f"   <-- caps impact_quantification (20 pts)")

    print(f"\nHighest-cost bullets — fix these first:\n" + "-" * 74)
    for r in sorted(rows, key=lambda r: -r["cost"])[:args.top]:
        print(f"\n  [{r['entry']}] {r['issues']}")
        print(f"    {r['text']}")

    print("\n" + "=" * 74)
    print("WHAT TO ADD — only things you can actually substantiate:")
    print("  * users / records / requests / students / clients served")
    print("  * a before-and-after (latency, load time, manual hours, error rate)")
    print("  * team size, timeline, or how many services / screens / models")
    print("  * money: revenue touched, cost saved, budget handled")
    print("\nThe tailor will carry a real number into every résumé it writes and")
    print("place it in the first 12 words. It will NEVER invent one — so an empty")
    print("source bullet stays an empty résumé bullet.")

    if args.csv:
        out = Path(args.csv)
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(sorted(rows, key=lambda r: -r["cost"]))
        print(f"\nfull table -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
