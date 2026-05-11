# Architecture Audit — Strongman Competition OS
**Date:** 2026-05-11  
**Scope:** Full codebase — server.py (2,117 lines), 17 broadcast HTML files, 11 Jinja2 templates  
**Status:** Functional prototype; production hardening required before deployment

---

## 1. System Topology

```
                        ┌─────────────────────────────────┐
                        │           Browser Clients         │
                        │                                   │
                  ┌─────┴──────┐  ┌──────────┐  ┌─────────┴──────┐
                  │ Comp Admin  │  │  OBS     │  │ Judge Panels    │
                  │ /comp/*     │  │ Overlays │  │ judge.html      │
                  │ (auth'd)    │  │ *.html   │  │ judge-master.   │
                  └─────┬───── ┘  └────┬─────┘  └──────┬──────── ┘
                        │              │                 │
                  ┌─────▼──────────────▼─────────────────▼──────────┐
                  │               Nginx (SSL, reverse proxy)          │
                  │         strongpass.live — nginx.conf              │
                  └─────────────────────┬────────────────────────────┘
                                        │
                  ┌─────────────────────▼────────────────────────────┐
                  │        Gunicorn — 1 worker, 8 threads (gthread)   │
                  │           gunicorn.conf.py — port 8080            │
                  └─────────────────────┬────────────────────────────┘
                                        │
                  ┌─────────────────────▼────────────────────────────┐
                  │              server.py — Flask monolith            │
                  │                                                    │
                  │  ┌──────────────┐  ┌──────────────────────────┐  │
                  │  │ state_lock   │  │  _sse_condition          │  │
                  │  │ threading.   │  │  threading.Condition     │  │
                  │  │ Lock         │  │  _sse_version: int       │  │
                  │  └──────┬───────┘  └────────────┬─────────────┘  │
                  │         │                        │                │
                  │  ┌──────▼──────┐  ┌─────────────▼────────────┐  │
                  │  │ state.json  │  │  /stream SSE endpoint    │  │
                  │  │ atomic r/w  │  │  full state + leaderboard│  │
                  │  └─────────────┘  └──────────────────────────┘  │
                  │                                                    │
                  │  ┌─────────────────────────────────────────────┐ │
                  │  │  comp.db (SQLite WAL)                        │ │
                  │  │  athletes / events / heats / raw_results /   │ │
                  │  │  competition_state                            │ │
                  │  └─────────────────────────────────────────────┘ │
                  └────────────────────────────────────────────────── ┘
```

---

## 2. Route Inventory (46 routes total)

### Public / Unauthenticated Routes

| Method | Path | Handler | Notes |
|--------|------|---------|-------|
| GET | `/` | `root()` | Serves `home.html` raw (not Jinja2) |
| GET | `/state.json` | `state_json()` | Full state + leaderboard compute |
| POST | `/update` | `update_state()` | Judge panel state push — **no auth** |
| GET | `/stream` | `stream()` | SSE endpoint — full state on change |
| GET | `/proxy` | `proxy()` | External API proxy |
| GET | `/<path:filename>` | `static_files()` | Any root-dir file (safe extensions) |
| GET | `/results` | → redirect `/results/<cat>` | |
| GET | `/results/<category>` | `public_results()` | Public leaderboard page |
| POST | `/judge/set_status` | `judge_set_status()` | DNS/DNF from judge panel — **no auth** |

### Competition Admin Routes (HTTP Basic Auth when COMP_PASSWORD set)

| Method | Path | Handler |
|--------|------|---------|
| GET | `/comp/`, `/comp` | → redirect `/comp/events` |
| GET | `/comp/dashboard` | → redirect `/comp/events` |
| GET | `/comp/events` | `comp_events()` — Setup page |
| GET | `/comp/heats` | `comp_heats()` |
| GET | `/comp/callroom` | `comp_callroom()` |
| GET | `/comp/arena` | `comp_arena()` |
| GET | `/comp/results` | `comp_results()` |
| GET | `/comp/run` | `comp_run()` |
| GET | `/comp/leaderboard` | `comp_leaderboard()` |
| GET | `/comp/athletes` | `comp_athletes()` |
| POST | `/comp/action/next_heat` | `action_next_heat()` |
| POST | `/comp/action/prev_heat` | `action_prev_heat()` |
| GET,POST | `/comp/action/set_heat` | `action_set_heat()` |
| POST | `/comp/action/backup` | `action_backup()` |
| POST | `/comp/action/set_lanes` | `action_set_lanes()` |
| POST | `/comp/action/save_results` | `action_save_results()` |
| POST | `/comp/action/save_results_run` | `action_save_results_run()` |
| POST | `/comp/action/update_event_type` | `action_update_event_type()` |
| POST | `/comp/action/update_event_scoring` | `action_update_event_scoring()` |
| POST | `/comp/action/generate_heats` | `action_generate_heats()` |
| POST | `/comp/action/generate_test` | `action_generate_test()` |
| POST | `/comp/action/add_athlete` | `action_add_athlete()` |
| POST | `/comp/action/delete_athlete` | `action_delete_athlete()` |
| POST | `/comp/action/withdraw_athlete` | `action_withdraw_athlete()` |
| POST | `/comp/action/reinstate_athlete` | `action_reinstate_athlete()` |
| POST | `/comp/action/add_event` | `action_add_event()` |
| POST | `/comp/action/delete_event` | `action_delete_event()` |
| POST | `/comp/action/edit_event` | `action_edit_event()` |
| POST | `/comp/action/add_category` | `action_add_category()` |
| POST | `/comp/action/reorder_category` | `action_reorder_category()` |
| POST | `/comp/action/delete_category` | `action_delete_category()` |
| POST | `/comp/action/jump_to` | `action_jump_to()` |
| POST | `/comp/action/clear_all` | `action_clear_all()` |
| POST | `/comp/action/simulate_results` | `action_simulate_results()` |
| POST | `/comp/action/push_to_broadcast` | `action_push_to_broadcast()` |
| POST | `/comp/action/next_event` | `action_next_event()` |
| POST | `/comp/action/set_broadcast_mode` | `action_set_broadcast_mode()` |
| POST | `/comp/action/set_result_type` | `action_set_result_type()` |
| GET | `/comp/api/broadcast_mode` | `api_broadcast_mode()` |
| GET | `/comp/api/state` | `api_comp_state()` |
| GET | `/comp/api/heat` | `api_current_heat()` |
| GET | `/comp/api/leaderboard` | `api_leaderboard()` |
| GET | `/comp/api/event_points` | `api_event_points()` |
| GET | `/api/adapters/strengthresults` | `adapter_strengthresults()` |
| GET | `/api/adapters/ironpodium` | `adapter_ironpodium()` |
| GET | `/api/adapters/custom` | `adapter_custom()` |

### Dead Routes (template exists, no backend route)

| Template | Expected Route | Expected Action | Status |
|----------|---------------|-----------------|--------|
| `comp_entry.html` | `GET /comp/entry` | Display results entry | **Route missing** |
| `comp_entry.html` | `POST /comp/action/save_entry` | Save entry results | **Route missing** |
| `comp_dashboard.html` | `GET /comp/dashboard` | Dashboard | Redirects to /comp/events |

---

## 3. Data Layer

### SQLite Schema (comp.db)

```sql
athletes (
  id INTEGER PRIMARY KEY,
  name TEXT,
  category TEXT,
  status TEXT DEFAULT 'active'   -- 'active' | 'withdrawn' | 'injury' | etc.
)

events (
  id INTEGER PRIMARY KEY,
  name TEXT,
  event_number INTEGER,
  event_type TEXT DEFAULT 'reps', -- 'reps'|'weight'|'distance'|'time'|'object'
  primary_metric TEXT,            -- 'reps'|'weight'|'distance'|'time'|'objects'
  secondary_metric TEXT,          -- 'time' for tiebreak, or NULL
  primary_direction TEXT,         -- 'higher'|'lower'
  secondary_direction TEXT        -- 'higher'|'lower'|NULL
)

heats (
  id INTEGER PRIMARY KEY,
  event_id INTEGER,
  category TEXT,
  heat_number INTEGER,
  lane INTEGER,
  athlete_name TEXT
)

raw_results (
  id INTEGER PRIMARY KEY,
  athlete_id INTEGER,
  event_id INTEGER,
  raw_value REAL,
  tiebreak REAL,
  result_type TEXT DEFAULT 'score', -- 'score'|'dns'|'dnf'
  UNIQUE(athlete_id, event_id)
)

competition_state (
  id INTEGER PRIMARY KEY,
  category TEXT,
  event INTEGER,
  heat INTEGER
)
```

### state.json — Dual-Purpose Blob

`state.json` is used for two entirely different concerns in the same file:

**Broadcast / Overlay state:**
- `lanes[]`, `laneCount`, `athletes[]`, `categories[]`
- `eventName`, `eventNum`, `eventSub`, `compName`, `compSub`
- `scorebug`, `leaderboard`, `lowerThirdVisible`, `eventBarVisible`
- `champion`, `h2h`, `lineup`, `repsVisible`, `lightsVisible`
- `judgeL1`…`judgeL8` (timer, reps, lights per lane)
- `manualVisible`, `manualLine1`, `manualLine2`, `manualScore`
- `champName`, `champDetail`, `champScore`, etc.
- `h2hLeft{}`, `h2hRight{}`
- `lineupIndex`, `lineupAuto`, `lineupDuration`, `lbCategory`

**Competition engine config:**
- `competition_config{}` — lanes, scoring_mode, heat_order_mode
- `competition_status` — setup|athletes|heats|live|finished
- `category_order[]` — persisted category list
- `category_start_counts{}` — athlete counts per category
- `compCategory`, `compEvent`, `compHeat`
- `data_source_mode` — engine|api|manual
- `competition{}` — snapshot block
- `broadcast{}` — broadcast snapshot block

---

## 4. Service Inventory (all within server.py)

| Service | Lines | Responsibility |
|---------|-------|---------------|
| SSE infrastructure | 47–56 | Condition variable, version counter, notify |
| State R/W | 58–77 | load_state, save_state (atomic), merge_state |
| Competition config | 79–88 | comp_config, lane_count |
| Competition status | 89–116 | workflow stage tracking |
| Category management | 118–215 | persist/restore order, start counts |
| DB connection | 217–272 | db(), ensure_schema(), WAL setup, migrations |
| Heat balancing | 274–292 | balanced_heats() |
| Competition state | 294–312 | get/set comp_state (DB) |
| Scoring engine | 314–449 | _compute_event_points_core, calculate_event_points |
| Leaderboard | 487–617 | get_leaderboard, get_leaderboard_detailed, public |
| Heat generation | 620–778 | generate_heats, regenerate_remaining_heats |
| Broadcast sync | 785–880 | sync_comp_to_broadcast() |
| Auth | 133–157 | HTTP Basic Auth, before_request hook |
| Backup | 1339–1357 | create_backup(), timestamped copies |
| External adapters | 1888–1968 | StrengthResults, IronPodium, custom API |
| All route handlers | 882–2068 | 1,186 lines of routes |

---

## 5. Realtime Architecture

### SSE Implementation

- Global `threading.Condition` (`_sse_condition`) + version counter (`_sse_version`)
- Any state change → `_sse_notify()` → increments version, `notify_all()`
- Each connected SSE client runs `generate()` in a thread, blocks on `wait_for(version_changed, timeout=30)`
- On wake: loads full state.json + calls `get_public_results()` (full leaderboard all categories)
- On timeout: sends SSE keepalive comment

### SSE Cost Per Update

Every state change (including a single judge button tap) causes:
1. `sync_comp_to_broadcast()` — reads DB, computes leaderboard for current category
2. `_sse_notify()` — wakes ALL connected clients
3. Per connected client: `load_state()` + `get_public_results()` = leaderboard for **all 6 categories**

With 10 SSE clients and 6 categories × 20 athletes each, a single tap generates ~60 leaderboard rows computed 10 times = 600 point calculations triggered per judge action. This scales poorly under heavy concurrent judging.

### Current Connectivity by File

| File | SSE | Polling | Verdict |
|------|-----|---------|---------|
| `judge.html` | ✓ primary | 2s fallback | Acceptable |
| `judge-master.html` | ✓ primary | 2s fallback | Acceptable |
| `templates/comp_results_public.html` | ✓ only | — | Correct |
| `control.html` | ✗ none | 3s + 5s + 30s + 30s | Needs SSE |
| `home.html` | ✗ none | 5s | Needs SSE |
| `results.html` | ✗ none | 15s | Needs SSE |
| `debug.html` | ✗ none | 2s | Dev tool |
| `scorebug.html` | via ws-client | one-time + 500ms poll | Replace ws-client |
| `leaderboard.html` | via ws-client | one-time + 500ms poll | Replace ws-client |
| `lowerthird.html` | via ws-client | one-time + 500ms poll | Replace ws-client |
| `reps.html` | via ws-client | one-time + 500ms poll | Replace ws-client |
| `champion.html` | via ws-client | one-time + 500ms poll | Replace ws-client |
| `h2h.html` | via ws-client | one-time + 500ms poll | Replace ws-client |
| `lineup.html` | via ws-client | one-time + 500ms poll | Replace ws-client |
| `manual_lowerthird.html` | via ws-client | one-time + 500ms poll | Replace ws-client |
| `templates/comp_callroom.html` | ✗ none | 5s full reload | Disruptive |
| `templates/comp_heats.html` | ✗ none | 10s full reload | Disruptive |

**Note on `ws-client.js`:** This 24-line file polls `/state.json` every 500ms and calls `window.onStateUpdate`. It is used by 8 overlay files. It predates the SSE implementation and should be replaced with SSE. With 8 overlay browser windows open, this generates 960 HTTP requests per minute against state.json.

---

## 6. Critical Production Bug — Gunicorn Bypass of Initialization

The `if __name__ == "__main__":` block at lines 2071–2117 contains all startup initialization:
- `ensure_schema()` — creates tables, runs migrations
- State file initialization — creates default state.json
- Template migration — moves root HTML files to templates/
- `_restore_category_order()` — restores persisted categories
- `sync_comp_to_broadcast()` — initial broadcast sync

**Gunicorn imports `server.py` as a module — this block never executes.**

Consequences:
1. DB schema setup never runs under Gunicorn unless tables already exist
2. `CATEGORY_ORDER` is always the compiled default on startup; user-configured categories are never restored
3. If state.json is missing or corrupt, no recovery occurs
4. Startup sync never runs

**Fix required:** Move all initialization into an application factory function or `@app.before_first_request` equivalent, called unconditionally at module load time.

---

## 7. Authentication Model

```
/comp/*              → HTTP Basic Auth (COMP_PASSWORD env var)
/update              → No auth (judge panels need access without credentials)
/stream              → No auth (overlays need access)
/judge/set_status    → No auth (judge panels)
/api/adapters/*      → No auth
/state.json          → No auth (read-only broadcast data)
```

Auth checks only the password field (no username). A blank `COMP_PASSWORD` disables auth entirely.

---

## 8. Scoring System

### Supported Event Types

| Key | Label | Unit | Winner | Tiebreaker |
|-----|-------|------|--------|------------|
| `reps` | Reps | reps | higher | — |
| `weight` | Weight | kg | higher | — |
| `distance` | Distance | m | higher | — |
| `time` | Time | s | lower | — |
| `object` | Objects | objs | higher | time (lower) |

### Scoring Presets

`max_weight`, `reps`, `distance`, `time`, `objects_time`, `distance_time`

### Points Calculation

Fixed scale: rank 1 = `total_athletes` points, rank 2 = `total_athletes - 1`, etc. Tied athletes split points equally. Zero scores or DNS receive 0 points.

### Missing: Weight & Reps

Phase 4 requires a `weight_reps` scoring type where:
- Primary: weight (higher wins)
- Secondary: reps (higher wins, as tiebreaker)
- Display: "140kg × 6 reps"

---

## 9. Deployment Stack

| Component | File | Notes |
|-----------|------|-------|
| Gunicorn | `gunicorn.conf.py` | 1 worker, 8 threads, gthread, 3600s timeout |
| Nginx | `nginx.conf` | SSL, SSE unbuffered, 1h read timeout on /stream |
| Systemd | `strongman.service` | Auto-restart, COMP_PASSWORD env var |
| SSL | Let's Encrypt | Manual certbot setup required |
| Deploy | `deploy.sh` | First-time Hetzner Ubuntu setup script |
