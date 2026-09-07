# JobPilot — JobRight parity, round 2 — DESIGN

**Date:** 2026-09-02
**Status:** approved by default (Matthew asked for "like JobRight in every way": autofill, Google
connector for account creation + verification codes, ATS-keyword one-page résumé, optional
cover letters). Built autonomously; decisions below are the defaults chosen, each reversible.

## What already exists (verified 2026-09-02)
Match score, tailor + ATS optimizer score (0–100, keyword injection, one retry), cover letter
(always), interview prep, outreach drafts, autofill extension v1.11 (Workday multi-page,
Greenhouse sections, review checklist, never submits), Sheets sync (service account).

## Gaps closed in this round
1. **Gmail connector** — none existed. The 2026-07-01 brief designed OAuth; the user has now
   green-lit reading mail. Decision: **IMAP + Gmail App Password** (stdlib `imaplib`, mailbox
   opened read-only, `BODY.PEEK` so nothing is marked read). One-minute setup, no Google Cloud
   project. OAuth (`google-auth-oauthlib`, already installed) is the upgrade path if Google
   drops app passwords for personal accounts.
   - `config/gmail.yaml` (gitignored): `address`, `app_password`. Env overrides
     `JOBPILOT_GMAIL_ADDRESS` / `JOBPILOT_GMAIL_APP_PASSWORD`.
   - `agents/email_reader.py`: `recent_messages(since)`, `find_verification(since, hint)`
     (6-digit-style codes near "code/verify/one-time" words, or a verify/confirm link),
     `scan_job_mail(days)` (rule-based confirmation / rejection / interview classifier matched
     to open applications by company name; idempotent via `ApplicationEvent.note`
     `gmail-uid:<uid>`). No LLM; rules first.
   - `server/gmail.py` → `/api/gmail/health`, `/code?since&hint&wait` (polls ≤90 s),
     `/scan?days`, `POST /apply` (commits via `record_status_change(source="email")`).
   - Dashboard: "Sync from email" review modal (mirror of the Sheet modal) + setup card.
     **Review queue only** — nothing changes until the user ticks rows.

2. **Account-creation + verification-code assist in the extension** (Workday, iCIMS,
   SmartRecruiters, Greenhouse login walls).
   - One **ATS password** set in the toolbar popup (JobRight/Simplify use one password too),
     stored in `chrome.storage.local` only — never in `/api/autofill/profile`, which is
     CORS-wildcard readable. Generate button uses `crypto.getRandomValues`.
   - New `content/account.js`: detects Create Account / Sign In / OTP forms, fills email +
     password (blank fields only), fills a code into single or segmented OTP boxes.
   - Widget gains "Fill account" and "Get code from Gmail" buttons; once an account fill has
     happened on a host, an OTP box appearing within 15 min auto-fetches the code.
   - **Still never clicks** Create Account / Verify / Submit, never ticks "I agree", never
     auto-opens a verification link (a button shows it). User does the final click.
   - **Hard credential blocklist** in the mapper (Python + JS mirror): password / OTP /
     SSN-like fields never reach the LLM planner or get filled by the generic path.
     Trap fixture gains OTP + SSN fields.

3. **Résumé: measured one page + PDF + optional cover letter.**
   - `agents/pdf_export.py`: docx → PDF via Word COM (`docx2pdf`, Word is installed), page
     count by regex on the PDF. `tailor_for_job` re-renders with one fewer bullet until the
     PDF is one page (≤4 passes), then writes the sidecar JSON from the trimmed data.
   - Autofill attaches the PDF when it exists (`tailor.attach_format: pdf`, default).
   - `tailor_for_job(job, score, cover_letter=True)`; `POST …/tailor?cover_letter=0|1`;
     dashboard checkbox (remembered) and `tailor.cover_letter_default` in settings.
   - Dashboard detail shows ATS score + missing keywords after tailoring and a PDF download.

## Out of scope (say so, don't build)
Orion-style chat, H1B filter, LinkedIn contact scraping, auto-clicking Create Account /
Submit, storing passwords server-side, CAPTCHA.

## Tests
Unit: code extraction + classifier rules on canned emails (no network); mapper credential
guard (Python) + JS parity; PDF page-count regex. Playwright fixture `auth_pages.html`
for detect/fill/never-click. Live e2e traps extended.
