# Backing up your competition data

Your competition lives in two files:

- **`state.json`** — broadcast state, judge data, competition config (lanes, etc.), category start counts.
- **`comp.db`** — athletes, events, heats, raw results, competition state (current category/event/heat).

---

## Option 1: One-click backup (recommended)

1. Open **Competition** → **Setup**.
2. Click **💾 Backup Now**.
3. A copy is saved in the **`backups/`** folder next to the server, with a timestamp:
   - `backup_2026-03-08_14-30-00_state.json`
   - `backup_2026-03-08_14-30-00_comp.db`

**When to use:** Before the event, after each category, or after entering a batch of results.

---

## Option 2: Manual copy

1. **Stop the server** (so files aren’t being written).
2. Copy both files somewhere safe (USB, cloud, another folder):
   - `state.json`
   - `comp.db`
3. Name them with the date or round, e.g. `comp_after_U80.db`, `state_saturday.json`.
4. Start the server again.

---

## Option 3: Backup script (terminal)

From the project folder (where `server.py` lives):

```bash
# Create backups folder and copy with timestamp
mkdir -p backups
cp state.json "backups/state_$(date +%Y-%m-%d_%H-%M).json"
cp comp.db "backups/comp_$(date +%Y-%m-%d_%H-%M).db"
```

You can run this in a second terminal during the comp (the server can stay running; the copy is a snapshot).

---

## Restoring from a backup

1. **Stop the server.**
2. Replace the live files with your backup:
   - Copy `backup_YYYY-MM-DD_HH-MM-SS_state.json` → `state.json`
   - Copy `backup_YYYY-MM-DD_HH-MM-SS_comp.db` → `comp.db`
3. Start the server again.
4. Refresh the competition and broadcast pages.

**Note:** Restoring overwrites the current `state.json` and `comp.db`. If you want to keep the current run, copy the current files somewhere else before restoring.

---

## Suggested schedule for a big comp

- **Before the event:** One-click backup (or manual) after setup and heat generation.
- **After each category (or every 2–3 events):** Click **Backup Now**.
- **End of day:** Extra manual copy to USB or cloud.

The `backups/` folder can get large over time; delete old backups when you no longer need them.
