# Strongpass Live — Engineer Handover

**Current local build:** `BUILD-20260518-K` (not yet deployed to production)
**Last production build:** `BUILD-20260517-C`
**Stable tag:** `v2-runtime-stable`
**Production URL:** `https://strongpass.live`
**GitHub remote:** `https://github.com/anthony89f-creator/Strongpass-live.git`
**Last updated:** 2026-05-18 (BUILD-20260518-K)

---

## What This System Is

Strongpass Live is a broadcast director for strongman/powerlifting competitions. It runs as a single Flask/SQLite/Gunicorn process on a Hetzner Ubuntu 24.04 VPS. A director operator (in the arena) controls which overlay graphics appear in OBS via a browser-based switcher at `/director.html`.

OBS browser sources connect to the same server and receive live state via Server-Sent Events (SSE). The director's preview pane loads overlays in isolated iframes with state injected via `postMessage` instead of SSE.

---

## Critical Operational Rules

1. **ALWAYS use `systemctl reload strongpass` — NEVER `systemctl restart`.**
   Restart causes a ~3-second gap where nginx returns 502. OBS browser sources cache this as a broken stream. Reload does a zero-downtime worker swap (Gunicorn `--preload`).

2. **One Gunicorn worker, multiple gthreads.** SSE connections each hold a gthread. Default pool: 32. At 24+ active connections the server logs a warning. If SSE connections pile up (e.g. OBS scenes not closed), threads exhaust and new requests block.

3. **State lives in `state.json`.** No migrations. Patching state is safe: `load_state()` / `save_state()` under `state_lock` (threading.Lock) everywhere it's touched in routes.

4. **Do NOT touch `server.py` casually.** It is 2600+ lines of production-critical code. If a route change is needed, validate locally before deploying.

---

## Authentication

Two-layer auth:

| Layer | Route | Form field | Cookie |
|---|---|---|---|
| Beta gate | `POST /beta/login` | `token` | `sp_beta` |
| Comp auth | `POST /comp/login` | `password` | Flask `session` with `comp_ok=True` |

The beta gate is a simple HMAC comparison against an env var. The comp password is stored in an env var read at startup. Both use `session.permanent = True` (30-day cookies).

All `/broadcast/*` and `/update` routes require `session.get("comp_ok")`. They return 401 if not authenticated.

---

## Files You Will Touch

| File | Purpose |
|---|---|
| `director.html` | Broadcast switcher UI — the only file an operator uses |
| `leaderboard.html` | Overlay: multi-athlete leaderboard |
| `h2h.html` | Overlay: head-to-head athlete comparison |
| `lineup.html` | Overlay: weight-category lineup viewer with auto-cycle |
| `lowerthird.html` | Overlay: athlete nameplate / lower third graphic |
| `champion.html` | Overlay: winner announcement full-screen |
| `reps.html` | Overlay: live rep counter + judge lights |
| `server.py` | Flask app — routes, SSE, state management, competition engine |
| `sse-client.js` | SSE client drop-in used by all overlay iframes in PGM mode |
| `state.json` | Runtime state (not committed — gitignored) |
| `comp.db` | SQLite database (not committed — gitignored) |

---

## Production Server

| Item | Value |
|---|---|
| Host | `root@strongpass.live` |
| App path | `/opt/strongpass/current/` |
| Service | `strongpass.service` (systemd) |
| Gunicorn | single worker, `--worker-class=gthread --threads=32` |
| Nginx | reverse proxy on :443 (TLS) → :8000 (gunicorn) |
| State file | `/opt/strongpass/current/state.json` |
| DB | `/opt/strongpass/current/comp.db` |

---

## Deployment Workflow

```bash
# Edit files locally
# Deploy a single file
scp ~/Desktop/files/director.html root@strongpass.live:/opt/strongpass/current/

# Deploy multiple files
scp ~/Desktop/files/{director.html,lineup.html,server.py} root@strongpass.live:/opt/strongpass/current/

# Reload (zero-downtime)
ssh root@strongpass.live "systemctl reload strongpass"

# Verify build
# Open director in browser → title bar shows "Director [BUILD-20260517-C]"
# Runtime HUD (top-right corner) shows same build ID
```

---

## Verifying a Deployment

1. Open `https://strongpass.live/director.html` in Chrome
2. Check browser title: `Director [BUILD-20260517-C]` (or current build)
3. Open DevTools Console — first line should be `[Director BOOT] BUILD_ID=BUILD-20260517-C`
4. Click any overlay button — should see `[CLICK] leaderboard — raw DOM event fired` in console
5. Watchdog logs appear every 2 seconds: `[WATCHDOG] {build: BUILD-20260517-C, ...}`
6. TAKE → overlay appears in PGM monitor → CUT → PGM goes dark

---

## Known Stable State (BUILD-20260517-C)

All six overlays verified on production:
- PVW handshake: PASS (all 6)
- Scaling: PASS (host iframe only, no double-scale)
- TAKE: PASS — `POST /broadcast/take` returns 200
- CUT: PASS — `POST /broadcast/clean_group` returns 200
- CLN: PASS — `POST /broadcast/clean` returns 200
- Lineup category selector: PASS — category buttons update `#disp-name` in PVW

---

## Rollback

The repo is tagged `v2-runtime-stable`. To restore to this state on the server:

```bash
git checkout v2-runtime-stable
scp director.html lineup.html leaderboard.html h2h.html champion.html lowerthird.html reps.html server.py sse-client.js root@strongpass.live:/opt/strongpass/current/
ssh root@strongpass.live "systemctl reload strongpass"
```

---

## Contact / Repository

- GitHub: `https://github.com/anthony89f-creator/Strongpass-live.git`
- Branch: `main`
- Tag: `v2-runtime-stable`
