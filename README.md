# Campus Placement Scheduler & Dynamic Replan Engine

An interview scheduling and dynamic disruption-repair system for campus placement drives.

## Project Structure

```
placement_scheduler/
├── data_gen/
│   ├── generate.py           # Phase 1 ✓ — dataset generator
│   └── sanity_check.py       # Phase 1 ✓ — summary stats / feasibility check
│
├── data/
│   ├── companies.csv         # Phase 1 ✓ — raw generator output (kept for
│   ├── students.csv          #   reproducibility / re-diffing against DB state)
│   ├── shortlists.csv
│   ├── rooms.csv
│   └── room_unavailability.csv
│
├── db/
│   ├── schema.sql            # Phase 2 — tables: companies, students, rooms,
│   │                         #   shortlists, interviews, unscheduled_log, replan_log
│   └── store.py              # Phase 2 — thin data-access layer
│
├── scheduler/
│   ├── core.py               # Phase 2 — initial greedy assignment
│   ├── replan.py             # Phase 3 — targeted local repair + diff output
│   └── metrics.py            # Phase 5 — quality metrics computation
│
├── dashboard/
│   └── app.py                # Phase 4 — Streamlit dashboard & disruption injector
│
├── tests/
│   ├── test_scheduler.py     # Invariant tests: no double-booking, panel/room concurrency
│   ├── test_replan.py        # Disruption handling, diff correctness, churn threshold
│   └── test_defense_rehearsal.py # End-to-end scripted disruption scenario
│
├── docs/
│   └── writeup.md            # Metrics report + 3 defense questions answered
│
├── requirements.txt
├── README.md
└── .gitignore
```

## Setup & Running Locally (Offline / Defense Fallback)

1. **Create and activate a virtual environment:**
   ```bash
   python -m venv venv
   # On Windows:
   venv\Scripts\activate
   # On Linux/macOS:
   source venv/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Generate synthetic data:**
   ```bash
   python data_gen/generate.py
   python data_gen/sanity_check.py
   ```

4. **Initialize DB & Run initial scheduler:**
   ```bash
   python -c "from db.store import store; store.init_db()"
   python -c "from scheduler.core import schedule_initial; schedule_initial()"
   ```

5. **Run the Streamlit Dashboard:**
   ```bash
   streamlit run dashboard/app.py
   ```

6. **Run Test Suite:**
   ```bash
   pytest
   ```

