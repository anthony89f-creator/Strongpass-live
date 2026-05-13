# UI/UX + Scoring Redesign Plan — StrongPass Competition OS
**Created:** 2026-05-13  
**Status:** Planning — not yet implemented  
**Prerequisite reading:** `known_issues.md` (M3, M5), `roadmap.md` (P3)

---

## Scope

Three sequential phases. Each phase is independently deployable and rollback-safe.

| Phase | Focus | Risk | Blocker |
|-------|-------|------|---------|
| 1 | Mobile-first responsive redesign | Low — CSS only, no logic changes | None |
| 2 | Leaderboard architecture redesign | Medium — adds JSON API + JS rendering | Phase 1 done |
| 3 | Weight+reps scoring type | Medium — backend + 3 templates | None (independent) |

---

## Phase 1 — Mobile-First Responsive Redesign

### Goal
All comp-facing pages are usable on a 390px (iPhone 14) screen without horizontal scrolling, pinching, or hidden data. OBS overlays remain unchanged. `judge.html` is already well-optimized — no changes needed there.

### Current state summary (from audit)
- 6/24 HTML files have any `@media` rules
- `comp_heats.html`, `comp_athletes.html`, `comp_results.html`, `comp_leaderboard.html`: zero responsive rules
- `comp_results_public.html`: hides ALL event columns on mobile — data entirely missing on phones
- `comp_run.html`: collapses grid at 900px but score inputs (130px), lane column, ath-name column remain cramped
- CSS custom properties inconsistently defined per-template — no shared base

### Files to change

#### 1a — `templates/base_comp.html` (new file)
Shared Jinja2 base template for all `/comp/*` pages. Contains:
- `<meta name="viewport" content="width=device-width, initial-scale=1.0">`
- CSS custom properties: `--bg`, `--surface`, `--surface2`, `--accent`, `--text`, `--text-muted`, `--danger`, `--radius`, `--font`
- Shared font import (Barlow Condensed — already used across templates)
- Shared reset: `box-sizing:border-box`, `margin:0`, `padding:0`
- Shared utility classes: `.btn`, `.btn-danger`, `.card`, `.flash-messages`, `.page-header`
- Block structure: `{% block head %}`, `{% block body %}`

All 8 comp Jinja2 templates extend this base via `{% extends "base_comp.html" %}`.

**OBS note:** Overlay HTML files (`/overlay/`, root `*.html`) do NOT extend this base — they are raw-served and must remain independent.

#### 1b — `templates/comp_run.html`
Critical page — used actively during competition.

Changes:
- Extend `base_comp.html`
- Score input width: `130px` → `min(130px, 35vw)` — prevents overflow on narrow screens
- Lane-num column: add `min-width:2rem; max-width:3rem` — currently collapses awkwardly
- Athlete name column: add `overflow:hidden; text-overflow:ellipsis; white-space:nowrap` — long names break layout
- Jump-to-heat input: `80px` fixed → `max-width:80px; width:100%`
- Leaderboard sidebar: collapse below the run table at `≤768px` (currently collapses at 900px but still crammed)
- Secondary metric label (e.g., "Reps"): ensure visible at all widths — currently can overflow
- Add `touch-action:manipulation` on all score inputs (prevents 300ms tap delay)

Breakpoints:
```css
/* 900px: existing collapse preserved */
@media (max-width: 900px) { /* already exists — refine */ }
/* 600px: narrow phone — single column, inputs full-width */
@media (max-width: 600px) { 
  .score-input { width: 100%; min-width: 0; }
  .lane-header, .lane-num { font-size: 0.75rem; }
}
```

#### 1c — `templates/comp_heats.html`
Current: `flex:0 0 25%` — breaks with >4 lanes or <480px screen. 10s `setInterval(location.reload)` kept as-is (Phase 2 concern).

Changes:
- Replace `flex:0 0 25%` with `flex: 1 1 min(25%, 120px)` — wraps cleanly at any lane count
- Heat cell min-width: `80px` — prevents text overlap
- Add `@media (max-width: 600px)` — heat cells wrap to 2-per-row
- Add `overflow-x: auto` on the heat grid container as fallback

#### 1d — `templates/comp_leaderboard.html`
Currently renders only name + total score. Mobile fix is simple because the table is narrow.

Changes:
- Add `viewport` meta tag (currently absent)
- Add `overflow-x: auto` wrapper on table
- Add `@media (max-width: 600px)` — reduce font size, tighten padding
- **Do not add per-event columns here** — that is Phase 2

#### 1e — `templates/comp_results_public.html`
Current: `@media (max-width:700px) { .event-col { display:none } }` — ALL event data hidden on mobile.

Changes:
- Remove the blanket `display:none` rule
- Replace with horizontal scroll: wrap table in `overflow-x: auto` container
- Fixed column widths: `ath-name-col` 176px → `min-width:120px`, `ath-rank` 44px → `min-width:36px`
- Sticky first column (name): `position:sticky; left:0; background:var(--surface)` — keeps name visible while scrolling event columns
- Sticky `ath-rank` column: same treatment
- `@media (max-width: 600px)` — reduce name column to 100px, rank to 32px, event column min-width to 52px

#### 1f — `templates/comp_athletes.html`
Changes:
- Add `viewport` meta tag
- `.btn-actions` already has `flex-wrap:wrap` — good
- Add `@media (max-width: 600px)` on `.athlete-row`: stack actions below athlete name
- Input fields for athlete name/category: `width:100%` below 600px

#### 1g — `templates/comp_results.html`
Legacy results entry page (admin use only — lower priority).

Changes:
- Add `viewport` meta tag
- `.score-input` 120px fixed → `min(120px, 40vw)`
- Add `@media (max-width: 600px)` — single column layout

#### 1h — `templates/comp_home.html`
Competition setup page. Already has `viewport` meta tag. Changes:
- Replace inline styles throughout with CSS class-based rules (extend base)
- Add `@media (max-width: 600px)` on form grid — stack fields to single column
- Category/event select elements: `width:100%` on mobile

#### 1i — `templates/comp_callroom.html`
OBS-facing + operator-facing split. Already uses `clamp()` for fonts.

Changes:
- Add `@media (max-width: 768px)` — two-panel grid → single column (callroom stacks above arena)
- Panel min-height on mobile: use `min-height:40vh` per panel instead of `height:50%`

### What is NOT changing in Phase 1
- `judge.html` — already optimized
- Any overlay HTML file (`/overlay/*.html`, root `*.html`)
- SSE logic — no polling introduced
- Backend routes — zero Python changes
- `state.json` format
- Database schema

### Commit strategy
```
feat(phase5-ui): add shared base_comp.html template + CSS variables
feat(phase5-ui): comp_run.html mobile layout (score inputs, sidebar breakpoints)
feat(phase5-ui): comp_heats.html responsive heat grid
feat(phase5-ui): comp_results_public.html sticky columns + horizontal scroll (replaces mobile hide)
feat(phase5-ui): comp_leaderboard/athletes/results/home/callroom mobile layout
```

Each commit is safe to deploy independently. Rollback: `git revert <sha>` — no DB/state changes.

---

## Phase 2 — Leaderboard Architecture Redesign

### Goal
Replace `location.reload()` with SSE-driven DOM updates for the leaderboard. Implement per-event breakdown in `comp_leaderboard.html`. Eliminate the 10s full-page reload in `comp_heats.html`.

### Current state summary
- `comp_results_public.html`: SSE → `location.reload()` (full reload, 3s debounce) — data consistent but wasteful, causes flicker
- `comp_leaderboard.html`: no SSE at all, server-rendered only, ignores `detailed_rows`/`events_list`
- `comp_heats.html`: `setInterval(() => location.reload(), 10000)` — polling via full reload
- `comp_run.html` sidebar: server-rendered only, no live updates
- Backend `get_leaderboard_detailed()` already computes full per-event data — just not exposed via API

### Files to change

#### 2a — New route: `GET /comp/api/leaderboard`
Returns JSON:
```json
{
  "version": 42,
  "events_list": ["Log Press", "Deadlift", "Farmer's Walk"],
  "rows": [
    {
      "rank": 1,
      "name": "Alice",
      "category": "Open Women",
      "total": 28.5,
      "events": [10, 8.5, 10]
    }
  ]
}
```
`version` is `_sse_version` at time of render — allows client to skip re-render if unchanged.

Auth: behind `_require_comp_auth` (same as all `/comp/api/*` routes).

**Input validation:** wrap `get_leaderboard_detailed()` call in try/except; return 500 with JSON error body on failure. Category filter via `?category=` query param (optional).

**Relates to:** known_issues.md M7 — apply `_safe_int()` pattern here for any integer params.

#### 2b — `templates/comp_leaderboard.html`
Full redesign. Server renders initial HTML with full per-event breakdown (as now, but actually used). JS layer adds live updates via SSE.

Table structure:
```
Rank | Name | [Event 1] | [Event 2] | ... | Total
```
- Column headers generated from `events_list` (Jinja2 loop, same as `comp_results_public.html`)
- Each row has `data-athlete-id="{{ row.name }}"` for targeted DOM update
- `overflow-x: auto` wrapper (Phase 1 already applied)

JS update flow:
```javascript
const evtSrc = new EventSource("/stream");
evtSrc.onmessage = (e) => {
  const data = JSON.parse(e.data);
  if (data.version === lastVersion) return;  // no change
  fetch("/comp/api/leaderboard")
    .then(r => r.json())
    .then(updateTable);
};
```
`updateTable()` diffs rows by athlete name, updates cells in-place — no full reload, no flicker.

**OBS note:** `comp_leaderboard.html` is served to OBS browser sources. SSE + EventSource works transparently in OBS Chromium. The `?token=` cookie set on first load carries through the EventSource connection. No change needed to beta auth flow.

#### 2c — `templates/comp_results_public.html`
Replace `location.reload()` with same fetch-and-diff pattern as 2b.

Current:
```javascript
evtSrc.onmessage = () => {
  clearTimeout(reloadTimer);
  reloadTimer = setTimeout(() => location.reload(), 3000);
};
```

Replacement: fetch `/comp/api/leaderboard`, diff rows, update DOM in-place.

Benefit: eliminates page flicker and scroll-position reset on every score change.

#### 2d — `templates/comp_heats.html`
Replace `setInterval(location.reload, 10000)` with SSE-triggered fetch of `/comp/api/heats_data` (new endpoint returning current heat assignments as JSON). On SSE event, fetch and update DOM.

New endpoint: `GET /comp/api/heats_data` — returns current heat structure. Low complexity — reuses existing heat query logic.

#### 2e — `comp_run.html` sidebar
Currently server-rendered only. Add SSE listener that fetches `/comp/api/leaderboard?category=<current_category>` on score change, updates sidebar rows in-place. Capped at 20 entries as now.

### What is NOT changing in Phase 2
- SSE stream format — no new event types needed; existing version counter drives all updates
- Backend leaderboard computation logic
- Database schema
- Any overlay files

### Commit strategy
```
feat(phase5-lb): add /comp/api/leaderboard JSON endpoint
feat(phase5-lb): comp_leaderboard.html per-event breakdown + SSE DOM updates
feat(phase5-lb): comp_results_public.html SSE DOM update (replaces location.reload)
feat(phase5-lb): comp_heats.html SSE update (replaces 10s setInterval reload)
feat(phase5-lb): comp_run.html sidebar live update via SSE
```

Rollback: any commit in this phase can be reverted independently. The `/comp/api/leaderboard` endpoint is additive — removing it only breaks the JS in templates that were updated in the same phase.

---

## Phase 3 — Weight+Reps Scoring Type

### Goal
Add `weight_reps` as a first-class scoring type: heavier weight ranks higher; equal weight is broken by more reps. Common in log press ladders, axle press, etc.

### Current state summary
- `app/config.py` EVENT_TYPES: `{reps, weight, distance, time, object}` — `weight_reps` absent
- `app/config.py` SCORING_PRESETS: 6 presets — no `weight_reps` preset
- `judge.html`: 4 scoring blocks — no `weight_reps` block
- `comp_run.html`: handles primary + optional secondary metric but no `weight_reps`-specific display
- `comp_home.html`: scoring preset `<select>` has no `weight_reps` option
- Scoring engine: `_compute_points_for_event()` — needs `weight_reps` branch

### Files to change

#### 3a — `app/config.py`
Add to `EVENT_TYPES`:
```python
"weight_reps": {
    "label": "Weight + Reps",
    "primary_metric": "weight_kg",
    "secondary_metric": "reps",
    "primary_label": "Weight (kg)",
    "secondary_label": "Reps",
    "higher_is_better": True,
    "secondary_higher_is_better": True,
}
```

Add to `SCORING_PRESETS`:
```python
"weight_reps": {
    "label": "Weight + Reps (Log, Axle)",
    "event_type": "weight_reps",
    "scoring": "points",
    "tiebreak": "secondary_desc",
}
```

#### 3b — Scoring engine in `server.py`
`_compute_points_for_event()` (currently handles primary sort only).

Add `weight_reps` sort: sort athletes descending by `primary_score` (weight), then descending by `secondary_score` (reps) for ties. Points assigned by rank.

Display format helper: `_format_score_display(event_type, primary, secondary)`:
- `weight_reps`: `"140kg × 6"`
- `reps`: `"12 reps"`
- `weight`: `"140kg"`
- `time`: `"1:23.4"`
- other: `str(primary)`

This helper is used in comp_run.html and comp_results_public.html rendering (template filter or passed in context).

**Relates to:** known_issues.md M3 — `action_save_results` near-duplicate handlers. The `weight_reps` secondary score must be handled consistently in both handlers. This is an opportunity to extract `_save_heat_results()` as part of this phase.

#### 3c — `judge.html`
Add new scoring block after the existing `block-objects-time` block:

```html
<div class="scoring-block" id="block-weight-reps" style="display:none">
  <!-- Weight input (number, kg) -->
  <div class="number-input-row">
    <button class="adj-btn" data-target="weight-kg" data-delta="-2.5">−</button>
    <input type="number" id="weight-kg" inputmode="decimal" step="2.5" min="0">
    <button class="adj-btn" data-target="weight-kg" data-delta="+2.5">+</button>
    <span class="unit-label">kg</span>
  </div>
  <!-- Rep counter (integer) -->
  <div class="counter-row">
    <button class="counter-btn minus" data-target="reps-wr">−</button>
    <span class="counter-display" id="reps-wr-display">0</span>
    <input type="hidden" id="reps-wr" value="0">
    <button class="counter-btn plus" data-target="reps-wr">+</button>
  </div>
</div>
```

`showScoringBlock(type)` JS function already switches block visibility — add `'weight_reps': 'block-weight-reps'` to its dispatch map.

Score submission: `primary_score = weight_kg`, `secondary_score = reps` — follows existing pattern used by `block-objects-time`.

#### 3d — `comp_run.html`
Score entry: already supports `secondary_metric` input for `objects_time`. Extend:
- Add `weight_reps` to the condition that shows secondary input (`{% if event_type in ['objects_time', 'weight_reps'] %}`)
- Secondary input label: "Reps" (from `EVENT_TYPES[type].secondary_label`)
- Score display in the results table: use `_format_score_display()` — renders "140kg × 6" instead of bare number

#### 3e — `comp_home.html`
Add to scoring preset `<select>`:
```html
<option value="weight_reps">Weight + Reps (Log, Axle)</option>
```

Add to `setScoringFromPreset()` JS dispatch map: `'weight_reps'` → sets primary to `weight_kg`, secondary to `reps`.

### What is NOT changing in Phase 3
- Existing event types — backward compatible addition only
- Database schema — `primary_score` + `secondary_score` columns already exist
- SSE format
- OBS overlays — score display is text rendered server-side in templates

### Commit strategy
```
feat(scoring): add weight_reps event type to config + scoring engine
feat(scoring): judge.html weight+reps scoring block
feat(scoring): comp_run.html + comp_home.html weight_reps support
```

Phase 3 is fully independent of Phases 1 and 2. Can be implemented in any order.

---

## Cross-Cutting Concerns

### OBS compatibility
- Phases 1–3 add no new auth requirements to overlay routes
- Phase 2 SSE fetch calls use `/comp/api/*` — these are behind `_require_comp_auth`. The `comp_leaderboard.html` and `comp_results_public.html` templates are only served to authenticated admins, so the fetch calls inherit the session cookie. OBS browser sources that are set to leaderboard URLs carry the `sp_beta` cookie from initial load.
- If leaderboard pages are served publicly (no comp auth), the `/comp/api/leaderboard` endpoint must be moved or duplicated to a public route. This is an architectural decision to make before Phase 2 implementation — flag if leaderboard pages are intended to be publicly accessible.

### SSE preservation
- No phase adds polling. All live updates go through the existing `/stream` SSE endpoint.
- Phase 2 adds fetch calls triggered by SSE events — these are one-shot HTTP requests, not polling intervals.

### Rollback safety
- Each commit in each phase is independently revertable with `git revert`
- No database schema changes across all three phases
- No changes to `state.json` format
- No changes to `app/beta_auth.py` or auth flow

### Known issues addressed by this plan
| Issue | Phase |
|-------|-------|
| M5 — comp_leaderboard.html ignores detailed_rows | Phase 2 |
| M3 — action_save_results near-duplicate handlers | Phase 3 (extract _save_heat_results) |
| M7 — event_points missing input validation | Phase 2 (apply _safe_int pattern to new API) |
| L4 — ws-client.js still exists | Phase 1 (verify + delete as part of cleanup commit) |

### Known issues NOT addressed by this plan
| Issue | Reason deferred |
|-------|----------------|
| H1 — No CSRF | Security audit (P2) — separate workstream |
| H4 — DB connection per operation | Phase 6 in refactor plan — architectural change |
| M4 — state.json dual-purpose | Medium-term architectural change |
| L1 — No structured logging | Phase 9 hardening |

---

## Implementation Order Recommendation

1. **Phase 3** first — isolated, zero risk, delivers visible product value (new scoring type)
2. **Phase 1** second — CSS-only, no logic, safe to deploy to live immediately
3. **Phase 2** last — highest complexity, depends on Phase 1 base template

Each phase should be reviewed and deployed before starting the next.
