# Known Issues — StrongPass Competition OS
**Last updated:** 2026-05-16 (Phase 6 — timer tick freeze fix `dec4b37`)

Severity: **CRITICAL** → **HIGH** → **MEDIUM** → **LOW**

---

## CRITICAL

None currently active. TD-C1 (Gunicorn init bypass) resolved in Phase 1. C1 (control.html 5 s poll) resolved in `a96ea86`. C2 (timer tick freeze) resolved in `dec4b37`.

---

## HIGH

### H1 — No CSRF Protection on Any Form
**Source:** TD-H1  
**File:** All `templates/*.html`  
**Impact:** Any page that lures an authenticated admin to click a link can trigger `clear_all`, `delete_athlete`, etc.  
**Partially mitigated by:** Beta gate (requires site-level auth, reduces public exposure)  
**Fix:** Add Flask-WTF or a minimal CSRF token in the `before_request` / form rendering flow.  
**Blocked by:** Nothing — standalone fix

### ~~H2~~ — `/update` and `/judge/set_status` Had No Per-Endpoint Auth
**Resolved in:** Phase 3  
`/update`, `/control.html`, `/judge.html`, `/judge-master.html`, `/debug.html` now protected by session-based comp auth. HTTP Basic Auth removed. `/comp/login` login page with `session["comp_ok"]` cookie. `/comp/logout` clears session. Non-HTML requests (curl, AJAX) receive 401; browser requests redirect to login page. `COMP_PASSWORD` env var required; if unset, comp auth is skipped (dev mode). Public overlay routes (`/stream`, `/state.json`, `*.html` overlays) remain accessible without comp auth.

### ~~H3-sub~~ — Timer ticks triggered full SSE payload rebuilds
**Resolved in:** `5a1ff30`  
`_results_version` counter now separates score changes from timer/state changes. SSE payload stripped from 136KB → 10KB. Timer ticks no longer invalidate the results cache or trigger frontend re-renders.

### ~~H3-heats-reload~~ — `comp_heats.html` infinite reload loop after Generate Heats
**Resolved in:** `5e45c26`  
`lastResultsVersion` initialised as `null` in JS; `null` coerces to `0` for numeric comparison, so any `results_version > 0` (true after the first `_invalidate_results_cache()` call — load test data, add athlete) caused `N <= null → N <= 0 → false → location.reload()`. Page reloaded, null again, infinite loop. Fix: seed `lastResultsVersion` from the first SSE message and return early (no reload) to establish a baseline. Subsequent higher versions still trigger reload as intended.

### ~~H1~~ — results_version resets to 0 on server restart
**Resolved in:** `a96ea86`  
`_RESTART_TOKEN` (epoch at startup) now injected into every SSE payload and `/state.json`. All `results_version`-tracking pages detect token change and reset their local version counter, triggering an immediate re-fetch. Prevents indefinite stale-results display after server restart.

### ~~H2~~ — comp_run.html had no live updates
**Resolved in:** `a96ea86`  
SSE listener added. Reloads page on `compHeat`/`compEvent` change from any source. Score entry forms unaffected — reload only fires on heat advance, not on score updates.

### ~~H3-arena~~ — comp_arena.html reloaded on heat change (OBS unsafe)
**Resolved in:** `a96ea86`  
Replaced `location.reload()` with full DOM-patch on every SSE push. Event name, category, heat label, and all lane panels update in-place. Zero blank frames. Fully stable as OBS browser source.

### ~~H3-callroom~~ — comp_callroom.html had 30 s periodic fallback reload
**Resolved in:** `a96ea86`  
Removed `setInterval(() => location.reload(), 30000)` fallback. Added proper SSE reconnect with 5 s backoff. Heat-change reload preserved (callroom shows next-heat data that requires a server render).

### ~~H3~~ — `set_lanes` Swallows `regenerate_remaining_heats` Errors
**Resolved in:** Phase 2  
`except Exception: pass` replaced with `app.logger.error(...)` — failure now appears in Gunicorn logs. A user-visible flash message would require Flask flash infrastructure (not yet added; acceptable for now).

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

### ~~M1~~ — comp_leaderboard.html null coercion on results_version
**Resolved in:** `a96ea86`  
Seeded `lastVersion` from first SSE message without triggering fetch. Added `renderTable` null guard. Added restart_token detection.

### ~~M2~~ — comp_results_public.html null coercion on results_version
**Resolved in:** `a96ea86`  
Same fix as M1.

### ~~M4~~ — control.html category color fallback mismatch
**Resolved in:** `a96ea86`  
Replaced positional array with named object matching `app/config.py` CAT_COLORS. Added separate `CAT_COLOR_PALETTE` array for `addCategory()` cycling.

### ~~L3~~ — ws-client.js dead code
**Resolved in:** `a96ea86`  
Deleted. Zero imports confirmed across all HTML files.

### ~~M6~~ — No `/health` Endpoint
**Resolved in:** Phase 2  
`GET /health` added — returns `{"status":"ok","db":"ok","restart_token":N}`. Performs live `SELECT 1` against SQLite; returns `"db":"error"` if it fails.

### ~~M7~~ — `comp/api/event_points` Missing Input Validation
**Resolved in:** Phase 2 audit  
`_safe_int()` was already in use at the call site (line ~2010). The `int()` bare call noted in TD never reached production — confirmed fixed.

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

### ~~L4~~ — `ws-client.js` Still Exists in Root
**Resolved in:** Phase 2 audit  
File confirmed deleted from disk — no longer present at `/opt/strongpass/current/ws-client.js`. Zero references across all HTML files verified.

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
| H3 | `set_lanes` swallows regenerate errors | Phase 2 |
| M6 | No `/health` endpoint | Phase 2 |
| M7 | `api_event_points` missing input validation | Phase 2 audit |
| L4 | `ws-client.js` still on disk | Phase 2 audit |
| SSE N×load | N clients × N disk reads per SSE event | Phase 2 (SSE payload cache) |
| control.html DOM churn | Full athlete/lane rebuild on every timer tick | Phase 2 (hash dirty check) |
| scorebug.html animation reset | `#bug-scroll` CSS animation jumped to frame 0 every ~1s | Phase 3 (`_lastScrollKey`) |
| leaderboard.html transition flash | `.lb-row` CSS transitions reset every tick | Phase 3 (`_lastLbKey`) |
| lowerthird.html storm | animOut+animIn (700ms) fired on every SSE tick | Phase 3 (`_lastLanesKey`) |
| champion.html particle storm | 40 DOM elements created on every SSE tick when visible | Phase 3 (`_champVisible`) |
| reps.html innerHTML rebuild | lights strip + rep cards torn down/rebuilt every tick | Phase 3 (`_builtLaneCount` + `patchContent`) |
| lineup.html fade storm | showCategory() fade-out/in fired on every SSE tick | Phase 3 (`_lastCatKey`) |
| lb category dropdown | showed only current-heat category (not all categories) | Phase 3 (reads `live.categories`) |
| category color reversion | colors reverted to hardcoded fallbacks on SSE update | Phase 3 (preserves stored color in `sync_comp_to_broadcast`) |
| category color no persistence | colors lost on restart; new categories ignored color picker | `f45fb42` (`category_colors` dict in state.json; `set_category_color` endpoint; inline color picker) |
| lb standings DB query per tick | `sync_comp_to_broadcast()` ran `get_leaderboard()` (DB query) on every timer tick | `2711492` (`_get_cached_standings()` reads from shared `_results_cache` — no per-tick DB query) |
| director lb switch DB latency | `/director/lb` ran `get_leaderboard()` for each switch | `2711492` (`lbStandings` pre-computed; `/director/lb` uses dict lookup) |
| comp auth Basic Auth | browser dialog, no separate operator session | Phase 3 (session-based `/comp/login`) |
| lb coupled to scoring operator | leaderboard overlay followed scoring operator's active tab | Phase 4 (`lbAthletes` + `lbCategoryOverride` + `/director/lb`) |
| lbCategory overwritten on sync | director's category selection reset by every syncFromCompEngine() | Phase 4 (guard: `!state.lbCategoryOverride`) |
| Timer tick freeze (C2) | `_apply_judge_scores_to_raw_results()` called `sync_comp_to_broadcast()` unconditionally — 3+ DB opens + 4 disk R/W per tick at 4 Hz saturated gthread pool, causing 10-120s UI freezes | `dec4b37` (sync only when `results_changed=True`; unchanged ticks call `_sse_notify()` only; 50-150ms → 2-5ms per tick) |
| double sync in save_next | `action_save_results_run` called `sync_comp_to_broadcast()` twice when advancing to next heat (one after save, one after state advance) | `dec4b37` (single sync after all mutations) |
