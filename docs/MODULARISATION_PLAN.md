# Phase 3: Modularisation Plan

## 1. Dependency Analysis

### Current file structure
```
server.py (2175 lines, single module)
  ├── Constants / config       lines 12–43
  ├── Flask app + globals      line 18, 20–44, 45–56
  ├── State I/O                lines 58–131
  ├── Auth helpers             lines 133–166
  ├── DB helpers               lines 217–272
  ├── Pure utilities           lines 274–293
  ├── Comp state accessors     lines 294–313
  ├── Scoring engine           lines 314–534
  ├── Heat generation          lines 594–880
  ├── SSE /stream endpoint     lines 882–909
  └── Route handlers (57)      lines 910–2069
  └── Startup                  lines 2070–2175
```

### Dependency graph (arrows = "imports / uses")

```
server.py globals
  ├── DIR, TPL_DIR, STATE_FILE, DB_FILE, BACKUPS_DIR  ← no deps (computed from __file__)
  ├── LANES, CAT_COLORS, EVENT_TYPES, SCORING_PRESETS ← no deps (pure literals)
  ├── CATEGORY_ORDER                                   ← no deps BUT mutated at runtime
  ├── state_lock                                       ← threading, no other deps
  ├── _sse_condition, _sse_version, _sse_notify        ← threading, no other deps
  ├── load_state / save_state                          ← STATE_FILE, state_lock, json, os
  ├── merge_state                                      ← load_state, save_state, state_lock, _sse_notify
  ├── db()                                             ← DB_FILE, sqlite3
  ├── ensure_schema()                                  ← db()
  ├── balanced_heats()                                 ← math only — PURE
  ├── _event_direction_flags()                         ← EVENT_TYPES — PURE
  ├── Scoring engine funcs                             ← db(), EVENT_TYPES, CATEGORY_ORDER
  ├── Heat generation                                  ← db(), CATEGORY_ORDER, balanced_heats()
  └── Route handlers                                   ← everything above
```

### Key circular dependency risks

| Risk | Resolution |
|---|---|
| `sse.py` uses `_sse_version` as a mutable int | Import module object, not the name: `import app.sse as _sse_mod`; reference `_sse_mod._sse_version` |
| `state.py` would call `_sse_notify()` from sse.py | One-way: `state` imports `sse`; `sse` does NOT import `state` → no cycle |
| `config.py` `DIR` computed from `__file__` | `os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` (two levels up from `app/config.py`) |

---

## 2. Extraction Order

Safe extraction order maximises independence — each step depends only on things extracted in prior steps:

```
Step 1: app/config.py      ← no deps inside project; pure constants
Step 2: app/sse.py         ← threading only; no project deps
Step 3: app/database.py    ← config.DB_FILE only
Step 4: app/utils.py       ← config.EVENT_TYPES only (pure funcs + balanced_heats)
```

Steps 5+ (deferred — not in scope of Phase 3):
```
Step 5: app/state.py       ← config + sse + state_lock (needs CATEGORY_ORDER decision)
Step 6: app/scoring.py     ← database + config + state
Step 7: app/heats.py       ← database + config + utils + state
Step 8: app/routes/        ← all of the above
```

---

## 3. Safe Module Boundaries

### `app/config.py` — pure constants, no runtime mutation

**Include:**
- `DIR`, `TPL_DIR`, `STATE_FILE`, `DB_FILE`, `BACKUPS_DIR`
- `LANES` (default lane count fallback)
- `CAT_COLORS`
- `EVENT_TYPES`
- `SCORING_PRESETS`
- `DEFAULT_CATEGORY_ORDER` (renamed from the literal default)
- `UPDATE_MAX_BODY` (request size cap from route handler)
- `_SAFE_STATIC_EXTS` (set used in static file handler)
- `_DEFAULT_COMP_CONFIG` (dict used in comp config helpers)

**Exclude (mutable / live state):**
- `CATEGORY_ORDER` — mutated at runtime; stays in server.py until `app/state.py` is created

**Boundary rule:** Nothing in config.py may import from the project. Only stdlib.

---

### `app/sse.py` — SSE signalling primitives

**Include:**
- `_sse_condition` (threading.Condition)
- `_sse_version` (int, starts at 0)
- `_sse_notify()` (increments version, notifies waiters)

**Exclude:**
- The `/stream` route handler — stays in server.py; it calls `_sse_notify` indirectly and uses Flask `Response` which server.py already imports.

**Boundary rule:** Nothing in sse.py may import from the project. Only `threading`.

---

### `app/database.py` — SQLite connection and schema helpers

**Include:**
- `db()` — returns a WAL-mode sqlite3 connection
- `ensure_schema()` — CREATE TABLE + all ALTER TABLE migrations

**Exclude:**
- `get_comp_state`, `set_comp_state`, `get_heat_lanes` — these use db() but also reference CATEGORY_ORDER (comp state fallback); defer to state.py
- `get_category_start_count`, `lock_category_start_counts` — use both db() and state I/O; defer to state.py

**Boundary rule:** Only imports `app.config.DB_FILE` and stdlib (`sqlite3`).

---

### `app/utils.py` — pure computation helpers

**Include:**
- `balanced_heats(athletes, lanes)` — pure, uses only `math`
- `_event_direction_flags(event_row, event_type)` — pure, reads `EVENT_TYPES`
- `_event_type_from_scoring(primary_metric, secondary_metric)` — pure lookup
- `_safe_int(v, default)` — pure cast helper
- `_filter_payload_to_active_lanes(payload, lane_count)` — pure dict filter
- `_default_inactive_judge()` — pure dict factory
- `_protect_lane_stopped_timers(old_payload, new_payload)` — pure merge logic

**Exclude:**
- Any function that calls `db()`, `load_state()`, or references Flask — those have heavier deps

**Boundary rule:** May only import `app.config.EVENT_TYPES` and stdlib (`math`).

---

## 4. Shared State Strategy

### Immutable globals (safe to import by value)

`DIR`, `TPL_DIR`, `STATE_FILE`, `DB_FILE`, `BACKUPS_DIR`, `LANES`, `CAT_COLORS`,
`EVENT_TYPES`, `SCORING_PRESETS`, `DEFAULT_CATEGORY_ORDER` — these never change after startup. Import them by name; `from app.config import STATE_FILE` is safe.

### Mutable globals

| Name | Type | Mutation pattern | Safe import? |
|---|---|---|---|
| `CATEGORY_ORDER` | `list` | `.clear()` + `.extend()` (in-place) | Yes for the list object; `from app.x import CATEGORY_ORDER` keeps the same list reference |
| `_sse_version` | `int` | rebinding via `global _sse_version; _sse_version += 1` | NO — `from app.sse import _sse_version` copies the int at import time and never updates |
| `state_lock` | `threading.Lock` | never rebound, used as context manager | Yes — import once, use freely |

### `_sse_version` access pattern (critical)

```python
# WRONG — copies value at import time:
from app.sse import _sse_version
lambda: _sse_version != seen_version  # always compares against 0

# CORRECT — always reads current module attribute:
import app.sse as _sse_mod
lambda: _sse_mod._sse_version != seen_version
```

The stream generator in server.py must use the module-reference pattern after sse.py is extracted.

### `CATEGORY_ORDER` strategy

Until `app/state.py` exists, `CATEGORY_ORDER` stays in `server.py`. Functions that modify it (`_restore_category_order`, `_persist_category_order`, and the 12 mutation sites across routes) need no changes in Phase 3.

When `app/state.py` is eventually created it will expose `CATEGORY_ORDER` as the canonical list. `server.py` will import it once:
```python
from app.state import CATEGORY_ORDER  # same list object — in-place mutations still work
```

---

## 5. Import Strategy

### Package layout

```
project_root/
├── server.py             ← monolith; imports from app.*
├── app/
│   ├── __init__.py       ← empty package marker
│   ├── config.py
│   ├── sse.py
│   ├── database.py
│   └── utils.py
```

### Import rules in server.py (after each extraction)

```python
# config
from app.config import (
    DIR, TPL_DIR, STATE_FILE, DB_FILE, BACKUPS_DIR,
    LANES, CAT_COLORS, EVENT_TYPES, SCORING_PRESETS,
    DEFAULT_CATEGORY_ORDER, UPDATE_MAX_BODY,
    _SAFE_STATIC_EXTS, _DEFAULT_COMP_CONFIG,
)

# sse — module-ref pattern required for _sse_version
import app.sse as _sse_mod
from app.sse import _sse_notify, _sse_condition

# database
from app.database import db, ensure_schema

# utils
from app.utils import (
    balanced_heats, _event_direction_flags, _safe_int,
    _event_type_from_scoring, _filter_payload_to_active_lanes,
    _default_inactive_judge, _protect_lane_stopped_timers,
)
```

### `app/` alongside `app = Flask(...)` — no conflict

Python resolves `from app.config import ...` via `sys.modules['app']`, not the local `app` binding. The local `app = Flask(...)` variable does not shadow the package because the package is already in `sys.modules` by the time Flask is instantiated.

---

## 6. Transition Plan

Each step: create module → update server.py imports → remove duplicated definitions → test → commit.

### Step 1 — config.py

1. Create `app/__init__.py` (empty)
2. Create `app/config.py` with all constants
3. In `server.py`: add `from app.config import ...`, delete the duplicated constants
4. Smoke test: `python -c "from app.config import DIR, STATE_FILE, EVENT_TYPES; print(DIR)"`
5. Gunicorn boot test: `gunicorn -w1 --threads 8 server:app`
6. Commit: `feat(config): extract pure constants to app/config.py`

### Step 2 — sse.py

1. Create `app/sse.py` with `_sse_condition`, `_sse_version`, `_sse_notify`
2. In `server.py`: `import app.sse as _sse_mod` + `from app.sse import _sse_notify, _sse_condition`
3. Update stream generator: change `lambda: _sse_version != seen_version` → `lambda: _sse_mod._sse_version != seen_version`
4. Delete `_sse_condition`, `_sse_version`, `_sse_notify` from server.py
5. Test: open `/stream` in browser, change state, verify push fires
6. Commit: `feat(sse): extract SSE primitives to app/sse.py`

### Step 3 — database.py

1. Create `app/database.py` with `db()` and `ensure_schema()`
2. In `server.py`: `from app.database import db, ensure_schema`
3. Delete `db()` and `ensure_schema()` from server.py
4. Test: load `/comp/run`, check DB operations work end-to-end
5. Commit: `feat(database): extract db() and ensure_schema() to app/database.py`

### Step 4 — utils.py

1. Create `app/utils.py` with all pure helpers
2. In `server.py`: `from app.utils import balanced_heats, _event_direction_flags, ...`
3. Delete moved functions from server.py
4. Test: generate heats, run a scoring flow, check leaderboard
5. Commit: `feat(utils): extract pure helpers to app/utils.py`

---

## 7. Rollback Strategy

Each extraction is an independent commit. Rollback is per-commit:

```bash
# Revert all four extractions:
git revert HEAD~3 HEAD~2 HEAD~1 HEAD  # or by hash

# Revert only utils (last commit):
git revert HEAD

# Manual rollback of a specific module:
git show HEAD:server.py > server.py   # restore server.py at that point
rm -rf app/                            # remove extracted package
git checkout HEAD -- server.py         # or from git directly
```

Pre-Phase-3 checkpoint: commit `226edb9` (Phase 2 docs). Tag before starting:
```bash
git tag phase3-start
# To restore completely:
git checkout phase3-start -- server.py
rm -rf app/
```

### Safety properties of this refactor

| Property | Guarantee |
|---|---|
| No logic changes | Extractions are copy-then-delete; zero algorithmic changes |
| No interface changes | All public function signatures preserved |
| Incremental | Each module is independently reversible |
| Test surface | Gunicorn boot + key endpoint checks after each step |
| No route handlers touched | Route logic stays in server.py until Phase 4+ |
