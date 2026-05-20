 # CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A collection of self-contained HTML apps plus a scheduled news digest. HTML files need no build step; open directly in a browser.

- `Test Work/cgt-calculator.html` — Australian Capital Gains Tax calculator (July 2027 reform scenarios)
- `Test Work/Test.Shooter.html` — Canvas top-down shooter
- `Test Work/Test.Tictactoe.html` — Two-player Tic Tac Toe
- `news-digest/` — Morning Telegram digest (Python, GitHub Actions, see `news-digest/README.md`)

## Git Workflow

After completing any meaningful unit of work, commit and push to GitHub:

```
git add <changed files>
git commit -m "concise description of what changed and why"
git push
```

Commit at logical checkpoints — a feature added, a bug fixed, a section of work done — not just at the end of a session. This ensures no progress is ever lost. Keep commit messages specific (e.g. `fix: clamp negative CGT taxable gain to zero` not `update calculator`).

## Running

Open any file directly in a browser:
```
open cgt-calculator.html
open shooter.html
open tictactoe.html
```

## Architecture

### cgt-calculator.html

All logic lives in a single `<script>` block. The core flow:

1. **Scenario detection** (`detectScenario`): classifies the sale into one of three scenarios based on the `REFORM` date constant (`2027-07-01`):
   - **S1** — both purchase and sale before reform: 50% CGT discount applies
   - **S2** — purchased before, sold after reform: gain is split at `july2027Value` input; pre-reform portion gets 50% discount, post-reform portion is indexed by CPI via `cpiGrow` with no discount
   - **S3** — both purchase and sale after reform: pure CPI indexation, no discount

2. **Structure-specific calcs** (`calcS1`, `calcS2`, `calcS3`): each calculator handles three ownership structures — `individual`, `trust`, `company`. Company always pays 30% flat. Individual/trust use marginal rates via `bracketTax` + Medicare levy. A 30% minimum tax floor (`gainTaxWithFloor`) applies to post-reform gains for individuals and trusts (trusts only from `TRUSTFLR = 2028-07-01`).

3. **Rendering** (`renderDetailed`, `renderComparison`): builds HTML strings injected into `#rMain` and `#rComparison`. `renderComparison` always runs all three structures to show a side-by-side table.

4. **Live UI sync** (`syncUI`): fires on date/structure changes to show/hide conditional inputs (e.g. `july2027Value` field only appears for S2).

Key constants: `REFORM = 2027-07-01`, `TRUSTFLR = 2028-07-01`, Australian 2024–25 tax brackets hardcoded in `bracketTax`.

### shooter.html

Canvas game (800×600) using `requestAnimationFrame`. All code in a single inline `<script>`.

**State machine**: `STATE = { MENU, PLAYING, LEVEL_COMPLETE, GAME_OVER }` — `gameLoop` dispatches to update/draw based on `gameState`.

**Object pooling**: Bullets use a fixed-size circular pool (`BULLET_POOL_SIZE = 80`) via `bulletHead` index to avoid GC pressure.

**Level/wave system**: `LEVELS` array defines wave specs per level; `getLevelDef(n)` generates procedural levels beyond the defined ones. Each level has multiple waves; `buildSpawnQueue` shuffles enemy types within a wave, then `startWave` drains the queue on a timer.

**Enemy types** (`ENEMY_TYPES`): `basic`, `fast`, `tank` — differ in speed, HP, size, and color.

**Input**: WASD movement, mouse position for aiming angle (`player.angle`), left-click to shoot. Space/Enter advance screens.

**Rendering order**: background grid → enemies → bullets → particles → muzzle flashes → player → crosshair → HUD → overlay screens.

### tictactoe.html

Simple DOM-based game. `WINS` array stores all 8 winning line combinations. `scores` object persists across `init()` resets. Win highlighting adds `win` CSS class with a pulse animation.
