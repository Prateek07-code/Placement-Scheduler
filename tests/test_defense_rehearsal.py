"""
tests/test_defense_rehearsal.py — Phase 6.

Runs the assignment brief's EXACT example disruption end-to-end, using the
same auto-selection logic as dashboard's "Load example scenario" button and
scheduler/run_example_scenario.py -- not a hand-picked convenient case.

This is a REGRESSION GUARD on the actual live-defense scenario, not a
correctness test in the same sense as test_scheduler.py/test_replan.py.
Those prove the mechanism is correct in general; this proves the specific
path you'll demo live keeps working as the codebase evolves.
"""

import sys
from pathlib import Path
import pytest
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import store
from scheduler.core import run_scheduler
from scheduler.replan import replan, CHURN_THRESHOLD

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


def _build_example_scenario(conn):
    """Identical selection logic to the dashboard's 'Load example scenario'
    button -- auto-picks the biggest Day-1 recruiter, its busiest panel,
    and 15 real students with a Day-1 booking. If this logic ever finds
    nothing (e.g. Day 1 becomes empty after a data regen), that's exactly
    the kind of silent breakage this test exists to catch."""
    day1 = store.get_schedule_df(conn, day=1)
    assert not day1.empty, "Day 1 has no scheduled interviews -- example scenario cannot be built"

    biggest = day1.groupby("company_id").size().idxmax()
    panel_counts = day1[day1.company_id == biggest].groupby("panel_num").size()
    dropped_panel = int(panel_counts.idxmax())
    withdrawing = list(day1.student_id.unique()[:15])
    assert len(withdrawing) == 15, "Expected at least 15 students with a Day-1 booking"

    disruptions = [
        {"type": "company_late", "company_id": biggest, "day": 1, "hours_late": 3},
        {"type": "panel_dropout", "company_id": biggest, "panel_num": dropped_panel, "day": 1},
    ] + [
        {"type": "student_withdraw", "student_id": s, "from_day": 1} for s in withdrawing
    ]
    return biggest, dropped_panel, withdrawing, disruptions


def test_example_scenario_preview_does_not_persist(conn):
    day1_before = store.get_schedule_df(conn, day=1)
    _, _, _, disruptions = _build_example_scenario(conn)

    diff = replan(conn, disruptions, commit=False)

    # A real repair attempt should have happened (not a no-op)...
    assert diff["bumped"] or diff["moved"] or diff["backfilled"], \
        "Preview produced no changes at all -- scenario may no longer be disruptive"

    # ...but nothing should be persisted.
    day1_after = store.get_schedule_df(conn, day=1)
    assert len(day1_before) == len(day1_after), "Preview leaked changes into the DB"

    log = pd.read_sql_query("SELECT * FROM replan_log", conn)
    assert log.empty, "Preview wrote a replan_log entry"


def test_example_scenario_commit_end_to_end(conn):
    biggest, dropped_panel, withdrawing, disruptions = _build_example_scenario(conn)

    diff = replan(conn, disruptions, commit=True)

    # --- Business-level expectations for THIS scenario, not general invariants ---

    # Every withdrawn student must have zero interviews on day >= 1, everywhere.
    full_sched = store.get_schedule_df(conn)
    still_there = full_sched[full_sched.student_id.isin(withdrawing) & (full_sched.day >= 1)]
    assert still_there.empty, "A withdrawn student still has a scheduled interview somewhere"

    # Withdrawal should have triggered at least some backfill (this dataset
    # has plenty of previously-unscheduled students waiting) -- if this ever
    # goes to zero, the backfill pathway itself may be broken.
    assert len(diff["backfilled"]) > 0, "No backfills occurred -- opportunistic backfill may be broken"

    # affected_students must be exactly the union of moved/bumped/backfilled
    expected_affected = set(
        [m["student_id"] for m in diff["moved"]] +
        [b["student_id"] for b in diff["bumped"]] +
        [f["student_id"] for f in diff["backfilled"]]
    )
    assert set(diff["affected_students"]) == expected_affected

    # Churn must be a real, computed number, and this specific scenario is
    # expected to stay under threshold (documented in docs/writeup.md) --
    # if a future change pushes it over, that's worth knowing before the defense.
    assert diff["churn_ratio"] < CHURN_THRESHOLD, (
        f"Example scenario churn {diff['churn_ratio']:.1%} now EXCEEDS the "
        f"{CHURN_THRESHOLD:.0%} threshold -- this contradicts the write-up's "
        f"reported number and needs to be re-verified before the defense."
    )

    # --- Hard invariants must still hold after a real commit ---
    for (sid, day), grp in full_sched.groupby(["student_id", "day"]):
        ivals = sorted(zip(grp.start_min, grp.end_min))
        for i in range(len(ivals) - 1):
            assert ivals[i][1] <= ivals[i + 1][0], f"Student {sid} double-booked on day {day}"
    for (rid, day), grp in full_sched.groupby(["room_id", "day"]):
        ivals = sorted(zip(grp.start_min, grp.end_min))
        for i in range(len(ivals) - 1):
            assert ivals[i][1] <= ivals[i + 1][0], f"Room {rid} double-booked on day {day}"

    # --- Audit trail must reflect the commit ---
    log = pd.read_sql_query("SELECT * FROM replan_log ORDER BY replan_id DESC", conn)
    assert len(log) == 1
    assert log.iloc[0]["churn_count"] == diff["churn_count"]


def test_example_scenario_is_deterministic_given_same_seed(conn):
    """Re-running data_gen with the same seed should make the SAME company
    come out as 'biggest Day-1 recruiter' every time -- if this ever
    becomes non-deterministic, your live demo could pick a different
    company each run, which is confusing to narrate consistently."""
    biggest_1, _, _, _ = _build_example_scenario(conn)

    # rebuild schedule fresh from the same static data, without regenerating CSVs
    store.load_static_data(conn, DATA_DIR)
    run_scheduler(conn)
    biggest_2, _, _, _ = _build_example_scenario(conn)

    assert biggest_1 == biggest_2, (
        "Biggest Day-1 recruiter changed between runs on identical data -- "
        "scheduler may have nondeterministic tie-breaking somewhere"
    )