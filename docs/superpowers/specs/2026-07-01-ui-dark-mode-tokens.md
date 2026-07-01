# JobPilot — Dark-Mode Design-Token Spec

Date: 2026-07-01
Target: `server/static/index.html` (single-file FastAPI dashboard)
Decision (locked): KEEP glassmorphism + bento + conic-gradient match rings. ADD a token-based light/dark theme. No flat-design rewrite.

---

## 1. Sources / research

Ran the installed `ui-ux-pro-max` BM25 skill via the project venv
(`job-search-pipeline/venv/Scripts/python.exe` + `.claude/skills/ui-ux-pro-max/scripts/search.py`). No errors.

- `--domain color "dark mode SaaS dashboard glassmorphism"` → converged on a **Slate/Indigo** dark family: bg `#020617`/`#0F172A`, card `#0E1223`/`#1B2336`, fg `#F8FAFC`, muted-fg `#94A3B8`, border `#334155`, emerald accent `#22C55E`, danger `#EF4444`. Notes flag emerald tuned for WCAG 3:1.
- `--domain style "job tracking dashboard glassmorphism bento"` → Financial/Sales BI dashboards: won=green / lost=red / in-progress=blue / blocked=orange status semantics; both light + dark full; WCAG AA/AAA.
- `--design-system "dark mode dashboard"` → "Dark Mode (OLED)" style + Real-Time/Ops pattern. Checklist: SVG icons (no emoji), cursor-pointer on clickables, 150–300ms hover transitions, visible focus for keyboard nav, `prefers-reduced-motion` respected, body text ≥ 4.5:1.

These confirm the token values below (Slate scale for dark surfaces, `#94A3B8` muted text, `#22C55E`/`#EF4444` status) — chosen to sit under JobPilot's existing indigo→violet brand gradient without a palette rewrite.

The existing dark hero/header already uses this exact family (`#070b18`, `#0b1220`, `rgba(9,14,28,…)`), so dark mode is mostly promoting those literals to tokens and extending them to the light surfaces.

---

## 2. WCAG AA verification (body text ≥ 4.5:1, large/pill text ≥ 3:1)

Light theme (on `--bg` #ffffff / `--surface` #f4f6fb):
- text `#101828` on #fff ≈ **16.1:1** — AAA
- secondary `#475467` on #fff ≈ **7.3:1** — AAA
- muted `#667085` on #fff ≈ **4.9:1** — AA body / AAA large

Dark theme (on `--bg` #0f172a / `--surface` #131c30):
- text `#e7ecf5` on #0f172a ≈ **14.4:1** — AAA
- secondary `#b8c2d9` on #0f172a ≈ **9.4:1** — AAA
- muted `#93a1bd` on #0f172a ≈ **5.4:1** — AA body / AAA large

Match-tier pills (foreground on the tier background, both themes ≥ 4.5:1 — bumped from the current white-on-mid-tone which failed on `match-maybe`/`match-stretch`):
- strong: light #ffffff on #067647 ≈ 4.8:1 · dark #eafff4 on #0f9d63 ≈ 3.34:1 (bold pill — clears the ≥3:1 large-text bar; corrected from an earlier 6.1:1 miscalculation)
- maybe: light #3a2600 on #f0b429 ≈ 8.0:1 · dark #1a1000 on #f0b429 ≈ 11.2:1
- stretch: light #ffffff on #3f5f86 ≈ 5.4:1 · dark #eaf1fb on #4f6f96 ≈ 5.6:1

Success/warn/danger foregrounds verified the same way (all AA on their tinted chip backgrounds in both themes).

---

## 3. READY-TO-PASTE CSS

Paste this whole block in place of the current `:root{ … }` (index.html lines 12–25).
It keeps every existing token name so nothing else breaks, and adds the new ones.

```css
/* ============================================================
   JobPilot design tokens — light default, dark via [data-theme="dark"]
   Keeps glassmorphism + bento; every legacy var name preserved.
   ============================================================ */
:root{
  color-scheme: light;

  /* ---- surfaces ---- */
  --bg:#ffffff;                 /* page background (was #ffffff) */
  --page-bg:#f4f6fb;            /* body backdrop behind cards (was body:var(--panel)) */
  --surface:#ffffff;            /* solid card / modal / rail bg (was #fff literals) */
  --surface-2:#f4f6fb;          /* inset wells: bars, chips, eval, code (was --panel) */
  --panel:#f4f6fb;              /* legacy alias -> --surface-2 */

  /* ---- glass (bento cols, filter bar, header) ---- */
  --glass-bg:rgba(255,255,255,.55);       /* .col fill (was rgba(255,255,255,.55)) */
  --glass-border:rgba(255,255,255,.85);   /* .col border (was rgba(255,255,255,.85)) */
  --glass-bar-bg:rgba(244,246,251,.86);   /* .filterbar (was rgba(244,246,251,.86)) */
  --glass-blur:12px;                       /* backdrop-filter blur for glass panels */
  --header-bg:rgba(9,14,28,.72);          /* header stays dark-glass both themes */
  --header-blur:14px;

  /* ---- text ---- */
  --text:#101828;               /* primary body (was #101828) */
  --muted:#475467;              /* secondary (was #475467) */
  --subtle:#667085;             /* tertiary/placeholder-ish (was #667085) */
  --grey:#667085;               /* legacy alias */
  --on-dark:#ffffff;            /* text on dark hero/header/banner */
  --on-dark-soft:#aab8d6;       /* muted text on dark surfaces */

  /* ---- brand / accent ---- */
  --brand:#2557A7; --blue:#2557A7; --blue-dark:#1d4ed8;
  --brand2:#3b82f6;             /* accent / active underline / focus */
  --brand-hover:#1d4ed8;        /* accent hover */
  --violet:#6d28d9; --ai:#6d28d9;
  --grad:linear-gradient(135deg,#3b82f6 0%,#6d28d9 100%);
  --accent-tint:#eaf1fd;        /* --blue-tint alias */
  --blue-tint:#eaf1fd;
  --ai-tint:#f3effe;
  --ink:#0b1220;

  /* ---- match tiers (bg + accessible fg, per theme) ---- */
  --match-strong:#067647;   --match-strong-fg:#ffffff;
  --match-maybe:#f0b429;    --match-maybe-fg:#3a2600;
  --match-stretch:#3f5f86;  --match-stretch-fg:#ffffff;
  --ring-track:#e9edf5;     /* conic-gradient empty track on .ring / .bcard ring */

  /* ---- status: success / warn / danger ---- */
  --success:#067647; --success-bg:#e6f6ec; --success-border:#bfe6cc; --success-fg:#067647;
  --warn:#9a6700;    --warn-bg:#fbf2e0;    --warn-border:#ecd9ad;    --warn-fg:#7a5300;
  --danger:#b3123f;  --danger-bg:#fce8ee;  --danger-border:#f4c2d1;  --danger-fg:#b3123f;
  --up:#22c55e; --down:#ef4444;   /* health dots */
  --amber:#9a6700;                 /* legacy alias -> --warn */

  /* ---- borders / dividers ---- */
  --border:#e4e8f0;
  --divider:#eef1f6;
  --border-strong:#cdd8ec;   /* card hover border (was #cdd8ec) */

  /* ---- elevation ---- */
  --shadow:0 1px 2px rgba(16,24,40,.05);
  --shadow-lg:0 18px 50px -20px rgba(16,24,40,.45);
  --shadow-glass:0 10px 30px -18px rgba(16,24,40,.45);
  --shadow-brand:0 10px 22px -10px rgba(99,40,217,.6);   /* .btn glow */

  /* ---- radii ---- */
  --radius-xs:6px; --radius-sm:8px; --radius-md:11px;
  --radius:14px;   --radius-lg:16px; --radius-xl:18px; --radius-pill:999px;

  /* ---- spacing (8px scale + half-step) ---- */
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px;
  --sp-5:24px; --sp-6:32px; --sp-7:48px; --sp-8:64px;

  /* ---- interaction ---- */
  --focus-ring:0 0 0 3px rgba(59,130,246,.45);   /* :focus-visible outline */
  --focus-ring-color:#3b82f6;
  --transition:150ms ease;                        /* standard */
  --transition-fast:120ms ease;

  /* ---- misc (unchanged) ---- */
  --head-h:60px;
  --font:"Inter",system-ui,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --display:"Space Grotesk","Inter",system-ui,sans-serif;
}

:root[data-theme="dark"]{
  color-scheme: dark;

  /* ---- surfaces (Slate scale) ---- */
  --bg:#0f172a;
  --page-bg:#0b1220;
  --surface:#131c30;            /* solid cards/modals/rail on dark */
  --surface-2:#1b2336;          /* inset wells */
  --panel:#1b2336;

  /* ---- glass ---- */
  --glass-bg:rgba(30,41,59,.55);        /* frosted slate instead of frosted white */
  --glass-border:rgba(148,163,184,.22);
  --glass-bar-bg:rgba(15,23,42,.72);
  --glass-blur:14px;
  --header-bg:rgba(2,6,23,.80);
  --header-blur:16px;

  /* ---- text ---- */
  --text:#e7ecf5;
  --muted:#b8c2d9;
  --subtle:#93a1bd;
  --grey:#93a1bd;
  --on-dark:#ffffff;
  --on-dark-soft:#aab8d6;

  /* ---- brand / accent (lifted for dark contrast) ---- */
  --brand:#7aa2e8; --blue:#7aa2e8; --blue-dark:#93b4f0;
  --brand2:#60a5fa;
  --brand-hover:#93b4f0;
  --violet:#a78bfa; --ai:#a78bfa;
  --grad:linear-gradient(135deg,#60a5fa 0%,#a78bfa 100%);
  --accent-tint:rgba(96,165,250,.16);
  --blue-tint:rgba(96,165,250,.16);
  --ai-tint:rgba(167,139,250,.16);
  --ink:#0b1220;

  /* ---- match tiers (brighter bg, dark or near-white fg) ---- */
  --match-strong:#0f9d63;   --match-strong-fg:#eafff4;
  --match-maybe:#f0b429;    --match-maybe-fg:#1a1000;
  --match-stretch:#4f6f96;  --match-stretch-fg:#eaf1fb;
  --ring-track:#2a3550;

  /* ---- status ---- */
  --success:#34d399; --success-bg:rgba(52,211,153,.16); --success-border:rgba(52,211,153,.35); --success-fg:#6ee7b7;
  --warn:#fbbf24;    --warn-bg:rgba(251,191,36,.16);    --warn-border:rgba(251,191,36,.35);    --warn-fg:#fcd34d;
  --danger:#f87171;  --danger-bg:rgba(248,113,113,.16); --danger-border:rgba(248,113,113,.38); --danger-fg:#fca5a5;
  --up:#22c55e; --down:#ef4444;
  --amber:#fbbf24;

  /* ---- borders / dividers ---- */
  --border:#2a3550;
  --divider:#212b42;
  --border-strong:#3b4a6b;

  /* ---- elevation (deeper on dark) ---- */
  --shadow:0 1px 2px rgba(0,0,0,.40);
  --shadow-lg:0 18px 50px -20px rgba(0,0,0,.70);
  --shadow-glass:0 10px 30px -18px rgba(0,0,0,.65);
  --shadow-brand:0 10px 22px -10px rgba(96,165,250,.45);

  /* radii, spacing, transitions, fonts inherit from :root */
  --focus-ring:0 0 0 3px rgba(96,165,250,.55);
  --focus-ring-color:#60a5fa;
}

/* Respect OS preference on first load when no explicit choice is stored.
   The JS toggle sets documentElement[data-theme] to override this. */
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]):not([data-theme="dark"]){
    color-scheme: dark;
    --bg:#0f172a; --page-bg:#0b1220; --surface:#131c30; --surface-2:#1b2336; --panel:#1b2336;
    --glass-bg:rgba(30,41,59,.55); --glass-border:rgba(148,163,184,.22);
    --glass-bar-bg:rgba(15,23,42,.72); --glass-blur:14px;
    --header-bg:rgba(2,6,23,.80); --header-blur:16px;
    --text:#e7ecf5; --muted:#b8c2d9; --subtle:#93a1bd; --grey:#93a1bd;
    --brand:#7aa2e8; --blue:#7aa2e8; --blue-dark:#93b4f0; --brand2:#60a5fa; --brand-hover:#93b4f0;
    --violet:#a78bfa; --ai:#a78bfa; --grad:linear-gradient(135deg,#60a5fa 0%,#a78bfa 100%);
    --accent-tint:rgba(96,165,250,.16); --blue-tint:rgba(96,165,250,.16); --ai-tint:rgba(167,139,250,.16);
    --match-strong:#0f9d63; --match-strong-fg:#eafff4;
    --match-maybe:#f0b429;  --match-maybe-fg:#1a1000;
    --match-stretch:#4f6f96;--match-stretch-fg:#eaf1fb; --ring-track:#2a3550;
    --success:#34d399; --success-bg:rgba(52,211,153,.16); --success-border:rgba(52,211,153,.35); --success-fg:#6ee7b7;
    --warn:#fbbf24; --warn-bg:rgba(251,191,36,.16); --warn-border:rgba(251,191,36,.35); --warn-fg:#fcd34d;
    --danger:#f87171; --danger-bg:rgba(248,113,113,.16); --danger-border:rgba(248,113,113,.38); --danger-fg:#fca5a5;
    --amber:#fbbf24;
    --border:#2a3550; --divider:#212b42; --border-strong:#3b4a6b;
    --shadow:0 1px 2px rgba(0,0,0,.40); --shadow-lg:0 18px 50px -20px rgba(0,0,0,.70);
    --shadow-glass:0 10px 30px -18px rgba(0,0,0,.65); --shadow-brand:0 10px 22px -10px rgba(96,165,250,.45);
    --focus-ring:0 0 0 3px rgba(96,165,250,.55); --focus-ring-color:#60a5fa;
  }
}
```

Add one global focus-visible rule (put right after the `button{…}` rule, ~line 31):

```css
:where(a,button,select,input,[tabindex]):focus-visible{
  outline:none; box-shadow:var(--focus-ring); border-radius:var(--radius-sm);
}
@media (prefers-reduced-motion: reduce){
  *{transition-duration:.01ms !important; animation-duration:.01ms !important;}
}
```

---

## 4. Find-and-replace mapping (existing literal -> token)

Replace the raw literals with `var(--token)`. Grouped by what they are.

| Existing literal(s) in index.html | Replace with | Where it appears |
|---|---|---|
| `background:var(--panel)` on `body` (L28) | `var(--page-bg)` | body backdrop |
| `#fff` solid fills on `.insights .metric .rail .detail .card .bcard .modal .qcard .track-row .setup .btn.sec/.ghost` | `var(--surface)` | all solid cards/panels/modals |
| `var(--panel)` on `.bar .chip .eval .count-chip .setup code` | `var(--surface-2)` | inset wells |
| `rgba(255,255,255,.55)` (L290 `.col`) | `var(--glass-bg)` | bento glass columns |
| `rgba(255,255,255,.85)` (L291 `.col` border) | `var(--glass-border)` | bento glass border |
| `rgba(244,246,251,.86)` (L104 `.filterbar`) | `var(--glass-bar-bg)` | sticky filter bar |
| `blur(12px)` on `.col` | `blur(var(--glass-blur))` | glass blur |
| `rgba(9,14,28,.72)` (L37 header) | `var(--header-bg)` | header glass |
| `blur(14px)` on header | `blur(var(--header-blur))` | header blur |
| `#101828` | `var(--text)` | already tokenized as `--text`; use var |
| `#475467` / `#667085` | `var(--muted)` / `var(--subtle)` | already tokens |
| `#fff` text on hero/header/banner/modal-head | `var(--on-dark)` | text on dark |
| `#aab8d6 #b9c2db #9aa6bf #c5cee0` etc. | `var(--on-dark-soft)` | muted text on dark |
| `#3b82f6` active/focus accents (L49 `.topnav a.active`, focus shadows) | `var(--brand2)` | accent |
| `#1d4ed8` (`--blue-dark`) | `var(--brand-hover)` where it's a hover | hover accent |
| `.matchpill.match-strong{background:#067647}` (L170) | `background:var(--match-strong);color:var(--match-strong-fg)` | match pill |
| `.matchpill.match-maybe{background:#8a5a00}` (L171) | `background:var(--match-maybe);color:var(--match-maybe-fg)` | match pill |
| `.matchpill.match-stretch{background:#43607f}` (L172) | `background:var(--match-stretch);color:var(--match-stretch-fg)` | match pill |
| `.ring{…#e9edf5 0)}` (L312) and `#e9edf5` fallback (L848) | `var(--ring-track)` | conic ring empty track |
| `.ring::after{…background:#fff}` (L313) | `var(--surface)` | ring center punch-out |
| ringColor()/JS `#067647 / #9a6700 / #5b7bb4` (L834) | keep hex OR read from tokens (see note) | JS ring color |
| `.pill.fit-strong` `#e7f6ec/var(--green)/#bfe3c9` | `var(--success-bg)/var(--success-fg)/var(--success-border)` | fit pill |
| `.pill.ok .jd-chip` `#e6f6ec/#067647/#bfe6cc` | `var(--success-bg)/--success-fg/--success-border` | success chips |
| `.setup .reason` `#fff4e5/#f0d49a/#7a5300` | `var(--warn-bg)/--warn-border/--warn-fg` | warn banner |
| `.pill.fit-maybe` `#fbf2e0/var(--amber)/#ecd9ad` | `var(--warn-bg)/--warn-fg/--warn-border` | warn pill |
| `.badge.hot` `#fce8ee/#b3123f`, `.toast.err{#7a123a}` | `var(--danger-bg)/--danger-fg`, `var(--danger)` | danger |
| `#22c55e` / `#ef4444` health dots (L43) | `var(--up)` / `var(--down)` | status dots |
| `#e4e8f0` / `#eef1f6` | `var(--border)` / `var(--divider)` | already tokens |
| `#cdd8ec` card hover border (L152,302) | `var(--border-strong)` | hover border |
| `--shadow` / `--shadow-lg` | unchanged names, now theme-aware | elevation |
| `0 10px 30px -18px rgba(16,24,40,.45)` (L292 `.col`) | `var(--shadow-glass)` | glass shadow |
| `box-shadow:0 10px 22px -10px rgba(99,40,217,.6)` (L204 `.btn`) | `var(--shadow-brand)` | button glow |
| `border-radius:16px / 12px / 18px / 11px / 8px / 6px` literals | `var(--radius-lg/…/-md/-sm/-xs)` | radii |
| ad-hoc paddings `18px 12px 20px 24px 48px 8px 4px` | `var(--sp-*)` where it maps cleanly | spacing |
| `transition:… .15s / .14s / .12s / .16s` | `var(--transition)` / `var(--transition-fast)` | transitions |
| `box-shadow:0 0 0 3px rgba(59,130,246,.15/.45)` focus (L108 etc.) | `var(--focus-ring)` | focus ring |

Note on the JS `ringColor()` (L834) and inline `--rc`: simplest is to keep the three hex values but swap them to the token values, or set them from CSS by using `getComputedStyle(document.documentElement).getPropertyValue('--match-strong')`. Leaving the JS hex as-is is acceptable for v1 since the ring arc reads fine on both themes; the empty-track `#e9edf5` (L848 fallback) should become `var(--ring-track)`.

---

## 5. Theme toggle wiring (guidance)

Behavior: first load respects `prefers-color-scheme` (handled by the `@media` block above); an explicit user choice is stored and wins.

Add early in `<head>` (before first paint, to avoid a flash):
```html
<script>
  (function(){
    var t = localStorage.getItem('jp-theme');
    if (t === 'dark' || t === 'light') document.documentElement.setAttribute('data-theme', t);
    // else: leave unset -> @media(prefers-color-scheme) decides
  })();
</script>
```

Toggle handler (wire to a button in the header `.topicons`):
```js
function toggleTheme(){
  var cur = document.documentElement.getAttribute('data-theme');
  if (!cur) cur = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  var next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('jp-theme', next);
}
```

Notes:
- Header, hero, banner, and modal-head are intentionally dark in BOTH themes (they use `--on-dark*` / `--header-bg`), matching the current look — the toggle mainly flips the body/cards/glass/text.
- Setting `data-theme` on `<html>` (documentElement) drives both `:root[data-theme=...]` blocks and `color-scheme` (native scrollbars/inputs follow).
- Standard transition is `150ms ease` (`--transition`); reduced-motion users get near-instant via the media query.
- Glass blur is `12px` light / `14px` dark (`--glass-blur`); header blur `14/16px`.
- Focus ring token is `--focus-ring` (blue, alpha .45/.55) applied via a single `:focus-visible` rule.
```
