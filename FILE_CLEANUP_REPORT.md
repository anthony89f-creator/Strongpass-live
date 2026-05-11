# File Cleanup Report — Strongman Competition OS
**Date:** 2026-05-11

---

## Summary

| Category | Count | Action |
|----------|-------|--------|
| Dead legacy scripts (ws-client.js, unused pollers) | 1 | Delete |
| Root-level stale HTML duplicates | 3 | Delete (templates/ version is current) |
| Unimplemented template stubs | 1 | Delete or complete |
| Dev tools in production root | 5 | Move to `scripts/` / `tests/` |
| macOS artifacts | 2 | Delete |
| Docs to organize | 5 | Move to `docs/` |
| Active files to keep | 37+ | No change |

---

## DELETE — Dead Code (safe to remove immediately)

### ws-client.js
**Path:** `/ws-client.js`  
**Reason:** Legacy polling client. Polls `/state.json` every 500ms. Predates the SSE implementation. Used by 8 overlay files (`scorebug.html`, `leaderboard.html`, `lowerthird.html`, `reps.html`, `champion.html`, `h2h.html`, `lineup.html`, `manual_lowerthird.html`). All 8 already also do a one-time `fetch('/state.json')` on load.  
**Action:** Delete the file. Update the 8 overlay files to use a new `sse-client.js` (Phase 2). Remove `<script src="ws-client.js"></script>` tags from all 8 files.  
**Risk:** None — the overlays remain functional via the one-time fetch until SSE is wired up.

---

## DELETE — Stale Root-Level HTML Duplicates

These files exist in both the project root AND in `templates/`. The root versions are older, have fewer features, and are NOT served by any Flask route at their logical URL. They are only reachable at `/<filename>` via the catch-all static_files handler — an accidental route that nobody links to.

### comp_athletes.html (root)
**Path:** `/comp_athletes.html`  
**Active version:** `templates/comp_athletes.html` (served at `/comp/athletes`)  
**Difference:** Root version is 157 lines; templates version is 177 lines and has additional features (focus on add-athlete input, "Next Step" navigation section, additional JavaScript).  
**Action:** Delete root copy.  
**Risk:** None — the live route uses `templates/comp_athletes.html`.

### comp_home.html (root)
**Path:** `/comp_home.html`  
**Active version:** `templates/comp_home.html` (served at `/comp/events`)  
**Difference:** Root version is 253 lines; templates version is 282 lines and has the full scoring preset UI, category reorder buttons, and additional fields.  
**Action:** Delete root copy.  
**Risk:** None.

### comp_run.html (root)
**Path:** `/comp_run.html`  
**Active version:** `templates/comp_run.html` (served at `/comp/run`)  
**Difference:** Root version is 597 lines; templates version is 556 lines but is the actively maintained version that receives Jinja2 context variables. The root file is a raw HTML prototype.  
**Action:** Delete root copy.  
**Risk:** None — the live route uses `templates/comp_run.html`.

---

## DELETE or COMPLETE — Unimplemented Template Stubs

### templates/comp_entry.html
**Path:** `templates/comp_entry.html`  
**Reason:** A partially-implemented alternative results entry UI. The template references `GET /comp/entry` and `POST /comp/action/save_entry` — neither route exists in `server.py`. The template is served by no route and is therefore inaccessible.  
**Action:** Either (a) delete it, or (b) implement the missing routes if this feature is desired. Recommend deletion unless the team wants this as an alternative entry interface.  
**Risk:** Low. Nobody can reach it currently.

---

## MOVE to scripts/ — Development Tools in Production Root

These files serve no production purpose and should not live in the root directory alongside the application code.

| File | Move to |
|------|---------|
| `simulate_comp.py` | `scripts/simulate_comp.py` |
| `simulate_judge_to_run.py` | `scripts/simulate_judge_to_run.py` |
| `simulate_lane1_judge_time.py` | `scripts/simulate_lane1_judge_time.py` |
| `test_judge_pages.py` | `tests/test_judge_pages.py` |
| `test_judge_sync.py` | `tests/test_judge_sync.py` |

**Risk:** None for production. Update any paths in `SIMULATION_REPORT.md`.

---

## MOVE to docs/ — Documentation in Root

| File | Move to |
|------|---------|
| `BACKUPS.md` | `docs/BACKUPS.md` |
| `SIMULATION_REPORT.md` | `docs/SIMULATION_REPORT.md` |
| `STRESS_TEST_AND_RECOMMENDATIONS.md` | `docs/STRESS_TEST.md` |
| `ARCHITECTURE_AUDIT.md` | `docs/ARCHITECTURE_AUDIT.md` |
| `TECH_DEBT_REPORT.md` | `docs/TECH_DEBT_REPORT.md` |
| `FILE_CLEANUP_REPORT.md` | `docs/FILE_CLEANUP_REPORT.md` |
| `REFACTOR_PLAN.md` | `docs/REFACTOR_PLAN.md` |

**Risk:** None.

---

## DELETE — macOS Artifacts

| File | Action |
|------|--------|
| `.DS_Store` | Delete + add to `.gitignore` |
| `templates/.DS_Store` | Delete + add to `.gitignore` |

---

## KEEP — Active Files (do not touch)

### Core Application
| File | Status |
|------|--------|
| `server.py` | Active — to be modularized in Phase 3 |
| `comp.db` | Active — competition database |
| `state.json` | Active — broadcast + engine state |
| `requirements.txt` | Active — to be expanded |
| `gunicorn.conf.py` | Active |
| `nginx.conf` | Active |
| `strongman.service` | Active |
| `deploy.sh` | Active |
| `logo.png` | Active |

### Active Broadcast Overlays (root dir, served raw)
| File | Status |
|------|--------|
| `home.html` | Active — entry point |
| `control.html` | Active — main broadcast control |
| `judge.html` | Active — lane judge panel |
| `judge-master.html` | Active — judge coordinator panel |
| `scorebug.html` | Active — OBS overlay |
| `leaderboard.html` | Active — OBS overlay |
| `lowerthird.html` | Active — OBS overlay |
| `reps.html` | Active — OBS overlay |
| `champion.html` | Active — OBS overlay |
| `h2h.html` | Active — head-to-head overlay |
| `lineup.html` | Active — athlete lineup overlay |
| `manual_lowerthird.html` | Active — manual lower third |
| `results.html` | Active — public results display |
| `debug.html` | Active (dev use) — keep but move to dev environment only |

### Active Jinja2 Templates (templates/)
| File | Status |
|------|--------|
| `comp_home.html` | Active — setup/events page |
| `comp_athletes.html` | Active |
| `comp_heats.html` | Active |
| `comp_callroom.html` | Active |
| `comp_arena.html` | Active |
| `comp_run.html` | Active |
| `comp_results.html` | Active |
| `comp_leaderboard.html` | Active — to be expanded in Phase 5 |
| `comp_results_public.html` | Active — already uses SSE |
| `comp_dashboard.html` | **Orphaned** — no active route, but not harmful |

### Notes on comp_dashboard.html
`/comp/dashboard` redirects to `/comp/events`. The template exists but is rendered by no route. Safe to delete, or re-activate if the dashboard view is desired (it has a nice competition progress indicator). Recommend keeping as a candidate for Phase 5 implementation.

---

## Resulting Clean Directory Structure (after cleanup)

```
/
├── server.py                  ← monolith (to be modularized in Phase 3)
├── comp.db
├── state.json
├── requirements.txt
├── gunicorn.conf.py
├── nginx.conf
├── strongman.service
├── deploy.sh
├── logo.png
│
├── home.html                  ← broadcast/overlay HTML files (served raw)
├── control.html
├── judge.html
├── judge-master.html
├── scorebug.html
├── leaderboard.html
├── lowerthird.html
├── reps.html
├── champion.html
├── h2h.html
├── lineup.html
├── manual_lowerthird.html
├── results.html
├── debug.html
│
├── templates/                 ← Jinja2 templates
│   ├── comp_home.html
│   ├── comp_athletes.html
│   ├── comp_heats.html
│   ├── comp_callroom.html
│   ├── comp_arena.html
│   ├── comp_run.html
│   ├── comp_results.html
│   ├── comp_leaderboard.html
│   ├── comp_results_public.html
│   └── comp_dashboard.html   ← orphaned, keep for now
│
├── scripts/                   ← dev/simulation tools (moved from root)
│   ├── simulate_comp.py
│   ├── simulate_judge_to_run.py
│   └── simulate_lane1_judge_time.py
│
├── tests/                     ← test files (moved from root)
│   ├── test_judge_pages.py
│   └── test_judge_sync.py
│
├── docs/                      ← documentation (moved from root)
│   ├── ARCHITECTURE_AUDIT.md
│   ├── TECH_DEBT_REPORT.md
│   ├── FILE_CLEANUP_REPORT.md
│   ├── REFACTOR_PLAN.md
│   ├── BACKUPS.md
│   ├── SIMULATION_REPORT.md
│   └── STRESS_TEST.md
│
└── backups/                   ← timestamped backups (auto-generated)
```
