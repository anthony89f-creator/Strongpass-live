# StrongPass Deployment Checklist

**Server:** `root@strongpass.live` (Hetzner Ubuntu 24.04)  
**App root:** `/opt/strongpass/current/`  
**Service:** `strongpass.service` (systemd)  
**Deploy method:** rsync + `systemctl reload strongpass` (NEVER restart)

---

## Standard Deploy

```bash
# 1. Sync files — never sync comp.db or state.json (live data)
rsync -az \
  --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='venv' --exclude='comp.db' --exclude='state.json' \
  --exclude='.git' \
  /Users/tonyfarrell/Desktop/files/ root@strongpass.live:/opt/strongpass/current/

# 2. Syntax check on server before reloading
ssh root@strongpass.live \
  "python3 -c 'import ast; ast.parse(open(\"/opt/strongpass/current/server.py\").read()); print(\"OK\")'"

# 3. Zero-downtime reload (Gunicorn ExecReload — no 502 gap)
ssh root@strongpass.live "systemctl reload strongpass"

# 4. Confirm active
ssh root@strongpass.live "systemctl is-active strongpass"
```

---

## Post-Deploy Verification

```bash
TOKEN=a5QOTUZFWySEIPXYEBGndu0E

# Health check — verify db=ok, sse_active reasonable, restart_token changed
curl -s "https://strongpass.live/health?token=$TOKEN" | python3 -m json.tool

# SSE stream — verify it connects and sends data
curl -N -s "https://strongpass.live/stream?token=$TOKEN" | head -5

# Broadcast state — verify program field present
curl -s "https://strongpass.live/broadcast/state?token=$TOKEN" | python3 -m json.tool

# Overlay URLs — verify each serves with 200
for f in leaderboard h2h lineup lowerthird reps champion; do
  CODE=$(curl -o /dev/null -sw "%{http_code}" "https://strongpass.live/${f}.html?token=$TOKEN")
  echo "$f.html: $CODE"
done
```

---

## TAKE/CUT Verification (requires browser — HTTPS cookie auth)

1. Open `https://strongpass.live/comp/login` — log in as operator
2. Open `https://strongpass.live/director.html` — load director
3. Click **Leaderboard** → confirm PVW renders actual overlay
4. Click **TAKE** → confirm PGM shows leaderboard, OBS confirms live
5. Click **CUT** → confirm PGM goes to CLEAN
6. Repeat TAKE/CUT 5× — no freeze, no double connection
7. Reload director.html mid-session → confirm on-air state reconstructed
8. Check Network tab → director should show exactly 2 EventSource connections

---

## Environment Variables

Set in `/etc/systemd/system/strongpass.service`:

| Variable | Purpose |
|---|---|
| `BETA_TOKEN` | Site-wide beta gate — pass as `?token=` in OBS URLs |
| `COMP_PASSWORD` | Operator session auth — used at `/comp/login` |
| `SECRET_KEY` | Flask session signing — leave unset to derive from COMP_PASSWORD |

---

## Service Management

```bash
# Check status
systemctl status strongpass

# Live logs
journalctl -u strongpass -f

# Zero-downtime deploy (USE THIS)
systemctl reload strongpass

# Full restart — causes ~3s gap OBS caches as 502 (AVOID during live events)
systemctl restart strongpass

# After editing service file
systemctl daemon-reload && systemctl reload strongpass
```

---

## SSE Connection Budget

| Surface | Connections |
|---|---|
| Director (`director.html`) | 1 |
| Active PGM iframe | 1 |
| OBS browser sources (6 overlays) | 6 |
| `control.html` | 1 |
| Public screens, call room | ~2 |
| **Total typical** | **~11** |
| **Thread limit (Gunicorn)** | **32** |
| **Warning threshold** | **24** |

PVW iframes in director load with `?pvw=1` — they do NOT open SSE connections.  
Overlay buttons in director are lazy-created on first click.

---

## Key Routes

| Route | Auth | Purpose |
|---|---|---|
| `/health` | beta | Uptime + SSE count + program scene |
| `/broadcast/state` | beta | Current on-air overlay + all flags |
| `/broadcast/take` | beta + comp | Atomically take overlay to program |
| `/broadcast/clean` | beta + comp | Clear all overlays from program |
| `/stream` | beta | SSE state stream |
| `/state.json` | beta | Full state including results |
| `/director/lb` | beta + comp | Set leaderboard category override |
| `/comp/login` | — | Operator session login |

---

## Rollback

```bash
# If a deploy breaks something, the previous server.py is not versioned on server.
# Keep a local git history — rollback is: git revert HEAD + re-deploy.

# To restore previous state.json from backup:
ssh root@strongpass.live "ls /opt/strongpass/current/backups/ | tail -5"
ssh root@strongpass.live "cp /opt/strongpass/current/backups/state_YYYYMMDD.json /opt/strongpass/current/state.json"
ssh root@strongpass.live "systemctl reload strongpass"
```

---

## Failure Patterns

| Symptom | Cause | Fix |
|---|---|---|
| OBS shows 502 | Used `systemctl restart` (not `reload`) | Always use `reload` |
| Director freezes after TAKE | Stale code (old `refreshPVW` pattern) | Re-deploy latest director.html |
| 13 SSE connections on load | Old director.html with eager iframe creation | Re-deploy latest director.html |
| `categories: []` in state | Frontend clobbering via `/update` | Verify `_UPDATE_EXCLUDE` includes `"categories"` |
| PVW iframe opens SSE | Overlay missing `?pvw=1` block | Re-deploy overlay HTML files |
| TAKE returns 401 | Not logged in to comp session | Log in at `/comp/login` first |
| TAKE returns `{"error":"unknown scene"}` | Invalid scene key in request | Valid keys: leaderboard, h2h, lineup, lowerthird, champion, reps, lights |
