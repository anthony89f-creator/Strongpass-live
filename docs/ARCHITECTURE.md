# Strongpass Live — System Architecture

## High-Level Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Hetzner VPS (Ubuntu 24.04)           │
│                                                         │
│  nginx (:443 TLS) ──► gunicorn (:8000, 1 worker,       │
│                         gthread pool 32)                │
│                         │                               │
│                    Flask app (server.py)                │
│                         │                               │
│              SQLite (comp.db) + state.json              │
└─────────────────────────────────────────────────────────┘
         │                │               │
         ▼                ▼               ▼
  director.html    overlay iframes    OBS browser sources
  (operator UI)    in PVW mode        in PGM mode
  SSE + fetch      postMessage        SSE
```

---

## State Model

All runtime state lives in a single flat JSON object, `state.json`, locked by a `threading.Lock` on every write. Key fields:

| Field | Type | Description |
|---|---|---|
| `program.scene` | `str\|null` | Key of overlay currently on-air in any group |
| `program.groups` | `{A,B,C: key}` | Per-group on-air overlay; multiple groups can be live |
| `program.takenAt` | `int ms` | Timestamp of last TAKE |
| `program.cleanAt` | `int ms` | Timestamp of last CLEAN |
| `leaderboard` | `bool` | Group A overlay flag |
| `h2h` | `bool` | Group A overlay flag |
| `lineup` | `bool` | Group A overlay flag |
| `lowerThirdVisible` | `bool` | Group B overlay flag |
| `champion` | `bool` | Group B overlay flag |
| `repsVisible` | `bool` | Group C overlay flag |
| `lightsVisible` | `bool` | Group C overlay flag |
| `lineupIndex` | `int` | Active lineup category (0-based) |
| `lineupAuto` | `bool` | Auto-cycle enabled |
| `lineupDuration` | `int` | Seconds per category in auto-cycle |
| `lbCategory` | `str` | Active leaderboard category |
| `lbDisplayCount` | `int` | Rows shown in leaderboard |
| `athletes` | `list` | Leaderboard athletes for **current competition category only** |
| `lbStandings` | `dict` | `{category: [athletes]}` — **all categories** |
| `categories` | `list` | `[{name, color, athleteCount}]` — all categories with athletes |
| `lanes` | `list` | Display lanes for current heat |

`lbStandings` is the only field containing cross-category athlete data. It is used by the lineup overlay PVW to enumerate all weight categories. The `athletes` field only contains athletes for the current active competition category.

---

## Overlay Groups

Overlays are organised into three mutual-exclusion groups. Within each group, only one overlay can be live at a time. Across groups, all can be live simultaneously (multi-layer compositor).

| Group | Overlays | Visual layer |
|---|---|---|
| A | leaderboard, h2h, lineup | Full-frame background |
| B | lowerthird, champion | Mid-layer slot |
| C | reps (+ lights) | Top-layer data strip |

Server-side mapping (`_OVERLAY_GROUPS` in `server.py`):

```python
_OVERLAY_GROUPS = {
    "leaderboard": {"flag": "leaderboard",       "group": "A"},
    "h2h":         {"flag": "h2h",               "group": "A"},
    "lineup":      {"flag": "lineup",            "group": "A"},
    "lowerthird":  {"flag": "lowerThirdVisible",  "group": "B"},
    "champion":    {"flag": "champion",           "group": "B"},
    "reps":        {"flag": "repsVisible",        "group": "C"},
    "lights":      {"flag": "lightsVisible",      "group": "C"},
}
```

---

## PVW vs PGM Iframe Model

**PVW (Preview):** One active iframe at a time, in `#pvw-screen`. Loaded with `?pvw=1`. Sets `window.DISABLE_SSE = true`. Receives state via `postMessage` from director. Used by the operator to preview before taking live.

**PGM (Program):** Up to 3 active iframes (one per group), in `#pgm-screen`. Loaded without the `?pvw=1` flag. Connects via SSE via `sse-client.js`. This is what OBS browser sources replicate.

### Key invariants

- PVW iframes are created on demand (click) and destroyed on overlay switch. No iframe pool.
- PGM iframes are created on TAKE and destroyed on CUT (for that group). Other groups' PGM iframes are unaffected.
- Maximum 1 PVW frame + 3 PGM frames in the DOM at any time.
- Old frame always gets `src = 'about:blank'` before `.remove()` to terminate SSE connections inside.

---

## SSE Architecture

`/stream` uses `gthread`-compatible SSE:
- Each client holds one gthread for the duration of their connection
- A `Condition` variable (`_sse_condition`) is used to broadcast state changes
- Every write to `state.json` increments `_sse_version` and notifies the Condition
- Clients get a `data: <json>\n\n` message on each change; keepalive comment `\n\n` every 30s
- The `results` blob (~126 KB) is **omitted from SSE** to keep messages small

`sse-client.js` (in PGM overlays):
1. Fetches `/state.json` immediately on load (render before SSE completes)
2. Connects `EventSource('/stream')`
3. Calls `window.onStateUpdate(data)` on each message
4. Reconnects with exponential backoff (1s → 2s → 4s → 30s max)
5. Disconnects on `pagehide` (OBS scene switch frees the gthread)
6. Reconnects + fetches fresh state on `pageshow` / `visibilitychange`

---

## postMessage Architecture (PVW)

Director → PVW iframe via `postMessage`:

```
director.html                    overlay iframe (?pvw=1)
     │                                    │
     │  window.DISABLE_SSE = true         │
     │  (set before sse-client.js runs)   │
     │                                    │
     │◄───── overlay-runtime-ready ───────│ (all listeners attached)
     │                                    │
     │──── postMessage(pvwStateFor()) ───►│ (initial state)
     │                                    │ onStateUpdate(d) called
     │                                    │ render() called
     │                                    │ requestAnimationFrame(...)
     │◄───────── overlay-ready ───────────│ (after first paint)
     │                                    │
     │   (settled — handshake complete)   │
     │                                    │
     │──── postMessage(pvwStateFor()) ───►│ (on every SSE tick, pushToPVW)
     │──── postMessage(pvwStateFor()) ───►│ (on ctrl action, _pushCurrentPVW)
```

### `pvwStateFor(ovl)`

Builds the state object sent to a PVW iframe:
1. Clones `liveState`
2. Deletes `results` (large, not needed for rendering)
3. Deletes `lbStandings` **unless overlay is `lineup`** (lineup needs cross-category athletes)
4. Sets all overlay visibility flags to `false`
5. Sets this overlay's `pvwFlags` to `true`

This ensures the PVW overlay renders as if it were the only thing on air.

---

## Scaling Architecture

All overlay iframes are 1920×1080 in CSS. The director's monitor box is smaller (e.g. ~590px wide). `applyScale()` applies:

```
scale = pvwBody.offsetWidth / 1920
iframe.style.transform = `scale(${scale})`
iframe.style.transformOrigin = `top left`
```

**Critical rule: scale the host iframe element ONLY.** Do NOT also apply `scale()` to `contentDocument.body`. Doing so causes double-scaling (`scale²`): if `scale = 0.306`, the content appears at `0.306 × 0.306 = 0.094x` — effectively invisible.

`_applyScaleToBody()` exists as a utility for diagnostic logging only. It is NOT called from `applyScale()`.

`applyScale()` is triggered by:
- `ResizeObserver` on `pvw-body` (catches flex reflow, ctrl-panel open/close)
- `window.resize` (debounced 40ms)
- After TAKE, CLEAN, selectPVW completes

---

## Director State Machine

```
init
  │
  ├─ fetchState() → liveState populated
  ├─ connect() → SSE open, liveState updated on every tick
  │
  ▼
operator clicks overlay button
  │
  ├─ _activeTransitionId++
  ├─ _enqueue(selectPVW(key, id))
  │
  ▼
selectPVW(key, id)
  │
  ├─ [same overlay] → pushToPVW() only
  │
  └─ [new overlay] → _createPVWFrame(ovl, id)
       │
       ├─ install message listener
       ├─ set iframe.src = overlay.src + '?pvw=1'
       │
       ├─ [overlay-runtime-ready] → postMessage(pvwStateFor(ovl))
       ├─ [overlay-ready] → settle → resolve()
       │
       └─ [timeout 5s] → settle('timeout') → resolve() [never deadlocks queue]
```

The transition queue (`_enqueue`) is a serial Promise chain. Concurrent clicks queue up and execute one at a time. A stale-ID cancel poll (50ms interval) inside `_createPVWFrame` aborts any transition superseded by a newer click before the iframe finishes loading.

---

## TAKE / CUT / CLEAN API

| Action | Endpoint | Body | Effect |
|---|---|---|---|
| TAKE | `POST /broadcast/take` | `{"scene": key}` | Clears same-group peers; sets scene flag; updates `program.groups[group]` |
| CUT (group) | `POST /broadcast/clean_group` | `{"group": "A"\|"B"\|"C"}` | Clears flags for one group only; other groups untouched |
| CLN (all) | `POST /broadcast/clean` | `{}` | Clears all director flags; resets `program.groups` to `{}` |

All three require `session.get("comp_ok")` or return 401. All call `_sse_notify()` after saving state.

The director applies **optimistic local updates** before the SSE round-trip arrives, so the UI reflects the change immediately.

---

## Ctrl Panel Actions

When an overlay is in PVW, the ctrl panel renders overlay-specific controls:

| Overlay | Controls | API call |
|---|---|---|
| leaderboard | Category select, row count, freeze | `POST /director/lb` (category); `POST /update` (count, frozen) |
| lineup | Category buttons, duration, auto toggle | `POST /update` (`lineupIndex`, `lineupDuration`, `lineupAuto`) |
| h2h | Left/right athlete name inputs | `POST /update` (`h2hLeft`, `h2hRight`) |
| lowerthird, champion, reps | (none) | — |

After any `/update` call succeeds, director calls `_pushCurrentPVW()` to reflect the change in the PVW iframe immediately without waiting for the SSE echo.

---

## Keyboard Shortcuts (director.html)

| Key | Action |
|---|---|
| `Enter` or `Space` | TAKE (same as clicking TAKE button) |
| `Escape` | CUT/CLEAN (cleans current PVW selection's group) |
| `D` | Toggle scale debug HUD (shows iframe transforms, pvwBody dimensions) |
