"""
Runs the EXACT example scenario given in the assignment brief:

    "the biggest Day-1 recruiter is 3 hours late, one of its panels
     dropped, and 15 students just withdrew"

This is a compound disruption: company_late + panel_dropout on the SAME
company (which _merge_context() must combine correctly, not process as two
independent partial repairs), plus 15 independent student_withdraw events
that may ripple into several different companies (whichever else those 15
students had shortlists with).

Run this before your defense to rehearse the exact flow you'll be doing
live: pick the biggest Day-1 recruiter, build the disruption list, preview,
inspect the diff, then commit.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import store
from scheduler.core import run_scheduler
from scheduler.replan import replan

DB_PATH = "/home/claude/placement_scheduler/scheduler.db"
DATA_DIR = "/home/claude/placement_scheduler/data"


def print_diff(diff, label):
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    print(f"Moved:      {len(diff['moved']):4d} interviews")
    print(f"Bumped:     {len(diff['bumped']):4d} interviews (lost their slot entirely)")
    print(f"Backfilled: {len(diff['backfilled']):4d} interviews (new placements from waitlist)")
    print(f"Withdrawn:  {len(diff['withdrawn']):4d} interviews (student left the process)")
    print(f"Churn:      {diff['churn_count']} interviews ({diff['churn_ratio']:.1%} of that day's schedule)")
    print(f"Affected students needing notification: {len(diff['affected_students'])}")
    if diff["warnings"]:
        for w in diff["warnings"]:
            print(f"⚠️  WARNING: {w}")
    else:
        print("No warnings -- churn within the 10% threshold.")

    if diff["moved"]:
        print(f"\nSample of moved interviews (first 5 of {len(diff['moved'])}):")
        for m in diff["moved"][:5]:
            print(f"  {m['student_id']} @ {m['company_id']}: "
                  f"room {m['old']['room_id']}/{m['old']['start_min']}min -> "
                  f"room {m['new']['room_id']}/{m['new']['start_min']}min")

    if diff["bumped"]:
        print(f"\nSample of bumped (unscheduled) interviews (first 5 of {len(diff['bumped'])}):")
        for b in diff["bumped"][:5]:
            print(f"  {b['student_id']} @ {b['company_id']}: lost slot, no replacement found")

    if diff["backfilled"]:
        print(f"\nSample of backfilled interviews (first 5 of {len(diff['backfilled'])}):")
        for f in diff["backfilled"][:5]:
            print(f"  {f['student_id']} @ {f['company_id']}: newly placed at "
                  f"room {f['new']['room_id']}/{f['new']['start_min']}min")


def main():
    conn = store.get_connection(DB_PATH)
    store.init_schema(conn)
    store.load_static_data(conn, DATA_DIR)
    run_scheduler(conn)

    # Identify "the biggest Day-1 recruiter" -- by scheduled interview count,
    # which is the operational definition a coordinator would actually mean.
    day1_sched = store.get_schedule_df(conn, day=1)
    biggest = day1_sched.groupby("company_id").size().idxmax()
    companies = store.get_companies(conn)
    biggest_name = companies[companies.company_id == biggest].name.iloc[0]
    print(f"Biggest Day-1 recruiter by scheduled interviews: {biggest} ({biggest_name})")

    # Pick a real panel that actually has interviews today, so the dropout
    # is a meaningful disruption, not a no-op.
    panel_counts = day1_sched[day1_sched.company_id == biggest].groupby("panel_num").size()
    dropped_panel = panel_counts.idxmax()
    print(f"Panel dropping out: panel #{dropped_panel} of {biggest} "
          f"({panel_counts[dropped_panel]} interviews scheduled on it today)")

    # Pick 15 real students who have at least one scheduled interview today,
    # so the withdrawal disruption is meaningful.
    import pandas as pd
    all_day1 = store.get_schedule_df(conn, day=1)
    withdrawing_students = list(all_day1.student_id.unique()[:15])
    print(f"15 students withdrawing: {withdrawing_students}")

    disruptions = [
        {"type": "company_late", "company_id": biggest, "day": 1, "hours_late": 3},
        {"type": "panel_dropout", "company_id": biggest, "panel_num": int(dropped_panel), "day": 1},
    ] + [
        {"type": "student_withdraw", "student_id": s, "from_day": 1} for s in withdrawing_students
    ]

    # --- PREVIEW (what a "one-click replan" button would show first) ---
    preview_diff = replan(conn, disruptions, commit=False)
    print_diff(preview_diff, "PREVIEW (not yet committed)")

    # Confirm nothing was actually persisted during preview.
    check = store.get_schedule_df(conn, day=1)
    print(f"\n[sanity check] Day-1 schedule size after preview: {len(check)} "
          f"(should equal pre-disruption count: {len(day1_sched)})")
    assert len(check) == len(day1_sched), "Preview leaked changes into the DB!"

    # --- COMMIT (coordinator clicks 'confirm') ---
    final_diff = replan(conn, disruptions, commit=True)
    print_diff(final_diff, "COMMITTED")

    # Re-verify hard invariants hold on the live DB after commit.
    full_sched = store.get_schedule_df(conn)
    for (sid, day), grp in full_sched.groupby(["student_id", "day"]):
        ivals = sorted(zip(grp.start_min, grp.end_min))
        for i in range(len(ivals) - 1):
            assert ivals[i][1] <= ivals[i + 1][0], f"Student {sid} double-booked after commit!"
    for (rid, day), grp in full_sched.groupby(["room_id", "day"]):
        ivals = sorted(zip(grp.start_min, grp.end_min))
        for i in range(len(ivals) - 1):
            assert ivals[i][1] <= ivals[i + 1][0], f"Room {rid} double-booked after commit!"
    print("\n[sanity check] No double-bookings anywhere after commit. Invariants hold.")


if __name__ == "__main__":
    main()
