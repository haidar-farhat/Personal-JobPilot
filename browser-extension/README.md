# JobPilot Autofill extension

JobPilot fills application forms from the local profile, verifies the values it
set, and leaves the application in a clear **manual review required** state. It
never clicks a Submit/Apply button and autofill alone never marks a job applied
on the JobPilot board.

## Install and use

1. Start the JobPilot dashboard/backend on `127.0.0.1:7777`.
2. Open `chrome://extensions`, enable Developer mode, choose **Load unpacked**,
   and select this `browser-extension` directory.
3. Open an application, choose a resume preference, and select **Autofill this
   application**. Review highlighted fields, upload any flagged document, move
   through any remaining ATS steps, and submit manually.

Profile-backed fields are mapped deterministically using, in order: browser
`autocomplete` semantics, accessible labels (`aria-label`, multi-node
`aria-labelledby`, `<label>`, legend), stable field names/IDs, section context,
and ATS automation IDs. Ambiguous essays can use the configured local Ollama
model; template output remains available when local inference is offline.
Cloud fallback is opt-in in `config/settings.yaml` and is never enabled merely
because an API key exists in the environment.

## Apply with autofill from the dashboard

**APPLY WITH AUTOFILL** on a JobPilot job card does what JobRight's button
does: the dashboard calls `POST /api/autofill/arm {app_id}` (or
`{url, company, title}`), which arms the job's host for 15 minutes, then opens
the job link. When the extension lands on an application form on that host
(or on a page whose URL starts with the armed URL), it opens the panel with an
"Autofilling for *title* @ *company* — from JobPilot" banner and runs the fill
by itself — no click. The run is the same one the **Autofill** pill triggers:
each field scrolls into view, flashes mint for ~600 ms, gets its value, and the
panel's progress bar and "Filling 12 / 30 fields" counter advance with it; the
checklist shows ○ pending, a spinner while running, ✓ done, – skipped, ! failed;
the result line reads "✓ Filled N fields · M need your review" with **Jump to**
buttons for anything flagged.

Guarantees, same as manual autofill: the arm is single-use (the extension
`DELETE`s it after a run that found fields, and it expires anyway); it never
fires on a job-description page or a sign-in wall — it waits for the step that
shows the form; a reload of the same URL never refills; the disabled switch and
the user's own answers are always honoured; nothing is ever submitted.

Contract (extension ⇄ backend): `GET /api/autofill/armed?host=<hostname
without www.>&url=<page url>` → the pending record `{app_id, url, host,
company, title, ts}` or `{}`; `DELETE /api/autofill/armed?host=` clears it.
Service-worker messages: `{type:"ARMED", host, url}` and
`{type:"ARM_CONSUMED", host}`.

## Supported application systems

| System/form style | Support | Notes |
| --- | --- | --- |
| Greenhouse | Dedicated adapter | Handles structured sections and common custom questions; verified values are highlighted. |
| Workday | Dedicated multi-step adapter | Retries dynamic controls and may advance through safe Next/Save-and-Continue steps, but stops before review/submit. |
| Lever, Ashby, SmartRecruiters, iCIMS, Taleo, Jobvite, Workable, BambooHR, Rippling, Paylocity, Dayforce, SuccessFactors, Breezy | Portable semantic mapper | Uses labels, names, `autocomplete`, ARIA, and choice matching. Vendor UI changes may require manual completion. |
| Jobright/Simplify-style overlays and common careers forms | Portable semantic mapper + in-page review widget | Works best when the underlying form exposes standard inputs and accessibility metadata. |

## Safety and known limitations

- Existing non-empty answers are preserved. Unverified writes, ambiguous
  choices, missing profile data, files, and essays are surfaced for review.
- Sign-up / sign-in walls: **Fill account** types your email and the one ATS
  password you set in the toolbar popup into *empty* fields; you tick the terms
  box and click Create Account / Sign In. When the ATS asks for an emailed
  verification code, the pill reads it from Gmail (read-only, via the local
  backend's `config/gmail.yaml`) and fills the box; you click Verify. A
  verification *link* is shown, never auto-opened. The ATS password lives only
  in `chrome.storage.local`.
- Password / one-time-code / SSN fields are excluded from every fill-plan —
  the generic mapper and the LLM never touch them.
- CAPTCHAs, identity verification, e-signature, and legal attestations remain manual.
- Cross-origin or closed shadow DOM controls may not be reachable by an
  extension content script. Visually similar custom controls with no semantic
  metadata can require manual selection.
- Workday navigation is capped and only uses adapter-classified continuation
  actions. JobPilot never activates controls classified as submit/review.
- Site support describes tested strategies, not a guarantee that every employer
  customization will fill without review.

## Validation

Focused coverage lives in `tests/test_autofill_mapper.py`,
`tests/test_autofill_semantics.py`, `tests/test_extension_choice_matching.py`,
and the Greenhouse/Workday fixtures under `tests/e2e/fixtures`. Run:

```powershell
python -m pytest -q tests/test_autofill_mapper.py tests/test_autofill_semantics.py tests/test_extension_choice_matching.py tests/test_extension_source_safety.py
```

Also run `node --check` on the extension JavaScript files after editing.
