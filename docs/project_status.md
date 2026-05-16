# Project Status — StrongPass Competition OS
**Last updated:** 2026-05-16 (Phase 5: color persistence, cached standings, director freeze controls)  
**Status:** Beta — live, protected, pre-production

---

## Deployment

| Component | State | Notes |
|-----------|-------|-------|
| Server | **Live** | strongpass.live, Hetzner Ubuntu 24.04 |
| SSL | **Active** | Let's Encrypt via Certbot |
| Nginx | **Active** | Reverse proxy, SSE unbuffered, HTTP→HTTPS redirect |
| Gunicorn | **Active** | 1 worker, 8 gthread threads, port 8080 |
| Systemd | **Enabled** | `strongpass.service`, auto-restart |
| Beta gate | **Active** | `BETA_TOKEN` set in service file |
| Admin auth | **Active** | `COMP_PASSWORD` set in service file |
| DB | **Operational** | SQLite WAL at `/opt/strongpass/current/comp.db` |

**Service file location (server):** `/etc/systemd/system/strongpass.service`  
**App root (server):** `/opt/strongpass/current/`  
**Venv (server):** `/opt/strongpass/venv/`  
**Logs (server):** `/opt/strongpass/logs/` (access.log, error.log, service.log)

---

## Codebase State

| Layer | Status |
|-------|--------|
| `server.py` | ~2,070 lines — monolith with app/ helpers extracted |
| `app/config.py` | Done — all constants; weight_reps event type added |
| `app/sse.py` | Done — SSE condition + notify |
| `app/database.py` | Done — `db()` + `ensure_schema()` |
| `app/utils.py` | Done — pure helpers |
| `app/beta_auth.py` | Done — sitewide beta gate |
| `templates/` | 10 Jinja2 comp templates |
| Root HTML files | 14 broadcast/overlay HTML files (raw-served) |

**server.py line count trend:**  
2,175 (pre-Phase 3) → 2,036 (post-Phase 3 extraction) → ~2,130 (Phase 5: color helpers + cached standings)

---

## What Has Been Completed

### Phase 1 — Gunicorn Initialization Fix
**Commit:** `1f69015`  
All startup code (schema, state init, category restore, broadcast sync) moved to `_startup()` called at module level. Runs under both `python server.py` and Gunicorn.

### Phase 2 — SSE Migration
**Commits:** `4fa344d`, `f624a8e`, `23e1bc6`, `88c48f3`, `226edb9`  
- Created `sse-client.js` (EventSource-based, exponential backoff reconnect)
- 8 overlay files migrated from `ws-client.js` 500ms polling to SSE
- `control.html`, `home.html`, `results.html` migrated to SSE
- `comp_callroom.html`, `comp_arena.html` migrated from 5s page reload to SSE-triggered reload
- Polling reduction: **1,008 → 12 req/min** (98.8%)

### Phase 3 — Partial Modularisation
**Commits:** `4a1a04a`, `152074f`  
Extracted `app/config.py`, `app/sse.py`, `app/database.py`, `app/utils.py`.

### Phase 4 — Performance Optimization
**Commits:** `074b7bd`, `cc59b30`  
- Shared results cache (`_results_cache`, `_results_dirty`) eliminates redundant leaderboard recomputes
- Change detection in `_apply_judge_scores_to_raw_results`: skips DB write + cache invalidation when score unchanged
- `load_state()` call count reduced in hot path
- Before: ~551 DB queries/tick (8 SSE clients, 6 cats, 5 events). After: ~20 queries/tick (no score change), ~85 (score change)

### Phase 5 (scoring) — Weight+Reps Event Type
**Commit:** `d8e0975`  
Added `weight_reps` as a first-class scoring type (heavier weight wins, more reps as tiebreak). Changes: `app/config.py` (EVENT_TYPES, SCORING_PRESETS), `app/utils.py` (_event_type_from_scoring), `judge.html` (block-weight-reps with weight input + rep counter), `comp_home.html` (preset dropdown), `comp_run.html` (secondary input step fix for reps).

### Phase 5 (mobile) — Responsive Redesign
**Commits:** `5ce14d4`, `b12c759`  
`comp_results_public.html`: replaced `display:none` on mobile with horizontal scroll + sticky min-width (all event data now visible on phones). All 7 other comp templates: added `@media(max-width:600px)` breakpoints. `comp_callroom.html`: `@media(max-width:768px)` single-column stacked layout.

### Phase 5 (leaderboard) — Architecture Redesign
**Commit:** `ab093ec`  
`comp_results_public.html`: SSE-driven DOM re-render via `/api/results/<cat>` replaces `location.reload()` — no flicker for spectators. `comp_leaderboard.html`: full per-event breakdown table (was name+total only, fixes M5), SSE live updates. `comp_heats.html`: 10s polling replaced with SSE-triggered reload. New public endpoint `/api/results/<category>` and updated `/comp/api/leaderboard` (version + optional detailed breakdown).

### Phase 1 Stabilization — Real-Time Reliability
**Commit:** `a96ea86` and related  
- Eliminated all remaining `location.reload()` and polling patterns
- `comp_arena.html`: full DOM-patch via SSE (OBS-safe, zero blank frames)
- `comp_callroom.html`: removed 30s periodic reload, added proper SSE reconnect
- `comp_run.html`: added SSE listener, reloads on heat/event change only
- `comp_heats.html`: fixed null-coercion infinite reload loop (null-seed pattern)
- `comp_leaderboard.html`, `comp_results_public.html`, `results.html`: null-seed + restart_token patterns
- `control.html`: replaced 5s polling with SSE-debounced sync (80ms debounce); fixed category color mapping
- Added `_RESTART_TOKEN` (epoch at startup) injected into all SSE payloads + `/state.json`; clients detect restart and reset version counters
- Deleted `ws-client.js` (zero remaining imports confirmed)

### Phase 2 Optimization — SSE Efficiency + Control Stability
**Commit:** Phase 2 (2026-05-16)  
- **SSE payload cache**: `_get_sse_payload(version)` — payload built once per `_sse_version`, shared across all connected clients. Eliminates N disk reads + N `json.dumps()` per event when N clients connected.
- **control.html hash-based dirty checking**: `_syncHashes` tracks lanes, athletes, event, champ fingerprints. `renderLaneConfig()`, `renderAthleteTable()`, `renderH2HCategorySelects()`, `initInputs()` only called when their underlying data changes. Full DOM rebuild no longer fires on every timer tick (~1/s during competition).
- **`/health` endpoint**: `GET /health` → `{"status":"ok","db":"ok","restart_token":N}` — performs live SQLite `SELECT 1`; for uptime monitors and load balancers.
- **H3 set_lanes logging**: `except Exception: pass` → `app.logger.error(...)` — heat regeneration failures now visible in Gunicorn logs.

### Phase 5 — Broadcast Director Controls + Performance
**Commits:** `f45fb42`, `963f9c5`, `2711492` (2026-05-16)

**Category Color Persistence:**
- `state.json["category_colors"]` = canonical `{name: hex}` store
- `_load_category_colors()` / `_set_category_color()` helpers
- `action_add_category()` saves color; new `/comp/action/set_category_color` endpoint
- All color consumers (sync, templates, overlays) read from canonical store
- `comp_home.html` dot replaced with inline `<input type="color">` picker (auto-submits)

**Multi-Category Cached Standings:**
- `_get_cached_standings()` derives leaderboard from shared `_results_cache` — no per-tick DB queries
- `_build_lb_standings()` pre-computes all categories simultaneously
- `lbStandings` dict pre-stored in state.json; stripped from SSE; available via `/state.json`
- `/director/lb` uses cache lookup — instant response, no DB query

**Director Freeze + Display Controls:**
- `lbFrozen` — overlay holds snapshot while scoring continues; FROZEN badge in footer
- `lbDisplayCount` — 5/8/10/15 rows selector in control.html
- Mini preview panel — top 5 lbAthletes visible to director without opening overlay
- All director state syncs across concurrent operators via SSE

---

### Category Color Persistence (detail)
**Commit:** `f45fb42` (2026-05-16)

Category colors were previously lost on restart and ignored on new category creation. Fix introduces `state.json["category_colors"]` as the canonical `{name: hex}` store.

**Changes:**
- `_load_category_colors(s=None)` helper reads from `category_colors`, falls back to legacy `categories` array, then `CAT_COLORS` config
- `_set_category_color(name, color)` writes to `category_colors` dict atomically
- `action_add_category()` now reads the `color` form field and saves it
- New `/comp/action/set_category_color` POST endpoint — updates color and triggers SSE
- `sync_comp_to_broadcast()` uses `_load_category_colors()` instead of hardcoded `CAT_COLORS`
- comp_home route uses `_load_category_colors()` for template rendering
- `comp_home.html`: static colored dot replaced with inline `<input type="color">` form; `onchange` submits immediately — no extra button

Colors now survive: server restarts, SSE syncs, category reorder, new category creation, clear_all (category_colors persists separately from athletes/events).

---

### Phase 4 — Broadcast Director Layer (Architecture Refactor)
**Commit:** `641aafb` (2026-05-16)

**Core architectural separation — Competition Engine vs Broadcast Director:**

The leaderboard overlay was tightly coupled to the scoring operator's active category. This is resolved by introducing a director override layer on top of the competition engine feed.

**Key changes:**
- `lbAthletes` — new state field: the pre-resolved leaderboard athletes for the director-selected category. Computed server-side by `sync_comp_to_broadcast()` and `/director/lb`. Falls back to `athletes` (current scoring category) when no override is active.
- `lbCategoryOverride` — boolean flag. When `true`, `lbAthletes`/`lbCategory` are not overwritten by competition engine syncs.
- `/director/lb` POST endpoint — director sets category → server fetches that category's leaderboard immediately → SSE → overlay updates in <100ms. Protected by comp session auth.
- `control.html` — AUTO/MANUAL badge (green/gold) next to leaderboard dropdown. "↺ Auto" reset button. `syncFromCompEngine()` guards `lbCategory` update with `!state.lbCategoryOverride`.
- `leaderboard.html` — uses `lbAthletes || athletes`. Shows DIRECTOR badge when override active. Hash `_lastLbKey` uses `lbAthletes`.

**Architecture documented:** `docs/ARCHITECTURE_BROADCAST.md` — full state schema, data flow diagrams, AUTO vs MANUAL overlay modes, protected route table, performance architecture.

### Phase 3 — Operational Reliability, Overlay Stability, Auth
**Commit:** Phase 3 (2026-05-16)

**P1 — Overlay flicker/animation storm (all 6 OBS overlays):**  
Root cause: every overlay was doing full innerHTML rebuilds or running animations on every SSE event (~1/s timer tick), regardless of whether data changed. Each fix adds a hash fingerprint and a `_wasVisible` guard so DOM operations only fire when content actually changes.

| Overlay | Root Cause | Fix |
|---------|-----------|-----|
| `scorebug.html` | `#bug-scroll` `innerHTML=` reset CSS `animation` to frame 0 every tick | `_lastScrollKey` hash; only rebuild scroll DOM when athletes/scoreUnit change |
| `leaderboard.html` | `.lb-row` CSS transitions (opacity+translateX) reset every tick; timer accumulation | `_lastLbKey` hash; cancel pending `_animTimer` on rebuild |
| `lowerthird.html` | `animOut()→animIn()` (700ms) fired on every SSE tick when visible | `_lastLanesKey` hash; most severe — nameplates animated in/out every second |
| `champion.html` | `spawnParticles()` created 40 DOM elements every tick while champion shown | `_champVisible` guard; particles spawn only on `false→true` transition |
| `reps.html` | Full innerHTML rebuild of lights strip + rep cards every tick | `_builtLaneCount` + `patchContent()` — structure built once, content patched |
| `lineup.html` | `showCategory()` fade-out/in fired on every SSE tick when visible | `_lastCatKey` hash; animation only on category data change |

**P1 — Comp auth replacement:**  
- Removed HTTP Basic Auth (browser password dialog, no logout, ugly)
- `COMP_PASSWORD` env var → session-based auth via `session["comp_ok"]`
- `/comp/login` (GET: form, POST: verify + set session), `/comp/logout`
- Protected paths: `/update`, `/control.html`, `/judge.html`, `/judge-master.html`, `/debug.html`, all `/comp/*` routes
- Public overlay paths (`/stream`, `/state.json`, `scorebug.html`, etc.) remain open
- `app.secret_key` initialized from `SECRET_KEY → COMP_PASSWORD → BETA_TOKEN` cascade

**P2 — Leaderboard category dropdown showed only one category:**  
`live.athletes` in `sync_comp_to_broadcast()` is scoped to the current active category. Fixed by reading `live.categories` (all active categories) directly into `lbCategorySelect` in `syncFromCompEngine()`.

**P2 — Category color reversion to yellow on SSE update:**  
`sync_comp_to_broadcast()` always overwrote stored category colors with hardcoded `CAT_COLORS` values. Fixed by checking `state.json`'s existing `categories` array first — stored color takes precedence, `CAT_COLORS` is now a fallback only.

### Security — Beta Auth Gate
**Commits:** `1f87961`, `1329897`  
- `app/beta_auth.py`: sitewide cookie-based gate
- Login page at `/beta/login`, logout at `/beta/logout`
- `?token=` query param for OBS browser sources
- `robots.txt` returning `Disallow: /`
- `X-Robots-Tag: noindex, nofollow` on all responses
- Session cookie: `sp_beta`, 7-day expiry, HTTPS-only, HttpOnly, SameSite=Lax
- Layered: beta gate → comp Basic Auth (both required for `/comp/*`)

---

## Known Gaps vs Refactor Plan

| Plan Phase | Description | State |
|------------|-------------|-------|
| 4 (scoring) | Weight & Reps event type | **Done** (`d8e0975`) |
| 5 | Leaderboard expansion (per-event breakdown) | **Done** (`ab093ec`) |
| 6 | DB layer — request-scoped connection, migration framework | **Not started** |
| 7 | Frontend cleanup — shared CSS, base template | **Not started** |
| 9 | Production hardening — logging, healthcheck, .env, Docker | **Not started** |
| 10 | Security — CSRF, rate limiting | **Not started** |
| 11 | Deployment docs | Partial |

---

## Phase 5 Deployment Verification (2026-05-13)

All checks passed against live `strongpass.live`:

| Check | Result |
|-------|--------|
| Beta gate (no auth → 401) | ✅ |
| Beta gate (`?token=` → 200) | ✅ |
| Comp admin auth (beta-only → 401, beta+comp → 200) | ✅ |
| SSE stream `/stream` | ✅ 200 |
| `/api/results/<category>` JSON (version, events, rows) | ✅ |
| `/comp/api/leaderboard?detailed=1` JSON | ✅ |
| `weight_reps` option in event setup dropdown | ✅ |
| `judge.html` `block-weight-reps` block present | ✅ |
| All 7 OBS overlays (`*.html`) | ✅ 200 |
| Overlays use `sse-client.js` (not polling) | ✅ |
| `comp_leaderboard` detailed breakdown CSS/HTML | ✅ |
| `comp_results_public` uses fetch+SSE (no `location.reload`) | ✅ |
| `comp_heats` uses SSE-triggered reload (no `setInterval`) | ✅ |
| Mobile `viewport` meta on all comp templates | ✅ |

---

## Product Roadmap State (from priorities)

| Priority | Item | State |
|----------|------|-------|
| 1 | Temporary site protection | **Done** |
| 2 | Security audit | Not started |
| 3 | Mobile-first redesign planning | Not started |
| 4 | Organiser/member architecture planning | Not started |
| 5 | Stripe subscription planning | Not started |
| 6 | Organiser access control | Not started |
| 7 | Members area architecture | Not started |
| 8 | Livestream/content platform roadmap | Not started |
