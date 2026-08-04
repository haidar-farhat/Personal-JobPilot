# Compiled Spec — Mission Control v4

Source of truth for the build. Implementation = swap `<style>` block (index.html
lines 12–569) + swap Google Fonts link (line 11) + hero backdrop asset. No JS edits,
no markup edits, every selector and CSS var name preserved.

## External Library Decision
### Q1 core motion: atmospheric hero (starfield drift + limb glow) + micro-entrances
### Q2 native entries sufficient: yes — all effects are CSS-only
### Decision: **no external library**. Fonts only: Google Fonts (Michroma, IBM Plex
Sans, IBM Plex Mono) replacing Inter/Space Grotesk in the existing link tag.

## Library citations
- Hero skeleton: hero-archetypes **#1 The Void Entry** (Family A: Immersive) +
  composition **Pattern C Centered-with-Satellites**
- Atmosphere: background-techniques **C1 Film Grain** (SVG feTurbulence, opacity .04),
  **C3 Line Grid** (masked, hero only), **D2 Starfield Parallax** — *adapted Custom:*
  CSS 3-layer radial-gradient starfield with slow background-position drift instead of
  canvas JS (JS budget: zero; drift 120–240s linear loops)
- Limb glow + backdrop: Custom (Higgsfield image, job 6227dc94) — subordinate to void
- Typography: typeset-research → Michroma (display 400, uppercase, tracked),
  IBM Plex Mono (telemetry), IBM Plex Sans (body)

## Token map (all legacy var names keep working)
| Var | Dark (HAL void, default-dark) | Light (Discovery white) |
|---|---|---|
| --bg / --page-bg | #0a0b0d / #050608 | #fbfaf7 / #f1efe9 |
| --surface / -2 / --panel | #0e1013 / #13161a / #13161a | #ffffff / #f4f2ec / #f4f2ec |
| --text / --muted / --subtle | #e8e6e0 / #9aa0a6 / #767c83 | #17181a / #565a5f / #7c8085 |
| --brand/--blue/--brand2 | #e5372b | #c22a20 |
| --blue-dark (hover) | #ff5a4e | #8f1d15 |
| --violet/--ai (AI accent) | #8fb0d8 steel | #33506e steel |
| --grad | flat red 135deg #e5372b→#c22a20 | #c22a20→#a02218 |
| match strong/maybe/stretch | #2ea567 / #c9920e / #4a627e | #1e7a46 / #a3770a / #46607d |
| --border / --divider / strong | #22262c / #1a1e23 / #333941 | #ddd8cc / #e8e4d9 / #c2bcae |
| radii xs…xl | 2/3/4/5/6/8 (pill stays 999) | same |
| --font / --display / mono | IBM Plex Sans / Michroma / IBM Plex Mono | same |
| glass vars (legacy names) | flat translucent blacks, blur 0 | flat translucent whites |

## Entrance CSS (complete, in build)
- `heroFade` 900ms ease-out (opacity) on .hero-inner
- `cardReveal` 260ms ease-out clip-path inset(0 40% 0 0)→0 + opacity on .card,
  stagger nth-child(1..8) × 22ms
- `detailIn` 240ms translateX(-8px)+fade on .d-head
- `viewIn` 280ms scale(.996)→1+fade on #appliedView/#boardView/#companiesView/#advisorView:not([hidden])
- `modalIn` 200ms scale(1.015)→1+fade on .modal
- Continuous: `starDrift1/2/3` 240/180/120s linear infinite (background-position),
  `limbBreath` 14s opacity .75↔1, `lampBreath` 4s on .dot.up
- `@media (prefers-reduced-motion: reduce)` collapses all (existing rule retained)

## Section notes
- header: flat #050608cc strip, hairline bottom; nav uppercase 12px tracked, active =
  2px red underline; health lamps = 7px round LEDs with glow; brand = Michroma,
  "Pilot" span solid red (gradient text banned)
- hero: void in BOTH themes (space through the window); backdrop img + limb glow via
  .aurora (repurposed), starfield via .grid (repurposed); search-wrap = monolith slab:
  always-dark #0b0c0e, hairline #2a2f36, radius 4, inset top light; .find = red block,
  uppercase, tracked
- cards: flat panels, hover = border brighten + 2px red left-rule (::before scaleY),
  NO lift; .sel = red hairline ring; .ai-card left rule steel
- pills/badges/statuses: mono 11px uppercase where short (status/match/date), tier
  colors from token map, tabular-nums everywhere numeric
- board columns: flat panels (glass vars now flat), drag-over = red hairline inset
- modals: panel + Michroma title, head stays dark void band in both themes
- detail banner: flat void band (no purple gradient), radius 4
- toast: void panel, red err variant kept
- print block + responsive block: retained verbatim except color literals mapped
- body::after film grain fixed overlay (opacity .04, z 9999, pointer-events none,
  hidden in @media print)

## Phase 3 checklist: layout ✓ entrances ✓ interactions ✓ (or intentional none: rail)
JS-required effects: none selected ✓ tokens not hardcoded ✓ entrance variety ✓
External-library block ✓ citations ✓ anti-garbage review passed ✓
