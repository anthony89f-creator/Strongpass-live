# Phase 2: SSE Migration — Summary

## What changed

### New file
- `sse-client.js` — drop-in replacement for `ws-client.js`. Same contract (`window.onStateUpdate(data)` on every push). Uses `EventSource(/stream)` with exponential backoff reconnect instead of `setInterval(fetch, 500)`.

### Modified files (14)

| File | Change |
|---|---|
| `champion.html` | ws-client.js → sse-client.js; removed duplicate initial fetch |
| `scorebug.html` | ws-client.js → sse-client.js; removed duplicate initial fetch |
| `lowerthird.html` | ws-client.js → sse-client.js; removed duplicate initial fetch |
| `lineup.html` | ws-client.js → sse-client.js; removed duplicate initial fetch |
| `leaderboard.html` | ws-client.js → sse-client.js; removed duplicate initial fetch |
| `reps.html` | ws-client.js → sse-client.js; removed duplicate initial fetch |
| `h2h.html` | ws-client.js → sse-client.js; removed duplicate initial fetch |
| `manual_lowerthird.html` | ws-client.js → sse-client.js; removed duplicate initial fetch |
| `results.html` | Added sse-client.js + onStateUpdate handler; removed setInterval(15s) |
| `home.html` | Replaced setInterval(5s fetch) with SSE open/error for status dot |
| `control.html` | Replaced setInterval(checkConnection, 3s) with SSE open/error |
| `templates/comp_callroom.html` | Replaced setInterval(reload, 5s) with smart SSE reload on event/heat change |
| `templates/comp_arena.html` | Replaced setTimeout(reload, 5s) with smart SSE reload on event/heat change |

### NOT changed (intentional)
- `judge.html` — already uses SSE correctly (with 2s fallback poll when SSE is down)
- `judge-master.html` — same
- `templates/comp_results_public.html` — already uses EventSource
- `templates/comp_heats.html` — kept 10s reload; no clean state signal to watch without frequent false-triggers from judge timer ticks
- `debug.html` — debug tool; polling is appropriate
- `control.html` `syncFromCompEngine(5s)` — kept; control panel sync is intentional and functional

## Request reduction benchmark

### Before (all clients open, competition running)

| Source | Interval | Req/min |
|---|---|---|
| 8 × ws-client.js overlays | 500ms | **960** |
| control.html checkConnection | 3000ms | 20 |
| control.html syncFromCompEngine | 5000ms | 12 |
| home.html status poll | 5000ms | 12 |
| results.html load() | 15000ms | 4 |
| **Total** | | **~1,008 req/min** |

### After

| Source | Interval | Req/min |
|---|---|---|
| 8 × sse-client.js overlays | SSE push only | 0 (+ 8 persistent connections) |
| control.html SSE status | SSE open/error | 0 |
| control.html syncFromCompEngine | 5000ms | 12 |
| home.html SSE status | SSE open/error | 0 |
| results.html initial load + onStateUpdate | SSE push only | 0 |
| **Total** | | **~12 req/min** |

**HTTP polling reduction: 1,008 → 12 req/min (98.8% reduction)**

Note: SSE connections are long-lived HTTP requests that stay open. Each client adds 1 persistent connection rather than generating requests per interval.

## Architecture summary

### Before
```
ws-client.js (per overlay)
  └── setInterval(fetch /state.json, 500ms)
      └── if different → onStateUpdate(data)   ← diff check in client
```

### After
```
sse-client.js (per overlay, shared pattern)
  ├── fetch /state.json (once, on load)        ← immediate first render
  └── EventSource /stream                      ← server pushes on change
      ├── onopen: reset retry backoff
      ├── onmessage: onStateUpdate(data)        ← server decides when to push
      └── onerror: exponential backoff (1s→30s) + reconnect
```

### SSE stream (`/stream` endpoint)
- Uses `threading.Condition` + `_sse_version` counter
- Sends full state JSON only when state changes (`_sse_notify()` called)
- Sends `: keepalive` comment every 30s to prevent proxy timeouts
- `seen_version = -1` at connect → first push fires immediately
- Headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`, `Access-Control-Allow-Origin: *`

## Reconnect behaviour

| Scenario | Behaviour |
|---|---|
| OBS browser source reloads | EventSource reconnects → immediate state push → overlay renders |
| Server restarts | SSE onerror fires → 1s wait → reconnect (doubles on failure, max 30s) |
| Network blip | Same as server restart |
| No state changes for 30s | Server sends keepalive comment; connection stays open |
| comp_callroom/arena disconnects | Falls back to 30s page reload until SSE reconnects |

## UI flickering assessment

**None introduced.** The `onStateUpdate` contract is unchanged — overlays still receive the full state object and call their existing `render()` / `update()` functions. The only behavioral difference is:
- **Before**: `render()` called every 500ms regardless of state change
- **After**: `render()` called only when state actually changes

This is strictly better: no extra render calls means no flicker from redundant updates.

## Rollback

```bash
git revert 88c48f3 23e1bc6 f624a8e 4fa344d  # reverts all Phase 2 commits
# OR: 
git checkout 1f69015 -- champion.html scorebug.html lowerthird.html lineup.html \
    leaderboard.html reps.html h2h.html manual_lowerthird.html results.html \
    home.html control.html templates/comp_callroom.html templates/comp_arena.html
git rm sse-client.js
git commit -m "Revert Phase 2 SSE migration"
```

Pre-Phase-2 checkpoint: commit `1f69015` (Phase 1 — Gunicorn init fix).

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| SSE connection count under Gunicorn | Low | Gunicorn is configured `workers=1, threads=8`; each SSE client holds one thread. 8 overlays = 8 threads, well within the 8-thread limit. |
| Nginx proxy timeout on SSE | Low | `X-Accel-Buffering: no` prevents nginx from buffering SSE; keepalive every 30s prevents idle timeout if nginx `proxy_read_timeout ≥ 30s` |
| comp_callroom/arena false reload | Very low | Only reloads when `compEvent` or `compHeat` changes; judge timer ticks update `judgeL*` fields which are not checked |
| Browser source EventSource support | None | OBS uses Chromium 90+, full EventSource support |
| State delivery on first connect | None | `_sse_version = 0 != seen_version = -1` ensures first SSE message fires immediately; plus sse-client.js retains initial fetch for belt-and-suspenders |
