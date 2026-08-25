"""
scheduler/core.py — Phase 2: initial feasible schedule.

ALGORITHM (the thing you need to be able to explain in 60 seconds live):

  1. Day window: 9:00-17:00 with a fixed 13:00-13:30 lunch break, i.e. two
     working blocks per day (240 min + 210 min = 450 min), same as the
     capacity check from Phase 1.

  2. Per day, per company (processed in priority_tier DESC order — more
     selective/prestigious companies claim rooms first): bind each of the
     company's panels to one physical room for the whole day. This is the
     key simplification — once a (company, panel_num) is bound to a room,
     room double-booking and panel double-booking collapse into the SAME
     constraint automatically, because that room/panel pair only ever
     belongs to one company that day and runs interviews back-to-back.

     If room supply runs out before a company gets any panels bound, its
     entire shortlist for that day is logged as NO_ROOM_FOR_COMPANY.

  3. For each room a company secured, generate a fixed slot calendar
     (back-to-back interviews of that company's duration, skipping lunch).
     Pool all of a company's slots across its rooms into one sorted list —
     this is "how many interview slots this company has today."

  4. Walk the company's shortlist, highest CGPA first (rationale: higher-
     CGPA students are statistically more likely to be double-booked
     elsewhere and harder to place later, so we protect their slot first).
     For each student, scan the company's remaining slots in time order and
     take the first one that doesn't overlap any of that student's existing
     bookings that day (across any company). Assign and remove the slot.

     If the company has no slots left at all -> COMPANY_CAPACITY_EXHAUSTED.
     If slots exist but every one conflicts with this student's day ->
     STUDENT_TIME_CONFLICT.

Student conflicts only need to be checked WITHIN a day, because a company
only ever interviews on one fixed day — so a student can never have a
same-time conflict across two different days.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import store

DAY_START = 9 * 60        # 540
LUNCH_START = 13 * 60      # 780
LUNCH_END = 13 * 60 + 30   # 810
DAY_END = 17 * 60          # 1020


def generate_day_slots(duration_min):
    """All back-to-back start times for a given interview duration, across
    the two working blocks of a day (morning / post-lunch)."""
    slots = []
    t = DAY_START
    while t + duration_min <= LUNCH_START:
        slots.append(t)
        t += duration_min
    t = LUNCH_END
    while t + duration_min <= DAY_END:
        slots.append(t)
        t += duration_min
    return slots


def overlaps(start, end, existing):
    for (es, ee) in existing:
        if start < ee and es < end:
            return True
    return False


def assign_rooms_to_panels(conn, companies_day_df, available_rooms):
    """
    Round-robin room allocation, priority order.

    Why round-robin instead of fulfilling one company fully before moving
    to the next: with rooms scarce (as they are on Day 1-3), a "fulfill
    fully then move on" policy lets the first few high-priority companies
    consume the ENTIRE room pool, leaving later companies with zero rooms
    at all -- not "a smaller shortlist," total exclusion. Round-robin still
    respects priority order (a company's Nth room is always granted before
    a lower-priority company's Nth room), but ensures scarcity is shared
    across companies rather than concentrated on whoever sorts last.

    Returns dict: company_id -> list of room_ids actually secured (may be
    fewer than the company's requested panel count).
    """
    room_pool = sorted(available_rooms, key=lambda r: (r[1] != "standard", r[0]))
    room_ids_in_order = [r[0] for r in room_pool]

    companies_ordered = list(companies_day_df.itertuples())
    needed = {c.company_id: int(c.panels) for c in companies_ordered}
    secured = {c.company_id: [] for c in companies_ordered}

    idx = 0
    progressed = True
    while idx < len(room_ids_in_order) and progressed:
        progressed = False
        for c in companies_ordered:
            cid = c.company_id
            if len(secured[cid]) >= needed[cid]:
                continue
            if idx >= len(room_ids_in_order):
                break
            room_id = room_ids_in_order[idx]
            idx += 1
            panel_num = len(secured[cid]) + 1
            store.assign_panel_room(conn, cid, panel_num, room_id, int(c.day))
            secured[cid].append(room_id)
            progressed = True

    return secured


def schedule_company(conn, company, secured_rooms, student_day_bookings):
    """
    Schedule one company's shortlist into its secured rooms.
    student_day_bookings: dict student_id -> list[(start,end)] for THIS day,
    mutated in place as we place interviews (so later companies in the same
    day see earlier companies' placements — this is what prevents
    cross-company double-booking of a student).
    """
    cid = company["company_id"]
    day = int(company["day"])
    duration = int(company["interview_duration_min"])

    if not secured_rooms:
        shortlist = store.get_shortlist(conn, cid)
        for _, s in shortlist.iterrows():
            store.log_unscheduled(
                conn, cid, s["student_id"], day, "NO_ROOM_FOR_COMPANY",
                f"{cid} secured 0 of {company['panels']} requested rooms/panels on day {day} "
                f"(room supply exhausted by higher-priority companies)."
            )
        return {"scheduled": 0, "unscheduled": len(shortlist)}

    # Pool all slots across this company's secured rooms into one sorted
    # (start_min, room_id) list -> "what interview slots exist today"
    per_room_slots = generate_day_slots(duration)
    pooled_slots = []
    for room_id in secured_rooms:
        for s in per_room_slots:
            pooled_slots.append((s, room_id))
    pooled_slots.sort(key=lambda x: (x[0], x[1]))

    # panel_num lookup: which panel number owns which room for this company
    room_to_panel = {}
    for panel_num, room_id, _day in store.get_panel_room_assignments(conn, cid):
        room_to_panel[room_id] = panel_num

    remaining_slots = list(pooled_slots)
    shortlist = store.get_shortlist(conn, cid)  # already ordered CGPA DESC

    n_scheduled, n_unscheduled = 0, 0

    for _, srow in shortlist.iterrows():
        student_id = srow["student_id"]

        if not remaining_slots:
            store.log_unscheduled(
                conn, cid, student_id, day, "COMPANY_CAPACITY_EXHAUSTED",
                f"{cid} had {len(pooled_slots)} total slots on day {day}; all filled "
                f"before reaching this student (shortlist rank by CGPA)."
            )
            n_unscheduled += 1
            continue

        existing = student_day_bookings.get(student_id, [])
        placed = False
        for i, (start, room_id) in enumerate(remaining_slots):
            end = start + duration
            if not overlaps(start, end, existing):
                # place it
                store.insert_interview(conn, cid, student_id, day, room_id,
                                        room_to_panel[room_id], start, end)
                student_day_bookings.setdefault(student_id, []).append((start, end))
                del remaining_slots[i]
                placed = True
                n_scheduled += 1
                break

        if not placed:
            store.log_unscheduled(
                conn, cid, student_id, day, "STUDENT_TIME_CONFLICT",
                f"Student already booked at every one of {cid}'s {len(remaining_slots)} "
                f"remaining slots on day {day}."
            )
            n_unscheduled += 1

    return {"scheduled": n_scheduled, "unscheduled": n_unscheduled}


def run_scheduler(conn):
    """Top-level entry point: schedules all 4 days independently (a company
    only ever interviews on one fixed day, so days share no constraints)."""
    store.reset_schedule(conn)
    all_companies = store.get_companies(conn)
    results = []

    for day in sorted(all_companies["day"].unique()):
        companies_day = all_companies[all_companies["day"] == day]
        unavailable = store.get_unavailable_rooms(conn, day)
        rooms_df = store.get_rooms(conn)
        available_rooms = [
            (r["room_id"], r["room_type"])
            for _, r in rooms_df.iterrows()
            if r["room_id"] not in unavailable
        ]

        secured = assign_rooms_to_panels(conn, companies_day, available_rooms)

        student_day_bookings = {}
        for _, company in companies_day.iterrows():
            cid = company["company_id"]
            stats = schedule_company(conn, company, secured.get(cid, []), student_day_bookings)
            results.append({"day": int(day), "company_id": cid, **stats})

    conn.commit()
    return results


if __name__ == "__main__":
    DB_PATH = "scheduler.db"
    DATA_DIR = "data"

    conn = store.get_connection(DB_PATH)
    store.init_schema(conn)
    store.load_static_data(conn, DATA_DIR)
    results = run_scheduler(conn)

    total_sched = sum(r["scheduled"] for r in results)
    total_unsched = sum(r["unscheduled"] for r in results)
    print(f"Scheduled: {total_sched}  |  Unscheduled: {total_unsched}  |  "
          f"% scheduled: {100 * total_sched / (total_sched + total_unsched):.1f}%")
