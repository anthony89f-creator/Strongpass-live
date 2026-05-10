#!/usr/bin/env python3
"""
Test judge pages behaviour and roles for the competition.

Roles:
- Judge Master (judge-master.html): Controls global timer (Start All / Pause / Reset).
  Pushing timer state overwrites judgeL1..judgeL4 in state. Lane judges sync from state.
- Lane judges (judge.html?lane=N): Each controls only their lane (judgeLN).
  They can Stop/Pause their own timer and Submit to scoreboard. Their push must not
  be overwritten by the master for that lane (server protects lane-stopped state).
- Server (/update): Merges payload into state.json. Protects lanes that have
  timerRunning=false from being set back to true by a later master push.
- Run Competition: Sets broadcast.eventMetric / eventSecondaryMetric via
  sync_comp_to_broadcast so judge UIs show the correct scoring type (reps/time/weight/etc).
  Lane judge must not "jump to reps" when eventMetric is temporarily missing.

Run: python test_judge_pages.py
     python test_judge_pages.py --live http://127.0.0.1:8080  # against running server
"""
import json
import os
import sys

DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(DIR, "state.json")

USE_LIVE = "--live" in sys.argv
if USE_LIVE:
    sys.argv = [a for a in sys.argv if a != "--live"]
    try:
        import requests
    except ImportError:
        print("Install requests for --live: pip install requests")
        sys.exit(1)
BASE_URL = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080").rstrip("/")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


def test_protect_lane_stopped_timer():
    """Server must preserve a lane's stopped timer when master pushes running state."""
    print("\n[1] Protect lane-stopped timer (server logic)")
    sys.path.insert(0, DIR)
    from server import _protect_lane_stopped_timers, load_state as server_load_state
    from server import STATE_FILE as SERVER_STATE_FILE

    # Setup: lane 2 stopped (lane judge paused), lane 1 still running (so we only protect lane 2)
    current = server_load_state()
    current["judgeL1"] = {
        "reps": 0,
        "light": "none",
        "timerRunning": True,
        "timerRemaining": 50,
        "timerSecs": 60,
    }
    current["judgeL2"] = {
        "reps": 0,
        "light": "none",
        "timerRunning": False,
        "timerRemaining": 45,
        "timerSecs": 60,
        "primaryValue": "",
        "secondaryValue": "",
    }
    with open(SERVER_STATE_FILE, "w") as f:
        json.dump(current, f, indent=2)

    # Simulate master pushing all lanes running (pass current so protection uses same state)
    payload = {
        "judgeL1": {"timerRunning": True, "timerRemaining": 50, "timerSecs": 60},
        "judgeL2": {"timerRunning": True, "timerRemaining": 50, "timerSecs": 60},
        "judgeL3": {"timerRunning": True, "timerRemaining": 50, "timerSecs": 60},
        "judgeL4": {"timerRunning": True, "timerRemaining": 50, "timerSecs": 60},
    }
    _protect_lane_stopped_timers(payload, current)

    if payload["judgeL2"]["timerRunning"] is not False:
        raise AssertionError("Lane 2 must stay stopped, got timerRunning=%r" % payload["judgeL2"]["timerRunning"])
    if payload["judgeL2"]["timerRemaining"] != 45:
        raise AssertionError("Lane 2 must keep stopped time 45, got %r" % payload["judgeL2"]["timerRemaining"])
    if payload["judgeL1"]["timerRunning"] is not True:
        raise AssertionError("Lane 1 must still be running")
    print("    OK: Lane 2 stayed stopped after master push; L1/L3/L4 still running.")


def test_protect_via_update_route():
    """Full /update route: merge then read state and assert lane 2 still stopped."""
    print("\n[2] /update route preserves lane-stopped (integration)")
    from server import app, load_state as server_load_state

    current = server_load_state()
    current["judgeL2"] = {
        "reps": 0,
        "light": "none",
        "timerRunning": False,
        "timerRemaining": 33,
        "timerSecs": 60,
    }
    save_state(current)

    with app.test_client() as c:
        r = c.post(
            "/update",
            json={
                "judgeL1": {"timerRunning": True, "timerRemaining": 40, "timerSecs": 60},
                "judgeL2": {"timerRunning": True, "timerRemaining": 40, "timerSecs": 60},
                "judgeL3": {"timerRunning": True, "timerRemaining": 40, "timerSecs": 60},
                "judgeL4": {"timerRunning": True, "timerRemaining": 40, "timerSecs": 60},
            },
            content_type="application/json",
        )
    assert r.status_code == 200
    after = load_state()
    assert after.get("judgeL2", {}).get("timerRunning") is False
    assert after.get("judgeL2", {}).get("timerRemaining") == 33
    print("    OK: After POST /update, judgeL2 still stopped with timerRemaining=33.")


def test_broadcast_has_event_metric():
    """After sync_comp_to_broadcast, state must have broadcast.eventMetric for judge UI."""
    print("\n[3] Broadcast state has eventMetric for judge pages")
    from server import app, sync_comp_to_broadcast, get_comp_state, set_comp_state
    from server import CATEGORY_ORDER

    if not CATEGORY_ORDER:
        print("    SKIP: No categories (need test data).")
        return
    set_comp_state(CATEGORY_ORDER[0], 1, 1)
    sync_comp_to_broadcast()
    state = load_state()
    broadcast = state.get("broadcast") or {}
    metric = broadcast.get("eventMetric") or state.get("eventMetric")
    assert metric, "Lane judge needs eventMetric to show correct scoring mode (not default reps)"
    print("    OK: broadcast.eventMetric = %r" % metric)


def test_live_state_endpoint():
    """If running against live server, GET state.json and check structure."""
    if not USE_LIVE:
        return
    print("\n[4] Live: GET state.json structure")
    r = requests.get(BASE_URL + "/state.json", timeout=5)
    assert r.status_code == 200
    data = r.json()
    broadcast = data.get("broadcast") or data
    assert "lanes" in broadcast or "lanes" in data or "eventMetric" in broadcast or "eventMetric" in data
    print("    OK: state.json has broadcast/lanes/eventMetric.")


def main():
    print("=" * 60)
    print("JUDGE PAGES — roles and behaviour test")
    print("=" * 60)
    issues = []
    try:
        test_protect_lane_stopped_timer()
    except Exception as e:
        issues.append("Protect lane-stopped: " + str(e))
        print("    FAIL:", e)
    try:
        test_protect_via_update_route()
    except Exception as e:
        issues.append("Update route: " + str(e))
        print("    FAIL:", e)
    try:
        test_broadcast_has_event_metric()
    except Exception as e:
        issues.append("Broadcast eventMetric: " + str(e))
        print("    FAIL:", e)
    if USE_LIVE:
        try:
            test_live_state_endpoint()
        except Exception as e:
            issues.append("Live state: " + str(e))
            print("    FAIL:", e)

    print("\n" + "=" * 60)
    if issues:
        print("ISSUES:")
        for i in issues:
            print("  -", i)
        sys.exit(1)
    print("All judge flow checks passed.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
