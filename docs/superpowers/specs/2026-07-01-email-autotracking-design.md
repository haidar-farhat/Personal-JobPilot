# JobPilot — Email Auto-Tracking & Contact Enrichment — DESIGN BRIEF

**Date:** 2026-07-01
**Status:** DESIGN ONLY — nothing built, no Gmail access taken. This is Iteration 6 of the
JobRight-parity roadmap, which is explicitly **opt-in and approval-gated** (roadmap line 35,
guardrail line 22). Building/wiring anything here needs Matthew's explicit go-ahead.

## Goal
Close the last gap on JobRight pillar 3 ("automated tracking / auto-update"): when a
confirmation / rejection / interview-invite email lands, **advance the Kanban stage
automatically** (or via a one-click review), so the board reflects reality without manual
dragging. Optional stretch: pillar-4 contact enrichment.

## Why this is gated (not auto-built)
Reading a user's inbox is a privacy/permission boundary. Per the standing safety rules,
reading personal data and creating standing rules both require explicit approval. So this
document stops at design; no code is written and no mailbox is read until Matthew approves.

## Recommended architecture (Approach A — in-app read-only OAuth)
JobPilot runs continuously under the watchdog (independent of any Claude session), so it needs
its **own** Gmail access to work while unattended.

- **Auth:** Google OAuth, scope `gmail.readonly` ONLY (never send/modify/delete). Token stored
  locally in `config/` (same posture as the Sheets service-account already documented). 127.0.0.1 only.
- **New module `agents/email_tracker.py`:**
  - `scan_inbox(since)` → pull recent threads via the Gmail API, **pre-filtered** to job-relevant
    mail (from/subject matched against the companies in the `applications` table + an ATS-domain
    allowlist: greenhouse.io, lever.co, ashbyhq.com, myworkday.com, etc.). Never reads unrelated mail.
  - `classify(email)` → **local Gemma** (reuse `utils/ollama_client`) returns
    `{app_match: job_id|null, kind: confirmation|rejection|interview_invite|other, confidence, evidence}`.
    Classification runs locally — email text never leaves the machine.
  - Map `kind → ApplicationStatus`: confirmation→`applied`, interview_invite→`interview`,
    rejection→`rejected`. Reuse the existing `changeStatus` path + `STATUS_META`.
- **Endpoint `GET /api/email/scan`** (threadpooled, graceful `ok:false` when offline) → returns a
  **review queue** of proposed changes, not applied yet.
- **UI:** a "Sync from email" review modal (mirror the existing Sheet-sync modal): each row shows
  the email → matched job → proposed stage change, with a checkbox. **Nothing changes until Matthew
  approves.** `POST /api/email/apply` commits the checked ones.
- **Storage:** persist only `{email_id, job_id, kind, applied_at}` for idempotency — **never** store
  full email bodies.

### Approach B (rejected for the standalone app)
Driving the connected Gmail MCP from a Claude session works only while Claude is running, so the
board wouldn't update unattended. Fine for a one-off "scan my inbox now" from chat, but not the
always-on tracker. Could be a lightweight bonus later.

## Safety gates (all mandatory)
- Read-only scope; **never** send, label, archive, or delete mail.
- **Opt-in**: feature is dark until Matthew connects Gmail; a clear setup card (like the Sheets one).
- **Review-queue by default** — auto-advance is off unless Matthew explicitly turns it on, and even
  then only for high-confidence `confirmation`/`rejection` (never auto-move to `interview`).
- Inbox pre-filter to job-relevant senders only; no full-inbox scan, no compiling personal data.
- Local classification (Ollama); no email content to third parties.

## Contact enrichment (pillar-4 stretch) — recommend DEFER
Third-party enrichment APIs (Apollo/Clay/etc.) mean cost + ToS + sending someone's data to a
vendor. The outreach drafter already covers pillar 4 safely with **generic role titles + user-supplied
names**. Recommend NOT adding automated contact lookup; if wanted, it's a separate approval.

## Decisions needed from Matthew before building
1. **Green-light** reading Gmail at all (read-only)? If no, we stop here — parity is already at 5/5 on features.
2. **Auth:** in-app OAuth (Approach A, always-on) vs. a chat-only "scan now" via the Gmail connector (B)?
3. **Auto-advance vs review-queue**: fully manual review (safest) or auto-advance high-confidence only?
4. Contact enrichment: **defer** (recommended) or in scope?

## Effort estimate (Approach A, review-queue)
~1 build iteration: OAuth setup card + `email_tracker.py` (scan+classify) + 2 endpoints + review
modal + tests (Gmail API mocked, like the Sheets tests). No new external deps beyond
`google-api-python-client` (already implied by the Sheets integration).
