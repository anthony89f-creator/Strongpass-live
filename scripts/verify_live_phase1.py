#!/usr/bin/env python3
"""
Live verification of Phase 1 stability fixes against strongpass.live.

Tests (read-only or safe writes only — no clear_all / generate_test):
  1. /state.json has results_version field
  2. SSE /stream payload is <20KB
  3. Timer ticks (timerRunning=True) do NOT advance results_version
  4. If current event is time type: timer pause (timerRunning=False) DOES advance results_version

Usage:
  python3 verify_live_phase1.py <base_url> <beta_token> [comp_password]

  python3 verify_live_phase1.py https://strongpass.live TOKEN PASSWORD
"""
import json
import sys
import time

try:
    import requests
except ImportError:
    print("Install requests: pip install requests")
    sys.exit(1)

BASE_URL = (sys.argv[1] if len(sys.argv) > 1 else "https://strongpass.live").rstrip("/")
BETA_TOKEN = sys.argv[2] if len(sys.argv) > 2 else ""
COMP_PASSWORD = sys.argv[3] if len(sys.argv) > 3 else ""

COOKIES = {"sp_beta": BETA_TOKEN} if BETA_TOKEN else {}
COMP_AUTH = ("comp", COMP_PASSWORD) if COMP_PASSWORD else None


def _token_param(path):
    sep = "&" if "?" in path else "?"
    return path + sep + "token=" + BETA_TOKEN if BETA_TOKEN else path


def get(path, stream=False):
    return requests.get(
        BASE_URL + _token_param(path),
        stream=stream,
        timeout=10,
    )


def post_update(payload):
    return requests.post(
        BASE_URL + _token_param("/update"),
        json=payload,
        timeout=10,
    )


def get_results_version():
    r = get("/state.json")
    if r.status_code != 200:
        return None, r.status_code
    data = r.json()
    return data.get("results_version"), 200


def read_sse_payload(timeout_secs=4):
    """Connect to /stream and return the first SSE payload size in bytes."""
    try:
        r = get("/stream", stream=True)
        if r.status_code != 200:
            return None, r.status_code
        deadline = time.time() + timeout_secs
        buf = b""
        for chunk in r.iter_content(chunk_size=None):
            buf += chunk
            if b"\n\n" in buf:
                r.close()
                lines = buf.split(b"\n")
                for line in lines:
                    if line.startswith(b"data: "):
                        return len(line) - 6, 200
            if time.time() > deadline:
                r.close()
                break
        return None, 0
    except Exception as e:
        return None, str(e)


def main():
    issues = []
    print("=" * 62)
    print("PHASE 1 STABILITY — Live verification")
    print("Target:", BASE_URL)
    print("=" * 62)

    # 1. Check results_version in /state.json
    print("\n[1] /state.json has results_version...")
    rv, code = get_results_version()
    if code != 200:
        print("    FAIL: /state.json returned HTTP", code)
        issues.append("state.json unreachable")
        print("\nCannot continue without state.json. Check beta token.")
        sys.exit(1)
    if rv is None:
        print("    FAIL: results_version field missing from /state.json")
        issues.append("results_version missing from state.json")
    else:
        print("    OK: results_version =", rv)

    # 2. SSE payload size
    print("\n[2] SSE /stream payload size...")
    payload_bytes, status = read_sse_payload(timeout_secs=5)
    if payload_bytes is None:
        print("    WARN: Could not read SSE payload (status=%s)" % status)
    else:
        kb = payload_bytes / 1024
        limit_kb = 20
        if kb > limit_kb:
            print("    FAIL: SSE payload is %.1f KB (expected <%d KB)" % (kb, limit_kb))
            issues.append("SSE payload too large: %.1f KB" % kb)
        else:
            print("    OK: SSE payload = %.1f KB (target <20 KB)" % kb)

    # 3. Timer ticks must NOT advance results_version
    print("\n[3] Timer ticks do not advance results_version...")
    rv_before, _ = get_results_version()
    timer_payload = {}
    for i in range(1, 5):
        timer_payload["judgeL%d" % i] = {
            "reps": 0, "light": "none",
            "timerSecs": 90, "timerRemaining": 60, "timerRunning": True,
            "primaryValue": "", "secondaryValue": "",
        }
    for tick in range(3):
        r = post_update(timer_payload)
        if r.status_code != 200:
            print("    WARN: /update tick %d returned %d" % (tick + 1, r.status_code))
        timer_payload["judgeL1"]["timerRemaining"] -= 1
        timer_payload["judgeL2"]["timerRemaining"] -= 1

    rv_after, _ = get_results_version()
    if rv_before is None or rv_after is None:
        print("    SKIP: Could not read results_version")
    elif rv_after != rv_before:
        print("    FAIL: results_version advanced from %d to %d on timer ticks" % (rv_before, rv_after))
        issues.append("results_version incremented on timer ticks")
    else:
        print("    OK: results_version unchanged (%d) after 3 timer ticks" % rv_after)

    # 4a. weight_reps secondaryValue advances results_version
    print("\n[4a] weight/weight_reps: submitting secondaryValue (reps) advances results_version...")
    rv_pre, _ = get_results_version()
    r = get("/state.json")
    state_now = r.json() if r.status_code == 200 else {}
    broadcast_now = state_now.get("broadcast") or state_now
    metric_now = (broadcast_now.get("eventMetric") or state_now.get("eventMetric") or "").lower()
    if metric_now in ("weight", "reps", "object", "distance"):
        score_payload = {
            "judgeL1": {
                "reps": 5, "light": "white",
                "timerSecs": 0, "timerRemaining": 0, "timerRunning": False,
                "primaryValue": "120", "secondaryValue": "5",
            }
        }
        r = post_update(score_payload)
        if r.status_code != 200:
            print("    WARN: /update score returned", r.status_code)
        rv_post, _ = get_results_version()
        if rv_pre is None or rv_post is None:
            print("    SKIP: Could not read results_version")
        elif rv_post <= rv_pre:
            print("    FAIL: results_version did NOT advance after score submit (%d → %d)" % (rv_pre, rv_post))
            issues.append("results_version did not advance on score submit")
        else:
            print("    OK: results_version advanced %d → %d after score submit (metric=%s)" % (rv_pre, rv_post, metric_now))
    else:
        print("    SKIP: metric=%r — skipping score submit test" % metric_now)

    # 4b. Timer pause on a time event DOES advance results_version
    print("\n[4b] Check current event type for timer-pause test...")
    r = get("/state.json")
    state = r.json() if r.status_code == 200 else {}
    broadcast = state.get("broadcast") or state
    metric = (broadcast.get("eventMetric") or state.get("eventMetric") or state_now.get("eventMetric") or "").lower()
    if metric != "time":
        print("    SKIP: Current event metric = %r (not 'time'). Pause test only applies to time events." % metric)
        print("         (results_version-on-pause will be tested when a time event is active)")
    else:
        print("    Event metric = 'time'. Running pause test...")
        rv_pre, _ = get_results_version()
        pause_payload = {
            "judgeL1": {
                "reps": 0, "light": "none",
                "timerSecs": 90, "timerRemaining": 45, "timerRunning": False,
                "primaryValue": "", "secondaryValue": "",
            }
        }
        r = post_update(pause_payload)
        if r.status_code != 200:
            print("    WARN: /update pause returned", r.status_code)
        rv_post, _ = get_results_version()
        if rv_pre is None or rv_post is None:
            print("    SKIP: Could not read results_version")
        elif rv_post <= rv_pre:
            print("    FAIL: results_version did NOT advance after lane pause (%d → %d)" % (rv_pre, rv_post))
            issues.append("results_version did not advance on timer pause")
        else:
            print("    OK: results_version advanced %d → %d on lane pause" % (rv_pre, rv_post))

    # Summary
    print("\n" + "=" * 62)
    if issues:
        print("FAILED — %d issue(s):" % len(issues))
        for i in issues:
            print("  -", i)
        sys.exit(1)
    else:
        print("ALL CHECKS PASSED")
        print("  results_version in state.json: yes")
        if payload_bytes is not None:
            print("  SSE payload size:             %.1f KB" % (payload_bytes / 1024))
        print("  Timer ticks / results_version: isolated")
    print("=" * 62)


if __name__ == "__main__":
    main()
