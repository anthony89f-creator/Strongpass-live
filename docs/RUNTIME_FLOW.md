# Strongpass Live — Runtime Flow Reference

## 1. Director Boot Sequence

```
page load
  │
  ├─ BUILD_ID = 'BUILD-20260518-C'
  ├─ document.title = 'Director [BUILD-20260518-C]'
  ├─ console.log [Director BOOT] (visible immediately)
  │
  ├─ init()
  │   ├─ Build 6 .ovl-btn elements + click handlers
  │   ├─ Create pvw-dbg-hud element (press D to show)
  │   ├─ Attach ResizeObserver to pvwBody → fires applyScale() on resize
  │   ├─ setTimeout(applyScale, 80)  ← initial layout measurement
  │   ├─ fetchState() → GET /state.json → onStateUpdate(d) → liveState populated
  │   ├─ connect() → EventSource('/stream') → SSE open
  │   └─ setInterval(watchdog, 2000)
  │
  └─ idle — waiting for operator input or SSE ticks
```

---

## 2. Overlay Button Click → PVW Handshake

### Full sequence for a new overlay (different from currently loaded)

```
operator clicks "Lineup" button
  │
  ├─ _activeTransitionId++ → id = N
  ├─ _enqueue(selectPVW_queued)   [DL() only — behind ?debug=1]
  │
  ▼
selectPVW('lineup', N)   [runs when queue is available]
  │
  ├─ [PVW#N] selectPVW ENTER key=lineup
  ├─ pvwSelection = 'lineup'
  ├─ ovl = { key:'lineup', src:'/lineup.html', group:'A', pvwFlags:['lineup'] }
  ├─ _pvwKey !== 'lineup' → new overlay → _createPVWFrame(ovl, N)
  │
  ▼
_createPVWFrame(ovl, N)
  │
  ├─ _destroyPVWFrame()    [src=about:blank + remove old iframe]
  ├─ create <iframe id="pvw-frame-lineup" class="ovl-frame pvw-frame">
  ├─ append to #pvw-screen
  ├─ _pvwKey = 'lineup'
  │
  ├─ install window.addEventListener('message', onMessage)
  ├─ start cancelPoll (setInterval 50ms — aborts if id !== _activeTransitionId)
  ├─ start timeout (setTimeout 5000ms — failsafe)
  │
  ├─ [PVW#N] src-set:lineup → frame.src = '/lineup.html?pvw=1'
  │
  │   [iframe loads lineup.html]
  │   ├─ window.DISABLE_SSE = true   (set before sse-client.js runs)
  │   ├─ window.PVW_MODE = true
  │   ├─ state listener attached: window.addEventListener('message', ...)
  │   ├─ window.onStateUpdate defined
  │   └─ window.parent.postMessage({ type:'overlay-runtime-ready' }, '*')
  │
  ├─ [PVW#N] runtime-ready received — pushing initial state
  ├─ frame.contentWindow.postMessage(pvwStateFor(ovl), '*')
  ├─ [PVW#N] initial state pushed
  │
  │   [inside iframe]
  │   ├─ onStateUpdate(d) called
  │   ├─ render() → showCategory(lineupIndex)
  │   └─ requestAnimationFrame(() => postMessage({type:'overlay-ready'}))
  │
  ├─ [PVW#N] overlay-ready received — statePushed=true
  ├─ settle('overlay-ready') → cleanup → resolve()
  │
  ▼
selectPVW continues
  │
  ├─ id === _activeTransitionId → not stale
  ├─ [PVW#N] iframe-ready:lineup
  ├─ applyScale()   [scale iframe to fit monitor box]
  ├─ renderOvlButtons()
  ├─ updateTakeBtn()
  ├─ renderCtrlPanel()  [build lineup category buttons]
  ├─ [PVW#N] selectPVW COMPLETE pvwSelection=lineup
  │
  └─ ongoing: pushToPVW(ovl) called on every SSE tick
```

### Same overlay re-clicked (already in PVW)

```
click "Lineup" again (already selected)
  │
  ├─ _pvwKey === 'lineup' → push only
  ├─ pushToPVW(ovl)  [postMessage current state]
  └─ selectPVW COMPLETE immediately (no iframe reload)
```

---

## 3. TAKE → Program

```
operator clicks TAKE (or presses Enter/Space)
  │
  ├─ take() → _enqueue(_doTake)
  │
  ▼
_doTake()
  │
  ├─ Guard: _taking=false AND pvwSelection='lineup' → proceed
  ├─ _taking = true
  ├─ take-btn.disabled = true
  ├─ start 8s safety timeout (resets _taking if route hangs)
  │
  ├─ POST /broadcast/take {"scene":"lineup"}
  │
  │   [server]
  │   ├─ Clear same-group peers (leaderboard, h2h flags → false)
  │   ├─ s['lineup'] = true
  │   ├─ program.groups['A'] = 'lineup'
  │   ├─ program.scene = 'lineup'
  │   ├─ save_state(s)
  │   └─ _sse_notify() → all SSE clients get new state
  │
  ├─ response 200 {"ok":true, "scene":"lineup", "takenAt":...}
  │
  ├─ Optimistic local update (before SSE echo arrives):
  │   ├─ liveState.leaderboard = false
  │   ├─ liveState.h2h = false
  │   ├─ liveState.lineup = true
  │   └─ liveState.program.groups.A = 'lineup'
  │
  ├─ _createPGMGroupFrame(ovl)
  │   ├─ _destroyPGMGroup('A')  [remove old Group A PGM iframe if any]
  │   ├─ create <iframe id="pgm-frame-group-A" class="ovl-frame pgm-frame">
  │   ├─ frame.src = '/lineup.html'  [NO ?pvw=1 — SSE mode]
  │   │   [inside iframe: sse-client.js connects, fetches /state.json, calls onStateUpdate]
  │   └─ _pgmFrames.A = 'lineup'
  │
  ├─ applyScale()
  ├─ renderPGMStatus() → pgm-monitor shows "ON AIR · Lineup"
  ├─ renderOvlButtons() → "lineup" button gets .live class
  ├─ showStatus('Taken live — Lineup', true)
  │
  └─ finally: _taking=false, take-btn.disabled=false
```

---

## 4. CUT (Group Clean)

```
operator clicks CUT (or presses Escape)
  │
  ├─ goClean() → _enqueue(_doClean)
  │
  ▼
_doClean()
  │
  ├─ pvwSelection = 'lineup' → ovl.group = 'A'
  │
  ├─ POST /broadcast/clean_group {"group":"A"}
  │
  │   [server]
  │   ├─ flags = ['leaderboard','h2h','lineup']
  │   ├─ s[flag] = False  for all Group A flags
  │   ├─ program.groups.A removed
  │   ├─ program.scene = null  (if scene was in group A)
  │   ├─ save_state(s)
  │   └─ _sse_notify()
  │
  ├─ response 200 {"ok":true, "group":"A", "cleanAt":...}
  │
  ├─ Optimistic local update:
  │   ├─ liveState.lineup = false  (and leaderboard, h2h)
  │   └─ liveState.program.groups.A deleted
  │
  ├─ _destroyPGMGroup('A')  [src=about:blank + remove iframe]
  ├─ renderPGMStatus() → "Clean" (if no other groups live)
  ├─ renderOvlButtons() → lineup button loses .live class
  └─ showStatus('Group A clean — Lineup off', true)
```

Note: Groups B and C are unaffected. A lower third (Group B) or rep counter (Group C) can remain live during a Group A CUT.

---

## 5. SSE State Propagation

```
Any state change (heat advance, score update, TAKE, CUT, etc.)
  │
  ├─ server writes to state.json (under state_lock)
  ├─ _sse_version += 1
  └─ _sse_condition.notify_all()

All connected SSE clients (director + all PGM overlay iframes):
  │
  ├─ _sse_condition.wait() wakes up
  ├─ yield "data: {json}\n\n"  (results blob omitted — ~126KB)
  │
  ▼
director.html (onStateUpdate):
  ├─ Object.assign(liveState, d)
  ├─ renderCtxStrip()        update competition context strip
  ├─ renderPGMStatus()       update on-air indicator
  ├─ renderPGMVisibility()   ensure PGM iframes visible
  ├─ renderOvlButtons()      update .live and .selected classes
  ├─ renderCtrlPanel()       dirty-check; rebuild only if relevant fields changed
  ├─ pushToPVW(ovl)          forward state to active PVW iframe via postMessage
  └─ _reconstructPGMFrames() create/destroy PGM iframes per group if state changed

PGM overlay iframe (e.g. lineup.html, SSE mode):
  ├─ sse-client.js receives event
  └─ window.onStateUpdate(d) → render() → DOM update
```

---

## 6. Ctrl Panel Action → Immediate PVW Update

When a ctrl panel control is changed (e.g. lineup category button):

```
ctrlLineupIdx(1)  [operator clicks "U90" category button]
  │
  ├─ postUpdate({lineupIndex: 1})
  │   ├─ POST /update {"lineupIndex":1}
  │   ├─ server: saves lineupIndex=1 to state.json, calls _sse_notify()
  │   ├─ response 200
  │   ├─ Object.assign(liveState, {lineupIndex:1})
  │   ├─ _pushCurrentPVW()
  │   │   └─ postMessage(pvwStateFor(lineup ovl))  [includes lbStandings]
  │   │       [inside lineup PVW iframe]
  │   │       ├─ onStateUpdate(d)
  │   │       ├─ d.lineupIndex=1 !== prevIndex=2 → showCategory(1)
  │   │       ├─ getCategoryData() → uses state.lbStandings → returns all 6 categories
  │   │       ├─ cats[1] = {name:'U90', athletes:[...20...]}
  │   │       └─ #disp-name.textContent = 'U90'
  │   │
  │   ├─ renderCtrlPanel()    dirty-check rebuilds btn[1] as .active
  │   ├─ renderOvlButtons()
  │   └─ (SSE echo arrives ~50-200ms later — no-op because liveState already matches)
```

---

## 7. Scaling Flow

```
trigger: ResizeObserver fires (pvwBody dimension changed)
  OR window resize (debounced 40ms)
  OR applyScale() called directly from TAKE/CUT/selectPVW

applyScale()
  │
  ├─ w = pvwBody.offsetWidth   (e.g. 590px at 1280px viewport)
  ├─ scale = 590 / 1920 = 0.3073
  │
  ├─ for each .ovl-frame (PVW + all PGM):
  │   ├─ el.style.transform = 'scale(0.3073)'
  │   └─ el.style.transformOrigin = 'top left'
  │
  └─ _dbgUpdateHUD()   (if debug HUD is visible)

Expected result:
  1920px CSS iframe visually appears as 590px  ✓
  contentDocument.body has NO transform applied ✓
  Content is readable at correct scale           ✓
```

---

## 8. Overlay Boot Sequence (PVW Mode)

The boot order inside any overlay HTML loaded with `?pvw=1`:

```
overlay.html loads
  │
  1. Global <script> at top (if any): sets DISABLE_SSE guard
  │
  2. PVW bootstrap block (near top of <script>):
  │   ├─ window.PVW_MODE = true
  │   ├─ window.DISABLE_SSE = true
  │   └─ install window.addEventListener('message', ...)
  │       [handler calls onStateUpdate when message arrives]
  │
  3. Overlay application code:
  │   ├─ state = {}  (initial empty state)
  │   ├─ render functions defined
  │   └─ window.onStateUpdate = function(d) { ... }
  │
  4. sse-client.js runs: sees DISABLE_SSE=true → returns immediately
  │
  5. Two-phase handshake trigger (very last line of <script>):
  │   if (window.PVW_MODE) {
  │     window.parent.postMessage({ type: 'overlay-runtime-ready' }, '*');
  │   }
  │
  [director receives overlay-runtime-ready]
  ├─ postMessage(pvwStateFor(ovl))
  │
  [overlay receives state message]
  ├─ onStateUpdate(d) called
  ├─ render() called  → DOM updated
  └─ requestAnimationFrame(() => {
       window.parent.postMessage({ type: 'overlay-ready' }, '*');
     })

[director receives overlay-ready → handshake complete]
```

---

## 9. Multi-Overlay PGM Compositor

Up to three PGM overlays can be live simultaneously:

```
pgm-screen  (position:relative, overflow:hidden)
  │
  ├─ pgm-frame-group-A  (z-index:10)  ← leaderboard OR h2h OR lineup
  ├─ pgm-frame-group-B  (z-index:20)  ← lowerthird OR champion
  └─ pgm-frame-group-C  (z-index:30)  ← reps (+ lights)
```

All frames are `position:absolute`, `width:1920px`, `height:1080px`, `background:transparent`. Group A overlays have opaque backgrounds. Groups B and C have transparent backgrounds so they composite over Group A.

OBS browser sources are pointed at the individual overlay URLs (not the director). Each overlay manages its own SSE connection and renders directly from server state.
