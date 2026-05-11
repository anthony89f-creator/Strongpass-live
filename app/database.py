import sqlite3
from app.config import DB_FILE


def db():
    con = sqlite3.connect(DB_FILE, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def ensure_schema():
    con = db(); cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS athletes (id INTEGER PRIMARY KEY, name TEXT, category TEXT, status TEXT DEFAULT 'active');
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, name TEXT, event_number INTEGER, event_type TEXT DEFAULT 'reps');
        CREATE TABLE IF NOT EXISTS heats (id INTEGER PRIMARY KEY, event_id INTEGER, category TEXT, heat_number INTEGER, lane INTEGER, athlete_name TEXT);
        CREATE TABLE IF NOT EXISTS raw_results (id INTEGER PRIMARY KEY, athlete_id INTEGER, event_id INTEGER, raw_value REAL, tiebreak REAL, UNIQUE(athlete_id, event_id));
        CREATE TABLE IF NOT EXISTS competition_state (id INTEGER PRIMARY KEY, category TEXT, event INTEGER, heat INTEGER);
    """)
    # Migrate: add event_type column if it doesn't exist yet
    try:
        cur.execute("ALTER TABLE events ADD COLUMN event_type TEXT DEFAULT 'reps'")
        con.commit()
    except: pass
    # Migrate: add tiebreak column to raw_results if missing
    try:
        cur.execute("ALTER TABLE raw_results ADD COLUMN tiebreak REAL")
        con.commit()
    except: pass
    # Migrate: add result_type column (score | dns | dnf)
    try:
        cur.execute("ALTER TABLE raw_results ADD COLUMN result_type TEXT DEFAULT 'score'")
        con.commit()
    except: pass
    # Add extended scoring structure columns
    for col_sql in [
        "ALTER TABLE events ADD COLUMN primary_metric TEXT",
        "ALTER TABLE events ADD COLUMN secondary_metric TEXT",
        "ALTER TABLE events ADD COLUMN primary_direction TEXT DEFAULT 'higher'",
        "ALTER TABLE events ADD COLUMN secondary_direction TEXT",
    ]:
        try: cur.execute(col_sql); con.commit()
        except: pass
    # Backfill from event_type for existing rows
    _type_map = {
        "reps":     ("reps",     None,   "higher", None),
        "weight":   ("weight",   None,   "higher", None),
        "distance": ("distance", None,   "higher", None),
        "time":     ("time",     None,   "lower",  None),
        "object":   ("objects",  "time", "higher", "lower"),
    }
    for _etype, (_pm, _sm, _pd, _sd) in _type_map.items():
        cur.execute(
            "UPDATE events SET primary_metric=?, secondary_metric=?, "
            "primary_direction=?, secondary_direction=? "
            "WHERE event_type=? AND primary_metric IS NULL",
            (_pm, _sm, _pd, _sd, _etype)
        )
    con.commit(); con.close()
