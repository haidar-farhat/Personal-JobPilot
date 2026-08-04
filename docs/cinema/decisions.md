# Decisions — JobPilot Dashboard Re-skin

## Start questionnaire (inferred from request, autonomous session)
- Start mode: Surprise me (user asked for "immersive, cinematic, awwwards winning"; no reference given)
- Image placeholders: No — one real generated asset (Higgsfield) for the hero void
- Niche: personal AI job-search mission control (local tool, data-dense, live)
- Pages: one page, six views (Home/Applied/Board/Insights/Companies/Advisor) + 3 modals

## Genre / Director / Film
- Genre: Sci-Fi. Director: **Stanley Kubrick**. Film: **2001: A Space Odyssey (1968)**.
- Not in directors-200.md (library's sci-fi lane is Villeneuve-dominated) — deliberate
  anti-convergence pick, researched externally.
- Research (sources recorded): Typeset In The Future (2001 typography — Gill Sans title,
  Eurostile Bold Extended / Microgramma for the 2001 world, Futura); Eye on Design (THD
  Sentient — HAL telemetry numerals); Firedog (production design environments).
- Interpretation: the film's UI language is *instrumentation, not decoration* — matte
  black consoles, indicator lamps, telemetry type, one red eye. Perfect fit for a tool
  named Job**Pilot**: the dashboard becomes mission control for a job hunt.

## Previous-work audit (this repo's own design history)
- v1 `index.editorial-v1.html.bak` (editorial), v2 `index.cinematic-v2.html.bak`,
  live v3: aurora-gradient hero + glassmorphism + bento + blue→violet gradient identity.

## Shell-ban list
- aurora / mesh-gradient hero blobs
- glassmorphism (backdrop blur cards)
- blue→violet gradient accents and gradient text
- default rounded-xl radii, bouncy translateY hover lifts
- "SaaS page wearing cinematic makeup"

## Primary composition family
- **Full-bleed void + instrument panel.** Constraint honestly recorded: the app shell
  (header / 3-pane main / view sections) is a live JS contract and cannot be re-wireframed.
  Uniqueness is expressed through material, typography, framing, and motion language —
  where v3 was glass-and-gradient, v4 is void-and-instrument.

## Constraint contract (must not break)
- All IDs, data-* attributes, JS-toggled classes, and e2e selectors per the recon map
  (tests: dashboard_render, dashboard_interactions, applied_view must stay green).
- CSS-block replacement (lines 12–569) + fonts link swap only; no JS edits; `[hidden]`
  stays display:none; `.d-body h3` stays uppercase (test casing precedent).

## Tooling notes
- Higgsfield: hero void backdrop generated (job 6227dc94-daf1-4fb5-9ede-0da7bcdcc6b2).
- 21st.dev Magic MCP: returned malformed MCP responses twice (server-side protocol
  error) — proceeded without it.
