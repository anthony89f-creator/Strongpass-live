# Technical Debt Report — Strongman Competition OS
**Date:** 2026-05-11  
**Severity:** CRITICAL / HIGH / MEDIUM / LOW

---

## CRITICAL — Production-Breaking Issues

### TD-C1: Gunicorn Bypasses All Initialization
**File:** `server.py:2071–2117`  
**Impact:** App starts in broken state under Gunicorn in production

All startup code lives in `if __name__ == "__main__":` which Gunicorn never executes:
- DB schema is never set up (tables may not exist)
- `CATEGORY_ORDER` always resets to hardcoded default on restart — user-configured categories are lost
- `state.json` is never initialized if missing
- `_restore_category_order()` never called
- `sync_comp_to_broadcast()` never called at boot

**Fix:** Move initialization into a module-level `startup()` call or `@app.before_first_request` equivalent.

---

### TD-C2: CATEGORY_ORDER is a Global Mutable List
**File:** `server.py:21`  
**Impact:** Fragile, surprising, will break if ever scaled beyond 1 worker

```python
CATEGORY_ORDER = ["U80", "U90", "U105", "U120", "Womens", "Mens Open"]
```

This is mutated in-place by `action_add_category()`, `action_delete_category()`, `action_reorder_category()`. With 1 Gunicorn worker it works. With 2+ workers, processes have separate copies and state diverges immediately.

Furthermore, this list is the source of truth, but it's only persisted to `state.json` via `_persist_category_order()` — called inconsistently. It's restored from `state.json` only in `_restore_category_order()` which itself is only called in the `__main__` block (see TD-C1).

**Fix:** Store category order exclusively in DB or always derive from state.json. Remove global mutation.

---

### TD-C3: SSE Stream Triggers Full Leaderboard Recalculation for All Categories on Every Update
**File:** `server.py:883–908`, `server.py:594–617`  
**Impact:** Severe performance degradation under live judging with many SSE clients

Every judge button tap triggers:
1. `sync_comp_to_broadcast()` — 1 DB connection, leaderboard for current category
2. `_sse_notify()` — wakes all connected SSE clients
3. Each SSE client thread: `get_public_results()` → `get_leaderboard_detailed()` for ALL categories

With 6 categories × 20 athletes × 10 events × 10 SSE clients: hundreds of DB reads per tap.

**Fix:** Either (a) send lightweight partial state updates via SSE instead of full state, or (b) cache the leaderboard and only invalidate on result changes, or (c) move `get_public_results()` out of the SSE hot path.

---

## HIGH — Functional / Security Issues

### TD-H1: No CSRF Protection on Any Form
**File:** All `templates/*.html`  
**Impact:** Cross-site request forgery possible against any authenticated user

All `/comp/action/*` routes use plain HTML forms with POST. No CSRF tokens. Any page that can lure an admin to click a link can trigger `clear_all`, `delete_athlete`, etc.

**Fix:** Add Flask-WTF or a minimal CSRF token implementation.

---

### TD-H2: `/update` and `/judge/set_status` Have No Authentication
**File:** `server.py:1090–1113`, `server.py:1995–2020`  
**Impact:** Anyone on the network can push arbitrary state or mark athletes DNS/DNF

These endpoints are intentionally open so judge panels work without credentials. However, on a public deployment this allows remote state injection. On LAN-only events this is acceptable; on internet-facing deploys it is not.

**Fix:** Either (a) add network-level restriction (nginx allow/deny by IP range), or (b) add a separate judge token, or (c) document the risk and restrict in nginx for production.

---

### TD-H3: set_lanes Swallows regenerate_remaining_heats Errors Silently
**File:** `server.py:1371–1374`  
**Impact:** Heat regeneration failure is invisible; operator sees no error

```python
try:
    regenerate_remaining_heats()
except Exception:
    pass
```

**Fix:** Log the exception and return an error response or flash message.

---

### TD-H4: New DB Connection Opened for Every Operation
**File:** `server.py:217–222`, throughout  
**Impact:** Connection churn under heavy judging; each operation opens/closes its own connection

`db()` creates a new SQLite connection every call. Many handlers open multiple connections. Under simultaneous judge submissions and leaderboard requests, this creates unnecessary overhead.

**Fix:** Use a request-scoped connection via Flask's `g` object, or use a small thread-local connection pool.

---

### TD-H5: /state.json GET Has No Concurrency Protection
**File:** `server.py:940–953`  
**Impact:** Low — atomic writes (rename) prevent torn reads, but route still reads without lock

`state_json()` calls `load_state()` without `state_lock`. Writes use atomic rename so readers never see partial JSON. However, two rapid reads can see different versions. For broadcast purposes this is acceptable; for competition state it could cause brief inconsistency.

**Fix:** Low priority given atomic writes, but document the decision.

---

### TD-H6: Migration Code Uses Bare `except: pass`
**File:** `server.py:234–256`  
**Impact:** Schema migration failures are invisible; app continues with wrong schema

```python
try:
    cur.execute("ALTER TABLE events ADD COLUMN event_type TEXT DEFAULT 'reps'")
    con.commit()
except: pass
```

This is intentional for "column already exists" but swallows any other error (disk full, permissions, etc.).

**Fix:** Catch `sqlite3.OperationalError` specifically (which covers "duplicate column"), not all exceptions.

---

### TD-H7: action_save_results Has Weak Input Validation
**File:** `server.py:1391–1414`  
**Impact:** Invalid float inputs silently skipped; out-of-range values accepted in some paths

`action_save_results` uses bare `except: continue` (line 1395). `action_save_results_run` (lines 1652–1665) is better — validates range and NaN. The two handlers are inconsistent.

**Fix:** Consolidate both into one handler with consistent validation.

---

### TD-H8: Duplicate Result-Save Logic
**File:** `server.py:1378–1416` and `server.py:1636–1689`  
**Impact:** Validation divergence (one has NaN/range checks, the other doesn't); maintenance burden

`action_save_results` and `action_save_results_run` do the same thing with slightly different validation. Any bug fix must be applied to both.

**Fix:** Extract a shared `_upsert_results(event_id, form_data)` function.

---

## MEDIUM — Architecture / Maintainability Issues

### TD-M1: Monolithic server.py (2,117 lines)
**File:** `server.py`  
**Impact:** Maintenance burden; difficult to test individual services; onboarding friction

All routes, services, DB access, auth, SSE, heat generation, scoring, leaderboard, backups, and external adapters live in one file. No separation of concerns.

**Fix:** Phase 3 modularization into `app/routes/`, `app/services/`, `app/realtime/`, etc.

---

### TD-M2: state.json Serves Two Unrelated Concerns
**Impact:** Coupling between broadcast display state and competition engine config

The file contains both "what the OBS overlays should show" and "where we are in the competition". These have different change frequencies, audiences, and consumers. Mixing them means a judge button tap rewrites competition config fields and vice versa.

**Fix:** Separate into `broadcast_state.json` (overlay config) and keep engine config in DB. Or clearly partition the state dict with namespaced keys.

---

### TD-M3: control.html Uses Only Polling (No SSE)
**File:** `control.html:693–707`, `control.html:1281–1294`  
**Impact:** 8 HTTP requests per minute per open control panel; UI can be up to 5s stale

`control.html` is the most important admin page but has no SSE connection. It polls `/state.json` every 3s (connection check) and every 5s (full sync). In a live event this means overlay operators are always working with slightly stale data.

**Fix:** Add SSE connection to control.html as primary update path; keep polling only as reconnect fallback.

---

### TD-M4: Eight OBS Overlay Files Import ws-client.js (500ms Polling)
**Files:** `scorebug.html`, `leaderboard.html`, `lowerthird.html`, `reps.html`, `champion.html`, `h2h.html`, `lineup.html`, `manual_lowerthird.html`  
**Impact:** 8 overlays × 2 req/s = 960 HTTP requests/minute against state.json at rest

All 8 overlay files use the legacy `ws-client.js` which polls every 500ms. Additionally, each file does a one-time `fetch('/state.json')` on load. After load, updates only arrive via ws-client polling — there is no SSE subscription.

**Fix:** Replace ws-client.js with a shared `sse-client.js` that subscribes to `/stream`.

---

### TD-M5: comp_callroom.html and comp_heats.html Do Full Page Reload
**Files:** `templates/comp_callroom.html:175`, `templates/comp_heats.html:84`  
**Impact:** 5s/10s full page reload causes flash, session reset, and scroll position loss

```javascript
setInterval(() => location.reload(), 5000);
setInterval(() => location.reload(), 10000);
```

**Fix:** Replace with SSE or targeted fetch that updates only the DOM elements that change.

---

### TD-M6: results.html Polls Every 15 Seconds Instead of Using SSE
**File:** `results.html:250`  
**Impact:** Public results page can show data up to 15s stale during a live event

`results.html` is the public-facing results display. It has sophisticated rendering logic (per-event breakdown, category filter) but fetches on a 15s timer with no SSE.

**Fix:** Add SSE subscription; update the table when state changes arrive.

---

### TD-M7: get_competition_status() Opens Multiple DB Connections
**File:** `server.py:89–107`  
**Impact:** 3 DB connections opened in one call; called on some page loads

Opens two DB connections directly, then calls `get_all_heats()` which opens a third. Small overhead, repeated on each page load that calls this function.

**Fix:** Use request-scoped connection from TD-H4 fix.

---

### TD-M8: comp_run.html Opens a Second DB Connection for Winner Detection
**File:** `server.py:1775–1785`  
**Impact:** Code smell; inconsistent with surrounding code which uses `con` and `con2`

```python
con2 = db()
# ...
con2.close()
```

**Fix:** Consolidate into the existing `con` opened at line 1726.

---

## LOW — Polish / Quality Issues

### TD-L1: No Structured Logging
**Impact:** Production debugging requires reading raw stdout; no log levels; no request IDs

The app uses no `logging` module. Errors are swallowed or printed to stdout.

**Fix:** Add structured logging (`logging.getLogger(__name__)`) throughout; configure Gunicorn to use Python logging.

---

### TD-L2: requirements.txt Is Minimal
**File:** `requirements.txt`  
**Impact:** No version pinning; breaks if Flask or Gunicorn release breaking changes

```
flask
gunicorn
```

No versions. A `pip install` in a year may install an incompatible Flask version.

**Fix:** Pin versions: `flask==3.x.x`, `gunicorn==21.x.x`. Add `python-dotenv` for `.env` support.

---

### TD-L3: No .env or Environment Variable Documentation
**Impact:** Deployers must read the code to discover `COMP_PASSWORD`, `PROXY_ALLOWLIST`

No `.env.example`, no documentation of available environment variables beyond comments in `strongman.service`.

**Fix:** Create `.env.example` and document all env vars.

---

### TD-L4: No Healthcheck Endpoint
**Impact:** Load balancers, uptime monitors, and Docker HEALTHCHECK cannot verify app health

**Fix:** Add `GET /health` returning `{"status": "ok", "db": "ok"}`.

---

### TD-L5: Category Colors Are Hardcoded
**File:** `server.py:22`  
**Impact:** Custom categories get a fallback color; no way to set custom colors via UI

```python
CAT_COLORS = {"U80":"#CC0044", ...}
```

**Fix:** Store colors in state or DB; allow setting in UI.

---

### TD-L6: Lane Count Hardcoded Maximum of 4 in set_lanes
**File:** `server.py:1366`  
**Impact:** System references "1–8 lanes" in comments but enforces max 4 in the action handler

```python
lanes = max(1, min(4, lanes))
```

But `_protect_lane_stopped_timers` iterates `range(1, 9)` (8 lanes), and `_default_inactive_judge` sets inactive lanes up to `judgeL8`. The cap should be consistent.

**Fix:** Define a `MAX_LANES = 8` constant and apply it consistently.

---

### TD-L7: No Dockerfile or docker-compose.yml
**Impact:** Deployment requires manual Hetzner setup; no reproducible environment

**Fix:** Phase 9/11 — add Dockerfile, docker-compose.yml, .dockerignore.

---

### TD-L8: comp_leaderboard.html (templates/) Shows No Per-Event Breakdown
**File:** `templates/comp_leaderboard.html`  
**Impact:** The server computes `get_leaderboard_detailed()` but the template only shows name + total score

The template (75 lines) only renders `athletes` (simple list), ignoring `detailed_rows` and `events_list` which are passed from the backend but never displayed.

**Fix:** Phase 5 — implement the detailed leaderboard UI.

---

### TD-L9: Simulation Scripts Are in Production Root
**Files:** `simulate_comp.py`, `simulate_judge_to_run.py`, `simulate_lane1_judge_time.py`  
**Impact:** Development tools served from production root; reachable via `/<filename>` (blocked by extension check, but still present)

**Fix:** Move to `scripts/` or `dev/` directory.

---

### TD-L10: Test Files in Production Root
**Files:** `test_judge_pages.py`, `test_judge_sync.py`  
**Impact:** Same as above; test dependencies not in requirements.txt

**Fix:** Move to `tests/` directory; add `requests` to dev requirements.
