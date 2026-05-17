# Strongpass Live — Changelog

## BUILD-20260517-C (2026-05-17) — Current stable

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
