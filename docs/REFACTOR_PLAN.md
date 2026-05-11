# Refactor Plan — Strongman Competition OS
**Date:** 2026-05-11  
**Approach:** Staged incremental refactors — each phase is a safe, testable checkpoint  
**Priority order:** Architectural risks first, cosmetic cleanup last

---

## Dependency Map

```
TD-C1 (Gunicorn init bug)
  └─ blocks everything — fix first

TD-C2 (CATEGORY_ORDER global) 
  └─ depends on Phase 3 (modularization)

TD-C3 (SSE cost per update)
  └─ Phase 2 (SSE migration) partially mitigates
  └─ Phase 8 (leaderboard caching) fully fixes

Phase 2 (SSE migration)
  └─ depends on Phase 1 (audit complete)
  └─ unblocks: overlay reliability, judge panel stability

Phase 3 (modularization)
  └─ depends on Phase 2 (SSE complete, no more ws-client)
  └─ depends on Phase 1 (route map complete)
  └─ unblocks: Phase 4, Phase 5, Phase 6 cleanly

Phase 4 (weight & reps scoring)
  └─ depends on Phase 3 (clean scoring engine module)
  └─ isolated DB schema change — can be done anytime after Phase 1

Phase 5 (leaderboard expansion)
  └─ depends on Phase 3 (leaderboard service isolated)
  └─ depends on Phase 2 (SSE in place for realtime lb)

Phase 6 (DB layer)
  └─ depends on Phase 3 (db.py module)
  └─ unblocks: Phase 8 (performance optimizations)

Phase 7 (frontend cleanup)
  └─ depends on Phase 2 (SSE replaces ws-client)
  └─ depends on Phase 3 (shared static dir available)

Phase 8 (performance)
  └─ depends on Phase 6 (clean DB layer)
  └─ depends on Phase 3 (leaderboard service isolated)

Phase 9 (production hardening)
  └─ depends on Phase 3 (app factory pattern)
  └─ partly independent — Dockerfile, logging can start any time

Phase 10 (security)
  └─ depends on Phase 3 (centralized route auth)
  └─ CSRF depends on Phase 7 (frontend forms)

Phase 11 (deployment)
  └─ depends on Phase 9

Phase 12 (future foundations)
  └─ depends on Phase 3 (clean architecture)
```

---

## Risk Assessment

| Phase | Risk Level | Rollback Strategy | Primary Risk |
|-------|-----------|-------------------|--------------|
| 1 (Audit) | None | N/A | Documentation only |
| 2 (SSE) | Low | Revert HTML files only | SSE disconnect behavior on mobile/proxy |
| 3 (Modularize) | Medium | Git revert entire phase | Import errors break all routes |
| 4 (Scoring) | Low | Schema migration additive only | Points calc changes for existing events |
| 5 (Leaderboard) | Low | Template-only changes | UI regression on leaderboard page |
| 6 (DB layer) | Medium | Revert db.py | Connection pool exhaustion edge cases |
| 7 (Frontend) | Low | Revert HTML files | CSS/JS breakage on specific overlays |
| 8 (Performance) | Medium | Revert caching layer | Cache invalidation bugs |
| 9 (Hardening) | Low | Revert config files | Gunicorn startup failure |
| 10 (Security) | Medium | Revert auth middleware | CSRF breaks forms, locked out of admin |
| 11 (Deployment) | Low | Revert deploy files | N/A — additive |
| 12 (Foundations) | Low | N/A — additive only | Over-engineering |

---

## Phased Implementation Plan

### PHASE 0 — Safe Cleanup (Before Any Refactor)
**Duration:** 30 minutes  
**Risk:** None  
**Commit checkpoint:** Yes

Tasks:
1. Delete `ws-client.js` (will be replaced; overlays still work via one-time fetch)
2. Delete root-level `comp_athletes.html`, `comp_home.html`, `comp_run.html` (stale duplicates)
3. Delete `templates/comp_entry.html` (unimplemented stub with dead routes)
4. Add `.gitignore` entries: `.DS_Store`, `venv/`, `*.pyc`, `__pycache__/`, `backups/`, `.env`, `state.json`, `comp.db`
5. Delete `.DS_Store` files
6. Create `scripts/`, `tests/`, `docs/` directories
7. Move simulation scripts to `scripts/`
8. Move test files to `tests/`
9. Move docs to `docs/`

Verification: Run the app; all routes respond normally. No JS console errors.

---

### PHASE 1 — Fix Gunicorn Initialization Bug (TD-C1)
**Duration:** 1 hour  
**Risk:** Low — additive only  
**Commit checkpoint:** Yes  
**Priority:** CRITICAL — fix before any other work

The entire `if __name__ == "__main__":` block must be split:
- Initialization code → runs at module import time (Gunicorn-compatible)
- Dev server startup → stays in `__main__` block

**Specific changes to `server.py`:**

```python
# NEW: Module-level initialization — runs under both python server.py AND gunicorn
def _startup():
    os.makedirs(TPL_DIR, exist_ok=True)
    ensure_schema()
    _init_state_file()    # extracted from __main__
    _restore_category_order()
    try:
        sync_comp_to_broadcast()
    except Exception:
        pass

# Call at module level (outside any if block)
_startup()
```

Verification: `gunicorn server:app` starts cleanly; DB schema present; category order restored from state.json; `/comp/events` shows correct categories without running `python server.py` first.

---

### PHASE 2 — SSE Migration (Replace All Polling)
**Duration:** 2–3 hours  
**Risk:** Low  
**Commit checkpoint:** Yes per sub-task  
**Priority:** HIGH — OBS overlays are stale without this

#### 2a: Create shared `sse-client.js`
New file served from root. Provides:
```javascript
// Connects to /stream, auto-reconnects, dispatches to window.onStateUpdate
// Drop-in replacement for ws-client.js
```

#### 2b: Migrate all 8 ws-client.js overlay files
Replace `<script src="ws-client.js"></script>` with `<script src="sse-client.js"></script>` in:
- `scorebug.html`, `leaderboard.html`, `lowerthird.html`, `reps.html`
- `champion.html`, `h2h.html`, `lineup.html`, `manual_lowerthird.html`

The `window.onStateUpdate` pattern is already in place — just swap the transport.

#### 2c: Migrate control.html to SSE
Replace:
- `setInterval(checkConnection, 3000)` → SSE `onerror` / `onopen` handlers
- `setInterval(syncFromCompEngine, 5000)` → SSE `onmessage` handler
- Keep 30s API refresh intervals — they serve a different purpose (external API mode)

#### 2d: Migrate home.html and results.html to SSE

#### 2e: Replace comp_callroom.html page reload with SSE DOM updates

#### 2f: Replace comp_heats.html page reload with SSE DOM updates

#### 2g: (Optional) Migrate judge.html / judge-master.html to SSE-only
Currently SSE-primary with 2s fallback poll. The fallback is reasonable insurance for mobile/proxy environments. Keep the fallback pattern but document it.

Verification after each sub-task: Open browser. Change state via comp admin. Overlay updates within 1s. No polling activity visible in browser DevTools Network tab (except 30s keepalives).

---

### PHASE 3 — Modularize Backend
**Duration:** 4–6 hours  
**Risk:** Medium — all imports and routes must be rewired  
**Commit checkpoint:** Yes per module  
**Priority:** HIGH — required before Phase 4, 5, 6 can be done cleanly

Target structure:
```
app/
├── __init__.py          ← app factory: create_app()
├── config.py            ← Config class, env vars
├── extensions.py        ← shared objects (state_lock, sse_condition)
├── database/
│   ├── __init__.py
│   ├── connection.py    ← db(), ensure_schema(), request-scoped connection
│   └── migrations.py    ← migration runner
├── services/
│   ├── __init__.py
│   ├── state.py         ← load_state, save_state, merge_state
│   ├── scoring.py       ← _compute_event_points_core, calculate_event_points
│   ├── leaderboard.py   ← get_leaderboard, get_leaderboard_detailed
│   ├── heat_service.py  ← generate_heats, regenerate_remaining_heats
│   ├── broadcast.py     ← sync_comp_to_broadcast, get_broadcast_mode
│   └── backup.py        ← create_backup
├── realtime/
│   ├── __init__.py
│   ├── sse.py           ← SSE endpoint, _sse_condition, _sse_notify
│   └── stream.py        ← /stream route
├── routes/
│   ├── __init__.py
│   ├── comp.py          ← /comp/* view routes
│   ├── actions.py       ← /comp/action/* routes
│   ├── api.py           ← /comp/api/* routes
│   ├── overlays.py      ← /, /state.json, /update, overlay static
│   ├── judges.py        ← /judge/* routes
│   ├── results.py       ← /results/* routes
│   └── adapters.py      ← /api/adapters/* routes
├── auth.py              ← _check_auth, before_request hook
└── utils.py             ← _safe_int, _canonical_category, balanced_heats
```

Root level:
```
server.py         ← one-liner: from app import create_app; app = create_app()
```

**Migration strategy:** Extract module by module, starting with the deepest dependencies:
1. `database/connection.py` first (no dependencies)
2. `services/state.py` (depends on connection)
3. `services/scoring.py` (depends on state, connection)
4. etc.

At each step: run the app, hit every affected route, verify no regression.

---

### PHASE 4 — Weight & Reps Scoring
**Duration:** 2 hours  
**Risk:** Low — additive schema change + new scoring preset  
**Commit checkpoint:** Yes

#### 4a: Schema (additive — backward compatible)
No new columns needed. The existing `raw_value` (weight) and `tiebreak` (reps) columns are sufficient. The scoring semantics just need a new preset.

#### 4b: Add scoring preset
```python
SCORING_PRESETS["weight_reps"] = {
    "primary_metric":    "weight",
    "secondary_metric":  "reps",
    "primary_direction": "higher",
    "secondary_direction": "higher",   # more reps wins tiebreak
}

EVENT_TYPES["weight_reps"] = {
    "label": "Weight & Reps",
    "unit":  "kg",
    "higher": True,
    "primary_metric": "weight",
    "secondary_metric": "reps",
    "primary_direction": "higher",
    "secondary_direction": "higher",
}
```

#### 4c: Update `_event_type_from_scoring()` to return `"weight_reps"` for this combination

#### 4d: Update scoring engine sort key
The `sort_key` in `_compute_event_points_core` already handles secondary metrics with `secondary_higher`. When `secondary_direction == "higher"`, reps are ranked descending (more reps wins). This should already work — verify with tests.

#### 4e: Update Run page UI
When `event_metric == "weight"` and `event_secondary_metric == "reps"`:
- Show weight input (kg) and reps input (integer) side by side
- Label: "140 kg × 6 reps" format for display

#### 4f: Update judge panel
Judge panel needs to handle `primary_metric == "weight"` + `secondary_metric == "reps"`:
- `primaryValue` = weight in kg
- `secondaryValue` = reps

#### 4g: Update broadcast overlays
Format: `"140kg × 6"` when secondary_metric is reps.

#### 4h: Update simulate_results for weight_reps events

Verification: Set an event to Weight & Reps. Enter scores for multiple athletes with varied weight/reps combinations. Verify ranking order: higher weight first, then higher reps. Verify points assignment correct. Verify leaderboard displays correctly.

---

### PHASE 5 — Leaderboard Expansion
**Duration:** 3 hours  
**Risk:** Low — template and service changes only  
**Commit checkpoint:** Yes

#### 5a: Enhance comp_leaderboard.html template
The server already computes `detailed_rows` and `events_list` but the template ignores them. Expand to show:

```
Rank | Athlete | Cat | Ev1 Raw | Ev1 Pos | Ev1 Pts | ... | Total Pts
```

Features:
- Category toggle (already works via URL param)
- All-categories combined view
- Per-event breakdown columns
- Rank position with medal styling for top 3
- SSE-connected for realtime updates
- Mobile-responsive horizontal scroll

#### 5b: Add SSE subscription to comp_leaderboard.html
Currently a static server-rendered page. Add an SSE client that calls the leaderboard API and re-renders when state changes.

#### 5c: Add public leaderboard endpoint with full detail
`GET /comp/api/leaderboard/detailed?category=<cat>` — returns the full `get_leaderboard_detailed()` output as JSON for the frontend to render.

#### 5d: Add OBS leaderboard overlay improvements to leaderboard.html
Currently only shows name + score. Add category-filtered view option, event progress info.

---

### PHASE 6 — Database Layer Cleanup
**Duration:** 2 hours  
**Risk:** Medium  
**Commit checkpoint:** Yes

#### 6a: Request-scoped DB connection
Use Flask's `g` object:
```python
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_FILE, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA synchronous=NORMAL")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()
```

This means one connection per request instead of N connections.

#### 6b: Consolidate duplicate result-save handlers
Merge `action_save_results` and `action_save_results_run` into a shared `_save_heat_results()` function.

#### 6c: Fix bare `except: pass` in migrations
Replace with `except sqlite3.OperationalError: pass` (only catch "column already exists").

#### 6d: Add a proper migration framework
Simple version table + ordered migration list:
```python
MIGRATIONS = [
    ("0001_initial", "CREATE TABLE IF NOT EXISTS ..."),
    ("0002_add_event_type", "ALTER TABLE events ADD COLUMN event_type ..."),
    ...
]
```

---

### PHASE 7 — Frontend Cleanup
**Duration:** 2–3 hours  
**Risk:** Low  
**Commit checkpoint:** Yes

#### 7a: Create static/ directory
Move shared assets:
- `logo.png` → `static/logo.png`
- Create `static/css/comp.css` — extract repeated CSS variables and shared styles
- Create `static/js/sse-client.js` — (from Phase 2)

#### 7b: Extract shared nav/topbar CSS across templates
All templates redefine `.topbar`, `.section`, `.btn`, etc. with nearly identical CSS. Extract to a shared CSS file or a Jinja2 base template.

#### 7c: Create base template (`templates/base.html`)
Shared: `<head>`, fonts, CSS variables, nav bar, footer scripts.
All `comp_*.html` templates extend it.

#### 7d: Remove duplicate/redundant code
- Remove inline styles where CSS classes cover it
- Remove unused JS functions (grep for unreferenced function definitions)

---

### PHASE 8 — Performance Optimization
**Duration:** 2 hours  
**Risk:** Medium  
**Commit checkpoint:** Yes

#### 8a: Cache leaderboard in memory
Add an in-memory leaderboard cache invalidated on result changes:
```python
_leaderboard_cache = {}  # {category: (timestamp, data)}
_leaderboard_cache_lock = threading.Lock()
CACHE_TTL = 5  # seconds
```

#### 8b: Remove get_public_results() from SSE hot path
Instead of computing all leaderboards on every SSE push, compute lazily and serve from cache.

#### 8c: Optimize sync_comp_to_broadcast
Currently opens multiple DB connections. After Phase 6, uses request-scoped connection — this is handled.

#### 8d: Reduce SSE payload size
Currently sends the full state (600+ lines of JSON) on every change. Consider:
- Sending only changed keys (delta updates) OR
- Sending a version number + separate `/state.json` fetch if version changes

---

### PHASE 9 — Production Hardening
**Duration:** 2 hours  
**Risk:** Low  
**Commit checkpoint:** Yes

Tasks:
1. Add `GET /health` endpoint → `{"status":"ok","db":"ok","version":"1.0.0"}`
2. Add `GET /version` endpoint
3. Add structured `logging` throughout — replace all bare `except: pass` with logged errors
4. Add `python-dotenv` support for `.env` file
5. Create `.env.example` with all environment variables documented
6. Update `requirements.txt` with pinned versions
7. Create `Dockerfile`
8. Create `docker-compose.yml`
9. Add crash-safe startup validation

---

### PHASE 10 — Security Improvements
**Duration:** 2 hours  
**Risk:** Medium (CSRF can break forms)  
**Commit checkpoint:** Yes

Tasks:
1. Add CSRF protection (Flask-WTF or minimal token implementation)
2. Fix Basic Auth to use username + password (or migrate to token-based auth)
3. Document auth model for judge endpoints
4. Add rate limiting on `/update` (flask-limiter: 60 req/min per IP)
5. Add input validation middleware for API endpoints
6. Review CORS — tighten from `*` to specific origins for `/comp/*`
7. Add `Referrer-Policy` and `X-Frame-Options` headers for admin routes

---

### PHASE 11 — Deployment Stack
**Duration:** 1 hour  
**Risk:** Low  
**Commit checkpoint:** Yes

Tasks:
1. Finalize `Dockerfile` and `docker-compose.yml`
2. Update `deploy.sh` with new structure
3. Write `docs/DEPLOYMENT.md` — step-by-step Hetzner setup
4. Write `docs/OPERATIONS.md` — restart, update, backup, restore procedures
5. Update `nginx.conf` with any Phase 9/10 changes

---

### PHASE 12 — Future Expansion Foundations
**Duration:** 1 hour  
**Risk:** None — structural annotations only  
**Commit checkpoint:** Yes

Tasks:
1. Add `TODO: multi-tenant` comments to auth.py
2. Add DB abstraction layer interface (Protocol/ABC) so PostgreSQL can be swapped in
3. Add `competition_id` column to athletes, events tables (not used yet but schema-ready)
4. Create `app/api/v1/` directory structure for future public API
5. Add `CHANGELOG.md`

---

## Migration Strategy

### Git Branch Strategy

```
main (production-ready)
  └── phase/1-gunicorn-init-fix
  └── phase/2-sse-migration
  └── phase/3-modularize
  └── phase/4-weight-reps-scoring
  └── phase/5-leaderboard-expansion
  └── phase/6-db-layer
  └── phase/7-frontend-cleanup
  └── phase/8-performance
  └── phase/9-production-hardening
  └── phase/10-security
  └── phase/11-deployment
  └── phase/12-foundations
```

Each phase branch:
1. Creates the phase branch from `main`
2. Makes all changes for that phase
3. Runs the app manually to verify no regressions
4. Commits with a descriptive message
5. Merges to main after verification

### Rollback Procedure

Each merge to main is a rollback checkpoint. If a phase introduces a regression:
```bash
git log --oneline -10        # find the last good commit
git revert HEAD              # revert last merge (preferred)
# or
git reset --hard <hash>      # hard reset if no shared state changes
```

`comp.db` and `state.json` are not tracked in git (per `.gitignore`) so they are never affected by rollbacks.

---

## Execution Order (Accounting for User Priority Message)

Based on priority guidance received:

| Order | Phase | Issue Addressed |
|-------|-------|-----------------|
| 1st | Phase 0 (Cleanup) | Dead files, .gitignore |
| 2nd | Phase 1 (Gunicorn Init Fix) | TD-C1 — critical production bug |
| 3rd | Phase 2 (SSE Migration) | TD-M4, TD-M3, TD-M5, TD-M6 — centralized realtime |
| 4th | Phase 3 (Modularize) | TD-M1 — monolith, enables everything else |
| 5th | Phase 4 (Weight & Reps) | New scoring type (can be done in parallel with 3) |
| 6th | Phase 5 (Leaderboard) | Detailed leaderboard, realtime |
| 7th | Phase 6 (DB Layer) | TD-H4, TD-M7, TD-L6 — connection management |
| 8th | Phase 8 (Performance) | TD-C3 — SSE cost, leaderboard caching |
| 9th | Phase 7 (Frontend) | TD-M4, shared CSS/JS |
| 10th | Phase 9 (Hardening) | TD-L1–L4 — logging, health, Docker |
| 11th | Phase 10 (Security) | TD-H1 — CSRF, rate limiting |
| 12th | Phase 11 (Deployment) | Hetzner docs |
| 13th | Phase 12 (Foundations) | Future-proofing |
