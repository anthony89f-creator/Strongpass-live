# Project Status — StrongPass Competition OS
**Last updated:** 2026-05-13  
**Status:** Beta — live, protected, pre-production  
**Last updated:** 2026-05-13 (post phase5 deployment verification)

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
2,175 (pre-Phase 3) → 2,036 (post-Phase 3 extraction) → 2,036 (stable, no further extractions yet)

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
