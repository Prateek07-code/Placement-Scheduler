"""
scheduler/replan.py — Phase 3: the replan engine.

ARCHITECTURE (say this out loud in the defense):

  Every disruption ultimately does ONE of two things to a company's day:
    - SHRINKS capacity (panel drops, room goes unavailable, company arrives
      late -- all "this company now has fewer usable interview slots")
    - FREES capacity (a student withdraws -- their slot becomes empty)

  There is exactly ONE repair primitive, repair_company_day(), that both
  cases funnel into. The four disruption-specific functions
  (handle_company_late, handle_panel_dropout, handle_room_unavailable,
  handle_student_withdraw) only need to answer two questions:
    1. which interview rows are now INVALID (need a new slot, or removal)
    2. what does the company's SURVIVING capacity look like now

  repair_company_day() then does the same thing every time: compute free
  slots from surviving capacity, rank everyone who needs a slot (orphans
  from THIS disruption first, then opportunistic backfill from students who
  were already unscheduled), and greedily place them -- exactly the same
  greedy logic as the Phase 2 scheduler, just re-run over a much smaller
  slice of the schedule.

MINIMAL DISTURBANCE, BY CONSTRUCTION:
  - Only the directly affected (company_id, day) is ever touched.
  - Interviews scheduled after the disruption's effective time that don't
    conflict with the capacity loss are left completely alone -- not
    re-optimized, even if some other arrangement would be marginally
    better. Only what's broken gets touched.

PREVIEW / COMMIT:
  replan(conn, disruptions, commit=False) always actually performs the
  repair inside a live DB transaction (so the diff reflects a real
  placement attempt, not an estimate), then either commits or rolls back.
  This is what lets a dashboard show "here's what would happen" before the
  coordinator confirms it.

CHURN:
  churn = (interviews moved) + (interviews bumped to unscheduled), as a
  fraction of that day's total scheduled interviews BEFORE the replan.
  New backfill placements are NOT churn -- filling an empty seat disturbs
  no one. Threshold: 10%. Exceeding it does not block the replan; it's
  surfaced as a warning in the diff for the coordinator to see.
"""

import sys
import json
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import store
from scheduler.core import generate_day_slots, overlaps, DAY_START

CHURN_THRESHOLD = 0.10


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _get_company_row(conn, company_id):
    df = store.get_companies(conn)
    row = df[df["company_id"] == company_id]
    if row.empty:
        raise ValueError(f"Unknown company_id {company_id}")
    return row.iloc[0]


def _get_secured_rooms(conn, company_id):
    """Rooms currently bound to this company (panel_num -> room_id), as
    established by Phase 2's assign_rooms_to_panels."""
    rows = store.get_panel_room_assignments(conn, company_id)
    return {panel_num: room_id for panel_num, room_id, _day in rows}


def _get_valid_scheduled(conn, company_id, day):
    """Currently-scheduled (status='scheduled') interviews for this
    company/day, as a DataFrame."""
    sched = store.get_schedule_df(conn, day=day)
    return sched[sched["company_id"] == company_id].copy()


def _student_other_bookings(conn, student_id, day, exclude_interview_id=None):
    if exclude_interview_id is None:
        return store.get_student_day_bookings(conn, student_id, day)
    sched = store.get_schedule_df(conn, day=day)
    sched = sched[(sched.student_id == student_id) & (sched.interview_id != exclude_interview_id)]
    return list(zip(sched.start_min, sched.end_min))


def _cgpa_lookup(conn):
    return pd.read_sql_query("SELECT student_id, cgpa FROM students", conn).set_index("student_id")["cgpa"]


# ---------------------------------------------------------------------------
# The single repair primitive
# ---------------------------------------------------------------------------

def repair_company_day(conn, company_id, day, invalid_interview_ids,
                        surviving_rooms, valid_from_min, exclude_students,
                        diff):
    """
    The one repair function every disruption type funnels into.

    invalid_interview_ids : set of interview_id rows no longer valid at
        their current room/time (need to move, or be dropped).
    surviving_rooms : list of room_id this company can still use today.
    valid_from_min : slots before this time are not offered (used by the
        late-arrival case; pass DAY_START for disruptions that don't shift
        the start of the day).
    exclude_students : student_ids who must NOT be re-placed (e.g. the
        student who just withdrew -- their own invalidated interview is
        dropped outright, not repaired).
    diff : the running diff dict this call appends to.
    """
    company = _get_company_row(conn, company_id)
    duration = int(company["interview_duration_min"])
    cgpa = _cgpa_lookup(conn)

    valid_sched = _get_valid_scheduled(conn, company_id, day)
    still_valid = valid_sched[~valid_sched.interview_id.isin(invalid_interview_ids)]

    # Free-slot pool: full slot calendar for surviving rooms, minus slots
    # occupied by interviews that remain valid as-is.
    occupied = set(zip(still_valid.room_id, still_valid.start_min))
    all_slots = generate_day_slots(duration)
    free_slots = []
    for room_id in surviving_rooms:
        for s in all_slots:
            if s < valid_from_min:
                continue
            if (room_id, s) in occupied:
                continue
            free_slots.append((s, room_id))
    free_slots.sort(key=lambda x: (x[0], x[1]))

    room_to_panel = {v: k for k, v in _get_secured_rooms(conn, company_id).items()
                      if v in surviving_rooms}

    # --- Explicit withdrawals: cancel outright, never re-placed ---
    withdrawn_rows = valid_sched[
        valid_sched.interview_id.isin(invalid_interview_ids)
        & valid_sched.student_id.isin(exclude_students)
    ]
    for _, row in withdrawn_rows.iterrows():
        interview_id = int(row["interview_id"])
        _cancel_interview(conn, interview_id)
        diff["withdrawn"].append({
            "interview_id": interview_id, "company_id": company_id,
            "student_id": row["student_id"], "day": day,
        })

    # --- Rank 1: orphans (had a confirmed slot, it broke, not a withdrawal) ---
    orphan_rows = valid_sched[
        valid_sched.interview_id.isin(invalid_interview_ids)
        & ~valid_sched.student_id.isin(exclude_students)
    ]
    orphan_rows = orphan_rows.assign(_cgpa=orphan_rows.student_id.map(cgpa))
    orphan_rows = orphan_rows.sort_values("_cgpa", ascending=False)

    for _, row in orphan_rows.iterrows():
        student_id = row["student_id"]
        interview_id = int(row["interview_id"])
        existing = _student_other_bookings(conn, student_id, day, exclude_interview_id=interview_id)

        placed = False
        for i, (start, room_id) in enumerate(free_slots):
            end = start + duration
            if not overlaps(start, end, existing):
                old = {"room_id": row["room_id"], "start_min": int(row["start_min"]),
                       "end_min": int(row["end_min"])}
                new = {"room_id": room_id, "start_min": start, "end_min": end}
                _update_interview(conn, interview_id, room_id, room_to_panel[room_id], start, end)
                del free_slots[i]
                diff["moved"].append({
                    "interview_id": interview_id, "company_id": company_id,
                    "student_id": student_id, "day": day, "old": old, "new": new,
                })
                placed = True
                break

        if not placed:
            _cancel_interview(conn, interview_id)
            store.log_unscheduled(
                conn, company_id, student_id, day, "REPLAN_BUMPED",
                "Interview invalidated by disruption and no free slot was available to repair it."
            )
            diff["bumped"].append({
                "interview_id": interview_id, "company_id": company_id,
                "student_id": student_id, "day": day,
                "old": {"room_id": row["room_id"], "start_min": int(row["start_min"])},
            })

    # --- Rank 2: opportunistic backfill from previously-unscheduled students ---
    if free_slots:
        candidates = _get_backfill_candidates(conn, company_id, day, exclude_students, cgpa)
        for student_id in candidates:
            if not free_slots:
                break
            existing = store.get_student_day_bookings(conn, student_id, day)
            for i, (start, room_id) in enumerate(free_slots):
                end = start + duration
                if not overlaps(start, end, existing):
                    new_id = store.insert_interview(conn, company_id, student_id, day,
                                                      room_id, room_to_panel[room_id], start, end)
                    del free_slots[i]
                    diff["backfilled"].append({
                        "interview_id": new_id, "company_id": company_id,
                        "student_id": student_id, "day": day,
                        "new": {"room_id": room_id, "start_min": start, "end_min": end},
                    })
                    break


def _update_interview(conn, interview_id, room_id, panel_num, start_min, end_min):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE interviews SET room_id=?, panel_num=?, start_min=?, end_min=?, updated_at=?
           WHERE interview_id=?""",
        (room_id, panel_num, start_min, end_min, now, interview_id),
    )


def _cancel_interview(conn, interview_id):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE interviews SET status='cancelled', updated_at=? WHERE interview_id=?",
        (now, interview_id),
    )


def _get_backfill_candidates(conn, company_id, day, exclude_students, cgpa):
    """Students on this company's shortlist NOT currently scheduled with
    this company (status='scheduled'), ordered by CGPA desc -- same
    tie-break rule as the Phase 2 scheduler, for consistency."""
    shortlist = store.get_shortlist(conn, company_id)
    sched = store.get_schedule_df(conn, day=day)
    already_scheduled = set(sched[sched.company_id == company_id].student_id)
    candidates = shortlist[
        ~shortlist.student_id.isin(already_scheduled)
        & ~shortlist.student_id.isin(exclude_students)
    ]
    return list(candidates.sort_values("cgpa", ascending=False)["student_id"])


# ---------------------------------------------------------------------------
# Disruption handlers -- each answers "what's invalid?" and "what survives?"
# ---------------------------------------------------------------------------

def handle_company_late(conn, company_id, day, hours_late):
    """Company arrives `hours_late` hours after 9am. Any interview scheduled
    to start before the new arrival time is invalid. Interviews already at
    or after the new arrival time are untouched -- they still fit in the
    (shrunk) day, so moving them would be disturbance without necessity."""
    new_start_min = DAY_START + int(hours_late * 60)
    valid_sched = _get_valid_scheduled(conn, company_id, day)
    invalid_ids = set(valid_sched[valid_sched.start_min < new_start_min].interview_id)
    secured_rooms = list(_get_secured_rooms(conn, company_id).values())
    return {
        "company_id": company_id, "day": day,
        "invalid_ids": invalid_ids,
        "surviving_rooms": secured_rooms,
        "valid_from_min": new_start_min,
        "exclude_students": set(),
    }


def handle_panel_dropout(conn, company_id, panel_num, day, effective_from_min=None):
    """A panel drops out for the remainder of the day (assumption: a
    dropped panel doesn't come back later the same day -- stated explicitly
    since it's a live-defensible simplification). Interviews on that panel
    at/after the effective time are invalid; its room leaves the company's
    surviving capacity."""
    if effective_from_min is None:
        effective_from_min = DAY_START
    secured = _get_secured_rooms(conn, company_id)
    valid_sched = _get_valid_scheduled(conn, company_id, day)
    invalid_ids = set(valid_sched[
        (valid_sched.panel_num == panel_num) & (valid_sched.start_min >= effective_from_min)
    ].interview_id)
    surviving_rooms = [r for p, r in secured.items() if p != panel_num]
    return {
        "company_id": company_id, "day": day,
        "invalid_ids": invalid_ids,
        "surviving_rooms": surviving_rooms,
        "valid_from_min": DAY_START,
        "exclude_students": set(),
    }


def handle_room_unavailable(conn, room_id, day, effective_from_min=None):
    """A room becomes unavailable. Structurally identical to a panel
    dropout from the owning company's point of view -- find whichever
    company/panel was bound to this room and delegate to that handler."""
    if effective_from_min is None:
        effective_from_min = DAY_START

    q = "SELECT company_id, panel_num FROM panel_room_assignment WHERE room_id=? AND day=?"
    row = conn.execute(q, (room_id, day)).fetchone()

    conn.execute(
        "INSERT OR IGNORE INTO room_unavailability (room_id, day, reason) VALUES (?, ?, ?)",
        (room_id, day, "disruption: became unavailable mid-day"),
    )

    if row is None:
        return None  # room wasn't in use by anyone -- nothing to repair
    return handle_panel_dropout(conn, row["company_id"], row["panel_num"], day, effective_from_min)


def handle_student_withdraw(conn, student_id, from_day):
    """Student withdraws entirely: every scheduled interview on from_day
    onward is cancelled (earlier days already happened, untouched). Can
    free capacity across MULTIPLE companies at once, so returns a list of
    per-(company, day) repair contexts."""
    sched = store.get_schedule_df(conn)
    affected = sched[(sched.student_id == student_id) & (sched.day >= from_day)]

    contexts = []
    for (company_id, day), grp in affected.groupby(["company_id", "day"]):
        secured_rooms = list(_get_secured_rooms(conn, company_id).values())
        contexts.append({
            "company_id": company_id, "day": int(day),
            "invalid_ids": set(grp.interview_id),
            "surviving_rooms": secured_rooms,
            "valid_from_min": DAY_START,
            "exclude_students": {student_id},
        })
    return contexts


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def _empty_diff():
    return {"moved": [], "bumped": [], "backfilled": [], "withdrawn": [], "warnings": []}


def _merge_context(contexts_by_key, ctx):
    """Compound disruptions can touch the same (company, day) more than
    once (e.g. a late company that ALSO has a panel drop). Merge rather
    than overwrite: union invalid_ids/exclude_students, intersect
    surviving_rooms, take the LATEST valid_from_min (more restrictive
    constraint wins) -- so the single repair pass sees the company's true
    combined situation, not two independent partial repairs."""
    key = (ctx["company_id"], ctx["day"])
    if key not in contexts_by_key:
        contexts_by_key[key] = ctx
        return
    existing = contexts_by_key[key]
    existing["invalid_ids"] |= ctx["invalid_ids"]
    existing["exclude_students"] |= ctx["exclude_students"]
    existing["surviving_rooms"] = [r for r in existing["surviving_rooms"] if r in ctx["surviving_rooms"]]
    existing["valid_from_min"] = max(existing["valid_from_min"], ctx["valid_from_min"])


def replan(conn, disruptions, commit=False):
    """
    disruptions: list of dicts, each shaped like one of:
      {"type": "company_late", "company_id": "C001", "day": 1, "hours_late": 3}
      {"type": "panel_dropout", "company_id": "C001", "panel_num": 2, "day": 1}
      {"type": "room_unavailable", "room_id": "R05", "day": 1}
      {"type": "student_withdraw", "student_id": "S0123", "from_day": 1}

    Returns a diff dict. If commit=False (default), all DB changes made
    during repair are rolled back before returning -- the diff still
    reflects a REAL attempted repair, not an estimate, just not a
    persisted one. Dashboard "preview" should call this; "confirm" should
    call again with commit=True.
    """
    diff = _empty_diff()
    contexts_by_key = {}

    for d in disruptions:
        dtype = d["type"]
        if dtype == "company_late":
            _merge_context(contexts_by_key,
                            handle_company_late(conn, d["company_id"], d["day"], d["hours_late"]))
        elif dtype == "panel_dropout":
            _merge_context(contexts_by_key,
                            handle_panel_dropout(conn, d["company_id"], d["panel_num"], d["day"],
                                                  d.get("effective_from_min")))
        elif dtype == "room_unavailable":
            ctx = handle_room_unavailable(conn, d["room_id"], d["day"], d.get("effective_from_min"))
            if ctx is not None:
                _merge_context(contexts_by_key, ctx)
        elif dtype == "student_withdraw":
            for ctx in handle_student_withdraw(conn, d["student_id"], d["from_day"]):
                _merge_context(contexts_by_key, ctx)
        else:
            raise ValueError(f"Unknown disruption type: {dtype}")

    # Snapshot "before" totals per touched day, for churn calculation.
    before_totals = {}
    for (_company_id, day) in contexts_by_key:
        if day not in before_totals:
            before_totals[day] = len(store.get_schedule_df(conn, day=day))

    for ctx in contexts_by_key.values():
        repair_company_day(
            conn, ctx["company_id"], ctx["day"], ctx["invalid_ids"],
            ctx["surviving_rooms"], ctx["valid_from_min"], ctx["exclude_students"],
            diff,
        )

    total_before = sum(before_totals.values())
    churn_count = len(diff["moved"]) + len(diff["bumped"])
    churn_ratio = churn_count / total_before if total_before else 0.0
    if churn_ratio > CHURN_THRESHOLD:
        diff["warnings"].append(
            f"Churn ratio {churn_ratio:.1%} exceeds the {CHURN_THRESHOLD:.0%} threshold -- "
            f"this replan disturbs an unusually large share of the affected day(s). "
            f"Recommend coordinator review before confirming."
        )

    diff["churn_count"] = churn_count
    diff["churn_ratio"] = round(churn_ratio, 4)
    diff["affected_students"] = sorted(set(
        [m["student_id"] for m in diff["moved"]] +
        [b["student_id"] for b in diff["bumped"]] +
        [f["student_id"] for f in diff["backfilled"]]
    ))

    if commit:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO replan_log
               (timestamp, disruption_type, disruption_details, changes, affected_students, churn_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (now, ",".join(d["type"] for d in disruptions), json.dumps(disruptions),
             json.dumps({k: v for k, v in diff.items() if k != "affected_students"}),
             json.dumps(diff["affected_students"]), churn_count),
        )
        conn.commit()
    else:
        conn.rollback()

    return diff
