# Handover — Phase 5: Broadcast Director Controls + Category Color Persistence
**Date:** 2026-05-16  
**Commits:** `f45fb42`, `963f9c5`, `2711492`  
**Status:** Complete — deployed and verified live  
**Follows:** `HANDOVER_PHASE4_DIRECTOR.md`

---

## What Was Done

### 1. Category Color Persistence (f45fb42, 963f9c5)

**Root cause:** `action_add_category()` silently discarded the color form field. All downstream code reconstructed colors from hardcoded `CAT_COLORS` dict on every sync/restart.

**Fix — canonical store:**
- `state.json["category_colors"]` = `{name: hex_color}` dict — single source of truth
- Persists independently of categories list, athletes, events, and restarts
- Backward compat: falls back to legacy `categories` array colors, then `CAT_COLORS`

**New helpers in server.py:**
- `_load_category_colors(s=None)` — reads from `category_colors`, with backward-compat fallback
- `_set_category_color(name, color)` — atomic write to `category_colors` dict

**Routes updated:**
- `action_add_category()` — now saves color field from form
- New `POST /comp/action/set_category_color` — updates color, fires `sync_comp_to_broadcast()`
- `comp_home` route — uses `_load_category_colors()` instead of hardcoded `CAT_COLORS`
- `comp_run` route — passes `{**CAT_COLORS, **_load_category_colors()}` to template
- `public_results` route — same merge pattern
- `sync_comp_to_broadcast()` — uses `_load_category_colors(_s)` for category dots in state

**comp_home.html:**
- Static colored dot replaced with `<input type="color">` form that submits `onchange`
- No extra button needed — color picker submits inline

**Verified:** create categories with colors → restart → colors survive; SSE payload carries correct colors

---

### 2. Multi-Category Cached Standings (2711492)

**Root cause:** `sync_comp_to_broadcast()` called `get_leaderboard(category=category)` on every tick — a fresh DB query even when no scores had changed.

**Fix:**
- `_get_cached_standings(category=None)` — derives leaderboard from `_results_cache` (already maintained for public results). No extra DB queries.
- `_build_lb_standings()` — pre-computes all categories simultaneously using the warm cache, returns `{cat: [athletes], "all": [all_athletes]}`

**Hot path reduction:**
- Timer tick (no score change): `_results_dirty = False` → `_get_cached_standings()` returns from memory instantly
- Score change: `_results_dirty = True` → `_get_cached_results()` runs once for all categories; all subsequent calls use cache

**`lbStandings` in state.json:**
- All category standings pre-computed and stored as `state.json["lbStandings"]`
- `/director/lb` reads from `lbStandings` dict — no DB query for instant category switch
- `lbStandings` stripped from SSE payload (keeps stream lean at ~10KB); accessible via `/state.json`
- Available for future features (control.html preview on hover, etc.)

---

### 3. Director Leaderboard Freeze (2711492)

**New state fields:**
```json
"lbFrozen": false,      // when true: overlay holds snapshot, ignores new scores
"lbDisplayCount": 10    // number of rows on overlay (5/8/10/15)
```

**`leaderboard.html` changes:**
- `_frozenAthletes` — snapshot captured on `lbFrozen false→true` transition
- While frozen: displays snapshot, hash guard uses `'F'` tag so key differs from live
- FROZEN badge (blue dot) replaces LIVE dot in footer when frozen
- DIRECTOR badge (gold) still shows when `lbCategoryOverride` is active
- `lbDisplayCount` controls `displayAthletes.slice(0, displayCount)` — default 10

**`control.html` changes:**
- `❄ Freeze` / `▶ Live` toggle button — blue highlight when frozen
- `Rows:` selector — 5/8/10/15 options
- Mini preview panel — top 5 of current `lbAthletes` with rank/name/score, updated from SSE
- All director state (`lbFrozen`, `lbDisplayCount`) syncs across concurrent operators via SSE

---

## Deployment Verification

| Check | Result |
|-------|--------|
| `/health` | `{"db":"ok","status":"ok"}` ✅ |
| All 8 overlay files 200 | ✅ |
| `comp_run`, `public_results` 200 | ✅ |
| Category colors survive restart | ✅ `category_colors` in state.json |
| New category color saved | ✅ via `action_add_category` |
| Existing category color update | ✅ via `set_category_color` |
| SSE categories carry custom color | ✅ `u80: #CC0044` in payload |
| `lbStandings` in state.json | ✅ `['men', 'u80', 'all']` |
| `lbStandings` NOT in SSE | ✅ stripped from stream |
| `lbAthletes` in SSE | ✅ present |
| `/director/lb` with override | ✅ instant, no DB query |
| `/director/lb` reset to auto | ✅ |

---

## State Schema Additions

```json
"category_colors": {"U90": "#F5C842", "U80": "#CC0044"},
"lbStandings": {"U90": [...], "U80": [...], "all": [...]},
"lbFrozen": false,
"lbDisplayCount": 10
```

---

## Consumer Map — Who Uses What

| Consumer | Color source | Standings source |
|----------|-------------|-----------------|
| `leaderboard.html` overlay | `state.categories[].color` (from SSE) | `lbAthletes` (from SSE) |
| `scorebug.html` overlay | not used | `state.athletes` |
| `lineup.html` overlay | `state.categories[].color` | `state.categories[]` |
| `control.html` | `state.categories[].color` | `state.lbAthletes` (preview) |
| `comp_run.html` | `cat_colors` template var | `leaderboard` template var |
| `comp_results_public.html` | `cat_colors` template var | rendered server-side |
| `comp_home.html` | `categories[].color` template var | n/a |

All consumers now use colors that flow from `category_colors` in state.json through `_load_category_colors()`. No hardcoded CAT_COLORS overwrite anywhere in the hot path.

---

## What's Not Yet Done (Future Work)

| Item | Priority | Description |
|------|----------|-------------|
| CSRF protection | HIGH | All POST forms unprotected; add Flask-WTF tokens |
| DB request-scoped connections | HIGH | `flask.g` per-request; Phase 6 work |
| Arena screen routing | Medium | `comp_arena.html` is Jinja2; director routing needs new render mode |
| control.html color picker | Medium | `renderCategoryList()` shows static dots; could add inline color editing |
| Lineup category override | Low | Same pattern as `lbCategoryOverride` |
| Scorebug category override | Low | Director control for scorebug's category |
| lbStandings in SSE (lightweight) | Low | Include counts or top-5 for hover preview without full standings payload |

---

## Rollback Notes

Each change is purely additive:
1. Category colors: `category_colors` key in state.json is optional. If removed, falls back to `CAT_COLORS`.
2. lbStandings: optional state field. `/director/lb` falls back to `_get_cached_standings()` if not populated.
3. lbFrozen: defaults to `false`. Removing the field from state.json has no effect on normal operation.
4. lbDisplayCount: defaults to 10. Field is optional in both overlay and control panel.
