# JobPilot — JobRight takeover (UI parity, autofill animation, Orion-style agent) — DESIGN

**Date:** 2026-09-07  **Status:** built and live on :7777 the same day (577 unit + 110 e2e green); extension v1.13.0 needs a manual ↻ in chrome://extensions; first live bot cycle 15:23 PDT: Linear submitted, Replit + Snowflake submitted_unverified, 4× OpenAI honest bail
**Why now:** Matthew's JobRight Turbo ends today. JobPilot must look and behave like
jobright.ai/jobs/recommend so the daily workflow does not change.

## What was captured from JobRight today (source of truth for this build)

Profile (`/jobs/profile`): identity, address, links, education, work experience, 34 skills,
EEO answers — **already identical** to `config/applicant_profile.yaml` + `config/base_resume.yaml`
(verified line by line; nothing to transfer).

Saved filter ("Financial Analyst + 9 roles, San Francisco, CA"):
- Roles (14): Financial Analyst, Risk Analyst, Corporate Finance Analyst, Quantitative
  Analyst/Researcher, Investment Manager, Equity Analyst, Investment Banker, Credit Analyst,
  Investment Analyst/Associate, Underwriter, Data Analyst, Data Scientist, AI Engineer,
  Machine Learning Engineer.
- Experience level: Intern/New Grad, Entry Level, Mid Level. Job type: Full-time, Internship.
  Work model: Onsite, Remote, Hybrid. Date: past 24 hours. Industry: Artificial Intelligence.
- JobPilot already scans a superset of these titles; the dashboard gets a "JobRight" preset
  (default view = these filters) — see §3.

Applied history: 69 jobs (API `/swan/job/applied/jobs-v3`, cursor paginated) exported to
`config/jobright_applied_export.json` and imported with `scripts/import_jobright_applied.py`
(66 jobs created, 3 matched existing, 67 marked APPLIED with the real applyTime; applied count
24 → 91). Liked: 0. External: 0.

Agent (`/agent`): 7 jobs "added, awaiting application start"; per job the agent runs
"Generate Custom Resume → Confirm Custom Resume (Action Required: View Resume / I Want To
Tweak It / Download)" and "Generate Cover Letter → Confirm Cover Letter", then applies and the
job shows "Applied by Agent" + "Download Resume" in the Applied list.

Agent filter (`/swan/agent/filter/get`, verbatim): jobTypes [Full-time, Internship], city
"San Francisco, CA" radius 25, seniority [Intern/New Grad, Entry, Mid], companyCategory
["Artificial Intelligence (AI)"], daysAgo 1, workModel [Onsite, Remote, Hybrid], no salary
floor, no H1B filter. Agent settings (`/swan/agent/get`): agentMode=1 (apply),
resumePreference=1, enableCustomizeResume=true, enableCustomizeCoverLetter=true.
Agent task queue (`/swan/agent/task/list`): 17 tasks — 10 done (status 4), 7 pending
(status 1/2): Serval Automation Engineer (Ashby), Rillet Applied AI Engineer (Ashby), Scale AI
SWE AI Enablement (Greenhouse), Valar Labs SWE (Polymer), ServiceNow Assoc Applications Dev
Engineer (SmartRecruiters), EPAM Junior Python AI Developer, Lumi Software Engineer — imported
into JobPilot as source `jobright_agent` and queued for our bot (see §5).

Orion (per-job chat, bottom-right panel 350×540, mint gradient header "✦ Orion", chevron to
collapse): greeting "I see that you're asking about this **{title}** role at **{company}**.
What would you like to know?" + four quick prompts as right-aligned mint chips with ↑ icon:
"Tell me why this job is a good fit for me." / "Give me some resume tips if I want to
apply." / "Generate custom resume tailored to this job." / "Show me Connections for potential
referral." Answers are markdown bullets in a grey bubble with 👍 👎 copy icons; input
"Ask me anything…" with mic + mint send button.

"Generate Your Custom Resume" drawer (right-side, ~1200px, 3-step stepper):
- Step 1 "See Your Difference": headline "Your Resume is a Low Match for This Job" + sub
  "Resumes under 6.0 are likely to be filtered out by ATS — we'll help you fix it fast.";
  gauge 3.5 "Poor" (0–10); comparison table Overview | Job | Your resume (select) with rows
  Job Title, Years of Experience (✓ when met), Industry Experience (tags), ATS Job Keywords
  (1/8: present vs missing tags), Summary ("Your current summary does not effectively
  showcase…"); CTA "Improve My Resume for This Job".
- Step 2 "Align Your Resume": "1. Choose sections to enhance" checkboxes Summary / Skills /
  Work Experience (radio: Quick Edit — first 2 key experiences | Full Edit — all) / Projects;
  "2. Add missing ATS job keywords (0/7)" with "Select all", grouped Functional Skills and
  Tools as toggle chips + "Add Keywords" input per group; CTA "Generate My New Resume";
  loading card "Finalizing Your New Resume… It usually takes about 10–20 seconds".
- Step 3 "Review Your New Resume": "Great! Your score jumped from 3.5 to 8" gauge 8.0 "Good";
  "See What's Changed": Summary updated · Enhanced 5 work experience bullets · Add 7 missing
  skills; suggestion chips ("Use stronger action verbs…", "Shorten my summary…", "Remove skills
  not related to this job"); tabs AI Rewrite | Editor | Style; chat input "Tell me how you'd
  like to tweak your resume…"; right pane = résumé preview with file name
  "{First Last}_{title}_{date}", "Edit on resume", "Fit to one page", page 1/1; footer
  buttons "Download Resume" and mint "APPLY NOW". In the preview every inserted/rewritten
  phrase is highlighted (soft yellow-green background) so the user sees exactly which ATS
  keywords were woven in ("financial modeling", "stakeholder partnership", …).
JobPilot mapping: step 1 = optimizer keyword presence on the current résumé (score = ATS
score/10), step 2 = section toggles + `ats_keywords` chips (selected ones are passed to
`POST /tailor?keywords=…` — tailor already injects the JD keywords; the checkboxes just let
the user exclude ones they can't honestly claim), step 3 = new ATS score, keywords covered,
PDF preview, Download PDF/.docx, "I Want To Tweak It" (free-text → re-tailor with the note).

## 1. Data model the UI needs (JobRight → JobPilot mapping)

| JobRight card element | JobPilot source |
|---|---|
| `displayScore` + `rankDesc` (GOOD MATCH ≥ 70?, FAIR MATCH < 70, STRONG ≥ 85) | `fit_score`; tiers: strong ≥ 85 → "STRONG MATCH", ≥ 70 → "GOOD MATCH", else "FAIR MATCH" |
| recommendationScores: Experience Level / Skill Match / Industry Experience (0–100) | `dimensions.level_match`, `dimensions.skills_overlap`, `dimensions.archetype_fit` |
| "Why This Job Is A Match" paragraph | first 2 sentences of `key_matches` joined, else evaluation summary |
| meta badges: "1 hour ago", "Unicorn ($5.8B)", "Early applicant", "Reposted" | `postedLabel`, company profile chips if present, "Easily apply" when tailored materials exist |
| detail grid: location, job type, salary, work model, level, years exp | `location`, `employment_type`, `payLabel`, `is_remote`, `seniority_level`, parsed "N+ years" from description |
| right block: dark rounded panel with ring %, tier, company facts | same; facts = archetype label, AI-forward, H1B unknown (omit) |
| buttons: ASK ORION (ghost) · APPLY WITH AUTOFILL (mint, dark text) | ASK JOBPILOT (opens copilot panel) · APPLY WITH AUTOFILL (arms extension + opens link) |
| Applied list: status tabs Applied / Interviewing / Offer / Rejected / Archived, "Applied on {date}", "Applied by Agent", Download Resume, status dropdown | existing Applied view data (`date_applied`, `auto_applied`, files) |

## 2. Visual system (light only by default; keep the dark toggle working)

- Page bg `#f5f6f8`; sidebar + cards white; card radius 12px, 1px `#e8eaee` border,
  hover shadow `0 4px 16px -6px rgba(15,23,42,.12)`.
- Text `#111827`; muted `#6b7280`; font: Inter/system sans, 13–14px body, 16px card title
  (600), 22px page title "JOBS" (700, letter-spaced caps for section labels).
- Mint primary `#12c98f` (buttons), mint tint `#e6fbf4`, dark match block gradient
  `linear-gradient(160deg,#0f2f2a,#173f38)` with ring stroke `#2ee6a6`; FAIR ring `#f5b942`
  (JobRight uses the same mint for all tiers — copy that).
- Left nav 160px: logo "JobPilot" (mint star mark), items with 16px line icons: Jobs, Resume,
  Profile, Agent, Applied, Companies, Coaching(=Advisor), Skills; bottom: Feedback? no —
  Settings (prefs modal). Active item: mint tint pill.
- Top bar: "JOBS ›" then tabs Recommended · Liked (count) · Applied (count) · External (count);
  right: search box "Search by title or company".
- Filter chip row under it: Location, Roles (+N), Level (+N), Job type, Work model, Date
  posted, AI-forward, Years exp, "Hidden jobs" toggle, "All Filters" (opens the existing rail
  as a drawer). Chips are white pills with caret; active = mint outline.
- Job card (list): 3 columns — logo box 56px (initial) · body · 118px dark match block.
- Job detail = its own view (`/jobs/info` in JobRight): tabs Overview | Company; header
  (logo, company · posted, title, chips), match block with 3 sub-scores, summary, tags,
  Responsibilities, Qualification (skill tags "Represents the skills you have"), Required,
  Benefits, Company card. Right column "AI Tools": **Customize Your Resume** (→ tailor),
  **Build Cover Letter** (→ tailor with cover), **Analyze How Well You Fit** (→ evaluation +
  10 dims), plus Interview prep and Outreach cards below (JobPilot extras, same card style).
  Sticky top-right: APPLY WITH AUTOFILL.

## 3. Dashboard changes (`server/static/index.html`, owner: UI agent)

1. New skin layer `<style id="jr-skin">` after `#mc-skin` (do not delete the older layers) +
   the layout changes above. Keep every existing element id / data attribute / aria contract
   the e2e and unit tests use (`#rail #results #detail .card #findBtn #clearBtn #what #where
   #statusSel #tailorBtn #prefsBtn #prefsModal #prefLove #prefAvoid #prefFloor #prefsSave
   #appliedView #apTabs .ap-tab #appliedList .ap-row .d-head h2 #f-ai #f-locations-* #health
   #resultCount`, `<nav class="topnav" aria-label="Primary">`, `id="filterBtn"
   aria-expanded="false" aria-controls="rail"`, `.topnav{display:flex` inside the
   `@media(max-width:1100px)` block, the "submit it yourself, then use Log apply" copy).
2. Left nav replaces the header brand/topnav visually (topnav markup stays, restyled as the
   sidebar); hero section hidden (`display:none`) — JobRight has no hero.
3. Card template = §1 table. Sub-scores rendered as three "NN%  Label" pairs.
4. "ASK JOBPILOT" opens a bottom-right copilot panel (`#copilot`) with the Orion greeting and
   four quick prompts wired to existing endpoints: fit → evaluation (`/api/application/{id}
   /evaluation`), resume tips → tailor result ATS keywords_missing + key_gaps as bullets,
   generate custom resume → `POST /tailor`, connections → outreach (`/outreach`). Free-text
   input posts to a new `POST /api/application/{id}/ask` (server: prompt = JD + evaluation +
   resume summary + question → `generate_text`; 60 s timeout; plain markdown answer).
5. "APPLY WITH AUTOFILL": `POST /api/autofill/arm` `{app_id}` (server stores `{host, url,
   app_id, company, title, ts}` in a module dict, 15-minute TTL) then opens the job URL via
   the existing open-link. New `GET /api/autofill/armed?host=…` returns the pending arm for
   that host (and any host for the same `app_id`), or `{}`.
6. Applied view gets JobRight's status tabs + "Applied on", "Applied by Agent" badge
   (`auto_applied` or lead_source JobRight note), Download Resume, status dropdown.
7. Filter preset: `JOBRIGHT_PRESET` = roles/levels/types above; applied on first load when
   no saved filter state exists; "Your Saved Filters" card on the right shows it.
8. Keep dark mode functional (tokens) but default to light.

### §1–§3 status (UI agent, 2026-09-07) — BUILT

- `server/static/index.html`: `<style id="jr-skin">` layer + sidebar shell, JOBS top bar/tabs,
  chip filters (Roles/Level/Work model/Years are derived client-side from title/location/JD;
  Location/Job type/Date/AI chips drive the existing rail state; "All Filters" = `#filterBtn`
  opening `#rail` as a drawer), 3-column cards with the dark match block, full-page detail
  (`#detail`, Overview | Company, AI Tools column), résumé wizard (`#wizard`), copilot
  (`#copilot`), Applied status tabs, new Resume / Profile / Agent views.
- `server/dashboard.py`: `company_id` in the application payload; `POST /api/application/{id}/ask`;
  `GET /api/resume/list`, `GET /api/resume/base/{ai|bt}`; `GET /api/profile/full`;
  `POST /api/agent/run`, `GET /api/agent/status` (daemon thread around
  `agents.auto_applier.runner.run_auto_apply`, exceptions surface as `last_summary.error`).
- Preset default does NOT narrow the feed (all chips fully selected = everything); the
  "Your Saved Filters" card applies the archetype mapping on click. Hidden Jobs toggle now
  keeps skipped jobs out of the feed by default (JobRight behaviour).
- Tests: `tests/e2e/test_applied_view.py` expects 7 status tabs (Offer Received added);
  QA shots in `tests/qa-screenshots/jr-*.png` (`jr_shots.py` regenerates them).

## 4. Extension (`browser-extension/`, owner: extension agent)

Target look = JobRight's autofill: a 360px white panel, bottom-right, mint header
"JobPilot Autofill", progress bar + "Filling 12 / 30 fields", step list (Analyzing form →
Personal info → Work experience → Education → Résumé → Screening questions → EEO → Review),
each field flashes a mint outline + soft glow for 600 ms as it is filled, ~90 ms stagger
between fields, then "✓ Filled N fields · M need your review" and the review checklist with
"Jump to" buttons; final CTA "Review & submit yourself" (never clicks submit).

1. `fill.js`: fill sequentially with `await sleep(90)` between fields; emit
   `jpaf-progress` `{type:"field", label, ok, index, total}` and `{type:"step", name, status}`
   events; keep all safety gates (never overwrite user answers, gated option matcher,
   credential blocklist).
2. `widget.js`: new panel markup/CSS per above; progress bar; counter; step list reuses
   `stepsInit/stepSet`; review list reuses `renderReview`.
3. Auto-run: on `looksLikeApplication()`, service worker `GET /api/autofill/armed?host=` — if
   armed for this host (or the page URL contains the armed url), open the panel and run the
   fill once automatically (respect the disabled switch). Clear the arm after use
   (`DELETE /api/autofill/arm/{app_id}`… or POST arm with `consumed`). Never auto-submit.
4. `manifest.json` → 1.13.0. Update `browser-extension/README.md`.
5. Tests: extend `tests/e2e/test_extension_widget_toggle.py`/`test_extension_features.py`
   with one check for the progress events and one for the armed auto-run (fixture host).

## 5. Auto-apply engine v2 (`agents/auto_applier/`, owner: bot agent — running)

Replace stale per-ATS selector fillers with `MapperApplier`: inject scan.js + fill.js in
Playwright, `POST /api/autofill/plan`, apply, attach files, re-scan, required-field check,
submit only when every required field is filled, `submitted_unverified` is permanent (no
double applications). Details + dry-run evidence in
`2026-09-01-jobpilot-hardening-design.md` § "Auto-apply engine v2".

## 6. Ops

- Root cause of the 2026-09-06 outage: Windows reboot 16:27; the Startup-folder entry
  `JobPilot.vbs` had been **disabled in Startup Apps on 2026-08-23** (registry
  `StartupApproved\StartupFolder` = 03). Re-enabled (02) today; `schtasks` ONLOGON is not
  available without admin. `watchdog.py` gains a single-instance guard.

## Out of scope (say so)

LinkedIn/insider connections scraping, H1B sponsorship data, JobRight's "Coaching" and
"Interview" products (JobPilot's interview-prep copilot stays as is), CAPTCHA, account
creation, auto-clicking Submit in the extension.
