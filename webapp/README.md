# Chev Terminal — Modular Frontend Architecture

This document describes the production file structure for the Chev Terminal web
dashboard (`webapp/`), produced by a three-phase refactor of the original
single-file monolith (`index.html`): **Phase 1 CSS modularization**, **Phase 2
JS split**, and **Phase 3 Final Hardening**.

---

## Load model (critical to understand before touching anything)

`index.html` is now a thin shell. Styles and scripts are loaded as separate files:

- **CSS** — 14 `<link rel="stylesheet">` tags in the `<head>`, in cascade order:
  `variables → layout → components → animations`. (Could *not* use `@import`:
  it blocks and loads serially, hurting performance.)
- **JS** — classic `<script src>` tags (no `type="module"`), loaded **in
  document order**, sharing one global scope exactly as the original monolith
  did.

### Why classic scripts, not ES modules?
The original 7,610-line script relied on **76 mutable global `let`/`const`
bindings** reassigned across the whole file. ES-module `import`s are read-only
(live-binding wall), so a true ESM split would have required rewriting every one
of those bindings into a state module — a large, unverifiable change. Instead we
kept the proven approach: slice the monolith into ordered classic files with a
**byte-exact reconstruction invariant** (concatenating the slices equals the
original script, verified). This gives a clean, scannable structure with
**zero behavioural regression**.

### Execution order (this *is* the dependency chain)
1. `js/utils/error-handler.js` — **first**, installs the global error boundary.
2. External libs: `lightweight-charts`, `twemoji`.
3. `js/config/state.js` — **declares and initializes all mutable globals**
   (`currentSymbol`, `currentTf`, `groups`, `API_BASE`, `pollTimer`,
   `chevToolsOn`, `activeIdeaRow`, …). Must stay first among app modules.
4. `js/charts/chart-core.js` … through `js/ui/status-bar.js` — charting, then UI
   domain modules, then boot/status. Order is execution-critical: function
   declarations are hoisted into the shared global scope, but top-level
   `const`/`let` bindings follow TDZ rules, so dependents must load after
   `config/state.js`.

> **Deferred execution note:** `defer` was deliberately **not** added to the 17
> app modules. They currently run synchronously during parse and several assume
> the DOM / preceding globals are already present. Adding `defer` would shift
> them to after-parse execution and is a *future* performance workstream that
> requires a careful per-module review (it is independent of the error handler,
> which is synchronous by design so it can catch init errors in the modules
> that follow it).

---

## File map

```
webapp/
├── index.html                 # Shell only: <link> tags (head) + <script src> tags (body)
├── css/
│   ├── variables.css          # Design tokens / :root custom properties
│   ├── layout.css             # Reset, body shell, scrollbars
│   ├── animations.css         # ALL @keyframes + their trigger classes (isolated)
│   └── components/
│       ├── topbar.css         # #topbar, vitals strip, freshness stamps
│       ├── panels.css         # Left/right panel shells, intel tabs, radar
│       ├── trading-logs.css   # .watchRow, .logRow, sparklines, DNA, breakdowns
│       ├── arsenal.css        # #chevArsenal, dropdown menus
│       ├── chart.css          # #chartColumn/#chartWrap, toolbar, canvas
│       ├── chart-popups.css   # #objectTree, #chartToolPopup, #drawCtx
│       ├── toasts.css         # Notifications + status bars
│       ├── intel.css          # Brain-state banner, hypothesis cards, scenario, timeline, conviction
│       ├── hypothesis-tracker.css
│       ├── command-palette.css
│       └── strategy.css
└── js/
    ├── utils/error-handler.js # Global error boundary (window.onerror / onunhandledrejection)
    ├── config/state.js        # Global state, constants, mutable globals (load FIRST)
    ├── charts/
    │   ├── chart-core.js      # Lightweight Charts init + data feeding
    │   ├── indicators.js      # EMA/BB/VWAP/RSI toggles
    │   ├── rsi-canvas.js      # RSI sub-panel drawing canvas
    │   ├── timeframe-load.js  # Timeframe buttons + initial load
    │   ├── drawing.js         # Drawing tools, canvas annotations, object tree (contains 2 IIFEs)
    │   └── engine.js          # Engine tab overlays, engine/hypo caches, layers tab
    └── ui/
        ├── panel-toggle.js    # Right-panel (Intel) drawer + tab switching (isolated)
        ├── watchlist.js       # Left watchlist: groups, rows, prices
        ├── trading-logs.js    # Live/closed trades, trade overlay, gold-glow active state
        ├── chev-corner.js     # Chev's Corner idea cards
        ├── jane.js            # Jane's trades
        ├── chat.js            # Chev chat panel
        ├── sparklines.js      # Watchlist sparklines + proximity alerts
        ├── radar.js           # Radar multi-symbol scan + hypothetical entry
        ├── trade-dna.js       # Trade DNA hypothesis→trade linking
        └── status-bar.js      # Clock + Dexter connection indicator
```

---

## Phase 3 — Final Hardening

### 1. Global Error Boundary (`js/utils/error-handler.js`)
- Installs `window.onerror` and `window.onhandledrejection` as the **first**
  script, so it catches initialization and runtime errors in all modules.
- **Never blocks the terminal:** handlers return `true` / swallow exceptions so a
  single broken module cannot blank the app. The handler itself is wrapped in
  guards and cannot throw.
- **Classification** (so operators can triage): each error is labelled
  `Chart Engine Exception` (lightweight-charts / canvas / drawing pipeline) or
  `Critical UI Failure` (DOM / panel / event wiring), else `Unhandled Exception`.
- **Source identification:** extracts `file:line` from the stack when available,
  and de-duplicates repeat errors (capped) to avoid console flooding.

### 2. Dependency audit
- Verified all app modules are classic, in-order scripts; `config/state.js`
  initializes the 76 mutable globals before any consumer runs.
- See the "Deferred execution note" above for the deliberate decision *not* to
  add `defer` to app modules in this pass.

### 3. Dead-code audit
- A reliable, conservative dead-code sweep was attempted. The automated AST /
  text scanners proved **unreliable** on this codebase (they flagged core boot
  functions such as `loadChart`, `selectIdea`, `renderTradingLogs` as "unused"),
  so **no `// TODO: Verify & Remove` markers were auto-injected** — doing so
  blindly would risk the zero-regression guarantee. Recommend a runtime coverage
  pass (or a hand-verified call-graph) as a separate, careful task before any
  function is deleted.

### 4. Integration audit
- **CSS↔JS bridge:** every CSS class injected by JS (`classList.add` /
  `className =`) was checked against the extracted stylesheets. Result: bridge
  is intact — no injected class lacks a matching rule (initial false positives
  were case-sensitivity artifacts; both `@keyframes pulseFlash` and
  `.logRow.pulse` are present).
- **Global-state sync:** the 76 mutable globals are all declared/initialized in
  `js/config/state.js` (loaded first), so every later module sees a consistent
  state.

---

## Verification invariant (how we guarantee zero regression)
For the JS split: concatenating the 17 extracted files equals the original
inline script **byte-for-byte** (verified), and every file passes `node --check`.
Do not break this invariant when editing: if you add/remove a file, re-run the
concatenation-equality check.
