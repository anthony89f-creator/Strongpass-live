# Strongman Comp Control — Simulation Report & Recommendations

## Simulation summary

A full workflow was run automatically (`python3 simulate_comp.py`) against the Flask server:

1. **Setup** — Clear all → generate test data (events, categories, 20 athletes per category, heats).
2. **Run comp** — Enter scores for heat 1 (save only), next heat, enter scores for heat 2.
3. **Injury** — Withdraw one athlete (U80) with reason "injury".
4. **Rebalance** — Regenerate heats; current heat repopulated with rebalanced lanes (e.g. Athletes 9–12).
5. **Leaderboard** — U80 shows 19 athletes (20 − 1 withdrawn); top 3 and scores correct.
6. **Scoring config** — Update event scoring to Max Weight via setup; persists to run/results.
7. **Stress** — Simulate all results (fill all events with random data); leaderboard updates.

All steps completed with no recorded failures.

---

## Judge → Run Competition simulation

A dedicated simulation verifies that **judge panel submissions** (all lanes, all scoring types) populate **raw_results** and appear on the **Run Competition** page:

- **Script:** `simulate_judge_to_run.py`
- **Run (no server):** `python3 simulate_judge_to_run.py --self-test`
- **Run (live server):** `python3 simulate_judge_to_run.py [base_url]` (default `http://127.0.0.1:8080`)

The simulation:

1. Loads test data (events: weight, time, distance, reps, object).
2. Sets broadcast mode to **engine** (required for `/update` to write to `raw_results`).
3. For each event type, sets comp state and POSTs `/update` with `judgeL1`…`judgeL4` payloads (reps, time with `timerSecs`/`timerRemaining`, weight/distance/objects with `primaryValue`, and tiebreak `secondaryValue` for object/distance+time).
4. Asserts `raw_results` has the expected rows and values.
5. Submits a single-lane payload (e.g. `judgeL1` only).
6. GETs `/comp/run` and checks that score values appear in the page.

All scoring types (reps, time, weight, distance, objects+time) and both “all lanes” and “single lane” submits are covered.

---

## Bug fixed during this pass

### Regenerate heats used “any event” instead of “current event”

**Issue:** `regenerate_remaining_heats()` treated “already competed” as “has a score in **any** event” for that category. So after event 1, athletes who had only done event 1 were excluded from heats for event 2, and could disappear from the run sheet.

**Change:** “Already scored” is now filtered by **current event** (`event_id` from comp state). Only athletes who have a result for the **current** event (and those in earlier heats) are excluded; the rest are rebalanced into remaining heats for this event.

**Files:** `server.py` — `regenerate_remaining_heats()` now resolves `current_event` → `event_id` and uses it in the `raw_results` query.

### Judge scores not populating Run Competition (500 on `/update`)

**Issue:** POST `/update` with judge payloads returned 500. `_apply_judge_scores_to_raw_results` used `ev_row.get("primary_metric")` but `ev_row` was a `sqlite3.Row`, which has no `.get()` method.

**Change:** Convert the event row to a dict before use: `ev_row = dict(ev_row)` so `.get()` and defaulting work. Also, `generate_test` now inserts `primary_metric`, `secondary_metric`, and directions for each event type so judge→raw_results logic sees the correct metric (weight, time, distance, reps, objects+time).

**Files:** `server.py` — `_apply_judge_scores_to_raw_results()`; `action_generate_test()`.

---

## Potential faults / edge cases

| Area | Risk | Suggestion |
|------|------|------------|
| **Heats table** | `event_id` is always `1` in heats; structure is “one heat plan per category” reused for all events. | Document that heats are per-category, not per-event; or extend schema later to support per-event heats if needed. |
| **Categories** | `CATEGORY_ORDER` is a global list; add/delete category mutates it in memory only (not persisted). | Persist category order (e.g. DB or config) so restarts and multiple workers keep same order. |
| **Concurrent use** | No CSRF on forms; no auth; single SQLite DB. | For multi-user: add auth, CSRF, and consider connection pooling or read-only replicas for heavy read pages. |
| **Validation** | Score form accepts any float; no min/max or “did not attempt”. | Add validation (and optional “DNS”/“DNF”) and clear error messages in UI. |
| **Referrer redirects** | Many actions do `redirect(request.referrer or "/comp/")`; missing referrer can send user to home. | Prefer explicit success URLs (e.g. back to run or callroom) where it matters. |
| **Tiebreak on Run page** | Run page has one score input per athlete; no tiebreak field for object/distance+time events. | Add optional tiebreak input when `current_event.secondary_metric` is set. |
| **Results page** | Results entry shows generic “score” unit; now uses `event_types[event_type].unit` — good. | Consider also showing tiebreak column when event has secondary_metric. |

---

## How to make it a professional, user-friendly tool

### 1. **Clear roles and navigation**

- **Single “control centre”** for the organiser: one place that shows “where we are” (category, event, heat), next action (e.g. “Enter scores for this heat” / “Move to next heat”), and links to Call Room, Arena, Run, Leaderboard, Setup.
- **Consistent nav** on every comp page: same top bar with Comp Home | Call Room | Arena | Run | Leaderboard | Setup | Broadcast, with current location highlighted.
- **Breadcrumbs** where useful: e.g. “Setup → Events” or “Run → U80 → Event 2 → Heat 3”.

### 2. **Safety and confirmations**

- **Destructive actions** (clear all, delete event, delete athlete, withdraw) in a modal or confirmation page: “You are about to … This will … Continue / Cancel.”
- **Undo where feasible** (e.g. “Reinstate” for withdrawals is already there; consider “Revert last heat scores” with a clear warning).
- **Read-only broadcast view** for audience/display devices, no edit links.

### 3. **Data entry UX**

- **Run page:** Show event name and **unit** (kg, reps, s, m) next to each score field; for object/distance+time, add a second (tiebreak) field where needed.
- **Validation:** Inline or on submit: “Score must be a number”; optional “Min/max” or “Did not start / No lift” so organiser doesn’t have to enter a fake number.
- **Save feedback:** After “Save scores” or “Save & next heat”, show a short success message (“Saved. Now on Heat 4”) and keep context (same category/event) obvious.

### 4. **Setup flow**

- **Wizard or checklist:** “1. Events (name + scoring) → 2. Categories → 3. Athletes → 4. Generate heats → 5. Start comp.” Disable or grey “Generate heats” until events + athletes exist; show a short explanation.
- **Scoring dropdown** (already fixed): Keep current behaviour — selection persists and is used on Run/Results.
- **Categories:** If you support custom categories, persist order and names; show athlete count per category and warn if 0 athletes.

### 5. **Heats and withdrawals**

- **After withdrawal:** Automatically suggest “Regenerate heats to rebalance lanes” with one button (you already do this when they hit “Generate heats” with existing results).
- **Call room / Arena:** Clearly show “Current heat” vs “Up next” and, if possible, “Following heat” so staff and athletes know the order.
- **Document** that “already competed” for rebalancing is “has a score in **this event**” (after the bug fix above).

### 6. **Leaderboard and results**

- **Per-category view** with optional “All categories”; show event-by-event breakdown (points and raw score) so promoters can answer “why is X ahead of Y?”
- **Export:** CSV/Excel of results and leaderboard for backup and external use.
- **Public view:** Optional read-only leaderboard URL (no edit links, no setup) for screens or shared links.

### 7. **Technical hardening**

- **Config:** Port, LANES, default category order, and optional “competition name” in a config file or env, not only in code.
- **Backups:** Optional “Export competition” (DB + state.json) and “Import” so events can be backed up or moved.
- **Errors:** Log exceptions; show a generic “Something went wrong” plus an error id; avoid leaking stack traces to users.
- **Auth (if multi-user):** Simple login for “organiser” vs “display only”; protect all `/comp/action/` and setup routes.

### 8. **Accessibility and devices**

- **Touch-friendly:** Run page and score entry with large tap targets and numeric keypad where appropriate.
- **Contrast and labels:** Ensure all inputs have visible labels and sufficient contrast for arena/call room conditions.
- **Offline / flaky network:** Consider a “pending saves” queue and retry for score submission if you expect bad WiFi.

---

## Quick wins (no schema change)

- Add a one-line success message after save (e.g. “Scores saved” or “Heats regenerated”) and optional “Back to Run” link.
- Show unit (kg, reps, s, m) next to score inputs on Run and Results (partially done).
- Add tiebreak column on Run when event has `secondary_metric`.
- Persist category order (e.g. in state.json or a small config table).
- Add CSRF to all POST forms.
- Add a simple “Competition name” field (e.g. in state or config) and show it in the header of comp pages.

---

## Files touched in this pass

- **server.py** — `regenerate_remaining_heats()` now filters “already scored” by current event; `_event_type_from_scoring()` and `update_event_scoring` keep `event_type` in sync for Run/Results.
- **templates/comp_home.html** — Scoring dropdown shows correct selected option and posts to `update_event_scoring` with hidden primary/secondary metric and directions.
- **templates/comp_results.html** — Score unit uses `event_types[event_type].unit`.
- **simulate_comp.py** — New script: full organizer flow + stress test (run with server on port 8080).
- **SIMULATION_REPORT.md** — This report.
