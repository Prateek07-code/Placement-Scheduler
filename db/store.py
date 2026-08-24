"""
db/store.py — the only module allowed to run raw SQL in this project.

Everything else (scheduler, replan engine, dashboard) calls functions here.
This is a deliberate boundary: it's what lets the Streamlit dashboard and the
scheduler share state safely, and it's what makes replan's "diff" possible
later (we can always ask the DB "what did this interview look like before").
"""

import sqlite3
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn):
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()


def load_static_data(conn, data_dir):
    """Load the Phase 1 generator output (companies/students/rooms/shortlists)
    into the DB. Idempotent-ish: clears these tables first so re-running the
    pipeline from a fresh CSV set doesn't duplicate rows."""
    data_dir = Path(data_dir)

    companies = pd.read_csv(data_dir / "companies.csv")
    students = pd.read_csv(data_dir / "students.csv")
    rooms = pd.read_csv(data_dir / "rooms.csv")
    room_unavail = pd.read_csv(data_dir / "room_unavailability.csv")
    shortlists = pd.read_csv(data_dir / "shortlists.csv")

    cur = conn.cursor()
    for tbl in ["companies", "students", "rooms", "room_unavailability", "shortlists"]:
        cur.execute(f"DELETE FROM {tbl}")

    companies.to_sql("companies", conn, if_exists="append", index=False)
    students.to_sql("students", conn, if_exists="append", index=False)
    rooms.to_sql("rooms", conn, if_exists="append", index=False)
    room_unavail.to_sql("room_unavailability", conn, if_exists="append", index=False)
    shortlists.to_sql("shortlists", conn, if_exists="append", index=False)
    conn.commit()


def reset_schedule(conn):
    """Wipe schedule state (interviews, room/panel assignments, logs) while
    keeping static data (companies/students/rooms/shortlists) intact. Used
    to re-run the scheduler cleanly during dev, and by the dashboard's
    'rebuild from scratch' escape hatch."""
    cur = conn.cursor()
    for tbl in ["interviews", "panel_room_assignment", "unscheduled_log", "replan_log"]:
        cur.execute(f"DELETE FROM {tbl}")
    conn.commit()


# ---------- reads ----------

def get_companies(conn, day=None):
    q = "SELECT * FROM companies"
    params = ()
    if day is not None:
        q += " WHERE day = ?"
        params = (int(day),)
    q += " ORDER BY priority_tier DESC, company_id ASC"
    return pd.read_sql_query(q, conn, params=params)


def get_shortlist(conn, company_id):
    """Students shortlisted by a company, joined with CGPA for ordering."""
    q = """
        SELECT s.student_id, s.cgpa, s.branch
        FROM shortlists sl
        JOIN students s ON s.student_id = sl.student_id
        WHERE sl.company_id = ?
        ORDER BY s.cgpa DESC, s.student_id ASC
    """
    return pd.read_sql_query(q, conn, params=(company_id,))


def get_rooms(conn):
    return pd.read_sql_query("SELECT * FROM rooms ORDER BY room_type ASC, room_id ASC", conn)


def get_unavailable_rooms(conn, day):
    # Defensive cast: callers sometimes pass numpy.int64 (e.g. from
    # df["day"].unique()), which SQLite's param binding does not reliably
    # match against an INTEGER column -- silently returning zero rows
    # instead of erroring. Cast to a plain Python int at this boundary so
    # every caller is protected, not just the ones that remember to.
    day = int(day)
    q = "SELECT room_id FROM room_unavailability WHERE day = ?"
    return set(pd.read_sql_query(q, conn, params=(day,))["room_id"])


def get_student_day_bookings(conn, student_id, day):
    """All (start_min, end_min) intervals a student is already booked into
    on a given day, across ALL companies. This is the core conflict check."""
    day = int(day)
    q = """
        SELECT start_min, end_min FROM interviews
        WHERE student_id = ? AND day = ? AND status = 'scheduled'
    """
    rows = conn.execute(q, (student_id, day)).fetchall()
    return [(r["start_min"], r["end_min"]) for r in rows]


def get_schedule_df(conn, day=None):
    q = """
        SELECT i.interview_id, i.company_id, c.name AS company_name, i.student_id,
               i.day, i.room_id, i.panel_num, i.start_min, i.end_min, i.status
        FROM interviews i
        JOIN companies c ON c.company_id = i.company_id
        WHERE i.status = 'scheduled'
    """
    params = ()
    if day is not None:
        q += " AND i.day = ?"
        params = (int(day),)
    q += " ORDER BY i.day, i.start_min, i.room_id"
    return pd.read_sql_query(q, conn, params=params)


def get_unscheduled_df(conn, day=None):
    q = "SELECT * FROM unscheduled_log"
    params = ()
    if day is not None:
        q += " WHERE day = ?"
        params = (int(day),)
    return pd.read_sql_query(q, conn, params=params)


# ---------- writes ----------

def assign_panel_room(conn, company_id, panel_num, room_id, day):
    conn.execute(
        "INSERT INTO panel_room_assignment (company_id, panel_num, room_id, day) VALUES (?, ?, ?, ?)",
        (company_id, panel_num, room_id, int(day)),
    )


def get_panel_room_assignments(conn, company_id):
    q = "SELECT panel_num, room_id, day FROM panel_room_assignment WHERE company_id = ?"
    rows = conn.execute(q, (company_id,)).fetchall()
    return [(r["panel_num"], r["room_id"], r["day"]) for r in rows]


def insert_interview(conn, company_id, student_id, day, room_id, panel_num, start_min, end_min):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO interviews
           (company_id, student_id, day, room_id, panel_num, start_min, end_min,
            status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)""",
        (company_id, student_id, int(day), room_id, panel_num, start_min, end_min, now, now),
    )
    return cur.lastrowid


def log_unscheduled(conn, company_id, student_id, day, reason_code, reason_detail=""):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO unscheduled_log
           (company_id, student_id, day, reason_code, reason_detail, logged_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (company_id, student_id, int(day), reason_code, reason_detail, now),
    )
