#!/usr/bin/env python3
"""
Test that judge panel submissions (POST /update with judgeL1, judgeL2, ...)
are written to raw_results and appear on the Run Competition page.

Run with the Flask server already running (e.g. python server.py).
Usage: python test_judge_sync.py [base_url]
Default base_url: http://127.0.0.1:5000
"""
import json
import os
import sqlite3
import sys

try:
    import requests
except ImportError:
    print("Install requests: pip install requests")
    sys.exit(1)

DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(DIR, "comp.db")
BASE_URL = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000").rstrip("/")

def main():
    print("Testing judge score -> raw_results -> Run Comp flow")
    print("Base URL:", BASE_URL)

    # 1. Get current competition state from DB (what Run Comp would show)
    if not os.path.exists(DB_FILE):
        print("ERROR: comp.db not found at", DB_FILE)
        print("Start the server and ensure you have a competition with events/athletes/heats.")
        sys.exit(1)
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT category, event, heat FROM competition_state LIMIT 1").fetchone()
    if not row:
        print("ERROR: No competition_state in DB. Open Run Comp once to set category/event/heat.")
        con.close()
        sys.exit(1)
    category, event, heat = row["category"], row["event"], row["heat"]
    print("  Current state: category=%r, event=%s, heat=%s" % (category, event, heat))

    # 2. Get event_id and heat lanes
    ev = con.execute("SELECT id FROM events WHERE event_number=?", (event,)).fetchone()
    if not ev:
        print("ERROR: No event with event_number=%s" % event)
        con.close()
        sys.exit(1)
    event_id = ev["id"]
    lanes = con.execute(
        "SELECT lane, athlete_name FROM heats WHERE category=? AND heat_number=? ORDER BY lane",
        (category, heat)
    ).fetchall()
    if not lanes:
        print("ERROR: No heats for category=%r heat=%s" % (category, heat))
        con.close()
        sys.exit(1)
    print("  Lanes:", [r["athlete_name"] for r in lanes])

    # 3. POST judge score for lane 1 — payload adapts to the current event type
    ev_type = con.execute("SELECT event_type, primary_metric FROM events WHERE event_number=?", (event,)).fetchone()
    primary_metric = (ev_type["primary_metric"] or "reps").lower() if ev_type else "reps"
    test_value = 99.0
    judge_data = {"light": "none", "timerRunning": False,
                  "timerRemaining": 60, "timerSecs": 60,
                  "reps": 0, "primaryValue": "", "secondaryValue": ""}
    if primary_metric == "reps":
        judge_data["reps"] = int(test_value)
    elif primary_metric == "time":
        judge_data["timerRemaining"] = 21   # elapsed = 60-21 = 39 s
        test_value = 39.0
    else:
        judge_data["primaryValue"] = str(test_value)  # weight / distance / objects
    payload = {"judgeL1": judge_data}
    r = requests.post(BASE_URL + "/update", json=payload, timeout=5)
    if r.status_code != 200:
        print("ERROR: POST /update returned", r.status_code, r.text)
        con.close()
        sys.exit(1)
    print("  POST /update (judgeL1 reps=%s): 200 OK" % test_value)

    # 4. Check raw_results for lane 1 athlete
    athlete_name = lanes[0]["athlete_name"]
    ath = con.execute("SELECT id FROM athletes WHERE name=?", (athlete_name,)).fetchone()
    if not ath:
        print("ERROR: Athlete %r not in athletes table" % athlete_name)
        con.close()
        sys.exit(1)
    athlete_id = ath["id"]
    raw = con.execute(
        "SELECT raw_value, tiebreak FROM raw_results WHERE athlete_id=? AND event_id=?",
        (athlete_id, event_id)
    ).fetchone()
    con.close()

    if not raw:
        print("FAIL: No row in raw_results for athlete_id=%s event_id=%s (judge score was not written)" % (athlete_id, event_id))
        sys.exit(1)
    got = float(raw["raw_value"]) if raw["raw_value"] is not None else None
    if got != test_value:
        print("FAIL: raw_results has raw_value=%r, expected %s" % (got, test_value))
        sys.exit(1)
    print("  raw_results: athlete %r event_id=%s raw_value=%s (OK)" % (athlete_name, event_id, got))
    print("")
    print("SUCCESS: Judge score is in the database. Refresh the Run Competition page to see it.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
