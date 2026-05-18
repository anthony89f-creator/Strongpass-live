# Strongpass Live — Changelog

## BUILD-20260518-B (2026-05-18) — Overlay registry + operator polish

### Fixed: pgmChanged undeclared variable in onStateUpdate PGM reconstruction

`var pgmChanged = false` was missing before the `_PGM_GROUP_ORDER.forEach` block in
`onStateUpdate`. The variable was set inside the callback and read after it — in non-strict
mode this created an implicit global; in strict mode it would throw ReferenceError.

---

### Refactored: All OVERLAYS.find() hot-path calls replaced with OVL_BY_KEY

O(1) registry lookup introduced in BUILD-20260518-B now used consistently:
- `selectPVW()`: overlay resolution at transition time
- `_doTake()`: overlay lookup before POST
- `_doClean()`: overlay lookup for group determination (×1 direct, ×1 scene-check)
- `onStateUpdate()` PGM loop: group-frame creation

Remaining `OVERLAYS.find` is the fallback group search in `onStateUpdate` (finds by group+pgmKey
properties, not by key — OVL_BY_KEY does not apply there).

---

### Removed: _applyScaleToBody() dead code with unconnected MutationObserver

`_applyScaleToBody()` was retained as reference after the double-scaling fix in BUILD-20260517-B
but was never called from `applyScale()`. It contained a `MutationObserver` stored at
`frame._scaleObserver` that was never `.disconnect()`ed when frames were destroyed — a memory
leak pattern if the function had been called. Removed entirely.

---

### Improved: applyScale() debug operations gated behind DEBUG flag

`getBoundingClientRect()` and `getComputedStyle()` were called unconditionally on every
`applyScale()` invocation (ResizeObserver, window resize, every TAKE/CUT/CLN, init).
These force layout reflow even when the results are only used for DL() debug logs.
Both calls now execute only when `DEBUG=true` (`?debug=1`). The `var prev` variable
(only used in a DL() log) was also removed from the frames loop.

---

### Improved: _enqueue() console.logs gated behind DL()

The "accepted/running/done" queue slot logs ran on every overlay button click and every
TAKE/CUT/CLN operation regardless of debug mode. Now gated behind DL(). Error paths
(fn rejected, fn threw, prev-slot rejected) remain unconditional.

---

### Improved: selectPVW() intermediate console.logs reduced

Removed 8 intermediate console.log calls from selectPVW() that logged internal state
transitions ("assigning pvwSelection", "pvwSelection assigned OK", "ovl resolved",
"calling _createPVWFrame", "_createPVWFrame returned", "iframe-ready confirmed",
"ABORT: key=... destroy path", "selectPVW COMPLETE", "selectPVW FINALLY").
Kept: EXCEPTION (error), ABORT stale/unknown-key (error), green overlay-ready
confirmed (styled success, useful at a glance). All removed logs remain as DL() if
DEBUG=true.

---

### Improved: postUpdate() no longer calls renderPGMStatus/renderPGMVisibility

`postUpdate()` handles ctrl-panel actions (leaderboard count, lineup index, etc.) that
update `liveState` fields unrelated to PGM iframe presence. Calling `renderPGMStatus()`
and `renderPGMVisibility()` on every ctrl action was redundant — SSE echo handles those
after the server processes the update. Removed from `postUpdate()`; both functions still
called from `onStateUpdate`, `_doTake`, `_doClean`, `_doCleanAll`.

---

### Added: Per-group status strip between monitors and overlay selector

New `#group-strip` element shows A/B/C group live state at a glance. Each pill shows:
- Group letter badge
- Overlay name (or "Clear")
- Red dot + highlighted border when live

Populated by `renderGroupStrip()`, called from `onStateUpdate`, `_doTake`, `_doClean`,
`_doCleanAll`, and `init()`.

---

### Added: Group badge on overlay selector buttons

Each `.ovl-btn` now has `data-group="A|B|C"` set at init. CSS `::before` renders the
group letter as a small badge in the bottom-left corner of each button, giving the
operator immediate visual grouping context without needing to memorise which overlays
share mutual-exclusion groups.

---

### Added: Dynamic CUT label showing target group

`updateCutBtn()` sets the CUT button text to "CUT A", "CUT B", or "CUT C" based on the
current PVW selection's group. Falls back to plain "CUT" when no overlay is selected.
Called alongside `updateTakeBtn()` in `selectPVW()` and after optimistic updates in
`_doTake`, `_doClean`, `_doCleanAll`.

---

## BUILD-20260518-A (2026-05-18) — Stabilisation pass

### Fixed: console.trace() in applyScale() causing severe console noise

**Symptom:** Every scale recalculation (on every ResizeObserver fire, every window resize, every TAKE/CUT/selectPVW) emitted a full JS stack trace via `console.trace()`. Stack traces are many orders of magnitude more expensive than `console.log()` and caused measurable DevTools slowdown during long sessions.

**Fix:** Removed `console.trace()` from `applyScale()` entirely. The function still logs scale calculations via `DL()` when `DEBUG=true`.

---

### Fixed: Per-SSE-tick verbose logging with no production gate

**Symptom:** `DL()` logged on every SSE message, every `onStateUpdate()`, every `renderOvlButtons()` entry/exit. In production with 1-3 SSE ticks per second this produced hundreds of log lines per minute.

**Fix:** Added `var DEBUG = /[?&]debug=1/.test(location.search)`. `DL()` returns a no-op when `!DEBUG`. Also gated watchdog `console.log` object dump behind `DEBUG`. Warn/error paths remain unconditional. Enable full logging via `?debug=1` in the director URL.

---

### Fixed: Verbose PVW message handler in all 6 overlay files

**Symptom:** Every `postMessage` to a PVW iframe (fired on every SSE tick via `pushToPVW()`) triggered:
- `JSON.stringify(e.data).slice(0, 200)` — serialises the full state object
- `getBoundingClientRect()` — forces layout reflow
- `getComputedStyle()` — forces style recalc
- 6–8 `console.log()` calls

This ran in all 6 overlays on every SSE event while an overlay was in PVW, causing consistent hidden CPU overhead.

**Fix:** Stripped the diagnostic block from all 6 overlays (lowerthird, reps, leaderboard, lineup, h2h, champion). Reduced PVW message handler to the minimal correct form: call `onStateUpdate`, send `overlay-ready` once. Error handler preserved.

---

### Fixed: Runtime HUD always visible, covering conn-wrap

**Symptom:** `#runtime-hud` was `display:block` at all times at `position:fixed; top:6px; right:8px; z-index:9999`, covering the connection status indicator in the header.

**Fix:** `#runtime-hud` is now `display:none` by default. It activates only when debug mode is toggled (D key), same as the PVW scale HUD. The `_hudUpdate()` function now checks `classList.contains('on')` before redrawing. HUD content updated: shows SSE state, removes hardcoded `queue: running` line.

---

### Fixed: CLN button calling group-selective clean instead of global clean

**Symptom:** Both CUT and CLN called `goClean()`, which POSTs `/broadcast/clean_group` and clears only the selected PVW group. CLN should clear all groups. Operators expecting a full blackout got only a group-level cut.

**Fix:** Added `_doCleanAll()` / `goCleanAll()` that POSTs `/broadcast/clean` (the global clean endpoint). CLN button now wired to `goCleanAll()`. Optimistic local update clears all group flags and destroys all PGM frames. CUT and Escape key retain group-selective behaviour.

---

### Improved: Typography and operator readability

All sub-10px font sizes bumped for readability at production monitor distance:

| Element | Before | After |
|---|---|---|
| `.header-sub` | 7px | 9px |
| `.header-title` | 12px | 13px |
| `.conn-text` | 9px | 10px |
| `.ctx-item` | 9px | 10px |
| `.ctx-item:first-child` | 10px | 12px |
| `.mon-label` | 8px | 10px |
| `.live-badge` | 6px | 8px |
| `.t-eyebrow` | 6px | 8px |
| `.monitor-current` | 10px | 12px |
| `.mfoot-tag` | 7px | 9px |
| `.ovl-selector-label` | 6px | 9px |
| `.ovl-btn` | 11px | 12px |
| `.ctrl-label` | 7px | 9px |
| `.ctrl-btn` | 10px | 11px |
| `.ctrl-select` | 11px | 12px |
| `.ctrl-input` | 11px | 12px |
| `.status-bar` | 9px | 11px |
| `.auth-notice` | 10px | 12px |
| `#cut-btn` | 12px | 13px |
| `#clean-btn` | 10px | 11px |

Also improved dim text contrast and increased ovl-btn, ctrl-btn padding for cleaner tap targets.

---

### Fixed: _hudUpdate() hardcoded "queue: running"

The runtime HUD always displayed `queue: running` regardless of actual queue state. Replaced with SSE state (`connecting/open/closed`) and a note pointing to `?debug=1` for full logging.

---

## BUILD-20260517-C (2026-05-17) — Previous stable

**Tag:** `v2-runtime-stable`

### Fixed: Lineup category selector not updating PVW display

**Symptom:** Clicking category buttons (U90, U105, etc.) in the lineup ctrl panel updated `liveState.lineupIndex` and returned 200 from `/update`, but `#disp-name` in the PVW overlay always showed the same category (whichever athletes were in `state.athletes`).

**Root cause:** `state.athletes` in the broadcast state contains only athletes for the **current active competition category** (a single leaderboard). `getCategoryData()` in `lineup.html` filters athletes by category name — all athletes matched only the one live category, so `getCategoryData()` returned a single-element list, making `lineupIndex % 1 = 0` always.

**Fix (two files):**

1. `director.html` — `pvwStateFor()`: Stop deleting `lbStandings` for the lineup overlay.
   `lbStandings` is a `{category: [athletes]}` dict containing all 6 categories' athletes.
   Other overlays still have `lbStandings` stripped (it's ~120-athlete × 6 categories worth of data).

2. `lineup.html` — `getCategoryData()`: If `state.lbStandings` is present, use it to build
   the per-category athlete lists. Falls back to `state.athletes` filter if `lbStandings`
   is absent (backwards-compatible with direct SSE state in PGM mode).

**Verified:** `#disp-name` correctly updates to U90, U105, etc. when category buttons are clicked. `liveState.lineupIndex` and PVW display are in sync.

---

### Fixed: CUT returning HTTP 405

**Symptom:** `goClean()` POSTed to `/broadcast/clean_group` and received 405 Method Not Allowed on production.

**Root cause:** `POST /broadcast/clean_group` existed in local `server.py` but had never been deployed to production. Production only had `/broadcast/clean` (global clean, all groups).

**Fix:** Deployed `server.py` with the `broadcast_clean_group` route to production and reloaded the service. The route clears only the flags for the requested group (`A`, `B`, or `C`), leaving other groups' overlays live.

**Verified:** `POST /broadcast/clean_group {"group":"A"}` returns `{"ok":true,"group":"A","cleanAt":...}` with status 200.

---

## BUILD-20260517-B (2026-05-17)

### Fixed: Two-phase PVW handshake — state push race condition

**Symptom:** Intermittent blank preview panes, especially on lowerthird and reps. State was sometimes pushed into the overlay before its `window.addEventListener('message', ...)` was attached, so the message was lost.

**Root cause:** The director was pushing initial state from `iframe.onload`. On some engines/pages, the overlay's inline `<script>` blocks haven't executed yet when `onload` fires. The `postMessage` went into a void.

**Fix:**
- All 6 overlay HTML files: Added `overlay-runtime-ready` signal as the very last statement in the `<script>` block, after all listeners and `onStateUpdate` are defined:
  ```javascript
  if (window.PVW_MODE) {
    window.parent.postMessage({ type: 'overlay-runtime-ready' }, '*');
  }
  ```
- `director.html` — `_createPVWFrame()`: Replaced `iframe.onload` state push with a two-phase message handler. State is only pushed after `overlay-runtime-ready` is received. `iframe.onload` is kept only for lifecycle logging.
- Added 5-second timeout failsafe so the transition queue never permanently deadlocks if an overlay fails to complete the handshake.
- Added 50ms cancel poll so rapid clicks abort stale in-flight iframe loads.

**Verified:** All 6 overlays complete the handshake in ~260–285ms. Handshake sequence confirmed: `runtime-ready received` → `initial state pushed` → `overlay-ready received` → `selectPVW COMPLETE`.

---

### Fixed: Double-scaling of PVW content

**Symptom:** PVW monitor showed tiny content in the top-left corner (~9.4% of expected size). Lower third and reps overlays appeared completely blank.

**Root cause:** `applyScale()` applied `scale(0.306)` to both:
1. The host `<iframe>` element — correct
2. `contentDocument.body` via `_applyScaleToBody()` — incorrect

Combined: `0.306 × 0.306 = 0.094x`. Content appeared at 9.4% size.

**Fix:** Removed the `_applyScaleToBody(el, scale)` call from the `frames.forEach` loop inside `applyScale()`. The host element transform alone is sufficient and correct.

`_applyScaleToBody()` was retained as a function with full diagnostic logging but is not called from `applyScale()`.

**Verified:** Production scale check: `host=0.3063, body=none, doubleScaled=false` for all 6 overlays.

---

### Added: Full instrumentation suite

- `BUILD_ID` stamped in `document.title`, runtime HUD, and BOOT log
- Per-transition ID (`_activeTransitionId`) tracking through entire lifecycle
- `[CLICK]`, `[QUEUE]`, `[PVW#N]`, `[SCALE]`, `[WATCHDOG]` log prefixes
- 2-second watchdog reporting build, stage, taking, pvwSel, frame counts, SSE state
- Scale debug HUD (press `D` in director) showing live pvwBody dimensions and iframe transforms
- Deadlock detector in watchdog: warns if stage is stuck at `src-set:` or `iframe-load:`
- `ResizeObserver` on `pvwBody` for reliable scale updates on flex reflow

---

## Prior to BUILD-20260517-B (pre-instrumentation baseline)

- Director was a single-file VMix-style layout
- `iframe.onload` used for state push (race condition present)
- `_applyScaleToBody()` called from `applyScale()` (double-scaling present)
- No transition queue — rapid clicks could create multiple concurrent iframe loads
- No BUILD_ID or lifecycle stage tracking
- CUT/CLEAN called `/broadcast/clean` (global clear) — no group-selective clean
