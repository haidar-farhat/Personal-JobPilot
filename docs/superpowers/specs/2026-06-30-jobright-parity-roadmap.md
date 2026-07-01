# JobPilot → JobRight-parity Upgrade — Roadmap & Gap Analysis

**Date:** 2026-06-30
**Goal:** Upgrade JobPilot into an AI job-search suite that meets/beats JobRight AI on feature depth, UI polish, and speed — building on what already exists rather than rebuilding.
**Mode:** Continuous Build → QA (adversarial) → Refine loop, one feature per iteration, QA gate before "done."

## Phase 1 — Gap Analysis (current state vs JobRight's 5 pillars)

| # | JobRight pillar | JobPilot status | Evidence |
|---|-----------------|-----------------|----------|
| 1 | **AI job-match score** (resume-vs-JD semantic fit, match %) | **EXISTS** | `db/models.py` `JobScore.fit_score` (0–100) + 10 weighted dimensions + `ai_intensity`; `agents/ranker.py` 3-stage Gemma pipeline; `config/archetypes.yaml`. Was under-surfaced in UI. |
| 2 | **AI resume tailoring** (per-job/URL) | **EXISTS** | `agents/tailor.py` → one-page tailored .docx from JD + keywords + matches/gaps; `base_resume.yaml` / `base_resume_bt.yaml`. Not yet exposed as an on-demand "tailor for this job" UI action w/ diff. |
| 3 | **Automated application tracking** (Kanban, auto-update) | **PARTIAL → improving** | Statuses in `ApplicationStatus` enum; `/api/application/{id}/status`; Google Sheet sync (`server/sheets.py`). **Kanban board added (Iteration 2).** Email auto-update still missing. |
| 4 | **Company insiders & networking** (contacts + outreach) | **MISSING** | No contact enrichment, no outreach generation. |
| 5 | **Interview prep simulator** (mock Q&A from JD) | **MISSING** | `Application.interview_date` field only; no generator/agent. |

Scorecard: **2 built, 1 partial (now stronger), 2 missing.** This is mostly *surface + fill gaps*, not greenfield.

## Guardrails (carry every iteration)
- **Never auto-submit** applications (user clicks Apply — ToS boundary). Autofill stays review-only.
- **Data stays local**: 127.0.0.1 + Ollama only. No sending résumé/profile to third parties.
- **No scraping of personal contact data** for the networking pillar. Outreach = LLM drafting from public job/company text + user-supplied names. Any contact-enrichment API is **opt-in and requires explicit approval** before building (cost + ToS + privacy).
- Dashboard runs uvicorn **without --reload** → backend changes need a `:7777` restart (watchdog respawns); served HTML is fresh per request (frontend-only changes need no restart).
- Only `git add` explicit files; never touch pre-existing WIP.
- No CAPTCHA solving. Keep `dry_run` behavior intact.

## Iteration plan (ordered by ROI × low-risk × foundational)

1. **Kanban Application Board + match-score rings** — *DONE this iteration (frontend-only, additive).*
   New "Board" nav view; 5 stage columns (Saved/Applied/Response/Interview/Rejected) mapped to canonical statuses; HTML5 drag-drop → existing `changeStatus()`; conic-gradient match-score ring per card (tier-colored). Glassmorphism columns, 8px-grid spacing, hover/drop micro-interactions.
2. **Interview Prep Simulator** — new `agents/interview_prep.py` (Ollama Gemma) → generate role-specific Q&A + talking points from JD + fit analysis; `/api/application/{id}/interview` endpoint; detail-panel "Prep" panel. Local, safe, high differentiation.
3. **Match-score surfacing everywhere + "Tailor résumé for this job" action** — promote fit as a prominent "Match %" across list/detail; wire a one-click tailor action to `tailor.py` with a before/after view. (Surfaces pillars 1 & 2.)
4. **Outreach draft generator (safe)** — new agent to draft a recruiter/hiring-manager outreach message from the JD + company text + user-supplied contact name/title. No scraping. Detail-panel action + copy button.
5. **UI system pass** — global dark-mode toggle, Bento-grid dashboard home, consistent 8px rhythm, CLS-safe skeletons for streaming/AI outputs.
6. **(Approval-gated) Email auto-tracking & contact enrichment** — only if the user opts in: Gmail read for confirmation/status emails → auto-advance board stage; optional contact enrichment for pillar 4.

## QA gate (adversarial, every iteration)
Each feature is reviewed against: edge-case durability (malformed payloads, empty/'—' data, offline backend), state-desync (optimistic UI vs server), 8px-grid + CLS + glassmorphism polish, a11y (keyboard path), and functional parity. Iteration isn't "done" until defects are remediated or explicitly deferred with reason.
