# Competition Engine — Stress Test & Recommendations

This document summarizes stress-test findings and concrete recommendations for the competition engine (Flask server, broadcast, judge panels, comp control).

---

## 1. What Was Stress-Tested / Reviewed

- **Code paths**: Heat generation, scoring/leaderboard, broadcast sync, judge flow, competition actions.
- **Concurrency**: State read/write patterns, use of `state_lock`, risk of torn `state.json`.
- **Error handling**: Form parsing, empty categories, missing DB rows, corrupt state.
- **Database**: Schema, N+1 queries, connection usage.

---

## 2. Fixes Already Applied

| Area | Change |
|------|--------|
| **Form parsing** | `action_set_heat` and `action_jump_to` now use `_safe_int()` so non-numeric `event`/`heat` no longer raise 500s. Values are clamped to sensible ranges (event 1–999, heat 1–9999). |
| **State load** | `load_state()` now catches `FileNotFoundError`, `json.JSONDecodeError`, and `OSError` and returns `{}` instead of crashing when `state.json` is missing or corrupt. |
| **/update DoS** | `POST /update` rejects bodies larger than 512 KB (413) to limit abuse from huge JSON payloads. |

---

## 3. Recommendations (Priority Order)

### High priority

1. **Harden `regenerate_remaining_heats` in `set_lanes`**  
   Currently exceptions are swallowed (`try/except: pass`). At minimum log the exception; ideally validate DB/state before calling (e.g. ensure current category/event exist) and surface a user-visible message if regeneration fails.

2. **Protect `/state.json` reads**  
   `GET /state.json` uses `load_state()` with no lock. Under concurrent writes (e.g. `merge_state`, `sync_comp_to_broadcast`), clients can see partially written JSON. Options: (a) hold a shared lock for the duration of `load_state()` in the route and have writers use the same lock for the full read–modify–write, or (b) write to a temp file and rename so readers never see a torn file.

3. **Reduce leaderboard N+1**  
   `get_leaderboard()` and `get_leaderboard_detailed()` call `calculate_event_points(event_id, category)` in nested loops (categories × athletes × events). Under load this is slow and opens many DB connections. Consider: (a) batching — compute all event points for one category in one or a few queries and aggregate in memory, or (b) a materialized cache (e.g. recompute and store leaderboard when results change) so comp run and broadcast sync read cached data.

4. **Validate event_id in API**  
   `GET /comp/api/event_points?event_id=&category=` uses `int(event_id)` without try/except; invalid or missing `event_id` can 500. Use `_safe_int()` or similar and return 400 with a clear message for bad input.

### Medium priority

5. **Sync path under lock (optional but useful)**  
   `sync_comp_to_broadcast()` does a long read phase (DB + state) then a single `merge_state()`. Two concurrent syncs can interleave so the last merge overwrites the first. If you need strict ordering, run the whole sync (read + build payload + merge) under a dedicated lock so only one sync runs at a time.

6. **DB connection reuse**  
   Every `db()` is a new SQLite connection; scoring and leaderboard paths open many short-lived connections. Consider a single connection per request (e.g. request-scoped connection or a small pool) so one request doesn’t multiply connection churn.

7. **Category_start_counts TOCTOU**  
   `get_category_start_count()` and `lock_category_start_counts()` read state (and DB) outside the lock, then take the lock only to write. Another writer (e.g. `merge_state`) can change state in between. For consistency, either read state inside the same lock used for the write, or use a single “ensure category counts” path that’s the only writer for that key.

8. **Empty categories after Clear All**  
   `clear_all` clears `CATEGORY_ORDER`. Comp home and other views assume at least one category or use `CATEGORY_ORDER[0]`. Most call sites use `CATEGORY_ORDER[0] if CATEGORY_ORDER else ""`; ensure every such use is guarded and that the UI shows an explicit “Add category” state when the list is empty so no code path hits `IndexError`.

### Lower priority

9. **Logging**  
   Add structured logging (e.g. `logging` module) for: sync failures, `regenerate_remaining_heats` errors, invalid form/query params, and optionally each comp action. Helps with stress-test analysis and production debugging.

10. **Judge payload shape**  
    Judge panels send `judgeL1`, `judgeL2`, … with `reps`, `light`, `timerRemaining`, etc. If the server ever reads these for scoring, validate keys and types and ignore unknown keys so future client changes don’t break the server.

11. **Event type in DB**  
    Test data inserts `"object"` for Atlas Stones; schema and code use `event_type`. Confirm everywhere that expects `"object"` (e.g. objects+time) matches the DB value and that broadcast/judge `eventType` is consistent.

---

## 4. Suggested Stress Tests (Manual or Automated)

- **Heat generation**: Many categories (e.g. 6) × many athletes (e.g. 100+); call Generate Heats, then change lanes and trigger regenerate; switch event and category and repeat. Watch for slow response or errors.
- **Scoring**: Enter and save results for many lanes; open leaderboard and comp run repeatedly; run “Simulate results” with many athletes/events. Watch for timeouts and N+1-related slowness.
- **Broadcast + judges**: Multiple browser tabs on comp control, judge master, and judge lane; change heat, save results, push to broadcast. Poll `state.json` in a loop (e.g. curl or script) while triggering syncs. Check that state stays consistent and that judge URLs and lane data don’t disappear.
- **Concurrent writes**: Run several `POST /update` and comp actions (set heat, save results) in parallel (e.g. script with background jobs). Ensure no 500s and that `state.json` remains valid JSON and contains expected keys.
- **Error paths**: Send invalid form data (e.g. `event=abc`, `heat=-1`); delete or corrupt `state.json` and hit comp home and `state.json`; call APIs with missing or invalid `event_id`/`category`. Confirm no uncaught exceptions and sensible 4xx/5xx or fallback behavior.

---

## 5. Summary

- **Done**: Safe int parsing for set_heat/jump_to, resilient `load_state()`, and a body size limit on `/update` to reduce DoS risk.
- **Next**: Log (and optionally surface) errors in `set_lanes` regeneration; make state reads safe under concurrency; and reduce leaderboard/scoring N+1 and DB churn. After that, tighten category_start_counts and empty-category handling, then add logging and validation for judge/API payloads.

Applying the high-priority items will significantly improve robustness under load and invalid input; the rest will improve maintainability and production readiness.

---

## 6. Scale: 800 athletes, 20 categories, 10 events

The engine is tuned to support **~800 athletes, 20 categories, and 10 events** without timeouts or excessive DB churn.

**What was done**

- **Leaderboard (get_leaderboard)**  
  Previously: one new DB connection per `calculate_event_points` call → **athletes × events** connections per category (e.g. 40 × 10 = 400 per category).  
  Now: **one connection per category**; all event points for that category are computed in a loop using `_compute_event_points_core(con, ...)` over the same connection. So 20 categories → 20 connections total instead of thousands.

- **Leaderboard detailed (get_leaderboard_detailed)**  
  Same batching: one connection, loop over events with `_compute_event_points_core`, then build rows in memory.

- **Broadcast sync (sync_comp_to_broadcast)**  
  Lane scores used to call `get_raw_result(name, event_id)` per lane (one connection per lane).  
  Now: **get_raw_results_batch(event_id, list_of_names)** — one query for all current heat athletes.

**Rough impact**

| Path | Before (800/20/10) | After |
|------|--------------------|--------|
| get_leaderboard(one category) | ~400 connections, 400× scoring | 1 connection, 10× scoring |
| get_leaderboard(all) | 20 × 400 = 8000 connections | 20 connections |
| get_leaderboard_detailed(cat) | ~400 connections | 1 connection |
| sync_comp_to_broadcast (lanes) | 4+ connections for raw results | 1 batch query |

**Recommendation for your comp**

- Run a **dry run** with Load Test Data scaled up (or import 800 athletes, 20 categories, 10 events) and click through: Generate Heats, Run screen, Leaderboard, Change heat, Save results, Regenerate heats. If any page is slow, we can add optional caching (e.g. invalidate on result save).
- Keep **backups** of `comp.db` and `state.json` before the event and after each category.
