import math
from app.config import EVENT_TYPES


def balanced_heats(athletes, lanes):
    """
    Balance heats so the difference between heats is at most one athlete.
    Avoids single-athlete heats. Assign athletes sequentially, preserving order.
    Example: 17 athletes, 4 lanes → Heat1: 3, Heat2: 3, Heat3: 3, Heat4: 4, Heat5: 4.
    """
    n = len(athletes)
    if n == 0:
        return []
    n_heats = math.ceil(n / lanes)
    base = n // n_heats
    remainder = n % n_heats
    heats = []
    idx = 0
    for i in range(n_heats):
        size = base + (1 if i >= n_heats - remainder else 0)
        heats.append(athletes[idx:idx + size])
        idx += size
    return heats


def _event_direction_flags(event_row, event_type):
    """Derive primary_higher, has_secondary, secondary_higher from event row or EVENT_TYPES."""
    if event_row and event_row.get("primary_direction"):
        primary_higher = (event_row["primary_direction"] == "higher")
    else:
        primary_higher = EVENT_TYPES.get(event_type, {"higher": True})["higher"]
    if event_row and event_row.get("secondary_direction"):
        secondary_higher = (event_row["secondary_direction"] == "higher")
    else:
        secondary_higher = False
    has_secondary = bool(event_row and event_row.get("secondary_metric"))
    return primary_higher, has_secondary, secondary_higher


def _event_type_from_scoring(primary_metric, secondary_metric):
    """Map primary/secondary metric to event_type so Run/Results pages show correct unit."""
    if primary_metric == "weight" and not secondary_metric:
        return "weight"
    if primary_metric == "reps":
        return "reps"
    if primary_metric == "distance" and not secondary_metric:
        return "distance"
    if primary_metric == "time":
        return "time"
    if primary_metric == "objects" and secondary_metric == "time":
        return "object"
    if primary_metric == "distance" and secondary_metric == "time":
        return "distance"
    return "reps"


def _safe_int(val, default, min_val=None, max_val=None):
    """Parse form/query int without raising; clamp to [min_val, max_val] if given."""
    try:
        n = int(val)
    except (TypeError, ValueError):
        return default
    if min_val is not None and n < min_val:
        return min_val
    if max_val is not None and n > max_val:
        return max_val
    return n


def _filter_payload_to_active_lanes(payload, lane_count):
    """Return a copy of payload with only judgeL1..judgeL{lane_count} (and other non-lane keys).
    Lanes beyond lane_count are ignored so they are not updated by master/lane pushes."""
    out = {}
    for k, v in payload.items():
        if k == "resetAllTimers":
            out[k] = v
        elif k.startswith("judgeL"):
            try:
                idx = int(k[6:])
                if 1 <= idx <= lane_count:
                    out[k] = v
            except (ValueError, TypeError):
                pass
        else:
            out[k] = v
    return out


def _default_inactive_judge():
    """Default judge state for inactive lanes (timer not running)."""
    return {
        "reps": 0, "light": "none", "timerRunning": False,
        "timerRemaining": 60, "timerSecs": 60,
        "primaryValue": "", "secondaryValue": "",
    }


def _protect_lane_stopped_timers(payload, current):
    """Prevent an incoming push from restarting a lane whose timer has already stopped.
    Modifies payload in place. No-op when resetAllTimers is True."""
    if payload.get("resetAllTimers") is True:
        return
    for i in range(1, 9):
        key = "judgeL" + str(i)
        inc = payload.get(key)
        cur = current.get(key)
        if not isinstance(inc, dict) or not isinstance(cur, dict):
            continue
        cur_stopped = cur.get("timerRunning") is False
        inc_running = inc.get("timerRunning") is True
        cur_secs = cur.get("timerSecs")
        cur_rem  = cur.get("timerRemaining")
        if cur_secs is not None and cur_rem is not None:
            try:
                if abs(float(cur_rem) - float(cur_secs)) < 0.01:
                    continue
            except (TypeError, ValueError):
                pass
        if cur_stopped and inc_running:
            payload[key] = dict(inc)
            payload[key]["timerRunning"] = False
            if "timerRemaining" in cur:
                payload[key]["timerRemaining"] = cur["timerRemaining"]
