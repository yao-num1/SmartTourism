# Design — 智旅灵犀 SmartTourism

A locked design system for this app. Every page redesign reads this file before
emitting code. Do not regenerate per page — extend or amend this file when the
system needs to grow.

## Genre
atmospheric — dark canvas with warm radial blooms, confident sans display,
expressive but plain-English copy, single warm accent. The aesthetic of a tool
you'd actually want to use after dark.

## Theme
Custom OKLCH palette tuned for travel/exploration mood.
- `--color-paper`   oklch(8% 0.008 170)    — deep ink-green
- `--color-paper-2` oklch(12% 0.010 170)   — raised surface
- `--color-paper-3` oklch(16% 0.012 170)   — high surface
- `--color-ink`     oklch(94% 0.003 60)    — warm white
- `--color-ink-2`   oklch(72% 0.008 60)    — muted warm
- `--color-rule`    oklch(24% 0.010 170)   — divider
- `--color-rule-2`  oklch(20% 0.009 170)   — secondary divider
- `--color-accent`  oklch(72% 0.16 75)     — amber-gold (lantern light)
- `--color-accent-2` oklch(78% 0.14 85)    — warm highlight
- `--color-focus`   oklch(76% 0.20 75)     — focus ring
- `--color-muted`   oklch(48% 0.008 170)   — de-emphasised
- `--color-success` oklch(68% 0.14 150)    — jade green
- `--color-danger`  oklch(60% 0.16 25)     — warm red

## Typography
- Display: "Noto Serif SC", "Source Han Serif CN", "Songti SC", serif — weight 600, style normal
- Body: "Inter", "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif — weight 400
- Mono: "Geist Mono", "SF Mono", "Fira Code", monospace — weight 400
- Display tracking: -0.02em
- Body size: 14px floor, 1rem default
- Type scale anchor: text-display = clamp(2.5rem, 4vw + 1rem, 4rem)

## Spacing
4-point named scale:
- --space-3xs: 0.25rem; --space-2xs: 0.5rem; --space-xs: 0.75rem;
- --space-sm: 1rem; --space-md: 1.5rem; --space-lg: 2rem;
- --space-xl: 3rem; --space-2xl: 4.5rem; --space-3xl: 7rem;

## Motion
- Easings: cubic-bezier(0.16, 1, 0.3, 1) named --ease-out
- Reveal pattern: fade only, no slide, no bounce
- Reduced-motion fallback: opacity-only, <= 150ms
- Hover: 220ms ease-out

## Microinteractions stance
- Silent success — no celebratory toasts
- Hover delay 0ms · focus delay 0ms
- Focus rings instant and visible

## CTA voice
- Primary CTA: filled accent, sharp corners (2px radius), uppercase label
- Secondary CTA: 1px rule border, transparent fill, same sharp corners
- No pill-shaped buttons — sharp geometry throughout

## What pages MUST share
- The wordmark / logotype style
- The accent colour and its placement (<= 5% per viewport)
- The display + body fonts
- The CTA voice (sharp corners, padding rhythm)
- Section heading rhythm

## What pages MAY differ on
- Background texture intensity per page
- Card elevation levels

## Exports

### tokens.css
```css
:root {
  --color-paper:      oklch(8% 0.008 170);
  --color-paper-2:    oklch(12% 0.010 170);
  --color-paper-3:    oklch(16% 0.012 170);
  --color-ink:        oklch(94% 0.003 60);
  --color-ink-2:      oklch(72% 0.008 60);
  --color-rule:       oklch(24% 0.010 170);
  --color-rule-2:     oklch(20% 0.009 170);
  --color-accent:     oklch(72% 0.16 75);
  --color-accent-2:   oklch(78% 0.14 85);
  --color-focus:      oklch(76% 0.20 75);
  --color-muted:      oklch(48% 0.008 170);
  --color-success:    oklch(68% 0.14 150);
  --color-danger:     oklch(60% 0.16 25);

  --font-display: "Noto Serif SC", "Source Han Serif CN", "Songti SC", serif;
  --font-body: "Inter", "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif;
  --font-mono: "Geist Mono", "SF Mono", "Fira Code", monospace;

  --space-3xs: 0.25rem;  --space-2xs: 0.5rem;  --space-xs: 0.75rem;
  --space-sm:  1rem;     --space-md:  1.5rem;  --space-lg: 2rem;
  --space-xl:  3rem;     --space-2xl: 4.5rem;  --space-3xl: 7rem;

  --text-xs: 0.75rem;  --text-sm: 0.875rem; --text-md: 1.125rem;
  --text-lg: 1.375rem; --text-xl: 1.75rem;  --text-2xl: 2.25rem;

  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --dur-short: 220ms;
  --radius-sharp: 2px;
  --radius-sm: 4px;
  --radius-md: 8px;
}
```
