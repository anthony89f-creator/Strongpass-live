# StrongPass — Phase A: State, Protocol, Orchestration & Runtime
**Phase:** Architecture — Foundations
**Date:** 2026-05-16
**Status:** Design artefact. No implementation code.
**Predecessor:** `SYSTEM_DESIGN.md` (high-level definitions)
**Audience:** Future Claude sessions, the operator, contributors. Treat as the canonical reference for state, protocol, orchestration, runtime, and operator surfaces.

---

## How to read this document

Five **system designs** (Part I → V) followed by **deliverables** (Part VI).

| Part | Subject                                | Anchor                     |
|------|----------------------------------------|----------------------------|
| I    | State architecture                     | what is true / derived / ephemeral |
| II   | Command & event protocol               | wire contracts             |
| III  | Broadcast orchestration                | PGM/PRE, scene graph, transitions |
| IV   | Overlay runtime                        | scene mounting, GPU-safe paint |
| V    | UI & operator system                   | surfaces, tokens, language |
| VI   | Diagrams, wireframes, protocol spec, roadmap | reference artefacts |

Vocabulary follows broadcast-engineering convention (PGM/PRE/AUX, TAKE, AUTO, DSK, ASTON, VTR, bug, super, key). Where StrongPass already has a synonym (e.g. "lower-third" / "lowerthird.html") both terms are used.

---

# PART I — STATE ARCHITECTURE

State is the single subject most likely to bite us under live conditions. This part fixes the model.

## 1. The four state planes

State separates into four planes by *who owns mutation rights*, *what loss looks like*, and *what reconstructs it*. The planes never share storage and never write into each other.

```
                           PLANE                  OWNS        DURATION       LOSS RECOVERY
        ┌─────────────────────────────────────────────────────────────────────────────┐
        │  1. AUTHORITATIVE   competition truth      Engine     forever         backup │
        │  2. BROADCAST       what is on-air         Director   event           snapshot+events │
        │  3. EPHEMERAL       runtime caches/timers  Server     process         recomputed │
        │  4. SESSION         per-operator UI prefs  Operator   browser         lost; ok │
        └─────────────────────────────────────────────────────────────────────────────┘
```

### 1.1 Plane 1 — Authoritative competition state

The only state that is **the truth**. Everything else either derives from it or is staged on top of it.

**Contents**
- Athletes (id, name, category, status)
- Events (number, name, scoring_type, order, presets)
- Heats (event_id, category, heat_number, lane, athlete_id)
- `raw_results` (athlete_id, event_id, primary, secondary, judge_id, principal, ts, tiebreak)
- Competition state row (active event, active heat, active category, comp status)
- Category order & start counts
- Comp meta (name, sub, score_unit, lane_count)
- Append-only `audit_log` (every state-changing event)

**Store:** SQLite WAL today (`comp.db`); Postgres post-multi-tenant. WAL-checkpointed every N minutes.

**Owns:** Competition domain.
**Writers:** Only `Competition.handle(command)`.
**Readers:** Projection workers, replay engine, public APIs.
**Loss recovery:** Restored from filesystem backup. If filesystem itself is lost, restore from off-host nightly backup. Loss tolerance: **zero** for results.
**Replayable:** Yes — `audit_log` reconstructs final state from any earlier snapshot.

### 1.2 Plane 2 — Broadcast state

What is currently on-screen, plus what is *staged* (preview), plus the director's manual overrides. Distinct from competition truth — the director can override what is shown without rewriting history.

**Contents**
- **PGM** (program — currently on-air scene + per-overlay visibility & content)
- **PRE** (preview — staged scene, not yet on-air)
- **Director overrides** (lbCategoryOverride, manual fields, h2h slots, champion slot)
- **Scene queue** (cued items in order)
- **DSK/bug state** (persistent sponsor bug, comp eyebrow text)
- **AUTO/MANUAL flags** per controlled field
- **Restart token** (process start epoch)

**Store:** `broadcast_state.json` (PGM) + `preview_state.json` (PRE), both atomic-written. Periodic snapshot to `/snapshots/`. Audit log of every Broadcast event.

**Owns:** Broadcast domain.
**Writers:** Only `Broadcast.handle(command)`.
**Readers:** SSE fanout to overlays + director console.
**Loss recovery:** Re-derived from (latest snapshot) + (Broadcast events since snapshot). Loss tolerance: a few seconds of director input is acceptable; never a wrong on-air state.
**Replayable:** Yes — replay of Broadcast events from snapshot rebuilds PGM/PRE.

### 1.3 Plane 3 — Ephemeral runtime state

In-process state that exists only while the server is running. Loss is *expected* on restart and must not damage truth.

**Contents**
- Open SSE connections (client list, last-version per client, last-seen heartbeat)
- Projection caches (`_results_cache`, hash fingerprints)
- Locks, semaphores, dirty bits
- Active timers (lane timers — but values mirrored into Plane 2 each tick)
- Rate-limit counters
- In-flight command idempotency window

**Store:** Process memory only. Never written to disk.

**Owns:** Each subsystem owns its own runtime state.
**Writers:** That subsystem.
**Readers:** That subsystem.
**Loss recovery:** Recomputed on demand (projection caches) or rebuilt at restart (timers re-read from Plane 2 / Plane 1).
**Replayable:** Not directly — but every ephemeral structure has a deterministic re-derivation from Planes 1+2.

### 1.4 Plane 4 — Operator session state

Per-operator preferences and ephemeral UI choices. Lives in the browser. No server storage.

**Contents**
- Director's panel layout, collapsed/expanded sections
- Selected leaderboard *preview* category in the director console (separate from broadcast pin)
- Confirmation-dialog "don't ask again" toggles
- Theme preference (if we add light theme)
- Last-used lane (for judge devices)
- Connection diagnostics panel open/closed

**Store:** `localStorage` per surface.

**Owns:** Operator's browser.
**Writers:** Operator UI.
**Readers:** Operator UI.
**Loss recovery:** None needed — defaults are good.
**Replayable:** Not relevant.

### 1.5 Plane → store mapping (target)

```
/comp.db                       Plane 1 — authoritative competition + audit_log
/broadcast/state.pgm.json      Plane 2 — PGM (on-air)
/broadcast/state.pre.json      Plane 2 — PRE (preview)
/broadcast/queue.json          Plane 2 — cued scene queue
/snapshots/YYYYMMDD-HHMMSS/    Plane 1+2 periodic combined snapshots
/audit/events-NNNN.jsonl       Plane 1+2 append-only event log (rotated by size)
/projections/{name}.json       Plane 3 — projection cache mirrors (optional disk)
(browser localStorage)         Plane 4
```

Today everything is in `state.json` + `comp.db`. The split above is the target.

---

## 2. Projections (read models)

A projection is a **denormalised view** materialised from authoritative state and made available to a specific consumer. It is the only thing overlays ever consume.

### 2.1 Projection registry

| Projection                    | Source                            | Consumer                                        | Invalidated by                                | Version key      |
|-------------------------------|-----------------------------------|--------------------------------------------------|------------------------------------------------|------------------|
| `proj.lanes`                  | heats + raw_results + judgeLN     | lower-third, scorebug, reps overlays            | HeatAdvanced, ScoreRecorded, judge tick events | `lanes.version`  |
| `proj.athletes.{category}`    | raw_results for category          | scorebug, leaderboard preview                   | ScoreRecorded in category                      | `ath.{cat}.v`    |
| `proj.leaderboard.{category}` | raw_results aggregated per cat    | leaderboard overlay, public results page        | ScoreRecorded in category                      | `lb.{cat}.v`     |
| `proj.eventState`             | competition_state row             | every overlay (eventName/Num/Sub strings)       | HeatAdvanced, EventAdvanced                    | `ev.v`           |
| `proj.champion.{eventNumber}` | computed when event complete      | champion overlay                                | EventCompleted                                 | `champ.{e}.v`    |
| `proj.heatGrid`               | heats + athletes                  | `/comp/run` admin                               | HeatAdvanced, athlete reassignment             | `grid.v`         |
| `proj.lineup.{category}`      | athletes for category             | lineup overlay                                  | athlete add/remove in category                 | `lineup.{cat}.v` |
| `proj.publicResults.{cat}`    | per-event breakdown               | public `/api/results/<cat>`                     | any ScoreRecorded in cat                       | `pub.{cat}.v`    |

### 2.2 Projection contract

Every projection conforms to:

```jsonc
{
  "name": "proj.leaderboard.U90",
  "version": 847,            // monotonic per-projection
  "computedAt": "2026-05-16T14:33:22.918Z",
  "restartToken": 31822,     // process epoch
  "data": { /* projection payload */ }
}
```

Rules:
- Computed lazily: never on read of an unchanged version; recomputed only when invalidating events fire.
- Served from in-memory cache; optional disk-mirrored copy for cold-start.
- Each projection declares its **invalidation set** (the events that dirty it). The event bus dispatches to projections by this set.
- Projections must be **pure functions** of authoritative + broadcast state at a given event version. No clocks, no randomness.

### 2.3 What is *not* a projection

- Director overrides (Plane 2) — these are state, not derivations.
- Ephemeral runtime caches (Plane 3) — these are mechanism, not contract.
- Operator UI state (Plane 4) — never leaves the browser.

---

## 3. Snapshots

A snapshot is a **point-in-time, internally consistent capture** of Planes 1 + 2 with the matching `version` of the audit stream.

### 3.1 Snapshot triggers

- **Periodic**: every 5 minutes during a live event (configurable).
- **On heat advance**: bracketing each heat (before + after).
- **On event advance**: between events.
- **On demand**: `broadcast.takeSnapshot` command.
- **On graceful shutdown**: pre-stop hook.

### 3.2 Snapshot contents

```jsonc
{
  "snapshotId": "snap-2026-05-16T14-30-00-7f2a",
  "createdAt": "2026-05-16T14:30:00.018Z",
  "restartToken": 31822,
  "auditCursor": 18922,           // last event_id included
  "competition": {
    "dbBackupRef": "snap/comp-18922.db",     // sqlite VACUUM INTO
    "schemaVersion": 7
  },
  "broadcast": {
    "pgm": { /* full PGM state */ },
    "pre": { /* full PRE state */ },
    "queue": [ /* cued items */ ],
    "overrides": { /* MANUAL fields */ }
  },
  "projections": {
    "proj.lanes":         { "version": 902, "data": { /* … */ } },
    "proj.leaderboard.*": { /* one per category */ }
  }
}
```

### 3.3 Snapshot retention

- Last 24 hours: every snapshot.
- 1–7 days old: hourly.
- 7+ days: daily.
- Per-event: pinned final-state snapshot retained forever.

### 3.4 Snapshot operations

- **Restore** — replace current Plane 1 + Plane 2 with snapshot contents. Used for full disaster recovery.
- **Branch** — load snapshot into a *parallel* runtime (staging env) for rehearsal without touching production.
- **Diff** — compare two snapshots; powers the "what changed in this heat?" director view.

---

## 4. Event versioning

Three version counters. Each is a monotonically increasing 64-bit integer scoped to a stream, reset only by `restartToken` rotation.

| Counter                | Increments on                              | Scope                |
|------------------------|--------------------------------------------|----------------------|
| `competition.version`  | every committed Competition event          | global               |
| `broadcast.version`    | every committed Broadcast event            | global               |
| `judging.version`      | every committed Judging event              | global               |
| `proj.{name}.version`  | every recompute of the named projection    | per projection       |

All four are emitted in every SSE payload so a client can detect any missed events.

### 4.1 Version derivation

Two principles:

1. **Server is authoritative.** Clients never assign versions. A command request has no version; the response carries the resulting version.
2. **No gaps.** If a client sees `version=N+2` after `version=N`, it requests a resync (because something was missed).

### 4.2 Restart token

A `restartToken` is the process-start epoch (unix ms). It changes on every server restart. Clients store it. On mismatch, they treat their cached versions as void and request a full resync.

This is the *one* lever that bypasses gap detection — a restart is an explicit version reset.

### 4.3 Why not vector clocks

The system is single-writer per stream (only the server commits). There is no need for vector clocks or CRDTs. A single monotonic integer per stream is enough and is cheap to reason about.

---

## 5. Reconnect semantics

A reconnect is the most common "edge case" — every overlay reconnects multiple times during a normal event (OBS browser source refresh, Wi-Fi blip, nginx reload).

### 5.1 Reconnect protocol

```
Client opens /stream with:
  ?subscribe=lanes,leaderboard.U90,eventState
  &since.broadcast=812
  &since.competition=4421
  &since.judging=918
  &since.proj.lanes=901
  &restartToken=31822
```

Server response, three branches:

**A. Restart token matches AND no gaps:**
- Stream `version > since` events for subscribed slices.
- Client applies in order. Done.

**B. Restart token matches BUT client claims a `since` newer than server's current version:**
- Reject with `{code: "client_ahead", currentVersion: N}`.
- Client must have hallucinated state; force full resync.

**C. Restart token mismatched OR `since` older than the audit log tail:**
- Server sends a **full resync** frame containing all subscribed projections at current version.
- Client treats this as authoritative; resets its caches.

### 5.2 The audit-log tail

The server keeps a **rolling window of recent events in memory** (last 5 minutes, configurable). Reconnects within this window can stream deltas without disk reads. Reconnects past the window go to full resync.

### 5.3 Heartbeat

- Server sends `:heartbeat` comment every 15 seconds on every SSE stream.
- Client treats 30s without heartbeat as a disconnect → reconnect.
- Keepalive is at the SSE-protocol level — never carries data.

### 5.4 Reconnect storm protection

When the server restarts, every client reconnects within ~2 seconds. Mitigations:

- Clients use exponential backoff with jitter (`200ms * 2^attempt + rand(0..500ms)`).
- Server rate-limits resync requests at 100/sec; excess responds with `503` + `Retry-After`.
- Full-resync payloads are cached at the server per-`(subscription, version)` for the burst window (30s).

---

## 6. Replay & rollback

This is the system that lets us answer "what happened in heat 3?" — and undo a wrong entry without rewriting history.

### 6.1 The audit log

Every committed event lands in `audit/events-NNNN.jsonl`. Rotated by size. Never mutated.

Schema (one event per line):

```jsonc
{
  "auditId": 18923,
  "ts": "2026-05-16T14:33:22.918Z",
  "stream": "competition",
  "version": 4422,
  "principal": { "kind": "operator", "id": "op-tony", "session": "s-91ac" },
  "cmdId": "cmd-2c4e8a",
  "event": "Competition.ScoreRecorded",
  "payload": { "athleteId": 17, "eventId": 4, "primary": "48.5" }
}
```

### 6.2 Replay modes

Three distinct user-facing modes; one underlying mechanism (replay events from audit log onto a chosen base).

| Mode             | Use case                                    | Surface                      |
|------------------|---------------------------------------------|------------------------------|
| **Forensic**     | "Show me state at 14:32:00"                  | Scoring `/comp/audit`        |
| **Rehearsal**    | Play a recorded event into staging          | CLI tool                     |
| **Rollback**     | Undo last N Competition events               | Scoring with confirmation    |

### 6.3 Rollback semantics

Rollback **never deletes history**. It writes a new compensating event.

For each event being rolled back:
- `Competition.ScoreRecorded` → `Competition.ScoreRetracted`
- `Competition.HeatAdvanced` → `Competition.HeatRewound`
- `Broadcast.SceneTaken` → reload prior PGM via `Broadcast.SceneReverted`

The audit log shows both the original event and the compensating event with linked `cmdId`s. The "state at time T" view excludes events with a retraction.

### 6.4 Rollback authorisation

Authority to rollback is role-gated:
- Lane judge: cannot rollback anything.
- Master judge: can request rollback within their current heat.
- Scoring operator: can rollback within active event with confirmation.
- Owner / referee: can rollback any event with confirmation + reason captured.

The reason is mandatory and stored in the compensating event payload.

### 6.5 Replay determinism

Pure replay is deterministic *if* projection code is pure and the snapshot baseline is correct. Sources of non-determinism we must rule out:
- Wall-clock reads in projection code → forbidden. Use event `ts` from the audit log.
- Random IDs in projection code → forbidden. Use deterministic IDs only.
- External calls in projection code → forbidden. Projections are closed.

The CI suite contains a "replay test": run a recorded competition event-by-event from snapshot, assert final state hash matches the captured final state.

---

## 7. State recovery flow

The single most important diagram in this document: how a process starts back up and reaches a correct on-air state.

```
process starts
   │
   ▼
[1] read latest snapshot from /snapshots/
       │ found?
       │
       ├── yes ─► hydrate Plane 1 (DB), Plane 2 (PGM/PRE/queue), projections
       │
       └── no  ─► hydrate from disk:
                  - comp.db (Plane 1)
                  - broadcast/state.pgm.json (Plane 2)
                  - broadcast/state.pre.json (Plane 2)
                  - rebuild all projections from raw_results
   │
   ▼
[2] read audit log from snapshot.auditCursor → tail
       │
       └─► replay each event:
             - update competition.version / broadcast.version / judging.version
             - apply to projections
             - DO NOT re-emit to SSE (no clients yet)
   │
   ▼
[3] mint new restartToken = now()
   │
   ▼
[4] open /stream, start accepting commands
   │
   ▼
[5] first SSE event includes new restartToken → connected clients hard-resync
```

Crash-only design: the only "shutdown" path is **immediate process kill**. There is no "clean shutdown" that the recovery flow can rely on. Anything mid-flight must be re-derivable from disk.

---

## 8. Synchronisation strategy

How the three running components — Engine, Director console, Overlays — stay in agreement.

### 8.1 Server-authoritative

There is **one** writer per stream. Clients are pure mirrors of server-confirmed state. No client-side prediction, no optimistic state, no merge logic.

This is non-negotiable for a broadcast system. The cost of a mis-merged "phantom score" on air is higher than the cost of a 200ms input latency.

### 8.2 Hash-based dirty checking

Already implemented for overlays. Codify as a system rule:

> Every render function takes a slice of state, computes a stable fingerprint, and short-circuits if the fingerprint matches the last render.

The fingerprint algorithm is per-overlay (see Part IV §17.3) but always: deterministic, cheap, and ignores fields the overlay does not consume.

### 8.3 Optimistic UI on the director console — *with rollback*

The one exception to "no client prediction": the director console may render the *intent* of a command immediately (button depresses, scene picker highlights new scene). If the command is rejected, the UI rolls back with a brief flash. **Overlays never see optimistic state** — they only see committed state.

### 8.4 Conflict resolution

Per-domain rules:

| Domain        | Conflict rule                                                                                  |
|---------------|------------------------------------------------------------------------------------------------|
| Competition   | First commit wins. Subsequent conflicting commands are rejected with `{code: "stale_version"}`. |
| Broadcast     | Last director command wins (intentional — director is authority).                              |
| Judging       | Per-lane: last value wins until master judge confirms heat, after which scores are immutable.  |

### 8.5 Clock authority

The server clock is authoritative for `ts` on events. Client clocks are advisory only (used for UI relative timing like "submitted 3s ago"). NTP-sync on the server is mandatory.

---

## 9. Plane summary matrix

| Question                                | Plane 1 | Plane 2 | Plane 3 | Plane 4 |
|-----------------------------------------|---------|---------|---------|---------|
| Persisted across restart?               | yes     | yes     | no      | no (browser local)|
| Owns truth?                             | yes     | yes (for broadcast) | no | no |
| Derivable from another plane?           | no      | partially (overrides aren't) | yes | no |
| Replayable from audit log?              | yes     | yes     | yes     | no      |
| Visible to overlays?                    | via projections | via SSE | no | no |
| Visible to public API?                  | via projections | no | no | no |
| Mutated by client request?              | via commands | via commands | no | yes (local) |

---

# PART II — COMMAND & EVENT PROTOCOL

## 10. Conventions

### 10.1 Naming

**Commands** — imperative, dotted, lowerCamel within segments:
```
{domain}.{verb}{Noun}
  competition.recordScore
  competition.advanceHeat
  broadcast.takeScene
  broadcast.cueScene
  broadcast.pinLeaderboard
  judging.startTimer
  judging.submitLane
  identity.openSession
```

**Events** — past-tense, dotted, PascalCase noun phrase:
```
{Domain}.{NounPastTense}
  Competition.ScoreRecorded
  Competition.HeatAdvanced
  Competition.EventCompleted
  Broadcast.SceneTaken
  Broadcast.SceneCued
  Broadcast.LeaderboardPinned
  Judging.TimerStarted
  Judging.LaneSubmitted
  Identity.SessionEstablished
```

A command and the event(s) it produces share `cmdId` for traceability:
```
cmd-91ac4e → emits Competition.ScoreRecorded(version=4422)
           → emits Projection.Invalidated(name="proj.leaderboard.U90")
```

### 10.2 Required fields

Every command:
```jsonc
{
  "cmdId":     "cmd-91ac4e",           // client-supplied UUIDv7, 30s idempotency window
  "issuedAt":  "2026-05-16T14:33:22.701Z",
  "principal": { "kind":"operator", "id":"op-tony", "session":"s-91ac" },
  "domain":    "competition",
  "name":      "recordScore",
  "expected":  { "competition.version": 4421 },   // optional, for optimistic-lock
  "payload":   { /* command-specific */ }
}
```

Every event:
```jsonc
{
  "auditId":       18923,
  "stream":        "competition",
  "version":       4422,
  "ts":            "2026-05-16T14:33:22.918Z",
  "principal":     { "kind":"operator", "id":"op-tony", "session":"s-91ac" },
  "cmdId":         "cmd-91ac4e",
  "event":         "Competition.ScoreRecorded",
  "payload":       { /* event-specific */ },
  "causedBy":      null,                  // optional — parent event for derived events
  "compensates":   null                   // optional — set on rollback events
}
```

---

## 11. Command contracts

A non-exhaustive list of the contracts that matter most. Each lists payload, validation, emitted events.

### 11.1 Competition

#### `competition.recordScore`
```jsonc
{
  "payload": {
    "athleteId": 17,
    "eventId":   4,
    "primary":   "48.5",
    "secondary": null,
    "judgeId":   "j-lane2",
    "source":    "judge_panel"
  }
}
```
**Validation:** athlete exists, event exists, athlete is assigned to a lane in *current* heat of event, primary parses for the event's scoring type, athlete is not DNS/DNF, master judge has not yet sealed this heat.
**Emits:** `Competition.ScoreRecorded`, `Projection.Invalidated` (lanes, athletes.{cat}, leaderboard.{cat}, publicResults.{cat}).

#### `competition.advanceHeat`
```jsonc
{ "payload": {} }
```
**Validation:** all lanes in current heat have either a score or DNF/DNS, master judge has sealed the heat.
**Emits:** `Competition.HeatAdvanced`. If last heat of event: also `Competition.EventAdvanced` and (after champion compute) `Competition.EventCompleted` + `Competition.ChampionDetermined`.

#### `competition.declareDnf` / `competition.declareDns`
```jsonc
{ "payload": { "athleteId": 17, "eventId": 4, "reason": "withdrew" } }
```
**Validation:** athlete is assigned in event.
**Emits:** `Competition.ScoreRecorded` with `primary: "DNF"` (the projection layer handles ranking rules).

#### `competition.retractScore`
```jsonc
{ "payload": { "auditId": 18923, "reason": "judge error — wrong lane" } }
```
**Validation:** caller has rollback role; original event exists and not already compensated.
**Emits:** `Competition.ScoreRetracted` with `compensates: 18923`.

### 11.2 Broadcast

#### `broadcast.cueScene`
Stage a scene in PRE. Does not affect PGM.
```jsonc
{
  "payload": {
    "sceneId": "scene.champion",
    "params": {
      "name":   "THOR BJORNSSON",
      "detail": "U90",
      "score":  "48.5",
      "label":  "TOTAL POINTS",
      "eyebrow":"ATLAS STONES",
      "compTag":"U90 CATEGORY"
    }
  }
}
```
**Validation:** scene exists, params match scene schema.
**Emits:** `Broadcast.SceneCued`.

#### `broadcast.takeScene`
Promote PRE → PGM with the scene's defined transition.
```jsonc
{ "payload": { "transition": "auto" } }   // "auto" | "cut" | "fade"
```
**Validation:** PRE is non-empty.
**Emits:** `Broadcast.SceneTaken`, then per-overlay `Broadcast.OverlayShown` / `OverlayHidden`.

#### `broadcast.cutToBlack`
Emergency — immediately clear all overlays.
```jsonc
{ "payload": {} }
```
**Emits:** `Broadcast.CutToBlack`, then `Broadcast.OverlayHidden` for every overlay.

#### `broadcast.pinLeaderboard`
```jsonc
{ "payload": { "category": "U80", "override": true } }
```
**Emits:** `Broadcast.LeaderboardPinned`, `Projection.Invalidated` (leaderboard view).

#### `broadcast.setBug` / `broadcast.setAston`
DSK channels — bug (persistent corner graphic) and ASTON (commentator-set lower-third).
```jsonc
{ "payload": { "text": "Round 4 of 6", "duration": null } }
```

### 11.3 Judging

#### `judging.startTimer` / `stopTimer` / `resetTimer`
```jsonc
{ "payload": { "lane": 2, "duration": 60 } }
```
**Emits:** `Judging.TimerStarted` / `TimerStopped` / `TimerReset`. Timer state is then ticked server-side and mirrored into Plane 2 per tick.

#### `judging.setRepCount`
```jsonc
{ "payload": { "lane": 2, "n": 5 } }
```
**Emits:** `Judging.RepCountSet`.

#### `judging.setLight`
```jsonc
{ "payload": { "lane": 2, "verdict": "good" } }
```
**Emits:** `Judging.LightSet`.

#### `judging.submitLane`
Promotes the lane's working values into a `competition.recordScore`.
```jsonc
{ "payload": { "lane": 2 } }
```
**Emits:** `Judging.LaneSubmitted`, *causes* `Competition.ScoreRecorded`.

#### `judging.sealHeat`
Master-judge-only. Locks all lanes in current heat as final.
```jsonc
{ "payload": {} }
```
**Emits:** `Judging.HeatSealed`. After this, `competition.recordScore` is rejected for any athlete in this heat.

---

## 12. Event contracts

The reverse direction — events emitted *by* the engine. Two examples in full:

```jsonc
// Competition.ScoreRecorded
{
  "auditId": 18923,
  "stream": "competition",
  "version": 4422,
  "ts": "2026-05-16T14:33:22.918Z",
  "principal": { "kind":"operator", "id":"op-tony", "session":"s-91ac" },
  "cmdId": "cmd-91ac4e",
  "event": "Competition.ScoreRecorded",
  "payload": {
    "athleteId": 17,
    "athleteName": "BJORNSSON, T.",
    "eventId": 4,
    "eventName": "Atlas Stones",
    "category": "U90",
    "heatNumber": 2,
    "lane": 2,
    "primary": "48.5",
    "secondary": null,
    "scoringType": "reps",
    "judgeId": "j-lane2"
  }
}
```

```jsonc
// Broadcast.SceneTaken
{
  "auditId": 18924,
  "stream": "broadcast",
  "version": 813,
  "ts": "2026-05-16T14:33:24.002Z",
  "principal": { "kind":"operator", "id":"op-tony", "session":"s-91ac" },
  "cmdId": "cmd-7d20fe",
  "event": "Broadcast.SceneTaken",
  "payload": {
    "from": "scene.heat",
    "to":   "scene.champion",
    "transition": "auto",
    "transitionDurationMs": 700,
    "params": {
      "name":"THOR BJORNSSON","detail":"U90","score":"48.5",
      "label":"TOTAL POINTS","eyebrow":"ATLAS STONES","compTag":"U90 CATEGORY"
    }
  }
}
```

---

## 13. Event lifecycle

```
COMMAND ARRIVES (HTTP POST /command/{domain}/{name})
   │
   ▼
[1] Identity gate — beta + role check, attach principal
   │
   ▼
[2] Schema validation — payload conforms to command contract
   │
   ▼
[3] Idempotency check — cmdId seen within 30s? return cached response
   │
   ▼
[4] Domain handler runs:
       - validates against current Plane 1/2 state
       - emits zero or more events
       - aborts on validation fail → 4xx with error code
   │
   ▼
[5] Events committed to audit log (append-only, fsync)
       - assigned monotonic versions
       - assigned auditIds
   │
   ▼
[6] Projection bus dispatches:
       - subscribers run invalidations
       - projection caches recompute lazily (next read)
   │
   ▼
[7] SSE fanout:
       - changed projections + state slices serialised
       - delivered to subscribed clients per their filter
   │
   ▼
[8] Response to caller:
       - { ok: true, version: N, eventsEmitted: [...], elapsedMs: 12 }
```

---

## 14. Ordering & guarantees

| Guarantee                             | Holds?                                                       |
|---------------------------------------|---------------------------------------------------------------|
| Per-stream total order                | **Yes** — single writer per stream                            |
| Cross-stream ordering                 | Not preserved beyond `causedBy` link                          |
| Exactly-once per `cmdId`              | **Yes** — within 30s idempotency window                       |
| At-least-once delivery to SSE clients | **Yes** — via reconnect+resync                                |
| Exactly-once delivery to SSE clients  | No — but renders are idempotent, so duplicates are harmless   |
| Atomicity (event + audit + projection)| **Yes** — within a single request; failures roll back all     |

### 14.1 Optimistic locking

A command may include `expected.{stream}.version`. If the server's current version differs, the command is rejected `{code: "stale_version", current: N}`. Used for high-risk commands (heat advance, event advance, retraction).

### 14.2 Idempotency

Every command carries `cmdId`. The server keeps a `(cmdId → response)` cache for 30s. A duplicate `cmdId` returns the cached response without re-running the handler. Clients should regenerate `cmdId` for retries that *intentionally* mean a new attempt.

### 14.3 Conflict handling

Per Part I §8.4. Implementation note: each handler resolves conflicts itself; the protocol does not impose a single rule.

### 14.4 Failure recovery

If the audit-log fsync fails:
- The command is **rejected** with `5xx` before any projection or SSE side-effect.
- The handler must not have any side-effect that is not undone by the rollback.

If the projection or SSE step fails *after* audit commit:
- Audit commit stands (events are real, they happened).
- Projection rebuilds itself from authoritative + audit log.
- SSE clients eventually catch up via heartbeat/reconnect.

This is the "audit log is the line" principle: nothing is real until it's in the audit; once it's in the audit, it's real forever.

---

# PART III — BROADCAST ORCHESTRATION

This part designs the broadcast control layer with the rigor of a professional graphics system (Ross XPression, VizRT Viz Engine, ChyronHego). The names and concepts are deliberately drawn from that world.

## 15. Mental model: PGM / PRE / AUX

A live broadcast graphics engine maintains at least three parallel state buffers:

- **PGM (Program)** — what is currently on-air. The output you see in OBS.
- **PRE (Preview)** — what is staged, not yet on-air. The director composes this without affecting PGM.
- **AUX (Auxiliary)** — additional output buffers (recorded, monitor feeds). Used for archive and for the operator's confidence monitor.

The director's primary workflow is: **cue into PRE → review → TAKE → PGM updates**. This is the workflow every professional broadcast has had since the 1980s and the workflow StrongPass should adopt.

### 15.1 The TAKE

A TAKE is the atomic moment PRE becomes PGM. Five transition modes:

| Mode      | Description                                          | Default for          |
|-----------|------------------------------------------------------|----------------------|
| `cut`     | Instant swap, no animation                           | Emergency, sponsor bug |
| `fade`    | Cross-fade 400ms                                     | Scene-to-scene neutral |
| `wipe`    | Directional wipe 500ms                               | Lower-third change   |
| `auto`    | Each scene's defined transition (see §17.2)          | Default              |
| `roll`    | Plays a scripted sequence (the orchestrator runs)    | Champion reveal      |

### 15.2 The AUTO button

Separate from the `auto` transition mode: **AUTO mode** is whether the orchestrator is *cueing* scenes on the director's behalf based on competition events. Two settings:

- **AUTO**: the engine cues PRE automatically. E.g. when `Competition.HeatAdvanced` fires, the orchestrator cues `scene.heat` into PRE with the new heat's data, and (optionally) auto-TAKEs after a configurable delay.
- **MANUAL**: the director cues every scene by hand. PRE is whatever the director put there.

There is also a **per-overlay** AUTO/MANUAL flag (the existing director-override concept) — orthogonal to the global AUTO mode. The matrix:

| Global mode | Per-overlay  | Behaviour                                       |
|-------------|--------------|--------------------------------------------------|
| AUTO        | AUTO         | Fully automatic — engine drives                 |
| AUTO        | MANUAL       | Engine cues scenes; that overlay stays as director set it |
| MANUAL      | AUTO         | Director cues scenes; overlay still follows engine data when shown |
| MANUAL      | MANUAL       | Director cues and overrides — full manual       |

---

## 16. Scene graph

### 16.1 Concepts

- **Scene** — a named composite of layers with content bindings.
- **Layer** — a slot in a fixed z-order that holds at most one overlay at a time.
- **Overlay** — a renderable graphic (lower-third, leaderboard, etc.).
- **Slot** — a parameter on a scene (e.g. `championName`).
- **Binding** — a slot resolved to a concrete value (constant, projection field, or operator input).

### 16.2 Layer stack (z-order, bottom → top)

```
  z=0   DSK_BUG          sponsor bug, top-right (always-on if cued)
  z=1   EVENTBAR         event/heat/category band
  z=2   SCOREBUG         scrolling ticker
  z=3   LEADERBOARD      standings panel
  z=4   LOWERTHIRD       lane nameplates (auto or manual)
  z=5   REPS / LIGHTS    live judge state
  z=6   LINEUP           pre-event intro
  z=7   H2H              head-to-head spotlight
  z=8   CHAMPION         winner reveal
  z=9   ASTON            commentator caption (DSK, can fly over anything)
  z=10  CUT_TO_BLACK     emergency mask
```

Each layer has rules:
- **Exclusive layers** (LINEUP, H2H, CHAMPION) suppress all lower layers when visible.
- **Stackable layers** (DSK_BUG, EVENTBAR, SCOREBUG, LEADERBOARD, LOWERTHIRD, REPS) coexist.
- **Override layers** (ASTON, CUT_TO_BLACK) sit on top regardless of scene.

### 16.3 Scene catalogue

| Scene                 | Layers shown                                            | Suppressed                   | Auto-cue trigger                              |
|-----------------------|---------------------------------------------------------|-------------------------------|-----------------------------------------------|
| `scene.blank`         | (DSK_BUG only)                                          | all overlays                  | manual                                        |
| `scene.lineup`        | LINEUP                                                  | all except DSK_BUG, ASTON     | comp opens / between categories               |
| `scene.heat`          | EVENTBAR, SCOREBUG, LOWERTHIRD, REPS                    | LINEUP, H2H, CHAMPION         | HeatAdvanced                                   |
| `scene.heat+lb`       | EVENTBAR, LOWERTHIRD, REPS, LEADERBOARD                 | LINEUP, H2H, CHAMPION         | (operator preset)                              |
| `scene.result`        | EVENTBAR, LEADERBOARD                                   | LOWERTHIRD, REPS              | HeatSealed (configurable)                      |
| `scene.champion`      | CHAMPION                                                | all except DSK_BUG, ASTON     | EventCompleted                                 |
| `scene.h2h`           | H2H                                                     | LOWERTHIRD, REPS, LEADERBOARD | manual                                        |
| `scene.replay`        | LOWERTHIRD ("REPLAY"), EVENTBAR                         | REPS                          | manual                                        |
| `scene.sponsor`       | DSK_BUG full-bleed take                                 | (timer-driven)                | scheduled                                     |
| `scene.commentator`   | LOWERTHIRD (commentator-bound)                          | engine-driven LOWERTHIRD       | manual (from commentator tablet)              |

### 16.4 Scene definition (declarative)

A scene is a record:

```jsonc
{
  "id": "scene.champion",
  "version": 3,
  "layers": ["CHAMPION", "DSK_BUG"],
  "suppress": ["LOWERTHIRD","REPS","SCOREBUG","LEADERBOARD","EVENTBAR","LINEUP","H2H"],
  "slots": {
    "name":    { "required": true, "source": "operator|proj.champion.{eventNumber}.name" },
    "detail":  { "required": true, "source": "operator|proj.champion.{eventNumber}.detail" },
    "score":   { "required": true, "source": "operator|proj.champion.{eventNumber}.score" },
    "label":   { "required": true, "default": "TOTAL POINTS" },
    "eyebrow": { "required": false, "source": "proj.eventState.eventName" },
    "compTag": { "required": false, "source": "proj.eventState.categoryTag" }
  },
  "transitionIn":  { "kind": "roll", "stepsRef": "anim.champion.in"  },
  "transitionOut": { "kind": "roll", "stepsRef": "anim.champion.out" },
  "holdMs": 8000,
  "autoFollow": { "scene": "scene.heat", "afterHoldMs": 1500 }
}
```

`autoFollow` tells the orchestrator what to return to once the scene's hold expires.

---

## 17. Transitions

### 17.1 Transition ownership

Critical rule: **the scene owns its in/out animation**. The overlay implements the visual; the scene declares the timing and curve; the orchestrator runs the sequence.

This means a single overlay can be entered and exited differently by different scenes. It also means transitions are recorded in the audit log (each step is an event).

### 17.2 Sequenced transitions (the `roll` mode)

Long-form transitions (champion reveal) run as a sequence of steps each with timing:

```jsonc
{
  "id": "anim.champion.in",
  "steps": [
    { "atMs": 0,    "op": "hide", "layer": "SCOREBUG" },
    { "atMs": 0,    "op": "hide", "layer": "REPS" },
    { "atMs": 200,  "op": "animateOut", "layer": "LOWERTHIRD", "duration": 500 },
    { "atMs": 700,  "op": "show", "layer": "CHAMPION", "params": "{{sceneParams}}" },
    { "atMs": 700,  "op": "playEffect", "effect": "confetti" }
  ]
}
```

Each step emits a `Broadcast.OverlayTransitionStep` event. The audit log captures the full sequence — invaluable for "why did the lower-third flicker for 200ms?" post-mortems.

### 17.3 Suppression rules

When a scene declares `suppress: [X, Y, Z]`:
- Those layers receive a `forceHide=true` flag in the overlay payload.
- The overlay must hide regardless of its own visibility state.
- The prior visibility state of each suppressed layer is recorded in PGM so it can be restored on scene exit.

---

## 18. The cue queue

The orchestrator maintains an **ordered queue** of cued items waiting for TAKE. Used for:
- "Cue the next 3 lineups" (pre-show)
- "Cue replay clip, then return to heat scene" (recorded reveal)
- "Cue sponsor bug rotation" (timer-driven)

```jsonc
{
  "queue": [
    { "id": "q-001", "sceneId": "scene.h2h",      "cueAt": "manual",      "params": { /* ... */ } },
    { "id": "q-002", "sceneId": "scene.sponsor",  "cueAt": "T+00:02:30",  "params": { /* ... */ } },
    { "id": "q-003", "sceneId": "scene.heat",     "cueAt": "afterTakeOf:q-002", "params": "auto" }
  ]
}
```

`cueAt` modes:
- `manual` — sits in queue until director takes it
- `T+hh:mm:ss` — schedule (wall-clock or competition-clock; the director picks)
- `afterTakeOf:<id>` — chain
- `onEvent:<eventName>` — fire when a domain event matches (e.g. `onEvent:Competition.EventCompleted`)

### 18.1 Queue commands

- `broadcast.queueAdd(item)` — append
- `broadcast.queueRemove(itemId)` — cancel a cued item
- `broadcast.queueReorder([ids])` — change order
- `broadcast.queueTakeNext()` — promote head into PRE then TAKE

---

## 19. Sponsor injection (DSK)

Sponsors are a first-class concept. They sit on `DSK_BUG` and are scheduled by a rotation policy independent of the broadcast scene flow.

### 19.1 Sponsor rotation

```jsonc
{
  "sponsorRotation": {
    "enabled": true,
    "intervalSec": 180,
    "displayDurationMs": 12000,
    "transitionMs": 400,
    "queue": [
      { "id":"sp-a","label":"FORZA SUPPLEMENTS","logoUrl":"/media/sp-a.svg" },
      { "id":"sp-b","label":"NORDIC STRONG GEAR","logoUrl":"/media/sp-b.svg" }
    ],
    "suppressDuringScenes": ["scene.champion","scene.lineup"]
  }
}
```

The orchestrator emits sponsor cycle events. The director can pause/resume; per-scene suppression rules keep the bug off during reveal moments.

---

## 20. Commentator graphics (ASTON channel)

A separate command surface, exposed at `/commentator`, lets a commentator (or a producer-side operator) push a lower-third caption to the ASTON layer (z=9, above the engine-driven LOWERTHIRD).

- Captioned name, role, single line of context.
- Auto-dismisses after N seconds (default 6).
- Does not interact with engine projections at all — pure manual layer.

```jsonc
{
  "command": "broadcast.setAston",
  "payload": {
    "kind": "commentator",
    "line1": "PER LARSEN",
    "line2": "FORMER WORLD'S STRONGEST",
    "durationMs": 6000
  }
}
```

---

## 21. Replay mode

A first-class playback layer for highlights.

### 21.1 Concepts

- **Marker** — a director-set timestamp during live broadcast: `broadcast.markReplay({ note })`.
- **Clip** — a pair of markers + a hosted video URL (Phase K).
- **Replay scene** — a scene whose LOWERTHIRD reads "REPLAY" and whose REPS layer is forced off (no live values during a replay clip).

### 21.2 Operator flow

1. Operator clicks "mark" during live action.
2. Producer cuts the clip from livestream timecode.
3. Operator cues the clip into PRE (`scene.replay` with the clip URL).
4. TAKE → engine pauses live timer mirroring, plays replay through OBS Source, returns to live scene on clip end.

### 21.3 Wire contract

```jsonc
{
  "command": "broadcast.cueReplay",
  "payload": {
    "clipId": "clip-91a",
    "label":  "TOP STONE LOAD — BJORNSSON",
    "expectedDurationMs": 12000,
    "returnTo": "scene.heat"
  }
}
```

Emits `Broadcast.ReplayCued` → `Broadcast.ReplayStarted` (on TAKE) → `Broadcast.ReplayEnded` (after duration).

---

## 22. Overlay state machine

Every overlay layer is a state machine. The state machine is run **per layer** by the orchestrator.

```
   ┌──────┐          cue              ┌─────────┐
   │ IDLE ├────────────────────────► │ CUED    │
   └──────┘                           │ (in PRE)│
        ▲                             └────┬────┘
        │ clear                             │ take
        │                                   ▼
        │                             ┌─────────┐
        │           transitionOut.end │ ENTERING│
        │ ◄─── EXITING ◄────┬─────────┤ (anim)  │
        │                   │         └────┬────┘
        │                   │              │ transitionIn.end
        │                   │              ▼
        │                   │         ┌─────────┐
        │                   │  hide   │ ON-AIR  │
        │                   └─────────┤         │
        │                             └─────────┘
        │
        │           hold expired (scene.holdMs)
        └─────────────────────────────────────────
```

Every transition between states emits an event. Every state has a maximum duration (timeouts protect against stuck states).

---

# PART IV — OVERLAY RUNTIME

This part is the contract every overlay must obey.

## 23. Connection lifecycle

```
[mount]
  load sse-client.js
  read query params (?token, ?scene, ?slot)
  initialise local mirror = empty
[connect]
  open EventSource /stream?subscribe=...&since.X=0&restartToken=null
  on open → status=CONNECTED
[handshake]
  first frame is full resync (server forces it on token=null)
  apply payload → local mirror populated
  compute fingerprints → first paint
[run]
  receive deltas → apply → recompute fingerprints → paint changed slices
  every 15s receive heartbeat → reset disconnect-timer
[disconnect]
  EventSource error OR 30s without heartbeat
  status=DISCONNECTED → freeze last paint (do NOT blank)
  begin exponential backoff reconnect (200ms*2^n + jitter, cap 5s)
[reconnect]
  open with since.X=last seen, restartToken=lastSeen
  apply deltas OR full resync based on server response
[unmount]
  close EventSource
  (no cleanup of DOM — OBS will destroy the source)
```

### 23.1 Visible state during disconnect

A disconnected overlay **freezes**. It does not:
- blank
- show a "disconnected" badge (this is a broadcast surface — the audience cannot see UI chrome)
- flicker
- reset animations

It holds the last frame until reconnect. The director console *does* show disconnect status — that's the right surface for that signal.

---

## 24. Scene mounting & unmounting

A single overlay file may render different "scene states" depending on the layer's current binding. The runtime architecture:

### 24.1 Mount model

An overlay HTML file at load time:
1. Builds the **structure** once (DOM skeleton).
2. Initialises empty content placeholders.
3. Subscribes to the SSE slice(s) it needs.
4. On every applicable delta, runs `patch(state)` which only touches placeholders.

Structure is **never rebuilt**. Content is **patched**.

### 24.2 The `_built` guard

Codify the pattern (already in `reps.html`):

> An overlay's structure is built exactly once per page lifetime. Subsequent updates patch content in place. Structure rebuild is *the* anti-pattern that broke leaderboard/lower-third in Phase 3.

A signal to rebuild structure (e.g. lane count changed from 4 to 6) requires:
- Computing the new structure off-DOM.
- Cross-fading or `requestIdleCallback`-deferring the swap.
- Never blanking visible content during the swap.

### 24.3 Show/hide semantics

`showOverlay` / `hideOverlay` commands manifest as a `visible` boolean on the layer's slice. The overlay:
- On `visible: false`: animate out (per scene's transitionOut), then set `display:none` *after* animation complete.
- On `visible: true → true` with content change: animate content patch (subtle — fade-in new value over 200ms).
- On `visible: false → true`: animate in (per scene's transitionIn).

`display:none` is only ever set **after** an exit animation completes. Never during.

---

## 25. DOM patching strategy

Three patch strategies, used per element class:

| Element                  | Strategy                                 | Reason                                   |
|--------------------------|-------------------------------------------|------------------------------------------|
| Static structure         | Built once, never touched                 | Cheapest                                 |
| Text values (names, scores) | `textContent` set if changed           | Cheap; no layout flash                   |
| Marquee/scroll content   | Build new DOM off-screen, swap node       | CSS animation must not reset             |
| Lists (lane rows, lb rows) | Reuse existing nodes; only `textContent` per cell | Avoid removing/recreating animated rows |

### 25.1 What never happens

- `element.innerHTML =` on any element with running CSS animation
- `display:none` then content swap then `display:block` (flashes)
- Creation of DOM nodes inside an SSE handler that fires every second
- `setInterval` running a timer (use CSS animation or `requestAnimationFrame` with throttling)

---

## 26. GPU-safe animation

OBS Browser Source runs Chromium. Compositor performance is real but not infinite. Rules:

### 26.1 Animate only `opacity` and `transform`

Anything else (width, top, left, color of text via JS) triggers layout or paint and costs more.

For motion: `transform: translateX/Y/Z`, `transform: scale`, `transform: rotate`.

For visibility transitions: `opacity` from 0 to 1.

### 26.2 Promote layers explicitly

For elements that animate frequently, set `will-change: transform, opacity` or `transform: translateZ(0)` to force GPU compositor layer. **But not on every element** — that exhausts video memory.

### 26.3 Avoid

- `box-shadow` on animated elements
- `filter` (blur, brightness) on animated elements
- `backdrop-filter` (not supported reliably in OBS)
- CSS gradients on `background` of moving elements (gradient is re-rasterised per frame on some GPUs)

### 26.4 Frame budget

OBS Browser Source typically runs at 30fps with a 33ms budget. Per frame we must complete:
- All JS handlers (SSE delta apply, patch)
- Style recalc
- Layout
- Paint
- Composite

Budget the JS portion at ≤8ms per frame. Test by recording a 10-second event-storm and reading the Performance trace.

---

## 27. Reconnect recovery (overlay-side)

Same protocol as §5 but tuned for overlay quirks:

- **Never blank during reconnect.** Hold last frame.
- **Never re-play an "enter" animation on reconnect.** If the leaderboard was on-air pre-disconnect and is still on-air post-reconnect, the overlay's `_wasVisible` guard prevents re-entry animation.
- **Restart token mismatch** is the *only* signal to reset internal counters (timer accumulators, hash caches). Otherwise overlay keeps its state.

---

## 28. Transport choice — SSE vs WebSocket

The system uses SSE for overlay-bound data and HTTP POST for commands. Why:

| Property                  | SSE                          | WebSocket                       |
|---------------------------|------------------------------|----------------------------------|
| One-way server→client      | Yes (matches model)          | Bidirectional (we don't need)    |
| Auto-reconnect             | Built-in                     | Manual                           |
| Survives `proxy_buffering off` nginx | Yes                  | Needs `Upgrade` handling         |
| OBS Browser Source compat  | Excellent                    | Good                             |
| Backpressure               | Limited                      | Better                           |
| Per-frame size at our scale (<5MB/s) | Fine               | Fine                             |

WebSocket revisits when: we need server-pushed prompts to judge devices (e.g. "next heat in 30s — confirm ready") **and** the volume justifies the operational cost.

---

## 29. Overlay scene catalogue (runtime view)

Each overlay file maps to one or more scene roles. Their data subscriptions:

| File                    | Slot/Layer       | Subscribes to                                       |
|-------------------------|------------------|------------------------------------------------------|
| `leaderboard.html`      | LEADERBOARD      | `proj.leaderboard.{lbCategory}`, `pgm.leaderboard.visible` |
| `lowerthird.html`       | LOWERTHIRD       | `proj.lanes`, `judging.lights`, `pgm.lowerthird.visible` |
| `scorebug.html`         | SCOREBUG         | `proj.athletes.{compCategory}`, `pgm.scorebug.visible`   |
| `reps.html`             | REPS / LIGHTS    | `judging.judgeLN.all`, `pgm.reps.visible`             |
| `champion.html`         | CHAMPION         | `pgm.champion.params`, `pgm.champion.visible`         |
| `lineup.html`           | LINEUP           | `proj.lineup.{currentCategory}`, `pgm.lineup.visible` |
| `h2h.html`              | H2H              | `pgm.h2h.left`, `pgm.h2h.right`, `pgm.h2h.visible`    |
| `manual_lowerthird.html`| LOWERTHIRD (manual) | `pgm.manualLowerthird.*`                           |
| `eventbar.html` (new)   | EVENTBAR         | `proj.eventState`, `pgm.eventbar.visible`             |
| `sponsorbug.html` (new) | DSK_BUG          | `pgm.bug.*`                                            |
| `aston.html` (new)      | ASTON            | `pgm.aston.*`                                          |

The new files (`eventbar`, `sponsorbug`, `aston`) are net-new in Phase A; they formalise existing inlined content into discrete layers.

---

# PART V — UI & OPERATOR SYSTEM

A broadcast operating system has multiple operator surfaces, each tuned to a role. The design language is consistent across all surfaces; the density and affordances differ.

## 30. Surface inventory

| Surface              | Path                 | Role               | Device           | Hands free? |
|----------------------|----------------------|--------------------|------------------|-------------|
| Director console     | `/control`           | Broadcast director | Desktop, 24"+    | One         |
| Scoring engine       | `/comp/run`          | Scoring operator   | Laptop           | One         |
| Lane judge           | `/judge?lane=N`      | Lane judge         | Phone / tablet   | Yes (gloves) |
| Master judge         | `/judge/master`      | Master judge       | Tablet           | Yes         |
| Commentator console  | `/commentator`       | Commentator        | Tablet           | One         |
| Arena display        | `/arena`             | (audience-facing)  | Big screen       | n/a         |
| Organiser dashboard  | `/org/<slug>`        | Organiser          | Desktop          | One         |
| Public results       | `/c/<slug>`          | Spectator          | Phone / desktop  | One         |
| Audit / replay       | `/comp/audit`        | Owner / referee    | Desktop          | One         |

Each surface speaks the same protocol (Part II) but constrains itself to the commands its role can issue (Part I §6.4).

---

## 31. Design language

### 31.1 Adjectives and what they mean here

| Word              | Concrete translation                                                                              |
|-------------------|----------------------------------------------------------------------------------------------------|
| **Tactical**      | High information density, status-as-colour, glance-readable, no narrative copy                   |
| **Premium**       | Deep neutrals, restrained accent, generous type, no rounded-toy edges                            |
| **Cinematic**     | Strong type hierarchy, broadcast-safe contrast, motion only where it serves the moment           |
| **Industrial**    | Monospace numerics, hard right-angles where data lives, grid alignment                           |
| **Operational**   | Verbs-as-buttons, primary action always identifiable, state always visible                       |
| **Fast**          | Sub-16ms paint, no skeleton loaders, immediate optimistic feedback (then committed truth)        |
| **Broadcast-grade**| sRGB safe, no clipping, no chroma fringes, no animation that breaks at 30fps                    |

### 31.2 Anti-patterns

We do not use:
- Iridescent gradients, glassmorphism, drop shadows on cards.
- Soft rounded corners on data surfaces. (Reserved for overlay panels only, radius 14–20.)
- Decorative icons in tables.
- Carousels, swipers, accordions in operator views.
- Modal dialogs for non-destructive operations.
- Toast notifications for state changes — state is already visible.

---

## 32. Token system

### 32.1 Color tokens

```
/* Surface */
--surface-bg          #08090C   /* page background, deepest */
--surface-1           #11131A   /* primary card, panel */
--surface-2           #1A1D26   /* nested card */
--surface-3           #232733   /* hover, active row */
--surface-border      #2C313E
--surface-border-hi   #3B414F

/* Text */
--text-hi             #F4F4F7
--text                #D6D7DD
--text-muted          #8A8E99
--text-dim            #5A5D67

/* Accent — StrongPass yellow (primary brand) */
--accent              #F5C842
--accent-hi           #FFD75A
--accent-deep         #C9A030

/* State */
--state-live          #4ADE80   /* on-air, AUTO, OK */
--state-manual        #FBBF24   /* MANUAL, attention */
--state-warn          #F97316   /* warning */
--state-err           #EF4444   /* danger, disconnect */
--state-info          #38BDF8

/* Category palette (defaults) */
--cat-u70             #2DD4BF
--cat-u80             #FBBF24
--cat-u90             #F5C842
--cat-u105            #F97316
--cat-u120            #EF4444
--cat-open            #A855F7
--cat-masters         #38BDF8

/* Overlay-only */
--ovl-name            #FFFFFF
--ovl-value           #FFFFFF
--ovl-eyebrow         #E5C760
--ovl-panel-bg        rgba(8,9,12,0.92)
```

### 32.2 Typography tokens

```
--font-display: "Barlow Condensed", "Barlow", Impact, sans-serif;
--font-body:    "Inter", system-ui, -apple-system, "Segoe UI", sans-serif;
--font-numeric: "Barlow", "JetBrains Mono", monospace;
                /* feature: tnum, lnum */

/* Type scale (px / line-height px) */
--t-display:    96 / 104;
--t-headline:   56 / 64;
--t-title:      32 / 40;
--t-section:    20 / 28;
--t-body:       15 / 22;
--t-caption:    12 / 16;     /* letter-spacing 0.12em, uppercase */
--t-numeric-l:  64 / 64;     /* score primary */
--t-numeric-m:  32 / 36;
--t-numeric-s:  18 / 22;
```

### 32.3 Spacing & radius

```
--s-0:   0;
--s-1:   4px;
--s-2:   8px;
--s-3:  12px;
--s-4:  16px;
--s-5:  24px;
--s-6:  32px;
--s-7:  48px;
--s-8:  64px;

--r-0:   0;
--r-1:   2px;     /* almost nothing — for sharp data surfaces */
--r-2:   6px;
--r-3:  10px;     /* default */
--r-4:  14px;     /* overlay panels */
--r-5:  20px;     /* champion-tier */
```

### 32.4 Motion tokens

```
--m-instant:   0ms;
--m-fast:      120ms;
--m-base:      200ms;
--m-slow:      400ms;
--m-reveal:    700ms;       /* champion only */

--ease:           cubic-bezier(0.2, 0, 0, 1);
--ease-out-back:  cubic-bezier(0.34, 1.56, 0.64, 1);   /* champion reveal only */
--ease-linear:    linear;                              /* timer countdowns only */
```

### 32.5 Elevation (very restrained)

```
--shadow-0:  none;
--shadow-1:  0 1px 0 rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.03);
--shadow-2:  0 2px 0 rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.04);
/* No --shadow-3. We do not float chrome. */
```

---

## 33. Component hierarchy

The operator surfaces compose from a small set of primitives. Every higher-order component is one of these or a composition.

### 33.1 Atoms

- **Pill** — colored dot + 12px caption. Status indicator.
- **Badge** — short capsule with state colour (AUTO, MANUAL, LIVE, OFFLINE).
- **Button (primary | secondary | danger | ghost)** — 40px tall, 16px horizontal pad, no rounded blobs.
- **Input (text | number | select)** — 40px tall, dark surface, 1px border.
- **Label** — above-input, caption type.
- **Hotkey hint** — kbd-styled key inside a button (`[ T ]` for TAKE).

### 33.2 Molecules

- **CommandButton** — Button + hotkey hint + busy state + reject flash.
- **StatusRow** — left: pill+text, right: actions. Used in overlay list, queue, lane preview.
- **ValueDisplay** — large numeric (tnum), label below. Used everywhere a score or count is shown.
- **CategoryChip** — colored chip with category name; clickable for filter / pin.

### 33.3 Organisms

- **OverlayPanel** — list of layer states with per-row toggle + AUTO/MANUAL badge.
- **ScenePicker** — grid of scene tiles with cue indicator.
- **PgmPreMonitor** — two side-by-side compositor previews (PGM and PRE).
- **LanePreview** — read-only mirror of the four lanes (name, value, light, timer).
- **QueuePanel** — ordered list of cued items with reorder handles.
- **LeaderboardPreview** — top-10 with pin indicator.
- **CommandLog** — recent commands with `cmdId`, principal, result.

### 33.4 Templates

A surface is composed of organisms in a fixed layout for its role. Wireframes in Part VI fix these.

---

## 34. Interaction language

### 34.1 Verbs as buttons

Every button is a verb. Never a noun. "TAKE" not "Program". "CUE" not "Preview". "PIN" not "Override". "REVEAL" not "Champion".

### 34.2 Primary action

Each surface has exactly one button styled as primary. On the director console it is **TAKE**. On the judge surface it is **SUBMIT**. On the master judge surface it is **SEAL HEAT**. On scoring it is **ADVANCE**.

The primary action is always:
- Top-right (desktop) or bottom (mobile)
- Filled `--accent`
- Hotkey-bound (T for TAKE, S for SUBMIT, etc.)
- Glow on hover, depress on press

### 34.3 Hotkeys

Hotkeys are essential on the director console. Reserved set:

| Key | Action                                |
|-----|----------------------------------------|
| T   | TAKE                                   |
| C   | CUE (focus PRE picker)                 |
| B   | Cut to black                           |
| Esc | Cancel current scene/clear PRE         |
| 1–9 | Scene presets (configurable)           |
| Space | Pause/resume sponsor rotation        |
| L   | Toggle leaderboard layer               |
| H   | Toggle lower-third layer               |
| /   | Focus quick command                    |
| .   | Repeat last command                    |

### 34.4 Confirmations

Two-step confirmation **only** for:
- Cut to black (asks: "BREAK GLASS — CUT TO BLACK?")
- Rollback / retract score
- Delete athlete / event during live competition
- End competition

All other actions are single-click. The director is trusted; speed matters.

### 34.5 Optimistic / committed states

When a button is pressed:
- 0ms: button shows pressed state
- 0ms: optimistic UI applied (scene picker highlights, etc.)
- ~80ms typical: server response confirms → UI keeps state
- on reject: ~150ms: UI rolls back with brief red flash, error toast (this is the one toast we use, because rejection is exceptional)

### 34.6 No spinners

If a command takes >300ms the UI shows a thin progress bar at the top of the surface (broadcast-engine feel, not SaaS feel). Spinners are forbidden.

---

## 35. Per-surface design notes

### 35.1 Director console

- **Density**: 3-column layout on desktop. PRE/PGM monitor top, scene grid + queue middle, layer panel + commands bottom.
- **Confidence**: status bar across the top with always-visible: comp name, restart token, version, connected client count, beta-token life remaining.
- **Latency-aware**: every visible state shows its source — `[from PGM v.812]` in a tooltip.

### 35.2 Scoring engine

- **Tabular density**: this is a spreadsheet-grade view. Heat grid, score table, leaderboard side-by-side.
- **Keyboard-first**: Tab/Enter navigation, arrow keys to move between cells.
- **Undo cell-level**: every cell tracks last 5 values for quick recovery during data entry.

### 35.3 Lane judge

- **Thumb-zone primaries**: SUBMIT at bottom, REP+ above, START at top.
- **No nav chrome**: dedicated single-purpose surface.
- **Haptic on rep**: small vibration confirms increment.
- **Big-font scoreline**: current value at 64px so judges can confirm by glance.

### 35.4 Master judge

- **Four lanes in one view**: all timers, lights, current values.
- **Authority actions**: pause-all, seal-heat, override-lane.
- **Reasoning capture**: any override prompts for a reason (audit).

### 35.5 Commentator console

- **Read-mostly with two write actions**: send ASTON caption, send fact card.
- **Athlete reference**: scrollable cards with athlete data (PRs, hometown, prior placements).
- **No engine control**: commentator cannot affect competition or broadcast scenes.

### 35.6 Arena display

- **Audience-facing**: big-font, no chrome.
- **Renders same overlays as broadcast** but at 4K with simpler layouts.
- **Independent scene channel**: arena director can choose to show leaderboard while broadcast director shows lineup.

---

# PART VI — DELIVERABLES

## 36. Master architecture diagram

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         CLIENT SURFACES                                       │
│                                                                                │
│  Director (control)  Scoring (comp/run)  Judge  Master Judge  Commentator     │
│  Public results      Organiser dashboard  Arena display                       │
└──────────┬─────────────┬────────────┬─────────────┬─────────────┬────────────┘
           │             │            │             │             │
           │ commands    │            │ commands    │             │ commands
           │ (HTTPS POST)│ stream     │             │ stream      │
           ▼             ▼            ▼             ▼             ▼
    ┌──────────────────────────────────────────────────────────────────────┐
    │                          IDENTITY                                    │
    │  Beta token gate · session · role check · principal attach           │
    └──────────────────────────┬───────────────────────────────────────────┘
                               │
                  ┌────────────┴────────────┐
                  ▼                         ▼
          ┌───────────────┐         ┌──────────────────┐
          │  COMMAND BUS  │         │  STREAM SERVER   │
          │  /command/*   │         │  SSE /stream     │
          │  schema valid │         │  versioned slice │
          │  idempotency  │         │  resync engine   │
          └───────┬───────┘         └────────▲─────────┘
                  │                          │
                  ▼                          │
          ┌─────────────────┐                │
          │ DOMAIN HANDLERS │                │
          │  Competition    │                │
          │  Broadcast      │                │
          │  Judging        │                │
          │  Identity       │                │
          └─────┬───────────┘                │
                │ emits                      │
                ▼                            │
        ┌────────────────────┐               │
        │   EVENT BUS        │               │
        │  in-process now    │───── audit ──►│ (fanout to subscribers)
        │  redis later       │               │
        └─────┬──────┬───────┘               │
              │      │                       │
              │      └──────────────┐        │
              ▼                     ▼        │
   ┌───────────────────┐    ┌──────────────────┐
   │  PROJECTION       │    │   AUDIT LOG      │
   │  workers          │    │  append-only     │
   │  (invalidation)   │    │  fsync           │
   └──┬──────┬─────┬───┘    └────────┬─────────┘
      │      │     │                 │
      ▼      ▼     ▼                 ▼
  ┌──────┐ ┌────┐ ┌──────────┐  ┌──────────┐
  │lanes │ │ lb │ │ public.* │  │ snapshots│
  │      │ │ {} │ │          │  │ (rotated)│
  └──────┘ └────┘ └──────────┘  └──────────┘
                                       │
                                       ▼
                                  Plane 1 (comp.db)
                                  Plane 2 (broadcast/*.json)
```

---

## 37. State flow diagram (single command, end-to-end)

```
Director clicks TAKE (T hotkey)
   │
   │ optimistic UI: PRE highlight fades, PGM monitor swaps
   ▼
POST /command/broadcast/takeScene  { cmdId, expected.broadcast.version }
   │
   ▼  [1] identity gate
   │      principal = { operator/op-tony, role=director, session=s-91ac }
   ▼  [2] schema validate
   │      ok
   ▼  [3] idempotency
   │      cmdId not seen → continue
   ▼  [4] handler
   │      reads PRE, writes PGM = PRE, clears PRE
   │      emits Broadcast.SceneTaken (v=813)
   │      emits per-layer state transitions:
   │         Broadcast.OverlayHidden(SCOREBUG, v=814)
   │         Broadcast.OverlayHidden(REPS, v=815)
   │         Broadcast.OverlayTransitionStep(LOWERTHIRD.out, v=816)
   │         Broadcast.OverlayShown(CHAMPION, v=817)
   │         Broadcast.OverlayTransitionStep(CHAMPION.in, v=818)
   ▼  [5] audit fsync (all 6 events committed)
   ▼  [6] projection bus
   │      no projection invalidated (broadcast state only)
   ▼  [7] SSE fanout
   │      changed slices: pgm.scene, pgm.<each layer>.visible
   │      versioned delta sent to subscribed overlays + director console
   ▼  [8] response
       { ok:true, version:818, eventsEmitted:6, elapsedMs:14 }
   │
   ▼  director console: optimistic confirmed, no rollback
   ▼  overlays: receive delta → run transition steps → on-air

   Total latency button-to-on-air: ~16ms server + ~700ms scripted transition
```

---

## 38. Scene graph diagram

```
                            scene.blank
                                │
              ┌─────────────────┼───────────────────┐
              ▼                 ▼                   ▼
         scene.lineup       scene.heat          scene.commentator
              │                 │
              │                 ├──► scene.heat+lb
              │                 │           │
              │                 │           ▼
              │                 │      scene.result
              │                 │           │
              │                 ▼           │
              │            scene.h2h        │
              │                 │           │
              │                 │           ▼
              │                 └────►  scene.champion ───┐
              │                                            │
              ▼                                            │
         scene.lineup ◄──────────── scene.replay ◄─────────┤
                                                            │
                                              autoFollow ──┘
                                              (after holdMs)

   DSK_BUG layer is orthogonal; renders across all scenes unless suppressed.
   ASTON layer is orthogonal; renders across all scenes always (top).
```

State transitions are all valid; the orchestrator computes the diff.

---

## 39. Operator workflow maps

### 39.1 Director — full event run

```
Pre-show
  ├ load scene.blank → broadcast confirms idle
  ├ cue scene.lineup → review PRE → TAKE
  └ cue scene.blank → TAKE (camera B-roll)

Per heat
  ├ on Competition.HeatAdvanced → orchestrator cues scene.heat into PRE (AUTO mode)
  ├ TAKE → PGM = scene.heat
  ├ during heat: optionally cue scene.heat+lb mid-run for variety
  ├ on Judging.HeatSealed → orchestrator cues scene.result
  │   ├ director decides: TAKE result, or skip to next heat
  │   └ if last heat of event: orchestrator queues scene.champion after result
  └ TAKE → next heat

Spotlight moments
  ├ cue scene.h2h with two athletes → TAKE
  └ Esc → returns to last non-spotlight scene

Final
  ├ scene.champion (event-final)
  ├ scene.lineup (recap)
  └ scene.blank
```

### 39.2 Master judge — per heat

```
Heat starts
  ├ verify 4 lanes ready (athletes assigned in /comp engine)
  └ confirm START
       │
       ▼
Per-athlete during heat
  ├ observe lane judges' submissions
  ├ veto override any lane verdict (with reason captured)
  └ pause-all if dispute
       │
       ▼
Heat complete
  ├ review all lane scores in master view
  ├ SEAL HEAT (confirms all lanes final)
  └ scoring operator now sees "Advance" enabled in /comp/run
```

### 39.3 Lane judge — per athlete

```
Athlete called → tap START → timer runs
  ├ tap REP+ per successful rep (haptic confirm)
  ├ at finish:
  │    ├ tap STOP timer
  │    ├ tap GOOD or NOGOOD
  │    └ tap SUBMIT → score promoted, lane locks
  └ wait for next athlete in heat

After heat sealed
  ├ lane resets, prepares for next heat
  └ if next heat assigns this lane to a new athlete, surface auto-updates
```

---

## 40. UI wireframes (selected)

### 40.1 Director console — full layout

```
╔════════════════════════════════════════════════════════════════════════════════╗
║ ● LIVE  · IRON CHALLENGE 2025 · STRONGMAN SERIES         restart 31822  v 818 ║
║ AUTO MODE [●AUTO] [○MAN]   clients: 14  beta: 6d 4h                            ║
╠════════════════════════════════════════════════════════════════════════════════╣
║                                                                                ║
║ ┌─ PRE (preview) ────────────────┐  ┌─ PGM (program — ON AIR) ──────────────┐ ║
║ │                                │  │                                       │ ║
║ │      [render scene.champion]   │  │     [render scene.heat]               │ ║
║ │      params:                   │  │     event 4 · heat 2 · U90            │ ║
║ │      THOR BJORNSSON 48.5       │  │     4 lanes active                    │ ║
║ │                                │  │                                       │ ║
║ │                                │  │                                       │ ║
║ │  [ CUE ▾  ]   scene.champion ▾ │  │             [ ▶ TAKE  (T) ]           │ ║
║ └────────────────────────────────┘  └───────────────────────────────────────┘ ║
║                                                                                ║
║ ┌─ SCENE PICKER ───────────────────┐  ┌─ QUEUE ──────────────────────────────┐║
║ │ [blank] [lineup] [heat] [heat+lb]│  │ q-001  scene.h2h         manual      │║
║ │ [result] [champion] [h2h] [replay]│  │ q-002  scene.sponsor     T+02:30    │║
║ │ [sponsor] [commentator]          │  │ q-003  scene.heat        afterTakeOf │║
║ │ [+ custom]                       │  │ [+ add] [reorder]  [▶ take next]    │║
║ └──────────────────────────────────┘  └──────────────────────────────────────┘║
║                                                                                ║
║ ┌─ LAYERS ───────────────────────────────────────┐ ┌─ LANE PREVIEW ───────────┐║
║ │ ●LIVE  DSK_BUG       AUTO   [Forza Supplements]│ │ L1 JOHANSSON 48.5 GOOD   │║
║ │ ●LIVE  EVENTBAR      AUTO   "EVENT 4 · HEAT 2" │ │ L2 BJORNSSON  --   ---   │║
║ │ ●LIVE  SCOREBUG      AUTO                      │ │ L3 HALL      44.0 GOOD   │║
║ │ ●LIVE  LOWERTHIRD    AUTO   (lanes 1-4)        │ │ L4 LUND      41.5 GOOD   │║
║ │ ○OFF   LEADERBOARD   MANUAL (pinned U80)       │ │                          │║
║ │ ●LIVE  REPS                                    │ │ Timer L1: --             │║
║ │ ○OFF   LINEUP                                  │ │ Timer L2: 22.4s ●        │║
║ │ ○OFF   H2H                                     │ │ Timer L3: --             │║
║ │ ○OFF   CHAMPION                                │ │ Timer L4: --             │║
║ │ ○OFF   ASTON                                   │ │                          │║
║ │ [ B CUT TO BLACK ]                             │ │                          │║
║ └────────────────────────────────────────────────┘ └──────────────────────────┘║
║                                                                                ║
║ ┌─ COMMAND LOG ────────────────────────────────────────────────────────────┐  ║
║ │ v818  takeScene  scene.heat→scene.champion   op-tony  14ms  OK            │  ║
║ │ v812  pinLeaderboard  U80                     op-tony  3ms   OK            │  ║
║ │ v807  showOverlay  LEADERBOARD                op-tony  2ms   OK            │  ║
║ └──────────────────────────────────────────────────────────────────────────┘  ║
╚════════════════════════════════════════════════════════════════════════════════╝
```

### 40.2 Lane judge — phone

```
┌──────────────────────────────┐
│ ●  L2  U90  HEAT 2           │
│ ATLAS STONES  · EVENT 4      │
├──────────────────────────────┤
│                              │
│        T. BJORNSSON          │
│        ─────────────         │
│        ICELAND               │
│                              │
│   ┌────────────────────┐     │
│   │      48.5 s        │     │
│   │ ●●●●●●●●●●● remain │     │
│   └────────────────────┘     │
│                              │
│        REPS                  │
│        ┌──────┐              │
│        │  5   │              │
│        └──────┘              │
│                              │
│   [          REP +       ]   │
│   [          REP -       ]   │
│                              │
│   ┌─────────┬──────────┐     │
│   │  GOOD   │  NOGOOD  │     │
│   └─────────┴──────────┘     │
│                              │
│   ┌──────────────────────┐   │
│   │       SUBMIT         │   │
│   └──────────────────────┘   │
│                              │
│   status: working            │
│   ● connected  · cmd q: 0    │
└──────────────────────────────┘
```

### 40.3 Master judge — tablet (horizontal)

```
┌──────────────────────────────────────────────────────────────────────┐
│ MASTER  ·  EVENT 4 ATLAS STONES  ·  HEAT 2 / U90       [ SEAL HEAT ] │
├──────────────────────────────────────────────────────────────────────┤
│ LANE 1            LANE 2           LANE 3           LANE 4           │
│ JOHANSSON S.      BJORNSSON T.     HALL E.          LUND O.          │
│ ──────────────    ──────────────   ──────────────   ──────────────   │
│ 60.0s             48.5s ●          60.0s            60.0s            │
│                                                                       │
│   48.5  GOOD       --    ---        44.0  GOOD       41.5  GOOD      │
│                                                                       │
│ REPS  12          REPS  5          REPS  10         REPS  9          │
│                                                                       │
│ [override]        [override]       [override]       [override]       │
├──────────────────────────────────────────────────────────────────────┤
│ [ PAUSE ALL ]    submitted: 3/4    avg time: 52s   [ START NEXT ]    │
└──────────────────────────────────────────────────────────────────────┘
```

### 40.4 Commentator console — tablet

```
┌──────────────────────────────────────────────────────────────────────┐
│ COMMENTATOR  ·  IRON CHALLENGE 2025                                  │
├──────────────────────────────────────────────────────────────────────┤
│ ┌─ ATHLETE FACT CARDS ─────────┐  ┌─ ASTON CAPTION ─────────────────┐│
│ │ ▼ U90 (current)              │  │ Line 1 [PER LARSEN___________] ││
│ │   JOHANSSON, S.   prev: 1st  │  │ Line 2 [FORMER WORLD'S STR___] ││
│ │   BJORNSSON, T.   PR: 50.0   │  │ Duration [ 6s ▾ ]              ││
│ │   HALL, E.        debut      │  │                                ││
│ │   LUND, O.        prev: 4th  │  │       [  SEND ASTON  ]         ││
│ │   [send to ASTON]            │  └─────────────────────────────────┘│
│ │                              │                                     │
│ │ ▼ Upcoming categories        │  ┌─ FACT QUEUE ───────────────────┐ │
│ │   U80, U70                   │  │ (pre-written facts, taggable)  │ │
│ │                              │  │ • "Bjornsson's 5th appearance" │ │
│ │                              │  │ • "Stockholm crowd favourite"  │ │
│ │                              │  │ [+ add fact]                   │ │
│ └──────────────────────────────┘  └────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

### 40.5 Arena display

```
╔════════════════════════════════════════════════════════════════════════════════╗
║                                                                                ║
║                                                                                ║
║                       IRON  CHALLENGE  2025                                    ║
║                                                                                ║
║                       EVENT 4  ·  ATLAS STONES                                 ║
║                       HEAT 2 / U90                                             ║
║                                                                                ║
║       ┌──────────────────────────────────────────────────────────────┐         ║
║       │  LANE 1  JOHANSSON S.  ░░░░░░░░░░░░░░░  48.5    [GOOD]       │         ║
║       │  LANE 2  BJORNSSON T.  ░░░░░░░         --        ----        │         ║
║       │  LANE 3  HALL E.       ░░░░░░░░░░░░░   44.0    [GOOD]        │         ║
║       │  LANE 4  LUND O.       ░░░░░░░░░░░     41.5    [GOOD]        │         ║
║       └──────────────────────────────────────────────────────────────┘         ║
║                                                                                ║
║                                                                                ║
║                       LEADERBOARD  U90                                         ║
║                       1  BJORNSSON T.  48                                      ║
║                       2  JOHANSSON S.  44                                      ║
║                       3  HALL E.       38                                      ║
║                                                                                ║
║                                                                                ║
╚════════════════════════════════════════════════════════════════════════════════╝
```

### 40.6 Audit / replay view

```
╔════════════════════════════════════════════════════════════════════════════════╗
║ AUDIT  ·  IRON CHALLENGE 2025                            [ replay mode: OFF ]  ║
╠════════════════════════════════════════════════════════════════════════════════╣
║ time           stream      version  event                  principal   action ║
║ ────           ──────      ───────  ─────                  ─────────   ────── ║
║ 14:33:24.0     broadcast   813      Broadcast.SceneTaken   op-tony     view   ║
║ 14:33:22.918   competition 4422     Competition.ScoreRec.. op-tony     view   ║
║ 14:32:18.402   judging     1014     Judging.HeatSealed     op-mike     view   ║
║ 14:32:12.198   competition 4421     Competition.ScoreRec.. op-mike     view   ║
║                                                                                ║
║ [ scrub timeline                                                            ]  ║
║ [───────●──────────────────────────────────────────────────] now              ║
║                                                                                ║
║ ┌─ SELECTED ──────────────────────────────────────────────────────────┐       ║
║ │ Competition.ScoreRecorded  v4422                                     │       ║
║ │ athleteId 17 (BJORNSSON, T.)  event 4 (Atlas Stones)  primary 48.5   │       ║
║ │ cmdId cmd-91ac4e   principal op-tony   ts 14:33:22.918               │       ║
║ │                                                                      │       ║
║ │ [ retract  (requires reason) ]   [ restore state at this point ]    │       ║
║ └──────────────────────────────────────────────────────────────────────┘       ║
╚════════════════════════════════════════════════════════════════════════════════╝
```

---

## 41. Overlay orchestration diagram

```
                            DOMAIN EVENTS
                                  │
                                  ▼
                         ┌─────────────────┐
                         │ ORCHESTRATOR    │
                         │ (broadcast.run) │
                         └────┬────────────┘
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
      AUTO mode logic    Scene picker     Queue runner
      cues PRE on        (operator)        (scheduled)
      domain events
              │
              ▼
        ┌──────────┐         TAKE ──►   ┌──────────┐
        │   PRE    │ ───────────────►   │   PGM    │
        │ (staged) │                    │ (on-air) │
        └──────────┘                    └────┬─────┘
                                              │
                                              ▼
                                    ┌─────────────────────┐
                                    │ LAYER STATE MACHINE │
                                    │ runs per layer      │
                                    │ IDLE→CUED→ENTERING  │
                                    │ →ON-AIR→EXITING→IDLE│
                                    └─────────┬───────────┘
                                              │ emits
                                              ▼
                                    Broadcast.OverlayShown/Hidden/
                                    OverlayTransitionStep events
                                              │
                                              ▼
                                          AUDIT LOG
                                              │
                                              ▼
                                       SSE FANOUT
                                              │
                              ┌───────────────┼────────────────┐
                              ▼               ▼                ▼
                       lower-third      leaderboard       champion
                       (subscribes      (subscribes      (subscribes
                        LOWERTHIRD)      LEADERBOARD)     CHAMPION)
```

---

## 42. Deployment topology

### 42.1 Current

```
                Internet
                   │
                   ▼
            ┌─────────────┐
            │   nginx     │  :443  TLS, proxy_buffering off /stream
            └──────┬──────┘
                   │ :8080
            ┌──────▼──────┐
            │  gunicorn   │  1 worker, 8 gthread threads
            │ (server.py) │
            └──────┬──────┘
                   │
        ┌──────────┼───────────┐
        ▼          ▼           ▼
      comp.db   state.json   templates/
      (WAL)     (atomic)
        │
        └─► nightly rsync backup
```

### 42.2 Phase A target (no topology shift; new files; same VM)

```
                Internet
                   │
                   ▼
            ┌─────────────┐
            │   nginx     │
            └──────┬──────┘
                   │
            ┌──────▼──────┐
            │  gunicorn   │
            │ (server.py) │
            └──┬──────┬───┘
               │      │
               │      └────► SSE fanout (per-client subscription map)
               │
   ┌───────────┼──────────────┬───────────────┐
   ▼           ▼              ▼               ▼
 comp.db   broadcast/      projections/    audit/
 (WAL)     ├─state.pgm.json (in-memory     events-NNNN.jsonl
           ├─state.pre.json  +disk mirror) (rolled by size)
           ├─queue.json
           └─overrides.json

           snapshots/  ← periodic full-state captures

           off-host:  rsync nightly + per-event final snapshot
```

### 42.3 Multi-tenant target (Phase H, for reference)

```
                       Internet
                          │
                ┌─────────┴─────────┐
                │ nginx (host-route)│
                │ *.strongpass.live │
                └─────┬───────┬─────┘
                      │       │
              ┌───────┴───┐  ┌┴──────────┐
              │ app pool  │  │ static/CDN│
              │ (workers) │  │ (overlays,│
              └───────┬───┘  │  public)  │
                      │      └───────────┘
              ┌───────┼───────┐
              ▼       ▼       ▼
         Postgres   Redis  Object store
         (orgs,    (SSE   (media,
          comps,    bus,   snapshots,
          audit)    cache) backups)
```

---

## 43. Protocol specification (concise reference)

### 43.1 Command endpoint

```
POST /command/{domain}/{name}
Headers:
  Content-Type: application/json
  Authorization: <session cookie>
  X-Beta-Token: <required for OBS sources>
Body:
  {
    "cmdId": "uuid-v7",
    "issuedAt": "iso8601",
    "expected": { "<stream>.version": int }?,   // optional optimistic lock
    "payload": { ...command-specific... }
  }

Response 200:
  {
    "ok": true,
    "version": int,                  // resulting version on the affected stream
    "eventsEmitted": [{ "auditId":int, "stream":str, "version":int, "event":str }],
    "elapsedMs": int
  }

Response 4xx:
  {
    "ok": false,
    "error": {
      "code": "stale_version"|"invalid_payload"|"forbidden"|"not_found"|"rate_limited",
      "message": "human-readable",
      "details": { ... }
    }
  }
```

### 43.2 Stream endpoint

```
GET /stream?
    subscribe=lanes,leaderboard.U90,eventState,judging.lights
    &since.competition=4421
    &since.broadcast=812
    &since.judging=917
    &since.proj.lanes=901
    &restartToken=31822
Headers:
  Accept: text/event-stream
  X-Beta-Token: <required>
Response: text/event-stream

Frame types:
  event: delta
  data: { "version": {...}, "slices": { "lanes": {...}, ... } }

  event: resync
  data: { "version": {...}, "slices": { ...full state for subscribed... }, "restartToken": int }

  event: error
  data: { "code": "...", "message": "..." }

  :heartbeat (comment line, every 15s)
```

### 43.3 Subscription filter grammar

```
subscribe = item ("," item)*
item      = projection | sliceSlot
projection = "proj." NAME ("." KEY)?         // e.g. proj.leaderboard.U90
sliceSlot  = "pgm." LAYER                    // e.g. pgm.lowerthird
           | "pre." LAYER
           | "judging.judgeL" 1..N
           | "judging.lights"
           | "lanes"
           | "eventState"
```

### 43.4 Error code catalogue

| Code              | Meaning                                                |
|-------------------|---------------------------------------------------------|
| `invalid_payload` | Schema validation failed                               |
| `stale_version`   | `expected.{stream}.version` differs from current       |
| `forbidden`       | Role does not allow this command                       |
| `not_found`       | Referenced entity (athlete, event, scene) missing      |
| `rate_limited`    | Too many commands in window (per-session limit)        |
| `idempotent_replay` | cmdId matches a recent command — response cached     |
| `client_ahead`    | Client claims a `since` newer than server has          |
| `internal`        | Server fault — retry with same cmdId                   |

---

## 44. Phased implementation roadmap

This Phase A document is the *spec*; the roadmap below is the *order* in which to bring it to life. Each phase is independently shippable and reversible.

### A.0 — Audit log foundation
**Goal:** Every state mutation written to an append-only log.
- Create `audit_log` table (Plane 1) with the canonical schema.
- Wrap every existing mutation path (`/update`, `/director/lb`, `/comp/*`) to also write to `audit_log`.
- No projection split yet; no API surface change. This is purely the seam.
**Exit:** A 30-minute mock event produces a complete, replayable audit log. Replay tool reproduces final state.

### A.1 — State plane split
**Goal:** `state.json` no longer dual-purpose.
- Introduce `broadcast/state.pgm.json` (PGM), `broadcast/state.pre.json` (PRE empty for now), `broadcast/overrides.json`.
- Move engine config (lane count, comp meta, category order) out of state.json into `engine_config.json`.
- Compatibility view at legacy `/state.json` returns merged shape during cutover.
**Exit:** All overlays continue to render correctly. Legacy view removed in next release.

### A.2 — Typed command surface
**Goal:** Replace free-form `/update` with typed commands.
- Define schemas (Part II §11) and dispatch table.
- Implement `/command/{domain}/{name}`.
- Keep `/update` as shim → translates to typed commands.
- Director console, judge surface, scoring surface migrate to typed commands.
**Exit:** Recorded session replays cleanly; legacy `/update` shim emits deprecation log lines.

### A.3 — Event bus + projections
**Goal:** Events drive projection invalidation.
- In-process event bus.
- Projection registry (Part I §2).
- Each domain handler emits events; subscribers (projections, audit, SSE) react.
- Server runs unchanged externally — internal seams only.
**Exit:** Every existing render still works, but powered by projections instead of direct state reads.

### A.4 — Versioned sliced SSE
**Goal:** Bandwidth-aware fanout; per-client subscriptions.
- `subscribe=`, `since.X=`, `restartToken=` query params.
- Per-stream version counters in payload.
- Per-client last-sent map.
- 15s heartbeat, 60s full resync (optional).
**Exit:** 20 simultaneous overlays produce <50KB/s idle, <500KB/s score storm. Reconnect <2s.

### A.5 — Snapshots + replay
**Goal:** Periodic snapshots, replay engine.
- Snapshot writer (periodic + on-demand).
- Replay reads (snapshot + audit log tail).
- `/comp/audit` view with scrubbable timeline.
- Rollback command (`competition.retractScore`) with role check.
**Exit:** Disaster restore drill — kill DB mid-event, restore from snapshot, replay tail, system catches up cleanly.

### A.6 — Broadcast orchestrator: PGM/PRE
**Goal:** First-class PRE buffer + TAKE.
- `broadcast.cueScene` / `broadcast.takeScene` commands.
- PRE persisted alongside PGM.
- Director console: PGM/PRE monitors, TAKE button (T hotkey).
- Scene catalogue (Part III §16.3) implemented declaratively.
**Exit:** Director runs a full event using only CUE/TAKE.

### A.7 — Scene transitions + layer state machine
**Goal:** Scene-owned animations, layer state machine.
- Declarative scene transitions (Part III §17).
- Layer state machine running per layer with timeout protection.
- Audit log includes per-step `Broadcast.OverlayTransitionStep` events.
**Exit:** Champion reveal runs identically every time; full transition sequence visible in audit.

### A.8 — Queue, sponsor, ASTON
**Goal:** Auxiliary broadcast surfaces.
- Cue queue (`broadcast.queueAdd/Remove/Reorder/TakeNext`).
- Sponsor rotation policy.
- ASTON channel for commentator captions.
- Commentator console surface.
**Exit:** Commentator pushes a fact card live during a heat; sponsor bug cycles on schedule.

### A.9 — Overlay runtime hardening
**Goal:** Codify Part IV rules.
- Document `_built` guard and patch-not-replace rule.
- Add `eventbar.html`, `sponsorbug.html`, `aston.html` overlays.
- Per-overlay subscription manifests.
**Exit:** 30-minute event-storm test produces no overlay flicker, animation reset, or DOM thrash.

### A.10 — Operator surfaces (UI system)
**Goal:** Part V realised.
- Director console redesign (Part V §35.1 + wireframe §40.1).
- Lane judge phone redesign (§40.2).
- Master judge tablet view (§40.3).
- Commentator console (§40.4).
- Arena display (§40.5).
- Token system across all surfaces.
**Exit:** Stage rehearsal with four operators using their dedicated surfaces — no operator needs to fall back to another role's screen.

### A.11 — Audit/replay UI
**Goal:** Owner-facing audit & rollback.
- `/comp/audit` view (wireframe §40.6).
- Rollback with reason capture, role check.
- Forensic state-at-time scrubbing.
**Exit:** A disputed score can be inspected, the audit trail produced, and the score retracted with reason — all without restarting the server.

### A.12 — Operational maturity
**Goal:** Run a paying event.
- Structured logs (JSON, principal, cmdId).
- CSRF on all command endpoints.
- Per-session rate limiting on `/command/*`.
- Runbook for top 5 failure modes.
- Off-host snapshot backup verified by quarterly restore drill.
**Exit:** First paid event runs without operator intervention beyond the planned operator workflows.

### Cross-cutting (gates progress)

- **DB connection scoping** (`g.db` request-scoped) — gates anything multi-worker.
- **Schema migration framework** — gates Phase A.5 (snapshots assume schema versioned).
- **CI replay test** — gates A.3 onward (catches projection non-determinism early).

### Dependency graph

```
A.0 ─► A.1 ─► A.2 ─► A.3 ─► A.4 ─► A.5
                       │       │
                       ▼       ▼
                      A.6 ─► A.7 ─► A.8
                                      │
                                      ▼
                                     A.9 ─► A.10 ─► A.11 ─► A.12
```

---

## 45. Open decisions (deferred)

Decisions deliberately not made in this Phase A doc. Each must be called before the relevant sub-phase ships.

| Decision                                                  | Affects | Default recommendation                             |
|-----------------------------------------------------------|---------|-----------------------------------------------------|
| In-process vs Redis event bus from start                  | A.3     | In-process; switch when going multi-worker         |
| Disk-mirror projections vs memory-only                    | A.3     | Memory only; disk only if cold-start time hurts    |
| Snapshot format: single JSON vs SQLite VACUUM INTO + JSON | A.5     | VACUUM INTO for DB, JSON for broadcast             |
| Hotkey scheme: configurable per-operator vs fixed         | A.10    | Fixed v1; configurable when we have >1 director    |
| `sponsorRotation` config: DB-backed vs file-backed        | A.8     | File-backed v1; DB when multi-tenant               |
| Idempotency window: 30s memory vs Redis                   | A.2     | 30s memory; Redis when multi-worker                |
| Audit log rotation policy: size vs time                   | A.0     | Size (16MB files); easier to inspect               |

---

## 46. Glossary (broadcast vocabulary)

- **PGM (Program)** — currently on-air output.
- **PRE (Preview)** — staged scene, not yet on-air.
- **AUX** — auxiliary output (monitor, archive).
- **TAKE** — promote PRE to PGM with a transition.
- **CUT** — instant TAKE with no transition.
- **CUE** — stage content into PRE without going on-air.
- **AUTO** — orchestrator drives; opposite of MANUAL.
- **DSK (Downstream Keyer)** — layer that sits above all scene content (bug, ASTON).
- **BUG** — persistent corner graphic, usually sponsor.
- **ASTON / Super** — overlaid caption (commentator-driven lower-third).
- **L3 / Lower-third** — overlay in the lower portion of frame, athlete identity.
- **GFX channel** — a discrete graphics output (one channel per overlay layer in our model).
- **GPI (General Purpose Interface)** — external trigger input (future: hardware buttons).
- **TC (Timecode)** — frame-accurate clock reference (future: livestream sync).
- **VTR** — historical name for replay (now disk-based).
- **AUTO/MANUAL (per-field)** — does this field follow engine truth or director override?
- **Slot** — a parameter on a scene definition.
- **Binding** — a slot resolved to a concrete value.
- **Restart token** — process-start epoch broadcast in every SSE payload.
- **Snapshot** — internally consistent point-in-time capture of authoritative + broadcast state.
- **Stream** — a versioned event sequence (competition, broadcast, judging).
- **Slice** — a named subset of state delivered as an SSE delta.
- **Plane** — one of four state categories (authoritative, broadcast, ephemeral, session).

---

*End of Phase A: State, Protocol, Orchestration & Runtime.*
*Next: when Phase A.0 begins, a separate plan document will translate this spec into ordered implementation steps with file paths and acceptance tests.*
