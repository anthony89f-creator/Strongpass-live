# StrongPass Broadcast Architecture
**Version:** Phase 4 — Broadcast Director Layer  
**Date:** 2026-05-16  
**Status:** Production

---

## 1. System Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                     COMPETITION ENGINE                           │
│                                                                  │
│  SQLite DB ──► get_leaderboard() ──► sync_comp_to_broadcast()   │
│  athletes, scores, heats, events, timers, lane assignments       │
│                       │                                          │
│          Authoritative source of all scoring data                │
└──────────────────────┬───────────────────────────────────────────┘
                       │  writes competition feed
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                  BROADCAST DIRECTOR LAYER                        │
│                                                                  │
│  state.json ─────────────────────────────────────────────────►  │
│    competition_fields: lanes, athletes, categories, eventName…   │
│    director_fields:    lbCategory, lbCategoryOverride,           │
│                        lbAthletes, overlay visibility flags      │
│                                                                  │
│  Director can PIN leaderboard to any category independently      │
│  of which category the scoring operator has open.                │
│                                                                  │
│  /director/lb ─── sets lbCategory + lbCategoryOverride          │
│  /update ──────── sets overlay visibility + display text         │
└──────────────────────┬───────────────────────────────────────────┘
                       │  SSE /stream  (flat JSON)
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                  OVERLAY CONSUMERS                               │
│                                                                  │
│  OBS Browser Sources — sse-client.js                             │
│    leaderboard.html  — uses lbAthletes (director-resolved)       │
│    lowerthird.html   — uses lanes (auto-follow live heat)        │
│    scorebug.html     — uses athletes (current scoring category)  │
│    champion.html     — uses champName/champScore etc.            │
│    lineup.html       — uses categories + athletes                │
│    reps.html         — uses judgeL1…judgeL4 (live timer data)   │
│    manual_lowerthird — uses manualLine1/2, manualScore           │
│    h2h.html          — uses h2hLeft/h2hRight                     │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2. State Schema

```json
{
  "── COMPETITION ENGINE FIELDS (auto, written by sync_comp_to_broadcast) ──": {},
  "lanes":          [{"num":1,"name":"ATHLETE","detail":"U90","score":"48.5"}],
  "laneCount":      4,
  "athletes":       [{"name":"...","score":"...","category":"U90","origin":""}],
  "categories":     [{"name":"U90","color":"#F5C842","athleteCount":8}],
  "eventName":      "ATLAS STONES",
  "eventNum":       "EVENT 4 OF 6",
  "eventSub":       "HEAT 2 · U90",
  "compCategory":   "U90",
  "compEvent":      4,
  "compHeat":       2,
  "compEventName":  "Atlas Stones",
  "eventMetric":    null,
  "eventType":      "reps",
  "data_source_mode": "engine",

  "── CHAMP FIELDS (auto, written when event complete) ──": {},
  "champName":       "THOR BJORNSSON",
  "champDetail":     "U90",
  "champScore":      "48.5",
  "champScoreLabel": "TOTAL POINTS",
  "champEvents":     "EVENT 4 OF 6",
  "champEyebrow":    "ATLAS STONES",
  "champComp":       "U90 CATEGORY",

  "── DIRECTOR OVERRIDE FIELDS (set by /director/lb, persist independently) ──": {},
  "lbCategory":         "U80",
  "lbCategoryOverride": true,
  "lbAthletes":         [{"name":"...","score":"...","category":"U80"}],

  "── BROADCAST CONTROL FIELDS (set by /update via control.html) ──": {},
  "scorebug":           false,
  "leaderboard":        true,
  "lowerThirdVisible":  true,
  "eventBarVisible":    true,
  "champion":           false,
  "h2h":                false,
  "lineup":             false,
  "repsVisible":        false,
  "lightsVisible":      false,
  "manualVisible":      false,
  "manualLine1":        "",
  "manualLine2":        "",
  "manualScore":        "",
  "manualUnit":         "",
  "compName":           "IRON CHALLENGE 2025",
  "compSub":            "STRONGMAN SERIES",
  "scoreUnit":          "PTS",
  "lineupIndex":        0,
  "lineupAuto":         true,
  "lineupDuration":     5,
  "h2hLeft":            {"name":"","origin":"","category":"","photoUrl":""},
  "h2hRight":           {"name":"","origin":"","category":"","photoUrl":""},

  "── LIVE JUDGE STATE (set by /update from judge panels) ──": {},
  "judgeL1": {"reps":0,"light":"none","timerRunning":false,"timerRemaining":60,"timerSecs":60,"primaryValue":"","secondaryValue":""},
  "judgeL2": "...",
  "judgeL3": "...",
  "judgeL4": "...",

  "── ENGINE CONFIG (persisted, read by comp engine) ──": {},
  "competition_config": {"lanes": 4},
  "category_order": ["U90","U80","U70","U65","Open","Masters"],
  "category_start_counts": {},
  "competition_status": {}
}
```

---

## 3. Data Flow

### Normal Timer Tick (no score change)
```
Timer fires in judge.html
  │
  └─► POST /update {judgeL1: {timerRemaining: 59, ...}}
        │
        └─► server: merge into state.json, _sse_notify()
              │
              └─► SSE /stream sends new payload
                    │
                    └─► All overlays: Object.assign(state, data); render()
                          │
                          └─► Hash check: _lastLanesKey, _lastScrollKey, etc.
                                Only redraws changed elements
```

### Score Entry (athlete finishes)
```
Judge POST /update {judgeL2: {primaryValue: "48.5"}}
  │
  └─► _apply_judge_scores_to_raw_results()
        │
        └─► DB write (raw_results), _invalidate_results_cache()
              │
              └─► sync_comp_to_broadcast()
                    │
                    ├─► get_leaderboard(category=compCategory) → athletes
                    ├─► compute lbAthletes from lbCategoryOverride/lbCategory
                    └─► atomic write to state.json → _sse_notify()
```

### Director Selects Leaderboard Category
```
Director clicks "U80" in control.html dropdown
  │
  └─► POST /director/lb {category:"U80", override:true}
        │
        └─► get_leaderboard(category="U80") → lb_athletes
              │
              └─► state.json: lbCategory="U80", lbCategoryOverride=true, lbAthletes=[...]
                    │
                    └─► _sse_notify()
                          │
                          └─► leaderboard.html: lbAthletes = U80 athletes → renders
                                (scorebug still shows U90 — scoring operator's category)
```

### Director Resets to Auto
```
Director clicks "↺ Auto" in control.html
  │
  └─► POST /director/lb {category:"auto", override:false}
        │
        └─► get_leaderboard(category=compCategory) → lb_athletes
              │
              └─► state.json: lbCategoryOverride=false, lbAthletes follows comp
```

---

## 4. AUTO vs MANUAL Overlay Modes

| Overlay | Mode | Follows | Can Override |
|---------|------|---------|--------------|
| `leaderboard.html` | AUTO or MANUAL | `lbAthletes` (comp category or director pin) | ✅ via `/director/lb` |
| `lowerthird.html` | AUTO | `lanes` (live heat) | ❌ (manual_lowerthird.html for manual) |
| `scorebug.html` | AUTO | `athletes` (scoring category) | ❌ |
| `champion.html` | MANUAL | `champName/Score/etc` | ✅ via `/update` from control.html |
| `lineup.html` | MANUAL | `lineup`, `lineupIndex` | ✅ via `/update` |
| `h2h.html` | MANUAL | `h2hLeft/Right` | ✅ via `/update` |
| `manual_lowerthird.html` | MANUAL | `manualLine1/2/Score` | ✅ via `/update` |
| `reps.html` | AUTO | `judgeL1…4` (live judge) | ❌ |

### Director Override Principle
- **AUTO** = field auto-updates from competition engine on every `sync_comp_to_broadcast()`
- **MANUAL** = broadcast director has explicitly set the value; engine syncs do NOT overwrite it
- Override persists in `state.json` until the director explicitly resets it
- Control.html shows **AUTO** (green) / **MANUAL** (gold) badge for each director-controlled field

---

## 5. Protected Routes

| Category | Routes | Auth Required |
|----------|--------|---------------|
| Public overlays | `/*.html` (overlay files), `/stream`, `/state.json` | Beta token only |
| Scoring | `/update`, `/judge/*` | Beta + comp session |
| Control | `/control.html`, `/judge.html`, `/judge-master.html`, `/debug.html` | Beta + comp session |
| Director | `/director/lb` | Beta + comp session |
| Comp engine | `/comp/*` routes | Beta + comp session |

---

## 6. Performance Architecture

### SSE Payload Cache
`_get_sse_payload(version)` — payload string built once per `_sse_version`, shared across all N connected clients. Eliminates N disk reads + N `json.dumps()` per SSE event.

### Hash-Based Dirty Checking
Every overlay file uses a hash fingerprint to skip DOM operations when data hasn't changed:

| File | Hash Variable | What It Guards |
|------|--------------|----------------|
| `scorebug.html` | `_lastScrollKey` | `#bug-scroll` innerHTML — CSS animation reset |
| `leaderboard.html` | `_lastLbKey` | `.lb-row` innerHTML — CSS transition reset |
| `lowerthird.html` | `_lastLanesKey` | `animOut(animIn)` — 700ms animation |
| `champion.html` | `_champVisible` | `spawnParticles()` — 40 DOM elements |
| `reps.html` | `_builtLaneCount` | `buildStructure()` — structure rebuild |
| `lineup.html` | `_lastCatKey` | `showCategory()` — fade animation |
| `control.html` | `_syncHashes` | `renderLaneConfig()`, `renderAthleteTable()`, `initInputs()` |

### Results Cache
`_get_cached_results()` — SQLite leaderboard recomputed only when `_results_dirty = True`. Timer ticks that write an unchanged score value do NOT invalidate the cache.

### Debouncing
- `_debouncedSync` in control.html: 80ms — absorbs rapid SSE bursts
- `_debouncePush` in control.html: 600ms — prevents rapid pushUpdate spam
- `setInterval(syncFromCompEngine, 30000)` — 30s fallback if SSE drops

---

## 7. File Map

| File | Type | Role |
|------|------|------|
| `server.py` | Python/Flask | Competition engine + broadcast API |
| `app/config.py` | Python | Constants, category config, event types |
| `app/sse.py` | Python | SSE condition + notify |
| `app/database.py` | Python | SQLite connection + schema |
| `app/utils.py` | Python | Pure helpers |
| `app/beta_auth.py` | Python | Sitewide beta gate |
| `state.json` | JSON | Broadcast state store |
| `sse-client.js` | JS | Drop-in SSE overlay client |
| `control.html` | HTML/JS | Broadcast director console |
| `leaderboard.html` | OBS overlay | Top-10 standings |
| `scorebug.html` | OBS overlay | Scrolling ticker |
| `lowerthird.html` | OBS overlay | Lane nameplates (auto-follows heat) |
| `champion.html` | OBS overlay | Event winner display |
| `reps.html` | OBS overlay | Live rep counters + lights |
| `lineup.html` | OBS overlay | Category intro graphic |
| `h2h.html` | OBS overlay | Head-to-head comparison |
| `manual_lowerthird.html` | OBS overlay | Single manual nameplate |
| `judge.html` | Score entry | Judge rep/score input panel |
| `judge-master.html` | Score entry | All-lanes judge panel |
| `templates/comp_*.html` | Jinja2 | Competition engine admin pages |
