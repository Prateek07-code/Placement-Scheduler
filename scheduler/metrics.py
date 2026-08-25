"""
scheduler/metrics.py — Phase 5: quality metrics.

Five metrics, each answering a DIFFERENT question about schedule quality:
  1. % scheduled          -> did we cover the shortlist? (coverage)
  2. Student clashes       -> how often was the STUDENT the blocker, not capacity?
  3. Room utilization      -> was the physical resource used efficiently?
  4. Avg student wait time -> what's the actual on-campus experience like?
  5. Replan churn          -> already defined in scheduler/replan.py; surfaced
                              here too so one report has everything.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from db import store
from scheduler.core import DAY_START, LUNCH_START, LUNCH_END, DAY_END


def pct_scheduled(conn, day=None):
    sched = store.get_schedule_df(conn, day=day)
    unsched = store.get_unscheduled_df(conn, day=day)
    total = len(sched) + len(unsched)
    return 100 * len(sched) / total if total else 0.0


def student_clash_count(conn, day=None):
    unsched = store.get_unscheduled_df(conn, day=day)
    return int((unsched.reason_code == "STUDENT_TIME_CONFLICT").sum())


def room_utilization(conn, day):
    """room-minutes occupied / room-minutes available, for one day."""
    sched = store.get_schedule_df(conn, day=day)
    occupied_minutes = int((sched.end_min - sched.start_min).sum())

    rooms = store.get_rooms(conn)
    unavailable = store.get_unavailable_rooms(conn, day)
    available_rooms = len(rooms) - len(unavailable)

    day_minutes = (LUNCH_START - DAY_START) + (DAY_END - LUNCH_END)  # 450
    available_minutes = available_rooms * day_minutes

    return 100 * occupied_minutes / available_minutes if available_minutes else 0.0


def avg_student_wait_time(conn, day):
    """Average gap (minutes) between consecutive interviews for students
    with 2+ interviews on this day. Students with only one interview don't
    contribute (no wait to measure), so this is NOT the same as averaging
    over all students -- it isolates genuine waiting experience."""
    sched = store.get_schedule_df(conn, day=day)
    gaps = []
    for student_id, grp in sched.groupby("student_id"):
        if len(grp) < 2:
            continue
        times = sorted(zip(grp.start_min, grp.end_min))
        for i in range(len(times) - 1):
            gap = times[i + 1][0] - times[i][1]
            gaps.append(gap)
    return sum(gaps) / len(gaps) if gaps else 0.0


def full_report(conn):
    print("=" * 70)
    print("QUALITY METRICS REPORT")
    print("=" * 70)

    print(f"\nOverall % scheduled: {pct_scheduled(conn):.1f}%")
    print(f"Overall student clashes (STUDENT_TIME_CONFLICT): {student_clash_count(conn)}")

    print(f"\n{'Day':<5}{'% Scheduled':<15}{'Room Util %':<15}{'Avg Wait (min)':<18}{'Student Clashes'}")
    for day in [1, 2, 3, 4]:
        print(f"{day:<5}{pct_scheduled(conn, day):<15.1f}{room_utilization(conn, day):<15.1f}"
              f"{avg_student_wait_time(conn, day):<18.1f}{student_clash_count(conn, day)}")


if __name__ == "__main__":
    DB_PATH = str(Path(__file__).parent.parent / "dashboard.db")  # or scheduler.db, whichever you're using
    conn = store.get_connection(DB_PATH)
    full_report(conn)