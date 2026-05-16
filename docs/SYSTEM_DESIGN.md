# StrongPass — System Design
**Phase:** Architecture & Systems Design (pre-implementation)
**Date:** 2026-05-16
**Status:** Design artefact only. No code in this document.
**Audience:** Future Claude sessions, the operator, eventual contributors.
**Scope:** Replaces the loose collection of audit docs with one coherent target architecture for the live broadcast control plane and its near-term evolution (organiser tenancy, members, billing, content).

---

## 0. Reading order

Sections 1–10 are **definitions**: what each subsystem is, where its boundary sits, and what it owns.
Sections 11–14 are **artefacts**: wireframes, UI language, diagrams, and a phased roadmap.

You can read 1–10 linearly; the artefacts in 11–14 reference them by name.

---

# PART A — DEFINITIONS

## 1. Domain boundaries

StrongPass has five durable domains today and three that the roadmap brings online. Each is a *bounded context* in the DDD sense — internal models do not leak across the boundary; contracts between domains are explicit (events, projections, or HTTP).

### 1.1 Active domains

| # | Domain            | Owns (canonical state)                                                             | Authoritative store      | Speaks to                              |
|---|-------------------|------------------------------------------------------------------------------------|---------------------------|----------------------------------------|
| 1 | **Competition**   | Events, heats, athletes, lanes, raw_results, category order, comp status, scoring rules | `comp.db` (SQLite)        | Broadcast (publishes projections), Judging (accepts scores) |
| 2 | **Judging**       | Per-lane judge state: timer, light verdict, rep count, primary/secondary inputs    | In-memory + `state.json`  | Competition (submits scores), Broadcast (publishes lane state) |
| 3 | **Broadcast**     | Scene state, overlay visibility, director overrides, manual text fields, scoreboard pin | `state.json`              | Overlays (SSE), Competition (subscribes to projections) |
| 4 | **Overlay runtime** | None — pure read-side. Renders DOM from the broadcast contract.                  | (stateless)               | Broadcast (consumes SSE)               |
| 5 | **Identity/Access** | Beta token, comp operator session, soon: organiser/member accounts              | session cookies + env vars | All others (gating)                    |

### 1.2 Roadmap domains

| # | Domain        | Owns                                                  | When                            |
|---|---------------|-------------------------------------------------------|----------------------------------|
| 6 | **Tenancy**   | Organiser org, members of an org, competition ownership | P4 (organiser/member architecture) |
| 7 | **Billing**   | Stripe customer, subscription, entitlement to features | P5                                |
| 8 | **Content**   | VOD assets, livestream embed config, public profile pages | P8                                |

### 1.3 Boundary rules (invariants)

1. **Competition is the only writer of scoring truth.** Judging *submits* values; Competition *decides* whether they are accepted (validation, current-heat check, type rules). Overlays must never derive a score from anything other than a Competition-produced projection.
2. **Broadcast never writes back into Competition.** A director cannot change a score from the control console — that is intentional. The director may *override what is shown*; they cannot rewrite history.
3. **Overlay runtime has no domain logic.** It is a renderer. All semantics (which athlete is current, what unit to display, who is champion) live in upstream projections.
4. **`state.json` is a *contract surface*, not internal state.** Anything in it is part of the public broadcast API. Internal state (caches, locks, threads) never appears there.
5. **Identity gates routes, not domains.** Authorisation lives at the HTTP edge; domain code assumes a valid principal.

> The current monolith implements all five active domains in `server.py`. The boundaries above are logical, not physical. The roadmap (§14) extracts them into modules without splitting into services.

---

## 2. Command / event architecture

The system today is *imperative*: clients POST `/update` with arbitrary keys and the server merges them into `state.json`. That is acceptable for a single operator but breaks down as soon as we have:
- Two directors trying to set the same overlay.
- An audit trail requirement (post-event dispute resolution).
- Organisers running concurrent competitions.

The target is **command/event separation** — CQRS-lite — without adopting a heavy framework.

### 2.1 Command surface

A **command** is a named, validated, intent-bearing message addressed to a specific domain. It either succeeds with a defined outcome or fails with a typed error. Commands are *not* free-form state patches.

Canonical commands by domain:

**Competition**
- `RecordScore(athleteId, eventId, primary, secondary?, judgeId, source)`
- `AdvanceHeat()` / `AdvanceEvent()` / `SetActiveCategory(cat)`
- `ConfigureEvent(eventNumber, name, scoringType, presets…)`
- `DeclareDNF(athleteId, eventId)` / `DeclareDNS(...)`
- `ReorderCategories(order[])`

**Judging**
- `StartTimer(lane, durationSecs)` / `StopTimer(lane)` / `ResetTimer(lane)`
- `SetLight(lane, verdict: good|nogood|none)`
- `SetRepCount(lane, n)`
- `SetPrimaryValue(lane, v)` / `SetSecondaryValue(lane, v)`
- `SubmitLane(lane)` — promotes lane judge state into a `RecordScore` command on Competition.

**Broadcast**
- `ShowOverlay(name)` / `HideOverlay(name)` / `ToggleOverlay(name)`
- `PinLeaderboard(category|"auto")`
- `SetManualLowerThird(line1, line2, score, unit)`
- `SetH2H(left, right)`
- `SetChampion({name, detail, score, label, eventTag})` / `ClearChampion()`
- `LoadScene(sceneId)` / `RunSceneStep(stepId)`

**Identity**
- `BetaLogin(token)` / `CompLogin(password)` / `Logout(scope)`

All commands carry: `commandId` (idempotency), `issuedAt`, `principal`, and `tenantId` (future).

### 2.2 Event surface

An **event** is a fact: something happened. Events are *emitted* by domain code after a command succeeds and are the only way state changes propagate outward.

Canonical events:

**Competition**: `ScoreRecorded`, `ScoreRetracted`, `HeatAdvanced`, `EventAdvanced`, `EventCompleted`, `CategoryActivated`, `ChampionDetermined`
**Judging**: `TimerStarted`, `TimerExpired`, `LightSet`, `LaneSubmitted`, `LaneReset`
**Broadcast**: `OverlayShown`, `OverlayHidden`, `LeaderboardPinned`, `ChampionShown`, `SceneLoaded`, `ManualFieldsSet`
**Identity**: `SessionEstablished`, `SessionEnded`

### 2.3 Why CQRS-lite, not full event sourcing

Full event sourcing (rebuild state from a log) is overkill for one competition at a time. But splitting command intent from state mutation gives us four things that we want now:

1. **Audit log.** Every score change is a named event with a principal — disputes are answerable.
2. **Multi-operator safety.** Two directors clicking "show champion" emit two commands; the second is rejected (or no-ops) because the first already produced `ChampionShown`.
3. **Replayable test fixtures.** A recorded session can be replayed without OBS in the loop.
4. **Future bus.** When we go multi-tenant, the event stream is what publishes to per-org SSE channels.

### 2.4 Transport mapping

| Today                         | Target                                          |
|-------------------------------|--------------------------------------------------|
| `POST /update {anyKey: anyValue}` | `POST /command/{domain}/{name}` with typed body |
| Server merges, fires SSE      | Server validates → runs handler → emits event → updates projection → SSE |
| Free-form `state.json` writes | All writes go through one of N command handlers |
| Implicit ordering             | Each event has a monotonically increasing `version` (the `_sse_version` we already track) |

`/update` stays as a *compatibility shim* during migration; new clients use the typed surface.

---

## 3. Broadcast orchestration

Broadcast is the most operationally complex domain. It is the place where automation meets a human director making live judgement calls. The architecture must let both coexist.

### 3.1 The AUTO/MANUAL principle (already in the codebase, formalised here)

Every director-controllable field has two states:

- **AUTO** — the field is computed by Competition projections on every `sync_comp_to_broadcast`. The director sees the value but does not own it.
- **MANUAL** — the director has explicitly set the field. Competition syncs *must not* overwrite it. Persists until the director resets to AUTO.

This applies to:
- Leaderboard category (`lbCategoryOverride` already implements this)
- Lower-third lane contents (auto-follow heat vs `manual_lowerthird`)
- Champion display
- Lineup index
- H2H slots
- Manual scorebug text

The control console shows the AUTO/MANUAL badge per field; the underlying state has explicit boolean overrides per field.

### 3.2 Scenes (formalised in §7)

The director does not toggle 10 overlays individually for routine moments — they pick a **scene** and the orchestrator handles the visibility/timing/transitions.

### 3.3 Director console responsibilities

The control console is a *director cockpit*. Its job:

1. **Show the truth.** Real-time mirror of `state.json` via SSE (≤200ms latency). No 5-second polling.
2. **Issue commands.** Buttons → typed commands → server. Optimistic UI with rollback on reject.
3. **Surface conflicts.** If Competition advanced the heat while the director had a manual lower-third up, surface that — don't silently overwrite.
4. **Run scenes.** One click loads a scene; the orchestrator emits the underlying overlay commands in sequence.
5. **Lock destructive ops.** "Clear champion mid-broadcast" requires a second click. "Reset all overlays" requires a typed confirmation.

### 3.4 Director ↔ Scoring ↔ Judging separation

These are three operator roles, often three different humans on a real event:

- **Scoring operator**: drives Competition (advances heats, fixes scores after disputes). Sees `/comp/run`. Does not touch overlays.
- **Lane judge**: drives Judging (timer, lights, reps, submit). Sees `/judge.html` on a phone or tablet.
- **Director**: drives Broadcast. Sees `/control.html` on a desktop next to OBS.

The protocol between them is asynchronous and event-driven. A lane judge submits → Competition records → Broadcast projection updates → director sees the new score in the leaderboard preview and decides whether to show champion now or after the next heat.

---

## 4. Overlay runtime architecture

Overlays are OBS Browser Source HTML pages with three jobs and only three jobs: **connect**, **patch**, **render**. They do no logic, no validation, no input handling.

### 4.1 The overlay contract

Every overlay:
1. Loads `sse-client.js`, opens `/stream?token=...`.
2. Receives a JSON payload per SSE event.
3. Maintains a *local mirror* of relevant slice of `state.json`.
4. Computes a **change fingerprint** for the slice it cares about (the `_lastXxxKey` pattern already in the code).
5. Only mutates the DOM when the fingerprint changes.
6. Never animates unless the data transition justifies it (the `_wasVisible` guard pattern — see Phase 3 fixes for the historical reason).

### 4.2 Required overlay properties

| Property              | Reason                                                                                  |
|-----------------------|------------------------------------------------------------------------------------------|
| **Restart-token aware** | Server restart resets `_sse_version` to 0; clients must reset their counters via `_RESTART_TOKEN`. |
| **OBS-safe**          | No `location.reload()`. No `display:none` flashes during state transition. No layout-shifting unicode. |
| **Idempotent paint**  | Re-rendering the same data must produce the same DOM. No counters, no accumulating animations. |
| **Backoff reconnect** | Exponential backoff on `/stream` disconnect; do not hammer the server during nginx restart. |
| **Token-bearing**     | Beta token via `?token=` query param (the only auth mode usable inside OBS Browser Source). |
| **Zero-dependency**   | Pure JS + CSS. No frameworks — overlay weight is bandwidth at every event. |

### 4.3 Overlay taxonomy

Overlays fall into three classes; the class determines its data source and update rules:

| Class           | Drives off                          | Update cadence                            | Examples                              |
|-----------------|-------------------------------------|--------------------------------------------|---------------------------------------|
| **Live**        | Judging state (`judgeLN`)           | Every SSE tick (~1/s during heats)         | `reps.html`, lower-third lights, `scorebug.html` |
| **Projected**   | Competition projection (`lbAthletes`, `athletes`, `categories`) | Per score/event change | `leaderboard.html`, `champion.html`, `lineup.html` |
| **Manual**      | Director-set fields                 | Only on director action                    | `h2h.html`, `manual_lowerthird.html`  |

The class determines acceptable animation triggers:
- Live overlays: animate on value transition only (rep count change, light verdict change).
- Projected overlays: animate on projection version change.
- Manual overlays: animate on enter/exit only — never on tick.

---

## 5. Projection systems

A **projection** is a read model: a denormalised view of one or more domain stores, shaped for a specific consumer.

### 5.1 Projections that exist (made explicit)

| Projection                  | Source                            | Consumer                              | Refresh trigger              |
|-----------------------------|-----------------------------------|----------------------------------------|------------------------------|
| `broadcast.lanes`           | `heats` + `raw_results` + `judgeLN` | Lower-third, scorebug, reps          | Heat change, score change, judge tick |
| `broadcast.athletes`        | `raw_results` for active category | Scorebug                              | Score change                 |
| `broadcast.lbAthletes`      | `raw_results` for `lbCategory`    | Leaderboard overlay                   | Score change OR director pin |
| `broadcast.categories`      | `athletes` table                  | Lineup, control console category list | Athlete add/remove           |
| `competition.eventState`    | `competition_state` row           | All overlays (eventName/Num/Sub)      | `AdvanceHeat`/`AdvanceEvent` |
| `competition.champion`      | Computed when all heats of event complete | `champion.html`                | `EventCompleted`             |
| `comp_run.heatGrid`         | `heats` + `athletes`              | `/comp/run` admin page                | Heat advance, athlete reassign |
| `public.leaderboard.{cat}`  | `raw_results` per category, with per-event breakdown | `/api/results/<cat>` (public) | Score change |

### 5.2 Projection patterns

- **In-memory caches with explicit dirty bits.** `_results_cache` + `_results_dirty` is the right pattern; codify it as "every projection has an invalidation function called by exactly the events that affect it."
- **Versioned payloads.** Each projection has a `version` integer. Clients store the last version they rendered; server sends only on version change. (`_sse_version` is the global version today; per-projection versions are the target.)
- **Computed lazily, served from cache.** Never recompute on read; recompute on event, serve from cache on demand.
- **Public projections have their own endpoints.** Spectators hit `/api/results/<cat>` and never see internal broadcast state.

### 5.3 The `state.json` problem

`state.json` is currently *the projection store, the command target, and the persistent config*. That overload is the deepest piece of debt in the system.

Target: split into three files / collections:
- `broadcast_state.json` — runtime broadcast (overlay visibility, manual fields, lbCategoryOverride). Mutated by Broadcast commands. Sent over SSE.
- `projections/*.json` (or in-memory) — read models written by Competition events.
- `engine_config.json` — persistent configuration (lane count, category order, comp name). Mutated by `/comp/*` admin only.

SSE payload becomes `{broadcast: {...}, projections: {...}, version: N, restartToken: T}`.

---

## 6. Realtime transport design

The transport carries projection deltas and director commands. Two halves:

### 6.1 Server → client (fanout)

**Today: SSE one-shot full state.** Works, but every tick sends the entire state to every client.

**Target: SSE with versioned, sliced payloads.**

- Each connected client declares which slices it consumes (`?subscribe=lanes,lbAthletes,judgeL1,judgeL2`).
- The server keeps last-sent version per client per slice.
- On `_sse_notify(changedSlices)`, the server sends only changed slices to clients subscribed to them.
- A periodic *full resync* payload (every 60s) covers any client that missed a delta.
- Restart token in every payload (already implemented) — clients reset on mismatch.

This is bandwidth-meaningful at >20 connected clients (livestream chat overlays, public results pages, multiple OBS scenes).

### 6.2 Client → server (commands)

**Today: `POST /update` with free-form JSON.**

**Target: `POST /command/{domain}/{name}`**

- Body is a typed command (validated server-side against a schema dict).
- Response: `{ok: true, version: N, eventsEmitted: [...]}` on success, `{ok: false, error: {code, message}}` on failure.
- Idempotency: client supplies `commandId`; server dedupes within a 30s window.

### 6.3 Fallbacks

- **SSE drops**: client reconnects with exponential backoff; on reconnect the first payload is a full resync.
- **Network partition longer than 30s**: client shows a "DISCONNECTED" badge on the director console; overlays freeze last-known state (do not blank).
- **Server restart**: restart-token mismatch → clients reset version counters and treat next payload as authoritative.

### 6.4 Why not WebSocket

We considered it. SSE is one-way and simpler — it survives nginx proxying with `proxy_buffering off` (already in our nginx config). The only realtime client→server need is commands, which fit naturally over HTTP POST. We revisit WebSocket if and only if we need server-pushed prompts to judges (e.g., "next heat starting in 30s, confirm ready").

---

## 7. Scene graph architecture

A **scene** is a composite, named, transitionable state of the broadcast: which overlays are visible, with what content, in what arrangement. Scenes let one click do the work that today is 4–6 toggles.

### 7.1 The scene model

A scene is a function from `(competitionState, directorInputs) → desiredOverlayState`. The orchestrator computes the diff between current overlay state and desired overlay state, then applies the minimum set of commands to reach it.

### 7.2 Standard scenes

| Scene ID           | Purpose                              | Visible overlays                          | Notes                                 |
|--------------------|--------------------------------------|--------------------------------------------|---------------------------------------|
| `scene.blank`      | Camera-only, no overlays             | (none)                                     | Pre-show, intermission                |
| `scene.lineup`     | Category intro before heat 1         | `lineup`                                   | Auto-advances athletes if `lineupAuto` |
| `scene.heat`       | Active heat                          | `lowerthird`, `scorebug`, `reps`, `eventBar` | Default during a heat                 |
| `scene.heat+lb`    | Active heat with leaderboard side    | `lowerthird`, `eventBar`, `leaderboard`    | For long events where lb fits         |
| `scene.result`     | Just after heat — show top 3 stones  | `leaderboard`, `eventBar`                  | Lowerthird off, scorebug off          |
| `scene.champion`   | Event winner reveal                  | `champion`                                 | All else suppressed                   |
| `scene.h2h`        | Head-to-head spotlight               | `h2h`                                      | Manual                                |
| `scene.manual`     | Director-driven nameplate            | `manual_lowerthird`                        | Full director control                 |

### 7.3 Scene transitions

A scene transition runs a *sequence* of operations with explicit timing:

```
scene.heat → scene.champion:
  t+0ms:    hide scorebug, hide reps
  t+200ms:  animate-out lowerthird
  t+700ms:  show champion (with confetti)
  t+8000ms: hide champion
  t+8500ms: show leaderboard (next heat preview)
```

Transitions are first-class — the director picks "Reveal champion" and the orchestrator runs the sequence. Each step is an emitted Broadcast command.

### 7.4 Scene locks

A scene declares what is **suppressed** (force-hidden regardless of director input) for the duration:
- `scene.champion` suppresses scorebug, lower-third, reps (do not interrupt the moment).
- `scene.lineup` suppresses scorebug, reps.
- `scene.blank` suppresses everything.

Suppression is non-destructive: when the scene ends, prior visibility states are restored.

### 7.5 Compatibility with manual overrides

A scene sets defaults; explicit director toggles after the scene loads win. Loading a scene is *one command*; toggling an overlay afterwards is a separate command. The orchestrator does not re-run a scene on every tick.

---

## 8. Operator workflows

Four operator personas. Each has a primary surface, a primary goal, and a list of moments where they hand off.

### 8.1 Lane judge (phone, beside a competition lane)

**Surface:** `/judge.html` (auto-targets their assigned lane via `?lane=N`)
**Goal:** Capture what happened at this lane, second-by-second.

```
Heat starts (master judge calls go)
  → tap START (60s timer begins, light=none)
  → tap REP for each completed rep (haptic confirms, count increments)
  → athlete finishes → tap GOOD or NOGOOD
  → for weight-based events: type weight → tap SUBMIT
  → for weight+reps: type weight, count reps, tap SUBMIT
  → for time events: STOP timer at finish, value auto-fills, SUBMIT
  → submission promotes lane state → Competition.RecordScore
```

Failure modes that the UI must surface clearly:
- Lane not yet assigned for this heat (show "WAITING")
- Score already submitted (show submitted value, lock inputs)
- Connection lost (offline indicator, queue submission for reconnect)

### 8.2 Master judge (tablet, central position)

**Surface:** `/judge-master.html`
**Goal:** Oversight + final verdict authority across all lanes.

```
See all lanes' timers, lights, current values at a glance
  → veto override a lane verdict if needed (with reason captured)
  → confirm heat complete → SubmitHeat (commits all lanes' draft scores)
  → if dispute mid-heat: pause all timers
```

The master judge holds the authoritative "final" word — lane submissions are draft until the master judge confirms the heat.

### 8.3 Scoring operator (laptop, scoring table)

**Surface:** `/comp/run`
**Goal:** Drive the competition through events and heats; resolve disputes.

```
Pre-event: configure events, set lane count, register athletes, generate heats
During event:
  → wait for master judge to submit heat
  → review heat results in /comp/run
  → AdvanceHeat → next heat loads in lower-third
  → when last heat of event complete → AdvanceEvent → champion auto-determined
  → after final event → close competition, export results
Dispute flow:
  → pause comp via Broadcast scene.blank
  → edit raw_results entry (audit-logged)
  → resume
```

### 8.4 Broadcast director (laptop, beside OBS)

**Surface:** `/control.html`
**Goal:** Decide what the audience sees and when.

```
Pre-show:
  → load scene.lineup → cycle through categories
  → load scene.blank → camera B-roll
Each heat:
  → load scene.heat (or scene.heat+lb)
  → during heat: maybe pin leaderboard to a different category to tease the standings
  → heat ends: review judge values, decide:
      - immediate reveal? → scene.champion (if event final heat)
      - show standings? → scene.result
      - hold for replay? → scene.blank (hand to producer)
  → load next heat
H2H moment (script element):
  → pick two athletes → SetH2H → scene.h2h
Final:
  → scene.champion for overall winner
  → fade to blank
```

### 8.5 Handoff matrix

| From → To             | Trigger                      | Mechanism                                |
|-----------------------|------------------------------|-------------------------------------------|
| Lane judge → Master   | Lane SUBMIT                  | Master judge sees lane status flip       |
| Master → Scoring      | Master confirms heat         | `comp_run` shows heat results, "Advance" button enables |
| Scoring → Director    | Heat advanced                | SSE updates director console: new heat in lower-third preview |
| Director → Scoring    | Director wants to pause      | Out-of-band (voice / Slack); director loads scene.blank, scoring operator delays advance |
| Competition → Overlays | Score recorded, heat advanced | Event → projection update → SSE         |

---

## 9. Deployment topology

### 9.1 Current (single-VM monolith)

```
Internet ──► strongpass.live (Hetzner)
              │
              ├── nginx :443 (Let's Encrypt SSL, HTTP→HTTPS redirect, proxy_buffering off for /stream)
              │     │
              │     └─► gunicorn :8080 (1 worker, 8 gthread threads)
              │           │
              │           └─► server.py
              │                 ├── app/ helpers (config, sse, database, utils, beta_auth)
              │                 ├── SQLite WAL (comp.db)
              │                 ├── state.json (atomic write)
              │                 └── templates/ + raw HTML overlays
              │
              └── systemd: strongpass.service (auto-restart on crash, env from /etc/strongpass.env)
```

Operational reality: one competition at a time, ~5–15 concurrent SSE clients, 1–3 operators, no horizontal scaling needed for the *event itself*.

### 9.2 Near-term hardening (no topology change)

- Move env vars to `/etc/strongpass.env`, out of the service file.
- Add `/health` checks (already done) + uptime monitor (UptimeRobot, Better Stack).
- Nightly `comp.db` backup to off-server storage (S3-compatible or rsync).
- Structured logs to `/opt/strongpass/logs/` with rotation (logrotate).
- Pinned `requirements.txt` + `pip-tools` lock file.
- Docker compose for reproducible local + staging — production stays bare-metal until there's a reason to change.

### 9.3 Multi-tenant target (P4+)

```
                Internet
                   │
              ┌────┴────┐
              │  nginx  │  (TLS, per-org subdomain routing: {org}.strongpass.live)
              └────┬────┘
                   │
              ┌────┴────┐
              │ app pool│  (gunicorn workers — stateless except for SSE connections)
              └────┬────┘
                   │
        ┌──────────┼──────────┐
        ▼          ▼          ▼
   ┌─────────┐ ┌────────┐ ┌──────────┐
   │Postgres │ │ Redis  │ │ Object   │
   │(per-org │ │(SSE bus│ │ storage  │
   │ schemas │ │  + cache)│(media,  │
   │ or rows)│ │         │  backups)│
   └─────────┘ └────────┘ └──────────┘
```

Key shifts when we go multi-tenant:
- SQLite → Postgres (concurrent writes from multiple orgs).
- `state.json` → Redis hash per org + a Postgres `broadcast_state` table for persistence.
- SSE fanout via Redis pub/sub — app workers become stateless and we can horizontally scale.
- Object storage for athlete photos, event logos, livestream poster frames.
- CDN (Bunny / Cloudflare) in front of static overlays + public results pages.

This is the *target*. We do not build it until multi-tenant is on the table — premature distribution adds operational load without value.

### 9.4 Environments

| Env       | Purpose                          | URL                             | Data            |
|-----------|----------------------------------|---------------------------------|-----------------|
| **local** | Development                      | localhost:5000                  | Throwaway SQLite |
| **staging** | Pre-prod smoke (post-roadmap)   | staging.strongpass.live         | Cloned prod data, redacted |
| **prod**  | Live competitions                | strongpass.live                 | Real            |

Staging exists to let us run a full broadcast rehearsal against a copy of an upcoming event — not in scope until P3+.

---

## 10. Design system foundations

### 10.1 Visual identity (broadcast-first)

The overlays are the product's most visible surface — they appear on livestream and event displays. Constraints flow from that:

- **Broadcast-safe color.** sRGB only, no values below `#101010` or above `#F2F2F2` for surfaces (broadcast clipping). Pure white reserved for the most important text element on screen.
- **High-contrast type.** Minimum 7:1 contrast ratio for body, 14:1 for primary value. Tested against gradient and busy backgrounds, not just solid black.
- **Type stack:** Display: Barlow Condensed (already in use). Numeric/score: Barlow with `font-feature-settings: "tnum"`. UI body: system stack for admin pages, Barlow for overlays.
- **One motion language.** All overlay enter/exits use a 200ms cubic-bezier(0.2, 0, 0, 1) ease. No bounces, no overshoots — distracts from the athlete.

### 10.2 Color tokens

```
--bg          #0B0B0E   (admin)
--surface     #16161B   (cards, panels)
--surface-2   #1F1F26   (nested)
--border      #2A2A33
--text        #F2F2F2
--text-muted  #9A9AA8
--accent      #F5C842   (StrongPass yellow — competition default category color)
--accent-2    #4ADE80   (AUTO badge — green, success)
--accent-3    #FBBF24   (MANUAL badge — gold, attention)
--danger      #EF4444
--info        #38BDF8

Category palette (defaults, organiser-editable):
U70  #2DD4BF
U80  #FBBF24
U90  #F5C842
U105 #F97316
U120 #EF4444
Open #A855F7
Masters #38BDF8
```

### 10.3 Spacing & layout

Single base unit: `8px`. Spacing scale: 4, 8, 12, 16, 24, 32, 48, 64.
Radius scale: 6, 10, 14, 20 (overlays use 14 and 20 only).
Maximum content width on admin pages: 1280px.

### 10.4 Typographic scale

```
Display (champion):       96 / 120 / clamp(64px, 9vw, 120px)
Headline (overlay title): 48 / 56
Title (admin section):    24 / 32
Body large (overlay row): 20 / 28
Body (admin):             16 / 24
Caption (eyebrow):        12 / 16, letter-spacing: 0.12em, uppercase
Numeric primary:          tnum, weight 700
```

### 10.5 Motion tokens

```
--motion-fast:    150ms
--motion-base:    200ms
--motion-slow:    400ms
--motion-reveal:  700ms   (champion-tier reveal only)
--ease:           cubic-bezier(0.2, 0, 0, 1)
--ease-overshoot: cubic-bezier(0.34, 1.56, 0.64, 1)  (reserved for champion)
```

### 10.6 Overlay rendering rules

1. No `opacity: 0` followed by content swap followed by `opacity: 1` on every tick — that's the animation storm we fixed in Phase 3. Animations fire on transition only.
2. No CSS animations triggered by `innerHTML =` reset. Use class toggles to start animations; never recreate elements that are mid-animation.
3. No `transform: scale(>1.02)` — pushes pixels off-grid and reads as jitter on broadcast.
4. No `filter: blur(...)` (cost) or `backdrop-filter` (Chromium-only in OBS).
5. All overlays must paint correctly with the browser source set to 1920×1080@30fps; do not assume 60fps.

### 10.7 Admin/operator surface principles

- **Density over decoration.** Operators need to see lots of state at once. Tables, tight padding, monospace numerics.
- **Confirmations only when destructive.** "Show champion" does not need a confirm; "Reset scores" does.
- **Status colour, not text.** Live = green dot. Pending = amber dot. Stale = red dot. Operators glance, not read.
- **Touch targets ≥ 44px on judge surfaces.** Lane judges are on phones, often with gloves or wet hands.

---

# PART B — ARTEFACTS

## 11. Wireframes

ASCII wireframes — proportional layout, not pixel-precise. Each frame lists the data it binds to.

### 11.1 Director console — `/control.html`

```
┌──────────────────────────────────────────────────────────────────────────┐
│ STRONGPASS · DIRECTOR                       [● LIVE]   [restart: 31822]  │
│ Iron Challenge 2025                                       v.847          │
├──────────────────────────────────────────────────────────────────────────┤
│ ┌─ SCENE ─────────────────────────────┐  ┌─ STATUS ──────────────────┐  │
│ │ ( ) blank   ( ) lineup  (●) heat    │  │ Event 4 · ATLAS STONES    │  │
│ │ ( ) heat+lb ( ) result  ( ) champ   │  │ Heat 2 of 3 · U90         │  │
│ │ ( ) h2h     ( ) manual              │  │ 3 of 4 lanes submitted    │  │
│ └─────────────────────────────────────┘  └───────────────────────────┘  │
│                                                                          │
│ ┌─ OVERLAYS ──────────────────────────────────────────────────────────┐ │
│ │ ON  scorebug    AUTO    ON  lowerthird  AUTO   off champion         │ │
│ │ ON  leaderbd    MANUAL  ON  eventbar    AUTO   off h2h              │ │
│ │ off lineup              off reps                off manual           │ │
│ │ off lights                                                            │ │
│ └─────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│ ┌─ LEADERBOARD PIN ──────────┐  ┌─ LANES (live) ────────────────────┐  │
│ │ [U70] [U80] [U90●] [U105]  │  │ 1  JOHANSSON  S.  48.5  [GOOD]    │  │
│ │ [Open] [Masters]  [↺ AUTO] │  │ 2  BJORNSSON  T.  --    [---]     │  │
│ │ → currently MANUAL: U80    │  │ 3  HALL       E.  44.0  [GOOD]    │  │
│ └────────────────────────────┘  │ 4  LUND       O.  41.5  [GOOD]    │  │
│                                  └───────────────────────────────────┘  │
│ ┌─ CHAMPION SLOT ─────────────────────────────────────────────────────┐ │
│ │ Name [_________________]  Detail [______]  Score [_______] [PTS▾]   │ │
│ │ Eyebrow [____________]   Comp tag [_______]    [REVEAL] [CLEAR]     │ │
│ └─────────────────────────────────────────────────────────────────────┘ │
│ ┌─ MANUAL LOWER THIRD ───────┐  ┌─ H2H ─────────────────────────────┐  │
│ │ Line1 [___________________]│  │ Left  [_____________] [_______]   │  │
│ │ Line2 [___________________]│  │ Right [_____________] [_______]   │  │
│ │ Score [_____]  [SHOW][HIDE]│  │  [LOAD H2H]  [CLEAR]              │  │
│ └────────────────────────────┘  └───────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
   data: state.json (full) via SSE  · writes via /command/broadcast/*
```

Notes:
- Top status bar: live indicator, restart token, broadcast state version. Director glance-check.
- AUTO/MANUAL badge per overlay row. One-click toggle visibility, one-click reset to AUTO.
- Lane preview is read-only — director cannot edit scores here. By design.

### 11.2 Lane judge — `/judge.html?lane=N`

```
┌────────────────────────┐    ┌────────────────────────┐
│ LANE 2  ·  ATLAS STONES│    │ LANE 2  ·  DEADLIFT    │
│ HEAT 2 / U90           │    │ HEAT 2 / U90           │
│ T. BJORNSSON           │    │ T. BJORNSSON           │
│                        │    │                        │
│   ┌────────────────┐   │    │  Weight (kg)           │
│   │     60.0s      │   │    │  ┌──────────────────┐  │
│   │                │   │    │  │      350         │  │
│   │   ▣ ▣ ▣ ▣ ▣    │   │    │  └──────────────────┘  │
│   │   reps × 5     │   │    │                        │
│   └────────────────┘   │    │  Reps  (tiebreak)      │
│                        │    │  ┌──────────────────┐  │
│   [   START   ]        │    │  │       3          │  │
│   [   REP+    ]        │    │  └──────────────────┘  │
│   [   REP−    ]        │    │                        │
│                        │    │  [   −  ][   REP+   ]  │
│  ┌──────────────────┐  │    │                        │
│  │  GOOD  │ NOGOOD  │  │    │  [    SUBMIT    ]      │
│  └──────────────────┘  │    │                        │
│                        │    │  status: not submitted │
│   [    SUBMIT    ]     │    │                        │
│                        │    │  ● connected           │
│   ● connected          │    │                        │
└────────────────────────┘    └────────────────────────┘
   reps event                    weight+reps event
```

Notes:
- Same shell, different middle panel by event type (reps / weight / time / weight+reps / max distance).
- Connection status always present, top of fold.
- Two large primary actions (START, SUBMIT) glove-friendly. Verdict pair is large and central.
- Submitted state replaces the form with read-only confirmation.

### 11.3 Public results — `/results/<comp>`

```
┌──────────────────────────────────────────────────────────────────────────┐
│  IRON CHALLENGE 2025                                  [U70][U80][U90▾]   │
│  STRONGMAN SERIES · STOCKHOLM                                            │
├──────────────────────────────────────────────────────────────────────────┤
│  RANK  ATHLETE              E1   E2   E3   E4   E5   E6   TOTAL          │
│  ────  ────────────────    ──── ──── ──── ──── ──── ──── ─────           │
│   1    JOHANSSON, S.        12   10   12   11   --   --    45           │
│   2    BJORNSSON, T.        10   12    8   --   --   --    30           │
│   3    HALL, E.              8    8   10   12   --   --    38           │
│   4    LUND, O.              6    6    6    8   --   --    26           │
│   …                                                                       │
├──────────────────────────────────────────────────────────────────────────┤
│  Live · last updated 3s ago · Round 4 of 6                               │
└──────────────────────────────────────────────────────────────────────────┘
   mobile: sticky first column, horizontal scroll on events
```

### 11.4 Organiser dashboard (future, P4+) — `/org/<slug>`

```
┌──────────────────────────────────────────────────────────────────────────┐
│ STRONGPASS · Nordic Strongman Federation       [→ Run live]  [⚙ settings]│
├──────────────────────────────────────────────────────────────────────────┤
│ COMPETITIONS                                              [+ New event]  │
│ ┌────────────────────────────────────────────────────────────────────┐  │
│ │ ● LIVE   Iron Challenge 2025          Today · Stockholm    [Open]  │  │
│ │   Upcoming Nordic Open                Aug 12 · Oslo        [Edit]  │  │
│ │   Past    Spring Classic              Apr 03               [View]  │  │
│ └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│ TEAM                                            BILLING                  │
│ ┌────────────────────────┐                     ┌─────────────────────┐  │
│ │ A. Farrell    OWNER    │                     │ Plan: Pro           │  │
│ │ M. Lind       JUDGE    │                     │ Next: Jun 1, €49    │  │
│ │ + invite               │                     │ [Manage]            │  │
│ └────────────────────────┘                     └─────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

### 11.5 Champion overlay (visual zones, not chrome)

```
┌──────────────────────────────────────────────────────────────────────────┐
│                                                                          │
│                                                                          │
│                  ATLAS STONES                                            │
│                  EVENT 4 OF 6                                            │
│                                                                          │
│                  THOR BJORNSSON                                          │
│                  ━━━━━━━━━━━━━━                                          │
│                  48.5 PTS                                                │
│                                                                          │
│                  U90 CATEGORY                                            │
│                                                                          │
│                                                                          │
│                                              ▴ ▴ ▴   particles           │
└──────────────────────────────────────────────────────────────────────────┘
   safe zones: name centered, eyebrow + comp eyebrow above + below
   particle origin: bottom-right, drift up-left over 8s
```

---

## 12. UI concepts (design language)

The product has two visual halves that must feel like one brand:

### 12.1 Broadcast graphics language

**Mood:** stadium electricity meets clinical scoreboard. Heavy condensed type. Deep neutral background. One dominant accent (category color), one supporting (yellow). Generous negative space — TV pixels are not free.

**Construction:**
- Layer 1 (background): full-bleed gradient `var(--surface)` → `#000`, opacity tuned for video pass-through.
- Layer 2 (panels): solid `var(--surface)` rectangles with `border-radius: 20px` and `border: 1px solid rgba(255,255,255,0.06)`.
- Layer 3 (content): typographic — names, numbers, eyebrows. No icons in primary slots; icons only for status (light verdict, connection).
- Layer 4 (accent rail): 4px-tall colored bar on the leading edge of every panel, using the category color.

**Lower-third grammar:**
- Eyebrow caption (12px, tracked, uppercase) — what category / heat
- Athlete name (48px, condensed, capitalised) — primary
- Detail/origin (16px, muted)
- Right block: current value (huge, tnum) + verdict light

**Scorebug grammar:**
- Marquee-style horizontal scroll on `#bug-scroll`
- Format: `RANK · NAME · CATEGORY · TOTAL` repeated; gap chips between
- One-line, never wraps

**Leaderboard grammar:**
- 10 rows max
- Row composition: rank pill, name, total
- Pin badge in header when MANUAL: "PINNED · U80"
- Active heat athletes get a subtle accent rail on the row

### 12.2 Admin/operator language

**Mood:** cockpit. Dense, gridded, low chrome, clear state colour. The director should feel like an air-traffic controller, not a CMS user.

**Patterns:**
- **Status pill:** colored dot + label. Live (green), AUTO (green), MANUAL (gold), DISCONNECTED (red). Used everywhere status is meaningful.
- **Section header:** uppercase, tracked, muted color. No icons. Acts as a quiet divider.
- **Cards:** `--surface` panels with `--border` stroke. No drop shadow (cheapens the broadcast feel).
- **Forms:** label *above* input, never beside. Inputs are tall (40px min) and full-width unless inline with another control.
- **Buttons:**
  - Primary: filled `--accent`, dark text. One per surface, reserved for the moment-of-truth action.
  - Secondary: outlined, neutral. The everyday verbs.
  - Danger: filled `--danger`. Always preceded by a confirmation.
- **Tables:** zebra rows OFF (chrome-y); tight 12px row padding; column headers in muted caption type.

### 12.3 Public / spectator surface language

A third sub-language for `/results/<comp>` and the future spectator app. Lighter than the operator surface, no cockpit density:

- Bigger type, more spacing.
- Category tabs at the top, not a dropdown (mobile-friendly).
- "Live" badge with pulse animation when scores updating.
- Athlete rows tappable → reveals per-event breakdown.

### 12.4 Iconography

Minimal. Use only:
- ● live / connected
- ◐ pending / partial
- ○ idle / offline
- ⚠ warning
- ✓ confirmed
- ✗ denied
- ↺ reset to auto

No emoji. No icon font. Inline SVG only where pure unicode is ambiguous.

---

## 13. Architecture diagrams

### 13.1 Top-level data flow (target state)

```
              ┌────────────────────────────────────────────────────┐
              │                  IDENTITY                          │
              │  Beta token · Comp session · (future) org session  │
              └─────────────────────┬──────────────────────────────┘
                                    │ gate
        ┌───────────┐  command  ┌───┴─────────┐  command  ┌────────────┐
        │  Judging  │──────────►│ Competition │◄──────────│  Broadcast │
        │  (lanes)  │           │  (engine)   │           │ (director) │
        └────┬──────┘           └─────┬───────┘           └─────┬──────┘
             │                        │                         │
             │ events                 │ events                  │ events
             │  TimerStarted          │  ScoreRecorded          │  OverlayShown
             │  LaneSubmitted         │  HeatAdvanced           │  SceneLoaded
             ▼                        ▼                         ▼
       ┌──────────────────────────────────────────────────────────────┐
       │                       EVENT BUS                              │
       │             (in-process today, Redis pub/sub later)          │
       └─────────┬────────────────────────┬────────────────┬──────────┘
                 │                        │                │
                 ▼                        ▼                ▼
        ┌────────────────┐    ┌────────────────────┐  ┌───────────────┐
        │  PROJECTIONS   │    │     AUDIT LOG      │  │ NOTIFICATIONS │
        │ broadcast.lanes│    │  every event       │  │ (future:      │
        │ broadcast.lbAth│    │  + principal       │  │  org webhook) │
        │ public.results │    │  + timestamp       │  │               │
        └───────┬────────┘    └────────────────────┘  └───────────────┘
                │
                │ SSE versioned slices
                ▼
       ┌─────────────────────────────────────────────────────────────┐
       │                       OVERLAYS                              │
       │ leaderboard · lowerthird · scorebug · champion · reps ·     │
       │ lineup · h2h · manual_lowerthird                            │
       └─────────────────────────────────────────────────────────────┘
```

### 13.2 Scene graph (state machine)

```
                ┌─── scene.blank ◄─────────────────┐
                │         │                         │
                │         ▼                         │
                │   scene.lineup ────►  scene.heat ─┼───► scene.result
                │                        │          │           │
                │                        ▼          │           ▼
                │                  scene.heat+lb    │      scene.champion
                │                                   │           │
                │                                   │           │
                │   scene.h2h ──────────────────────┘           │
                │                                               │
                │   scene.manual ◄──────────────────────────────┘
                │         │
                └─────────┘
```

Director may transition to any node from any node; the orchestrator computes the diff.

### 13.3 Command/event lifecycle (single score)

```
   Lane judge
       │
       │ POST /command/judging/setPrimaryValue {lane:2, v:"48.5"}
       ▼
   Judging.handle ──► validates lane assigned, value parses
       │
       │ emit LaneSubmitted
       ▼
   Event bus ──► Competition.subscribe(LaneSubmitted)
       │
       ▼
   Competition.RecordScore ──► validate against current heat/event/category
       │                       insert raw_results row
       │
       │ emit ScoreRecorded(athleteId, eventId, value, version+1)
       ▼
   Projections.subscribe(ScoreRecorded)
       │
       ├─► invalidate broadcast.lbAthletes (lbCategory)
       ├─► invalidate broadcast.athletes (compCategory)
       ├─► invalidate broadcast.lanes (current heat)
       └─► invalidate public.results.{cat}
       │
       ▼
   SSE fanout ──► all subscribed clients receive {slices: {...}, version: N}
       │
       ▼
   Overlays render diff (hash-checked, animation-guarded)
```

### 13.4 Deployment topology (current vs target)

```
NOW (single-tenant, single host)             TARGET (multi-tenant, P4+)
┌──────────────────┐                          ┌────────────────────────────┐
│  strongpass.live │                          │  *.strongpass.live          │
│  Hetzner VM      │                          │                             │
│                  │                          │  ┌──────┐                   │
│  nginx ─► gunicorn 1w/8t                    │  │nginx │ (router)          │
│        ─► flask (server.py)                 │  └───┬──┘                   │
│  SQLite (WAL) + state.json                  │      │                      │
│  systemd: strongpass.service                │  ┌───┴───┬───────┬────────┐ │
│  TLS: certbot                               │  │ app w1│ app w2│ app w3 │ │
└──────────────────┘                          │  └───┬───┴───┬───┴───┬────┘ │
                                              │      │       │       │      │
                                              │      └───┬───┴───┬───┘      │
                                              │          │       │          │
                                              │      ┌───┴───┐ ┌─┴────┐     │
                                              │      │Postgres││Redis │     │
                                              │      │(orgs,  ││(SSE  │     │
                                              │      │ comps) ││ bus) │     │
                                              │      └────────┘└──────┘     │
                                              │                             │
                                              │      ┌────────────────┐     │
                                              │      │ Object storage │     │
                                              │      │ (media)        │     │
                                              │      └────────────────┘     │
                                              └────────────────────────────┘
```

### 13.5 Identity & access (target)

```
                   request
                      │
                      ▼
              ┌────────────────┐    no    ┌──────────────────┐
              │ beta cookie?   │─────────►│ 401 / login page │
              └───────┬────────┘          └──────────────────┘
                      │ yes
                      ▼
                 ┌─────────────┐
                 │ route class │
                 └──┬──┬──┬────┘
                    │  │  │
        public      │  │  │  protected             admin
        overlays    │  │  │  scoring               /comp/*
        ┌───────────┘  │  └────────────────┐      ┌──────────┐
        ▼              ▼                   ▼      ▼
   served    ┌──────────────┐    ┌──────────────────────┐
             │ comp session?│    │ comp session + role? │
             └───┬──┬───────┘    └────────┬─────────────┘
                 │  │ no                  │
                 │  └──► /comp/login      │ role check
                 │ yes                    ▼
                 ▼                  served / 403
              served
```

Future P6 adds the role-check branch (organiser, judge, scorer, director, athlete).

---

## 14. Phased engineering roadmap

Eight phases. Each phase is independently shippable, rollback-safe, and ends with a verifiable acceptance state. Each phase has a *purpose*, a *scope*, an *acceptance test*, and an *exit criterion* — no phase is "complete" without the exit criterion met.

### Phase A — State Schema Split (foundation)
**Purpose:** Untangle `state.json` so subsequent phases have clean seams.
**Scope:**
- Split `state.json` into `broadcast_state.json` + `engine_config.json`.
- Projections served via dedicated endpoints, not embedded in state.
- Keep a compatibility view: `/state.json` returns the merged shape for old overlays during cutover.
**Acceptance:** All overlays render against new schema. Old `/state.json` URL still works for one release.
**Exit:** Cutover complete; legacy view removed; docs updated.

### Phase B — Typed command surface (introduces CQRS-lite)
**Purpose:** Move from free-form `/update` to typed commands.
**Scope:**
- `/command/{domain}/{name}` endpoints with schema validation.
- `commandId` idempotency window.
- `/update` kept as a shim that translates to commands.
- Director console and judge surfaces migrate to typed commands.
**Acceptance:** A recorded session of typed commands replays cleanly; `/update` shim handles a stale client.
**Exit:** All in-tree clients use the typed surface; `/update` deprecation banner in logs.

### Phase C — Event bus + audit log (introduces events)
**Purpose:** Every state change is a named, logged event with a principal.
**Scope:**
- In-process event bus. Subscribers: projections, audit log, (later) notifications.
- Append-only `audit_log` table: `(id, ts, principal, event, payload, version)`.
- `/comp/audit` view for scoring operator to inspect changes.
**Acceptance:** Manually flipped score appears in audit log; replay of audit log reconstructs final state.
**Exit:** All score-mutating paths emit events; audit covers a full mock event end-to-end.

### Phase D — Versioned sliced SSE
**Purpose:** Bandwidth and CPU win at higher client counts; sets us up for selective per-org streams later.
**Scope:**
- `?subscribe=` query param on `/stream`.
- Per-slice version counters.
- Per-client last-seen map; deltas only.
- 60s heartbeat with full resync.
**Acceptance:** 20 simultaneous overlays produce <50 KB/s aggregate during idle, <500 KB/s during score storm.
**Exit:** All overlays declare subscriptions; full-state stream remains available as fallback.

### Phase E — Scene orchestrator
**Purpose:** First-class scenes replace 4-toggle director workflows.
**Scope:**
- Scene definitions (declarative, in `app/scenes.py` or DB).
- Scene transition runner (sequenced commands with timings).
- Scene suppression rules.
- Director console scene picker UI (wireframe §11.1).
**Acceptance:** Director runs a full event using only scene buttons (no per-overlay toggles).
**Exit:** Standard scenes (§7.2) documented; transitions tested on stage rehearsal.

### Phase F — Mobile-first judge redesign
**Purpose:** Lane judges work on phones in noisy environments. Today's `judge.html` is usable but desktop-flavoured.
**Scope:**
- Phone wireframe (§11.2) implemented.
- Big touch targets, haptic on rep increment.
- Offline-safe: submission queue with retry on reconnect.
- Per-event-type templates: reps, time, weight, weight+reps, max distance, AMRAP.
**Acceptance:** Judge on 4G with intermittent signal submits a heat without data loss.
**Exit:** Two mock heats run with judges on cellular only.

### Phase G — Operational hardening
**Purpose:** Production confidence at the level needed for paid events.
**Scope:**
- Structured logging (JSON, request IDs, principal).
- Pinned `requirements.txt`, lock file, Dockerfile.
- Nightly `comp.db` backup to off-host storage.
- Uptime monitoring + paging.
- CSRF on all mutating endpoints.
- Rate limit on `/command/*` per session.
- Documented runbook for the three top failure modes (DB corrupt, SSE storm, OBS disconnect).
**Acceptance:** A scripted chaos run (kill the process during heat advance, sever the DB write, etc.) recovers without data loss.
**Exit:** Runbook reviewed; backups verified by restore drill.

### Phase H — Multi-tenant foundation
**Purpose:** Make a second organiser possible.
**Scope:**
- `organisations`, `org_members`, `org_competitions` tables.
- Subdomain routing (`{slug}.strongpass.live`).
- Per-org session and role model.
- Org dashboard (§11.4).
- Migration of the existing single-tenant data into the "House" org.
**Acceptance:** Two orgs run simultaneously without cross-contamination of state.
**Exit:** Org A's director cannot see Org B's competitions; CI test enforces tenancy isolation.

### Phase I — Billing
**Purpose:** Monetisation gate for org tier.
**Scope:**
- Stripe Customer per org.
- Stripe Subscription with two tiers (Free + Pro).
- Webhook handler for subscription state transitions.
- Entitlement check at the org-level on premium routes.
**Acceptance:** A test card subscribes Org B, unlocks Pro features, cancels, downgrades. Edge cases: failed payment, refund, plan change mid-cycle.
**Exit:** Org B upgrades and runs a Pro-tier competition; payment lands in Stripe dashboard.

### Phase J — Public/spectator surface
**Purpose:** Audience-facing experience worth sharing.
**Scope:**
- Public competition page at `/c/<slug>`.
- Live results, athlete profiles, event schedule.
- Embeddable widget for organisers' own sites.
- Social meta tags (OG, Twitter) per competition / athlete.
**Acceptance:** Public results page on a phone over 4G loads <2s, updates live, looks broadcast-quality.
**Exit:** First public event link shared and tracked.

### Phase K — Content platform (longest horizon)
**Purpose:** Livestream + VOD for full content experience.
**Scope (decision-pending):**
- Self-hosted (Cloudflare Stream / Mux) vs partner.
- Replay clipping tools (mark in / mark out from director console).
- Athlete-level video tagging.
**Acceptance:** Live event streamed end-to-end with overlay graphics; replay clips generated per event.
**Exit:** First sponsor-grade highlight reel produced.

### Cross-phase: technical debt and security
Three items that run **alongside** Phases A–C and gate progression:

1. **Connection scoping.** Move SQLite connections to request-scoped `g.db`. Blocks Phase H.
2. **`CATEGORY_ORDER` in DB.** Currently a mutable module global; unsafe past 1 worker. Blocks any multi-worker deploy.
3. **Migration framework.** Bare-bones tool to evolve schema additively. Blocks Phase H.

### Phase ordering rationale

```
A ─► B ─► C ──┬──► D
              ├──► E ──► F
              │
              └──► G ──► H ──► I ──► J ──► K
```

A–C unblock everything else; they're the architectural seams. D/E/F are user-visible improvements that can ship in any order once C is done. G is operational maturity, needed before any paying customer (H+). H–K are the product expansion.

### What's *not* on this roadmap (and why)

- **Rewriting the engine in another framework.** No business reason. Flask + SQLite handles the live event load with margin.
- **WebSocket transport.** SSE is sufficient; revisit only if bidirectional push becomes necessary.
- **Mobile apps (iOS / Android).** Web-first; native is post-platform-product-fit.
- **AI scoring / vision.** Out of scope for a system whose value is operator-trusted accuracy.

---

## 15. Open decisions

Items where I'm deliberately *not* picking the answer in this doc. Each needs an explicit call before its phase starts.

| Decision                                       | Affects     | Default suggestion (revisit) |
|-----------------------------------------------|-------------|------------------------------|
| Per-org Postgres schema vs `org_id` everywhere | Phase H     | `org_id` everywhere — simpler, fewer migrations |
| Stripe Checkout vs Billing portal              | Phase I     | Checkout for v1; Billing portal once we have plan changes |
| In-process event bus vs Redis from day one     | Phase C     | In-process; switch to Redis when we go multi-worker |
| Org subdomain vs path prefix                   | Phase H     | Subdomain — clearer multi-tenant signal |
| Self-host video vs partner                     | Phase K     | Partner (Mux or Cloudflare Stream) |
| Public results: SSR vs hydrate                 | Phase J     | SSR — SEO and first-paint win for spectator surface |

---

## 16. Glossary

- **Slice** — a named subset of broadcast state delivered as an SSE delta (e.g., `lbAthletes`, `judgeL2`).
- **Projection** — a denormalised read model maintained by event subscribers.
- **Scene** — a named composite broadcast state with declared overlay visibility and content rules.
- **Director / Scoring operator / Master judge / Lane judge** — the four operator roles, separated by surface and authority.
- **AUTO / MANUAL** — per-field control state. AUTO follows engine projections; MANUAL is director-set and engine-immune.
- **Restart token** — startup epoch broadcast in every SSE payload; clients reset version counters on mismatch.
- **Command / Event** — intent vs fact. Commands request; events record.

---

*End of System Design document. Next phase: implementation kickoff begins with Phase A (state schema split), proposed in a separate plan doc.*
