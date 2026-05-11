import os

# Project root — two dirname levels up from app/config.py
DIR         = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL_DIR     = os.path.join(DIR, "templates")
STATE_FILE  = os.path.join(DIR, "state.json")
DB_FILE     = os.path.join(DIR, "comp.db")
BACKUPS_DIR = os.path.join(DIR, "backups")

LANES = 4

CAT_COLORS = {
    "U80":       "#CC0044",
    "U90":       "#D4A017",
    "U105":      "#0088CC",
    "U120":      "#00AA44",
    "Womens":    "#CC0088",
    "Mens Open": "#8800CC",
}

# Supported event types: higher=True → highest value wins; higher=False → lowest wins (time)
EVENT_TYPES = {
    "reps":     {"label": "Reps",     "unit": "reps", "higher": True,  "primary_metric": "reps",     "secondary_metric": None,   "primary_direction": "higher", "secondary_direction": None},
    "weight":   {"label": "Weight",   "unit": "kg",   "higher": True,  "primary_metric": "weight",   "secondary_metric": None,   "primary_direction": "higher", "secondary_direction": None},
    "distance": {"label": "Distance", "unit": "m",    "higher": True,  "primary_metric": "distance", "secondary_metric": None,   "primary_direction": "higher", "secondary_direction": None},
    "time":     {"label": "Time",     "unit": "s",    "higher": False, "primary_metric": "time",     "secondary_metric": None,   "primary_direction": "lower",  "secondary_direction": None},
    "object":   {"label": "Objects",  "unit": "objs", "higher": True,  "primary_metric": "objects",  "secondary_metric": "time", "primary_direction": "higher", "secondary_direction": "lower"},
}

SCORING_PRESETS = {
    "max_weight":    {"primary_metric": "weight",   "secondary_metric": None,   "primary_direction": "higher", "secondary_direction": None},
    "reps":          {"primary_metric": "reps",     "secondary_metric": None,   "primary_direction": "higher", "secondary_direction": None},
    "distance":      {"primary_metric": "distance", "secondary_metric": None,   "primary_direction": "higher", "secondary_direction": None},
    "time":          {"primary_metric": "time",     "secondary_metric": None,   "primary_direction": "lower",  "secondary_direction": None},
    "objects_time":  {"primary_metric": "objects",  "secondary_metric": "time", "primary_direction": "higher", "secondary_direction": "lower"},
    "distance_time": {"primary_metric": "distance", "secondary_metric": "time", "primary_direction": "higher", "secondary_direction": "lower"},
}

DEFAULT_CATEGORY_ORDER = ["U80", "U90", "U105", "U120", "Womens", "Mens Open"]

UPDATE_MAX_BODY = 512 * 1024  # 512 KB cap on /update to prevent DoS

_SAFE_STATIC_EXTS = {
    ".html", ".css", ".js", ".png", ".jpg", ".jpeg",
    ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".mp4", ".webm",
}

_DEFAULT_COMP_CONFIG = {
    "lanes": 4,
    "scoring_mode": "fixed_points",
    "heat_order_mode": "previous_event",
    "final_event_order_mode": "leaderboard",
}
