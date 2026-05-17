# Strongpass Live — Deployment Guide

## Production Server

| Item | Value |
|---|---|
| Host | `strongpass.live` |
| SSH user | `root` |
| App directory | `/opt/strongpass/current/` |
| Service name | `strongpass` (systemd) |
| Gunicorn port | `8000` (nginx proxies :443 → :8000) |
| Python | System Python 3 with Flask, gunicorn |

---

## Pre-Deployment Checklist

1. Test locally if possible: `python server.py` or start gunicorn locally
2. Bump `BUILD_ID` in `director.html` so you can verify the right file is served:
   ```javascript
   var BUILD_ID = 'BUILD-YYYYMMDD-X';  // e.g. BUILD-20260517-D
   ```
3. Confirm the service is currently running: `ssh root@strongpass.live "systemctl is-active strongpass"`

---

## Deploy: HTML/JS Files Only

Changes to overlay files or director only — no server restart needed:

```bash
# Single file
scp ~/Desktop/files/director.html root@strongpass.live:/opt/strongpass/current/

# Multiple files
scp ~/Desktop/files/{director.html,lineup.html,leaderboard.html} \
    root@strongpass.live:/opt/strongpass/current/

# Reload (zero-downtime — NEVER use restart)
ssh root@strongpass.live "systemctl reload strongpass"
```

Nginx serves HTML/JS files as static files directly without touching gunicorn. You do not even need to reload for HTML-only changes — nginx picks them up immediately. Reload is required only when `server.py` changes.

---

## Deploy: server.py Changes

Python changes require a gunicorn reload:

```bash
scp ~/Desktop/files/server.py root@strongpass.live:/opt/strongpass/current/
ssh root@strongpass.live "systemctl reload strongpass"
```

**CRITICAL: `systemctl reload`, not `systemctl restart`.**

- `reload` sends SIGHUP → gunicorn gracefully replaces workers (zero-downtime)
- `restart` terminates all connections → nginx returns 502 for ~3s → OBS browser sources cache the error and show a broken stream until manually refreshed

---

## Deploy: All Files at Once

```bash
scp ~/Desktop/files/{director.html,lineup.html,leaderboard.html,h2h.html,champion.html,lowerthird.html,reps.html,server.py,sse-client.js} \
    root@strongpass.live:/opt/strongpass/current/
ssh root@strongpass.live "systemctl reload strongpass"
```

---

## Verify Deployment

```bash
# Check service is active
ssh root@strongpass.live "systemctl is-active strongpass"

# Check gunicorn is listening
ssh root@strongpass.live "ss -tlnp | grep 8000"

# Check recent logs
ssh root@strongpass.live "journalctl -u strongpass -n 50 --no-pager"
```

Then in the browser:
1. Open `https://strongpass.live/director.html`
2. Hard-refresh: Cmd+Shift+R (macOS) or Ctrl+Shift+R
3. Check title bar: `Director [BUILD-YYYYMMDD-X]` — must match what you deployed
4. Open DevTools Console — first log line: `[Director BOOT] BUILD_ID=BUILD-YYYYMMDD-X`
5. Click an overlay → verify handshake: `overlay-ready confirmed — two-phase handshake complete BUILD_ID=...`

---

## Git Workflow

```bash
# Stage and commit source files (never commit comp.db, state.json, __pycache__)
git -C ~/Desktop/files add director.html lineup.html leaderboard.html h2h.html \
    champion.html lowerthird.html reps.html server.py sse-client.js .gitignore

git -C ~/Desktop/files commit -m "Description of changes"

# Push to GitHub
git -C ~/Desktop/files push origin main

# Tag a stable release
git -C ~/Desktop/files tag v2-runtime-stable
git -C ~/Desktop/files push origin v2-runtime-stable
```

### Files to NEVER commit

- `comp.db` — SQLite database (gitignored)
- `state.json` — runtime state (gitignored)
- `__pycache__/` — compiled Python (gitignored)
- `.env` files or any file containing credentials

---

## GitHub Repository Setup

The remote was configured on 2026-05-17:

```bash
git remote add origin https://github.com/anthony89f-creator/Strongpass-live.git
git branch -M main
git push -u origin main
```

GitHub HTTPS auth requires a personal access token. If auth fails:
- Use `gh auth login` (GitHub CLI) to authenticate, then retry the push
- Or set a token in the remote URL temporarily:
  ```bash
  git remote set-url origin https://TOKEN@github.com/anthony89f-creator/Strongpass-live.git
  ```

---

## Rollback Procedure

```bash
# Restore files from the stable tag
git checkout v2-runtime-stable

# Deploy all source files
scp ~/Desktop/files/{director.html,lineup.html,leaderboard.html,h2h.html,champion.html,lowerthird.html,reps.html,server.py,sse-client.js} \
    root@strongpass.live:/opt/strongpass/current/

# Reload
ssh root@strongpass.live "systemctl reload strongpass"
```

---

## Emergency Server Recovery

If the service is down:

```bash
# Check status
ssh root@strongpass.live "systemctl status strongpass"

# View last 100 log lines
ssh root@strongpass.live "journalctl -u strongpass -n 100 --no-pager"

# Restart (only if reload fails — will cause ~3s OBS interruption)
ssh root@strongpass.live "systemctl restart strongpass"

# If service fails to start: check Python syntax
ssh root@strongpass.live "cd /opt/strongpass/current && python -c 'import server'"
```

If state.json is corrupted:

```bash
# Reset to safe defaults (removes all overlay flags)
ssh root@strongpass.live "cd /opt/strongpass/current && python -c \"
import json
with open('state.json') as f: s = json.load(f)
for k in ['leaderboard','h2h','lineup','lowerThirdVisible','champion','repsVisible','lightsVisible']:
    s[k] = False
s['program'] = {'scene': None, 'groups': {}, 'takenAt': None, 'cleanAt': None}
with open('state.json', 'w') as f: json.dump(s, f)
print('Reset complete')
\""
ssh root@strongpass.live "systemctl reload strongpass"
```

---

## Environment Variables

The server reads these env vars at startup (set in the systemd unit file or `/etc/environment`):

| Var | Purpose |
|---|---|
| `BETA_TOKEN` (or similar) | Beta gate password — compared with HMAC |
| `COMP_PASSWORD` (or similar) | Competition director password |
| `SECRET_KEY` | Flask session secret — changing this invalidates all sessions |
| `PROXY_ALLOWLIST` | Comma-separated domains allowed through `/proxy` endpoint |

Check the systemd unit file for exact var names:
```bash
ssh root@strongpass.live "cat /etc/systemd/system/strongpass.service"
```
