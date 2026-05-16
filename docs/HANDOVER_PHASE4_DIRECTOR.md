# Handover — Phase 4 Broadcast Director Layer
**Date:** 2026-05-16  
**Commit:** `641aafb`  
**Status:** Complete — deployed and verified live  
**Follows:** `HANDOVER_PHASE3_RELIABILITY.md`

---

## What Was Done

### The Core Problem

The leaderboard overlay was tightly coupled to the scoring operator's active category:

1. `sync_comp_to_broadcast()` called `get_leaderboard(category=compCategory)` where `compCategory` is whichever category the scoring operator currently has open. `state.athletes` was always one-category scoped.

2. `syncFromCompEngine()` in control.html had: `if (live.compCategory) state.lbCategory = live.compCategory;` — this reset the director's manually chosen leaderboard category on every SSE sync tick (~every 80ms during competition).

Result: the broadcast director could not display U80 standings while U90 was being scored. The leaderboard overlay always followed the scoring operator's focus.

### The Fix

**New state fields in `state.json`:**

```json
"lbAthletes": [...],         // director-resolved leaderboard array
"lbCategoryOverride": false  // true = director has pinned a category
```

**`sync_comp_to_broadcast()` (server.py)**:
```python
_lb_override = _s.get("lbCategoryOverride", False)
_lb_cat      = _s.get("lbCategory", None)
if _lb_override and _lb_cat:
    lb_athletes = get_leaderboard() if _lb_cat == "all" else get_leaderboard(category=_lb_cat)
else:
    lb_athletes = leaderboard  # auto-follow current scoring category
# Added to broadcast_block:
"lbAthletes": lb_athletes, "lbCategoryOverride": _lb_override,
```

**New endpoint `/director/lb` (server.py)**:
```
POST /director/lb
Body: {"category": "U80"|"all"|"auto", "override": true|false}
Auth: comp session required
```
- Immediately fetches requested category's leaderboard
- Updates `lbCategory`, `lbCategoryOverride`, `lbAthletes` in state.json
- Fires SSE → overlay updates in <100ms
- `{category:"auto"}` or `{override:false}` clears override, reverts to comp engine

**`control.html`**:
- AUTO/MANUAL badge next to leaderboard dropdown (green=AUTO, gold=MANUAL)
- "↺ Auto" button appears when in MANUAL mode, triggers reset
- `syncFromCompEngine()` guards: `if (live.compCategory && !state.lbCategoryOverride)`
- Syncs `lbCategoryOverride` from server on every tick — other operators' changes propagate

**`leaderboard.html`**:
- `const displayAthletes = (state.lbAthletes && state.lbAthletes.length) ? state.lbAthletes : state.athletes;`
- DIRECTOR badge in footer when `state.lbCategoryOverride` is true
- `_lastLbKey` hashes `displayAthletes` (not `state.athletes`)

---

## Deployment Verification

| Check | Result |
|-------|--------|
| `/health` | `{"db":"ok","status":"ok"}` ✅ |
| `/director/lb` without auth | 401 ✅ |
| `/director/lb` with comp auth, `{category:"all",override:true}` | `{"ok":true,"lbCategory":"all","lbCategoryOverride":true}` ✅ |
| `state.json lbAthletes` count after override | 120 (all categories) ✅ |
| Reset to auto | `{"ok":true,"lbCategory":"U90","lbCategoryOverride":false}` ✅ |
| `lbCategoryOverride=false` in state.json after reset | ✅ |
| server.py compiles | `OK` ✅ |

---

## Usage Guide

### Leaderboard Director Override

**To pin leaderboard to a specific category:**
1. Open control.html
2. Under "Leaderboard:", the badge shows **AUTO** (green) by default
3. Select a category from the dropdown
4. Badge changes to **MANUAL** (gold) — leaderboard overlay now shows that category
5. The overlay remains on that category even when scoring operators switch to other categories

**To return to auto-follow:**
1. Click "↺ Auto" button next to the dropdown
2. Badge returns to **AUTO** (green) — leaderboard follows the current scoring category

**Production workflow example:**
- U90 is competing, scoring operator has U90 open
- Broadcast director wants to discuss U80 standings during interview
- Director selects "U80" in dropdown → leaderboard overlay switches to U80 immediately
- Scoring continues in U90 — scorebug/lower thirds/nameplates remain on U90
- After interview, director clicks "↺ Auto" → leaderboard follows U90 again

---

## Architecture Reference

Full architecture documented in `docs/ARCHITECTURE_BROADCAST.md`:
- State schema with all fields and their source-of-truth owners
- Data flow diagrams for normal tick, score entry, director override
- AUTO vs MANUAL mode table for all overlays
- Performance architecture (SSE cache, hash guards, debouncing)
- Protected route table

---

## What's Not Yet Done (Future Work)

| Item | Priority | Description |
|------|----------|-------------|
| Arena screen routing | Medium | `comp_arena.html` is a Jinja2 template; director routing requires a new render mode |
| Lineup category override | Low | `lineup.html` already supports manual `lineupIndex` via control.html; category override would follow same pattern as `lbCategoryOverride` |
| Per-overlay source indicator | Low | Overlays could show a small MANUAL badge; leaderboard already does this |
| CSRF protection | HIGH | All POST forms unprotected; add Flask-WTF tokens |
| DB request-scoped connections | HIGH | Phase 6: `flask.g` connection per request |
| Scorebug category override | Low | Scorebug shows current scoring category; director might want to show a specific category's recent scores |

---

## Rollback Notes

If the director layer causes issues:
1. `POST /director/lb {category:"auto", override:false}` — clears override immediately
2. In state.json directly: set `"lbCategoryOverride": false` and restart service
3. Git: `git revert 641aafb` — reverts all Phase 4 changes cleanly
4. The change is purely additive — no existing state fields were removed or changed in meaning
