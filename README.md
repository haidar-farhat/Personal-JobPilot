# Personal JobPilot

> A fully local, autonomous job-search agent. Scrapes ATS boards every 30 minutes, classifies each role into one of 6 career archetypes, scores it across 10 weighted dimensions with a local LLM, generates an ATS-clean tailored resume + cover letter, and queues high-fit roles for one-click submission — all on a single laptop, $0/month.

[![Python](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-D71F00)](https://www.sqlalchemy.org/)
[![Ollama](https://img.shields.io/badge/Ollama-Gemma--4_27B-000000)](https://ollama.com/)
[![Playwright](https://img.shields.io/badge/Playwright-Auto--apply-2EAD33?logo=playwright)](https://playwright.dev/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

---

## Why I built this

I'm a Bay Area data scientist looking for my next role. Applying to jobs is a slow, manual, soul-crushing loop: scroll boards, read JDs, decide if it's worth the effort, hand-tailor a resume, paste 30 form fields, repeat. It scales like O(n) human-hours.

So I built the system I wished existed — one that runs on my own hardware, uses no paid APIs, respects my data, and turns the job hunt into a `cron`-scheduled background process I can supervise from a single dashboard. The same engineering patterns I'd use to ship a production ML pipeline at work, applied to my own problem.

This repo is the result. It's been running 24/7 for several weeks and has:

- Scanned **130+ jobs** across Greenhouse, Lever, Ashby, and 40+ company career pages
- Scored every one on a **10-dimension archetype-weighted rubric** via local Gemma-4 27B
- Generated **48 ATS-clean tailored resumes + cover letters** as ready-to-send `.docx` files
- Auto-submitted **3 applications** via Playwright (with strict guardrails — never CAPTCHAs, never paid jobs)

---

## Architecture

```
                         ┌──────────────────────────────┐
                         │   APScheduler (cron loop)    │
                         └──────────────┬───────────────┘
                                        │
            ┌───────────────┬───────────┼───────────┬───────────────┐
            ▼               ▼           ▼           ▼               ▼
        Greenhouse        Lever       Ashby     Indeed RSS    Career pages
            │               │           │           │               │
            └───────────────┴─────┬─────┴───────────┴───────────────┘
                                  │ dedup hash
                                  ▼
                        ┌──────────────────┐
                        │   SQLite (jobs)  │  ◄── single source of truth
                        └────────┬─────────┘
                                 │
                ┌────────────────┼────────────────┐
                ▼                ▼                ▼
     ┌──────────────────┐ ┌────────────┐ ┌────────────────┐
     │ Stage 1: classify│ │ Stage 2:   │ │ Stage 3: write │
     │   archetype      │ │ score 10   │ │ 6-block eval   │
     │   + confidence   │ │ dimensions │ │ report (.md)   │
     └────────┬─────────┘ └─────┬──────┘ └────────┬───────┘
              └──────────┬──────┴─────────────────┘
                         ▼
                   Gemma-4 27B (Ollama, local, ~50 tok/s)
                         │
                         ▼
              ┌─────────────────────┐
              │  Resume Tailor      │  ──►  output/resumes/*.docx
              │  Cover Letter Gen   │  ──►  output/cover_letters/*.docx
              └─────────┬───────────┘
                        ▼
              ┌────────────────────┐         ┌──────────────────┐
              │  FastAPI dashboard │ ◄────►  │ Playwright       │
              │  + SSE live feed   │         │ auto-applier     │
              └────────────────────┘         └──────────────────┘
                http://127.0.0.1:7777
```

**Single SQLite database** is the source of truth. Every component reads/writes it independently — scanner, ranker, tailor, dashboard, auto-applier — so nothing is coupled and any one piece can be restarted in isolation.

---

## Engineering highlights

### 3-stage LLM pipeline (not one mega-prompt)

Most job-scoring projects throw the entire job description + resume into one massive prompt and ask the model for a JSON blob. That works in demos but in practice: hallucinations, inconsistent dimensions, and no way to debug why a job scored 67 vs 72.

I broke it into three focused LLM calls, each with a narrow contract:

| Stage | Input | Output | Why split |
|---|---|---|---|
| **1. Classify** | Title + JD | `{archetype, confidence, reasoning}` | One decision. Model isn't distracted. Lets us route to a tailored scoring rubric. |
| **2. Score dimensions** | JD + archetype + resume summary | `{10 dim scores, matches, gaps, ats_keywords}` | Constrained 10-dim schema. Each dim gets full attention. |
| **3. Write evaluation** | All of the above | 6-block markdown audit | Generates a human-readable report stored on disk — full audit trail. |

The weighted overall score is computed **in deterministic Python** from the dim scores, not by the LLM. This makes scoring reproducible, auditable, and instantly tunable by editing one yaml file.

### Archetype-routed scoring (one rubric per role type)

`config/archetypes.yaml` defines 6 archetypes — `data_analyst`, `data_scientist`, `quantitative_analyst`, `ml_engineer`, `business_analyst`, `product_analyst` — plus an `unknown` fallback. Each one ships with:

- A **dimension-weight vector** (10 weights summing to 1.0) so the same dim scores produce different overall fit depending on role type. A "comp_range" miss matters more for a Quant Analyst than a Business Analyst.
- An **auto-apply threshold** (`auto_apply_min_score`) — e.g. ML Engineer roles need 80+ before the bot will auto-submit; Data Analyst is 72+. Tuned per archetype because the cost of a bad ML Eng application is higher.

```yaml
quantitative_analyst:
  weights:
    technical_fit: 0.16
    archetype_fit: 0.18   # highest — quant work demands the niche
    comp_range:    0.10
    skills_overlap:0.14
    ...
  auto_apply_min_score: 73
```

Adding a new archetype means editing one yaml file. No code change, no migration, no model retraining.

### ATS-clean .docx generation that actually passes the parser

The resume is generated with `python-docx` using design choices borrowed from Jake Gutierrez's LaTeX template, then verified against the major ATS parsers (Greenhouse, Workday, Lever):

- Single-column layout — no tables, no text boxes, no headers/footers (all three break ATS parsers)
- Small-caps section headers via the raw `<w:smallCaps val="1"/>` XML element (python-docx doesn't expose this)
- Two-column tab stops at 7.6" so dates right-align to the same pixel on every line
- Garamond at 10pt body, 1.08 line spacing — hits one page for any reasonable amount of experience
- Hanging indents on bullets so wrapped lines don't break visual hierarchy

The same module renders a matching cover letter from a tailored prompt — same fonts, same margins, same brand.

### Live dashboard with server-sent events

The dashboard at `http://127.0.0.1:7777` is a single-file static HTML + vanilla JS app served by FastAPI. No build step, no React, no bundle — but it gets live updates via SSE: scan completions, new scores, application status changes all stream into the UI in real time.

Each application row opens into a drawer that shows:
- The full 10-dimension score grid with color-coded bars
- An archetype badge + threshold banner ("Below 73 — won't auto-apply")
- Collapsible evaluation report rendered from the markdown the LLM wrote
- One-click links to the tailored resume `.docx`, cover letter, and apply URL

### Auto-applier with hard guardrails

The Playwright bot will *only* submit applications when **all** of these hold:

1. Overall fit score ≥ the archetype's `auto_apply_min_score`
2. ATS is on the allowlist (Greenhouse, Lever, Ashby — never Workday)
3. No CAPTCHA detected on the page
4. No "salary requirements" or "cover letter (required)" fields the LLM hasn't already filled
5. The dedup table confirms we haven't applied to this company in the last 90 days

Every action is logged to `auto_apply_log` (JSON) on the Application row so I can audit exactly what the bot did. If any check fails, the application gets routed to the human review queue instead.

---

## Tech stack

| Layer | Stack | Why |
|---|---|---|
| **Language** | Python 3.14 | Type hints, `dataclass`, `match` statements |
| **Database** | SQLite + SQLAlchemy 2.0 | Zero-ops, single-file, plenty fast for this scale |
| **LLM** | Gemma-4 27B via [Ollama](https://ollama.com) | Local, free, no rate limits, ~50 tok/s on consumer GPU |
| **Scheduler** | APScheduler (BlockingScheduler) | Cron-like jobs in pure Python |
| **Web framework** | FastAPI + Uvicorn | Async, SSE-friendly, OpenAPI for free |
| **Scraping** | requests + BeautifulSoup4 (Greenhouse/Lever/Ashby JSON APIs) + Playwright (JS-rendered pages) | Right tool per source |
| **Document gen** | python-docx | Direct XML access for ATS-clean output |
| **Auto-apply** | Playwright (Chromium) | Stable selectors, native form-fill, screenshot on failure |
| **Frontend** | Vanilla HTML/JS + SSE | No build step, ships instantly |
| **Tests** | pytest | Deterministic surfaces (math, config, persistence) — LLM calls covered by integration |

Total monthly infra cost: **$0**.

---

## Setup

### Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com/) installed and the `gemma4:latest` model pulled:
  ```bash
  ollama pull gemma4
  ```
- ~16GB free disk (model is ~10GB)
- A GPU helps but is not required — runs on CPU at ~15 tok/s

### Install

```bash
git clone https://github.com/TheCromazone/Personal-JobPilot.git
cd Personal-JobPilot

python -m venv venv
source venv/bin/activate           # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Install Playwright browsers (for auto-applier)
playwright install chromium
```

### Configure

Copy the two example files and fill in your details:

```bash
cp config/applicant_profile.example.yaml config/applicant_profile.yaml
cp config/base_resume.example.yaml       config/base_resume.yaml
```

Then tune `config/settings.yaml` for the roles you want — search keywords, target locations, excluded keywords, scoring thresholds.

Both `applicant_profile.yaml` and `base_resume.yaml` are **gitignored** — your personal data stays on your machine.

### Run

**Recommended — let the watchdog run everything.** One process supervises Ollama, the dashboard, and the scheduler, restarting any that die:

```bash
python watchdog.py
```

On Windows, double-click `start_jobpilot.bat` (launches the watchdog and opens the dashboard), or run `register_autostart.bat` once to have it start automatically at every login — see [Reliability](#reliability--it-stays-up-on-its-own).

**Manual — run the pieces yourself** (useful for development):

```bash
# Terminal 1 — scheduler
python scheduler.py

# Terminal 2 — dashboard
python -m uvicorn server.dashboard:app --host 127.0.0.1 --port 7777
```

### Use

Open http://127.0.0.1:7777 and:

1. **Watch** new jobs appear as scanners find them
2. **Review** each scored role — open the drawer to see the 10-dim breakdown + LLM-written evaluation
3. **Approve** the high-fit ones — auto-applier submits via Playwright, low-fit ones get a tailored resume + cover letter ready for one-click manual submission
4. **Track** status (Applied → Response → Interview) right from the dashboard

---

## Browser autofill extension

`browser-extension/` is a separate **Manifest V3** Chrome/Edge extension that autofills
job-application forms from your résumé — on any site you open yourself. It scans the page,
asks the local JobPilot backend for a fill-plan (deterministic mapping for the standard
fields, Gemma-drafted answers for essays, résumé **auto-routed** AI vs BT), and fills the
fields. **It never clicks Apply** — you review the highlighted fields and submit yourself,
so it stays within site ToS.

**Install (load unpacked):**

1. Make sure the JobPilot backend is running (it auto-starts at login) — the extension
   talks to `http://127.0.0.1:7777`.
2. Open `chrome://extensions` (or `edge://extensions`) and enable **Developer mode**.
3. Click **Load unpacked** and select the `browser-extension/` folder.
4. On any application page, click the **JobPilot Autofill** toolbar icon →
   **Autofill this application**. Filled fields are outlined green; anything needing review
   is amber, and a toast reports the count.

If the backend is offline it still fills standard fields from a cached copy of your profile
(no AI essays). Résumé file uploads are flagged for manual attach — browsers block scripted
`<input type=file>` for security.

Backed by `server/autofill.py` (`/api/autofill/profile|plan|health`) and the pure
`agents/autofill_mapper.py`. Verify with `python -m pytest tests/test_autofill_mapper.py
tests/test_autofill_api.py -q` and, with the dashboard up,
`python -m pytest tests/e2e/test_extension_autofill.py -q -m live`.

---

## Project structure

```
Personal-JobPilot/
├── agents/
│   ├── scanner/                 # One file per source (Greenhouse, Lever, Ashby, ...)
│   ├── ranker.py                # 3-stage LLM scoring pipeline
│   ├── tailor.py                # Resume + cover letter .docx generation
│   └── auto_applier/            # Playwright submission bot
├── config/
│   ├── settings.yaml            # Search keywords, schedule, thresholds
│   ├── archetypes.yaml          # 6 archetypes × 10 dimensions × weights + thresholds
│   ├── target_companies.yaml    # Career pages to monitor
│   ├── base_resume.example.yaml         # ← copy to base_resume.yaml
│   └── applicant_profile.example.yaml   # ← copy to applicant_profile.yaml
├── db/
│   ├── models.py                # SQLAlchemy models
│   ├── database.py              # Session management
│   └── migrations/              # Schema migrations
├── server/
│   ├── dashboard.py             # FastAPI app + SSE
│   ├── static/index.html        # Single-file UI
│   └── autofill.py              # Autofill API for the browser extension
├── browser-extension/          # MV3 autofill extension (popup, service worker, scan/fill)
├── utils/
│   ├── ollama_client.py         # Ollama wrapper with JSON-mode + retries
│   ├── dedup.py                 # Fuzzy job dedup
│   └── notifications.py         # Windows toast for high-fit jobs
├── tests/                       # pytest suite for deterministic surfaces
├── scheduler.py                 # Entry point — starts APScheduler
├── watchdog.py                  # Supervisor — keeps all 3 services alive
├── start_jobpilot.bat           # Windows one-click launcher (runs the watchdog)
└── register_autostart.bat       # Install/remove login auto-start (no admin)
```

---

## Reliability — it stays up on its own

Running three long-lived processes on a laptop means they eventually die — a reboot, an OOM, a flaky scan. Babysitting them by hand is exactly the toil this project exists to kill, so the supervision is automated too:

- **`watchdog.py`** is a single supervisor that starts Ollama, the dashboard, and the scheduler, then health-checks them on a 30-second loop. Ollama and the dashboard are checked over HTTP (so a wedged-but-alive process still gets recovered); the scheduler is checked by process liveness. Anything down gets restarted, with a boot grace window so a service that's merely starting up is never thrashed. Already-healthy services are left untouched — no duplicate processes.
- **`register_autostart.bat`** drops a hidden launcher in the per-user Startup folder (no admin rights required) so the watchdog — and therefore the whole pipeline — comes back automatically on every login. After a reboot you do nothing; the dashboard is already live at `http://127.0.0.1:7777`.

Net effect: the system is genuinely fire-and-forget. Reboot your machine and the job hunt picks itself back up.

---

## What's next

- [ ] LinkedIn email-alert ingestion (Gmail API → IMAP parser already drafted)
- [ ] Bayesian fit-score calibration — re-weight dims based on which past applications got responses
- [ ] Browser extension for one-click "save this job" from any page
- [ ] Anonymized weekly digest export for accountability buddies
- [ ] Multi-applicant mode (turn it into a service for friends who are job searching)

---

## About me

I'm **Matthew Cromaz** — MS Quantitative Economics, Cal Poly SLO. Currently looking for **Data Scientist / Data Analyst / Quantitative Analyst** roles in the Bay Area.

- 🌐 [LinkedIn](https://www.linkedin.com/in/matthew-cromaz)
- 💻 [GitHub](https://github.com/TheCromazone)
- 📧 Reach out if you're hiring — happy to walk through this codebase or any of my other projects.

---

## License

MIT — see [LICENSE](LICENSE). Built for personal use; share-alike encouraged. PRs welcome if you've extended it for your own search.
