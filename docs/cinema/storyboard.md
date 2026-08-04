# Storyboard — "Mission Control" (2001: A Space Odyssey)

## Site cinematic grammar
- Shell: a black void; every content area is a matte instrument panel — flat surfaces,
  1px hairline borders, 2–6px radii, no blur, no glass, shadows almost absent.
- Navigation posture: the header is the spacecraft status strip — flat, hairline rule,
  uppercase labels, a red indicator underline for the active view, live health lamps.
- Framing discipline: hairlines separate everything; density cadence = spectacle (hero
  void) → dense telemetry (results/rail/detail) → calm structured views.
- Recurring materials: 35mm film grain (C1), fine engineering line-grid on the hero
  (C3, masked), starfield drift (D2, CSS adaptation), indicator lamps, one red eye.
- Allowed to repeat: hairlines, mono telemetry labels, lamp dots. Not allowed: gradients
  as decoration, glass, bounce.

## Director brief
- Visual thesis: **"A HAL-era flight console for your career."**
- Signature techniques → web translation:
  1. The red eye of HAL → single red accent (#e5372b dark / #c22a20 light) carried by
     active states, primary actions, and the health lamps; red is *signal*, never decor.
  2. Instrument typography → Michroma (Microgramma/Eurostile homage) for short structural
     titles; IBM Plex Mono for telemetry (counts, dates, statuses); IBM Plex Sans body.
  3. The void → hero is permanent black space with a thin bone-white planet limb
     (Higgsfield backdrop + radial glow), sparse drifting stars.
- Color tokens: void #050608 / panel #0e1013 / hairline #22262c / bone #e8e6e0 /
  muted #9aa0a6 / HAL red #e5372b / instrument green #2ea567 / amber #b07d10 /
  steel #4a627e. Light theme = "Discovery white": bone paper #f1efe9, panels #ffffff,
  ink #17181a, same red.
- Motion rules: slow, linear, precise. Continuous drift measured in minutes, entrances
  under 300ms, zero bounce, zero parallax scroll-jacking.

## Page scene (single page, six views)
- One big idea: the monolith search slab floating in the void.
- Page scene thesis: Act 1 — the void (hero, contemplation); Act 2 — mission telemetry
  (results triptych, dense work); Act 3 — flight records (Applied/Board/Companies/Advisor,
  calm archives).
- Hero dominance statement: a black void with a drifting starfield and one monolithic
  black search slab lit by a horizon limb — nothing else is allowed to glow.
- Restraint statement: no gradient decoration, no glass, one accent, two showy moments
  total (starfield drift, lamp breathing); everything else is hairlines and type.
- Material thesis: matte black consoles + film grain; light theme is the clinical white
  Discovery interior with the same instrumentation.
- Typography thesis: Michroma earns authority in ≤3-word uppercase titles; Plex Mono
  makes every number feel like telemetry; Plex Sans keeps dense data legible.
- Signature composition: **Monolith search** — hero-archetype #1 "The Void Entry"
  (Family A) + hero composition Pattern C (centered + satellites: mono telemetry
  eyebrow above, welcome chips below).
- Grid fallback test: flatten to a generic card grid → the void/instrument dichotomy,
  telemetry type, and lamp language all die → composition is load-bearing.
- Shared system holdback: pills/badges/buttons re-derived only after hero + panel
  materials locked (done in compiled spec order).
- Uniqueness guardrail: nothing from v3 survives at the material level — no aurora,
  no glass, no gradient text, no violet.

## Entrance map (≥4 distinct, adjacent differ, fadeUp ≤2)
1. Hero inner — slow fade-from-black (900ms), starfield drifts continuously
2. Result cards — clip-wipe from left (260ms, staggered ≤8)
3. Detail head — slide-in-left (240ms)  [translate use 1 of 2]
4. View sections — aperture settle (scale .996→1 + fade, 280ms)
5. Modals — HAL zoom (scale 1.015→1 + fade, 200ms)
6. Rail — intentionally static (calm instrument)

## Interaction budget
- Heavy (1): hero starfield + limb glow (D2 + custom radial, CSS-only)
- Showy (2): lamp breathing on health dots; red left-rule sweep on card hover
- Everything else: 120ms linear border/color shifts only
