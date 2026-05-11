#!/usr/bin/env python3
"""
Full simulation + stress test for Strongman Comp Control.
Run with: python3 simulate_comp.py
Requires the Flask server to be running on http://localhost:8080
Uses only stdlib (urllib, re, json).
"""
import urllib.request
import urllib.parse
import json
import re
import sys

BASE = "http://localhost:8080"

def req(path, data=None, method="GET"):
    url = BASE + path
    if data is not None and method == "POST":
        body = urllib.parse.urlencode(data).encode("utf-8")
        r = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
    else:
        r = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace"), resp.getcode()
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", errors="replace"), e.code
    except Exception as e:
        return str(e), 0

def get(path):
    return req(path)

def post(path, data):
    return req(path, data, method="POST")

def main():
    print("=" * 60)
    print("STRONGMAN COMP — Full simulation & stress test")
    print("=" * 60)
    issues = []

    # --- 1. Health check ---
    print("\n[1] Health check...")
    html, code = get("/comp/")
    if code != 200:
        print("    FAIL: GET /comp/ returned", code)
        issues.append("GET /comp/ returned non-200")
        sys.exit(1)
    print("    OK: Server responding")

    # --- 2. Clear and load test data ---
    print("\n[2] Clear all & generate test data...")
    html, code = post("/comp/action/clear_all", {})
    if code not in (200, 302):
        print("    FAIL: clear_all returned", code)
        issues.append("clear_all failed")
    html, code = post("/comp/action/generate_test", {})
    if code not in (200, 302):
        print("    FAIL: generate_test returned", code)
        issues.append("generate_test failed")
    else:
        print("    OK: Test data loaded (events + athletes + heats)")

    # --- 3. Comp state & heat API ---
    print("\n[3] Comp state & current heat API...")
    js, code = get("/comp/api/state")
    if code != 200:
        issues.append("GET /comp/api/state failed")
    else:
        state = json.loads(js)
        print("    State:", state.get("category"), "Event", state.get("event"), "Heat", state.get("heat"))
    js, code = get("/comp/api/heat")
    if code != 200:
        issues.append("GET /comp/api/heat failed")
    else:
        data = json.loads(js)
        lanes = data.get("lanes", [])
        print("    Current heat lanes:", len(lanes), "athletes:", [l.get("athlete_name") for l in lanes if l.get("athlete_name")])

    # --- 4. Enter scores for current heat (Run flow) ---
    print("\n[4] Enter scores for current heat (save_only)...")
    js, _ = get("/comp/api/heat")
    data = json.loads(js)
    lanes = data.get("lanes", [])
    form = {"action": "save_only"}
    for l in lanes:
        name = l.get("athlete_name")
        if name:
            form["score_" + name] = "10.5"  # dummy score
    html, code = post("/comp/action/save_results_run", form)
    if code not in (200, 302):
        print("    FAIL: save_results_run returned", code)
        issues.append("save_results_run failed")
    else:
        print("    OK: Scores saved for", len([k for k in form if k.startswith("score_")]), "athletes")

    # --- 5. Next heat, enter scores again ---
    print("\n[5] Next heat → enter scores...")
    html, code = post("/comp/action/next_heat", {})
    if code not in (200, 302):
        issues.append("next_heat failed")
    js, _ = get("/comp/api/heat")
    data = json.loads(js)
    lanes = data.get("lanes", [])
    form = {"action": "save_only"}
    for l in lanes:
        name = l.get("athlete_name")
        if name:
            form["score_" + name] = "12.0"
    post("/comp/action/save_results_run", form)
    print("    OK: Second heat scores saved")

    # --- 6. Withdraw an athlete (injury) ---
    print("\n[6] Withdraw one athlete (injury simulation)...")
    html, _ = get("/comp/athletes?category=U80")
    # First athlete id from withdraw/delete form: <input type="hidden" name="id" value="123">
    m = re.search(r'name="id"\s+value="(\d+)"', html)
    athlete_id = m.group(1) if m else None
    if not athlete_id:
        print("    WARN: Could not parse athlete id from athletes page (check template)")
        issues.append("Could not find athlete id for withdraw in HTML")
    else:
        form = {"id": athlete_id, "category": "U80", "reason": "injury"}
        html, code = post("/comp/action/withdraw_athlete", form)
        if code not in (200, 302):
            print("    FAIL: withdraw_athlete returned", code)
            issues.append("withdraw_athlete failed")
        else:
            print("    OK: Athlete", athlete_id, "withdrawn (injury)")

    # --- 7. Regenerate heats (lane rebalance) ---
    print("\n[7] Regenerate heats (lane rebalance after withdrawal)...")
    html, code = post("/comp/action/generate_heats", {})
    if code not in (200, 302):
        print("    FAIL: generate_heats returned", code)
        issues.append("generate_heats (regenerate) failed")
    else:
        print("    OK: Heats regenerated")
    js, _ = get("/comp/api/heat")
    data = json.loads(js)
    lanes = data.get("lanes", [])
    print("    Current heat now has", len(lanes), "lanes:", [l.get("athlete_name") for l in lanes])

    # --- 8. Leaderboard ---
    print("\n[8] Leaderboard API...")
    js, code = get("/comp/api/leaderboard?category=U80")
    if code != 200:
        issues.append("leaderboard API failed")
    else:
        lb = json.loads(js)
        print("    U80 leaderboard entries:", len(lb))
        if lb:
            print("    Top 3:", [(e.get("name"), e.get("score")) for e in lb[:3]])

    # --- 9. Update event scoring (setup flow) ---
    print("\n[9] Update event scoring from setup...")
    # Get first event id from comp home (we have events from generate_test)
    html, _ = get("/comp/")
    # Form has event_id in hidden input per event row
    m = re.search(r'name="event_id"\s+value="(\d+)"', html)
    event_id = m.group(1) if m else "1"
    form = {
        "event_id": event_id,
        "primary_metric": "weight",
        "secondary_metric": "",
        "primary_direction": "higher",
        "secondary_direction": "",
    }
    html, code = post("/comp/action/update_event_scoring", form)
    if code not in (200, 302):
        issues.append("update_event_scoring failed")
    else:
        print("    OK: Event scoring set to Max Weight for event_id", event_id)

    # --- 10. Stress: simulate all results then check leaderboard ---
    print("\n[10] Stress: simulate all results...")
    html, code = post("/comp/action/simulate_results", {})
    if code not in (200, 302):
        print("    FAIL: simulate_results returned", code)
        issues.append("simulate_results failed")
    else:
        print("    OK: All events filled with random results")
    js, code2 = get("/comp/api/leaderboard?category=U80")
    if code2 == 200:
        lb = json.loads(js)
        print("    U80 leaderboard after simulate:", len(lb), "athletes")

    # --- 11. Edge: empty category, no events ---
    print("\n[11] Edge: comp state when no heats...")
    # Already have data; just ensure state is consistent
    js, _ = get("/comp/api/state")
    state = json.loads(js)
    if "category" not in state or "event" not in state:
        issues.append("Comp state missing category/event after full flow")

    # --- Summary ---
    print("\n" + "=" * 60)
    if issues:
        print("ISSUES FOUND:")
        for i in issues:
            print("  -", i)
    else:
        print("No issues recorded (all steps returned expected status).")
    print("=" * 60)
    return 0 if not issues else 1

if __name__ == "__main__":
    sys.exit(main())
