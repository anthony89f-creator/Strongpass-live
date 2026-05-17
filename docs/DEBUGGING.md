# Strongpass Live — Debugging Guide

## Console Instrumentation Strategy

The director is heavily instrumented. Every significant action writes to the browser console. Understanding the log prefixes is essential for diagnosing issues.

### Log Prefixes

| Prefix | Source | Meaning |
|---|---|---|
| `[Director BOOT]` | director.html init | Page loaded, BUILD_ID confirmed |
| `[Director +Nms]` | `DL()` function | Timestamped director operation log |
| `[CLICK] key id=#N` | click handler | Operator clicked overlay button |
| `[QUEUE] accepted seq=N` | `_enqueue()` | Transition queued |
| `[QUEUE] running seq=N` | `_enqueue()` | Transition executing |
| `[PVW#N] selectPVW ENTER` | `selectPVW()` | New overlay transition started |
| `[PVW#N] _createPVWFrame BEGIN` | `_createPVWFrame()` | iframe being created |
| `[PVW#N] runtime-ready received` | message handler | Overlay sent `overlay-runtime-ready` |
| `[PVW#N] initial state pushed` | message handler | State sent to overlay |
| `[PVW#N] overlay-ready received` | message handler | Overlay rendered |
| `[PVW#N] selectPVW COMPLETE` | `selectPVW()` | Full handshake done |
| `[SCALE]` | `applyScale()` | iframe scale calculation |
| `[WATCHDOG]` | setInterval 2s | Periodic health dump |
| `[leaderboard]` / etc. | overlay iframe | Logs from inside the iframe |

### Watchdog Output (every 2 seconds)

```
[WATCHDOG] {
  build: 'BUILD-20260517-C',
  stage: '[PVW#3] COMPLETE:lineup',
  taking: false,
  pvw: 'lineup',
  scene: null,
  buttons: 6,
  takeBtnDisabled: false,
  pvwFrames: 1,
  pgmFrames: 0,
  pgmGroups: '{}',
  sseState: 1   // 0=connecting, 1=open, 2=closed
}
```

The watchdog warns if:
- `taking: true` persists (possible deadlock — 8s safety timeout auto-resets it)
- `buttons` ≠ 6 (DOM corruption)
- `pgmFrames` > 3 (iframe leak)
- Stage stuck at `src-set:*` or `iframe-load:*` (possible handshake deadlock)

---

## Diagnosing PVW Issues

### Overlay not appearing in preview

Follow the handshake trace in console:

1. `[CLICK] key` — did the click register?
2. `[PVW#N] selectPVW ENTER key=...` — is selectPVW running?
3. `[PVW#N] runtime-ready received` — did the overlay boot and send its signal?
   - If missing: overlay HTML may not have loaded (check Network tab for 404)
4. `[PVW#N] initial state pushed` — did director send state?
5. `[PVW#N] overlay-ready received` — did overlay render?
   - If missing after 5s: `TIMEOUT 5s` appears; check for errors from inside the iframe
6. `[PVW#N] selectPVW COMPLETE` — handshake done

If COMPLETE appears but display is blank: scale issue (see below).

### Scale debugging

Press `D` in director to toggle the scale debug HUD. It shows:
- `pvwBody.offsetWidth` — must be > 0. If 0, the container has collapsed (CSS layout issue).
- Computed scale (`W / 1920`)
- Per-iframe transform and bounding rect

To check scale from DevTools console:
```javascript
const f = document.querySelector('.pvw-frame');
const m = getComputedStyle(f).transform.match(/matrix\(([^,]+)/);
console.log('host scale:', m ? parseFloat(m[1]).toFixed(4) : 'none');
// Expected: ~0.306 for a 590px-wide monitor box
```

### Checking for double-scaling

Symptom: Content appears tiny (~9%) in top-left corner.

```javascript
const f = document.querySelector('.pvw-frame');
// Host transform:
const m = getComputedStyle(f).transform.match(/matrix\(([^,]+)/);
const host = m ? parseFloat(m[1]) : null;
console.log('host:', host);

// Body transform inside iframe:
try {
  const body = f.contentDocument.body;
  const bm = getComputedStyle(body).transform.match(/matrix\(([^,]+)/);
  const body_s = bm ? parseFloat(bm[1]) : null;
  console.log('body:', body_s);
  console.log('double-scaled?', body_s && Math.abs(body_s - host * host) < 0.001);
} catch(e) { console.log('cross-origin — cannot inspect body'); }
```

Expected: `host ≈ 0.306`, `body ≈ null or 1.0`, `double-scaled? false`.

If double-scaled: `_applyScaleToBody()` is being called from inside `applyScale()`. Remove that call.

---

## Diagnosing TAKE / CUT Failures

### TAKE returns 401

Session not authenticated. Re-authenticate:
1. `https://strongpass.live/beta/login` → enter beta password
2. `https://strongpass.live/comp/login` → enter comp password
3. Return to `/director.html`

### TAKE returns 400 `"unknown scene: ..."`

The `scene` key sent in the POST body doesn't match any key in `_OVERLAY_GROUPS`. Verify the overlay key in `OVERLAYS` array in director.html matches the key in `_OVERLAY_GROUPS` in server.py.

### CUT returns 405

`POST /broadcast/clean_group` route is missing from server.py. Verify:
```bash
ssh root@strongpass.live "grep -n 'clean_group' /opt/strongpass/current/server.py"
```
If missing: deploy the current server.py and reload.

### CUT returns 400 `"unknown group: ..."`

The group passed to `/broadcast/clean_group` is not `"A"`, `"B"`, or `"C"`. Check `ovl.group` in the `OVERLAYS` array.

---

## Diagnosing Lineup Category Selector

### `#disp-name` always shows the same category

**Step 1:** Check `/update` returns 200:
- Open DevTools Network → click category button → look for `POST /update` → check status

**Step 2:** Check `liveState.lineupIndex` updates:
```javascript
window.liveState.lineupIndex  // should change after each click
```

**Step 3:** Check how many categories `getCategoryData()` sees:
```javascript
const f = document.getElementById('pvw-frame-lineup');
const cats = f.contentWindow.getCategoryData();
console.log(cats.map(c => c.name + ':' + c.count));
// Expected: ['U80:20','U90:20','U105:20','U120:20','Womens:20','Mens Open:20']
// Problem: ['U80:20']  ← only one category, so lineupIndex % 1 = 0 always
```

**Step 4:** Check `lbStandings` is in PVW state:
```javascript
const f = document.getElementById('pvw-frame-lineup');
const lb = f.contentWindow.state?.lbStandings;
console.log('lbStandings:', lb ? Object.keys(lb) : 'MISSING');
// Expected: ['U80','U90','U105','U120','Womens','Mens Open','all']
// If MISSING: pvwStateFor() is deleting it — check BUILD-20260517-C is deployed
```

---

## Diagnosing SSE Issues

### Director shows "Disconnected"

Auto-reconnection is active. If stuck:
```bash
ssh root@strongpass.live "systemctl is-active strongpass"
ssh root@strongpass.live "journalctl -u strongpass -n 30 --no-pager"
```

If `SSE active=24+` appears in logs: approaching thread limit. Check for open OBS scenes not being closed (each holds a gthread). Thread pool: 32.

### State not updating in PGM overlay

Inside the overlay's DevTools:
- Look for `[PGM] live SSE active` — SSE is connected
- If SSE is active but no updates: check `window.onStateUpdate` is defined in the overlay
- Check Network tab for the `/stream` response — should be `text/event-stream`

### PVW state not updating after ctrl action

Check director console for `pushToPVW lineup — sent OK`. If not appearing: `_pushCurrentPVW()` was not called. Check `postUpdate()` in director.html — it calls `_pushCurrentPVW()` after a 200 response.

---

## Common Recovery Procedures

### Director frozen — buttons not responding

1. Check watchdog: `taking: true` for >10s = stuck lock. The 8s safety timeout should reset it automatically. If not, hard-refresh: `Cmd+Shift+R`.
2. Check queue: look for `[QUEUE] fn rejected` — a previous queue slot error may have corrupted the Promise chain. Hard-refresh to reset the queue.

### PVW blank after overlay switch

1. Check timeline in console — did all 4 handshake steps log correctly?
2. If TIMEOUT: overlay crashed during render. Check for JS errors from inside the iframe.
3. If scale is wrong: press `D` to see current scale values.

### Ghost PGM iframe after CLEAN

```javascript
// Force-remove all PGM frames:
document.querySelectorAll('.pgm-frame').forEach(f => { f.src='about:blank'; f.remove(); });
```
Then hard-refresh.

### OBS shows 502 / blank after server reload

Someone ran `systemctl restart`. In OBS: right-click each browser source → Refresh.

---

## Playwright Automation (Production Tests)

Located at `/tmp/*.mjs` on the developer's Mac. Run with:
```bash
cd /tmp && node lineup_prod_verify.mjs
```

### Auth pattern for Playwright tests

```javascript
import { chromium } from 'playwright';

const BASE    = 'https://strongpass.live';
const BETA_PW = '...';  // from env
const COMP_PW = '...';  // from env

const browser = await chromium.launch({ headless: false });
const ctx     = await browser.newContext({ ignoreHTTPSErrors: true });
const page    = await ctx.newPage();

// Step 1: Beta gate — POST form with 'token' field
await page.request.post(BASE + '/beta/login', {
  form: { token: BETA_PW },
  ignoreHTTPSErrors: true,
});

// Step 2: Comp login — navigate and fill password form
await page.goto(BASE + '/comp/login', { waitUntil: 'domcontentloaded' });
await page.locator('input[name=password],input[type=password]').first().fill(COMP_PW);
await page.locator('button[type=submit],input[type=submit]').first().click();
await page.waitForTimeout(800);

// Verify auth worked
const check = await page.evaluate(async () => {
  const r = await fetch('/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
    credentials: 'include',
  });
  return r.status;
});
// should be 200
```

**Important:** Use `page.request.post()` (not `page.goto()`) for the beta login. The beta endpoint expects a form POST with `Content-Type: application/form-urlencoded`. `page.request.post()` with `form:` handles this correctly and shares cookies with the page context.
