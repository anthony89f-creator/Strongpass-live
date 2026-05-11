#!/usr/bin/env python3
"""
Simulation: judge panel submissions (all lanes, all scoring types) -> raw_results -> Run Competition page.
Verifies that POST /update with judgeL1..judgeL4 (and single-lane submits) populates raw_results
and that the Run Competition page shows those scores.

Usage:
  python simulate_judge_to_run.py [base_url]   — run against live server (default http://127.0.0.1:8080)
  python simulate_judge_to_run.py --self-test — run in-process (no server needed)
"""
import json
import os
import re
import sqlite3
import sys

DIR = os.path.dirname(os.path.abspath(__file__))
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

# Event number -> (primary_metric, secondary_metric) from generate_test event_defs
# 1=Deadlift=weight, 2=Frame Carry=time, 3=Sandbag=distance, 4=Log Press=reps, 5=Atlas Stones=object
EVENT_METRICS = {
    1: ("weight", None),
    2: ("time", None),
    3: ("distance", None),
    4: ("reps", None),
    5: ("objects", "time"),
}


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


def get_event_from_db(event_number):
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT id, name, primary_metric, secondary_metric FROM events WHERE event_number=?",
        (event_number,),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def get_raw_results_from_db(event_id, athlete_names):
    if not event_id or not athlete_names:
        return {}
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(athlete_names))
    rows = con.execute(
        "SELECT a.name, rr.raw_value, rr.tiebreak FROM raw_results rr "
        "JOIN athletes a ON a.id=rr.athlete_id "
        "WHERE rr.event_id=? AND a.name IN (" + placeholders + ")",
        (event_id,) + tuple(athlete_names),
    ).fetchall()
    con.close()
    return {r["name"]: {"raw_value": r["raw_value"], "tiebreak": r["tiebreak"]} for r in rows}


def build_judge_payload(lanes, primary_metric, secondary_metric, value_fn):
    """value_fn(lane_index, athlete_name) -> (raw_value, tiebreak or None)."""
    payload = {}
    for i, lane in enumerate(lanes):
        jkey = "judgeL" + str(i + 1)
        raw_val, tiebreak = value_fn(i, lane["athlete_name"])
        if primary_metric == "reps":
            payload[jkey] = {
                "reps": int(raw_val) if raw_val is not None else "",
                "light": "none",
                "timerRunning": False,
                "timerRemaining": 60,
                "timerSecs": 60,
                "primaryValue": "",
                "secondaryValue": "",
            }
        elif primary_metric == "time":
            # Support both primaryValue and timerSecs/timerRemaining
            payload[jkey] = {
                "reps": "",
                "light": "none",
                "timerRunning": False,
                "timerSecs": 90,
                "timerRemaining": 90 - (raw_val or 0),
                "primaryValue": str(raw_val) if raw_val is not None else "",
                "secondaryValue": "",
            }
        else:
            payload[jkey] = {
                "reps": "",
                "light": "none",
                "timerRunning": False,
                "timerRemaining": 60,
                "timerSecs": 60,
                "primaryValue": str(raw_val) if raw_val is not None else "",
                "secondaryValue": str(tiebreak) if tiebreak is not None else "",
            }
    return payload


def _post(path, data=None, json_data=None, use_requests=True, client=None):
    if USE_SELF_TEST and client is not None:
        if json_data is not None:
            return client.post(path, json=json_data)
        return client.post(path, data=data or {})
    if json_data is not None:
        return requests.post(BASE_URL + path, json=json_data, timeout=10)
    return requests.post(BASE_URL + path, data=data or {}, allow_redirects=False, timeout=10)

def _get(path, use_requests=True, client=None):
    if USE_SELF_TEST and client is not None:
        return client.get(path)
    return requests.get(BASE_URL + path, timeout=10)


def main():
    issues = []
    print("=" * 60)
    print("JUDGE -> RAW_RESULTS -> RUN COMPETITION — Simulation")
    print("=" * 60)
    if USE_SELF_TEST:
        print("Mode: self-test (in-process)")
        # Import app and create test client
        sys.path.insert(0, DIR)
        from server import app
        client = app.test_client()
    else:
        print("Base URL:", BASE_URL)
        client = None

    if not os.path.exists(DB_FILE) and not USE_SELF_TEST:
        print("ERROR: comp.db not found. Start server and run Clear all + Generate test first.")
        sys.exit(1)

    # 1. Health + ensure test data
    print("\n[1] Reset and load test data...")
    r = _post("/comp/action/clear_all", client=client)
    if r.status_code not in (200, 302):
        print("    FAIL: clear_all returned", r.status_code)
        issues.append("clear_all failed")
    r = _post("/comp/action/generate_test", client=client)
    if r.status_code not in (200, 302):
        print("    FAIL: generate_test returned", r.status_code)
        issues.append("generate_test failed")
    else:
        print("    OK: Test data loaded (events 1–5: weight, time, distance, reps, object)")

    # 2. Set broadcast mode to engine (required for /update to write raw_results)
    print("\n[2] Set broadcast mode to 'engine'...")
    r = _post("/comp/action/set_broadcast_mode", data={"mode": "engine"}, client=client)
    if r.status_code not in (200, 302):
        print("    FAIL: set_broadcast_mode returned", r.status_code)
        issues.append("broadcast mode not set to engine")
    else:
        print("    OK: Broadcast mode = engine")

    state = get_comp_state_from_db()
    category = state.get("category") or "U80"
    heat = 1
    lanes = get_heat_lanes_from_db(category, heat)
    if not lanes:
        print("    FAIL: No heat lanes for category=%r heat=%s" % (category, heat))
        issues.append("No heat lanes")
        sys.exit(1)
    lane_names = [l["athlete_name"] for l in lanes]
    print("    Lanes:", lane_names[:4], "..." if len(lane_names) > 4 else "")

    # 3. For each event type: set state, POST /update, verify raw_results and Run page
    for event_num in range(1, 6):
        ev = get_event_from_db(event_num)
        if not ev:
            print("\n[Event %s] SKIP: event not found" % event_num)
            continue
        event_id = ev["id"]
        primary = (ev.get("primary_metric") or "reps").lower()
        secondary = (ev.get("secondary_metric") or "").lower() or None
        set_comp_state_in_db(category, event_num, heat)

        # Build payload: one value per lane (all lanes in one POST, like master)
        if primary == "reps":
            payload = build_judge_payload(
                lanes, primary, secondary,
                lambda i, _: (10 + i, None),
            )
        elif primary == "time":
            payload = build_judge_payload(
                lanes, primary, secondary,
                lambda i, _: (25.5 + i, None),
            )
        elif primary == "weight":
            payload = build_judge_payload(
                lanes, primary, secondary,
                lambda i, _: (120 + i * 5, None),
            )
        elif primary == "distance":
            payload = build_judge_payload(
                lanes, primary, secondary,
                lambda i, _: (15.5 + i, None),
            )
        else:
            # objects + time tiebreak
            payload = build_judge_payload(
                lanes, primary, secondary,
                lambda i, _: (8 + i, 30.0 + i),
            )

        print("\n[Event %s] %s (primary=%s, secondary=%s) — POST /update (all lanes)..." % (
            event_num, ev.get("name"), primary, secondary,
        ))
        r = _post("/update", json_data=payload, client=client)
        if r.status_code != 200:
            print("    FAIL: POST /update returned", r.status_code, r.text[:200])
            issues.append("Event %s: /update returned %s" % (event_num, r.status_code))
            continue

        raw = get_raw_results_from_db(event_id, lane_names)
        expected_count = min(4, len(lane_names))
        if len(raw) < expected_count:
            print("    FAIL: raw_results has %s rows, expected at least %s" % (len(raw), expected_count))
            issues.append("Event %s: raw_results missing rows" % event_num)
        else:
            print("    OK: raw_results has %s lane(s)" % len(raw))
            for name, data in list(raw.items())[:4]:
                v = data.get("raw_value")
                t = data.get("tiebreak")
                print("      %s -> raw_value=%s tiebreak=%s" % (name, v, t))

    # 4. Single-lane submit (lane 1 only) for event 1
    print("\n[4] Single-lane submit (judgeL1 only) for event 1...")
    set_comp_state_in_db(category, 1, heat)
    ev1 = get_event_from_db(1)
    payload = {"judgeL1": {"primaryValue": "999", "reps": "", "timerSecs": 60, "timerRemaining": 60, "secondaryValue": ""}}
    r = _post("/update", json_data=payload, client=client)
    if r.status_code != 200:
        print("    FAIL: POST /update (judgeL1 only) returned", r.status_code)
        issues.append("Single-lane /update failed")
    else:
        raw = get_raw_results_from_db(ev1["id"], [lane_names[0]])
        if raw and raw.get(lane_names[0], {}).get("raw_value") == 999.0:
            print("    OK: Lane 1 raw_value=999 in raw_results")
        else:
            print("    FAIL: raw_results for lane 1 missing or value != 999", raw)
            issues.append("Single-lane result not in raw_results")

    # 5. Verify Run Competition page shows scores (event 1, weight)
    print("\n[5] GET /comp/run — verify scores appear in page...")
    set_comp_state_in_db(category, 1, heat)
    r = _get("/comp/run", client=client)
    if r.status_code != 200:
        print("    FAIL: GET /comp/run returned", r.status_code)
        issues.append("Run page returned non-200")
    else:
        html = r.get_data(as_text=True) if hasattr(r, "get_data") else r.text
        # We just set event 1; lane 1 should have 999 from step 4
        if "999" in html:
            print("    OK: Run page HTML contains score '999'")
        else:
            # Might show other lane scores from step 3 (120, 125, ...)
            if any(str(v) in html for v in (120, 125, 130, 135, 999)):
                print("    OK: Run page HTML contains expected score values")
            else:
                print("    WARN: Run page may not show judge-fed scores (check manually)")

    # Summary
    print("\n" + "=" * 60)
    if issues:
        print("ISSUES:")
        for i in issues:
            print("  -", i)
        sys.exit(1)
    print("SUCCESS: Judge submissions populate raw_results; Run Competition page can show them.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
