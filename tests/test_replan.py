"""
tests/test_replan.py

Two categories:
  1. Invariants that must STILL hold after any replan (same three hard
     constraints as Phase 2 -- a replan must never introduce a conflict
     while fixing one).
  2. Behavioral tests -- does each disruption type do the specific thing
     it's supposed to do (company_late frees early slots and shifts only
     early interviews, panel_dropout only touches that panel's interviews,
     student_withdraw frees exactly that student's slots and nobody else's,
     preview mode truly doesn't persist).
"""

import sys
from pathlib import Path
import pytest
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import store
from scheduler.core import run_scheduler
from scheduler.replan import replan, DAY_START

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


def _assert_no_double_booking(sched, group_cols):
    for _, grp in sched.groupby(group_cols):
        intervals = sorted(zip(grp.start_min, grp.end_min))
        for i in range(len(intervals) - 1):
            assert intervals[i][1] <= intervals[i + 1][0], f"Double-booking in group {group_cols}"


def _pick_company_with_slack_and_shortfall(conn, day):
    sched = store.get_schedule_df(conn, day=day)
    counts = sched.groupby("company_id").size()
    return counts.idxmax()


# ---------- Invariants after replan ----------

def test_invariants_hold_after_company_late(conn):
    cid = _pick_company_with_slack_and_shortfall(conn, day=1)
    replan(conn, [{"type": "company_late", "company_id": cid, "day": 1, "hours_late": 3}], commit=True)

    sched = store.get_schedule_df(conn)
    _assert_no_double_booking(sched, ["student_id", "day"])
    _assert_no_double_booking(sched, ["room_id", "day"])
    _assert_no_double_booking(sched, ["company_id", "panel_num", "day"])


def test_invariants_hold_after_panel_dropout(conn):
    cid = _pick_company_with_slack_and_shortfall(conn, day=1)
    replan(conn, [{"type": "panel_dropout", "company_id": cid, "panel_num": 1, "day": 1}], commit=True)

    sched = store.get_schedule_df(conn)
    _assert_no_double_booking(sched, ["student_id", "day"])
    _assert_no_double_booking(sched, ["room_id", "day"])


def test_invariants_hold_after_compound_disruption(conn):
    cid = _pick_company_with_slack_and_shortfall(conn, day=1)
    sched_before = store.get_schedule_df(conn, day=1)
    some_students = list(sched_before.student_id.unique()[:15])

    disruptions = (
        [{"type": "company_late", "company_id": cid, "day": 1, "hours_late": 3}] +
        [{"type": "panel_dropout", "company_id": cid, "panel_num": 1, "day": 1}] +
        [{"type": "student_withdraw", "student_id": s, "from_day": 1} for s in some_students]
    )
    replan(conn, disruptions, commit=True)

    sched = store.get_schedule_df(conn)
    _assert_no_double_booking(sched, ["student_id", "day"])
    _assert_no_double_booking(sched, ["room_id", "day"])
    _assert_no_double_booking(sched, ["company_id", "panel_num", "day"])

    still_scheduled = sched[sched.student_id.isin(some_students) & (sched.day >= 1)]
    assert still_scheduled.empty, "A withdrawn student still has a scheduled interview"


# ---------- Behavioral: minimal disturbance ----------

def test_company_late_does_not_touch_other_companies(conn):
    cid = _pick_company_with_slack_and_shortfall(conn, day=1)
    other_before = store.get_schedule_df(conn, day=1)
    other_before = other_before[other_before.company_id != cid]
    before_snapshot = set(zip(other_before.interview_id, other_before.room_id, other_before.start_min))

    replan(conn, [{"type": "company_late", "company_id": cid, "day": 1, "hours_late": 3}], commit=True)

    after = store.get_schedule_df(conn, day=1)
    after_other = after[after.company_id != cid]
    after_snapshot = set(zip(after_other.interview_id, after_other.room_id, after_other.start_min))

    assert before_snapshot == after_snapshot, "Disruption to one company altered another company's schedule"


def test_company_late_leaves_already_late_interviews_untouched(conn):
    cid = _pick_company_with_slack_and_shortfall(conn, day=1)
    before = store.get_schedule_df(conn, day=1)
    before = before[before.company_id == cid]
    new_start = DAY_START + 3 * 60
    already_ok = before[before.start_min >= new_start]

    diff = replan(conn, [{"type": "company_late", "company_id": cid, "day": 1, "hours_late": 3}], commit=True)
    moved_ids = {m["interview_id"] for m in diff["moved"]}
    bumped_ids = {b["interview_id"] for b in diff["bumped"]}

    untouched_ids = set(already_ok.interview_id)
    assert not (untouched_ids & moved_ids), "An interview already after the new arrival time was moved"
    assert not (untouched_ids & bumped_ids), "An interview already after the new arrival time was bumped"


def test_panel_dropout_only_affects_that_panel(conn):
    cid = _pick_company_with_slack_and_shortfall(conn, day=1)
    before = store.get_schedule_df(conn, day=1)
    before_other_panels = before[(before.company_id == cid) & (before.panel_num != 1)]
    before_snapshot = set(zip(before_other_panels.interview_id, before_other_panels.room_id,
                               before_other_panels.start_min))

    replan(conn, [{"type": "panel_dropout", "company_id": cid, "panel_num": 1, "day": 1}], commit=True)

    after = store.get_schedule_df(conn, day=1)
    after_other_panels = after[
        (after.company_id == cid) & (after.panel_num != 1)
        & (after.interview_id.isin(before_other_panels.interview_id))
    ]
    after_snapshot = set(zip(after_other_panels.interview_id, after_other_panels.room_id,
                              after_other_panels.start_min))

    assert before_snapshot == after_snapshot, "Panel dropout disturbed interviews on a different, unaffected panel"


def test_student_withdraw_frees_exactly_their_slots(conn):
    """Withdrawal cancels a student's interviews across ALL days >= from_day
    (a real withdrawal, e.g. accepting an offer, removes them from the
    entire remaining process, not just 'today')."""
    sched = store.get_schedule_df(conn)
    student_id = sched.student_id.iloc[0]
    n_before = len(sched[sched.student_id == student_id])
    assert n_before >= 1

    diff = replan(conn, [{"type": "student_withdraw", "student_id": student_id, "from_day": 1}], commit=True)

    after = store.get_schedule_df(conn)
    n_after = len(after[after.student_id == student_id])
    assert n_after == 0, "Withdrawn student still has scheduled interviews on some day"
    assert len(diff["withdrawn"]) == n_before


def test_preview_mode_does_not_persist(conn):
    before = store.get_schedule_df(conn)
    before_snapshot = set(zip(before.interview_id, before.room_id, before.start_min, before.status))

    cid = _pick_company_with_slack_and_shortfall(conn, day=1)
    diff = replan(conn, [{"type": "company_late", "company_id": cid, "day": 1, "hours_late": 3}], commit=False)

    assert diff["moved"] or diff["bumped"], "Preview produced no changes to verify rollback against"

    after = store.get_schedule_df(conn)
    after_snapshot = set(zip(after.interview_id, after.room_id, after.start_min, after.status))
    assert before_snapshot == after_snapshot, "Preview mode (commit=False) persisted changes to the DB"

    log = pd.read_sql_query("SELECT * FROM replan_log", conn)
    assert log.empty, "Preview mode wrote a replan_log entry"


def test_backfill_uses_freed_slot_from_withdrawal(conn):
    """Withdrawing a student who had multiple company interviews can free
    slots at SEVERAL companies at once -- backfill should fire for each
    one that has a waiting candidate, not just the first."""
    unsched_before = store.get_unscheduled_df(conn, day=1)
    if unsched_before.empty:
        pytest.skip("No pre-existing unscheduled students on day 1 to test backfill against")

    sched = store.get_schedule_df(conn, day=1)
    candidate_companies = set(unsched_before.company_id) & set(sched.company_id)
    assert candidate_companies, "No company has both scheduled and unscheduled students to test with"
    cid = next(iter(candidate_companies))

    victim = sched[sched.company_id == cid].student_id.iloc[0]
    diff = replan(conn, [{"type": "student_withdraw", "student_id": victim, "from_day": 1}], commit=True)

    assert len(diff["backfilled"]) >= 1, "Freed slot from withdrawal was not backfilled"
    backfilled_companies = {b["company_id"] for b in diff["backfilled"]}
    assert cid in backfilled_companies, f"Expected a backfill at {cid}, got backfills at {backfilled_companies}"


def test_churn_fields_present_and_sane(conn):
    cid = _pick_company_with_slack_and_shortfall(conn, day=1)
    diff = replan(conn, [{"type": "company_late", "company_id": cid, "day": 1, "hours_late": 3}], commit=False)
    assert "churn_count" in diff and "churn_ratio" in diff
    assert diff["churn_count"] == len(diff["moved"]) + len(diff["bumped"])
    assert 0.0 <= diff["churn_ratio"] <= 1.0
