"""
tests/test_scheduler.py

These tests prove the THREE hard constraints from the assignment brief hold,
not just that the scheduler runs without crashing:
  1. No student is double-booked (overlapping interviews same day).
  2. No room is double-booked (two different company/panel interviews
     overlapping in the same room).
  3. No panel is double-booked (implied by #2 given our room-binding design,
     but tested independently in case that invariant is ever broken by a
     future change).

Plus: every shortlisted (company, student) pair is accounted for -- either
scheduled exactly once, or logged in unscheduled_log exactly once. Nothing
should vanish silently.
"""

import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import store
from scheduler.core import run_scheduler

DATA_DIR = Path(__file__).parent.parent / "data"


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    c = store.get_connection(str(db_path))
    store.init_schema(c)
    store.load_static_data(c, DATA_DIR)
    run_scheduler(c)
    yield c
    c.close()


def test_no_student_double_booking(conn):
    sched = store.get_schedule_df(conn)
    for (student_id, day), grp in sched.groupby(["student_id", "day"]):
        intervals = sorted(zip(grp.start_min, grp.end_min))
        for i in range(len(intervals) - 1):
            assert intervals[i][1] <= intervals[i + 1][0], (
                f"Student {student_id} double-booked on day {day}: "
                f"{intervals[i]} overlaps {intervals[i+1]}"
            )


def test_no_room_double_booking(conn):
    sched = store.get_schedule_df(conn)
    for (room_id, day), grp in sched.groupby(["room_id", "day"]):
        intervals = sorted(zip(grp.start_min, grp.end_min))
        for i in range(len(intervals) - 1):
            assert intervals[i][1] <= intervals[i + 1][0], (
                f"Room {room_id} double-booked on day {day}: "
                f"{intervals[i]} overlaps {intervals[i+1]}"
            )


def test_no_panel_double_booking(conn):
    sched = store.get_schedule_df(conn)
    for (company_id, panel_num, day), grp in sched.groupby(["company_id", "panel_num", "day"]):
        intervals = sorted(zip(grp.start_min, grp.end_min))
        for i in range(len(intervals) - 1):
            assert intervals[i][1] <= intervals[i + 1][0], (
                f"Panel {panel_num} of {company_id} double-booked on day {day}"
            )


def test_every_shortlist_entry_accounted_for(conn):
    shortlists = pytest.importorskip("pandas").read_sql_query(
        "SELECT company_id, student_id FROM shortlists", conn
    )
    sched = store.get_schedule_df(conn)[["company_id", "student_id"]]
    unsched = store.get_unscheduled_df(conn)[["company_id", "student_id"]]

    sched_pairs = set(map(tuple, sched.values))
    unsched_pairs = set(map(tuple, unsched.values))
    shortlist_pairs = set(map(tuple, shortlists.values))

    accounted = sched_pairs | unsched_pairs
    missing = shortlist_pairs - accounted
    assert not missing, f"{len(missing)} shortlist entries neither scheduled nor logged: {list(missing)[:5]}"

    overlap = sched_pairs & unsched_pairs
    assert not overlap, f"{len(overlap)} entries BOTH scheduled and logged unscheduled: {list(overlap)[:5]}"


def test_interview_fits_within_working_hours_and_skips_lunch(conn):
    sched = store.get_schedule_df(conn)
    for _, row in sched.iterrows():
        assert row.start_min >= 9 * 60
        assert row.end_min <= 17 * 60
        assert not (row.start_min < 13 * 60 + 30 and row.end_min > 13 * 60), (
            f"Interview {row.interview_id} overlaps lunch break: "
            f"{row.start_min}-{row.end_min}"
        )


def test_no_interview_in_unavailable_room(conn):
    room_unavail = pytest.importorskip("pandas").read_sql_query(
        "SELECT room_id, day FROM room_unavailability", conn
    )
    blocked = set(map(tuple, room_unavail.values))
    sched = store.get_schedule_df(conn)
    for _, row in sched.iterrows():
        assert (row.room_id, row.day) not in blocked, (
            f"Interview {row.interview_id} placed in room {row.room_id} on day {row.day}, "
            f"which was marked unavailable"
        )


def test_reset_schedule_is_idempotent(conn):
    """Re-running the scheduler on the same DB should not accumulate stale
    rows from a previous run (reset_schedule must fully clear state)."""
    before = len(store.get_schedule_df(conn))
    run_scheduler(conn)
    after = len(store.get_schedule_df(conn))
    # Not asserting before == after (RNG-free but re-run should be
    # deterministic given the same DB state) -- asserting no duplication:
    sched = store.get_schedule_df(conn)
    assert sched["interview_id"].is_unique
    assert after == before, "Re-running scheduler produced a different total without any input change"
