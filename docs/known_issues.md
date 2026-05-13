# Known Issues — StrongPass Competition OS
**Last updated:** 2026-05-13

Severity: **CRITICAL** → **HIGH** → **MEDIUM** → **LOW**

---

## CRITICAL

None currently active. TD-C1 (Gunicorn init bypass) was resolved in Phase 1.

---

## HIGH

### H1 — No CSRF Protection on Any Form
**Source:** TD-H1  
**File:** All `templates/*.html`  
**Impact:** Any page that lures an authenticated admin to click a link can trigger `clear_all`, `delete_athlete`, etc.  
**Partially mitigated by:** Beta gate (requires site-level auth, reduces public exposure)  
**Fix:** Add Flask-WTF or a minimal CSRF token in the `before_request` / form rendering flow.  
**Blocked by:** Nothing — standalone fix

### H2 — `/update` and `/judge/set_status` Have No Per-Endpoint Auth
**Source:** TD-H2  
**File:** `server.py:970`, `server.py:1859`  
**Impact:** Anyone with the beta password can push arbitrary state or mark athletes DNS/DNF. On LAN events this is tolerable; on internet-facing events it is a risk.  
**Partially mitigated by:** Beta gate now requires site-level cookie auth before any endpoint is reachable.  
**Fix options:** (a) Add a separate judge token distinct from BETA_TOKEN, or (b) document accepted risk and rely on beta gate for now.  
**Note:** Completely unauthenticated before beta gate. Currently protected by beta cookie only.

### ~~H3-sub~~ — Timer ticks triggered full SSE payload rebuilds
**Resolved in:** `5a1ff30`  
`_results_version` counter now separates score changes from timer/state changes. SSE payload stripped from 136KB → 10KB. Timer ticks no longer invalidate the results cache or trigger frontend re-renders.

### H3 — `set_lanes` Swallows `regenerate_remaining_heats` Errors
**Source:** TD-H3, STRESS_TEST recommendations item 1  
**File:** `server.py` — `action_set_lanes`  
**Impact:** Heat regeneration failure is invisible to the operator.  
**Fix:** Log the exception; return a visible flash message or error response.

### H4 — New DB Connection Opened Per Operation
**Source:** TD-H4, STRESS_TEST recommendations item 6  
**File:** `server.py` throughout  
**Impact:** Under simultaneous judge submissions, many short-lived connections open/close. Under current load (1 event, <10 concurrent) this is manageable. Not safe to scale.  
**Fix:** Request-scoped connection via Flask `g` object. Phase 6 in refactor plan.

---

## MEDIUM

### M1 — `CATEGORY_ORDER` Is a Global Mutable List
**Source:** TD-C2  
**File:** `server.py:29`  
**Impact:** With 1 Gunicorn worker it works. With 2+ workers, processes have separate copies and category state diverges. Currently safe (1 worker only).  
**Fix:** Store in DB or always derive from state.json. Depends on further modularisation.

### M2 — Migration Code Uses Bare `except: pass`
**Source:** TD-H6  
**File:** `app/database.py` — `ensure_schema()` migration block  
**Impact:** Schema migration failures (disk full, permissions, etc.) are silently swallowed.  
**Fix:** Catch `sqlite3.OperationalError` specifically; log all other exceptions.

### M3 — `action_save_results` Has Weak Input Validation
**Source:** TD-H7, TD-H8  
**File:** `server.py:1243` and `server.py:1496`  
**Impact:** Two near-duplicate save handlers with inconsistent validation (one has NaN/range check, one doesn't). Any bug fix must be applied to both.  
**Fix:** Extract shared `_save_heat_results()` function with consistent validation.

### M4 — state.json Serves Two Unrelated Concerns
**Source:** TD-M2  
**File:** `state.json` (runtime), `server.py`  
**Impact:** Broadcast display state and competition engine config are mixed in one file. A judge button tap rewrites competition config fields and vice versa.  
**Fix:** Separate into `broadcast_state.json` and engine config in DB. Medium-term architectural change.

### ~~M5~~ — `comp_leaderboard.html` Template Ignores Computed Data
**Resolved in:** `ab093ec`  
Full per-event breakdown table now rendered when category selected. SSE live updates added. Public `/api/results/<cat>` endpoint added for spectator pages.

### M6 — No `/health` Endpoint
**Source:** TD-L4  
**Impact:** No way for uptime monitors or load balancers to check app health programmatically.  
**Fix:** Add `GET /health` returning `{"status":"ok","db":"ok"}`.

### M7 — `comp/api/event_points` Missing Input Validation
**Source:** STRESS_TEST recommendations item 4  
**File:** `server.py` — `api_event_points()`  
**Impact:** `int(event_id)` without try/except — invalid or missing `event_id` returns 500.  
**Fix:** Use `_safe_int()` and return 400 for bad input.

---

## LOW

### L1 — No Structured Logging
**Source:** TD-L1  
**Impact:** Production debugging requires reading raw stdout; bare `except: pass` blocks hide errors silently.  
**Fix:** Add `logging.getLogger(__name__)` throughout; configure Gunicorn to use Python logging.

### L2 — `requirements.txt` Has No Version Pins
**Source:** TD-L2  
**File:** `requirements.txt`  
**Current content:** `flask`, `gunicorn`  
**Impact:** A `pip install` in future may install incompatible versions.  
**Fix:** Pin `flask==3.1.3`, `gunicorn==26.0.0`; add `itsdangerous`, `werkzeug`, `markupsafe`.

### L3 — Lane Count Maximum Inconsistency
**Source:** TD-L6  
**File:** `server.py` — `action_set_lanes`  
**Impact:** `set_lanes` caps at 4; `_protect_lane_stopped_timers` iterates up to 8; `_default_inactive_judge` sets up to `judgeL8`. The cap is inconsistent.  
**Fix:** Define `MAX_LANES = 8` in config and apply consistently.

### L4 — `ws-client.js` Still Exists in Root
**Source:** TD-M4 (partially resolved)  
**File:** `ws-client.js`  
**Impact:** All 8 overlay files were migrated to `sse-client.js` in Phase 2. The old file is no longer imported by any HTML file but still exists on disk.  
**Fix:** Delete `ws-client.js`. Verify no remaining references first: `grep -r "ws-client" *.html templates/`.

### L5 — No `.env` or Environment Variable Documentation
**Source:** TD-L3  
**Impact:** Deployers must read the code or service file to discover `BETA_TOKEN`, `COMP_PASSWORD`, `SECRET_KEY`, `PROXY_ALLOWLIST`.  
**Fix:** Create `.env.example` with all env vars documented.

### L6 — `?token=` in OBS URLs Appears in Access Logs
**Source:** Introduced by beta_auth (2026-05-13)  
**File:** `app/beta_auth.py`  
**Impact:** When OBS operators use `?token=SECRET` in overlay URLs, the token appears in nginx access logs at `/opt/strongpass/logs/access.log`.  
**Severity:** Low — logs are server-side only, not exposed publicly. Acceptable for temporary beta.  
**Fix when removing beta gate:** No action needed. Disappears when BETA_TOKEN is unset.

### L7 — OPTIONS Preflight Bypasses Beta Gate
**Source:** Introduced by beta_auth (2026-05-13)  
**File:** `app/beta_auth.py:_beta_gate`  
**Impact:** HTTP OPTIONS requests bypass authentication entirely. In practice, judge panels are same-origin and never send OPTIONS preflights. Only matters if a cross-origin caller sends preflight.  
**Severity:** Very low — OPTIONS returns only CORS headers, no data.  
**Fix:** No action required given current architecture.

---

## Resolved (for reference)

| ID | Description | Fixed in |
|----|-------------|----------|
| TD-C1 | Gunicorn bypasses all initialization | Phase 1 (`1f69015`) |
| TD-C3 | SSE triggers full leaderboard recalc per update | Phase 4 (`074b7bd`) |
| M5 | comp_leaderboard.html ignores detailed_rows | Phase 5 (`ab093ec`) |
| Mobile | comp_results_public.html hides event cols on phones | Phase 5 (`5ce14d4`) |
| Polling | comp_heats.html 10s setInterval reload | Phase 5 (`ab093ec`) |
| TD-M4 | 8 overlays using ws-client.js 500ms polling | Phase 2 (`f624a8e`) |
| TD-M3 | `control.html` polling only | Phase 2 (`23e1bc6`) |
| TD-M6 | `results.html` 15s polling | Phase 2 (`88c48f3`) |
| TD-M5 | `comp_callroom/arena` full page reload | Phase 2 (`23e1bc6`) |
| Form parse 500 | `set_heat`/`jump_to` with non-numeric input | Stress test pass |
| load_state crash | Corrupt/missing state.json caused 500 | Stress test pass |
| /update DoS | No body size limit | Stress test pass (512KB cap) |
| Gunicorn boot | `sqlite3.Row` had no `.get()` | Simulation pass |
