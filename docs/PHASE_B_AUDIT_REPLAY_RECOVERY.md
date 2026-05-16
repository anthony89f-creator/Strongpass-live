# StrongPass — Phase B: Audit, Event Stream, Replay & Recovery
**Phase:** Architecture — Audit / Replay / Recovery
**Date:** 2026-05-16
**Status:** Design artefact. No implementation code.
**Predecessors:** `SYSTEM_DESIGN.md`, `PHASE_A_STATE_AND_PROTOCOL.md`
**Doctrine inspirations:** aviation FDR/CVR redundancy, SMPTE broadcast replay infrastructure, esports demo/replay systems (GOTV-class), industrial SCADA tamper-evident logs.

---

## 0. Why this document exists

A live broadcast competition has three failure shapes that must be survivable without operator panic:

1. **Process death mid-heat.** Server crashes between a judge submission and the SSE fanout. The operator notices nothing; the system recovers in seconds; no score is lost or duplicated.
2. **Disputed outcome.** Athlete coach asserts the leaderboard at 14:33:22 showed a different total. We must produce: who entered which value, who confirmed it, what was on-air at that exact second, and what the leaderboard projection actually computed.
3. **Catastrophe.** Disk failure, host destruction, ransomware. We must restore the competition to within 5 seconds of its last good state from an off-host archive.

This document specifies the audit, event stream, replay and recovery systems that make those three survivable. It is the **black box** of the platform: when anything goes wrong, this is the system we ask.

---

## 1. Core doctrines

Three non-negotiable principles. Every later decision derives from them.

### 1.1 The audit log is the line

> **Nothing has happened until it is sealed in the audit log. Once sealed, it has happened forever.**

- Validation, projections, SSE fanout, UI updates — all are *consequences* of sealing.
- Crash before seal: as if nothing occurred.
- Crash after seal: state reconstructs the consequence on next start.
- The audit log is **append-only**. There is no `UPDATE`, no `DELETE`, no compaction that rewrites history.

### 1.2 Replay is read-only

> **The replay engine never writes back into the live event log.**

- To change history, append a compensating event (forward only).
- To explore "what if", branch into a sandbox process attached to an isolated log copy.
- Branch protection is enforced at storage layer: the live writer holds an exclusive append lock; replay readers cannot acquire it.

### 1.3 Determinism is mandatory

> **Given a snapshot and the subsequent event log tail, the system must produce bit-identical state.**

- Projections are pure functions. No `now()`, no random IDs, no external calls.
- Wall-clock reads use the event's `serverTs`, not the projection's local clock.
- A daily CI test ("replay determinism") asserts a recorded event run produces a matching final-state hash.

---

# PART I — THE EVENT STORE

## 2. Three log streams

A flight-data-recorder system records *multiple correlated channels* (pilot voice, instrument readouts, control surface positions) because no single channel answers every question. StrongPass adopts the same pattern: three logs, each answering a different question.

| Log         | Records                                | Answers                                          | Retention   |
|-------------|----------------------------------------|---------------------------------------------------|-------------|
| **Event log** | Sealed domain events (decided facts)  | "What did the engine decide?"                    | Long (per §17) |
| **Command log** | All commands received (incl. rejected) | "What was attempted? By whom? With what outcome?" | Medium      |
| **Wire log**  | Outbound SSE frames per client        | "What did each overlay/console actually receive?" | Short (24h) |

The three are independently durable and time-correlated. Diagnosing "the leaderboard showed wrong total" might involve all three:
- Event log shows the scores recorded.
- Wire log shows what the overlay was sent.
- Command log shows whether the director tried to pin to a different category.

### 2.1 Why command log separate from event log

A rejected command is a fact too. Someone with the wrong role tried to retract a score; someone hit "advance heat" before the master judge sealed. These reveal operational issues without ever producing an event.

### 2.2 Why wire log separate from event log

State and what was sent to clients can diverge by 100ms or by hours (a stuck SSE connection, a buffered nginx response). When an operator says "the overlay was wrong", we need both halves.

---

## 3. Event log physical model

The event log is the foundational artefact. Everything else derives from it.

### 3.1 Format

Append-only line-delimited JSON (NDJSON). Each line is one event. Each file is one **segment**.

```
audit/
  events-000001.jsonl     ← sealed (immutable, hash-chained)
  events-000002.jsonl     ← sealed
  events-000003.jsonl     ← currently being written
  events.idx              ← index (offset, version, ts per event)
  events.chain            ← rolling chain head hash
```

### 3.2 Segment rollover

A segment seals and a new one opens when **any** of:
- Segment reaches `16 MB` (configurable).
- Segment reaches `100,000 events`.
- Segment age reaches `24 hours`.
- Operator explicitly seals (post-event).

On seal: the segment's tail hash is written into its filename's sidecar (`events-000003.jsonl.sealed`) and the chain head advances.

### 3.3 Hash chain (tamper evidence)

Every event carries `prevHash` and `selfHash`. `prevHash` is the `selfHash` of the immediately prior event. `selfHash = SHA-256(canonical(event_without_selfHash))`.

```jsonc
{
  "auditId":   18923,
  "ts":        "2026-05-16T14:33:22.918Z",
  "prevHash":  "9f3b2a…c1",
  // ... event fields ...
  "selfHash":  "7e2a4f…d8"
}
```

Properties:
- Tamper of any field changes the hash; chain breaks become detectable.
- A periodic chain verifier rescans recent segments and asserts every `prevHash` matches its predecessor's `selfHash`.
- The chain head (`events.chain`) is fsynced to a separate filesystem (or off-host) at low frequency — a final tamper-proofing tier.

### 3.4 Canonical JSON for hashing

Order-stable, whitespace-stable, key-sorted serialisation. Required for deterministic `selfHash`. Specifically:

- Keys sorted lexicographically.
- No insignificant whitespace.
- UTF-8 NFC normalisation.
- Numbers serialised in their canonical form (no `1.50`, write `1.5`).
- `null` for absent optional fields (don't omit).

### 3.5 Two-tier durability

The lifecycle of an event mid-write:

```
[1] command handler builds event
[2] event added to in-memory ring buffer  ← "in-flight"
[3] write to OS page cache via append
[4] fsync                                 ← "sealed" — survives kernel crash
[5] chain head updated                    ← appears in events.idx
[6] command response returned
[7] projections / SSE fanout fire
```

Until step 5, the command **has not happened** — a crash between 3 and 5 leaves the event as bytes on disk that the recovery scan discards (because the index doesn't reference it). Idempotency in the command ID means the client safely retries.

### 3.6 Group commit

Under load (score storm at heat end), multiple events may arrive within the same fsync window. The writer batches: gather pending events into one fsync call. Properties:

- Worst case latency: `commitWindowMs` (default 5ms).
- Latency unchanged at low load (single event → single fsync).
- Throughput scales with the batch size.
- All events in a batch share a "commit barrier" — they become visible atomically.

### 3.7 In-memory tail

The most recent `replayWindowSec` (default 300s) of events is held in a ring buffer in memory:

- SSE reconnects within the window stream from memory (no disk read).
- Projection rebuilds within the window stream from memory.
- Beyond the window: disk read of the appropriate segment.

The window is sized for the worst real-world reconnect (network blip, OBS browser source reload), not for replay scrubbing.

### 3.8 Index

A compact secondary file maps `auditId → byte offset`. Used for:
- O(1) lookup by `auditId`.
- Binary search by `ts` (`bisect` on the (ts, offset) pairs).
- Binary search by `version` per stream.

Index is rebuildable from the segments — it is *not* part of the durability story. Loss of index triggers full rescan on restart (acceptable for our scale).

---

## 4. Event schema (canonical)

```jsonc
{
  "auditId":      18923,                 // monotonic 64-bit, gap-free per writer
  "ts":           "2026-05-16T14:33:22.918Z",   // server clock, ISO-8601 ms UTC
  "serverEpochMs": 1779285202918,        // server clock as int (deterministic compare)
  "monotonic":    18439528,              // process monotonic ns at write (drift-free)
  "stream":       "competition",         // "competition" | "broadcast" | "judging" | "identity"
  "version":      4422,                  // monotonic per stream
  "event":        "Competition.ScoreRecorded",
  "payload":      { /* event-specific */ },

  "principal": {                          // who caused it
    "kind":      "operator",             // "operator" | "system" | "replay"
    "id":        "op-tony",
    "session":   "s-91ac",
    "device":    "d-MacBook-Tony",
    "role":      "scoring",
    "ip":        "10.0.0.4"
  },

  "cmdId":        "cmd-91ac4e",           // command that originated this (null for system events)
  "traceId":      "tr-7b2af1",            // end-to-end trace across cascaded events
  "causationId":  null,                   // parent event auditId, for derived events
  "compensates":  null,                   // set to compensated auditId on rollback events

  "restartToken": 31822,                  // process epoch
  "schemaVersion": 1,                     // event-schema rev (for forward-compat reads)

  "prevHash":     "9f3b2a…c1",            // hash chain
  "selfHash":     "7e2a4f…d8"
}
```

### 4.1 Field rules

- **All fields required.** Missing fields are an error, not a default. (`payload` may be `{}` for events with no data.)
- **`ts` is informational; `serverEpochMs` is authoritative for ordering.** Two events with identical `serverEpochMs` are ordered by `monotonic`.
- **`principal.kind: "system"`** for engine-emitted events (e.g. `Broadcast.OverlayTransitionStep` during a scripted reveal).
- **`principal.kind: "replay"`** for events produced by a replay/sandbox process. These are never written to the live log — they exist only in the sandbox.
- **`schemaVersion`** lets us add fields without breaking old replays. Readers ignore unknown fields; writers never remove fields.

---

## 5. Command log

Same physical pattern (segmented NDJSON, hash chain) but separate filesystem path:

```
commands/
  commands-000001.jsonl
  commands-000002.jsonl  ← current
  commands.idx
  commands.chain
```

### 5.1 Schema

```jsonc
{
  "cmdAuditId":   90215,
  "ts":           "2026-05-16T14:33:22.701Z",
  "serverEpochMs":1779285202701,
  "monotonic":    18439461,
  "cmdId":        "cmd-91ac4e",
  "principal":    { /* as event */ },
  "traceId":      "tr-7b2af1",
  "domain":       "competition",
  "name":         "recordScore",
  "payload":      { /* command body — exactly as received */ },
  "expected":     { "competition.version": 4421 },
  "outcome": {
    "ok":           true,
    "version":      4422,
    "eventsEmitted":[{ "auditId":18923, "stream":"competition", "version":4422 }],
    "elapsedMs":    14,
    "error":        null
  },
  "prevHash":     "…",
  "selfHash":     "…"
}
```

### 5.2 Why log rejected commands

- Operational intelligence: "the director hit cut-to-black 3× in one heat — was something wrong?"
- Security: brute-force or role-violation attempts are visible.
- Debugging: "the score didn't appear" might be a `stale_version` rejection we can now see.
- Idempotency replays appear here too (with `outcome.error.code = "idempotent_replay"`).

---

## 6. Wire log

Captures what *actually* went out to each connected client over SSE. Higher volume, shorter retention, optional disk persistence (memory ring + sampling first).

```
wire/
  wire-YYYYMMDD-HH.jsonl       ← hourly segments
  wire.idx
```

### 6.1 Schema

```jsonc
{
  "wireId":      8217401,
  "ts":          "2026-05-16T14:33:22.940Z",
  "serverEpochMs":1779285202940,
  "clientId":    "cli-91ac",           // server-side connection id
  "principal":   { "kind":"operator","id":"op-tony","session":"s-91ac" },
  "kind":        "delta",              // "delta" | "resync" | "heartbeat" | "error"
  "subscribe":   ["lanes","leaderboard.U90","eventState"],
  "since":       { "competition":4421, "broadcast":812 },
  "versionsSent":{ "competition":4422, "broadcast":812 },
  "slices":      ["lanes","leaderboard.U90"],
  "bytes":       1438,
  "auditRefs":   [18923],              // event auditIds this delta is *because of*
  "userAgent":   "Mozilla/5.0 … OBS/30.1"
}
```

### 6.2 Retention

- Memory-resident: last 5 minutes (every frame).
- Disk: 24 hours, full fidelity.
- Beyond: discarded unless flagged "preserve" by a forensic operator during the event.

We do **not** keep wire logs forever — they are 100× larger than event logs and they are recoverable in spirit from event+command logs.

### 6.3 Wire log replay

A test overlay can be fed a recorded wire log to reproduce exactly what the live overlay rendered at the time. This is the deepest forensic capability and is reserved for dispute resolution.

---

# PART II — TIME & ORDERING

## 7. Authoritative clock

> **The server's NTP-synchronised wall clock is authoritative for `ts`. The server's monotonic clock is authoritative for ordering within a process. No client clock is ever authoritative for anything.**

### 7.1 Clock setup (operational)

- `chrony` on the production host, polling 4 NTP sources (pool + Cloudflare time + Google time).
- Drift alarm: warn if offset > 50ms, page if > 250ms.
- A clock-skew event is recorded automatically when the offset crosses a threshold; later replays can adjust if needed.

### 7.2 Three clock fields per event

- `ts` — ISO-8601 wall clock, ms precision. Human-readable. For UI display.
- `serverEpochMs` — int. Authoritative for ordering across events with possibly the same `ts` truncation.
- `monotonic` — process nanoseconds since boot. Authoritative for ordering *within a single process lifetime*. Resets on restart.

### 7.3 Cross-restart ordering

Within a single process lifetime, `monotonic` totally orders events.
Across restarts, `serverEpochMs` totally orders events (assuming NTP keeps the wall clock monotonic, which `chrony` does with slewing).
`restartToken` marks the boundary; the recovery flow uses it to detect process restarts in the log.

### 7.4 Client timestamps

Commands may carry `issuedAt`. The server records it in the command log but **always** writes its own `ts` and `serverEpochMs` onto resulting events. We never trust the client for ordering.

When `issuedAt` deviates from `serverEpochMs` by more than 5 seconds, we record a `clockSkew` field on the command — useful when triaging "judge submitted at 14:33:22 but server logged 14:33:25" disputes.

---

## 8. Ordering model

Per **stream**, events are totally ordered by their `version` field. There are three streams (`competition`, `broadcast`, `judging`) and a process-internal identity stream.

### 8.1 Version allocator

A single in-process counter per stream. Atomic increment. The increment happens *inside* the seal path (between fsync prep and chain hash) so allocated versions are gap-free.

### 8.2 Cross-stream ordering

Cross-stream ordering is **not** guaranteed by version alone (`broadcast.v=812` is unrelated to `competition.v=4422`).

For *cross-stream causality*, use `traceId` + `causationId`:
- `Judging.LaneSubmitted` (traceId=tr-7b2af1) **causes** `Competition.ScoreRecorded` (same traceId, causationId=audit of LaneSubmitted).
- Replays preserve causation by following the trace tree, not by interleaving streams by version.

### 8.3 Multi-worker future

Today: 1 gunicorn worker → single writer per stream → trivially correct.

Tomorrow (multi-worker / multi-host, Phase H+):
- A **lease-based primary** holds the writer lock per stream. Other workers proxy commands to it (over loopback or Redis).
- Failover: the primary's lease expires → second worker promotes → resumes from the last sealed `version`.
- Lease window must exceed worst-case fsync time (default 2s).
- This is **not** built in Phase B; the design is forward-compatible.

### 8.4 SSE delivery ordering

SSE preserves order **within a single connection**. On reconnect, the protocol's `since.X=` parameters give the server enough information to resume in order. Critical detail: the server **buffers** outbound frames per client and delivers in version order; it never sends `v=815` before `v=814` even if `815` is computed first.

---

## 9. Correlation IDs

Five IDs co-travel with events. Each answers a different question.

| ID            | Lifetime              | Purpose                                                |
|---------------|-----------------------|---------------------------------------------------------|
| `cmdId`       | one command           | Idempotency, dedup. Client-supplied UUIDv7.            |
| `traceId`     | one *user intent*     | Spans cascaded events. Server-allocated at command entry. |
| `causationId` | immediate parent      | Direct parent event for derived events.                |
| `sessionId`   | operator login session| Attribution across multiple commands by same login.    |
| `principalId` | the human/system      | Stable across sessions ("op-tony").                    |

### 9.1 Example cascade

`broadcast.takeScene(scene.champion)` →
- Cmd received: `cmdId=cmd-7d20fe`, server allocates `traceId=tr-bba90c`.
- Emits `Broadcast.SceneTaken` (causationId=null, traceId=tr-bba90c)
- Emits `Broadcast.OverlayHidden(SCOREBUG)` (causationId=audit of SceneTaken, traceId=tr-bba90c)
- Emits `Broadcast.OverlayHidden(REPS)` (same)
- Emits `Broadcast.OverlayTransitionStep(LOWERTHIRD.out)` (same)
- Emits `Broadcast.OverlayShown(CHAMPION)` (same)
- Emits `Broadcast.OverlayTransitionStep(CHAMPION.in)` (same)

All six events share `traceId`. The audit viewer can render the whole cascade as one expandable group.

### 9.2 Trace tree reconstruction

Given `traceId`, the viewer builds a parent-pointer tree using `causationId`. Top of tree → originating command. Leaves → terminal effects. Useful for:
- Auditing the full effect of a single director action.
- Replay step-by-step at the *trace* granularity rather than the event granularity.
- "What did this score recording actually cause?" investigations.

---

# PART III — SNAPSHOT ARCHITECTURE

## 10. Snapshot tiers

Snapshots are the **anchors** that bound how far back replay must walk. Without them, restoring from scratch means replaying every event since the system began. With them, restoring is O(window-since-snapshot).

Four tiers, each with different cadence, retention, and use.

| Tier            | Cadence                       | Retention                       | Use                              |
|-----------------|-------------------------------|----------------------------------|----------------------------------|
| **Micro**       | Every N events (default 200)  | In-memory ring, last 50          | SSE reconnect fast path          |
| **Periodic**    | Every 5 minutes               | Local disk, last 24h hourly      | Process recovery, scrub anchor   |
| **Milestone**   | On heat advance, event advance| Local disk, full event lifetime  | Per-heat scrub anchor, dispute   |
| **Final**       | On competition close          | Object storage, **forever**      | Long-term archive, public record |

### 10.1 Micro-snapshot

Cheap in-memory capture. Used so the *next* SSE reconnect doesn't have to replay 200 events — just one snapshot apply + the few-event tail.

Not durable. Lost on restart. That's fine — its job is just to accelerate reconnects.

### 10.2 Periodic snapshot

Full disk snapshot:

```
snapshots/
  periodic/
    YYYYMMDD-HHMMSS-<auditCursor>/
      comp.db.snapshot                  ← VACUUM INTO of SQLite
      broadcast.pgm.json
      broadcast.pre.json
      broadcast.queue.json
      broadcast.overrides.json
      projections/                      ← optional materialised
        proj.lanes.json
        proj.leaderboard.U90.json
        …
      manifest.json
```

`manifest.json` declares:

```jsonc
{
  "snapshotId":   "snap-20260516-143000-7f2a",
  "tier":         "periodic",
  "createdAt":    "2026-05-16T14:30:00.018Z",
  "auditCursor":  18922,                    // last event included
  "versions":     { "competition":4421, "broadcast":812, "judging":1014 },
  "restartToken": 31822,
  "comp": { "dbBackupRef": "comp.db.snapshot", "schemaVersion": 7 },
  "hash":         "sha256:…"                // hash of all included file hashes
}
```

### 10.3 Milestone snapshot

Same format as periodic, but pinned. Triggered by:
- `Competition.HeatAdvanced`
- `Competition.EventAdvanced`
- `Competition.EventCompleted`
- Explicit operator request

Lives for the lifetime of the competition. The audit viewer's "jump to start of heat 3" function reads a milestone snapshot rather than periodic.

### 10.4 Final snapshot

Created on competition close. Same format plus:
- Compressed.
- Encrypted with the org's archival key.
- Uploaded to object storage (S3-compatible).
- Indexed by `{org}/{competitionSlug}/final.snapshot.tar.gz.enc`.
- Manifest indexed in a public results table for fast lookup.

Final snapshots are the **source of truth** for completed competitions. After cutover (default 30 days), the local periodic snapshots are pruned; the final remains in archive forever.

### 10.5 Snapshot integrity

Each snapshot manifest includes a content hash. Recovery verifies the hash before applying. A corrupted snapshot is logged + skipped; the recovery falls back to the prior valid snapshot + a longer event-log replay.

---

## 11. Snapshot lifecycle

```
event written → audit cursor advances → snapshotter checks:
  ├─ time since last periodic > 5min?            → write periodic
  ├─ milestone event?                            → write milestone (also counts as periodic)
  ├─ events since last micro > 200?              → capture micro (memory only)
  └─ competition closed?                         → write final + archive
```

### 11.1 Pruning

- Periodic: keep last 24h every snapshot; 1–7 days hourly; 7–30 days daily; then drop.
- Milestone: keep for life of competition; archived alongside final.
- Final: never deleted.

### 11.2 Snapshot writing without blocking the writer

We must not pause command processing for 200ms while a snapshot writes.

Strategy:
- SQLite `VACUUM INTO` runs against a read-only handle; the WAL-locked writer continues.
- Broadcast state is captured by atomic copy of the current PGM/PRE JSON files (filesystem `link` + `rename`).
- Projections are captured from their in-memory map (read-locked microseconds).
- Manifest computed and written last.

End-to-end snapshot wall time target: ≤ 200ms for periodic at our scale.

---

# PART IV — REPLAY ENGINE

## 12. Replay engine modes

The replay engine has five distinct modes; one underlying state machine.

| Mode        | Direction | Side-effects                | Use case                         |
|-------------|-----------|------------------------------|----------------------------------|
| **Recover** | forward   | Hydrates live engine state   | Process restart, disaster recovery |
| **Forensic**| any (read-only) | Materialises state at T | Audit viewer, dispute resolution |
| **Sandbox** | forward   | Writes to *separate* log copy | Rehearsal, what-if, testing      |
| **Wire**    | forward   | Re-emits SSE frames to a test client | Overlay forensic reproduction |
| **Diff**    | bidirectional, read-only | Computes state delta between T1 and T2 | "What changed?" queries |

### 12.1 Recover mode

Used on process start, only.

```
[1] read latest valid periodic snapshot
[2] hydrate engine: Plane 1 + Plane 2 + projections from snapshot
[3] versions := snapshot.versions
[4] scan event log from snapshot.auditCursor + 1
[5] for each event:
      apply to in-memory state
      run projection invalidations (no SSE fanout yet)
      update versions
[6] open command/SSE servers
[7] mint new restartToken
[8] emit System.RestartCompleted event (marks the restart in the log)
[9] first SSE frame to every reconnecting client is a full resync with new restartToken
```

Crucially: recover-mode replay **does not re-write events to the live log**. The events are already there; we are recomputing the derived state they imply.

### 12.2 Forensic mode

Used by audit viewer and dispute resolution.

```
inputs: target_time T (or target auditId, or target version)
[1] find nearest periodic/milestone snapshot S with createdAt ≤ T
[2] hydrate a transient state from S into a forensic working set
[3] scan events from S.auditCursor + 1, stopping at T
[4] apply each to working set; project projections as needed
[5] expose working set read-only to UI
```

Working set is in-memory only, isolated from live state. No SSE fanout. No file writes.

### 12.3 Sandbox mode

Used for rehearsal, regression tests, "what if I retracted that score" exploration.

```
[1] clone the latest periodic snapshot to /tmp/sandbox-<id>/
[2] copy event log up to the chosen branch point
[3] start a *separate* replay process binding to that copy
[4] operator interacts via a sandbox-flagged UI
[5] all events the sandbox produces land in /tmp/sandbox-<id>/audit/
[6] sandbox is destroyed on exit
```

The sandbox shares no file handles with production. Branch protection is filesystem-enforced.

### 12.4 Wire mode

The deepest forensic level: feed a recorded wire log into a fresh overlay browser and watch it render exactly what aired.

```
[1] choose target client (clientId) and time window [T1, T2]
[2] extract wire frames for that client in that window
[3] open a controlled headless browser with the overlay
[4] play frames in original temporal cadence (or at chosen speed)
[5] capture screenshots / video for evidence
```

Wire-mode is rare. We build it for one reason: undisputed answers to "what did the audience actually see at time T".

### 12.5 Diff mode

```
inputs: T1, T2
[1] produce forensic state at T1
[2] produce forensic state at T2
[3] structural diff with custom rules:
      - score changes per athlete per event
      - leaderboard rank changes
      - on-air scene changes
      - layer visibility changes
      - override flips
[4] render as a structured changelog
```

Used by the audit viewer's "what changed in this heat?" panel.

---

## 13. Determinism rules

The replay engine is only as good as the determinism of the code it replays. Mandatory rules for any code that participates in projection or state mutation:

### 13.1 No wall-clock reads inside projections or domain handlers

Use the event's `serverEpochMs` if you need time.

### 13.2 No random IDs

IDs are server-allocated at sealing time and stored in the event. Replays read the existing IDs; they never generate new ones.

### 13.3 No external calls

No HTTP, no DNS, no file reads, no environment variable reads. Domain handlers are closed loops. Configuration is read at startup and pinned to the process.

### 13.4 Stable iteration order

`dict` iteration is insertion-ordered in Python; rely on that, but also document. Sorting where order matters: explicit key.

### 13.5 No locale or float surprises

Score formatting uses fixed locale (`en_US`). Float comparisons use defined epsilon. Number serialisation is canonical (§3.4).

### 13.6 Determinism CI test

Daily CI job:
1. Replay a recorded reference event log from snapshot.
2. Hash the resulting state (Plane 1 + Plane 2 + projections).
3. Compare to stored expected hash.
4. Fail loudly on mismatch.

A new mismatch is treated as a P1 — non-determinism is invisible until it bites in a disaster recovery.

---

## 14. Timeline scrubbing

### 14.1 Scrub UI requirements

The audit viewer shows a timeline. Operator drags a playhead. UI updates to show the state at that moment. Backing implementation:

- Periodic snapshots are **scrub anchors**. The playhead "snaps" between anchors for efficiency.
- Sub-anchor positions: from anchor, apply forward events one by one.
- Reverse scrubbing: snap to the prior anchor, then forward-apply.

### 14.2 Performance budget

- Anchor jump: ≤ 200ms (load snapshot + first projection).
- Sub-anchor step: ≤ 30ms per event applied.
- Full event-tier scrub from any point in a 4-hour comp: ≤ 1 second.

### 14.3 Speed controls

| Speed  | Behaviour                                       |
|--------|-------------------------------------------------|
| Pause  | Hold current state                              |
| Step   | Advance one event per click                     |
| 0.25× to 4× | Wall-clock paced replay using `ts` gaps     |
| Max    | Apply events as fast as projections can compute |

Step mode is the most useful: walk through a heat event-by-event to understand cascading effects.

### 14.4 Scrub safety

The scrub UI is **always** in forensic mode (read-only). To act on a finding (retract, restore, etc.) the operator exits scrub mode and issues a typed command against live state. Branch protection prevents accidents.

---

## 15. Historical overlay reconstruction

The single most-asked forensic question: **"What was on-air at time T?"**

Three layers of answer, each more authoritative:

### 15.1 PGM state at T (event-derived)

Forensic replay to T → read `pgm.*` slices → describe scene + layer visibility.

This answers "the engine believed the champion overlay was visible". Always available.

### 15.2 Wire log at T

The frames actually sent to each connected client around T. Confirms the bytes left the server.

Available for events within the wire-log retention window (24h default).

### 15.3 Wire-mode replay

Feeding the wire frames to a controlled overlay and capturing the rendered pixels.

The definitive answer: what the audience saw. Reserved for dispute resolution (slow, manual).

### 15.4 Overlay "render at T" preview

The audit viewer offers an inline preview pane that:
- Materialises PGM state at T (forensic replay).
- Renders the overlay HTML in a sandboxed iframe with the materialised slice as its only input.
- Shows the *intended* on-air visual (not the actually-aired — that requires wire-mode).

For most dispute scenarios this is enough.

---

# PART V — AUDIT SYSTEM

## 16. Audit viewer architecture

The audit viewer is a single application surface, queried like an investigative tool, not a CMS.

### 16.1 Core queries

The viewer must answer, in <500ms:

1. **Who changed this?** Given an `auditId`, return event + principal + cmd + trace.
2. **When did X change?** Given a state field and a time range, return events that mutated it.
3. **What was live at time T?** Forensic replay to T → describe PGM.
4. **What did this operator do?** Given a session or principal, list commands and emitted events with outcomes.
5. **What caused this event?** Walk causationId/traceId tree.
6. **What changed between T1 and T2?** Diff mode (§12.5).
7. **What did the overlay receive?** Wire-log slice for the client.
8. **What was rejected?** Filter command log by `outcome.ok=false`.

### 16.2 Filter dimensions

- By stream (competition / broadcast / judging / identity)
- By event name (autocomplete from registry)
- By time range (absolute, relative, "this heat", "this event", "this competition")
- By principal (operator id, session, device)
- By trace (all events in a trace)
- By athlete / event / category / lane (payload filters)
- By outcome (ok/rejected for commands)
- By tag (operator-applied during the event for "interesting" moments)

### 16.3 Search

Free-text search across event payloads, with a tokenised inverted index built lazily as segments seal. Searching unsealed segments scans linearly (cheap because in-memory).

### 16.4 Export

Operators must be able to export:
- The full event log for a competition (as the original NDJSON segments).
- A redacted version for a dispute (only the events relevant to a specific lane/athlete).
- A signed PDF of a single event with its trace context and hash chain proof (for legal evidence).

### 16.5 Authority

The audit viewer is **read-only**. Rollback and other actions are issued through the live command surface — the audit viewer can deep-link there with the relevant `auditId` pre-filled.

---

## 17. Retention policy

Different categories of data, different lifetimes.

| Data                            | Hot (in-memory) | Warm (local disk) | Cold (object storage) | Forever      |
|---------------------------------|-----------------|--------------------|------------------------|--------------|
| Event log (sealed segments)     | last 5min       | 90 days            | per-competition tarball| if final     |
| Command log                     | last 5min       | 30 days            | 1 year                 | no           |
| Wire log                        | last 5min       | 24 hours           | (none by default)      | no           |
| Periodic snapshots              | last 50 micro   | 24h hourly         | (none)                 | no           |
| Milestone snapshots             | (n/a)           | full comp lifetime | comp tarball           | no           |
| Final snapshots                 | (n/a)           | 30 days            | indefinite             | **yes**      |
| Operator session logs           | live            | 30 days            | aggregated only        | no           |

### 17.1 Forever-retained artefacts (per competition)

- Final snapshot
- Full event-log tarball (gzip + AES-256, key per org)
- Final leaderboard PDF (signed, hash of source events)
- Audit summary: every retraction, every override, every system event

### 17.2 Right to deletion (future, GDPR-relevant)

Athletes will have a right to ask for their personal data removal. Event logs are append-only and *cannot* be mutated, so the model is:
- Mark the athlete row in the warm DB as redacted.
- The audit viewer respects the redaction marker and shows `[REDACTED]` instead of the name in payload renderings.
- The event log itself is not modified (its integrity guarantee depends on that).
- The signed final-snapshot still contains the original name; that's contractual when the athlete competed.

---

## 18. Cold storage & archive

### 18.1 Format

- Tar containing:
  - `event-log/` — all NDJSON segments + index + chain file
  - `commands/` — sealed command log
  - `snapshots/final.snapshot/` — final snapshot
  - `snapshots/milestones/` — all milestones
  - `manifest.json` — competition metadata + integrity hashes
- Compressed (zstd level 19)
- Encrypted (AES-256-GCM, key per org, stored in KMS or per-org secret)

### 18.2 Storage layout

Object storage path: `s3://strongpass-archive/{org}/{year}/{competitionSlug}/comp.tar.zst.enc`.

A second copy is replicated to a second region (or a second provider) for catastrophic resilience.

### 18.3 Retrieval

- Operator (or owner) requests retrieval via UI.
- Async job: pull, decrypt, validate hash chain, restore to staging path.
- Available read-only as a sandbox replay.
- Restore-to-live is gated behind a typed confirmation + owner role; *almost never* used (final snapshots are the canonical record).

### 18.4 Archive integrity check

Quarterly automated job:
- Pick 5% random archives.
- Decrypt, validate hash chain end-to-end, replay determinism check.
- Alert on any failure.

---

# PART VI — RECOVERY

## 19. Failure modes catalogue

Each failure mode has: **detection**, **immediate response**, **recovery**, **prevention**.

### 19.1 Process kill mid-command

| Aspect       | Detail                                                       |
|--------------|--------------------------------------------------------------|
| Detection    | systemd notices PID exit                                     |
| Response     | systemd restart in <2s                                       |
| Recovery     | Recover-mode replay from latest snapshot + tail              |
| Visible loss | Up to 1 command if it was mid-fsync (cmdId-deduped on retry) |
| Prevention   | Group commit + idempotency window                            |

### 19.2 Disk fsync failure (rare hardware/OS issue)

| Aspect       | Detail                                                       |
|--------------|--------------------------------------------------------------|
| Detection    | fsync returns error code                                     |
| Response     | Command rejected with `5xx`; logs error; alarms              |
| Recovery     | Filesystem issue — usually requires operator intervention    |
| Visible loss | None (rejection = command never happened)                    |
| Prevention   | Filesystem health checks, smart disks                        |

### 19.3 Disk full

| Aspect       | Detail                                                       |
|--------------|--------------------------------------------------------------|
| Detection    | Filesystem fill alarm at 80%, hard fail at >95%              |
| Response     | At 95%: writer refuses new commands → status page red        |
| Recovery     | Operator prunes oldest periodic snapshots, archives to S3    |
| Visible loss | None until threshold; complete write halt at hard limit      |
| Prevention   | Cold-storage cutover at 30 days; alarms at 60%/80%/95%        |

### 19.4 SQLite WAL corruption

| Aspect       | Detail                                                       |
|--------------|--------------------------------------------------------------|
| Detection    | Startup integrity check (`PRAGMA integrity_check`)           |
| Response     | Refuse start; alarm; degrade to staging                      |
| Recovery     | Restore from latest periodic snapshot; replay tail           |
| Visible loss | Window of un-snapshotted events (max 5min)                   |
| Prevention   | WAL mode + checkpointing + nightly `VACUUM`                  |

### 19.5 Audit log hash-chain break

| Aspect       | Detail                                                       |
|--------------|--------------------------------------------------------------|
| Detection    | Periodic chain verifier; recover-mode replay validates       |
| Response     | **Halt operations**. Pull off-host chain head for comparison |
| Recovery     | Identify break point, restore segment from off-host backup   |
| Visible loss | Possibly tail since last good chain anchor                   |
| Prevention   | Off-host chain head replication; tamper-evident filesystem (immutable bits where possible) |

### 19.6 Network partition

| Aspect       | Detail                                                       |
|--------------|--------------------------------------------------------------|
| Detection    | SSE clients fail to receive heartbeat                        |
| Response     | Overlays freeze last frame; director console shows DISCONNECTED |
| Recovery     | Auto-reconnect with exponential backoff; resume from `since` |
| Visible loss | None (state is preserved server-side; clients catch up)      |
| Prevention   | Redundant network path (future)                              |

### 19.7 Clock skew

| Aspect       | Detail                                                       |
|--------------|--------------------------------------------------------------|
| Detection    | `chrony` offset metric                                       |
| Response     | Warn at >50ms; emergency at >250ms                           |
| Recovery     | NTP slew (not step) to avoid backwards time                  |
| Visible loss | None for ordering (monotonic clock is unaffected)            |
| Prevention   | Multiple NTP sources; alarm to oncall                        |

### 19.8 Two operators issue conflicting commands

| Aspect       | Detail                                                       |
|--------------|--------------------------------------------------------------|
| Detection    | Single-writer serialisation; second command sees stale `expected.version` |
| Response     | Second command rejected with `stale_version` + current version |
| Recovery     | Second operator re-issues with refreshed expected             |
| Visible loss | None                                                         |
| Prevention   | UI shows the version on the action panel; renders other ops' commands as they happen |

### 19.9 SSE storm (client reconnect surge)

| Aspect       | Detail                                                       |
|--------------|--------------------------------------------------------------|
| Detection    | Reconnect rate metric > threshold                            |
| Response     | Per-client backoff + server-side resync rate limit           |
| Recovery     | Storm subsides as backoff staggers reconnects                 |
| Visible loss | Up to 30s SSE catch-up for some clients                       |
| Prevention   | Jitter on client backoff; cached resync payload per (sub, version) |

### 19.10 Catastrophic host loss

| Aspect       | Detail                                                       |
|--------------|--------------------------------------------------------------|
| Detection    | Health checks fail for >60s; oncall paged                    |
| Response     | Spin up replacement host from image                          |
| Recovery     | Pull latest periodic + tail from off-host backup → replay    |
| Visible loss | Up to RPO (5s of un-replicated events; per-event final is safe) |
| Prevention   | Off-host audit-log replication (rolling rsync)               |

---

## 20. Recovery flow (canonical)

```
[STAGE 0] systemd starts the process

[STAGE 1] Hash-chain verifier scans last 24h of segments
   - chain ok          → continue
   - chain break       → ALERT, refuse start, force operator intervention

[STAGE 2] Snapshot selector picks the latest valid periodic
   - integrity hash matches manifest? proceed; else fall back to prior snapshot.

[STAGE 3] Hydrate:
   - Open comp.db (or restore from snapshot.comp.db.snapshot)
   - Load broadcast PGM/PRE/queue/overrides from snapshot
   - Rebuild projections from snapshot (if materialised) or recompute lazily

[STAGE 4] Apply event log tail from snapshot.auditCursor + 1
   - Set versions to snapshot.versions
   - For each event:
       apply mutation
       update versions
       invalidate projections (no SSE fanout yet)
   - Track replay throughput and ETA

[STAGE 5] Mint new restartToken; emit System.RestartCompleted event

[STAGE 6] Open command and SSE servers
   - First SSE frame to every reconnecting client: full resync with new restartToken

[STAGE 7] Run determinism CI snippet:
   - Compare projected state hash against recovery start hash plus net mutations
   - Mismatch → alarm

[STAGE 8] Operational: ready for commands.
```

### 20.1 Targets

- **RPO (Recovery Point Objective):** ≤ 5 seconds — the worst case is the events between the most recent fsync and the crash. Group commit + idempotency on retry keeps real loss below 1 second.
- **RTO (Recovery Time Objective):** ≤ 60 seconds — warm restart from local periodic + tail.
- **DR RTO (Disaster Recovery):** ≤ 5 minutes — cold restart from off-host snapshot + log.

### 20.2 Drill cadence

- Monthly: kill the process mid-event in staging, verify RTO.
- Quarterly: simulate host loss, restore from off-host, verify DR RTO.
- Annually: full archive retrieval drill across all retained competitions.

---

## 21. Conflict recovery (multi-operator)

Even single-writer, operators conflict.

### 21.1 Operator-visible conflict surfaces

- **Concurrent edit on the same entity.** E.g. two scoring operators editing the same athlete record. Resolution: optimistic-lock with `expected.entity.version`. Second one rejects with diff preview.
- **Concurrent broadcast action.** E.g. director A TAKEs while director B CUEs a different scene. The TAKE proceeds because PRE matched expected; B's cue lands as a follow-up. Both are visible in the command log.
- **Override war.** A pins leaderboard to U80, B clicks "↺ AUTO". Last operator wins; both actions visible in the audit; the surface highlights the recent override change.

### 21.2 Resolution UI patterns

- Action panels show the **most recent operator** who modified the relevant field (e.g. "PINNED: U80 · by op-tony · 4s ago").
- Reject toasts include the conflict's underlying audit reference: "rejected: another operator advanced the heat ([v.812](#audit/812))".
- Audit viewer has a "conflict events" filter that surfaces all rejected-conflict commands.

### 21.3 No automatic merging

We never merge concurrent commands. We never re-order. The first to commit wins; later conflicting commands fail visibly. Live broadcasts require human-arbitrated conflict, not best-effort merges.

---

## 22. Command deduplication

### 22.1 The 30-second window

`cmdId` is hashed into a ring of recent commands at the command-log layer. A duplicate within 30s returns the cached outcome verbatim:

```jsonc
{
  "ok": true,
  "version": 4422,
  "eventsEmitted": [...],
  "elapsedMs": 0,
  "deduplicated": true   // flag
}
```

### 22.2 Beyond 30 seconds

Past the window, a duplicate `cmdId` is treated as a *new attempt*. This is intentional: a 30-second delay almost certainly means the client refreshed and is intentionally retrying. Repeated submissions with the same `cmdId` are *also* logged in the command log with their distinct `cmdAuditId`s, so we can spot pathological retry loops.

### 22.3 Idempotency keys for non-command operations

- Snapshot creation: idempotent by `snapshotId`.
- Archive upload: idempotent by content hash.
- Replay export: idempotent by `(competition, T1, T2, format)` hash.

### 22.4 What's not idempotent on its own

`broadcast.takeScene` is not idempotent across cmdIds — TAKE-ing twice is a meaningful operation (cycle PRE/PGM). The protection comes from `cmdId` *plus* the operator's intent that the second click is a new TAKE.

---

# PART VII — DELIVERABLES

## 23. Replay architecture diagram

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              LIVE ENGINE                                      │
│                                                                                │
│  cmd in ──► Command Validator ──► Domain Handler ──► Event Builder            │
│                       │                                       │                │
│                       ▼                                       ▼                │
│                 Command Log                              Event Log             │
│                 (append+chain)                           (append+chain)        │
│                       │                                       │                │
│                       │                              ┌────────┴────────┐       │
│                       │                              ▼                 ▼       │
│                       │                       Projection Bus      SSE Fanout   │
│                       │                              │                 │       │
│                       │                              ▼                 ▼       │
│                       │                       Projection Cache    Wire Log     │
│                       │                                                 │      │
│                       │                                                 ▼      │
│                       │                                            Clients     │
└───────────────────────┴────────────────────────────────────────────────┘──────┘
                       │
                       ▼
            ┌──────────────────┐
            │   SNAPSHOTTER    │  micro (every N events)
            │                  │  periodic (every 5min)
            └─────────┬────────┘  milestone (heat/event)
                      │           final (comp close)
                      ▼
                 Snapshots
                      │
                      ▼
              Off-host archive

                                    ┌─────────────────────────────────┐
                                    │   REPLAY ENGINE                 │
                                    │                                 │
   audit viewer ──► forensic ──┐    │  Recover  : on process start    │
   dispute tool ──► forensic ──┼────┤  Forensic : audit viewer        │
   rehearsal     ──► sandbox ──┼───►│  Sandbox  : separate process    │
   reproduce visual ──► wire ──┘    │  Wire     : headless overlay    │
                                    │  Diff     : T1 vs T2            │
                                    └─────────────────────────────────┘
```

---

## 24. Event lifecycle diagram

```
   Client                                                    Server
     │
     │  POST /command/competition/recordScore  cmdId=cmd-91ac4e
     ├─────────────────────────────────────────────────────────►
     │                                                          │
     │                                              [1] Identity gate
     │                                              [2] Schema validate
     │                                              [3] Idempotency check
     │                                              [4] Domain handler
     │                                                    │
     │                                                    │  builds event
     │                                                    │  versions++   ┌──────────────┐
     │                                                    ├──────────────►│ Command Log  │
     │                                                    │               │   append     │
     │                                                    │               └──────┬───────┘
     │                                                    │                      │ fsync
     │                                                    │                      │
     │                                                    │               ┌──────┴───────┐
     │                                                    │               │  Event Log   │
     │                                                    │  group commit │   append     │
     │                                                    ├──────────────►│              │
     │                                                    │               └──────┬───────┘
     │                                                    │                      │ fsync
     │                                                    │                      ▼
     │                                                    │               SEALED — chain advances
     │                                                    │                      │
     │                                                    │                      │
     │                                                    │ ◄────────────────────┘
     │                                                    │
     │                                              [5] Projection bus
     │                                                    │
     │                                                    │ dispatches invalidation
     │                                                    │      ┌────────────────────┐
     │                                                    │      │ proj.leaderboard.* │
     │                                                    │      └────────────────────┘
     │                                                    │
     │                                              [6] SSE fanout
     │                                                    │
     │                                                    │      ┌──────────────┐
     │                                                    │      │  Wire Log    │ per-client frames
     │                                                    │      └──────┬───────┘
     │                                                    │             │
     │ ◄──────────────────────────────────────────────────┴─────────────┘
     │  SSE delta { versions, slices }
     │
     │  HTTP 200 { ok:true, version:4422, elapsedMs:14 }
     │ ◄────────────────────────────────────────────────────────
     │
```

---

## 25. Audit system diagram

```
                            EVENT LOG (sealed segments)
                                       │
                  ┌────────────────────┼─────────────────────┐
                  │                    │                     │
                  ▼                    ▼                     ▼
       ┌────────────────┐    ┌─────────────────┐    ┌────────────────┐
       │  Index Writer  │    │ Chain Verifier  │    │ Search Indexer │
       │  (auditId,     │    │ (periodic       │    │ (full-text on  │
       │   version, ts) │    │  sanity check)  │    │  sealed segs)  │
       └───────┬────────┘    └─────────────────┘    └────────┬───────┘
               │                                              │
               └───────────────────┬──────────────────────────┘
                                   ▼
                          ┌─────────────────┐
                          │  Audit Viewer   │
                          │                 │
                          │  - Filters      │
                          │  - Search       │
                          │  - Trace tree   │
                          │  - Diff panel   │
                          │  - State-at-T   │
                          │  - Export (PDF) │
                          └────────┬────────┘
                                   │
                ┌──────────────────┼──────────────────┐
                ▼                  ▼                  ▼
        Operator (read-only)  Owner (rollback)   Dispute exporter
```

---

## 26. Snapshot strategy diagram

```
   events ──►   ┌──── micro (every 200 events) ────►  memory ring (50)
                │
                ├──── periodic (every 5 min) ──────►  disk: snapshots/periodic/...
                │                                     retention 30d / hourly→daily
                │
                ├──── milestone (heat/event) ──────►  disk: snapshots/milestones/...
                │                                     retention: comp lifetime
                │
                └──── final (comp closed) ─────────►  archive: s3://archive/...
                                                      retention: forever


Pruning lifecycle:

   periodic:   24h all → 7d hourly → 30d daily → drop
   milestone:  kept until competition is finalised, then folded into final archive
   final:      forever; quarterly integrity check
```

---

## 27. Replay timeline wireframe

```
╔════════════════════════════════════════════════════════════════════════════════╗
║ AUDIT · IRON CHALLENGE 2025                       [forensic mode · read-only] ║
╠════════════════════════════════════════════════════════════════════════════════╣
║                                                                                ║
║ ┌─ TIMELINE ─────────────────────────────────────────────────────────────────┐ ║
║ │                                                                            │ ║
║ │ EV1     EV2       EV3        EV4 (active)         EV5         EV6          │ ║
║ │ ─◆──────◆─────────◆──────────●●────────────────────◆───────────◆──         │ ║
║ │  H1 H2  H1 H2 H3  H1 H2 H3   H1●H2                                          │ ║
║ │                              ▲                                              │ ║
║ │                              playhead: 2026-05-16 14:33:22.918              │ ║
║ │                                                                            │ ║
║ │  ◀◀  ◀  [ PAUSE ]  ▶  ▶▶    0.25× 0.5× [1×] 2× 4× max     step ►  jump ▶  │ ║
║ │                                                                            │ ║
║ │  [tag: dispute heat 2]  [tag: official restart]  [bookmark]                 │ ║
║ └────────────────────────────────────────────────────────────────────────────┘ ║
║                                                                                ║
║ ┌─ STATE AT PLAYHEAD ─────────────────────┐ ┌─ EVENT INSPECTOR ──────────────┐ ║
║ │ Plane 1 (competition)                   │ │ Competition.ScoreRecorded      │ ║
║ │   active event: 4 Atlas Stones          │ │ v.4422   auditId 18923         │ ║
║ │   active heat: 2 / U90                  │ │                                 │ ║
║ │   sealed heats: E1.H1..3, E2.H1..3,     │ │ athlete: BJORNSSON, T. (17)    │ ║
║ │                 E3.H1..3, E4.H1         │ │ event:   Atlas Stones (4)      │ ║
║ │                                         │ │ primary: 48.5                  │ ║
║ │ Plane 2 (broadcast)                     │ │                                │ ║
║ │   PGM scene: scene.heat                 │ │ principal: op-tony  s-91ac      │ ║
║ │   PRE scene: scene.champion (staged)    │ │ cmdId: cmd-91ac4e               │ ║
║ │   layers: SCOREBUG, LOWERTHIRD, REPS on │ │ traceId: tr-7b2af1              │ ║
║ │   leaderboard: pinned U80               │ │                                 │ ║
║ │                                         │ │ [view trace tree (4 events)]   │ ║
║ │ Wire log at this moment:                │ │ [retract...] [restore here]    │ ║
║ │   14 clients connected; delta sent: 14  │ │                                │ ║
║ └─────────────────────────────────────────┘ └────────────────────────────────┘ ║
║                                                                                ║
║ ┌─ OVERLAY PREVIEW (rendered from state-at-T) ───────────────────────────────┐ ║
║ │                                                                            │ ║
║ │       [renders the OBS overlay HTML driven by PGM state at playhead]       │ ║
║ │                                                                            │ ║
║ │   [view in wire-mode (replay actual SSE bytes to headless overlay)]        │ ║
║ └────────────────────────────────────────────────────────────────────────────┘ ║
║                                                                                ║
║ filters: stream=any  principal=any  trace=any  outcome=any   search [____]   ║
╚════════════════════════════════════════════════════════════════════════════════╝
```

---

## 28. Operator recovery workflows

### 28.1 Disputed score (most common forensic flow)

```
Coach reports: "BJORNSSON's Atlas Stones score was 50.0, not 48.5"
   │
   ▼
[1] Open audit viewer
[2] Filter: athlete=BJORNSSON, event="Atlas Stones"
[3] Surface shows: ScoreRecorded v.4422 — primary 48.5 — op-tony — 14:33:22.918
[4] Click trace tree → see causing LaneSubmitted from j-lane2 at 14:33:22.701
[5] Click "render overlay at T" → preview shows 48.5 was on-air
[6] Click "view wire-mode" (if available) → confirm 48.5 was actually sent
[7] Decision:
      a. Confirmed correct → export PDF evidence, share with coach
      b. Confirmed wrong → exit forensic mode → issue Competition.retractScore
         with reason "judge call error per video review" → new event appears
[8] If retracted: append corrected Competition.recordScore with new value
```

### 28.2 Mid-event process crash

```
Director notices overlays frozen
   │
   ▼
[1] systemd auto-restarts within 2s (no operator action)
[2] Recover-mode replay completes within RTO 60s
[3] First SSE frame to clients: full resync with new restartToken
[4] Director console flashes briefly (reconnect indicator) then resumes
[5] Director verifies PGM state matches expectation (it does — replay is deterministic)
[6] Audit viewer logs the System.RestartCompleted event automatically
```

### 28.3 Catastrophic host loss

```
[1] PagerDuty pages oncall: host unreachable >60s
[2] Oncall provisions replacement (preprovisioned image: 90s)
[3] Snapshot puller fetches latest periodic from off-host: <30s
[4] Audit-log tail pulled: <30s
[5] Recover-mode replay: <60s
[6] DNS update or static IP failover: <60s
[7] Clients reconnect; restartToken triggers full resync
[8] Total elapsed: ~5 minutes (within DR RTO)
[9] Operator runs verification: state hash matches expected for latest event
```

### 28.4 Chain break detection

```
[1] Chain verifier reports break at auditId N
[2] System refuses new commands (writer halts)
[3] Oncall notified
[4] Off-host chain head consulted: provides expected hash at N-1
[5] Two outcomes:
      a. Disk corruption → restore segment from off-host backup → verify
      b. Tamper → security incident → forensic image of disk → escalate
[6] System resumes only after chain re-verified end-to-end
```

---

## 29. Disaster recovery flow

```
                  ┌────────────────────────────────────────┐
                  │      DETECTION: oncall alarm           │
                  └─────────────────┬──────────────────────┘
                                    │
                  ┌─────────────────▼──────────────────────┐
                  │  ASSESSMENT (1–2 min)                  │
                  │  - Process death only?                 │
                  │  - Disk loss?                          │
                  │  - Host loss?                          │
                  └────┬─────────────┬───────────┬─────────┘
                       │             │           │
              process death     disk loss    host loss
                       │             │           │
                       ▼             ▼           ▼
              ┌─────────────┐ ┌─────────────┐ ┌──────────────────┐
              │ Just restart│ │ Restore from│ │ Provision host   │
              │ (systemd)   │ │ snapshot +  │ │ from image       │
              │             │ │ replay tail │ │                  │
              │ RTO: ~5s    │ │ RTO: <60s   │ │ Pull from off-   │
              │             │ │             │ │  host archive    │
              │             │ │             │ │ RTO: <5min       │
              └──────┬──────┘ └──────┬──────┘ └────────┬─────────┘
                     │               │                 │
                     └───────────────┼─────────────────┘
                                     ▼
                         ┌────────────────────────┐
                         │ Recover-mode replay    │
                         │ Mint new restartToken  │
                         │ Open command + SSE     │
                         └────────────┬───────────┘
                                      ▼
                         ┌────────────────────────┐
                         │ Clients full-resync    │
                         │ Determinism CI check   │
                         └────────────┬───────────┘
                                      ▼
                         ┌────────────────────────┐
                         │ Operator verification: │
                         │   state hash matches?  │
                         │   PGM matches expected?│
                         │   Log incident to      │
                         │   audit log.           │
                         └────────────────────────┘
```

---

## 30. Historical reconstruction flow

The signature use-case: **reconstruct the exact state at any past moment**.

```
Operator selects time T in audit viewer
   │
   ▼
[1] Locate nearest snapshot S where S.createdAt ≤ T
       periodic? milestone? final? — pick the closest match
   │
   ▼
[2] Load S into a transient forensic working set (memory only)
       - hydrate Plane 1 (comp.db.snapshot via :memory: attach)
       - hydrate Plane 2 (broadcast.* JSON)
       - hydrate projection cache (from materialised or recompute)
   │
   ▼
[3] Read events from segments at offset S.auditCursor + 1
       - apply each forward
       - stop when serverEpochMs ≥ T (or auditId equals target)
       - track replay progress (UI shows ETA)
   │
   ▼
[4] Working set is now state-at-T
   │
   ▼
[5] UI exposes:
       - Plane 1 read views (athletes, events, raw_results, leaderboard)
       - Plane 2 read views (PGM scene, layer visibility, queue)
       - Projection read views (lanes, leaderboard.*, eventState)
       - Overlay preview (render OBS overlay from this state)
   │
   ▼
[6] If wire log available for T:
       - per-client frames listed
       - "wire-mode" button: play frames into headless overlay
   │
   ▼
[7] Operator may "tag" this moment for later (bookmark with note)

   At no point are the underlying segments modified.
   At no point is live engine state affected.
```

---

## 31. Event retention policy summary

```
┌──────────────────┬──────────┬──────────┬──────────────┬──────────────┬─────────┐
│ Data             │ Memory   │ Local    │ Off-host     │ Object       │ Forever │
│                  │ ring     │ disk     │ rsync mirror │ archive (S3) │         │
├──────────────────┼──────────┼──────────┼──────────────┼──────────────┼─────────┤
│ Event log        │ 5 min    │ 90 d     │ 30 d         │ per-comp     │ if final│
│ Command log      │ 5 min    │ 30 d     │ 7 d          │ 1 y          │ no      │
│ Wire log         │ 5 min    │ 24 h     │ —            │ —            │ no      │
│ Micro snapshot   │ ring 50  │ —        │ —            │ —            │ no      │
│ Periodic snap.   │ —        │ 30 d     │ 24 h         │ —            │ no      │
│ Milestone snap.  │ —        │ comp     │ comp         │ comp tarball │ no      │
│ Final snap.      │ —        │ 30 d     │ 30 d         │ indefinite   │ yes     │
│ Chain head       │ —        │ live     │ live         │ —            │ no      │
│ Final PDF        │ —        │ 30 d     │ 30 d         │ indefinite   │ yes     │
└──────────────────┴──────────┴──────────┴──────────────┴──────────────┴─────────┘
```

---

## 32. Phased implementation roadmap

Phase B builds the audit/replay/recovery surface incrementally. Each step is shippable and reversible.

### B.0 — Append-only event log
**Goal:** Every state-changing operation appends to `audit/events-*.jsonl` with versioning.
**Scope:** Segment writer, in-memory tail, basic index. No chain yet.
**Exit:** Restart and recover-replay reaches identical state in a mock event.

### B.1 — Hash chain + integrity
**Goal:** Tamper evidence.
**Scope:** `prevHash`/`selfHash` per event, segment seal sidecar, chain head file, off-host chain head replication, chain verifier.
**Exit:** Manual tamper of a sealed segment is detected within 1 second on next verify pass.

### B.2 — Command log + outcomes
**Goal:** Capture every command (success and rejection).
**Scope:** `commands/` segments, idempotency window, outcome embedded.
**Exit:** Audit query "what did op-tony attempt?" returns commands + outcomes.

### B.3 — Snapshots
**Goal:** Periodic + milestone snapshots.
**Scope:** SQLite `VACUUM INTO`, atomic JSON capture, manifest, pruning. Includes off-host rsync.
**Exit:** Process restart picks latest valid snapshot and replays tail in <60s on a real-event-sized dataset.

### B.4 — Recover-mode replay
**Goal:** Crash-only recovery is deterministic and fast.
**Scope:** Recover-mode engine implemented (Stages 0–8 of §20).
**Exit:** 10× restart drill in staging produces identical final state hash. CI replay test running daily.

### B.5 — Audit viewer (forensic + trace tree)
**Goal:** Operator surface for read-only investigation.
**Scope:** Timeline scrubber, filters, trace tree, state-at-T pane.
**Exit:** Operator answers all 8 §16.1 queries in <500ms each on a real-event-sized log.

### B.6 — Diff + overlay preview
**Goal:** What-changed and what-was-on-air panes.
**Scope:** Diff mode, sandboxed overlay-render iframe.
**Exit:** Owner reviews a heat in 2 minutes and produces a dispute-ready evidence pack.

### B.7 — Wire log
**Goal:** Capture outbound SSE frames.
**Scope:** Per-client frame ring + 24h disk, basic wire-log viewer.
**Exit:** Reconstruct frames for any client within retention window.

### B.8 — Cold archive
**Goal:** Long-term storage and retrieval.
**Scope:** Tar + zstd + AES-256, S3 upload, restore-to-sandbox path.
**Exit:** Restore drill: any retained competition can be re-read end-to-end in <5min.

### B.9 — Wire-mode replay
**Goal:** Pixel-level forensic reproduction.
**Scope:** Headless overlay browser, frame player, screenshot capture.
**Exit:** Disputed moment reproduced visually and signed-off.

### B.10 — Final snapshot + signing
**Goal:** Competition close produces a permanent, signed final.
**Scope:** Final snapshot generation, signed PDF leaderboard, public verification surface.
**Exit:** Final snapshot hash matches across off-host copies; PDF verifies against event log.

### B.11 — Determinism CI + drills
**Goal:** Continuous safety nets.
**Scope:** Daily replay determinism test, monthly restart drill, quarterly DR drill, annual archive drill.
**Exit:** Drill calendar published; all four exercises green for 3 consecutive cycles.

### Dependency graph

```
B.0 ─► B.1 ─► B.2 ─► B.3 ─► B.4
                              │
                              ▼
                            B.5 ─► B.6
                              │
                              ▼
                            B.7 ─► B.9
                              │
                              ▼
                            B.8 ─► B.10
                              │
                              ▼
                            B.11
```

---

## 33. Open decisions (deferred)

| Decision                                                            | Affects | Default                                                |
|---------------------------------------------------------------------|---------|---------------------------------------------------------|
| Event-log format: NDJSON vs Protobuf vs Avro                        | B.0     | NDJSON — readability + tooling                          |
| Hash function: SHA-256 vs BLAKE3                                    | B.1     | SHA-256 — boring, supported everywhere                  |
| Object storage: S3 vs Backblaze B2 vs self-hosted MinIO             | B.8     | Backblaze B2 — cheap egress; revisit                    |
| Wire-log persistence: always vs only-when-flagged                   | B.7     | Always within 24h ring; "flagged" extends to 30d        |
| Sandbox process: same machine vs ephemeral container                | B.6     | Same machine v1; container later                        |
| Wire-mode browser: Playwright vs custom Chromium harness            | B.9     | Playwright — battle-tested                              |
| Off-host backup target: second VPS vs cloud storage                 | B.3     | Cloud storage — cheaper and geographically distinct     |
| Snapshot encryption key management: env vs KMS                      | B.8     | KMS once we have one; env for v1                        |
| PDF signing: self-signed CA vs trusted CA                           | B.10    | Self-signed CA with publicly verifiable transcript      |
| Audit search backend: Lucene-like vs SQL FTS                        | B.5     | SQL FTS (SQLite has FTS5); switch when scale demands    |

---

## 34. Glossary

- **Append-only** — bytes can only be added, never modified or removed.
- **Audit cursor** — the last `auditId` included in a snapshot.
- **Branch protection** — replay engines cannot write to the live log; sandbox processes get a copy.
- **Chain (hash chain)** — every event includes the hash of its predecessor; any tampering breaks downstream hashes.
- **Cold storage** — long-term, infrequent-access object storage (S3-class).
- **Compensating event** — a forward-only event that undoes a prior one.
- **Determinism** — given same inputs, same outputs; mandatory for replay correctness.
- **fsync** — OS call that forces buffered writes to durable media; the durability anchor.
- **Group commit** — batching multiple in-flight events into one fsync.
- **Hot/Warm/Cold/Forever** — retention tiers, fastest to slowest access.
- **Idempotency window** — duration during which a duplicate `cmdId` returns cached outcome.
- **Milestone snapshot** — snapshot pinned to a domain milestone (heat/event advance, comp close).
- **Monotonic clock** — never-decreasing process-local clock; immune to NTP slewing.
- **Periodic snapshot** — time-triggered snapshot for warm recovery.
- **RPO (Recovery Point Objective)** — max acceptable data loss window.
- **RTO (Recovery Time Objective)** — max acceptable restoration time.
- **Restart token** — process-start epoch; resets client version caches.
- **Sealed** — fsynced + chain-anchored; cannot be silently lost.
- **Segment** — one file in an append-only log; sealed when rolled over.
- **Snapshot** — internally consistent capture used as a replay anchor.
- **Stream** — versioned event sequence (competition / broadcast / judging / identity).
- **Trace** — set of all events caused by a single user intent; share `traceId`.
- **Wire log** — record of actual SSE frames sent to each client.

---

*End of Phase B. The next document — when implementation begins — translates B.0 through B.11 into ordered work items with file paths and acceptance tests. Phase C, if and when we proceed there, addresses identity, multi-tenancy, billing, and the public/spectator surface.*
