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


# ---------- Gaps found via coverage analysis (Phase 6) ----------

def test_room_unavailable_disrupts_the_owning_company(conn):
    """handle_room_unavailable had ZERO coverage before this test. It should
    behave identically to a panel_dropout on whichever company/panel owned
    that room -- this proves the delegation actually happens, not just that
    the function doesn't crash."""
    sched = store.get_schedule_df(conn, day=1)
    row = sched.iloc[0]
    room_id, cid, panel_num = row.room_id, row.company_id, row.panel_num

    before_other_panels = sched[(sched.company_id == cid) & (sched.panel_num != panel_num)]
    before_snapshot = set(zip(before_other_panels.interview_id, before_other_panels.room_id,
                               before_other_panels.start_min))

    diff = replan(conn, [{"type": "room_unavailable", "room_id": room_id, "day": 1}], commit=True)

    assert diff["moved"] or diff["bumped"], \
        "room_unavailable produced no changes -- delegation to panel_dropout may not be firing"

    unavail = pd.read_sql_query(
        "SELECT * FROM room_unavailability WHERE room_id=? AND day=1", conn, params=(room_id,)
    )
    assert not unavail.empty, "Room was not recorded in room_unavailability after the disruption"

    after = store.get_schedule_df(conn, day=1)
    after_other = after[(after.company_id == cid) & (after.panel_num != panel_num)
                         & (after.interview_id.isin(before_other_panels.interview_id))]
    after_snapshot = set(zip(after_other.interview_id, after_other.room_id, after_other.start_min))
    assert before_snapshot == after_snapshot, "room_unavailable disturbed a different, unaffected panel"

    assert (after.room_id == room_id).sum() == 0, "An interview is still scheduled in the unavailable room"


def test_room_unavailable_on_an_idle_room_is_a_safe_noop(conn):
    """A room nobody was using should return None from the handler and
    change nothing except the audit record -- exercises the 'row is None'
    branch specifically. Day 1 has zero idle rooms in this dataset (fully
    saturated), so this deliberately uses Day 4, independently confirmed to
    run under full room capacity (Phase 2 finding: panels, not rooms, bind
    that day)."""
    sched = store.get_schedule_df(conn, day=4)
    all_rooms = set(store.get_rooms(conn).room_id)
    used_rooms = set(sched.room_id)
    idle_rooms = all_rooms - used_rooms
    assert idle_rooms, "Day 4 was expected to have idle rooms based on Phase 2's utilization finding"
    idle_room = next(iter(idle_rooms))

    before = store.get_schedule_df(conn, day=4)
    diff = replan(conn, [{"type": "room_unavailable", "room_id": idle_room, "day": 4}], commit=True)

    assert not diff["moved"] and not diff["bumped"] and not diff["backfilled"], \
        "Disrupting an idle room should not change any interview"
    after = store.get_schedule_df(conn, day=4)
    assert len(before) == len(after)


def test_orphan_successfully_moved_lands_in_a_conflict_free_slot(conn):
    """The 'moved' success branch had ZERO coverage before this test -- every
    prior test happened to hit a fully-packed company where everything got
    bumped instead. This finds a company with genuine slack and proves
    relocation actually works, not just that bumping works."""
    sched = store.get_schedule_df(conn)
    unsched = store.get_unscheduled_df(conn)
    fully_served = sched.groupby("company_id").size().index.difference(set(unsched.company_id))
    assert len(fully_served) > 0, "No fully-served company in this dataset to test slack-based relocation with"

    for cid in fully_served:
        day = int(sched[sched.company_id == cid].day.iloc[0])
        diff = replan(conn, [{"type": "company_late", "company_id": cid, "day": day, "hours_late": 1}],
                      commit=False)
        if diff["moved"]:
            break
    else:
        pytest.skip("No fully-served company in this dataset produced a 'moved' interview at 1hr late")

    m = diff["moved"][0]
    assert m["new"]["room_id"] is not None
    assert m["new"]["start_min"] >= m["old"]["start_min"] or m["new"]["room_id"] != m["old"]["room_id"], \
        "Moved interview's new slot is identical to its old slot -- not a real move"

    diff2 = replan(conn, [{"type": "company_late", "company_id": cid, "day": day, "hours_late": 1}],
                    commit=True)
    full_sched = store.get_schedule_df(conn)
    for (rid, d), grp in full_sched.groupby(["room_id", "day"]):
        ivals = sorted(zip(grp.start_min, grp.end_min))
        for i in range(len(ivals) - 1):
            assert ivals[i][1] <= ivals[i + 1][0], f"Room {rid} double-booked after a move on day {d}"


def test_churn_warning_actually_fires_above_threshold(conn, monkeypatch):
    """Line 414 (the churn warning append) had never executed in any test.
    Rather than manufacture an artificially huge disruption, temporarily
    lower CHURN_THRESHOLD so any nonzero churn trips it -- this tests the
    WARNING LOGIC itself, independent of finding a naturally huge scenario."""
    import scheduler.replan as replan_module
    monkeypatch.setattr(replan_module, "CHURN_THRESHOLD", 0.0)

    cid = _pick_company_with_slack_and_shortfall(conn, day=1)
    diff = replan_module.replan(conn, [{"type": "panel_dropout", "company_id": cid, "panel_num": 1, "day": 1}],
                                 commit=False)

    assert diff["churn_count"] > 0, "Need a nonzero-churn scenario to test the warning against"
    assert diff["warnings"], "Churn exceeded threshold but no warning was appended"
    assert "exceeds" in diff["warnings"][0]
