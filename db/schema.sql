-- Placement Week Scheduler — DB schema
--
-- Design principle: this DB is the ONLY source of truth for schedule state.
-- The scheduler, the replan engine, and the Streamlit dashboard all read/write
-- through db/store.py, never through raw pandas/CSVs. That's what makes
-- "one-click replan" trustworthy in the dashboard later.

CREATE TABLE IF NOT EXISTS companies (
    company_id              TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    tier                    TEXT NOT NULL,
    day                     INTEGER NOT NULL,
    cgpa_cutoff             REAL NOT NULL,
    target_shortlist_size   INTEGER NOT NULL,
    panels                  INTEGER NOT NULL,
    interview_duration_min  INTEGER NOT NULL,
    priority_tier           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS students (
    student_id  TEXT PRIMARY KEY,
    cgpa        REAL NOT NULL,
    branch      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    room_id     TEXT PRIMARY KEY,
    room_type   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS room_unavailability (
    room_id  TEXT NOT NULL,
    day      INTEGER NOT NULL,
    reason   TEXT,
    PRIMARY KEY (room_id, day)
);

CREATE TABLE IF NOT EXISTS shortlists (
    company_id  TEXT NOT NULL,
    student_id  TEXT NOT NULL,
    PRIMARY KEY (company_id, student_id)
);

-- Which room each company's panel sits in for the whole day.
-- This is what makes room/panel double-booking a single constraint:
-- once bound here, a track (company_id, panel_num) IS a room, for the day.
CREATE TABLE IF NOT EXISTS panel_room_assignment (
    company_id  TEXT NOT NULL,
    panel_num   INTEGER NOT NULL,
    room_id     TEXT NOT NULL,
    day         INTEGER NOT NULL,
    PRIMARY KEY (company_id, panel_num)
);

-- The actual schedule. status is used from Phase 3 onward (replan can mark
-- an interview 'cancelled' without deleting history).
CREATE TABLE IF NOT EXISTS interviews (
    interview_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id     TEXT NOT NULL,
    student_id     TEXT NOT NULL,
    day            INTEGER NOT NULL,
    room_id        TEXT NOT NULL,
    panel_num      INTEGER NOT NULL,
    start_min      INTEGER NOT NULL,   -- minutes from midnight (540 = 9:00am)
    end_min        INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled | cancelled
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- Every interview that could NOT be placed, with a specific, queryable reason.
CREATE TABLE IF NOT EXISTS unscheduled_log (
    log_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id     TEXT NOT NULL,
    student_id     TEXT NOT NULL,
    day            INTEGER NOT NULL,
    reason_code    TEXT NOT NULL,   -- NO_ROOM_FOR_COMPANY | COMPANY_CAPACITY_EXHAUSTED | STUDENT_TIME_CONFLICT
    reason_detail  TEXT,
    logged_at      TEXT NOT NULL
);

-- Placeholder for Phase 3 — replan history, so the dashboard can show
-- "what changed" after each disruption.
CREATE TABLE IF NOT EXISTS replan_log (
    replan_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp            TEXT NOT NULL,
    disruption_type       TEXT NOT NULL,
    disruption_details    TEXT,   -- JSON
    changes               TEXT,   -- JSON: list of {interview_id, field, old, new}
    affected_students      TEXT,   -- JSON list of student_ids to notify
    churn_count            INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_interviews_day ON interviews(day);
CREATE INDEX IF NOT EXISTS idx_interviews_student ON interviews(student_id, day);
CREATE INDEX IF NOT EXISTS idx_interviews_company ON interviews(company_id);
CREATE INDEX IF NOT EXISTS idx_shortlists_company ON shortlists(company_id);
