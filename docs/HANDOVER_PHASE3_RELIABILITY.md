# Handover — Phase 3 Operational Reliability, Overlay Stability, Auth
**Date:** 2026-05-16  
**Status:** Complete — deployed and verified live  
**Follows:** `HANDOVER_PHASE2_OPTIMIZATION.md`

---

## What Was Done

### P3.1 — Overlay Animation Storms (6 files)

All 6 OBS overlay files were doing full DOM rebuilds or running CSS animations on every SSE timer tick (~1/s). The fixes add hash-based dirty checking so DOM operations only fire when content actually changes.

#### scorebug.html — CSS animation reset

**Problem:** `render()` set `#bug-scroll.innerHTML = html + html` on every SSE event. The `animation: scroll 80s linear infinite` CSS property on `#bug-scroll` was reset to frame 0 on every rebuild. Scroll jumped back to start every ~1 second.

**Fix:** Added `_lastScrollKey` (hash of `athletes + scoreUnit`). `innerHTML` only rebuilt when hash changes. Static fields updated via `textContent` on every render.

#### leaderboard.html — Transition flash + timer accumulation

**Problem:** `lb-body.innerHTML = ...` rebuilt on every SSE event, destroying `.lb-row` elements and resetting opacity+translateX CSS transitions. `setTimeout(() => add .in, 100)` accumulated timers.

**Fix:** Added `_lastLbKey` (hash of `category + filtered athletes`). Row DOM only rebuilt when hash changes. `_animTimer` cancelled on new rebuild. `_lbWasVisible` tracks visibility separately so toggle doesn't trigger row rebuild.

#### lowerthird.html — animOut+animIn storm (most severe)

**Problem:** `else if (want && currentlyVisible) { animOut(()=>animIn()); }` fired on every SSE event when visible. `animOut` takes 700ms (CSS transitions), then `animIn` rebuilds and slides back. Nameplates animated out and back in every ~1 second.

**Fix:** Added `_lastLanesKey` and `_lanesKey()`. `animOut(animIn)` only called when lane content changes.

#### champion.html — Particle DOM accumulation

**Problem:** `spawnParticles()` called on every `render()` when `state.champion` is true. Created 40 new animated DOM elements per SSE tick.

**Fix:** Added `_champVisible`. Particles spawn only on `false → true` visibility transition.

#### reps.html — innerHTML on every tick

**Problem:** `strip.innerHTML` and `bar.innerHTML` rebuilt on every SSE event. Resets CSS transitions on `.light-panel` (background color change) and `.lp-dot` (glow). Data changes every second (timer) so no simple hash guard works.

**Fix:** `buildStructure(count)` called once per `laneCount` change, creates elements with stable IDs (`lp-{i}`, `rc-{i}`, etc.). `patchContent(count)` patches every render via `getElementById` + `className` / `textContent` assignment. `_builtLaneCount` tracks last built count. `_repsWasVisible` guards slide-in animation — triggers only on `false → true` transition.

#### lineup.html — showCategory animation on every tick

**Problem:** `else if (want && isVisible) { showCategory(...); }` called on every SSE event. `showCategory()` fades content out (opacity→0) then 50ms later fades back in. Content flickered every ~1 second.

**Fix:** Added `_lastCatKey` and `_catKey()` (hash of `compName + currentIndex + cat.name + cat.count + athletes`). `showCategory()` records `_lastCatKey = _catKey()` after updating content. `render()` only calls `showCategory()` when key changes.

---

### P3.2 — Comp Auth Replacement

**Problem:** `/comp/*` routes used HTTP Basic Auth (browser password dialog, no logout, ugly UX). `/update`, `/judge.html`, `/control.html` were not protected at all.

**Fix:** Session-based comp auth.

```python
# Protected paths
_COMP_PROTECTED_PATHS = frozenset({"/update", "/control.html", "/judge.html", "/judge-master.html", "/debug.html"})
_COMP_AUTH_BYPASS     = frozenset({"/comp/login", "/comp/logout"})

# Routes
@app.route("/comp/login", methods=["GET","POST"])  # form + session["comp_ok"] = True
@app.route("/comp/logout")                          # session.pop("comp_ok")

# Before-request hook
@app.before_request
def _require_comp_auth():
    # Applies to: /comp/* and _COMP_PROTECTED_PATHS
    # HTML requests → redirect to /comp/login?next=...
    # Non-HTML requests → 401
```

Secret key cascade: `SECRET_KEY → COMP_PASSWORD → BETA_TOKEN` env var.

Public overlay routes (`/stream`, `/state.json`, `scorebug.html`, `leaderboard.html`, etc.) are **not** in `_COMP_PROTECTED_PATHS` and remain accessible without comp auth.

---

### P3.3 — Leaderboard Category Dropdown

**Problem:** `lbCategorySelect` in `control.html` was populated from `live.athletes`, but `sync_comp_to_broadcast()` only includes athletes from the currently active category. Result: dropdown showed one category.

**Fix:** In `syncFromCompEngine()` in `control.html`, populate `lbCategorySelect` from `live.categories` (all active categories, built from the full DB query in `sync_comp_to_broadcast()`).

---

### P3.4 — Category Color Persistence

**Problem:** `sync_comp_to_broadcast()` always wrote `CAT_COLORS.get(cat, "#F5C842")` — hardcoded fallback from `app/config.py`. Any stored custom color in `state.json` was overwritten on every sync.

**Fix:** Read `state.json`'s existing `categories` array first. Stored color takes precedence; `CAT_COLORS` is now a fallback only.

```python
_stored_cats = {c["name"]: c for c in _s.get("categories", []) if isinstance(c, dict) and c.get("name")}
stored_color = _stored_cats.get(cat, {}).get("color")
color = stored_color or CAT_COLORS.get(cat, "#F5C842")
```

---

## Files Changed

| File | Change |
|------|--------|
| `scorebug.html` | `_lastScrollKey` hash; `textContent` for static fields |
| `leaderboard.html` | `_lastLbKey` hash; `_animTimer` cancel; `_lbWasVisible` guard |
| `lowerthird.html` | `_lastLanesKey` hash; `_lanesKey()` fingerprint |
| `champion.html` | `_champVisible` guard; particles spawn once on show |
| `reps.html` | `buildStructure()` + `patchContent()` DOM patching |
| `lineup.html` | `_lastCatKey` hash; `_catKey()` fingerprint |
| `control.html` | `lbCategorySelect` populated from `live.categories` |
| `server.py` | Session-based comp auth; category color persistence fix |

---

## Deployment Verification (2026-05-16)

| Check | Result |
|-------|--------|
| `/health` | `{"db":"ok","restart_token":...,"status":"ok"}` ✅ |
| `/state.json` categories | 6 categories with stored colors ✅ |
| `/comp/login` GET | HTML login page ✅ |
| `/update` without comp auth (browser Accept) | 302 → /comp/login ✅ |
| `/update` without comp auth (curl) | 401 ✅ |
| `/comp/login` POST correct password | 200 + session cookie ✅ |
| `/leaderboard.html` (public overlay) | 200 ✅ |
| `_lastScrollKey` in scorebug.html | Present ✅ |
| `_lastLanesKey` in lowerthird.html | Present ✅ |
| `_champVisible` in champion.html | Present ✅ |
| `_builtLaneCount` in reps.html | Present ✅ |
| `_lastCatKey` in lineup.html | Present ✅ |

---

## Remaining Open Issues

| Issue | Severity | Notes |
|-------|----------|-------|
| H1 — CSRF on all forms | HIGH | All POST routes unprotected; mitigated by comp auth scope |
| H4 — new DB connection per operation | HIGH | Safe at 1 Gunicorn worker; Phase 6 fix |
| M1 — CATEGORY_ORDER global mutable | MEDIUM | Safe at 1 worker; store in DB for Phase 6 |
| M3 — `action_save_results` weak validation | MEDIUM | Near-duplicate handlers; Phase 6 refactor |
| L2 — `requirements.txt` no version pins | LOW | Minor |
| L5 — No `.env.example` | LOW | Deployer docs gap |

## Next Steps

1. **Stress test** — rapid category switching, multiple simultaneous control clients with Phase 3 fixes live
2. **CSRF protection** — `Flask-WTF` or minimal token in `before_request`
3. **DB layer** — request-scoped connection (`flask.g`), migration framework (Phase 6)
