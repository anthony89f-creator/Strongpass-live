# Stage 0 — Monorepo skeleton
**Started:** 2026-05-16
**Status:** Complete

## Goal

Establish the monorepo directory structure, root configuration, and per-package scaffolds — alongside the live monolith — with zero impact on the running system.

## What was built

### Top-level

| File                 | Purpose                                                                    |
|----------------------|----------------------------------------------------------------------------|
| `pyproject.toml`     | Root Python workspace (uv); ruff + mypy config; strict mode in `domain` and `event_store`. |
| `pnpm-workspace.yaml`| JS workspace declaration.                                                  |
| `Makefile`           | Top-level orchestration. `make help`, `bootstrap`, `test-py`, `verify-chain`, `legacy-run`. |
| `.gitignore`         | Appended monorepo patterns (audit/, snapshots/, wire/, node_modules/, etc.) — existing live-system patterns preserved. |
| `README.md`          | Project overview, doctrine, layout, quick-start.                           |
| `audit/.keep`        | Append-only event log dir (runtime; segments gitignored).                  |
| `commands/.keep`     | Command log dir.                                                           |
| `wire/.keep`         | SSE wire log dir.                                                          |
| `snapshots/.keep`    | Snapshot dir.                                                              |

### Package scaffolds (Python)

Each has its own `pyproject.toml`, all installable with `pip install -e` from the venv.

- `apps/server/`
- `packages/contracts/`
- `packages/domain/`
- `packages/event_store/`
- `packages/projections/`
- `packages/gateway/`
- `packages/auth/`
- `packages/cli/`

Python package directories use underscores (`event_store`, not `event-store`) so they're importable. The corresponding distribution names use hyphens (PEP 503).

### Package scaffolds (TypeScript)

- `packages/contracts/` (also a JS workspace member — exports types alongside schemas)
- `packages/overlay-runtime/`
- `packages/operator-ui/`
- `packages/arena-display/`
- `packages/design-tokens/`
- `packages/overlays/` (parent — each overlay scene later becomes a workspace member)

### Other dirs

- `infra/{docker,nginx,systemd,scripts}/` — placeholders for the deploy-related move
- `e2e/` — Playwright integration tests (later)
- `docs/stages/` — this file and its peers

## Design decisions made in this stage

1. **Build alongside, not replace.** No live-system file moved or renamed. `server.py`, `state.json`, `comp.db`, `app/`, `templates/`, and the existing HTML overlays remain at the original paths. The new structure lives next to them. Cutover happens stage-by-stage as each new package replaces an internal seam in the monolith.

2. **Underscored Python package dirs.** The strategy doc shows hyphenated dir names; the implementation uses underscores because Python imports can't span hyphens. Distribution names (`strongpass-event-store`) use the canonical hyphenated form.

3. **Stdlib-only test runner.** `make test-py` discovers tests with `unittest` rather than pytest. The user can switch later without changing test code (unittest tests are pytest-compatible). This keeps Stage 0 → Stage 2 progress independent of any pip install.

4. **Makefile orchestration over Nx/Turborepo.** At our scale (one operator, ~10 packages), a Makefile + native package managers is enough. Revisit when CI build graphs become unwieldy.

5. **`commands/`, `audit/`, `wire/`, `snapshots/` are runtime dirs.** Created here as durable filesystem locations; contents are gitignored. The event store package writes into them.

## Acceptance

- [x] `find . -type d -name packages -maxdepth 1` returns the new dir.
- [x] All package configs are valid TOML.
- [x] `make help` lists the targets.
- [x] No existing file modified beyond `.gitignore` (which has *additions only*).
- [x] Live monolith (`server.py`) continues to run if started.

## Rollback

```
rm -rf apps/ packages/ infra/ e2e/
rm -rf audit/ commands/ wire/ snapshots/   # only if no data inside
rm pyproject.toml pnpm-workspace.yaml Makefile README.md
git checkout -- .gitignore
```

Live system is unaffected; deleting these dirs returns the repo to its pre-skeleton state.

## Next

→ Stage 1 — Contracts package (JSON schemas + Python dataclasses + TS types + tests).
