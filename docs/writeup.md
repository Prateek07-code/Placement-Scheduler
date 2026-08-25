# Placement Week Scheduler — Metrics & Design Decisions

## 1. What does a "good" schedule mean?

I define quality across five metrics, each answering a different question —
a single "% scheduled" number hides too much:

| Metric | What it measures | Why it's separate from the others |
|---|---|---|
| **% Scheduled** | Shortlist coverage | The headline number, but hides *why* shortfall happens |
| **Student clashes** | Count of `STUDENT_TIME_CONFLICT` — times the student's own overlapping bookings (not capacity) blocked a slot | Isolates "the student was the constraint" from "the system was" |
| **Room utilization** | room-minutes used ÷ room-minutes available | A day can have low coverage but high utilization (genuinely out of room) or the reverse (idle rooms, panel-starved) |
| **Avg student wait time** | Gap between a student's back-to-back interviews, for students with 2+ that day | The lived experience of being on campus, not an operational metric |
| **Replan churn** | (moved + bumped) ÷ that day's total scheduled, per replan | How disruptive a *repair* was, separate from the base schedule's quality |

Measured results on the generated dataset:

| Day | % Scheduled | Room Util | Avg Wait | Student Clashes |
|---|---|---|---|---|
| 1 | 54.5% | 89.8% | 80.4 min | 0 |
| 2 | 75.1% | 90.2% | 57.4 min | 1 |
| 3 | 60.4% | 94.7% | 67.9 min | 2 |
| 4 | 58.2% | 44.9% | 112.1 min | 0 |

Overall: 60.6% scheduled, only 3 student clashes total across ~1,960 shortlist entries.

**A "good" schedule, for this system, is one that maximizes coverage subject to
never trading away the three hard constraints (no student/room/panel double-
booking), where room/panel scarcity — not student conflicts — is expected to
be the dominant source of shortfall.** The near-zero clash count validates
that expectation: infeasibility here is a capacity problem, not a scheduling-
cleverness problem, and Day 4's low room utilization despite low coverage
proves capacity type matters (panels, not rooms, bind that day).

## 2. When the schedule is infeasible, which constraint bends first — and who decides?

**Rooms bend before student shortlist size does**, and the answer to "who
decides" is split deliberately between the system and the coordinator:

- **The system decides the mechanical policy, pre-committed and consistent:**
  - Room contention is resolved by **round-robin allocation in priority-tier
    order** (Phase 2) — no company is ever fully shut out; scarcity is spread
    across all companies on a contested day rather than concentrated on
    whoever is processed last.
  - When a company can't interview its whole shortlist, **higher-CGPA
    students are scheduled first** — they're statistically more likely to be
    double-booked elsewhere and harder to place later, so protecting their
    slot first reduces downstream conflicts.
  - During a replan, the **same CGPA-priority rule governs who gets
    re-placed first** among orphaned interviews, and opportunistic backfill
    (Phase 3) uses the identical ordering for consistency.

- **The coordinator decides policy, not mechanics:** every replan runs in
  **preview mode first** (`commit=False`) — nothing is applied until the
  coordinator reviews the diff and explicitly confirms. The coordinator also
  decides *what to model* as a disruption in the first place (which company
  is late, which panel dropped) — the system never invents or guesses a
  disruption; it only reacts to one that's been told to it.

This split exists because the *mechanical* fairness rule needs to be
consistent and explainable (a coordinator under pressure shouldn't be
inventing tie-break logic on the spot), but the *decision to act* on a given
repair should never be automatic — a churn-heavy repair might be
mechanically correct and still be a bad idea on the ground (e.g. the
coordinator knows a company will informally extend their day).

## 3. How much reshuffling is acceptable during a replan?

**Churn = (interviews moved + interviews bumped) ÷ that day's total scheduled
interviews before the replan. Threshold: 10%.**

- New backfill placements are **excluded** from churn — filling a
  previously-empty seat disturbs no one, so it shouldn't count against a
  "disturbance" budget.
- Exceeding 10% does **not block** the replan — it surfaces a warning in the
  diff (`diff["warnings"]`) so the coordinator sees it before confirming.
  The system flags; the coordinator still decides, consistent with the
  answer to Q2.

Observed churn on real disruptions tested during this build:

| Scenario | Churn |
|---|---|
| Single panel dropout (C003, panel 1, Day 1) | 4.8% |
| Compound: biggest Day-1 recruiter 3h late + panel drop + 15 withdrawals | 3.9% |

Both comfortably under threshold, which is itself informative: even a
compound, multi-type disruption on the busiest day of the week stayed well
within an "acceptable" bound, because the repair is scoped to only the
directly affected company/day rather than reshuffling broadly. The 10%
number was chosen as a concrete, memorable bound rather than "as little as
possible" — the brief specifically warned against "moving 200 appointments
to fix a 2-hour delay," and a hard percentage makes that failure mode
detectable and reportable rather than a vague judgment call.