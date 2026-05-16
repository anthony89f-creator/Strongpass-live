# Handover — Phase 2 Optimization
**Date:** 2026-05-16  
**Status:** Complete — ready to deploy  
**Follows:** `HANDOVER_PHASE1_STABILITY.md`

---

## What Was Done

### P2.1 — SSE Payload Cache (`server.py`)

**Problem:** With N clients connected, every SSE event caused N `load_state()` disk reads + N `json.dumps()` calls — one per client thread.

**Fix:**
```python
_sse_payload_cache = {"version": -1, "payload": ""}
_sse_payload_lock  = threading.Lock()

def _get_sse_payload(version):
    with _sse_payload_lock:
        if _sse_payload_cache["version"] == version:
            return _sse_payload_cache["payload"]
        data = load_state()
        evs, _res = _get_cached_results()
        data["events"] = evs
        data["results_version"] = _sse_mod._results_version
        data["restart_token"] = _RESTART_TOKEN
        payload = json.dumps(data)
        _sse_payload_cache["version"] = version
        _sse_payload_cache["payload"] = payload
        return payload
```

SSE generator now calls `_get_sse_payload(seen_version)` instead of inline `load_state()` + `json.dumps()`. First client per event builds and caches; all others reuse the string.

**Impact:** For a production run with e.g. 8 clients (4 judge panels, 2 overlays, 1 director, 1 results screen), reduces disk reads from 8 → 1 per event.

---

### P2.2 — control.html Hash-Based Dirty Checking

**Problem:** `syncFromCompEngine()` was called every 80ms (SSE debounce). Each call unconditionally rebuilt the entire athlete table, lane config, H2H dropdowns, lineup buttons, and set all form inputs — even when only a timer value changed.

**Fix:** `_syncHashes` object tracks fingerprints of:
- `lanes` — JSON of `live.lanes`
- `athletes` — name|category|score per athlete
- `event` — `eventName|eventNum|eventSub|compCategory`
- `champ` — champ fields array

Render functions only fire when their hash changes:
- `renderLaneConfig()` → only on `lanesChanged`
- `renderAthleteTable()` + `renderH2HCategorySelects()` → only on `athletesChanged`
- `initInputs()` → only on `eventChanged || champChanged`
- Winner preview → only on `champChanged`
- `renderCategoryList()` + `renderLineupCatButtons()` → removed from sync path entirely (not driven by synced data; still called from addCategory/removeCategory/boot)

**Impact:** During active competition with timer running, every SSE tick triggered a full DOM rebuild. Now: zero DOM changes on timer ticks. DOM rebuilds only on lane assignment, score submission, heat advance, or operator action.

---

### P2.3 — `/health` Endpoint (`server.py`)

```
GET /health
→ {"status": "ok", "db": "ok", "restart_token": 1747399200}
```

Performs live `SELECT 1` against SQLite. Returns `"db": "error"` if DB is unreachable. No auth required (beta gate bypassed by intent — uptime monitors need unauthenticated access). Beta gate still covers all other endpoints.

**Note:** If you want to protect `/health` behind beta auth, add it to `app/beta_auth.py`'s exemption check.

---

### P2.4 — H3 Fix: `set_lanes` Logs Errors

`action_set_lanes()` in `server.py` — swallowed `regenerate_remaining_heats()` failures silently. Now: `app.logger.error("set_lanes: regenerate_remaining_heats failed: %s", exc, exc_info=True)`. Visible in Gunicorn logs at `/opt/strongpass/logs/service.log`.

---

### Audit Findings (no code change needed)

- **M7** (`api_event_points` missing validation): Already uses `_safe_int()` at line ~2010. Was never the bare `int()` call documented in TD. Closed.
- **L4** (`ws-client.js` still on disk): File does not exist. Already deleted. Closed.

---

## Open Issues After Phase 2

| ID | Severity | Description | Notes |
|----|----------|-------------|-------|
| H1 | HIGH | No CSRF protection on any form | Requires Flask-WTF |
| H2 | HIGH | `/update` and `/judge/set_status` no per-endpoint auth | Mitigated by beta gate |
| H4 | HIGH | New DB connection per operation | Request-scoped `g` object — Phase 6 |
| M1 | MEDIUM | `CATEGORY_ORDER` is global mutable list | Safe with 1 worker |
| M2 | MEDIUM | Migration uses bare `except: pass` | Fix: log in `ensure_schema()` |
| M3 | MEDIUM | `action_save_results` weak input validation | Two near-duplicate handlers |
| M4 | MEDIUM | `state.json` mixes engine/UI state | Medium-term architectural change |
| L1 | LOW | No structured logging | `logging.getLogger(__name__)` throughout |
| L2 | LOW | `requirements.txt` no version pins | Pin flask, gunicorn, werkzeug |
| L3 | LOW | Lane count maximum inconsistency | Define `MAX_LANES = 8` in config |
| L5 | LOW | No `.env.example` | Document all env vars |
| L6 | LOW | `?token=` in OBS URLs appears in access logs | Disappears when beta gate removed |
| L7 | LOW | OPTIONS preflight bypasses beta gate | Acceptable — OPTIONS returns no data |

---

## Deploy Checklist

```bash
# From local
rsync -av --exclude='*.pyc' --exclude='__pycache__' \
  /Users/tonyfarrell/Desktop/files/ root@strongpass.live:/opt/strongpass/current/

# On server
ssh root@strongpass.live
systemctl restart strongpass.service
systemctl status strongpass.service

# Verify
curl -s 'https://strongpass.live/health'
# → {"db":"ok","restart_token":...,"status":"ok"}
```

---

## Architecture State (post Phase 2)

```
                        SSE Event Fires
                              │
              ┌───────────────┼───────────────────────────────┐
              │               │                               │
        Overlay screens  control.html               comp_* templates
        (sse-client.js)  _debouncedSync()            (inline SSE)
              │               │                               │
              │         syncFromCompEngine()          location.reload()
              │         → checks _syncHashes          only on relevant
              │         → skips DOM rebuild            version change
              │           if data unchanged
              │
        Server: _get_sse_payload(version)
               └── first call: load_state() + json.dumps() → cache
               └── subsequent: return cached string
```

---

## Next Priorities

1. **H1 CSRF** — Flask-WTF `CSRFProtect` — affects all `<form method="post">` routes. Highest remaining security risk.
2. **H4 DB connections** — Request-scoped connection via Flask `g`. Prerequisite for scaling past 1 worker.
3. **M3 save_results** — Extract `_save_heat_results()` helper, consistent NaN/range validation.
4. **L2 requirements.txt** — Pin versions before next production incident.
5. **L1 Logging** — Add `logging.getLogger()` to server.py and app/*.py.
