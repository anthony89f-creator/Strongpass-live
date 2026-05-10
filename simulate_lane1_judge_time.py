#!/usr/bin/env python3
"""
Simulate being a judge on Lane 1 for a TIME event: master starts global timer,
lane 1 pauses to capture athlete time, master ticks again (must not overwrite lane 1),
then we verify the time is in raw_results and state shows lane 1 stopped.

Run: python simulate_lane1_judge_time.py [base_url]
     python simulate_lane1_judge_time.py --self-test  (in-process, no server)
"""
import json
import os
import sqlite3
import sys

DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(DIR, "state.json")
DB_FILE = os.path.join(DIR, "comp.db")

USE_SELF_TEST = "--self-test" in sys.argv
if USE_SELF_TEST:
    sys.argv = [a for a in sys.argv if a != "--self-test"]
else:
    try:
        import requests
    except ImportError:
        print("Install requests: pip install requests")
        sys.exit(1)

BASE_URL = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080").rstrip("/")


def set_comp_state_in_db(category, event, heat):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("DELETE FROM competition_state")
    cur.execute("INSERT INTO competition_state(category,event,heat) VALUES(?,?,?)", (category, event, heat))
    con.commit()
    con.close()


def get_comp_state_from_db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT category, event, heat FROM competition_state LIMIT 1").fetchone()
    con.close()
    if row:
        return dict(row)
    return {"category": "U80", "event": 1, "heat": 1}


def get_heat_lanes_from_db(category, heat):
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT lane, athlete_name FROM heats WHERE category=? AND heat_number=? ORDER BY lane",
        (category, heat),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_raw_results_from_db(event_id, athlete_name):
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT rr.raw_value FROM raw_results rr JOIN athletes a ON a.id=rr.athlete_id "
        "WHERE rr.event_id=? AND a.name=?",
        (event_id, athlete_name),
    ).fetchone()
    con.close()
    return float(row["raw_value"]) if row and row["raw_value"] is not None else None


def get_event_from_db(event_number):
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT id, name, primary_metric FROM events WHERE event_number=?",
        (event_number,),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def _post(path, json_data=None, client=None):
    if USE_SELF_TEST and client is not None:
        return client.post(path, json=json_data or {})
    return requests.post(BASE_URL + path, json=json_data or {}, timeout=10)


def _get(path, client=None):
    if USE_SELF_TEST and client is not None:
        return client.get(path)
    return requests.get(BASE_URL + path, timeout=10)


def load_state(client=None):
    if USE_SELF_TEST and client is not None:
        r = client.get("/state.json")
        if r.status_code != 200:
            return {}
        return r.get_json()
    try:
        r = requests.get(BASE_URL + "/state.json", timeout=5)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def main():
    issues = []
    print("=" * 60)
    print("SIMULATION: Lane 1 judge entering TIME (pause + submit)")
    print("=" * 60)
    if USE_SELF_TEST:
        sys.path.insert(0, DIR)
        from server import app
        client = app.test_client()
    else:
        client = None
        print("Base URL:", BASE_URL)

    if not os.path.exists(DB_FILE) and not USE_SELF_TEST:
        print("ERROR: comp.db not found. Run server and Generate test first.")
        sys.exit(1)

    # 1. Load test data and set broadcast mode
    print("\n[1] Load test data, set broadcast mode = engine...")
    if USE_SELF_TEST:
        client.post("/comp/action/clear_all")
        client.post("/comp/action/generate_test")
        client.post("/comp/action/set_broadcast_mode", data={"mode": "engine"})
    else:
        requests.post(BASE_URL + "/comp/action/clear_all", allow_redirects=False, timeout=10)
        requests.post(BASE_URL + "/comp/action/generate_test", allow_redirects=False, timeout=10)
        requests.post(BASE_URL + "/comp/action/set_broadcast_mode", data={"mode": "engine"}, allow_redirects=False, timeout=10)
    print("    OK")

    # 2. Set comp to TIME event (e.g. event 2 = Frame Carry), heat 1
    category = "U80"
    event_num = 2  # time event
    heat = 1
    set_comp_state_in_db(category, event_num, heat)
    lanes = get_heat_lanes_from_db(category, heat)
    if not lanes:
        print("    FAIL: No heat lanes")
        issues.append("No heat lanes")
        sys.exit(1)
    lane1_athlete = lanes[0]["athlete_name"]
    ev = get_event_from_db(event_num)
    if not ev or (ev.get("primary_metric") or "").lower() != "time":
        print("    WARN: Event 2 may not be time; continuing anyway")
    print("    Comp: %s event %s heat %s (time). Lane 1 = %s" % (category, event_num, heat, lane1_athlete))

    # 3. Trigger sync so state has eventMetric=time and default judge state
    print("\n[2] Trigger sync (POST /update empty) so state has eventMetric and judgeL1...")
    r = _post("/update", json_data={}, client=client)
    if r.status_code != 200:
        print("    FAIL: POST /update returned", r.status_code)
        issues.append("update failed")
    state = load_state(client) if USE_SELF_TEST else None
    if not USE_SELF_TEST:
        try:
            state = requests.get(BASE_URL + "/state.json", timeout=5).json()
        except Exception:
            state = {}
    broadcast = state.get("broadcast") or state
    metric = broadcast.get("eventMetric") or state.get("eventMetric")
    if metric != "time":
        print("    WARN: eventMetric = %r (expected 'time')" % metric)
    print("    OK: eventMetric = %r" % (metric or "(none)"))

    # 4. Simulate MASTER: start global timer (all lanes running, 90s)
    timer_secs = 90
    timer_remaining = 85  # after one "tick"
    print("\n[3] Simulate MASTER: push all lanes RUNNING (timerSecs=%s, timerRemaining=%s)..." % (timer_secs, timer_remaining))
    master_payload = {}
    for i in range(1, 5):
        master_payload["judgeL" + str(i)] = {
            "reps": 0, "light": "none",
            "timerSecs": timer_secs, "timerRemaining": timer_remaining, "timerRunning": True,
            "primaryValue": "", "secondaryValue": "",
        }
    r = _post("/update", json_data=master_payload, client=client)
    if r.status_code != 200:
        print("    FAIL: master push returned", r.status_code)
        issues.append("master push failed")
    else:
        print("    OK: Master pushed all lanes running")

    # 5. Simulate LANE 1 JUDGE: pause to capture time (45s remaining = 45s elapsed)
    lane1_pause_remaining = 45
    print("\n[4] Simulate LANE 1 JUDGE: Pause timer (timerRemaining=%s => elapsed %s s)..." % (lane1_pause_remaining, timer_secs - lane1_pause_remaining))
    lane1_payload = {
        "judgeL1": {
            "reps": 0, "light": "none",
            "timerSecs": timer_secs, "timerRemaining": lane1_pause_remaining, "timerRunning": False,
            "primaryValue": "", "secondaryValue": "",
        }
    }
    r = _post("/update", json_data=lane1_payload, client=client)
    if r.status_code != 200:
        print("    FAIL: lane 1 pause returned", r.status_code)
        issues.append("lane 1 pause failed")
    else:
        print("    OK: Lane 1 judge sent Pause")

    # 6. Simulate MASTER ticking again (tries to push all running) — server must KEEP lane 1 stopped
    print("\n[5] Simulate MASTER: tick again (push all RUNNING)...")
    master_payload2 = {}
    for i in range(1, 5):
        master_payload2["judgeL" + str(i)] = {
            "reps": 0, "light": "none",
            "timerSecs": timer_secs, "timerRemaining": timer_remaining - 1, "timerRunning": True,
            "primaryValue": "", "secondaryValue": "",
        }
    r = _post("/update", json_data=master_payload2, client=client)
    if r.status_code != 200:
        print("    FAIL: master tick 2 returned", r.status_code)
        issues.append("master tick 2 failed")

    # 7. Verify state: judgeL1 must still be STOPPED with timerRemaining=45
    print("\n[6] Verify state: judgeL1 must still be STOPPED (timerRemaining=45)...")
    if not USE_SELF_TEST:
        state = requests.get(BASE_URL + "/state.json", timeout=5).json()
    else:
        state = client.get("/state.json").get_json()
    j1 = state.get("judgeL1") or {}
    running = j1.get("timerRunning")
    rem = j1.get("timerRemaining")
    if running is True:
        print("    FAIL: judgeL1.timerRunning is True (master overwrote lane 1 pause)")
        issues.append("Lane 1 was overwritten by master")
    elif rem != lane1_pause_remaining:
        print("    FAIL: judgeL1.timerRemaining = %s (expected %s)" % (rem, lane1_pause_remaining))
        issues.append("Lane 1 timerRemaining wrong")
    else:
        print("    OK: judgeL1.timerRunning=%s, timerRemaining=%s" % (running, rem))

    # 8. Verify raw_results: lane 1 athlete must have raw_value = elapsed = 90 - 45 = 45
    print("\n[7] Verify raw_results: Lane 1 athlete time = 45.0 s (timerSecs - timerRemaining)...")
    event_id = ev["id"] if ev else None
    raw_val = get_raw_results_from_db(event_id, lane1_athlete) if event_id else None
    expected = float(timer_secs - lane1_pause_remaining)  # 45.0
    if raw_val is None:
        print("    FAIL: No raw_result for %s (judge time was not written)" % lane1_athlete)
        issues.append("raw_results missing lane 1 time")
    elif abs(raw_val - expected) > 0.01:
        print("    FAIL: raw_value = %s (expected %s)" % (raw_val, expected))
        issues.append("raw_results wrong value")
    else:
        print("    OK: %s -> raw_value = %s" % (lane1_athlete, raw_val))

    # Summary
    print("\n" + "=" * 60)
    if issues:
        print("ISSUES:")
        for i in issues:
            print("  -", i)
        sys.exit(1)
    print("SUCCESS: Lane 1 judge can enter time (pause); master does not overwrite; score in raw_results.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
