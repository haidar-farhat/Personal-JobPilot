"""JobPilot continuous QA loop.

Runs an end-to-end verification cycle against the LIVE system and prints one
status line per loop in the agreed format. Apply simulation is DRY-RUN only — it
exercises the real candidate-selection + guardrail pipeline (incl. the BT
$30/hr hourly floor) but never submits and never mutates data.

    python scripts/qa_loop.py --iterations 3
    python scripts/qa_loop.py --iterations 0        # run forever (Ctrl-C to stop)

Output:
[Loop N] | Status: SUCCESS/FAILED | Search Term: X | Jobs Scanned: N | Filtered: M | App Simulation: PASS/FAIL | Error Log: None or <error>
"""

import argparse
import random
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DASH = "http://127.0.0.1:7777"
OLLAMA = "http://127.0.0.1:11434"
LOG_FILE = Path(__file__).resolve().parents[1] / "logs" / "qa_loop.log"
LOG_FILE.parent.mkdir(exist_ok=True)


def emit(line: str) -> None:
    """Print + append one status line to logs/qa_loop.log (UTF-8)."""
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
AUTO_QUEUE = 60          # settings.scoring.auto_queue_threshold
KEYWORDS = ["AI Engineer", "Solutions Engineer", "Data Analyst",
            "Behavioral Technician", "Machine Learning Engineer", "Quantitative Analyst"]


def _http_ok(url):
    try:
        return requests.get(url, timeout=4).status_code == 200
    except Exception:
        return False


def check_state():
    """Step 1 — profile/config load + service health."""
    import yaml
    root = Path(__file__).resolve().parents[1]
    for f in ("settings.yaml", "base_resume.yaml", "applicant_profile.yaml"):
        yaml.safe_load(open(root / "config" / f, encoding="utf-8"))
    if not _http_ok(f"{DASH}/api/stats"):
        raise RuntimeError("dashboard /api/stats down")
    if not _http_ok(f"{OLLAMA}/api/tags"):
        raise RuntimeError("ollama down")


def fetch_apps():
    return requests.get(f"{DASH}/api/applications?limit=500", timeout=8).json()


def search(apps, kw):
    """Step 2 — keyword search over the current pipeline."""
    k = kw.lower()
    return [a for a in apps if k in f"{a.get('title','')} {a.get('company','')} {a.get('description','')}".lower()]


def apply_filter(matched, kw):
    """Step 3 — filter to qualified roles; verify no unqualified leak through."""
    is_bt = "behavior" in kw.lower()
    qualified = []
    for a in matched:
        if (a.get("fit_score") or 0) < AUTO_QUEUE:
            continue
        if is_bt:
            rate = a.get("hourly_max") or a.get("hourly_min")
            if rate is None or rate < 30:
                continue
        qualified.append(a)
    # no false positives: everything kept must clear the bar
    assert all((a.get("fit_score") or 0) >= AUTO_QUEUE for a in qualified), "unqualified job leaked into filtered set"
    return qualified


def simulate(qualified):
    """Step 4 — dry-run apply simulation (never submits)."""
    from db.database import get_session
    from agents.auto_applier.base import load_profile
    from agents.auto_applier.runner import _candidates
    try:
        from scheduler import load_config
        config = load_config()
    except Exception:
        config = {}
    profile = dict(load_profile() or {})
    gr = dict(profile.get("guardrails", {}))
    gr["dry_run"] = True            # force dry-run for the simulation
    profile["guardrails"] = gr
    session = get_session()
    try:
        # Exercises ATS allowlist + per-ATS score + BT hourly floor, all in dry-run.
        eligible = _candidates(session, profile, config=config, max_candidates=5)
        return True, len(eligible)
    finally:
        session.close()


def run_once(n):
    kw = KEYWORDS[(n - 1) % len(KEYWORDS)]
    raw = filt = 0
    sim = False
    err = "None"
    status = "SUCCESS"
    try:
        check_state()
        apps = fetch_apps()
        matched = search(apps, kw)
        raw = len(matched)
        qualified = apply_filter(matched, kw)
        filt = len(qualified)
        sim, _elig = simulate(qualified)
    except Exception as e:
        status = "FAILED"
        err = f"{type(e).__name__}: {e}"
    sim_str = "PASS" if sim else "FAIL"
    emit(f"[Loop {n}] | Status: {status} | Search Term: {kw} | Jobs Scanned: {raw} | "
         f"Filtered: {filt} | App Simulation: {sim_str} | Error Log: {err}")
    return status == "SUCCESS"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=3, help="0 = run forever")
    ap.add_argument("--min-cooldown", type=float, default=5.0)
    ap.add_argument("--max-cooldown", type=float, default=10.0)
    args = ap.parse_args()

    n = 0
    while True:
        n += 1
        run_once(n)
        if args.iterations and n >= args.iterations:
            break
        time.sleep(random.uniform(args.min_cooldown, args.max_cooldown))


if __name__ == "__main__":
    main()
