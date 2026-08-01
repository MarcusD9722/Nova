# aetherlings

## Brief
A Pokémon-Emerald-style monster-taming RPG: ten towns, a story arc, eight gym-equivalents,
wild encounters, catching, evolution, and a five-battle endgame. Original creatures and world
(Nintendo/Game Freak's names, designs and art are copyrighted), but the systems underneath are
deliberately Gen-3-faithful — same damage formula, same catch formula, same stat maths.

Built as a self-contained HTML5 Canvas game, mobile-first for iPhone, with the realistic path
to a real iOS app via Capacitor later. Same approach as `last-stand` in this repo.

## Status
content-complete v1 — 84/84 self-tests passing, critical path verified in browser

## Summary
**World.** The region of Verdane: ten towns, nine routes, two caves, a marsh, a cult hideout,
and the Conclave — 62 maps in total. You start in Willowmere, take a starter from Professor
Rowan, and work east and north through eight Sanctums for their Sigils while the Ashen Hand
digs toward the World-Root. It ends at Aurel Citadel with four Conclave Masters and the
Champion.

**Creatures.** 56 species across 20-odd evolution families, including three 3-stage starters,
a pseudo-legendary line, and the titan Verdurion. 12 elemental types with a full effectiveness
matrix; 84 moves.

**Systems.** Gen-3 damage formula with STAB, criticals, and the 85–100% roll. Stat stages,
five status conditions plus confusion, the Gen-3 catch formula with shake checks, the Gen-3
escape formula, EXP sharing, level-up moves, level and stone evolutions, a party of six plus
storage, four bag pockets, shops, healing Hearths, five field skills gated behind Sigils, and
three save slots in `localStorage`.

**Art.** No image files anywhere. Creatures are drawn by a parametric renderer — each species
declares a body archetype, a palette and feature flags (horns, wings, fins, aura…), and the
renderer draws it into a 32×32 buffer that upscales with smoothing off for a pixel-art look.
Tiles, buildings, water and people are drawn the same way.

## Files
- `index.html` — shell, CSS, canvas, on-screen controls, script tags. No build step.
- `src/data-types.js` — the 12 types and the effectiveness chart
- `src/data-moves.js` — 84 moves (plus Struggle, the out-of-PP fallback)
- `src/data-species.js` — 56 species: stats, learnsets, evolutions, sprite specs, dex text
- `src/data-items.js` — items, bag pockets, shop stock
- `src/data-maps.js` — all 62 maps, built from declarative paint ops rather than raw grids
- `src/data-story.js` — every NPC, all dialogue, trainers, the 8 Wardens, the Conclave
- `src/render-sprites.js` — parametric creature renderer, tile renderer, overworld people
- `src/engine-core.js` — loop, input, stat maths, creature model, save/load
- `src/engine-battle.js` — turn resolution, damage, status, catching, AI
- `src/engine-overworld.js` — movement, encounters, warps, field skills, script runner
- `src/engine-ui.js` — title, name entry, starter pick, menus, party, bag, storage, shops
- `src/tests.js` — 84 in-page assertions (see below)
- `src/main.js` — boot and canvas fitting

**Why several files rather than one `index.html` like `last-stand`:** this game is roughly
twenty times larger and would be unmaintainable inline. They load as **classic `<script>`
tags, not ES modules** — classic scripts work straight from `file://`, whereas modules are
blocked there by CORS. So double-clicking `index.html` still works with no server.

## How to run
- Double-click `index.html`, or open it in any modern browser. No server, no build, no deps.
- Or, from the repo root, serve it: `python -m http.server 8123 -d projects/aetherlings`
  (there is an `aetherlings` entry in `.claude/launch.json` that does exactly this).

**Controls.** Arrow keys or WASD to move; `Z`/`Space`/`Enter` = A (talk, confirm); `X` = B
(cancel, back); `Esc` or `M` = menu. On a phone, use the on-screen D-pad and A/B buttons.

**Getting started.** Pick a save slot, name your tamer, walk south-west around Rowan's
laboratory to its door, and talk to him for a starter. Then head north out of Willowmere.

## Self-tests
Open `index.html?test=1`. It runs 84 assertions and prints them to the console and to an
overlay. They cover the type chart, data integrity (every evolution target, learnset move,
shop item and script reference resolves), the damage and catch formulas against fixtures,
levelling and evolution, save/load round-tripping, and — most usefully — a crawl of the entire
warp graph that checks every map is reachable from the start, every warp lands on standable
ground, every NPC can be walked up to, and no NPC blocks a chokepoint.

That crawl is what makes 62 maps maintainable; it caught several real bugs during the build
(warps buried under map borders, scattered trees landing on NPCs, an elder standing in the
only approach to the laboratory door).

## Toward iOS (future, needs a Mac)
The game is a dependency-free web app, so packaging is straightforward when a Mac is available:
`npm create @capacitor/app` → copy `index.html` and `src/` into `www/` → `npx cap add ios` →
`npx cap open ios` → build and sign in Xcode. No code changes needed. The viewport, safe-area
insets, `apple-mobile-web-app-capable` meta tags and touch controls are already in place, and
the layout was checked at 375×812.

## Progress log
- 2026-07-26 — Project built in one pass: engine, 62 maps, 56 species, 84 moves, full story.
- 2026-07-26 — Self-test suite written; fixed warps buried by map borders, trees scattered
  over NPCs and paths, cave encounter tiles, and Sigils not being readable as story flags
  (which would have left every Warden re-battleable forever).
- 2026-07-26 — Browser verification found and fixed three more: a battle that hung whenever a
  combatant fainted mid-turn (the faint handler was being skipped along with the rest of the
  turn), a softlock when every move ran out of PP (no Struggle fallback existed), and a crash
  opening the Team screen before receiving a starter.
- 2026-07-26 — Verified end to end: new game → starter → wild encounter → catching (7 throws,
  correct shake behaviour) → Warden battle → Sigil → field skill → cutting brush → shop →
  save → reload from title. Battle matrix covers wild win/loss, trainer win, forced switch
  after a faint, the 6-Aetherling Champion fight, and the Struggle path.

## Next steps / suggestions
- **Balance is first-pass.** Encounter rates, the EXP curve and Warden difficulty are
  reasoned-about numbers, not tuned ones. Expect to adjust after actually playing.
- No sound at all yet — that's the most obvious gap.
- Battle AI never switches; it only picks the best move. Switching would make Wardens harder.
- Trainer rematches, a proper dex viewer, and running shoes would all be cheap additions.
- The Verdurion encounter is a one-shot: if you knock it out, it's gone. Worth reconsidering.
