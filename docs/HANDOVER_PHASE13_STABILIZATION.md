# Handover — Phase 13: Broadcast Stabilization
**Date:** 2026-05-16  
**Status:** Production-stable. Live at strongpass.live.  
**Branch:** main  
**Key commits:** `f91e471` (categories fix), `b715dc3` (comp_run reconnect), `e135818` (h2h fix + logging), `3e61f21` (scorebug removal + leaderboard fix)

---

## START HERE NEXT SESSION

### Current stable architecture

The system is a single-worker Flask/Gunicorn app (32 gthreads) serving:
- **Competition engine** — Flask routes writing to SQLite, triggering `sync_comp_to_broadcast()`
- **Broadcast state** — `state.json` on disk, atomic write via tmp+rename, guarded by `state_lock`
- **SSE stream** — `/stream` endpoint, all overlay consumers use `sse-client.js`
- **Overlays** — static HTML files read `state.json` on load, then live-update via SSE

The **most important invariant**: every overlay reads from `state.json` via SSE. Nothing is computed client-side. State mutations flow: competition action → `sync_comp_to_broadcast()` → `state.json` → `_sse_notify()` → overlays.

### Files most important to understand

| File | Why it matters |
|------|---------------|
| `server.py` | Single source of truth. `sync_comp_to_broadcast()` (line ~875) is the canonical state rebuild. `_UPDATE_EXCLUDE` (line ~1277) prevents frontend from clobbering server-computed fields. `/update` endpoint (line ~1260) is how control.html writes state. |
| `state.json` | Runtime state. Boolean overlay flags (`leaderboard`, `h2h`, `lineup`, etc.) live here alongside broadcast data. Never edit directly — use the API. |
| `sse-client.js` | Shared by all OBS overlays. Handles auth, reconnect, bfcache restore. Do not change without understanding the `pagehide`/`pageshow`/`visibilitychange` handlers. |
| `control.html` | Broadcast director UI. `pushUpdate()` serializes all frontend state to `/update`. `syncFromCompEngine()` reads `/state.json` every 30s and on every SSE event. Both must stay in sync. |
| `leaderboard.html` | Most complex overlay. Reads `lbAthletes || athletes`, `lbFrozen`, `lbCategoryOverride`, `lbDisplayCount`. Uses hash guard `_lastLbKey` to avoid animation flicker. |
| `h2h.html` | Reads `h2h` (bool), `h2hLeft`, `h2hRight` (objects). Google Drive URLs converted via `driveDirectUrl()` to `thumbnail?id=X&sz=w1920` format. |
| `templates/comp_run.html` | Inline SSE (not sse-client.js). Reloads on `compEvent`/`compHeat` change. Has its own `pageshow`/`visibilitychange` reconnect handlers (added Phase 13). |

### What MUST NOT be changed casually

1. **`sync_comp_to_broadcast()` patch keys** — `broadcast_block` keys are carefully scoped to avoid clobbering overlay visibility flags. Adding a key that collides with a frontend flag (e.g. `"leaderboard"`) will silently break toggles for that overlay.

2. **`_UPDATE_EXCLUDE`** — These keys are server-computed and must never be overwritten by frontend payloads: `categories`, `events`, `results`, `lbStandings`, `results_version`, `restart_token`, `scorebug`. Adding entries is safe; removing them risks state corruption.

3. **`sse-client.js` pagehide handler** — Closing the EventSource on `pagehide` and reconnecting on `pageshow` (e.persisted) and `visibilitychange` is load-bearing for OBS scene switching. The asymmetry (`pagehide` always closes; `pageshow` only reconnects if bfcache) is intentional.

4. **`_sse_notify()` call in `/update`** — Added Phase 13. Ensures overlay visibility toggles from control.html reach OBS overlays immediately, regardless of competition state. Removing it reintroduces the leaderboard toggle lag.

5. **Gunicorn thread count (32)** — Set in `/etc/systemd/system/strongpass.service`. SSE connections hold one gthread each. With 10+ OBS browser sources open, you need headroom. Do not reduce without measuring.

6. **Google Drive image format** — Use `thumbnail?id=FILE_ID&sz=w1920`, NOT `uc?export=view`. The `uc` format triggers a consent page redirect that OBS cannot follow.

### Recommended next development priorities

1. **Broadcast control UI redesign** — `control.html` is a monolith (1700+ lines). The biggest UX debt. Consider a preview/program (PGM/PVW) workflow where the director stages changes before going live.

2. **Overlay scene switching** — Currently the director must toggle each overlay manually. A "scene" abstraction (preset combinations of overlay states) would reduce live errors.

3. **Scorebug rebuild** (optional, isolated) — If needed, rebuild as a standalone module with its own SSE subscription and no shared state with other overlays. Do not reuse the old `scorebug.html`.

4. **Production UX hardening** — Control panel needs clearer live/off state indicators, undo capability, and per-overlay OBS status (is OBS connected?).

---

## 1. Current Stable Status

### Confirmed working in production

| System | Status | Notes |
|--------|--------|-------|
| Competition engine navigation | ✅ Stable | Heat/event advance, score save, category switching |
| SSE live sync | ✅ Stable | All overlays receive state within ~100ms of any competition action |
| OBS overlay delivery | ✅ Stable | All overlays return 200, auth via `?token=` session cookie |
| Leaderboard overlay | ✅ Fixed | Toggle now responds immediately (Phase 13 `_sse_notify()` fix) |
| Lineup overlay | ✅ Stable | Category cycling, auto-advance, color-coded |
| Head-to-Head overlay | ✅ Fixed | h2h athlete data now persists across control.html reloads |
| Google Drive athlete images | ✅ Fixed | `thumbnail?id=X&sz=w1920` format, CSS `url("...")` quoting |
| Call room live updates | ✅ Stable | SSE DOM patching, cross-category preview for last heat |
| Judge-to-run sync | ✅ Stable | Judge scores write to DB, trigger `sync_comp_to_broadcast()` |
| Cross-tab reconnect | ✅ Fixed | `pageshow`/`visibilitychange` handlers on all overlays and comp_run |
| Category synchronization | ✅ Fixed | `_UPDATE_EXCLUDE` prevents frontend from clobbering `categories` array |
| Director leaderboard override | ✅ Stable | `/director/lb`, `lbCategoryOverride`, `lbFrozen`, `lbDisplayCount` |

### Overlay inventory

| Overlay file | Visible flag | Uses sse-client.js |
|-------------|--------------|-------------------|
| `leaderboard.html` | `state.leaderboard` (bool) | ✅ |
| `lineup.html` | `state.lineup` (bool) | ✅ |
| `h2h.html` | `state.h2h` (bool) | ✅ |
| `reps.html` | `state.repsVisible` + `state.lightsVisible` | ✅ |
| `lowerthird.html` | `state.lowerThirdVisible` | ✅ |
| `champion.html` | `state.champion` | ✅ |
| ~~`scorebug.html`~~ | ~~`state.scorebug`~~ | Removed from broadcast system |

---

## 2. Removed / Deprecated

### Scorebug overlay

**Status:** Fully removed from live broadcast system as of Phase 13.

**What was removed:**
- `control.html`: toggle row, "Score Only" preset button, OBS URL entry, `scorebug:false` from state init, all references in `hideAll()` / `showAll()` / `initInputs()`
- `server.py`: `"scorebug"` added to `_UPDATE_EXCLUDE` — any payload containing `scorebug` is now silently dropped

**What remains:**
- `scorebug.html` file still exists on disk (not deleted) but is not reachable from control.html and not in any OBS URL list
- `state.json` may still have a stale `scorebug: false` key — harmless, ignored by all active overlays

**If rebuilding later:** Treat as a new isolated module. Give it its own state flag with a new name if needed. Do not resurrect `scorebug.html` as-is — it had unresolved OBS rendering issues and its diagnosis was inconclusive.

---

## 3. Key Architectural Fixes (Phases 11–13)

### Phase 11 fixes
- **Cross-category call room preview**: when `heat >= max_heat`, `api_callroom` queries heat 1 of the next `CATEGORY_ORDER` category and includes a `next_category` field so the call room UI shows the upcoming group
- **SSE reconnect on bfcache restore**: `sse-client.js` now handles `pageshow` (e.persisted) and `visibilitychange` to reconnect and re-fetch state after OBS scene switches
- **H2H Google Drive images**: `driveDirectUrl()` converts `/file/d/ID/view` to `thumbnail?id=ID&sz=w1920`

### Phase 12 fixes
- **Zero-downtime deploys**: Added `ExecReload=/bin/kill -s HUP $MAINPID` to systemd service — use `systemctl reload strongpass` not `systemctl restart`. Eliminates the 2–3s gap where OBS hits a 502.
- **sse-client.js double-fetch on load**: Fixed spurious `pageshow` reconnect — added `e.persisted` guard so only bfcache restores trigger the reconnect, not every normal page load

### Phase 13 fixes
- **`categories: []` clobbering** — Root cause: `control.html`'s `pushUpdate()` sends `JSON.stringify(state)` with `categories: []` (the frontend's API-mode array), which `/update` wrote directly to state.json, wiping `sync_comp_to_broadcast()`'s DB-sourced category data. Fix: `"categories"` added to `_UPDATE_EXCLUDE`.
- **comp_run.html dead after tab switch** — `pagehide` closed SSE with no `pageshow` reconnect. Added both reconnect handlers and a `/state.json` fetch on return to catch missed heat advances.
- **h2h athlete data wipe** — `control.html`'s `syncFromCompEngine()` never synced `live.h2hLeft`/`live.h2hRight`, so on every page reload the h2h inputs were blank. Any `pushUpdate()` then sent `{name:''}` via `syncH2HInputs()`, wiping state.json. Fix: `syncFromCompEngine()` now restores h2h inputs from server when they are blank.
- **Leaderboard toggle lag** — `/update` endpoint relied on `_apply_judge_scores_to_raw_results()` to fire `_sse_notify()`, but that function returns early (no SSE) if competition state/heat/event is missing from DB. Overlay toggle writes to state.json with no immediate notification. Fix: added explicit `_sse_notify()` call after `save_state()` in `/update`.
- **Scorebug removal** — See section 2.

---

## 4. Architectural Rules Going Forward

### Hard rules — do not break

**SSE is the only update path for overlays.** Overlays must not poll. They must not reload the page on state changes. They receive SSE events via `sse-client.js` → `window.onStateUpdate()` → `render()`. Any deviation from this pattern reintroduces the flicker, reload, and timing bugs that took three phases to fix.

**Overlay state is isolated per overlay.** Each overlay reads only the state fields it needs. No overlay should read fields that belong to another overlay's lifecycle. No shared render queues. No cross-overlay visibility dependencies.

**`sync_comp_to_broadcast()` is the only writer of engine-computed state.** Competition engine data (athletes, lanes, categories, standings) flows through this function only. It must not write overlay visibility flags. Overlay flags are owned by the broadcast director (control.html → `/update`).

**`_UPDATE_EXCLUDE` is a safety net, not a permission system.** Fields in this set are server-computed and must never come from frontend payloads. When adding new server-computed fields, add them to this set immediately.

**No `location.reload()` in overlay or admin pages during normal operation.** The only acceptable reload is in `comp_run.html` on `compEvent`/`compHeat` change — a deliberate navigation to a new heat, not a state refresh.

### Strong preferences

- New overlay visibility flags: boolean, top-level in state.json, toggled only by control.html
- New overlays: copy the pattern of `lineup.html` or `leaderboard.html` — isolated state object, hash guard, single `onStateUpdate` handler
- New control.html state fields: if they come from the server (not from user input), add them to `syncFromCompEngine()` and to `initInputs()`
- Deploy with `systemctl reload strongpass` (graceful), never `systemctl restart` (causes ~3s downtime gap that OBS will 502 and cache)

---

## 5. Remaining Future Work

### Broadcast director
- **Preview/program workflow** — stage overlay changes before going live; requires a `preview_state` alongside `live_state`
- **Overlay scene presets** — saved combinations of visibility flags (e.g. "Leaderboard + Event Bar", "H2H", "Clean")
- **Per-overlay OBS connection status** — know which sources are active in OBS without manual checking

### Control panel
- **Redesign `control.html`** — 1700+ line monolith; split into tabbed sections with cleaner state management
- **Undo / safe staging** — prevent accidental `hideAll()` during live broadcast

### Overlays
- **Scorebug rebuild** (optional) — isolated module if bottom-ticker is needed; fresh design, no shared state
- **Lower third improvements** — currently shows current heat lanes; could show custom name/event overlays

### Infrastructure
- **CSRF protection on `/update` and all POST routes** — currently unprotected (relying on beta auth)
- **Structured logging** — Gunicorn logs are stdout only; add request IDs and action timestamps

---

## 6. Failure Mode Reference

These are the bugs that caused real production failures. Memorize the patterns.

### Pattern 1: Frontend state clobbering server state
**Symptom:** A field that `sync_comp_to_broadcast()` computes (categories, athletes, standings) suddenly shows stale or empty data after any control.html interaction.  
**Cause:** `pushUpdate()` sends `JSON.stringify(state)` — the entire frontend state, including fields the frontend doesn't own. `/update` merges all keys.  
**Defense:** `_UPDATE_EXCLUDE`. When adding new server-computed fields, add them here immediately.

### Pattern 2: SSE fires but overlay doesn't update
**Symptom:** Toggling a visibility flag in control.html does nothing. Overlay eventually updates minutes later, or after a judge submits a score.  
**Cause:** `_sse_notify()` not being called. Pre-Phase 13, `/update` relied on `_apply_judge_scores_to_raw_results()` which can return early if competition state is incomplete.  
**Defense:** `_sse_notify()` is now called unconditionally in `/update` after `save_state()`.

### Pattern 3: Overlay goes dead after OBS scene switch
**Symptom:** OBS operator switches away from a scene and back; overlay no longer updates even though SSE is running.  
**Cause:** `pagehide` closes the EventSource; no `pageshow`/`visibilitychange` handler reconnects it.  
**Defense:** All overlays use `sse-client.js` which handles this. `comp_run.html` has its own inline handlers. Any new inline SSE code must replicate this pattern.

### Pattern 4: Control.html reload wipes overlay data
**Symptom:** h2h (or similar user-entered) overlay shows blank after an operator reloads control.html.  
**Cause:** `syncFromCompEngine()` didn't restore user-entered fields (h2hLeft/h2hRight) from server state. On reload, inputs are blank. Next `pushUpdate()` sends `{name:''}` and wipes state.json.  
**Defense:** `syncFromCompEngine()` now restores h2h inputs when they are blank. Any new user-entered overlay data field must follow this pattern: restore from server in `syncFromCompEngine()`, only when input is currently empty.

### Pattern 5: 502 cached by OBS
**Symptom:** OBS browser source shows 502 even after the server is back up.  
**Cause:** OBS caches error responses and does not auto-retry. A `systemctl restart` (not `reload`) during a live OBS session causes a ~3s window where requests 502.  
**Defense:** Always use `systemctl reload strongpass`. If a 502 does get cached, right-click the OBS browser source → Refresh.

---

## Deployment Checklist

Before going live at an event:

```bash
# 1. Verify service
ssh root@strongpass.live "systemctl is-active strongpass"

# 2. Health check
curl -s 'https://strongpass.live/health?token=TOKEN'

# 3. Verify SSE active clients
# (check sse_active in health response — should be 0 before event, N during)

# 4. Check state.json overlay flags are all false before event
ssh root@strongpass.live "python3 -c \"import json; s=json.load(open('/opt/strongpass/current/state.json')); [print(k,s.get(k)) for k in ['leaderboard','h2h','lineup','lowerThirdVisible','champion','repsVisible']]\""

# 5. Deploy latest
rsync -av /Users/tonyfarrell/Desktop/files/ root@strongpass.live:/opt/strongpass/current/ \
  --exclude='*.pyc' --exclude='__pycache__' --exclude='comp.db' --exclude='state.json'
ssh root@strongpass.live "systemctl reload strongpass"
```
