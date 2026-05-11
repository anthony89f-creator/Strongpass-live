# Phase 4: Performance Analysis — Leaderboard & Realtime

## Executive summary

The dominant bottleneck is `get_public_results()` called independently by every SSE client thread on every state change. With 8 overlays connected during a live heat, each judge timer tick triggers **8 independent full leaderboard recalculations** and **8 JSON serialisations** of an identical payload. The entire SSE stream bottleneck is eliminated by a shared in-memory cache keyed on results version.

---

## 1. Hot-path trace — single `/update` POST (judge timer tick, engine mode)

```
update_state() handler
  load_state()                          ← file read 1 (under state_lock)
  save_state()                          ← file write 1 (judgeL* overlay state)
  _apply_judge_scores_to_raw_results()
    get_broadcast_mode()
      load_state()                      ← file read 2 (duplicate config read)
    get_lane_count() → get_comp_config()
      load_state()                      ← file read 3 (duplicate config read)
    get_comp_state()                    ← db open + 1 query
    get_heat_lanes()                    ← db open + 1 query
    event row query                     ← 1 query (same connection)
    for each lane (×4):
      db() → SELECT athlete_id          ← db open + 1 query
      INSERT/UPDATE raw_results         ← 1 write (same value repeated every tick!)
      db.close()
    sync_comp_to_broadcast()
      get_broadcast_mode()
        load_state()                    ← file read 4 (duplicate of read 2)
      get_lane_count() → get_comp_config()
        load_state()                    ← file read 5 (duplicate of read 3)
      get_comp_state()                  ← db open + 1 query
      get_heat_lanes()                  ← db open + 1 query
      db() → events, athletes, counts   ← db open + ~5 queries
      get_raw_results_batch()           ← db open + 1 query
      get_leaderboard(category=cat)     ← db open + (N_events × ~2 queries)
        get_category_start_count(cat)
          load_state()                  ← file read 6
      load_state() under state_lock     ← file read 7
      save_state()                      ← file write 2 (broadcast block)
      _sse_notify()                     ← wakes 8 SSE threads

8 SSE threads wake simultaneously:
  each thread:
    load_state()                        ← file read × 8
    get_public_results()                ← repeated 8× independently
      get_public_events()               ← db open + 1 query
      for each category (×6):
        get_leaderboard_detailed(cat)
          db() → SELECT events           ← 1 query
          db() → SELECT athletes         ← 1 query
          get_category_start_count(cat)
            load_state()               ← file read (×6 per thread)
          for each event (×5):
            _compute_event_points_core()  ← 2 queries
    json.dumps(data)                   ← serialise × 8

Totals per judge tick:
  File reads:   7 (handler chain) + 8 (SSE threads) + 48 (get_category_start_count in SSE) = ~63
  File writes:  2
  DB queries:   ~15 (handler chain) + 8 × (1 + 6 × (2 + 5×2)) = ~15 + 8×67 = ~551
  json.dumps:   8
```

**Effective throughput with 1Hz judge updates and 8 SSE clients: ~551 DB queries/second.**

---

## 2. Duplicate computation table

| Computation | Per-tick count | Root cause |
|---|---|---|
| `get_public_results()` (all 6 cats, 5 events) | **8×** | Each SSE thread computes independently |
| `json.dumps(full_state)` | **8×** | Each SSE thread serialises independently |
| `load_state()` for broadcast mode check | **2×** | `_apply_judge_scores_to_raw_results()` + `sync_comp_to_broadcast()` both call `get_broadcast_mode()` |
| `load_state()` for lane count | **2×** | Same pair call `get_lane_count()` independently |
| `raw_results` UPSERT with same value | **4×/tick** | No change-detection; writes same value on every timer tick for reps events |
| `get_category_start_count` load_state | **up to 6×/tick** | One `load_state()` per category inside SSE `get_public_results()` |

---

## 3. Safe cache boundaries

### `get_public_results()` result cache

**What it contains:** `(events_list, results_by_cat)` — full per-category, per-event leaderboard breakdown.

**Safe to cache because:** this data changes only when `raw_results`, `athletes`, or `events` tables change. These are discrete admin actions (result entry, athlete add/withdraw, event add/delete). Timer ticks do NOT change the leaderboard unless a rep count or score changes.

**Invalidated by:**

| Action | Routes |
|---|---|
| Raw result written (judge or admin) | `_apply_judge_scores_to_raw_results`, `action_save_results`, `action_save_results_run`, `action_set_result_type`, `judge_set_status`, `action_simulate_results`, import routes |
| Athlete added / withdrawn / reinstated | `action_add_athlete`, `action_withdraw_athlete`, `action_reinstate_athlete` |
| Event structure changed | `action_add_event`, `action_delete_event`, `action_update_event_scoring`, `action_update_event_type`, `action_edit_event` |
| All data cleared | `action_clear_all`, `action_generate_test` |

**NOT invalidated by:**

| Action | Reason |
|---|---|
| `next_heat` / `prev_heat` / `set_heat` | Heat navigation doesn't change scores |
| `next_event` | Advances pointer; no raw_results change |
| `jump_to` | Navigation only |
| `set_lanes` | Config change, not data change |
| Judge timer ticks where score didn't change | Change detection in `_apply_judge_scores_to_raw_results` |

### In-memory competition config (lane count, broadcast mode)

**Safe to cache because:** only changes when the user explicitly hits `/comp/action/set_lanes` or `/comp/action/set_broadcast_mode`. Reads from these routes happen at ≪1 Hz.

**Not implemented in this phase** — file read cost is low compared to DB computation cost. Deferred.

---

## 4. Invalidation strategy

### Two-level cache

```
_results_lock          threading.Lock — serialises all cache reads/writes
_results_cache         tuple (events_list, results_by_cat) or None
_results_dirty         bool — True means cache must be recomputed before next use
```

**Write path:**
```
competition data changes
  → _invalidate_results_cache()       sets _results_dirty = True
  → sync_comp_to_broadcast()
  → _sse_notify()
```

**Read path (SSE stream, /state.json):**
```
_sse_notify() fires
  → acquire _results_lock
  → if _results_dirty:
      recompute get_public_results()  ← ONE thread only
      _results_dirty = False
      store in _results_cache
  → release lock
  → all threads read from _results_cache
```

### Change detection in `_apply_judge_scores_to_raw_results`

Before each UPSERT, SELECT the existing value. If identical (within ±0.001), skip the write and do NOT call `_invalidate_results_cache()`. This prevents timer ticks from dirtying the cache when the athlete's score hasn't changed.

```
Timer tick, reps event, athlete has 25 reps (no change):
  → SELECT existing: raw_value=25.0 → matches → skip UPSERT
  → _invalidate_results_cache() NOT called
  → sync_comp_to_broadcast() runs (overlay state still updated)
  → _sse_notify() fires
  → SSE threads wake → _results_dirty=False → cache hit → 0 DB leaderboard queries

Timer tick, athlete taps +1 (reps 25→26):
  → SELECT existing: raw_value=25.0 → differs → UPSERT raw_value=26.0
  → _invalidate_results_cache() called → _results_dirty=True
  → sync_comp_to_broadcast() runs
  → _sse_notify() fires
  → SSE threads wake → _results_dirty=True → ONE thread recomputes → cache populated
  → all threads use cached result
```

---

## 5. Expected improvements

| Metric | Before | After | Method |
|---|---|---|---|
| `get_public_results()` calls per SSE event | 8 | 1 | Shared cache (all SSE threads) |
| `get_public_results()` calls per timer tick (reps, no score change) | 8 | **0** | Change detection + cache dirty flag |
| DB queries per timer tick (reps, no score change, 4 lanes) | ~551 | ~20 | Cache hit: only sync_comp_to_broadcast queries |
| DB queries per score-change tick | ~551 | ~85 | 1× cache recompute instead of 8× |
| `json.dumps` per SSE event | 8 | 8 | Unchanged (state.json slice differs per client) |
| `load_state()` calls per `/update` | ~63 | ~15 | Cache hit eliminates get_category_start_count × 8 × 6 |

**Dominant-case improvement (reps event, timer running, score unchanged):**
551 → ~20 DB queries per tick = **96% reduction**

**Score-change case:**
551 → ~85 DB queries per tick = **85% reduction**

---

## 6. What is NOT changed

- Scoring logic — `_compute_event_points_core` unchanged
- Leaderboard ordering — identical: cache stores exactly the same data `get_public_results()` returned
- SSE compatibility — clients receive identical payload; `events` and `results` keys still present
- All routes — no interface changes
- `/state.json` — same response structure; uses same shared cache
- `sync_comp_to_broadcast()` — leaderboard in broadcast block still freshly computed for current category on each call

---

## 7. Rollback

All changes are in `server.py`. To revert:
```bash
git revert HEAD        # reverts optimisation commit
# OR restore specific functions:
git checkout HEAD~1 -- server.py
```

Pre-Phase-4 checkpoint: commit `4a1a04a` (Phase 3 extraction).
