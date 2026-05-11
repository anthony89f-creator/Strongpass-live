#!/usr/bin/env python3
"""
STRONGMAN UNIFIED SERVER  —  port 8080
Broadcast HTML files are served RAW (send_from_directory, bypasses Jinja2).
Comp pages live in templates/ subfolder and are rendered with render_template.
"""

import json, os, shutil, sqlite3, math, urllib.request, urllib.parse, threading, base64, functools
from datetime import datetime
from flask import Flask, render_template, redirect, jsonify, request, send_from_directory, abort, Response

from app.config import (
    DIR, TPL_DIR, STATE_FILE, DB_FILE, BACKUPS_DIR,
    LANES, CAT_COLORS, EVENT_TYPES, SCORING_PRESETS,
    DEFAULT_CATEGORY_ORDER, UPDATE_MAX_BODY, _SAFE_STATIC_EXTS, _DEFAULT_COMP_CONFIG,
)
import app.sse as _sse_mod
from app.sse import _sse_notify, _sse_condition
from app.database import db, ensure_schema
from app.utils import (
    balanced_heats, _event_direction_flags, _safe_int,
    _event_type_from_scoring, _filter_payload_to_active_lanes,
    _default_inactive_judge, _protect_lane_stopped_timers,
)

app = Flask(__name__, template_folder=TPL_DIR, static_folder=None)

CATEGORY_ORDER = list(DEFAULT_CATEGORY_ORDER)

state_lock = threading.Lock()

# ─── RESULTS CACHE ────────────────────────────────────────────────────────────
# Shared across all SSE threads and /state.json. Invalidated whenever raw_results,
# athletes, or events tables change. Timer ticks that write the same score value
# do NOT invalidate it (see change detection in _apply_judge_scores_to_raw_results).
_results_lock  = threading.Lock()
_results_cache = None   # (events_list, results_by_cat) tuple, or None if not yet built
_results_dirty = True   # True → recompute before next use

def _invalidate_results_cache():
    global _results_dirty
    with _results_lock:
        _results_dirty = True

def _get_cached_results():
    """Return (events_list, results_by_cat), recomputing only when dirty. Thread-safe."""
    global _results_cache, _results_dirty
    with _results_lock:
        if _results_dirty or _results_cache is None:
            try:
                evs, res = get_public_results()
            except Exception:
                evs, res = [], {}
            _results_cache = (evs, res)
            _results_dirty = False
        return _results_cache

def load_state():
    """Load state.json; returns {} on missing file, invalid JSON, or I/O error."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

def save_state(data):
    """Atomic write: write to .tmp then rename so readers never see partial JSON."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, STATE_FILE)

def merge_state(patch):
    with state_lock:
        s = load_state(); s.update(patch); save_state(s)
    _sse_notify()
    return s

def get_comp_config():
    """Centralized competition config (lanes, scoring_mode, heat_order, etc.) for multi-sport scaling. Stored in state as competition_config."""
    s = load_state()
    return s.get("competition_config", {})

def get_lane_count():
    """Dynamic lane/platform count (1–4). LANES constant is the default fallback."""
    config = get_comp_config()
    return config.get("lanes", LANES)

def get_competition_status():
    """Current workflow stage: setup | athletes | heats | live | finished. Stored in state; derived if missing."""
    s = load_state()
    status = s.get("competition_status") or ""
    if status in ("setup", "athletes", "heats", "live", "finished"):
        return status
    con = db()
    n_events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0] or 0
    n_athletes = con.execute("SELECT COUNT(*) FROM athletes WHERE status='active'").fetchone()[0] or 0
    con.close()
    heats = get_all_heats()
    n_heats = len(set((h["category"], h["heat_number"]) for h in heats)) if heats else 0
    if n_events == 0:
        return "setup"
    if n_athletes == 0:
        return "athletes"
    if n_heats == 0:
        return "heats"
    return "live"

def set_competition_status(status):
    """Set competition_status in state (setup, athletes, heats, live, finished)."""
    if status not in ("setup", "athletes", "heats", "live", "finished"):
        return
    with state_lock:
        s = load_state()
        s["competition_status"] = status
        save_state(s)

def _persist_category_order():
    """Save current CATEGORY_ORDER to state.json so it survives restarts."""
    with state_lock:
        s = load_state()
        s["category_order"] = list(CATEGORY_ORDER)
        save_state(s)

def _restore_category_order():
    """Load CATEGORY_ORDER from state.json on startup; falls back to compiled default."""
    s = load_state()
    saved = s.get("category_order")
    if saved and isinstance(saved, list) and len(saved) > 0:
        CATEGORY_ORDER.clear()
        CATEGORY_ORDER.extend(saved)

# ─── AUTH ─────────────────────────────────────────────────────────────────────
def _check_auth():
    """Return True if request is authenticated (or no password is configured)."""
    password = os.environ.get("COMP_PASSWORD", "").strip()
    if not password:
        return True
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            _, pwd = decoded.split(":", 1)
            if pwd == password:
                return True
        except Exception:
            pass
    return False

@app.before_request
def _require_comp_auth():
    """Protect all /comp/* routes with HTTP Basic Auth when COMP_PASSWORD is set."""
    if request.path.startswith("/comp/") or request.path == "/comp":
        if not _check_auth():
            resp = app.response_class("Unauthorized\n", status=401, mimetype="text/plain")
            resp.headers["WWW-Authenticate"] = 'Basic realm="Competition Control"'
            return resp

def _canonical_category(category):
    """Resolve category to the form used in CATEGORY_ORDER (e.g. 'u80' -> 'U80') so lookups match."""
    if not category:
        return category
    for cat in CATEGORY_ORDER:
        if cat.lower() == category.lower():
            return cat
    return category

def get_category_start_count(category):
    """
    Fixed number of athletes that started this category (for points scale).
    Stored in state.json as category_start_counts[category]. Set when heats are
    generated or on first use to current athlete count for that category, then not reduced.
    Category is normalized to match CATEGORY_ORDER (e.g. u80 -> U80) so case mismatches don't yield wrong counts.
    """
    canonical = _canonical_category(category)
    s = load_state()
    counts = s.get("category_start_counts") or {}
    if canonical in counts:
        return counts[canonical]
    con = db()
    n = con.execute("SELECT COUNT(*) FROM athletes WHERE category=?", (canonical,)).fetchone()[0]
    con.close()
    n = max(1, n)
    with state_lock:
        s = load_state()
        s.setdefault("category_start_counts", {})[canonical] = n
        save_state(s)
    return n

def lock_category_start_counts():
    """
    Snapshot current athlete count per category as the 'starting' count (for fixed
    points scale). Called when heats are generated so the competition has a defined start.
    Only sets categories that don't already have a count (so existing comps are not changed).
    """
    con = db()
    s = load_state()
    counts = s.get("category_start_counts") or {}
    changed = False
    for cat in CATEGORY_ORDER:
        if cat in counts:
            continue
        n = con.execute("SELECT COUNT(*) FROM athletes WHERE category=?", (cat,)).fetchone()[0]
        if n > 0:
            counts[cat] = n
            changed = True
    con.close()
    if changed:
        with state_lock:
            s = load_state()
            s["category_start_counts"] = s.get("category_start_counts") or {}
            for k, v in counts.items():
                if k not in s["category_start_counts"]:
                    s["category_start_counts"][k] = v
            save_state(s)

def get_comp_state():
    con = db()
    row = con.execute("SELECT category,event,heat FROM competition_state").fetchone()
    con.close()
    if row:
        return dict(row)
    return {"category": CATEGORY_ORDER[0] if CATEGORY_ORDER else "", "event": 1, "heat": 1}

def set_comp_state(category, event, heat):
    con = db(); cur = con.cursor()
    cur.execute("DELETE FROM competition_state")
    cur.execute("INSERT INTO competition_state(category,event,heat) VALUES(?,?,?)", (category, event, heat))
    con.commit(); con.close()

def get_heat_lanes(category, heat):
    con = db()
    rows = con.execute("SELECT lane,athlete_name FROM heats WHERE category=? AND heat_number=? ORDER BY lane", (category, heat)).fetchall()
    con.close()
    return [dict(r) for r in rows]

# ─── SCORING ENGINE ──────────────────────────────────────────────────────────

def _compute_event_points_core(con, event_id, category, total_athletes, primary_higher, has_secondary, secondary_higher):
    """
    Compute event points for one (event_id, category) using an open connection.
    Athletes with zero or no result get 0 points and are excluded from ranking.
    Points scale is based on total category size: rank 1 = total_athletes pts, rank 2 = total_athletes-1, etc.
    """
    results_rows = con.execute(
        "SELECT rr.athlete_id, rr.raw_value, rr.tiebreak, rr.result_type, a.name, a.status FROM raw_results rr "
        "JOIN athletes a ON a.id = rr.athlete_id WHERE rr.event_id=? AND a.category=?",
        (event_id, category)
    ).fetchall()
    active_athletes = con.execute(
        "SELECT id, name FROM athletes WHERE category=? AND status='active'", (category,)
    ).fetchall()
    active_athletes = [dict(a) for a in active_athletes]

    n_active = len(active_athletes)
    total_athletes = max(total_athletes, n_active, 1)

    # validResults: raw_value > 0 and not DNS. Zero/DNS/no result → 0 points, excluded from ranking.
    scored = []
    for r in results_rows:
        d = dict(r)
        raw = d["raw_value"]
        rtype = (d.get("result_type") or "score").lower()
        if rtype == "dns":
            continue  # DNS: never ranks, always 0 points
        if raw is not None and raw > 0:
            scored.append({
                "athlete_id": d["athlete_id"], "name": d["name"], "raw_value": d["raw_value"],
                "tiebreak": d["tiebreak"], "status": d.get("status") or "active",
                "result_type": rtype,
            })

    # Build output for all active athletes; default 0 points for zero/no result
    output = {}
    results_by_id = {r["athlete_id"]: dict(r) for r in results_rows}
    for a in active_athletes:
        aid = a["id"]
        r = results_by_id.get(aid)
        rtype = (r.get("result_type") or "score").lower() if r else "score"
        if r is None:
            output[aid] = {"points": 0, "raw_value": None, "tiebreak": None, "rank": None, "name": a["name"], "result_type": "score"}
        elif rtype == "dns" or r["raw_value"] is None or r["raw_value"] == 0:
            output[aid] = {"points": 0, "raw_value": r["raw_value"], "tiebreak": r["tiebreak"], "rank": None, "name": a["name"], "result_type": rtype}
        # else: will be filled when we assign points to validResults

    if not scored:
        return output

    def sort_key(x):
        primary = x["raw_value"] if primary_higher else -x["raw_value"]
        if has_secondary and x["tiebreak"] is not None:
            secondary = x["tiebreak"] if secondary_higher else -(x["tiebreak"] or 0)
        else:
            secondary = -(x["tiebreak"] or 0) if primary_higher else (x["tiebreak"] or 0)
        return (primary, secondary)
    scored.sort(key=sort_key, reverse=True)

    def _vals_equal(a, b):
        """Float-safe equality: equal if within 0.001 of each other."""
        if a is None and b is None: return True
        if a is None or b is None: return False
        return abs(float(a) - float(b)) < 0.001

    # Assign points to validResults only: points = total_athletes - rank + 1 (with tie splitting)
    i = 0
    while i < len(scored):
        j = i
        while j < len(scored) - 1:
            curr, nxt = scored[j], scored[j+1]
            same_primary = _vals_equal(curr["raw_value"], nxt["raw_value"])
            same_secondary = _vals_equal(curr["tiebreak"] or 0, nxt["tiebreak"] or 0)
            if has_secondary:
                if same_primary and same_secondary:
                    j += 1
                else:
                    break
            else:
                if same_primary:
                    j += 1
                else:
                    break
        tied_count = j - i + 1
        # Rank positions use full category scale: 1st → total_athletes, 2nd → total_athletes-1, ...
        positions = range(total_athletes - i, total_athletes - i - tied_count, -1)
        split_pts = sum(positions) / tied_count
        for k in range(i, j + 1):
            aid = scored[k]["athlete_id"]
            is_withdrawn = (scored[k].get("status") or "active") != "active"
            rtype = (scored[k].get("result_type") or "score").lower()
            output[aid] = {
                "points": 0.0 if is_withdrawn else round(split_pts, 2),
                "raw_value": scored[k]["raw_value"],
                "tiebreak": scored[k]["tiebreak"],
                "rank": i + 1,
                "name": scored[k]["name"],
                "result_type": rtype,
            }
        i = j + 1

    return output

def calculate_event_points(event_id, category):
    """
    Fixed points scale. Future sports can branch on get_comp_config().get("scoring_mode").
    Single-event entry point; for batched leaderboard see get_leaderboard (one connection per category).
    """
    canonical = _canonical_category(category)
    con = db()
    event_row = con.execute(
        "SELECT event_type, primary_metric, secondary_metric, primary_direction, secondary_direction "
        "FROM events WHERE id=?", (event_id,)
    ).fetchone()
    event_row = dict(event_row) if event_row else {}
    event_type = event_row.get("event_type") or "reps"
    primary_higher, has_secondary, secondary_higher = _event_direction_flags(event_row, event_type)
    total_athletes = get_category_start_count(category)
    out = _compute_event_points_core(con, event_id, canonical, total_athletes, primary_higher, has_secondary, secondary_higher)
    con.close()
    return out


def get_event_points_for_athlete(athlete_id, event_id, category):
    """Convenience: get just the points for one athlete in one event."""
    pts = calculate_event_points(event_id, category)
    return pts.get(athlete_id, {}).get("points", 0)


def get_raw_result(athlete_name, event_id):
    """Get the raw value stored for display purposes."""
    con = db()
    row = con.execute(
        "SELECT rr.raw_value, rr.tiebreak, rr.result_type FROM raw_results rr "
        "JOIN athletes a ON a.id=rr.athlete_id "
        "WHERE a.name=? AND rr.event_id=?", (athlete_name, event_id)
    ).fetchone()
    con.close()
    if row:
        return {"raw_value": row["raw_value"], "tiebreak": row["tiebreak"], "result_type": row["result_type"] or "score"}
    return None

def get_raw_results_batch(event_id, athlete_names):
    """Fetch raw results for many athletes in one event (one query). Returns { name: { raw_value, tiebreak, result_type } }."""
    if not event_id or not athlete_names:
        return {}
    con = db()
    placeholders = ",".join("?" * len(athlete_names))
    rows = con.execute(
        "SELECT a.name, rr.raw_value, rr.tiebreak, rr.result_type FROM raw_results rr "
        "JOIN athletes a ON a.id=rr.athlete_id "
        "WHERE rr.event_id=? AND a.name IN (" + placeholders + ")",
        (event_id,) + tuple(athlete_names)
    ).fetchall()
    con.close()
    return {r["name"]: {"raw_value": r["raw_value"], "tiebreak": r["tiebreak"], "result_type": r["result_type"] or "score"} for r in rows}


def get_leaderboard(category=None):
    """
    Build leaderboard by summing calculated points across all events.
    Uses one DB connection per category and batches event-point computation (scales to 800+ athletes, 20 categories, 10 events).
    """
    if category:
        cats_to_process = [category]
    else:
        cats_to_process = CATEGORY_ORDER

    output = []
    for cat in cats_to_process:
        canonical = _canonical_category(cat)
        con = db()
        events = con.execute(
            "SELECT id, name, event_number, event_type, primary_metric, secondary_metric, primary_direction, secondary_direction "
            "FROM events ORDER BY event_number"
        ).fetchall()
        events = [dict(e) for e in events]
        athletes = con.execute(
            "SELECT id, name, category FROM athletes WHERE category=? AND status='active'", (canonical,)
        ).fetchall()
        athletes = [dict(a) for a in athletes]
        if not athletes:
            con.close()
            continue

        total_athletes = get_category_start_count(cat)
        totals = {a["id"]: 0.0 for a in athletes}
        for ev in events:
            event_type = ev.get("event_type") or "reps"
            primary_higher, has_secondary, secondary_higher = _event_direction_flags(ev, event_type)
            pts_map = _compute_event_points_core(con, ev["id"], canonical, total_athletes, primary_higher, has_secondary, secondary_higher)
            for aid, info in pts_map.items():
                totals[aid] = totals.get(aid, 0) + info.get("points", 0)

        for a in athletes:
            output.append({
                "name":     a["name"],
                "category": a["category"],
                "score":    str(round(totals.get(a["id"], 0), 2)),
                "origin":   "",
            })
        con.close()

    output.sort(key=lambda x: float(x["score"]), reverse=True)
    return output


def get_leaderboard_detailed(category):
    """
    Returns per-event breakdown for leaderboard display.
    Uses one DB connection and batched event-point computation (scales to large categories and many events).
    """
    canonical = _canonical_category(category)
    con = db()
    events = con.execute(
        "SELECT id, name, event_number, event_type, primary_metric, secondary_metric, primary_direction, secondary_direction "
        "FROM events ORDER BY event_number"
    ).fetchall()
    events = [dict(e) for e in events]
    athletes = con.execute(
        "SELECT id, name FROM athletes WHERE category=? AND status='active' ORDER BY name", (canonical,)
    ).fetchall()
    athletes = [dict(a) for a in athletes]

    total_athletes = get_category_start_count(category)
    # Per-event points: event_id -> { athlete_id -> { points, raw_value, rank, name } }
    event_points = []
    for ev in events:
        event_type = ev.get("event_type") or "reps"
        primary_higher, has_secondary, secondary_higher = _event_direction_flags(ev, event_type)
        pts_map = _compute_event_points_core(con, ev["id"], canonical, total_athletes, primary_higher, has_secondary, secondary_higher)
        event_points.append((ev, pts_map))
    con.close()

    rows = []
    for a in athletes:
        event_data = []
        total = 0.0
        for ev, pts_map in event_points:
            info = pts_map.get(a["id"], {"points": 0, "raw_value": None, "rank": None, "tiebreak": None})
            total += info["points"]
            event_data.append({
                "event_name":      ev["name"],
                "event_type":      ev.get("event_type"),
                "primary_metric":  ev.get("primary_metric"),
                "secondary_metric": ev.get("secondary_metric"),
                "raw_value":       info["raw_value"],
                "tiebreak":        info.get("tiebreak"),
                "points":          info["points"],
                "rank":            info["rank"],
                "result_type":     info.get("result_type", "score"),
            })
        rows.append({
            "name":        a["name"],
            "event_data":  event_data,
            "total_points": round(total, 2),
        })

    rows.sort(key=lambda x: x["total_points"], reverse=True)
    for i, r in enumerate(rows):
        r["overall_rank"] = i + 1

    return rows, events


def get_public_events():
    """Events list for public results page (id, name, event_number, event_type, primary_metric, secondary_metric)."""
    con = db()
    rows = con.execute(
        "SELECT id, name, event_number, event_type, primary_metric, secondary_metric "
        "FROM events ORDER BY event_number"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_public_results():
    """Per-category results for public results page: events list and results[category] = [{ name, total_points, event_data }]."""
    events = get_public_events()
    results = {}
    for cat in CATEGORY_ORDER:
        try:
            rows, evs = get_leaderboard_detailed(cat)
        except Exception:
            continue
        if not rows:
            continue
        results[cat] = [{"name": r["name"], "total_points": r["total_points"], "event_data": r["event_data"]} for r in rows]
    return events, results


# ─── HEAT GENERATION ─────────────────────────────────────────────────────────

def get_max_heat(category):
    con = db()
    row = con.execute("SELECT MAX(heat_number) FROM heats WHERE category=?", (category,)).fetchone()
    con.close()
    return row[0] or 1

def get_athletes_ordered_for_event(category, next_event_number, total_events):
    """
    Return list of athlete names in the order they should compete (worst first, best last).
    - Event 1: registration/DB order.
    - Events 2 → N (including final): previous event rank (worst first). Overall standings are
      used only for scoring/display of the final event, not for heat order.
    Withdrawn (non-active) athletes are excluded.
    """
    con = db()
    active = [dict(r) for r in con.execute(
        "SELECT id, name FROM athletes WHERE category=? AND status='active' ORDER BY id", (category,)
    ).fetchall()]
    con.close()
    if not active:
        return []

    # Event 1: registration order (DB order by id)
    if next_event_number <= 1:
        return [a["name"] for a in active]

    # Events 2 → N (including final): previous event rank (worst first)
    con = db()
    prev_event_row = con.execute(
        "SELECT id FROM events WHERE event_number=?", (next_event_number - 1,)
    ).fetchone()
    con.close()
    if not prev_event_row:
        return [a["name"] for a in active]

    pts_map = calculate_event_points(prev_event_row["id"], category)
    # Build (name, rank); no result = rank 999 so they go first (worst)
    named_ranks = []
    for a in active:
        info = pts_map.get(a["id"], {})
        rank = info.get("rank") or 999
        named_ranks.append((a["name"], rank))
    named_ranks.sort(key=lambda x: (x[1], x[0]))  # ascending rank (best first)
    named_ranks.reverse()  # worst first so worst heat 1, best last heat
    return [name for name, _ in named_ranks]

def generate_heats():
    """Full regeneration — event 1 only. Locks starting athlete counts. Registration order."""
    lock_category_start_counts()
    con = db(); cur = con.cursor()
    cur.execute("DELETE FROM heats")
    for category in CATEGORY_ORDER:
        athletes = [r[0] for r in cur.execute(
            "SELECT name FROM athletes WHERE category=? AND status='active' ORDER BY id", (category,)
        ).fetchall()]
        if not athletes:
            continue
        for h, group in enumerate(balanced_heats(athletes, get_lane_count()), 1):
            for lane, name in enumerate(group, 1):
                cur.execute("INSERT INTO heats(event_id,category,heat_number,lane,athlete_name) VALUES(?,?,?,?,?)",
                            (1, category, h, lane, name))
    con.commit(); con.close()

def generate_heats_for_next_event(next_event_number):
    """
    Regenerate heats for the next event. Order: worst first, best last (so best compete in last heat).
    - Event 1: registration order.
    - Events 2 → second-last: previous event rank (worst first).
    - Final event: overall standings (lowest points first).
    """
    con = db()
    total_events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0] or 1
    ev_row = con.execute("SELECT id FROM events WHERE event_number=?", (next_event_number,)).fetchone()
    event_id = ev_row["id"] if ev_row else 1
    cur = con.cursor()
    cur.execute("DELETE FROM heats")
    for category in CATEGORY_ORDER:
        athletes = get_athletes_ordered_for_event(category, next_event_number, total_events)
        if not athletes:
            continue
        for h, group in enumerate(balanced_heats(athletes, get_lane_count()), 1):
            for lane, name in enumerate(group, 1):
                cur.execute("INSERT INTO heats(event_id,category,heat_number,lane,athlete_name) VALUES(?,?,?,?,?)",
                            (event_id, category, h, lane, name))
    con.commit()
    con.close()

def regenerate_remaining_heats():
    """
    Uses actual scores to determine who has already competed — not heat assignments.
    Rebalances remaining active athletes who haven't yet competed in the current event.
    """
    cs = get_comp_state()
    current_cat   = cs["category"]
    current_event = cs["event"]
    current_heat  = cs["heat"]
    con = db(); cur = con.cursor()
    cur_idx = CATEGORY_ORDER.index(current_cat) if current_cat in CATEGORY_ORDER else -1

    # Resolve current event_number -> event_id
    event_row = cur.execute("SELECT id FROM events WHERE event_number=?", (current_event,)).fetchone()
    current_event_id = event_row["id"] if event_row else None

    # Who has a score in THIS event = has competed in this event (truth, not heat assignments)
    if current_event_id:
        already_scored = {r[0] for r in cur.execute(
            "SELECT DISTINCT a.name FROM raw_results rr "
            "JOIN athletes a ON a.id = rr.athlete_id "
            "WHERE rr.event_id=? AND a.category=? AND a.status='active'",
            (current_event_id, current_cat)
        ).fetchall()}
    else:
        already_scored = set()

    # Also include anyone assigned to heats before current heat
    already_in_past = {r[0] for r in con.execute(
        "SELECT athlete_name FROM heats WHERE category=? AND heat_number<?",
        (current_cat, current_heat)
    ).fetchall()}

    already_ran = already_scored | already_in_past

    total_events = cur.execute("SELECT COUNT(*) FROM events").fetchone()[0] or 1

    # Wipe from current heat onwards, and all future categories
    cur.execute("DELETE FROM heats WHERE category=? AND heat_number>=?",
                (current_cat, current_heat))
    for cat in CATEGORY_ORDER:
        if CATEGORY_ORDER.index(cat) > cur_idx:
            cur.execute("DELETE FROM heats WHERE category=?", (cat,))

    # Active athletes who haven't competed yet (unordered)
    remaining_set = {r[0] for r in con.execute(
        "SELECT name FROM athletes WHERE category=? AND status='active'", (current_cat,)
    ).fetchall() if r[0] not in already_ran}
    # Order by previous event rank (or leaderboard for final): worst first
    ordered_current = get_athletes_ordered_for_event(current_cat, current_event, total_events)
    remaining = [n for n in ordered_current if n in remaining_set]

    heat_event_id = current_event_id or 1
    if remaining:
        for i, group in enumerate(balanced_heats(remaining, get_lane_count())):
            h = current_heat + i
            for lane, name in enumerate(group, 1):
                cur.execute("INSERT INTO heats(event_id,category,heat_number,lane,athlete_name) VALUES(?,?,?,?,?)",
                            (heat_event_id, current_cat, h, lane, name))

    # Fully regenerate all future categories using previous-event / leaderboard order
    for cat in CATEGORY_ORDER:
        if CATEGORY_ORDER.index(cat) <= cur_idx: continue
        athletes = get_athletes_ordered_for_event(cat, current_event, total_events)
        if not athletes: continue
        for h, group in enumerate(balanced_heats(athletes, get_lane_count()), 1):
            for lane, name in enumerate(group, 1):
                cur.execute("INSERT INTO heats(event_id,category,heat_number,lane,athlete_name) VALUES(?,?,?,?,?)",
                            (heat_event_id, cat, h, lane, name))
    con.commit(); con.close()

def get_broadcast_mode():
    """Section 1: Returns current data_source_mode: 'engine' | 'api' | 'manual'"""
    s = load_state()
    return s.get("broadcast", {}).get("data_source_mode", s.get("data_source_mode", "engine"))

def sync_comp_to_broadcast():
    """Push current competition state to broadcast state.json. Engine mode only. Single DB connection, single atomic write."""
    # Read state once to derive broadcast_mode and lane_count — avoids two separate load_state() calls.
    _s = load_state()
    if _s.get("broadcast", {}).get("data_source_mode", _s.get("data_source_mode", "engine")) != "engine":
        return
    lane_count = _s.get("competition_config", {}).get("lanes", LANES)
    cs = get_comp_state()
    category, event, heat = cs["category"], cs["event"], cs["heat"]
    heat_lanes = get_heat_lanes(category, heat)

    # One connection for all DB reads in this function
    con = db()
    ev_row = con.execute(
        "SELECT id, name, event_type, primary_metric, secondary_metric FROM events WHERE event_number=?", (event,)
    ).fetchone()
    ev = dict(ev_row) if ev_row else {}
    event_id   = ev.get("id")
    event_name = ev.get("name") or f"Event {event}"
    primary_metric   = ev.get("primary_metric")
    secondary_metric = ev.get("secondary_metric")
    event_type = ev.get("event_type")
    total_events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0] or 1
    total_active = con.execute(
        "SELECT COUNT(*) FROM athletes WHERE category=? AND status='active'", (category,)
    ).fetchone()[0]
    results_entered = con.execute(
        "SELECT COUNT(*) FROM raw_results rr "
        "JOIN athletes a ON a.id=rr.athlete_id "
        "WHERE rr.event_id=? AND a.category=? AND a.status='active'",
        (event_id, category)
    ).fetchone()[0] if event_id else 0
    # Category list for overlay (only categories that have athletes)
    cats = []
    for cat in CATEGORY_ORDER:
        n = con.execute("SELECT COUNT(*) FROM athletes WHERE category=? AND status='active'", (cat,)).fetchone()[0]
        if n > 0:
            cats.append({"name": cat, "color": CAT_COLORS.get(cat, "#F5C842"), "athleteCount": n})
    con.close()

    # Raw results for current heat lanes (separate helper uses its own connection)
    lane_names = [hl["athlete_name"] for hl in heat_lanes] if heat_lanes else []
    raw_batch  = get_raw_results_batch(event_id, lane_names) if event_id and lane_names else {}
    broadcast_lanes = []
    for i in range(lane_count):
        if i < len(heat_lanes):
            hl  = heat_lanes[i]
            raw = raw_batch.get(hl["athlete_name"])
            broadcast_lanes.append({
                "num": i+1, "name": hl["athlete_name"].upper(),
                "detail": category,
                "score": str(raw["raw_value"]) if raw else "—"
            })
        else:
            broadcast_lanes.append({"num": i+1, "name": "", "detail": "", "score": ""})

    leaderboard = get_leaderboard(category=category)

    champ_fields = {}
    if total_active > 0 and results_entered >= total_active and leaderboard:
        top = leaderboard[0]
        champ_fields = {
            "champName":       top["name"].upper(),
            "champDetail":     top.get("category", category),
            "champScore":      top["score"],
            "champScoreLabel": "TOTAL POINTS",
            "champEvents":     f"EVENT {event} OF {total_events}",
            "champEyebrow":    event_name.upper(),
            "champComp":       f"{category} CATEGORY",
        }

    competition_block = {
        "category":    category, "event": event, "heat": heat,
        "eventName":   event_name, "lanes": broadcast_lanes, "leaderboard": leaderboard,
        "eventMetric": primary_metric, "eventSecondaryMetric": secondary_metric, "eventType": event_type,
    }
    broadcast_block = {
        "lanes": broadcast_lanes, "laneCount": lane_count, "athletes": leaderboard,
        "categories": cats, "eventName": event_name.upper(),
        "eventNum": f"EVENT {event} OF {total_events}", "eventSub": f"HEAT {heat} · {category}",
        "compCategory": category, "compEvent": event, "compHeat": heat, "compEventName": event_name,
        "data_source_mode": "engine",
        "eventMetric": primary_metric, "eventSecondaryMetric": secondary_metric, "eventType": event_type,
        **champ_fields,
    }

    # Build full patch — detect heat change and include judge lane resets in the same atomic write
    patch = {**broadcast_block, "competition": competition_block, "broadcast": broadcast_block}
    with state_lock:
        prev = load_state()
        if (prev.get("compCategory"), prev.get("compEvent"), prev.get("compHeat")) != (category, event, heat):
            default_judge = {"reps": 0, "light": "none", "timerRunning": False,
                             "timerRemaining": 60, "timerSecs": 60, "primaryValue": "", "secondaryValue": ""}
            for i in range(1, lane_count + 1):
                patch["judgeL" + str(i)] = default_judge.copy()
        prev.update(patch)
        save_state(prev)
    _sse_notify()

# ─── SSE STREAM ENDPOINT ──────────────────────────────────────────────────────
@app.route("/stream")
def stream():
    """Server-Sent Events endpoint. Clients receive state pushes on every change instead of polling.
    Compatible with gunicorn --worker-class=gthread (single worker, multiple threads)."""
    def generate():
        seen_version = -1
        while True:
            with _sse_condition:
                changed = _sse_condition.wait_for(lambda: _sse_mod._sse_version != seen_version, timeout=30)
                seen_version = _sse_mod._sse_version
            if changed:
                data = load_state()
                evs, res = _get_cached_results()
                data["events"] = evs
                data["results"] = res
                yield f"data: {json.dumps(data)}\n\n"
            else:
                yield ": keepalive\n\n"
    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"]     = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

# ─── SECTION 1: BROADCAST DATA SOURCE MODE ────────────────────────────────────

@app.route("/comp/action/set_broadcast_mode", methods=["POST"])
def action_set_broadcast_mode():
    """Switch data_source_mode between engine / api / manual."""
    mode = request.form.get("mode", "engine")
    if mode not in ("engine", "api", "manual"):
        return jsonify({"error": "invalid mode"}), 400
    with state_lock:
        s = load_state()
        s.setdefault("broadcast", {})["data_source_mode"] = mode
        s["data_source_mode"] = mode
        save_state(s)
    if mode == "engine":
        try: sync_comp_to_broadcast()
        except: pass
    return redirect(request.referrer or "/comp/events")

@app.route("/comp/api/broadcast_mode")
def api_broadcast_mode():
    resp = jsonify({"data_source_mode": get_broadcast_mode()})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

# ─── BROADCAST ROUTES ─────────────────────────────────────────────────────────
@app.route("/")
def root():
    return send_from_directory(DIR, "home.html")

@app.route("/state.json")
def state_json():
    data = load_state()
    evs, res = _get_cached_results()
    data["events"] = evs
    data["results"] = res
    resp = app.response_class(response=json.dumps(data), mimetype="application/json")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

def _apply_judge_scores_to_raw_results(judge_payload=None):
    """Write judge panel scores to raw_results (engine mode only).
    Uses one DB connection for all lanes. Skips writes when value is unchanged;
    only invalidates the results cache when a value actually changes."""
    # Read state once to derive broadcast_mode and lane_count — avoids two separate load_state() calls.
    _s = load_state()
    if _s.get("broadcast", {}).get("data_source_mode", _s.get("data_source_mode", "engine")) != "engine":
        return
    cs = get_comp_state()
    category, event, heat = cs["category"], cs["event"], cs["heat"]
    heat_lanes = get_heat_lanes(category, heat)
    if not heat_lanes:
        return

    con = db()
    ev_row = con.execute(
        "SELECT id, primary_metric, secondary_metric FROM events WHERE event_number=?", (event,)
    ).fetchone()
    if not ev_row:
        con.close()
        return
    ev_row = dict(ev_row)
    event_id = ev_row["id"]
    primary_metric = (ev_row.get("primary_metric") or "reps")
    if not isinstance(primary_metric, str):
        primary_metric = "reps"
    primary_metric = primary_metric.lower()
    secondary_metric = (ev_row.get("secondary_metric") or "")
    secondary_metric = secondary_metric.lower() if isinstance(secondary_metric, str) else ""
    lane_count = _s.get("competition_config", {}).get("lanes", LANES)
    source = judge_payload if judge_payload is not None else _s

    results_changed = False
    cur = con.cursor()
    for i in range(min(lane_count, len(heat_lanes))):
        jd = source.get("judgeL" + str(i + 1)) if isinstance(source, dict) else {}
        if not isinstance(jd, dict):
            jd = {}
        athlete_name = heat_lanes[i]["athlete_name"]
        raw_value = None
        tiebreak = None
        if primary_metric == "reps":
            r = jd.get("reps")
            if r is not None and str(r).strip() != "":
                try: raw_value = float(r)
                except (TypeError, ValueError): pass
        elif primary_metric == "time":
            # Time events: score = elapsed seconds = timerSecs - timerRemaining
            pv = jd.get("primaryValue")
            if pv is not None and str(pv).strip() != "":
                try: raw_value = float(pv)
                except (TypeError, ValueError): pass
            if raw_value is None:
                try:
                    secs = jd.get("timerSecs")
                    rem = jd.get("timerRemaining")
                    if secs is not None and rem is not None:
                        raw_value = max(0.0, float(secs) - float(rem))
                except (TypeError, ValueError): pass
        else:
            pv = jd.get("primaryValue")
            if pv is not None and str(pv).strip() != "":
                try: raw_value = float(pv)
                except (TypeError, ValueError): pass
            if secondary_metric == "time" and (primary_metric in ("objects", "distance")):
                sv = jd.get("secondaryValue")
                if sv is not None and str(sv).strip() != "":
                    try: tiebreak = float(sv)
                    except (TypeError, ValueError): pass
        if raw_value is None:
            continue
        ath = con.execute("SELECT id FROM athletes WHERE name=?", (athlete_name,)).fetchone()
        if not ath:
            continue
        aid = ath["id"]
        # Change detection: skip write if value hasn't changed
        existing = con.execute(
            "SELECT raw_value, tiebreak FROM raw_results WHERE athlete_id=? AND event_id=?",
            (aid, event_id)
        ).fetchone()
        if existing and existing["raw_value"] is not None:
            try:
                same_primary = abs(float(existing["raw_value"]) - raw_value) < 0.001
                old_tb = existing["tiebreak"]
                same_tiebreak = (
                    (tiebreak is None and old_tb is None) or
                    (tiebreak is not None and old_tb is not None and abs(float(old_tb) - tiebreak) < 0.001)
                )
                if same_primary and same_tiebreak:
                    continue  # value unchanged — skip write, do NOT dirty the cache
            except (TypeError, ValueError):
                pass
        cur.execute(
            """INSERT INTO raw_results(athlete_id, event_id, raw_value, tiebreak)
               VALUES(?,?,?,?)
               ON CONFLICT(athlete_id, event_id)
               DO UPDATE SET raw_value=excluded.raw_value, tiebreak=excluded.tiebreak""",
            (aid, event_id, raw_value, tiebreak)
        )
        results_changed = True

    if results_changed:
        con.commit()
        _invalidate_results_cache()
    con.close()
    sync_comp_to_broadcast()

def _active_lane_count():
    """Number of active lanes from state['competition_config']['lanes'] (1-8). Used so only judgeL1..judgeL{N} are updated; inactive lanes are forced to timerRunning=False."""
    return get_lane_count()

@app.route("/update", methods=["POST", "OPTIONS"])
def update_state():
    if request.method == "OPTIONS":
        resp = app.response_class(status=200)
        resp.headers.update({"Access-Control-Allow-Origin":"*","Access-Control-Allow-Methods":"POST,OPTIONS","Access-Control-Allow-Headers":"Content-Type"})
        return resp
    if request.content_length and request.content_length > UPDATE_MAX_BODY:
        return jsonify({"error": "Payload too large"}), 413
    payload = request.get_json(force=True, silent=True) or {}
    lane_count = _active_lane_count()
    payload = _filter_payload_to_active_lanes(payload, lane_count)
    with state_lock:
        current = load_state()
        _protect_lane_stopped_timers(payload, current)
        merge_dict = {k: v for k, v in payload.items() if k != "resetAllTimers"}
        current.update(merge_dict)
        for i in range(lane_count + 1, 9):
            key = "judgeL" + str(i)
            current[key] = _default_inactive_judge().copy()
        save_state(current)
    _apply_judge_scores_to_raw_results(payload)
    resp = jsonify({"ok": True})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

_PROXY_ALLOWLIST = [h.strip().lower() for h in os.environ.get("PROXY_ALLOWLIST", "").split(",") if h.strip()]

@app.route("/proxy")
def proxy():
    """Proxy for external API sources (StrengthResults etc). Set PROXY_ALLOWLIST env var to restrict domains."""
    target = request.args.get("url", "")
    if not target:
        return jsonify({"error": "no url"}), 400
    if _PROXY_ALLOWLIST:
        parsed_host = urllib.parse.urlparse(target).netloc.split(":")[0].lower()
        if not any(parsed_host == a or parsed_host.endswith("." + a) for a in _PROXY_ALLOWLIST):
            return jsonify({"error": "domain not in PROXY_ALLOWLIST"}), 403
    try:
        req = urllib.request.Request(target, headers={"Accept": "application/json", "User-Agent": "StrongmanBroadcast"})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
        resp = app.response_class(response=body, mimetype="application/json")
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/<path:filename>")
def static_files(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _SAFE_STATIC_EXTS:
        abort(404)
    return send_from_directory(DIR, filename)

# ─── COMPETITION ROUTES ────────────────────────────────────────────────────────
@app.route("/comp/")
@app.route("/comp")
def comp_index():
    return redirect("/comp/events")

@app.route("/comp/dashboard")
def comp_dashboard():
    """Legacy: redirect to Setup (events) so flow is Home → Setup → Events & categories → Athletes → Heats → Run."""
    q = ("?backup=" + request.args.get("backup", "")) if request.args.get("backup") else ""
    return redirect("/comp/events" + q)

@app.route("/comp/events")
def comp_events():
    """Events/setup page (formerly comp_home)."""
    cs = get_comp_state()
    con = db()
    events = con.execute(
        "SELECT id, name, event_number, event_type, "
        "primary_metric, secondary_metric, primary_direction, secondary_direction "
        "FROM events ORDER BY event_number"
    ).fetchall()
    stats = {cat: con.execute("SELECT COUNT(*) FROM athletes WHERE category=? AND status='active'",(cat,)).fetchone()[0] for cat in CATEGORY_ORDER}
    con.close()
    categories = [{"name": c, "color": CAT_COLORS.get(c, "#F5C842")} for c in CATEGORY_ORDER]
    comp_config = get_comp_config()
    lane_count = get_lane_count()
    backup_status = request.args.get("backup")
    return render_template("comp_home.html", state=cs, events=events, stats=stats,
                           category_order=CATEGORY_ORDER, categories=categories,
                           event_types=EVENT_TYPES, scoring_presets=SCORING_PRESETS,
                           broadcast_mode=get_broadcast_mode(), comp_config=comp_config, lane_count=lane_count,
                           backup_status=backup_status)

@app.route("/comp/heats")
def comp_heats():
    rows = get_all_heats()
    # If no heats but we have athletes and events, auto-generate (so heats always show after setup)
    if not rows and CATEGORY_ORDER:
        con = db()
        has_athletes = con.execute("SELECT COUNT(*) FROM athletes WHERE status='active'").fetchone()[0] > 0
        has_events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0
        con.close()
        if has_athletes and has_events:
            generate_heats()
            cs = get_comp_state()
            if not cs.get("category") and CATEGORY_ORDER:
                set_comp_state(CATEGORY_ORDER[0], 1, 1)
                sync_comp_to_broadcast()
            rows = get_all_heats()
    from collections import OrderedDict
    grouped = OrderedDict()
    for cat in CATEGORY_ORDER:
        heats = OrderedDict()
        for r in rows:
            if r["category"] == cat:
                heats.setdefault(r["heat_number"], []).append(r)
        if heats: grouped[cat] = heats
    return render_template("comp_heats.html", rows=rows, rows_grouped=grouped, category_order=CATEGORY_ORDER, lane_count=get_lane_count())

@app.route("/comp/callroom")
def comp_callroom():
    cs = get_comp_state()
    category, event, heat = cs["category"], cs["event"], cs["heat"]
    con = db()
    event_row = con.execute("SELECT name FROM events WHERE event_number=?",(event,)).fetchone()
    con.close()
    return render_template("comp_callroom.html", state=cs, current=get_heat_lanes(category,heat),
        next_heat=get_heat_lanes(category,heat+1), event_name=event_row["name"] if event_row else f"Event {event}",
        max_heat=get_max_heat(category), category_order=CATEGORY_ORDER, lane_count=get_lane_count())

@app.route("/comp/arena")
def comp_arena():
    cs = get_comp_state()
    category, event, heat = cs["category"], cs["event"], cs["heat"]
    con = db()
    event_row = con.execute("SELECT name FROM events WHERE event_number=?",(event,)).fetchone()
    con.close()
    return render_template("comp_arena.html", state=cs, lanes=get_heat_lanes(category,heat),
        event_name=event_row["name"] if event_row else f"Event {event}", category_order=CATEGORY_ORDER)

@app.route("/comp/results")
def comp_results():
    cs = get_comp_state()
    category, event, heat = cs["category"], cs["event"], cs["heat"]
    lanes = get_heat_lanes(category, heat)
    con = db()
    # Updated: include metric columns for scoring config UI
    event_row = con.execute(
        "SELECT id, name, event_type, primary_metric, secondary_metric, "
        "primary_direction, secondary_direction FROM events WHERE event_number=?",
        (event,)
    ).fetchone()
    all_events = con.execute("SELECT * FROM events ORDER BY event_number").fetchall()
    con.close()
    event_type = event_row["event_type"] if event_row else "reps"
    event_name = event_row["name"] if event_row else f"Event {event}"
    event_id_for_raw = event_row["id"] if event_row else None

    # Raw results for display in the input form
    scores = {}
    tiebreaks = {}
    for l in lanes:
        raw = get_raw_result(l["athlete_name"], event_id_for_raw) if event_id_for_raw else None
        scores[l["athlete_name"]]    = raw["raw_value"] if raw else ""
        tiebreaks[l["athlete_name"]] = raw["tiebreak"]  if raw else ""

    return render_template("comp_results.html", state=cs, lanes=lanes, scores=scores,
        tiebreaks=tiebreaks, event_name=event_name, event_type=event_type,
        all_events=all_events, category_order=CATEGORY_ORDER, event_types=EVENT_TYPES,
        current_event=event_row)

@app.route("/comp/leaderboard")
def comp_leaderboard():
    cat = request.args.get("category","")
    con = db()
    all_events = con.execute("SELECT * FROM events ORDER BY event_number").fetchall()
    con.close()
    if cat:
        detailed_rows, events_list = get_leaderboard_detailed(cat)
        return render_template("comp_leaderboard.html",
            athletes=get_leaderboard(category=cat), detailed_rows=detailed_rows,
            events_list=events_list, category_order=CATEGORY_ORDER, selected_cat=cat,
            all_events=all_events, event_types=EVENT_TYPES)
    return render_template("comp_leaderboard.html",
        athletes=get_leaderboard(category=None), detailed_rows=None,
        events_list=[], category_order=CATEGORY_ORDER, selected_cat=cat,
        all_events=all_events, event_types=EVENT_TYPES)

@app.route("/comp/athletes")
def comp_athletes():
    cat = request.args.get("category","")
    con = db()
    athletes = con.execute("SELECT * FROM athletes WHERE category=? ORDER BY name",(cat,)).fetchall() if cat else con.execute("SELECT * FROM athletes ORDER BY category,name").fetchall()
    con.close()
    return render_template("comp_athletes.html", athletes=athletes, category_order=CATEGORY_ORDER, selected_cat=cat)

def get_all_heats():
    con = db(); rows = []
    for cat in CATEGORY_ORDER:
        r = con.execute("SELECT category,heat_number,lane,athlete_name FROM heats WHERE category=? ORDER BY heat_number,lane", (cat,)).fetchall()
        rows.extend([dict(x) for x in r])
    con.close()
    return rows

# ─── ACTIONS ──────────────────────────────────────────────────────────────────
@app.route("/comp/action/next_heat", methods=["POST"])
def action_next_heat():
    cs = get_comp_state()
    category, event, heat = cs["category"], cs["event"], cs["heat"]
    if heat < get_max_heat(category):
        set_comp_state(category, event, heat+1)
    else:
        idx = CATEGORY_ORDER.index(category) if category in CATEGORY_ORDER else -1
        if idx < len(CATEGORY_ORDER)-1: set_comp_state(CATEGORY_ORDER[idx+1], event, 1)
    try: create_backup()
    except Exception: pass
    sync_comp_to_broadcast()
    return redirect(request.referrer or "/comp/callroom")

@app.route("/comp/action/prev_heat", methods=["POST"])
def action_prev_heat():
    cs = get_comp_state()
    if cs["heat"] > 1: set_comp_state(cs["category"], cs["event"], cs["heat"]-1); sync_comp_to_broadcast()
    return redirect(request.referrer or "/comp/callroom")

@app.route("/comp/action/set_heat", methods=["GET", "POST"])
def action_set_heat():
    if request.method == "GET":
        event = _safe_int(request.args.get("event"), 1, 1, 999)
        heat = _safe_int(request.args.get("heat"), 1, 1, 9999)
        category = request.args.get("category") or (CATEGORY_ORDER[0] if CATEGORY_ORDER else "")
    else:
        event = _safe_int(request.form.get("event"), 1, 1, 999)
        heat = _safe_int(request.form.get("heat"), 1, 1, 9999)
        category = request.form.get("category", CATEGORY_ORDER[0] if CATEGORY_ORDER else "")
    set_comp_state(category, event, heat)
    sync_comp_to_broadcast()
    return redirect(request.referrer or "/comp/results")

def create_backup():
    """Copy state.json and comp.db into backups/ with a timestamp. Returns (success, message)."""
    try:
        os.makedirs(BACKUPS_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        prefix = os.path.join(BACKUPS_DIR, f"backup_{ts}")
        if os.path.exists(STATE_FILE):
            shutil.copy2(STATE_FILE, prefix + "_state.json")
        if os.path.exists(DB_FILE):
            shutil.copy2(DB_FILE, prefix + "_comp.db")
        return True, f"Backup saved as backup_{ts}_*"
    except Exception as e:
        return False, str(e)

@app.route("/comp/action/backup", methods=["POST"])
def action_backup():
    """One-click backup: saves state.json and comp.db to backups/ with timestamp."""
    ok, msg = create_backup()
    return redirect("/comp/events?backup=" + ("ok" if ok else "error"))

@app.route("/comp/action/set_lanes", methods=["POST"])
def action_set_lanes():
    """Set number of platforms/lanes (1–4). Persisted in competition_config; heats regenerated."""
    try:
        lanes = int(request.form.get("lanes", 4))
    except (TypeError, ValueError):
        lanes = 4
    lanes = max(1, min(4, lanes))
    with state_lock:
        s = load_state()
        s.setdefault("competition_config", {})["lanes"] = lanes
        save_state(s)
    try:
        regenerate_remaining_heats()
    except Exception:
        pass
    sync_comp_to_broadcast()
    return redirect(request.referrer or "/comp/events")

@app.route("/comp/action/save_results", methods=["POST"])
def action_save_results():
    cs = get_comp_state()
    event_num = cs["event"]
    con = db(); cur = con.cursor()

    # Get the event id from event_number
    event_row = con.execute("SELECT id FROM events WHERE event_number=?", (event_num,)).fetchone()
    if not event_row:
        con.close()
        return redirect("/comp/results")
    event_id = event_row["id"]

    for key, value in request.form.items():
        if not key.startswith("score_"): continue
        athlete_name = key[6:]
        try: raw_value = float(value)
        except: continue

        # Optional tiebreak (for object events)
        tiebreak_key = f"tiebreak_{athlete_name}"
        tiebreak = None
        if tiebreak_key in request.form:
            try: tiebreak = float(request.form[tiebreak_key])
            except: tiebreak = None

        athlete = con.execute("SELECT id FROM athletes WHERE name=?", (athlete_name,)).fetchone()
        if not athlete: continue

        # Upsert into raw_results — raw value stored separately from points
        cur.execute("""
            INSERT INTO raw_results(athlete_id, event_id, raw_value, tiebreak)
            VALUES(?,?,?,?)
            ON CONFLICT(athlete_id, event_id) DO UPDATE SET raw_value=excluded.raw_value, tiebreak=excluded.tiebreak
        """, (athlete["id"], event_id, raw_value, tiebreak))

    con.commit(); con.close()
    _invalidate_results_cache()
    sync_comp_to_broadcast()
    return redirect("/comp/results")

@app.route("/comp/action/update_event_type", methods=["POST"])
def action_update_event_type():
    event_id   = request.form.get("event_id")
    event_type = request.form.get("event_type","reps")
    if event_id and event_type in EVENT_TYPES:
        con = db()
        con.execute("UPDATE events SET event_type=? WHERE id=?", (event_type, event_id))
        con.commit(); con.close()
        _invalidate_results_cache()
    return redirect(request.referrer or "/comp/events")

@app.route("/comp/action/generate_heats", methods=["POST"])
def action_generate_heats():
    con = db()
    any_results = con.execute("SELECT COUNT(*) FROM raw_results").fetchone()[0]
    con.close()
    if any_results == 0:
        generate_heats(); set_comp_state(CATEGORY_ORDER[0] if CATEGORY_ORDER else "", 1, 1)
    else:
        regenerate_remaining_heats()
    sync_comp_to_broadcast()
    set_competition_status("heats")
    return redirect("/comp/events")

DEFAULT_CATEGORY_ORDER = ["U80", "U90", "U105", "U120", "Womens", "Mens Open"]

@app.route("/comp/action/generate_test", methods=["POST"])
def action_generate_test():
    if not CATEGORY_ORDER:
        CATEGORY_ORDER.extend(DEFAULT_CATEGORY_ORDER)
        _persist_category_order()
    con = db(); cur = con.cursor()
    for t in ["athletes","events","raw_results","heats"]: cur.execute(f"DELETE FROM {t}")
    for cat in CATEGORY_ORDER:
        for i in range(20): cur.execute("INSERT INTO athletes(name,category) VALUES(?,?)", (f"{cat} Athlete {i+1}", cat))
    event_defs = [
        ("Deadlift",      "weight"),
        ("Frame Carry",   "time"),
        ("Sandbag Carry", "distance"),
        ("Log Press",     "reps"),
        ("Atlas Stones",  "object"),
    ]
    _type_map = {
        "reps": ("reps", None, "higher", None),
        "weight": ("weight", None, "higher", None),
        "distance": ("distance", None, "higher", None),
        "time": ("time", None, "lower", None),
        "object": ("objects", "time", "higher", "lower"),
    }
    for i, (name, etype) in enumerate(event_defs, 1):
        cur.execute(
            "INSERT INTO events(name,event_number,event_type,primary_metric,secondary_metric,primary_direction,secondary_direction) VALUES(?,?,?,?,?,?,?)",
            (name, i, etype) + _type_map.get(etype, ("reps", None, "higher", None)),
        )
    con.commit(); con.close()
    _invalidate_results_cache()
    generate_heats(); set_comp_state(CATEGORY_ORDER[0], 1, 1); sync_comp_to_broadcast()
    return redirect("/comp/events")

@app.route("/comp/action/add_athlete", methods=["POST"])
def action_add_athlete():
    name = request.form.get("name","").strip().upper(); category = request.form.get("category","")
    if name and category:
        con = db(); con.execute("INSERT INTO athletes(name,category) VALUES(?,?)",(name,category)); con.commit(); con.close()
        _invalidate_results_cache()
        regenerate_remaining_heats()
        sync_comp_to_broadcast()
    return redirect(f"/comp/athletes?category={category}&focus=add_athlete" if category else "/comp/athletes?focus=add_athlete")

@app.route("/comp/action/delete_athlete", methods=["POST"])
def action_delete_athlete():
    aid = request.form.get("id"); cat = request.form.get("category","")
    if aid:
        con = db(); con.execute("DELETE FROM athletes WHERE id=?",(aid,)); con.commit(); con.close()
        _invalidate_results_cache()
    return redirect(f"/comp/athletes?category={cat}")

@app.route("/comp/action/withdraw_athlete", methods=["POST"])
def action_withdraw_athlete():
    aid = request.form.get("id"); cat = request.form.get("category","")
    reason = request.form.get("reason", "withdrawn")
    if aid:
        con = db()
        con.execute("UPDATE athletes SET status=? WHERE id=?", (reason, aid))
        con.commit(); con.close()
        _invalidate_results_cache()
        regenerate_remaining_heats()
        sync_comp_to_broadcast()
    return redirect(f"/comp/athletes?category={cat}")

@app.route("/comp/action/reinstate_athlete", methods=["POST"])
def action_reinstate_athlete():
    aid = request.form.get("id"); cat = request.form.get("category","")
    if aid:
        con = db()
        con.execute("UPDATE athletes SET status='active' WHERE id=?", (aid,))
        con.commit(); con.close()
        _invalidate_results_cache()
        regenerate_remaining_heats()
        sync_comp_to_broadcast()
    return redirect(f"/comp/athletes?category={cat}")

@app.route("/comp/action/add_event", methods=["POST"])
def action_add_event():
    name       = request.form.get("name","").strip()
    event_type = request.form.get("event_type","reps")
    # Scoring method: pull from SCORING_PRESETS if a preset was chosen,
    # otherwise fall back to EVENT_TYPES defaults for the selected event_type.
    scoring_preset     = request.form.get("scoring_method","")
    preset             = SCORING_PRESETS.get(scoring_preset) or EVENT_TYPES.get(event_type, {})
    primary_metric     = preset.get("primary_metric",    event_type)
    secondary_metric   = preset.get("secondary_metric")  or None
    primary_direction  = preset.get("primary_direction", "higher")
    secondary_direction= preset.get("secondary_direction") or None
    if name:
        con = db(); cur = con.cursor()
        next_num = (con.execute("SELECT MAX(event_number) FROM events").fetchone()[0] or 0) + 1
        cur.execute(
            "INSERT INTO events(name,event_number,event_type,primary_metric,secondary_metric,primary_direction,secondary_direction) VALUES(?,?,?,?,?,?,?)",
            (name, next_num, event_type, primary_metric, secondary_metric, primary_direction, secondary_direction)
        )
        con.commit(); con.close()
        _invalidate_results_cache()
    return redirect("/comp/events?focus=add_event")

@app.route("/comp/action/delete_event", methods=["POST"])
def action_delete_event():
    eid = request.form.get("event_id") or request.form.get("id")
    if eid:
        con = db(); cur = con.cursor()
        cur.execute("DELETE FROM events WHERE id=?", (eid,))
        cur.execute("DELETE FROM raw_results WHERE event_id=?", (eid,))
        # Re-number remaining events
        events = con.execute("SELECT id FROM events ORDER BY event_number").fetchall()
        for i, ev in enumerate(events, 1):
            cur.execute("UPDATE events SET event_number=? WHERE id=?", (i, ev["id"]))
        con.commit(); con.close()
        _invalidate_results_cache()
    return redirect(request.referrer or "/comp/events")

@app.route("/comp/action/update_event_scoring", methods=["POST"])
def action_update_event_scoring():
    """Section 3: Set primary/secondary metric and direction for an event."""
    eid = request.form.get("event_id")
    primary_metric    = request.form.get("primary_metric")
    secondary_metric  = request.form.get("secondary_metric") or None
    primary_direction = request.form.get("primary_direction", "higher")
    secondary_direction = request.form.get("secondary_direction") or None
    if eid:
        event_type = _event_type_from_scoring(primary_metric, secondary_metric)
        con = db()
        con.execute("UPDATE events SET primary_metric=?, secondary_metric=?, primary_direction=?, secondary_direction=?, event_type=? WHERE id=?",
                    (primary_metric, secondary_metric, primary_direction, secondary_direction, event_type, eid))
        con.commit(); con.close()
        _invalidate_results_cache()
    return redirect(request.referrer or "/comp/events")

@app.route("/comp/action/edit_event", methods=["POST"])
def action_edit_event():
    eid        = request.form.get("event_id") or request.form.get("id")
    name       = request.form.get("name","").strip()
    event_type = request.form.get("event_type","reps")
    scoring_preset = request.form.get("scoring_method","")
    if eid:
        con = db()
        if scoring_preset and scoring_preset in SCORING_PRESETS:
            preset = SCORING_PRESETS[scoring_preset]
            con.execute(
                "UPDATE events SET name=?, event_type=?, primary_metric=?, secondary_metric=?, primary_direction=?, secondary_direction=? WHERE id=?",
                (name, event_type, preset["primary_metric"], preset.get("secondary_metric"),
                 preset["primary_direction"], preset.get("secondary_direction"), eid)
            )
        else:
            con.execute("UPDATE events SET name=?, event_type=? WHERE id=?", (name, event_type, eid))
        con.commit(); con.close()
        _invalidate_results_cache()
    return redirect(request.referrer or "/comp/events")

@app.route("/comp/action/add_category", methods=["POST"])
def action_add_category():
    name = request.form.get("name","").strip()
    if name and name not in CATEGORY_ORDER:
        CATEGORY_ORDER.append(name)
        _persist_category_order()
    return redirect("/comp/events?focus=add_category")

@app.route("/comp/action/reorder_category", methods=["POST"])
def action_reorder_category():
    name = request.form.get("name","").strip()
    direction = request.form.get("direction","up").strip().lower()
    if name in CATEGORY_ORDER:
        idx = CATEGORY_ORDER.index(name)
        if direction == "up" and idx > 0:
            CATEGORY_ORDER[idx], CATEGORY_ORDER[idx - 1] = CATEGORY_ORDER[idx - 1], CATEGORY_ORDER[idx]
        elif direction == "down" and idx < len(CATEGORY_ORDER) - 1:
            CATEGORY_ORDER[idx], CATEGORY_ORDER[idx + 1] = CATEGORY_ORDER[idx + 1], CATEGORY_ORDER[idx]
        _persist_category_order()
    return redirect(request.referrer or "/comp/events")

@app.route("/comp/action/delete_category", methods=["POST"])
def action_delete_category():
    name = request.form.get("name","").strip()
    if name in CATEGORY_ORDER:
        was_current = (get_comp_state().get("category") == name)
        CATEGORY_ORDER.remove(name)
        _persist_category_order()
        if was_current:
            new_cat = CATEGORY_ORDER[0] if CATEGORY_ORDER else ""
            set_comp_state(new_cat, 1, 1)
            sync_comp_to_broadcast()
    return redirect(request.referrer or "/comp/events")

@app.route("/comp/action/save_results_run", methods=["POST"])
def action_save_results_run():
    """Same as save_results but called from the run page — supports save_next and save_only modes."""
    cs = get_comp_state()
    event_num = cs["event"]
    con = db(); cur = con.cursor()

    event_row = con.execute("SELECT id FROM events WHERE event_number=?", (event_num,)).fetchone()
    if not event_row:
        con.close()
        return redirect("/comp/run")
    event_id = event_row["id"]

    for key, value in request.form.items():
        if not key.startswith("score_"): continue
        athlete_name = key[6:]
        try:
            raw_value = float(value)
        except (ValueError, TypeError):
            continue
        if not (0.0 <= raw_value <= 99999.0) or raw_value != raw_value:  # reject NaN / out-of-range
            continue
        tiebreak_key = f"tiebreak_{athlete_name}"
        tiebreak = None
        if tiebreak_key in request.form:
            try:
                tiebreak = float(request.form[tiebreak_key])
                if not (0.0 <= tiebreak <= 99999.0) or tiebreak != tiebreak:
                    tiebreak = None
            except (ValueError, TypeError):
                tiebreak = None
        athlete = con.execute("SELECT id FROM athletes WHERE name=?", (athlete_name,)).fetchone()
        if not athlete: continue
        cur.execute("""
            INSERT INTO raw_results(athlete_id, event_id, raw_value, tiebreak, result_type)
            VALUES(?,?,?,?,'score')
            ON CONFLICT(athlete_id, event_id) DO UPDATE SET raw_value=excluded.raw_value, tiebreak=excluded.tiebreak, result_type='score'
        """, (athlete["id"], event_id, raw_value, tiebreak))

    con.commit(); con.close()
    _invalidate_results_cache()
    sync_comp_to_broadcast()

    mode = request.form.get("mode") or request.form.get("action","save_only")
    if mode == "save_next":
        category, event, heat = cs["category"], cs["event"], cs["heat"]
        if heat < get_max_heat(category):
            set_comp_state(category, event, heat+1)
        else:
            idx = CATEGORY_ORDER.index(category) if category in CATEGORY_ORDER else -1
            if idx < len(CATEGORY_ORDER)-1:
                set_comp_state(CATEGORY_ORDER[idx+1], event, 1)
        sync_comp_to_broadcast()

    return redirect(request.referrer or "/comp/run")

@app.route("/comp/run")
def comp_run():
    cs = get_comp_state()
    category, event, heat = cs["category"], cs["event"], cs["heat"]
    lanes = get_heat_lanes(category, heat)
    # If no heats at all, auto-generate when we have athletes and events (guided workflow)
    if not get_all_heats():
        con = db()
        has_athletes = con.execute("SELECT COUNT(*) FROM athletes WHERE status='active'").fetchone()[0] > 0
        has_events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0
        con.close()
        if has_athletes and has_events:
            generate_heats()
            if CATEGORY_ORDER:
                set_comp_state(CATEGORY_ORDER[0], 1, 1)
            sync_comp_to_broadcast()
            cs = get_comp_state()
            category, event, heat = cs["category"], cs["event"], cs["heat"]
            lanes = get_heat_lanes(category, heat)
    elif category and not lanes:
        con = db()
        has_athletes = con.execute("SELECT COUNT(*) FROM athletes WHERE category=? AND status='active'", (category,)).fetchone()[0] > 0
        con.close()
        if has_athletes:
            generate_heats()
            lanes = get_heat_lanes(category, heat)
            sync_comp_to_broadcast()
    # Heats exist but state has empty/wrong category (e.g. after Clear all) — show first category, heat 1
    if get_all_heats() and not lanes and CATEGORY_ORDER:
        if not category or category not in CATEGORY_ORDER:
            set_comp_state(CATEGORY_ORDER[0], 1, 1)
            sync_comp_to_broadcast()
            cs = get_comp_state()
            category, event, heat = cs["category"], cs["event"], cs["heat"]
            lanes = get_heat_lanes(category, heat)
    con = db()
    event_row  = con.execute(
        "SELECT id, name, event_type, primary_metric, secondary_metric FROM events WHERE event_number=?",
        (event,)
    ).fetchone()
    if event_row:
        event_row = dict(event_row)
    all_events = con.execute("SELECT * FROM events ORDER BY event_number").fetchall()
    total_events = len(all_events)
    # Next event name for banner
    next_event_row = con.execute("SELECT name FROM events WHERE event_number=?", (event+1,)).fetchone()
    next_event_name = next_event_row["name"] if next_event_row else ""
    con.close()

    event_type   = event_row["event_type"] if event_row else "reps"
    event_name   = event_row["name"]       if event_row else f"Event {event}"
    event_id_run = event_row["id"]         if event_row else None
    # For UI: show inputs for primary and optionally secondary metric (e.g. distance+time, objects+time)
    et_def = EVENT_TYPES.get(event_type, {})
    event_metric = (event_row.get("primary_metric") or et_def.get("primary_metric") or event_type) if event_row else (et_def.get("primary_metric") or "reps")
    event_secondary_metric = event_row.get("secondary_metric") or et_def.get("secondary_metric") if event_row else et_def.get("secondary_metric")
    if event_secondary_metric and not isinstance(event_secondary_metric, str):
        event_secondary_metric = None

    scores = {}; tiebreaks = {}; result_types = {}
    for l in lanes:
        raw = get_raw_result(l["athlete_name"], event_id_run) if event_id_run else None
        scores[l["athlete_name"]]       = raw["raw_value"]   if raw else ""
        tiebreaks[l["athlete_name"]]    = raw["tiebreak"]    if raw else ""
        result_types[l["athlete_name"]] = (raw["result_type"] if raw else "score") or "score"

    max_heat    = get_max_heat(category)
    is_last_heat = (heat >= max_heat)
    next_lanes  = get_heat_lanes(category, heat+1)

    # Next category (when moving past last heat)
    cat_idx      = CATEGORY_ORDER.index(category) if category in CATEGORY_ORDER else -1
    next_category = CATEGORY_ORDER[cat_idx+1] if cat_idx >= 0 and cat_idx < len(CATEGORY_ORDER)-1 else None

    # Leaderboard — filter by lb_cat query param
    lb_cat = request.args.get("lb_cat", category)
    if lb_cat == "all":
        leaderboard = get_leaderboard(category=None)
    else:
        leaderboard = get_leaderboard(category=lb_cat)

    # Event winner — only when every active (non-withdrawn) competitor has a result for this event
    winner = None
    if event_id_run:
        con2 = db()
        total_active = con2.execute(
            "SELECT COUNT(*) FROM athletes WHERE category=? AND status='active'", (category,)
        ).fetchone()[0]
        results_entered = con2.execute(
            "SELECT COUNT(*) FROM raw_results rr "
            "JOIN athletes a ON a.id=rr.athlete_id "
            "WHERE rr.event_id=? AND a.category=? AND a.status='active'",
            (event_id_run, category)
        ).fetchone()[0]
        con2.close()
        if total_active > 0 and results_entered >= total_active and leaderboard:
            winner = leaderboard[0]

    if get_all_heats():
        set_competition_status("live")

    return render_template("comp_run.html",
        state=cs,
        lanes=lanes,
        current_lanes=lanes,          # template uses current_lanes
        scores=scores,
        tiebreaks=tiebreaks,
        event_name=event_name,
        event_type=event_type,
        event_metric=event_metric,
        event_secondary_metric=event_secondary_metric,
        all_events=all_events,
        total_events=total_events,
        next_event_name=next_event_name,
        category_order=CATEGORY_ORDER,
        event_types=EVENT_TYPES,
        leaderboard=leaderboard,
        next_lanes=next_lanes,
        next_category=next_category,
        max_heat=max_heat,
        is_last_heat=is_last_heat,
        winner=winner,
        lb_cat=lb_cat,
        cat_colors=CAT_COLORS,
        result_types=result_types,
        broadcast_mode=get_broadcast_mode())

@app.route("/comp/action/jump_to", methods=["POST"])
def action_jump_to():
    category = request.form.get("category", CATEGORY_ORDER[0] if CATEGORY_ORDER else "")
    event    = _safe_int(request.form.get("event"), 1, 1, 999)
    heat     = _safe_int(request.form.get("heat"), 1, 1, 9999)
    set_comp_state(category, event, heat)
    sync_comp_to_broadcast()
    return redirect(request.referrer or "/comp/run")

@app.route("/comp/action/clear_all", methods=["POST"])
def action_clear_all():
    con = db(); cur = con.cursor()
    for t in ["raw_results", "heats", "events", "athletes"]:
        cur.execute(f"DELETE FROM {t}")
    con.commit(); con.close()
    _invalidate_results_cache()
    CATEGORY_ORDER.clear()
    _persist_category_order()
    set_comp_state("", 1, 1)
    sync_comp_to_broadcast()
    return redirect(request.referrer or "/comp/events")

@app.route("/comp/action/simulate_results", methods=["POST"])
def action_simulate_results():
    import random
    con = db(); cur = con.cursor()
    cur.execute("DELETE FROM raw_results")
    events = con.execute("SELECT id, event_number, event_type FROM events ORDER BY event_number").fetchall()
    athletes = con.execute("SELECT id, category FROM athletes WHERE status='active'").fetchall()
    for ev in events:
        for a in athletes:
            if ev["event_type"] == "reps":
                val = random.randint(1, 15); tb = None
            elif ev["event_type"] == "weight":
                val = random.choice([100,120,140,160,180,200,220,240,260,280,300]); tb = None
            elif ev["event_type"] == "distance":
                val = round(random.uniform(5.0, 25.0), 1); tb = None
            elif ev["event_type"] == "time":
                val = round(random.uniform(8.0, 30.0), 2); tb = None
            elif ev["event_type"] == "object":
                val = random.randint(0, 6); tb = round(random.uniform(10.0, 60.0), 2)
            else:
                val = random.randint(1, 10); tb = None
            cur.execute("""
                INSERT INTO raw_results(athlete_id, event_id, raw_value, tiebreak)
                VALUES(?,?,?,?)
                ON CONFLICT(athlete_id, event_id) DO UPDATE SET raw_value=excluded.raw_value, tiebreak=excluded.tiebreak
            """, (a["id"], ev["id"], val, tb))
    con.commit(); con.close()
    _invalidate_results_cache()
    sync_comp_to_broadcast()
    return redirect(request.referrer or "/comp/events")

@app.route("/comp/action/push_to_broadcast", methods=["POST"])
def action_push_to_broadcast():
    sync_comp_to_broadcast()
    return redirect(request.referrer or "/comp/events")

@app.route("/comp/action/next_event", methods=["POST"])
def action_next_event():
    cs = get_comp_state()
    con = db()
    max_event = con.execute("SELECT MAX(event_number) FROM events").fetchone()[0] or 1
    con.close()
    next_event = min(cs["event"] + 1, max_event)
    try: create_backup()
    except Exception: pass
    set_comp_state(CATEGORY_ORDER[0] if CATEGORY_ORDER else "", next_event, 1)
    generate_heats_for_next_event(next_event)
    sync_comp_to_broadcast()
    return redirect("/comp/run")

# ─── SECTION 2: EXTERNAL API ADAPTERS ────────────────────────────────────────

def normalize_to_broadcast(athletes_raw, event_name="", category=""):
    """Normalise external API athlete data into internal broadcast format."""
    normalized = []
    for item in athletes_raw:
        normalized.append({
            "name":     (item.get("name") or item.get("athlete_name") or item.get("fullName") or "").upper().strip(),
            "category": (item.get("category") or item.get("class") or item.get("division") or category),
            "score":    str(item.get("score") or item.get("points") or item.get("total") or "0"),
            "rank":     item.get("rank") or item.get("ranking") or item.get("position") or 0,
            "origin":   (item.get("origin") or item.get("country") or item.get("club") or ""),
        })
    normalized.sort(key=lambda x: -float(x["score"] or 0))
    return normalized

def _fetch_external(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "StrongmanBroadcast/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def _push_to_broadcast_if_api(normalized, event_name, category):
    if get_broadcast_mode() == "api":
        merge_state({
            "athletes": normalized, "eventName": event_name.upper(), "compCategory": category,
            "broadcast": {"data_source_mode": "api", "athletes": normalized, "eventName": event_name.upper()},
        })

@app.route("/api/adapters/strengthresults")
def adapter_strengthresults():
    """Adapter for StrengthResults-style APIs. Expects {"results":[...]} or {"athletes":[...]} or bare list."""
    url = request.args.get("url",""); category = request.args.get("category",""); event_name = request.args.get("event","")
    if not url: return jsonify({"error": "url parameter required"}), 400
    try:
        data = _fetch_external(url)
        raw  = data.get("results") or data.get("athletes") or (data if isinstance(data, list) else [])
        normalized = normalize_to_broadcast(raw, event_name=event_name, category=category)
        _push_to_broadcast_if_api(normalized, event_name, category)
        resp = jsonify({"athletes": normalized, "source": "strengthresults", "count": len(normalized)})
        resp.headers["Access-Control-Allow-Origin"] = "*"; return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/adapters/ironpodium")
def adapter_ironpodium():
    """Adapter for IronPodium-style APIs. Expects {"leaderboard":[...]} or {"standings":[...]}"""
    url = request.args.get("url",""); category = request.args.get("category",""); event_name = request.args.get("event","")
    if not url: return jsonify({"error": "url parameter required"}), 400
    try:
        data = _fetch_external(url)
        raw  = data.get("leaderboard") or data.get("standings") or data.get("results") or []
        normalized = normalize_to_broadcast(raw, event_name=event_name, category=category)
        _push_to_broadcast_if_api(normalized, event_name, category)
        resp = jsonify({"athletes": normalized, "source": "ironpodium", "count": len(normalized)})
        resp.headers["Access-Control-Allow-Origin"] = "*"; return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/adapters/custom")
def adapter_custom():
    """Generic adapter. Pass name_field, score_field, category_field, rank_field, data_path as query params."""
    url = request.args.get("url",""); category = request.args.get("category",""); event_name = request.args.get("event","")
    name_field = request.args.get("name_field","name"); score_field = request.args.get("score_field","score")
    category_field = request.args.get("category_field","category"); rank_field = request.args.get("rank_field","rank")
    data_path = request.args.get("data_path","")
    if not url: return jsonify({"error": "url parameter required"}), 400
    try:
        data = _fetch_external(url)
        raw  = data
        if data_path:
            for key in data_path.split("."): raw = raw.get(key, {}) if isinstance(raw, dict) else raw
        if not isinstance(raw, list): raw = []
        remapped = [{"name": item.get(name_field,""), "score": item.get(score_field,0),
                     "category": item.get(category_field,category), "rank": item.get(rank_field,0),
                     "origin": item.get("origin","")} for item in raw]
        normalized = normalize_to_broadcast(remapped, event_name=event_name, category=category)
        _push_to_broadcast_if_api(normalized, event_name, category)
        resp = jsonify({"athletes": normalized, "source": "custom", "count": len(normalized)})
        resp.headers["Access-Control-Allow-Origin"] = "*"; return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── DNS / DNF ────────────────────────────────────────────────────────────────
@app.route("/comp/action/set_result_type", methods=["POST"])
def action_set_result_type():
    """Mark an athlete's result for the current event as dns, dnf, or score (clears flag)."""
    athlete_name = request.form.get("athlete_name", "").strip()
    result_type  = request.form.get("result_type", "score").lower()
    if result_type not in ("score", "dns", "dnf"):
        result_type = "score"
    cs = get_comp_state()
    con = db(); cur = con.cursor()
    ev = con.execute("SELECT id FROM events WHERE event_number=?", (cs["event"],)).fetchone()
    if not ev:
        con.close(); return redirect(request.referrer or "/comp/run")
    ath = con.execute("SELECT id FROM athletes WHERE name=?", (athlete_name,)).fetchone()
    if not ath:
        con.close(); return redirect(request.referrer or "/comp/run")
    cur.execute("""
        INSERT INTO raw_results(athlete_id, event_id, raw_value, tiebreak, result_type)
        VALUES(?,?,0,NULL,?)
        ON CONFLICT(athlete_id, event_id) DO UPDATE SET result_type=excluded.result_type
    """, (ath["id"], ev["id"], result_type))
    con.commit(); con.close()
    _invalidate_results_cache()
    sync_comp_to_broadcast()
    return redirect(request.referrer or "/comp/run")

@app.route("/judge/set_status", methods=["POST"])
def judge_set_status():
    """Non-authed endpoint for lane judges to mark DNS/DNF from their panel."""
    data = request.get_json(silent=True) or {}
    athlete_name = str(data.get("athlete_name", "")).strip()
    result_type  = str(data.get("result_type", "score")).lower()
    if result_type not in ("score", "dns", "dnf"):
        result_type = "score"
    if not athlete_name:
        return jsonify({"ok": False, "error": "no athlete_name"}), 400
    cs = get_comp_state()
    con = db(); cur = con.cursor()
    ev = con.execute("SELECT id FROM events WHERE event_number=?", (cs["event"],)).fetchone()
    if not ev:
        con.close(); return jsonify({"ok": False, "error": "no event"}), 400
    ath = con.execute("SELECT id FROM athletes WHERE name=?", (athlete_name,)).fetchone()
    if not ath:
        con.close(); return jsonify({"ok": False, "error": "athlete not found"}), 400
    cur.execute("""
        INSERT INTO raw_results(athlete_id, event_id, raw_value, tiebreak, result_type)
        VALUES(?,?,0,NULL,?)
        ON CONFLICT(athlete_id, event_id) DO UPDATE SET result_type=excluded.result_type
    """, (ath["id"], ev["id"], result_type))
    con.commit(); con.close()
    _invalidate_results_cache()
    sync_comp_to_broadcast()
    return jsonify({"ok": True, "result_type": result_type})

# ─── PUBLIC RESULTS PAGE ──────────────────────────────────────────────────────
@app.route("/results")
@app.route("/results/")
def public_results_default():
    cat = CATEGORY_ORDER[0] if CATEGORY_ORDER else ""
    return redirect(f"/results/{cat}" if cat else "/results/_")

@app.route("/results/<path:category>")
def public_results(category):
    events = get_public_events()
    try:
        rows, _ = get_leaderboard_detailed(category)
    except Exception:
        rows = []
    return render_template("comp_results_public.html",
        events=events, rows=rows, selected_category=category,
        category_order=CATEGORY_ORDER, cat_colors=CAT_COLORS,
        total_events=len(events))

# ─── JSON API ─────────────────────────────────────────────────────────────────
@app.route("/comp/api/state")
def api_comp_state():
    resp = jsonify(get_comp_state()); resp.headers["Access-Control-Allow-Origin"] = "*"; return resp

@app.route("/comp/api/heat")
def api_current_heat():
    cs = get_comp_state()
    resp = jsonify({"lanes": get_heat_lanes(cs["category"],cs["heat"]), "state": cs})
    resp.headers["Access-Control-Allow-Origin"] = "*"; return resp

@app.route("/comp/api/leaderboard")
def api_leaderboard():
    cat = request.args.get("category","")
    resp = jsonify(get_leaderboard(category=cat if cat else None))
    resp.headers["Access-Control-Allow-Origin"] = "*"; return resp

@app.route("/comp/api/event_points")
def api_event_points():
    """Returns calculated points breakdown for a category + event."""
    cat      = request.args.get("category","")
    event_id = _safe_int(request.args.get("event_id"), 0, 1, 999999)
    if not cat or event_id <= 0:
        return jsonify({"error": "category and event_id (positive integer) required"}), 400
    pts = calculate_event_points(event_id, cat)
    # Convert keys to strings for JSON
    resp = jsonify({str(k): v for k,v in pts.items()})
    resp.headers["Access-Control-Allow-Origin"] = "*"; return resp

# ─── STARTUP INITIALIZATION ───────────────────────────────────────────────────
# These functions are called at module load time so they execute under both
# `python server.py` (direct) and `gunicorn server:app` (production).
# Previously all of this lived inside `if __name__ == "__main__":` which
# Gunicorn never executes, causing silent failures on every production start.

def _init_state_file():
    """Create state.json with safe defaults if missing; backfill any keys added
    since the file was last written. Idempotent — safe to call on every start."""
    if not os.path.exists(STATE_FILE):
        save_state({
            "competition": {},
            "competition_config": _DEFAULT_COMP_CONFIG.copy(),
            "broadcast": {"data_source_mode": "engine"},
            "data_source_mode": "engine",
        })
        return
    try:
        s = load_state()
        changed = False
        if "broadcast" not in s:
            s["broadcast"] = {"data_source_mode": s.get("data_source_mode", "engine")}
            changed = True
        if "competition" not in s:
            s["competition"] = {}
            changed = True
        if "data_source_mode" not in s:
            s["data_source_mode"] = "engine"
            changed = True
        if "competition_config" not in s:
            s["competition_config"] = _DEFAULT_COMP_CONFIG.copy()
            changed = True
        else:
            for k, v in _DEFAULT_COMP_CONFIG.items():
                if k not in s["competition_config"]:
                    s["competition_config"][k] = v
                    changed = True
        if changed:
            save_state(s)
    except Exception:
        pass


def _migrate_templates():
    """One-time migration: move any root-level HTML templates that were created
    before the templates/ subdirectory existed. Safe no-op if already done."""
    os.makedirs(TPL_DIR, exist_ok=True)
    for f in ["comp_home.html", "comp_heats.html", "comp_callroom.html", "comp_arena.html",
              "comp_results.html", "comp_leaderboard.html", "comp_athletes.html"]:
        src = os.path.join(DIR, f)
        dst = os.path.join(TPL_DIR, f)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.move(src, dst)


def _startup():
    """
    Initialize the application. Called unconditionally at module level so it
    runs under both `python server.py` and `gunicorn server:app`.

    Execution order:
      1. chdir to the app directory so relative paths work regardless of how
         the process was launched.
      2. Migrate any root-level templates still in the wrong place.
      3. Ensure the SQLite schema exists and is up to date.
      4. Ensure state.json exists with all required keys.
      5. Restore the persisted category order into CATEGORY_ORDER.
      6. Push the initial competition state to broadcast (best-effort).

    All steps are idempotent — calling _startup() more than once is safe.
    """
    os.chdir(DIR)
    _migrate_templates()
    ensure_schema()
    _init_state_file()
    try:
        _restore_category_order()
    except Exception:
        pass
    try:
        sync_comp_to_broadcast()
    except Exception:
        pass


_startup()

# ─── BOOT ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  STRONGMAN UNIFIED SERVER")
    print("=" * 55)
    print("  Home:         http://localhost:8080/")
    print("  Broadcast:    http://localhost:8080/control.html")
    print("  Competition:  http://localhost:8080/comp/")
    print("=" * 55)
    app.run(host="0.0.0.0", port=8080, debug=False)
