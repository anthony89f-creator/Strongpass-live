# Handover — Phase 1 Stability (SSE Pipeline Fixes)
**Date:** 2026-05-13  
**Commits:** `5a1ff30` (code), `3b3edba` (docs)  
**Live verification:** Passed against `strongpass.live`

---

## What This Session Addressed

After the Phase 5 deploy (weight+reps scoring, mobile redesign, leaderboard architecture), UI
glitches were observed during live competition. The root cause was traced to the SSE pipeline:
`sync_comp_to_broadcast()` called `_sse_notify()` unconditionally on every `/update` — including
every 1-second timer tick — causing comp pages to reload or re-render continuously even when no
scoring data had changed.

Six discrete bugs were identified and fixed.

---

## Bug Status Table

| # | Bug | Status | Files / Functions |
|---|-----|--------|-------------------|
| 1 | Timer ticks trigger full SSE payload rebuilds | ✅ Fixed | `app/sse.py` — added `_results_version = 0`; `server.py:_invalidate_results_cache()` — increments `_results_version`; `server.py:generate()` — `_sse_version` wakes thread, `_results_version` separates score changes from state changes |
| 2 | SSE payload included 136 KB results blob on every event | ✅ Fixed | `server.py:generate()` — `results` key removed from stream; `server.py:/state.json` — blob retained here, `results_version` added so consumers only fetch when needed |
| 3 | `comp_heats.html` reloaded on every timer tick | ✅ Fixed | `templates/comp_heats.html:connect()` — added `results_version` gating; `location.reload()` only fires when version advances |
| 4 | `comp_leaderboard.html` re-rendered on every tick; stale race condition on out-of-order fetch | ✅ Fixed | `templates/comp_leaderboard.html:connect()` — `results_version` gating before fetch; `renderTable()` — `===` → `<=` comparison |
| 5 | `comp_results_public.html` cold-start: `querySelector('.results-table')` returned null | ✅ Fixed | `templates/comp_results_public.html` — `.results-table` div moved outside `{% if rows %}`; `id="results-empty"` placeholder removed by JS on first render; `updateResults()` — `===` → `<=` |
| 6 | `weight_reps` tiebreak (`secondaryValue=reps`) not written to DB; timer guard missing | ✅ Fixed (tiebreak + guard) / ⏳ Time-event pause path needs live test | `server.py:_apply_judge_scores_to_raw_results()` — added `elif secondary_metric and secondary_metric != "time":` branch; added `not jd.get("timerRunning")` guard before writing elapsed time to DB |

---

## Live Verification Results

Script: `scripts/verify_live_phase1.py`  
Run against: `https://strongpass.live`  
Date: 2026-05-13

| Check | Result | Detail |
|-------|--------|--------|
| `results_version` present in `/state.json` | ✅ | Value = 0 at test start |
| SSE `/stream` payload size | ✅ | 9.8 KB (was 136 KB — 93% reduction) |
| 3 × timer ticks do not advance `results_version` | ✅ | Version held at 0 after ticks |
| Score submit (weight + `secondaryValue`) advances `results_version` | ✅ | 0 → 1 on submit |
| Timer-pause path writes elapsed time (time events) | ⏳ Skipped | Active event was `weight` type; requires time-metric event active to verify end-to-end |

### Re-running verification

```bash
python3 scripts/verify_live_phase1.py https://strongpass.live <BETA_TOKEN> <COMP_PASSWORD>
```

Check 4b (timer-pause) will auto-run when the active comp event has `eventMetric = time`.

---

## Architectural Changes (permanent)

### `_results_version` counter
- Lives in `app/sse.py` alongside `_sse_version`
- `_sse_version` still increments on every notify (used to wake SSE threads)
- `_results_version` only increments inside `_invalidate_results_cache()`, which is called when scores, events, athletes, or heats change — never on timer ticks
- Exposed in `/state.json`, SSE stream payload, and both API endpoints (`/comp/api/leaderboard`, `/api/results/<cat>`)

### SSE stream payload split
- **Stream (`/stream`):** ~10 KB — state, events list, `results_version`. No results blob.
- **State file (`/state.json`):** ~135 KB — full state + results blob + `results_version`. Fetched by consumers only when `results_version` advances.

### Frontend version gating pattern (all comp pages)
```javascript
// Shared pattern in comp_leaderboard.html, comp_results_public.html, comp_heats.html
es.onmessage = function(evt) {
  var msg = JSON.parse(evt.data);
  if (msg.results_version !== undefined && msg.results_version <= lastVersion) return;
  // fetch or reload
};
```
Version comparison is `<=` (not `===`) to handle out-of-order async fetch responses.

---

## Known Issues Updated This Session

`docs/known_issues.md` — H3-sub added as resolved:

> **~~H3-sub~~ — Timer ticks triggered full SSE payload rebuilds**  
> Resolved in `5a1ff30`. `_results_version` counter now separates score changes from
> timer/state changes. SSE payload stripped from 136 KB → 10 KB. Timer ticks no longer
> invalidate the results cache or trigger frontend re-renders.

---

## What Is Next

Immediate unresolved item from this session:

- **Bug 6 time-event pause path** — verify end-to-end that pausing a lane on a `time`-metric event writes `raw_value = timerSecs - timerRemaining` to `raw_results` DB and `results_version` advances. Run `verify_live_phase1.py` when event 2 (Frame Carry / any time metric) is active.

Next in the technical debt backlog (from `docs/roadmap.md`):

| Priority | Item | Blocker |
|----------|------|---------|
| High | CSRF protection on all forms (H1) | Nothing |
| Medium | Request-scoped DB connection via Flask `g` (H4) | Nothing |
| Medium | Fix bare `except: pass` in migrations (M2) | Nothing |
| Medium | `set_lanes` swallows `regenerate_remaining_heats` error (H3) | Nothing |
| Medium | `/health` endpoint (M6) | Nothing |
| Medium | `api_event_points` missing input validation — 500 on bad `event_id` (M7) | Nothing |

Next product priorities (from `docs/roadmap.md`): P2 Security Audit → P4 Organiser/Member Architecture Planning → P5 Stripe Planning.
