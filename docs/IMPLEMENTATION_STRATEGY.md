# StrongPass — Implementation Strategy
**Phase:** Bridge — architecture → execution
**Date:** 2026-05-16
**Status:** Planning. No implementation code in this document.
**Predecessors:** `SYSTEM_DESIGN.md`, `PHASE_A_STATE_AND_PROTOCOL.md`, `PHASE_B_AUDIT_REPLAY_RECOVERY.md`
**Audience:** The operator (sole developer initially), future contributors, future Claude sessions instructed to build a specific stage.

---

## 0. What this document is

This is the **build order** for StrongPass. It translates the macro architecture into:

- Monorepo layout (specific paths)
- Package boundaries (what code lives where, what depends on what)
- A stage-by-stage migration from the current Flask monolith into the target architecture, **without breaking the live system**
- Three production tiers (MVP-Prod, Arena-Grade, Commercial)
- Concrete tech decisions with the reasoning
- DX, testing, CI/CD, observability plans
- Operational readiness checklist

It is **not**:
- The codebase. No code in this document — that comes per stage, when each stage starts.
- A schedule. No dates — only ordering, dependencies, and parallelisability.
- A rewrite plan. We **strangle** the monolith; we never bin it.

---

## 1. Current state assessment

We are not starting from zero.

| What exists                                          | State                                     |
|------------------------------------------------------|--------------------------------------------|
| `server.py` (~2,070 lines) Flask monolith            | Live in production at strongpass.live      |
| `comp.db` SQLite (WAL)                               | Authoritative scoring data                 |
| `state.json` (55+ keys, dual-purpose)                | Broadcast contract + engine config         |
| `sse-client.js`                                      | Shared SSE client; 8 overlays use it       |
| 14 raw HTML overlays                                 | Production OBS sources                     |
| 10 Jinja2 comp admin templates                       | `/comp/*` admin                            |
| `app/` helpers (config, sse, database, utils, auth)  | Phase 3 extractions; partial modularisation|
| Nginx + Gunicorn + systemd                           | Operational; auto-restart, SSL             |
| Beta gate + comp password                            | Sitewide and admin auth                    |
| Audit / replay / typed commands / PGM-PRE            | **Not yet built** — Phase A/B specs only   |

**The operating constraints** (from prior decisions):

1. **The live system must not break** during migration. Every stage is independently shippable and rollback-safe.
2. **Visual styling of the control engine is frozen.** We refactor mechanics; we do not redesign chrome until the operator surfaces stage.
3. **All existing endpoints stay reachable** while the new ones come online. `/update`, `/state.json`, `/stream`, `/control.html`, `/judge.html`, every `/comp/*` route — all preserved.

This **forces** a strangler-fig migration. We extract behaviours from `server.py` into typed packages one slice at a time. The legacy shim (`/update`, `/state.json`) keeps running until the last legacy client is gone.

---

## 2. Build philosophy

Six principles. Apply them at every decision.

1. **Correctness over velocity.** The broadcast cannot be wrong. A late feature is worse than a wrong one only if "wrong" is invisible — which in a live event it usually isn't.
2. **Deterministic by default.** Every behaviour that affects scoring or broadcast state must be replayable. Non-determinism is a defect, not a quirk.
3. **Boring tech.** SQLite, Flask, vanilla JS for overlays, Svelte for operator UI, NDJSON for logs, systemd for ops. We pick frameworks people can hire for, debug at 2am, and replace incrementally.
4. **Operational first.** Every feature ships with: logs, an error path, a runbook entry, and a way to verify it works in production. No feature is "done" until an oncall could diagnose it cold.
5. **Strangler-fig migration.** New code wraps old code; old code retires when nothing reaches it. We never throw out the monolith — we hollow it.
6. **One tier at a time.** MVP-Prod (running real events) → Arena-Grade (paid customers) → Commercial (platform). Don't build commercial-tier features until arena-grade is stable, and don't build arena-grade until MVP-Prod is correct.

---

## 3. Monorepo structure

The current dir (`/Users/tonyfarrell/Desktop/files`) becomes the monorepo root. Target layout:

```
strongpass/
├── apps/
│   ├── server/                       Entry point. Wires packages. Runs gunicorn.
│   │   ├── src/server.py
│   │   ├── pyproject.toml
│   │   └── tests/
│   └── audit-viewer/                 (Later — may merge into operator-ui)
│
├── packages/
│   ├── contracts/                    Source of truth for event/command shapes.
│   │   ├── ts/                       TS types
│   │   ├── python/                   Pydantic models (generated)
│   │   ├── schema/                   JSON Schema (generated)
│   │   └── package.json
│   │
│   ├── domain/                       Pure Python domain logic. Zero framework imports.
│   │   ├── competition/
│   │   ├── broadcast/
│   │   ├── judging/
│   │   ├── identity/
│   │   └── pyproject.toml
│   │
│   ├── event-store/                  Append-only segment writer, hash chain, snapshots.
│   │   ├── segments/
│   │   ├── chain/
│   │   ├── snapshots/
│   │   ├── replay/
│   │   └── pyproject.toml
│   │
│   ├── projections/                  Registry, invalidation, cache.
│   │   └── pyproject.toml
│   │
│   ├── gateway/                      HTTP command surface + SSE fanout + wire log.
│   │   ├── command/                  /command/{domain}/{name}
│   │   ├── stream/                   /stream (versioned slices)
│   │   ├── wire/                     wire log capture
│   │   ├── compat/                   legacy /update + /state.json shims
│   │   └── pyproject.toml
│   │
│   ├── auth/                         Beta token, comp session, principals, future org.
│   │   └── pyproject.toml
│   │
│   ├── overlay-runtime/              Shared TS for overlays: SSE client, DOM patcher,
│   │   ├── src/sse.ts                hash dirty-check, scene state machine.
│   │   ├── src/patch.ts              Builds to /static/overlay-runtime.js (vanilla).
│   │   ├── src/scene.ts
│   │   └── package.json
│   │
│   ├── overlays/                     Each overlay scene. Compile to standalone HTML+JS.
│   │   ├── leaderboard/
│   │   ├── lowerthird/
│   │   ├── scorebug/
│   │   ├── champion/
│   │   ├── reps/
│   │   ├── lineup/
│   │   ├── h2h/
│   │   ├── manual_lowerthird/
│   │   ├── eventbar/                 (new in Phase A)
│   │   ├── sponsorbug/               (new)
│   │   ├── aston/                    (new)
│   │   └── package.json
│   │
│   ├── operator-ui/                  Svelte 5 SPA for the operator surfaces.
│   │   ├── src/routes/
│   │   │   ├── director/
│   │   │   ├── scoring/
│   │   │   ├── judge/
│   │   │   ├── master/
│   │   │   ├── commentator/
│   │   │   ├── audit/
│   │   │   └── org/
│   │   ├── src/lib/                  Stores, command client, design tokens.
│   │   └── package.json
│   │
│   ├── arena-display/                Audience-facing big screen surface.
│   │   └── package.json
│   │
│   ├── design-tokens/                Color, type, spacing, motion — shared across UI.
│   │   ├── tokens.json
│   │   ├── tokens.css                generated
│   │   └── package.json
│   │
│   ├── cli/                          ops tools (replay, snapshot, archive, dr-drill).
│   │   └── pyproject.toml
│   │
│   └── infra/                        Docker, nginx, systemd, deploy scripts.
│       ├── docker/
│       ├── nginx/
│       ├── systemd/
│       └── scripts/
│
├── e2e/                              Playwright integration tests.
│
├── docs/                             (this folder — already exists)
│
├── pnpm-workspace.yaml               JS workspace
├── pyproject.toml                    Python workspace (uv / hatch)
├── Makefile                          Top-level orchestration (`make dev`, `make test`)
├── .github/workflows/                CI
└── README.md
```

Notes:

- **Python and TypeScript coexist.** `pnpm` workspaces for JS, `uv` for Python. The Makefile is the user-facing orchestration; underneath it's two ecosystems.
- **`apps/server` is the only Python entry point** for the live system. Every other Python package is a library it imports.
- **`packages/overlays/*` build to standalone HTML+JS** consumed by OBS Browser Source. No runtime framework dependency in production overlays.
- **`packages/operator-ui` is a single Svelte app** with internal routes for each surface. Built once, served as a static SPA.
- **`packages/contracts` is the contract surface** — TS types and Python Pydantic models generated from a single JSON schema source. Changes here propagate everywhere; CI enforces.

---

## 4. Package boundaries

What each package owns, what it exposes, and what it depends on.

### 4.1 `contracts`

- **Owns:** Event schemas, command schemas, projection payload schemas, error code catalogue.
- **Exposes:** TypeScript types (`packages/contracts/ts/`), Python Pydantic models (`packages/contracts/python/`), JSON Schemas (`packages/contracts/schema/`).
- **Depends on:** nothing.
- **Build artefact:** typed packages published to internal workspace.
- **Discipline:** Any change is a versioned breaking change. CI verifies all consumers.

### 4.2 `domain`

- **Owns:** Domain logic — Competition, Broadcast, Judging, Identity handlers. Pure Python; no HTTP, no DB, no Flask. Stateless functions of `(currentState, command) → events`.
- **Exposes:** Handler functions per command type. Returns events to commit.
- **Depends on:** `contracts`.
- **Discipline:** No I/O. No `time.now()`. Every test is a function-level unit test with no fixtures beyond Python data structures.

### 4.3 `event-store`

- **Owns:** NDJSON segment writer, hash chain, fsync semantics, index, in-memory tail, snapshots (micro/periodic/milestone/final), replay engine (Recover/Forensic/Sandbox/Wire/Diff modes), cold archive.
- **Exposes:** `append(event)` → fsynced + chained, `read(auditId)`, `tail(since)`, `snapshot()`, `replay(mode, target)`.
- **Depends on:** `contracts`.
- **Discipline:** This is the durability core. Heavy testing. Every change goes through chain-verifier + replay determinism CI.

### 4.4 `projections`

- **Owns:** Projection registry, invalidation hooks, in-memory cache, on-disk mirror (optional), versioning per projection.
- **Exposes:** `register(name, sourceEvents, computeFn)`, `read(name)`, `invalidate(name)`.
- **Depends on:** `contracts`, `domain` (for reading authoritative state when recomputing).
- **Discipline:** Every projection is a pure function. Replay determinism CI catches violations.

### 4.5 `gateway`

- **Owns:** HTTP command server (`/command/{domain}/{name}`), SSE stream server (`/stream`), wire log, command log, legacy `/update` shim, legacy `/state.json` shim.
- **Exposes:** Flask blueprint mounted by `apps/server`.
- **Depends on:** `contracts`, `domain`, `event-store`, `projections`, `auth`.
- **Discipline:** Thin — handlers do schema validation, principal attach, then delegate to domain. No business logic here.

### 4.6 `auth`

- **Owns:** Beta token gate, comp session auth, principal model, role checks. Future: organisation membership, API keys.
- **Exposes:** Flask middleware, principal-from-request, role-check decorators.
- **Depends on:** `contracts` (for principal shape).
- **Discipline:** No business logic. Pure gating.

### 4.7 `overlay-runtime`

- **Owns:** Shared TS for every overlay: SSE client (reconnect, restart-token, subscriptions), DOM patch helpers, hash fingerprint utility, scene state machine, GPU-safe animation helpers.
- **Exposes:** Built JS at `dist/overlay-runtime.js`, included by every overlay HTML.
- **Depends on:** `contracts/ts` (event types).
- **Discipline:** Tiny. <20KB minified. No framework. Tree-shakeable.

### 4.8 `overlays/*`

- **Owns:** Each individual OBS browser source overlay. Built as standalone HTML+JS that loads `overlay-runtime.js` and renders one scene.
- **Exposes:** Static files served by gateway.
- **Depends on:** `overlay-runtime`, `design-tokens`.
- **Discipline:** No external network calls. No JS frameworks. Pure DOM patching.

### 4.9 `operator-ui`

- **Owns:** Svelte 5 SPA: director console, scoring engine, judge surfaces (lane + master), commentator console, audit viewer, organiser dashboard (future), public results.
- **Exposes:** Static SPA built to `dist/`, served by gateway under `/app/*`.
- **Depends on:** `contracts/ts`, `design-tokens`.
- **Discipline:** Per-surface routes. Shared command client. Optimistic UI with rollback. No global state library — Svelte 5 runes + scoped stores.

### 4.10 `arena-display`

- **Owns:** Audience-facing surface for big-screen displays.
- **Exposes:** Standalone HTML+JS.
- **Depends on:** `overlay-runtime`, `design-tokens`.
- **Discipline:** Same as overlays. Lives in its own package because its scenes diverge from broadcast overlays.

### 4.11 `design-tokens`

- **Owns:** Color, typography, spacing, radius, motion tokens. Generates CSS variables and Svelte tokens.
- **Exposes:** `tokens.json` (source) → `tokens.css` (generated) → consumed by overlays, operator-ui, arena-display.
- **Depends on:** nothing.
- **Discipline:** Token changes are reviewed like contract changes — they affect visuals across every surface.

### 4.12 `cli`

- **Owns:** Operations tools: `sp replay`, `sp snapshot`, `sp archive`, `sp dr-drill`, `sp verify-chain`, `sp export-comp`.
- **Exposes:** `sp` executable installed in venv.
- **Depends on:** all backend packages.
- **Discipline:** Used by oncall during incidents. Must be obvious and safe. Destructive operations are flag-gated.

### 4.13 `infra`

- **Owns:** Dockerfiles, docker-compose (local), nginx config, systemd unit, deploy scripts, backup scripts.
- **Exposes:** Files referenced by `apps/server` deployment.
- **Depends on:** nothing.
- **Discipline:** Infra-as-code; reviewed alongside the code change that needs it.

### 4.14 Dependency graph (packages)

```
                              contracts
                                  │
        ┌─────────────────────────┼──────────────────────────┐
        │                         │                          │
        ▼                         ▼                          ▼
      auth                     domain                  overlay-runtime
        │                         │                          │
        │                         ▼                          │
        │                  event-store                       │
        │                         │                          │
        │                         ▼                          │
        │                  projections                       │
        │                         │                          │
        └──────────┬──────────────┘                          │
                   ▼                                          │
                gateway ────────────► wire log               │
                   │                                          │
                   ▼                                          │
              apps/server                                     │
                                                              │
                                                              ▼
                            design-tokens ──► overlays, operator-ui, arena-display
                                                              ▲
                                                              │
                                                       contracts/ts
```

---

## 5. Technical decisions

Final recommendations with reasoning. Each decision has one **incumbent** (what we have now) and one **target** (what we move to). Where they're the same, we keep it.

### 5.1 Backend framework — **Flask (incumbent, keep)**

- **Decision:** Stay on Flask. Modularise via packages, add type hints + Pydantic schemas at every boundary.
- **Why:** 2,000 lines of working Flask is operationally proven. SSE works fine through nginx with `proxy_buffering off`. Async benefits are marginal at our connection count (<50). Migrating to FastAPI is a multi-month rewrite that buys us a `Schema` decorator we can get from Pydantic alone.
- **Revisit if:** Connection count goes above 500, or async I/O becomes a real bottleneck.

### 5.2 Frontend — **Svelte 5 (operator-ui) + Vanilla TS (overlays)**

- **Decision:** Svelte 5 with runes for operator surfaces. Hand-rolled TypeScript that compiles to vanilla JS for overlays.
- **Why operator-ui = Svelte 5:** Fine-grained reactivity matches SSE-driven state. Bundle is small (relevant during paid events on hotel Wi-Fi). Compile-time reactivity means no virtual DOM cost. Runes API stabilised in 2024; ergonomic.
- **Why overlays = vanilla:** OBS Browser Source runs each overlay as an isolated page. A framework runtime is dead weight when we render <20 elements per scene. Vanilla also makes the overlay reload deterministic.
- **Reject:** React (operator-ui — bundle cost, VDOM overhead in dense UIs). Vue (operator-ui — fine, Svelte is just better for this pattern). SvelteKit (operator-ui — SSR not needed; client-only SPA is simpler).

### 5.3 Event bus — **In-process Python (v1) → Redis Streams (multi-worker)**

- **Decision:** A trivial in-process pub-sub for v1. Move to Redis Streams when we cross to multi-worker (Phase H+).
- **Why:** A single gunicorn worker can fan out to all SSE clients in process. Redis adds latency, an operational dependency, and tail-event ordering complexity we don't need yet. The seam (a small Bus interface in `event-store`) is designed so swap is mechanical.
- **Reject:** RabbitMQ (too heavy), Kafka (way too heavy), NATS (lean but still over-spec for one process).

### 5.4 Realtime transport — **SSE (keep)**

- **Decision:** Server-Sent Events. POST for commands.
- **Why:** Already established. Survives nginx. Auto-reconnects. Matches our one-way fanout pattern. WebSocket buys bidirectional push we don't need.
- **Revisit if:** Judge surfaces need server-initiated prompts ("next heat starts in 30s, confirm ready"). At that point evaluate WebSocket or short-poll the command surface.

### 5.5 Persistence — **SQLite (v1) → Postgres (multi-tenant)**

- **Decision:** SQLite with WAL mode for v1. Postgres at the multi-tenant boundary (Phase H).
- **Why SQLite:** Single competition, single host, single writer. SQLite is faster and simpler than Postgres at this scale. The `VACUUM INTO` snapshot path is exactly what we need.
- **Why eventual Postgres:** Concurrent writes from multiple orgs requires real MVCC. The migration is mechanical (schema is straightforward) once we need it.
- **Audit log (parallel to DB):** NDJSON files. Not in either DB. The append-only requirement is the whole story; a DB makes that harder, not easier.

### 5.6 Overlay renderer — **Vanilla DOM + CSS animations**

- **Decision:** Vanilla DOM with hash-based dirty patching. CSS for animations. Zero JS frameworks in overlays.
- **Why:** Every overlay flicker we've fixed in prior phases came from over-eager re-rendering. The simpler the renderer, the easier to keep correct. CSS animations are GPU-composited and survive reconnect.
- **Reject:** GSAP (30KB, adds JS animation thread cost). React-overlay frameworks (too much runtime).

### 5.7 Animation system — **CSS animations + tiny JS orchestrator**

- **Decision:** All visual motion is CSS animations triggered by class toggles. JS only orchestrates timing (when to add/remove classes) — never animates property values per frame.
- **Why:** Compositor-driven, GPU-accelerated, survives JS jank. The class-toggle pattern is the only way to reliably restart an animation without re-creating DOM (which is the recurring source of broadcast jitter).

### 5.8 State management

- **Server:** Domain handlers + projections, as Phase A specifies. No "state manager" abstraction.
- **Client (operator-ui):** Svelte stores, one per logical concern (pgmStore, preStore, lanesStore, leaderboardStore). Each subscribes to its slice of `/stream`. No global state library.
- **Client (overlays):** A tiny module-level mirror object updated by the SSE client. Direct subscription, no observable framework.

### 5.9 Deployment — **Single VM (Hetzner) → containers when multi-tenant**

- **Decision:** Continue single-VM bare-metal for MVP-Prod and Arena-Grade. Containerise + horizontal scale at Commercial tier.
- **Why:** Bare-metal on Hetzner is the cheapest, fastest path for one operator + one event. Containers add operational surface area we don't need until we have orgs in different regions.
- **Local dev:** Docker compose. Mirrors prod environment locally; not what runs in prod.

### 5.10 Tooling

| Tool                    | Decision                       | Why                                                          |
|-------------------------|--------------------------------|---------------------------------------------------------------|
| Python package mgr      | **uv**                         | Fast, drop-in pip replacement; lock files; workspace support  |
| JS package mgr          | **pnpm**                       | Workspace-aware, disk-efficient, content-addressable          |
| Top-level orchestration | **Makefile** (or `just`)       | Boring, ubiquitous, no toolchain install                      |
| Python linter           | **ruff**                       | Fast, replaces flake8+isort+black                             |
| Python types            | **mypy** (strict mode in domain)| Hard mode where logic lives; loose mode at framework edges    |
| TS linter               | **biome** (or eslint+prettier) | Biome is faster; ecosystem still maturing — defer call to start of operator-ui |
| Schema source           | **JSON Schema 2020-12**        | Language-agnostic; generators exist for TS + Pydantic         |
| Test runners            | **pytest**, **vitest**, **playwright** | Standard, fast, well-instrumented                     |
| CI                      | **GitHub Actions**             | Already operative for Hetzner deploy; free at our usage       |
| Container runtime       | **Docker** (local) — host runs bare-metal | Reproducibility for dev; prod gets nginx+gunicorn+systemd |
| Observability           | **structured JSON logs** + **Sentry** + **statsd→Grafana** (later) | Logs first; metrics dashboard at Arena-Grade |

---

## 6. Build order

The implementation is a sequence of **stages**. Each stage:

- Has a single primary goal
- Ships behind a flag where it can affect live behaviour
- Has clear exit criteria
- Lists what *should not* be built yet (scope discipline)
- Maps to packages and Phase A/B specs by reference

### Notation

- **Goal**: the single outcome.
- **Touches**: which packages and which existing files.
- **Migration touchpoint**: how the live system stays intact while this stage rolls out.
- **Exit**: how we know we're done.
- **Risk**: technical and operational risks specific to this stage.
- **Defer**: explicit scope cuts.

### Stage 0 — Monorepo skeleton

- **Goal:** The repo layout exists. Empty packages, lint/test scaffolding, CI baseline.
- **Touches:** Root files only. `server.py` untouched.
- **Migration touchpoint:** Zero — purely additive scaffolding.
- **Exit:** `make test` runs in CI, passes (with no real tests yet). `pnpm install` and `uv sync` both succeed at root.
- **Risk:** Bikeshedding the layout. Mitigation: section 3 above is binding.
- **Defer:** Any code that doesn't exist to make CI green.

### Stage 1 — `contracts` package

- **Goal:** Event, command, projection, and error contracts defined once in JSON Schema; TS + Pydantic generated from it.
- **Touches:** `packages/contracts/`. `server.py` untouched.
- **Migration touchpoint:** None yet.
- **Exit:** Schemas for the events catalogued in Phase A §11 and Phase A §12. Generated TS+Python pass type-check.
- **Risk:** Schema fragility — adding optional fields right now and tightening later. Mitigation: `schemaVersion` per event, additive-only changes between stages.
- **Defer:** Schemas for billing, organisations, livestream — those tier up later.

### Stage 2 — `event-store` minimum (Phase B.0)

- **Goal:** Append-only NDJSON segment writer with versioning, in-memory tail, basic index. **No chain yet.**
- **Touches:** `packages/event-store/`. `server.py` gets a single new call: every mutation in the existing handlers writes a "shadow event" via `event_store.append(...)` after its existing logic.
- **Migration touchpoint:** Shadow writes only — current behaviour unaffected if the shadow path errors (logged + alarmed, but not crash-blocking).
- **Exit:** A 30-minute mock event produces a complete, in-order event log. The log can be loaded and read top-to-bottom by a simple CLI tool.
- **Risk:** Shadow writes diverging from real state. Mitigation: a daily CI test that replays the log and asserts the resulting projection matches the live state hash for the same time window.
- **Defer:** Hash chain (Stage 3), command log (Stage 5), snapshots (Stage 6).

### Stage 3 — Hash chain + chain verifier (Phase B.1)

- **Goal:** Tamper evidence. Every event has `prevHash`/`selfHash`. Off-host chain head replication.
- **Touches:** `packages/event-store/chain/`. `infra/scripts/` for off-host rsync.
- **Exit:** Manual tamper on a sealed segment detected on next verify.
- **Risk:** Hash collision is not the risk; canonical JSON serialisation bugs are. Mitigation: unit-test canonical-form with adversarial inputs.

### Stage 4 — `projections` package

- **Goal:** Move existing in-line projection logic (`get_leaderboard`, `_results_cache`, `sync_comp_to_broadcast`'s broadcast block) into the projections registry. Invalidation triggered by `event_store.append`.
- **Touches:** `packages/projections/`, `server.py` (refactor the affected functions to read from projections).
- **Migration touchpoint:** Existing `state.json` is still being written by `sync_comp_to_broadcast`; the projection just provides a parallel read path. Compare on every read; alert on divergence.
- **Exit:** All overlay reads come from projections; `state.json` write loop runs in parallel for one release; cutover when divergence stays at zero for 7 days.
- **Risk:** Divergence between projections and legacy state writers. Mitigation: explicit divergence detector.
- **Defer:** Materialised disk-mirror of projections (memory-only is fine for v1).

### Stage 5 — `gateway` typed command surface (Phase A.2)

- **Goal:** `/command/{domain}/{name}` endpoints alongside existing `/update`. Schema validation, idempotency window, principal attach.
- **Touches:** `packages/gateway/command/`, `apps/server/server.py` (mount new blueprint).
- **Migration touchpoint:** Both `/update` and `/command/*` work; new clients use the typed surface; `/update` becomes a translation shim internally.
- **Exit:** Recorded session of typed commands replays cleanly. `/update` shim emits a deprecation log line for every call.
- **Risk:** Shim drift — `/update` and `/command/*` interpret the same intent differently. Mitigation: the shim translates and calls the typed handler; there is one code path.

### Stage 6 — Snapshots (Phase B.3)

- **Goal:** Periodic + milestone snapshots. SQLite `VACUUM INTO`, atomic JSON capture, manifest, pruning, off-host rsync.
- **Touches:** `packages/event-store/snapshots/`, `infra/scripts/`.
- **Exit:** Process restart picks latest valid snapshot and replays tail in <60s on real-event-sized dataset.
- **Risk:** Snapshot during heavy write blocks the writer. Mitigation: VACUUM INTO uses a read snapshot of the WAL; broadcast capture is atomic file copy.

### Stage 7 — Recover-mode replay (Phase B.4)

- **Goal:** Crash-only recovery; deterministic; fast. CI determinism test.
- **Touches:** `packages/event-store/replay/`, `apps/server/server.py` (startup path).
- **Exit:** 10× restart drill produces identical final state hash. Determinism CI test running daily.
- **Risk:** Non-determinism in existing projection code (wall-clock reads, random IDs). Mitigation: aggressive code review of any function in the projection invalidation chain; explicit purity test in unit suite.

### Stage 8 — Versioned sliced SSE (Phase A.4)

- **Goal:** `subscribe=` and `since.*=` query params; per-slice version counters; selective delta delivery.
- **Touches:** `packages/gateway/stream/`, `packages/overlay-runtime/sse.ts`.
- **Migration touchpoint:** Old SSE clients (no subscribe param) get the full-state stream as today.
- **Exit:** 20 simultaneous overlays produce <50KB/s idle, <500KB/s during score storm.
- **Risk:** Bug in version tracking → clients see duplicated or missed deltas. Mitigation: client-side gap detection (request resync if version jumps non-contiguously).

### Stage 9 — Broadcast domain extraction (Phase A.6)

- **Goal:** `broadcast.cueScene`, `broadcast.takeScene`, `broadcast.cutToBlack`. PRE state alongside PGM. Scene catalogue declared.
- **Touches:** `packages/domain/broadcast/`, `packages/gateway/command/`, new file `packages/contracts/schema/scenes/*.json`.
- **Migration touchpoint:** Existing `/update` overlay-flag toggles continue to work. PRE is opt-in via a feature flag on the director console.
- **Exit:** Director can run a full event using only CUE/TAKE.
- **Risk:** Scene catalogue grows without bound. Mitigation: spec a closed set in Phase A §16.3 and require an architecture review for additions.

### Stage 10 — Scene transitions + layer state machine (Phase A.7)

- **Goal:** Scene-owned animations. Layer state machine with timeouts. Audit log includes per-step transition events.
- **Touches:** `packages/domain/broadcast/`, `packages/overlay-runtime/scene.ts`.
- **Exit:** Champion reveal runs identically every time; full transition sequence in audit.
- **Risk:** Step-level events bloat the audit. Mitigation: tagged with `causationId` so they collapse in the viewer.

### Stage 11 — Command log + wire log (Phase B.2, B.7)

- **Goal:** Capture every command (incl. rejected) and every outbound SSE frame.
- **Touches:** `packages/gateway/wire/`, `packages/event-store/segments/` (command log shares writer style).
- **Exit:** Audit query "what did op-tony attempt?" returns commands; "what did client X receive at T?" returns frames.
- **Risk:** Wire log volume overruns disk. Mitigation: 24h ring, sampling fallback at high client count.

### Stage 12 — Overlay runtime hardening (Phase A.9)

- **Goal:** Migrate every overlay to consume `overlay-runtime` package; codify build-once/patch-many; add `eventbar`, `sponsorbug`, `aston` overlays.
- **Touches:** `packages/overlay-runtime/`, `packages/overlays/*`.
- **Migration touchpoint:** New overlay files served alongside legacy ones; switch OBS scene by scene.
- **Exit:** 30-minute event-storm test produces zero overlay flicker.
- **Risk:** Browser-source caching — old overlays linger. Mitigation: cache-bust query param + documented OBS-source-replace procedure.

### Stage 13 — Operator UI: director console (Phase A.10)

- **Goal:** Replace `control.html` with the Svelte 5 director surface. PGM/PRE monitor, scene picker, queue panel, layer panel, command log, hotkeys.
- **Touches:** `packages/operator-ui/src/routes/director/`, `packages/design-tokens/`.
- **Migration touchpoint:** Old `/control.html` served at `/control-legacy.html` for one release; new SPA at `/app/director`. Director chooses which to use during the transition.
- **Exit:** A real rehearsal driven entirely by the new console succeeds with no fall-back to legacy.
- **Risk:** Hotkey conflicts with OBS. Mitigation: hotkeys only fire when the operator-ui window is focused; reserved-key registry.

### Stage 14 — Audit viewer + forensic replay (Phase B.5, B.6)

- **Goal:** Read-only audit surface with filters, trace tree, state-at-T, diff, overlay preview.
- **Touches:** `packages/operator-ui/src/routes/audit/`, `packages/event-store/replay/`.
- **Exit:** Owner reviews a heat in 2 minutes; produces a dispute-ready evidence pack.
- **Risk:** Forensic replay performance on large logs. Mitigation: anchor snapping + speed controls + materialised milestone snapshots.

### Stage 15 — Operator UI: judge surfaces (Phase A.10 cont.)

- **Goal:** Mobile-first lane judge phone surface; master judge tablet surface. Submission queue with retry on reconnect.
- **Touches:** `packages/operator-ui/src/routes/judge/` and `master/`.
- **Migration touchpoint:** `/judge-legacy.html` retained; new surface at `/app/judge`.
- **Exit:** Two mock heats run with judges on cellular only, no data loss.
- **Risk:** Offline queue divergence on long disconnects. Mitigation: max queue depth + visible queue indicator + explicit drop policy with reason captured.

### Stage 16 — Scene queue + commentator console (Phase A.8)

- **Goal:** Cued scene queue (manual/scheduled/chained). Commentator console for ASTON captions. Sponsor rotation.
- **Touches:** `packages/operator-ui/src/routes/commentator/`, `packages/domain/broadcast/queue/`, sponsor rotation in `packages/domain/broadcast/`.
- **Exit:** Commentator pushes a fact card live during a heat; sponsor bug cycles on schedule.

### Stage 17 — Arena display

- **Goal:** Audience-facing big-screen surface for venue projection.
- **Touches:** `packages/arena-display/`.
- **Exit:** Venue projector runs from the arena display URL; switches scenes independently of broadcast director.

### Stage 18 — Cold archive + final snapshot (Phase B.8, B.10)

- **Goal:** Object storage upload on competition close. Final snapshot signing. Restore-to-sandbox path.
- **Touches:** `packages/event-store/snapshots/`, `packages/cli/sp-archive/`.
- **Exit:** Any retained competition can be re-read end-to-end in <5min from cold.

### Stage 19 — Operational hardening (Phase A.12)

- **Goal:** Structured JSON logs, CSRF, rate limiting, runbook for top 5 failure modes, off-host snapshot drill verified quarterly.
- **Touches:** Cross-cutting.
- **Exit:** First paid event runs without operator intervention beyond planned workflows.

### Stage 20 — Wire-mode replay (Phase B.9)

- **Goal:** Pixel-level forensic reproduction — headless browser plays recorded wire frames.
- **Touches:** `packages/cli/sp-wire-replay/`, Playwright harness.
- **Exit:** Disputed moment reproduced visually and signed off.

### Stages 21+ — Commercial tier

Multi-tenancy (Phase H), billing (Phase I), public spectator surface (Phase J), livestream/content (Phase K). Each is a multi-stage initiative in its own right; out of scope of this document until MVP-Prod and Arena-Grade are stable.

---

## 7. Parallelisation map

Stages that can run in parallel after their dependencies are met.

```
   0 ─► 1 ─► 2 ─► 3 ─► 4 ─► 5
                            │
                            ├──► 6 ─► 7
                            │         │
                            │         ├──► 8
                            │         │
                            │         └──► 9 ─► 10
                            │                    │
                            ├──► 11              │
                            │                    │
                            │                    ▼
                            └─────────► 12 ──► 13 ──► 14
                                                       │
                                                       ├──► 15
                                                       │
                                                       ├──► 16
                                                       │
                                                       ├──► 17
                                                       │
                                                       ├──► 18 ──► 20
                                                       │
                                                       └──► 19
```

Critical path: 0 → 1 → 2 → 3 → 4 → 5 → 9 → 10 → 13 → 14 → 19.

Common parallels:
- 6 (snapshots) parallel with 8 (sliced SSE) once 5 done.
- 11 (wire/cmd log) parallel with 9/10 (broadcast extraction).
- 15 / 16 / 17 (UI surfaces) all parallel after 13 lands a stable design system.

What **cannot** be parallelised:
- Stages that modify the same file in `server.py` simultaneously.
- Stages 2 → 3 → 4 → 5: each consumes the prior's API.

---

## 8. MVP vs Arena-Grade vs Commercial

Three tiers. Each is a target. Each tier sets the *exit criteria* — the system meets the tier when those criteria are met.

### 8.1 Tier 1 — MVP-Prod

**Goal:** Run a single live event with operator confidence.

| Capability                        | State                              |
|-----------------------------------|-------------------------------------|
| Score recording                   | ✅ already live                     |
| Heat/event advance                | ✅ already live                     |
| Live overlays                     | ✅ already live (8 overlays)        |
| Audit log                         | new — Stage 2                       |
| Hash chain                        | new — Stage 3                       |
| Typed commands                    | new — Stage 5                       |
| PRE/PGM with TAKE                 | new — Stage 9                       |
| Snapshots + recover replay        | new — Stages 6–7                    |
| Director console (Svelte)         | new — Stage 13                      |
| Mobile-first judge surface        | new — Stage 15                      |
| Single-host deployment            | ✅ Hetzner VM                       |
| Operator-team-of-one              | yes                                 |

**Exit criteria:**
- Process kill mid-event recovers within 5s with no operator action.
- Replay determinism CI green for 30 consecutive days.
- 10 mock events on staging without manual intervention beyond planned operator actions.

**Infrastructure profile:** 1 Hetzner CPX21 VM, single gunicorn worker, SQLite, nginx, systemd. Off-host rsync to a second VPS or B2 bucket. No Postgres, no Redis, no container orchestration.

### 8.2 Tier 2 — Arena-Grade

**Goal:** Run paid events for external organisers with operational reliability that survives a venue's chaos.

| Adds over MVP-Prod                                  | Stage |
|-----------------------------------------------------|-------|
| Audit viewer with forensic replay                   | 14    |
| Scene queue + commentator console                   | 16    |
| Arena display                                       | 17    |
| Cold archive + final snapshot signing               | 18    |
| Wire log + frame retention                          | 11    |
| Operational hardening (CSRF, rate limits, runbooks) | 19    |
| Wire-mode replay (dispute resolution)               | 20    |
| Sponsor rotation                                    | 16    |
| Multi-operator (still one org)                      | (within 19) |

**Exit criteria:**
- DR drill: restore from cold archive in <5 minutes.
- Dispute resolution: produce signed evidence pack within 10 minutes.
- Multi-operator: two directors, one scoring operator, two judges run a 90-minute event without conflict resolution failures.

**Infrastructure profile:** Same VM tier with off-host audit-log mirroring and object storage. Monitoring stack (Grafana / Sentry) live. PagerDuty oncall rota.

### 8.3 Tier 3 — Commercial

**Goal:** Multi-organiser SaaS platform.

| Adds over Arena-Grade                | Stage band |
|---------------------------------------|------------|
| Multi-tenant orgs                     | 21+        |
| Postgres migration                    | 21+        |
| Redis event bus (multi-worker)        | 21+        |
| Stripe billing                        | 21+        |
| Per-org subdomain routing             | 21+        |
| Public spectator surface              | 21+        |
| Athlete profiles + accounts           | 21+        |
| Livestream / VOD integration          | 21+        |
| API for third-party org integrations  | 21+        |

**Exit criteria:** A second org runs an event in parallel with the House org with zero data cross-contamination, billed via Stripe, with public results pages live.

**Infrastructure profile:** Containerised app pool, Postgres (managed or self-hosted with replica), Redis, object storage (S3-compatible), CDN. Multi-region eventually.

### 8.4 Tier-by-tier feature matrix

```
                                       MVP-Prod   Arena-Grade   Commercial
Audit log                                 ✓            ✓             ✓
Hash chain                                ✓            ✓             ✓
Typed commands                            ✓            ✓             ✓
Snapshots + recovery                      ✓            ✓             ✓
PRE/PGM scene model                       ✓            ✓             ✓
Director console (Svelte)                 ✓            ✓             ✓
Mobile judge surface                      ✓            ✓             ✓
Audit viewer                                          ✓             ✓
Scene queue + commentator                             ✓             ✓
Arena display                                         ✓             ✓
Cold archive                                          ✓             ✓
Wire log + wire-mode replay                           ✓             ✓
Multi-operator (one org)                              ✓             ✓
Multi-tenant                                                        ✓
Postgres                                                            ✓
Stripe billing                                                      ✓
Public results                                                      ✓
Livestream / VOD                                                    ✓
```

---

## 9. Developer experience

### 9.1 Local development

A single command brings everything up.

```
git clone …
make bootstrap        # uv sync + pnpm install + generate contracts
make dev              # docker-compose up + watch builds + serve
```

What `make dev` runs:

- Python: gunicorn with auto-reload bound to localhost:5000.
- Svelte: vite dev server for operator-ui at localhost:5173.
- Overlays: vite build watch for overlay packages.
- A mock OBS browser (Playwright in headed mode) opens the leaderboard + lower-third for visual feedback.
- Docker compose brings up an ephemeral SQLite (file-backed) and optionally a Postgres for multi-tenant work.

Hot-reload: Python via gunicorn `--reload`. Svelte via Vite. Overlays via Vite watch + manual browser refresh (browser refresh is the contract: production overlays don't hot-reload either).

### 9.2 Docker strategy

- **Local-only.** Docker compose for development. Production runs bare-metal on Hetzner.
- **One Dockerfile per app** (`apps/server`, eventually one per worker type when we split).
- **Multi-stage builds.** Base image installs Python + uv + dependencies; final stage is a slim runtime layer.
- **No host-mounted code in CI builds.** Local dev mounts; CI builds are hermetic.
- **Containers come back** at the Commercial tier when we go multi-host; until then, Docker is a development tool, not a deployment target.

### 9.3 Environments

| Environment | Purpose                          | Data                          | Deploy method               |
|-------------|----------------------------------|-------------------------------|------------------------------|
| **local**   | Development on operator's machine| Throwaway SQLite              | `make dev`                  |
| **ci**      | Per-PR validation                | Empty, recreated per run      | GitHub Actions runner       |
| **staging** | Pre-prod rehearsal               | Cloned prod, redacted athletes| `make deploy-staging`       |
| **prod**    | Live competitions                | Real                          | `make deploy-prod` (guarded)|

Staging exists from Arena-Grade tier onward. Before then, we use prod with feature flags carefully.

### 9.4 Testing strategy

Five layers. Each catches a specific bug class.

| Layer                | Tool          | What it catches                                          | Where                          |
|----------------------|---------------|----------------------------------------------------------|---------------------------------|
| **Unit (domain)**    | pytest        | Domain logic correctness; pure functions                 | `packages/domain/*/tests/`     |
| **Unit (frontend)**  | vitest        | Svelte store logic; overlay patch helpers                | adjacent to source             |
| **Schema** (CI gate) | python+ts     | Contract drift; generator mismatch                       | `packages/contracts/tests/`    |
| **Replay determinism** | pytest      | Non-determinism in projections                            | `packages/event-store/tests/`  |
| **Integration**      | pytest+Flask testclient | Gateway → handlers → event log path             | `apps/server/tests/`           |
| **E2E**              | Playwright    | Director console + overlay end-to-end                    | `e2e/`                         |
| **Visual regression**| Playwright + pixel diff | Overlay rendering hasn't changed unexpectedly    | `e2e/visual/`                  |
| **Wire-mode replay** | Playwright + recorded wire log | Overlay still renders past sessions correctly | `e2e/wire/`                  |

### 9.5 Event replay testing (specific test category)

The most important novel test class. A captured event log from a past mock event is the input; the test:
1. Hydrates from the matching snapshot.
2. Replays events through the current code.
3. Hashes the resulting state.
4. Asserts the hash matches a stored expected value.

A mismatch means projection determinism broke. Treat as P1.

### 9.6 Integration test scenarios

The integration suite covers a closed list of "live event" scenarios:

- Heat happy path: 4 lanes submit → master seals → scoring advances.
- Heat with retraction: score recorded → retracted with reason → re-recorded.
- Director conflict: two directors press TAKE concurrently → first wins, second sees `stale_version`.
- SSE reconnect: client disconnects mid-heat → reconnects → catches up via since.X resync.
- Snapshot recovery: process killed mid-heat → restarts → state hash matches.
- Cold archive restore: archive a comp → restore to sandbox → verify hash chain.

Each is a separate test file with deterministic seed data.

### 9.7 Overlay testing

Per overlay:

- **Static render test:** given a known projection payload, the DOM tree matches a saved snapshot.
- **Animation transition test:** show/hide cycle produces the expected class toggle sequence; no orphan animations.
- **Reconnect test:** disconnect mid-render → state held → reconnect → no re-entry animation.
- **Storm test:** 30 SSE deltas per second for 60 seconds → no DOM thrash; <8ms per-frame JS budget held.

### 9.8 Deployment workflow

```
local → push → GitHub Actions:
                  lint + type-check + unit + replay determinism (parallel)
                                    │
                                    ▼
                       integration tests (sequential)
                                    │
                                    ▼
                          E2E tests (sequential)
                                    │
                                    ▼
            artefacts: server image, ui bundle, overlay bundle, schema package
                                    │
                                    ▼
                    [manual approval gate for production]
                                    │
                                    ▼
                       deploy to staging (auto on green)
                                    │
                              smoke test on staging
                                    │
                       deploy to production (operator-triggered)
                                    │
                              post-deploy verification
```

### 9.9 CI/CD strategy

GitHub Actions, three workflow files:

- `.github/workflows/ci.yml` — runs on every PR; lint, type, unit, replay determinism, integration, E2E.
- `.github/workflows/deploy-staging.yml` — runs on merge to main; deploys to staging.
- `.github/workflows/deploy-prod.yml` — operator-triggered; deploys to production after manual approval.

Caching:
- `uv` lockfile → Python deps cache.
- `pnpm` lockfile → Node deps cache.
- Schema generator outputs → contract package cache.

### 9.10 Observability

Three pillars, introduced as we tier up:

| Pillar       | MVP-Prod                                | Arena-Grade                                | Commercial                                  |
|--------------|------------------------------------------|---------------------------------------------|----------------------------------------------|
| **Logs**     | Structured JSON to disk; logrotate       | + ship to Loki / external aggregator        | + per-org tenancy in log queries            |
| **Metrics**  | `/health` + basic counters in log lines  | + statsd → Grafana dashboards               | + per-tenant rollups                         |
| **Errors**   | Bare except → log with stack             | Sentry (DSN per env)                        | + alert routing per org                      |
| **Traces**   | `traceId` in log lines                   | OpenTelemetry traces (export when affordable)| Distributed traces across services          |
| **Alerts**   | UptimeRobot ping                          | PagerDuty + on-call rota                    | Multi-region alerting                        |

The log format is the same at every tier:

```
{"ts":"...","level":"info","trace":"tr-...","cmd":"cmd-...","principal":"op-tony","msg":"command accepted","domain":"competition","name":"recordScore","elapsedMs":14}
```

Every log line carries `trace` so we can stitch a story without a tracing system in v1.

---

## 10. Operational readiness checklist

Each tier has an unambiguous readiness gate. Promotion to a tier requires every box ticked.

### 10.1 MVP-Prod readiness

- [ ] Audit log appends fsync-anchored; replay reproduces state hash
- [ ] Hash chain verifier passes on all sealed segments
- [ ] Snapshot writer produces a manifest with content hash
- [ ] Recover-mode replay completes in <60s on real-event-sized log
- [ ] Determinism CI test green for ≥30 consecutive days
- [ ] Typed command surface live; legacy `/update` shim translates
- [ ] PRE/PGM model live; director uses TAKE during rehearsal
- [ ] Director console SPA covers all current `control.html` workflows
- [ ] Mobile judge surface tested on 4G intermittent connection
- [ ] Off-host audit-log replication verified (1-hour lag tolerance)
- [ ] Off-host snapshot replication verified
- [ ] Process-kill restart drill performed and timed (≤60s)
- [ ] One full mock 90-minute event run end-to-end on staging with no fall-back

### 10.2 Arena-Grade readiness

- [ ] All MVP-Prod boxes still green
- [ ] Audit viewer answers all 8 forensic queries in <500ms
- [ ] Diff and overlay-preview panes functional in audit viewer
- [ ] Wire log captures every connected client's frames; 24h retention
- [ ] Cold archive uploads on competition close; tar+zstd+AES-256
- [ ] Cold-restore drill performed quarterly (<5 minutes)
- [ ] Wire-mode replay reproduces a recorded session visually
- [ ] Scene queue + commentator console live
- [ ] Sponsor rotation runs on schedule
- [ ] Arena display surface deployed to a test venue
- [ ] Runbooks written for the 10 failure modes in Phase B §19
- [ ] PagerDuty on-call rota live
- [ ] Sentry DSN configured for staging + prod
- [ ] Multi-operator drill: 5 simultaneous operators across roles for 90 min

### 10.3 Commercial readiness

- [ ] All Arena-Grade boxes still green
- [ ] Multi-tenant DB schema deployed (Postgres)
- [ ] Per-org subdomain routing in nginx
- [ ] Org isolation enforced (no cross-org reads) — CI tests pass
- [ ] Stripe Customer per org; billing webhook handler covered
- [ ] Public spectator results surface live with social meta
- [ ] Public results performance: <2s first paint on 4G
- [ ] At least one external org runs an event end-to-end alongside House
- [ ] SLA targets agreed and documented per tier
- [ ] Regional backup target chosen and verified

---

## 11. Risk catalogue (technical & operational)

### 11.1 Technical risks

| Risk                                       | Likelihood | Impact | Mitigation                                                        |
|---------------------------------------------|------------|--------|-------------------------------------------------------------------|
| Projection non-determinism                  | medium     | high   | Daily replay determinism CI; explicit purity rules                |
| `state.json` and projections diverge        | medium     | high   | Divergence detector during parallel-write window (Stage 4)        |
| Overlay reload causes brand-visible flicker | low        | high   | Static-render visual regression tests on every PR                 |
| `/update` shim drift                        | medium     | medium | Single code path: shim translates, calls typed handler            |
| Snapshot during heavy write blocks writer   | low        | medium | `VACUUM INTO` against WAL read-snapshot; benchmark per stage      |
| Hash chain break from filesystem corruption | very low   | very high | Off-host chain head; nightly verifier                            |
| Multi-operator UI race conditions           | medium     | medium | Optimistic-lock with `expected.version`; visible conflict toasts  |
| Audit log volume on long events             | low        | low    | Segment rollover; pruning policy; tested at synthetic 8h event    |
| Wire log volume                              | medium     | low    | 24h ring; sampling fallback at high client count                  |

### 11.2 Operational risks

| Risk                                  | Likelihood | Impact | Mitigation                                                         |
|---------------------------------------|------------|--------|--------------------------------------------------------------------|
| Single host failure mid-event         | low        | very high | Off-host snapshot + audit log; 5-min DR RTO target              |
| Operator error during stage rollout   | medium     | medium | Each stage rollback-safe; feature flags for new surfaces           |
| Hot-reload bug in dev leaks to prod   | low        | medium | Production uses `gunicorn` without `--reload`; CI catches via prod-mode test |
| Cert renewal failure (Let's Encrypt)  | low        | high   | Certbot timer + alarms; manual renewal runbook                     |
| nginx config drift                    | low        | medium | infra/nginx tracked in repo; deploy script verifies               |
| Schema drift between TS and Python    | medium     | medium | CI gate fails when generator outputs diverge                        |
| Off-host backup never verified         | medium     | very high | Quarterly restore drill is a calendared, scored exercise         |
| Knowledge concentrated in one operator | very high  | medium | Runbooks + this doc + recorded rehearsals                          |

---

## 12. Deliverables (diagrams)

### 12.1 Top-level implementation flow

```
   docs (architecture)         this doc (build order)            code (delivery)
   ──────────────────  ──►  ────────────────────────  ──►  ─────────────────────
   SYSTEM_DESIGN.md         IMPLEMENTATION_STRATEGY        Stage-by-stage PRs
   PHASE_A_…
   PHASE_B_…                                                Each stage:
                                                              - plan doc
                                                              - branch
                                                              - PR(s)
                                                              - tests
                                                              - acceptance
```

### 12.2 Package dependency (compile-time)

```
                                contracts (TS + Python)
                                    │
              ┌─────────────────────┼─────────────────────────┐
              ▼                     ▼                          ▼
           domain              overlay-runtime           operator-ui (Svelte)
              │                     │                          │
              ▼                     │                          │
        event-store                 │                          │
              │                     │                          │
              ▼                     │                          │
        projections                 │                          │
              │                     │                          │
              │                     │      design-tokens ──────┤
              │                     │            │             │
              │                     │            ▼             ▼
              │                     │       overlays      arena-display
              │                     │
              ▼                     │
            auth                    │
              │                     │
              ▼                     │
           gateway ◄────────────────┘
              │
              ▼
         apps/server
```

### 12.3 Stage dependency graph

```
S0 ──► S1 ──► S2 ──► S3 ──► S4 ──► S5
                                       │
                       ┌───────────────┼────────────────┐
                       ▼               ▼                ▼
                      S6              S8               S11
                       │               │                │
                       ▼               │                │
                      S7               │                │
                       │               │                │
                       └───────┬───────┘                │
                               ▼                        │
                              S9 ──► S10                │
                                       │                │
                                       ▼                │
                               ┌───── S13 ────►  S14 ──┘
                               │
                               ├──► S15
                               ├──► S16
                               ├──► S17
                               ├──► S18 ──► S20
                               └──► S19
                                       │
                                  MVP-Prod
                                       │
                                       ▼
                               Arena-Grade
                                       │
                                       ▼
                                  Commercial
                                  (S21+)
```

### 12.4 Infrastructure topology

```
MVP-Prod / Arena-Grade:
                Internet
                   │
                   ▼
            ┌────────────┐
            │   Hetzner  │ Ubuntu 24.04 (1 VM, CPX21 or larger)
            │    VM      │
            │            │
            │   nginx    │ TLS, proxy_buffering off /stream
            │   gunicorn │ 1 worker, 8 gthread threads
            │   systemd  │ strongpass.service
            │   sqlite   │ comp.db (WAL)
            │   audit/   │ NDJSON segments
            │   snapshots│ periodic / milestone
            └─────┬──────┘
                  │ rsync nightly + per-event
                  ▼
            ┌────────────┐         ┌──────────────────┐
            │ off-host   │ ◄──────►│ object storage   │
            │ backup VPS │         │ (Backblaze B2)   │
            └────────────┘         └──────────────────┘

Commercial:
                Internet
                   │
                   ▼
            ┌────────────┐
            │   CDN /    │
            │  edge      │
            └────┬───────┘
                 │
                 ▼
            ┌────────────┐
            │   nginx    │ subdomain routing *.strongpass.live
            └────┬───────┘
                 │
        ┌────────┼────────┐
        ▼        ▼        ▼
      app w1   app w2   app w3   (gunicorn workers, stateless)
        │        │        │
        └────────┼────────┘
                 │
        ┌────────┼─────────────┐
        ▼        ▼              ▼
      Postgres  Redis      Object storage
                            (audit, snapshots,
                             media, archive)
```

### 12.5 CI/CD topology

```
       Developer
          │ push
          ▼
   ┌──────────────────┐
   │  GitHub          │
   │  Actions         │
   └────┬─────────────┘
        │
        ▼
  ┌──────────────────────────────────────────────────┐
  │  PR pipeline                                     │
  │  ─────────────                                   │
  │  lint │ type │ unit │ schema │ replay-det │ integ│
  │   (parallel)                          ──► E2E    │
  └────────────────────┬─────────────────────────────┘
                       │ green
                       ▼
                  Merge to main
                       │
                       ▼
  ┌──────────────────────────────────────────────────┐
  │  Staging deploy (auto)                           │
  │  build artefacts + ship                          │
  │  smoke test on staging                           │
  └────────────────────┬─────────────────────────────┘
                       │
                       ▼
              [Operator approval]
                       │
                       ▼
  ┌──────────────────────────────────────────────────┐
  │  Production deploy                               │
  │  rolling restart of systemd unit                 │
  │  post-deploy verification + audit-log smoke      │
  └──────────────────────────────────────────────────┘
```

### 12.6 Testing architecture

```
   ┌────────────────────────────────────────────────────┐
   │                    e2e/                            │
   │   playwright: full-stack scenarios                 │
   └──────────────────────┬─────────────────────────────┘
                          │
   ┌──────────────────────┴─────────────────────────────┐
   │  apps/server/tests                                 │
   │  integration: Flask testclient → handlers → log    │
   └──────────────────────┬─────────────────────────────┘
                          │
   ┌──────────────────────┴─────────────────────────────┐
   │  packages/event-store/tests                        │
   │  replay determinism: recorded log → hash assertion │
   └──────────────────────┬─────────────────────────────┘
                          │
   ┌──────────────────────┴─────────────────────────────┐
   │  packages/contracts/tests                          │
   │  schema gen: TS+Python generated outputs match     │
   └──────────────────────┬─────────────────────────────┘
                          │
   ┌──────────────────────┴─────────────────────────────┐
   │  packages/domain/*/tests                           │
   │  unit: pure function correctness                   │
   └──────────────────────┬─────────────────────────────┘
                          │
                          ▼
                       Type check
                       (mypy strict in domain)
```

---

## 13. Per-stage execution template

When any stage starts, the operator (or Claude session) produces a one-page plan in `docs/stages/SXX_<slug>.md`:

```
# Stage S<N> — <Goal>
**Started:** <date>
**Ended:** <date | in-progress>

## Plan
- Specific files to create/touch (paths from §3)
- Specific commands to implement (from contracts)
- Specific tests to add

## Acceptance
- Bullet list copied from this doc

## Rollback
- How to revert if it lands wrong

## Notes after the fact
- What surprised
- What changed in this doc as a result
```

The doc you are reading is the durable plan; stage docs are the journal.

---

## 14. What we are explicitly **not** building (in any tier through Commercial)

Discipline matters. The following are out of scope until and unless an operator-need surfaces:

- **Native mobile apps.** Web is the only client surface.
- **AI-assisted scoring or computer-vision rep counting.** Operator-trusted manual scoring is the product.
- **In-product chat / messaging.** Out-of-band tools (Slack, voice) cover this.
- **A storefront for sponsors.** Sponsor logos are operator-uploaded only.
- **Custom scoring formula DSL.** A closed set of scoring types is enough.
- **Internationalisation beyond English.** Single-language until a real customer asks.
- **An admin UI for nginx / systemd / infrastructure.** SSH + this repo is the admin tool.
- **A drag-and-drop scene composer.** Scenes are declarative + code-reviewed; not user-editable.
- **Custom overlay HTML editor in the product.** Overlays are part of the codebase, not user content.

These boundaries protect the product's identity. We can revisit each individually if a paying customer asks, but the default is **no**.

---

## 15. Open decisions (deferred to stage entry)

| Decision                                            | Stage | Default                                          |
|------------------------------------------------------|-------|---------------------------------------------------|
| Schema source format (JSON Schema vs proto vs Avro) | S1    | JSON Schema 2020-12                              |
| Operator-UI router (TanStack vs SvelteKit pages)    | S13   | Svelte 5 + native client router; SvelteKit later if SSR matters |
| Linter for TS (biome vs eslint+prettier)            | S13   | biome — defer final pick to start of S13         |
| Object storage provider (Backblaze vs S3 vs R2)     | S18   | Backblaze B2 — cheap egress                       |
| Monitoring stack (Grafana Cloud vs self-host)       | S19   | Grafana Cloud free tier; self-host at Commercial |
| Container orchestration (when needed)               | S21+  | k3s on Hetzner first; consider managed at scale  |
| Auth model for orgs (oAuth vs credential)           | S21+  | Credential + magic link; oAuth (Google) later    |

---

## 16. Closing

The macro architecture is fixed; the micro implementation is open.

Three things stay true through every stage:

1. **The audit log is the line.** Don't write code that bypasses it.
2. **Replay is read-only.** Don't write code that lets it mutate live state.
3. **Determinism is mandatory.** Don't write projection code with side-effects.

If any future stage proposes to relax these, that's a design change that requires a doc update — not a code shortcut.

The next document, when work starts, is `docs/stages/S0_monorepo_skeleton.md` — the per-stage plan for the first concrete code change.

---

*End of Implementation Strategy.*
