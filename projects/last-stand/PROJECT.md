# last-stand

## Brief
A Bloons-TD-style tower defense, re-themed: soldiers defend the base, zombies are the horde.
Same core mechanics — waves/rounds, upgradeable defenses with branching ability paths,
escalating enemy tiers, a lives system, and a cash economy. Built as a self-contained HTML5
Canvas web game (playable now on Windows; the realistic path to iOS later via Capacitor).

## Status
playable v1 — verified in browser

## Summary
Single-file browser game. 4 soldier types, each with 2 upgrade paths of 3 tiers (BTD-style,
with cross-path locking). One fixed winding track. 20-wave campaign ending in a boss wave.
6 zombie tiers (Walker, Runner, Brute, Armored, Spitter that splits on death, and the boss
Horde Lord). Full cash economy and 100-life base.

## Files
- `index.html` — the entire game (HTML + CSS + JS inline, no dependencies, no build step)

## How to run
- Double-click `index.html`, or open it in any modern browser.
- Pick a soldier from the right panel, click the field (off the road) to deploy.
- Click a deployed soldier to open its upgrade panel; buy along one of two paths.
- Press **Start Wave** (or Space) to send the horde. Survive all 20 waves.
- Keys: `1`–`4` select a soldier, `Esc` cancels, speed toggle for 1×/2×.

### Soldiers
- **Rifleman** — cheap single-target. Paths: Marksman (range→dmg→pierce) / Rapid Fire (rate).
- **Gunner** — fast, low damage. Paths: Suppressor (slow→stun) / Heavy Barrel (dmg→armor-pierce).
- **Sniper** — global range, hitscan. Paths: Headhunter (dmg→crit→execute) / Spotter (rate→2 targets).
- **Grenadier** — splash. Paths: Demolitions (blast/dmg) / Incendiary (burn DoT).

## Toward iOS (future, needs a Mac)
The game is a dependency-free web app, so packaging is straightforward when a Mac is available:
`npm create @capacitor/app` → drop `index.html` into `www/` → `npx cap add ios` →
`npx cap open ios` → build/sign in Xcode. No code changes to the game needed.

## Progress log
- 2026-07-21 — Project started; built full game core in a single `index.html`.
- 2026-07-21 — Verified in browser: no console errors, tower placement / waves / upgrades /
  economy / lives / win-lose overlays all functional.

## Next steps / suggestions
- Balance tuning after real play (numbers are first-pass).
- Optional follow-ups: multiple maps, sound, sprite art, save/load, difficulty modes.
