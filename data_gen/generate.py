"""
Phase 1: Realistic dataset generator for the Placement Week Scheduler.

Design notes (things you should be able to defend live):

- Companies are grouped into three TIERS, mapped onto the 4-day window:
    Tier 1 (Day 1)      : mass recruiters. Low CGPA cutoff, huge shortlists,
                           many panels, short interviews. These are the
                           "everyone applies" companies (service/product mass hiring).
    Tier 2 (Day 2-3)     : mid-tier. Moderate cutoff, medium shortlists.
    Tier 3 (Day 3-4)     : niche / "dream" companies. High cutoff, small
                           shortlists, few panels, longer interviews (more
                           rigorous process), but HIGH priority — these are
                           the companies a coordinator protects hardest
                           during a replan.

- Student "attractiveness" (a hidden score = CGPA + noise) drives shortlist
  overlap: high-attractiveness students get pulled into many companies'
  shortlists (this is what makes top students collide across companies —
  the realistic clash pattern placement season actually has). This is done
  via weighted sampling without replacement, not uniform random choice.

- CGPA cutoff acts as a hard eligibility filter *before* the weighted
  sampling — a company never shortlists a student below its cutoff.

- Priority score (1-5, 5 = protect hardest during replan) is derived from
  selectivity: high cutoff + small shortlist = more prestigious/harder to
  reschedule around student preference, so it gets protected first.

All randomness is seeded for reproducibility (--seed).
"""

import argparse
import numpy as np
import pandas as pd
from pathlib import Path

BRANCHES = ["CSE", "IT", "ECE", "EEE", "ME", "CE", "AIML", "CHEM"]
BRANCH_WEIGHTS = [0.28, 0.14, 0.16, 0.10, 0.10, 0.08, 0.10, 0.04]

COMPANY_NAME_POOL = [
    "Infratek", "Bluewave Systems", "Nimbus Cloud", "Orbit Digital", "Cognify",
    "Stratos Analytics", "Vantage Softworks", "Ironleaf Tech", "Quantiva",
    "Northbridge Consulting", "Meridian Labs", "Pinnacle Robotics", "Delta Forge",
    "Cerulean AI", "Redshift Systems", "Anvil Cybersecurity", "Zenith Financial Tech",
    "Solace Networks", "Hexon Semiconductors", "Aurex Global", "Kinetic Motors",
    "Pathfinder Aerospace", "Crestline Pharma", "Voltaire Energy", "Cobalt Retail Tech",
    "Sable Data Systems", "Ridgeline Capital", "Halcyon Health Informatics",
    "Fernwood Logistics", "Marrow Biotech", "Tessellate Games", "Obsidian Defense Systems",
    "Wavelength Telecom", "Granite Infra", "Lumen Research Labs",
]

DAY_WINDOW_MIN = 8 * 60  # 9am-5pm minus 1hr lunch handled in scheduler, keep raw here
NUM_DAYS = 4
NUM_ROOMS = 20
NUM_STUDENTS = 800
NUM_COMPANIES = 35


def gen_students(rng, n=NUM_STUDENTS):
    cgpa = rng.normal(7.5, 0.8, n)
    cgpa = np.clip(cgpa, 6.0, 9.85).round(2)
    branch = rng.choice(BRANCHES, size=n, p=BRANCH_WEIGHTS)
    # Hidden "attractiveness" score used only for shortlist weighting.
    # Correlated with CGPA but with noise so it's not a pure CGPA sort
    # (extracurriculars / interview polish / branch demand also matter).
    attractiveness = cgpa + rng.normal(0, 0.5, n)

    df = pd.DataFrame({
        "student_id": [f"S{i:04d}" for i in range(1, n + 1)],
        "cgpa": cgpa,
        "branch": branch,
        "_attractiveness": attractiveness,
    })
    return df


def assign_days_and_tiers(n=NUM_COMPANIES):
    """
    Tier1: Day 1 mass recruiters (8 companies)
    Tier2: Day 2-3 mid-tier (18 companies)
    Tier3: Day 3-4 niche/dream (9 companies)
    """
    plan = (
        [("Tier1", 1)] * 8 +
        [("Tier2", 2)] * 9 + [("Tier2", 3)] * 9 +
        [("Tier3", 3)] * 4 + [("Tier3", 4)] * 5
    )
    assert len(plan) == n
    return plan


def gen_companies(rng, n=NUM_COMPANIES):
    names = rng.choice(COMPANY_NAME_POOL, size=n, replace=False)
    plan = assign_days_and_tiers(n)

    rows = []
    for i in range(n):
        tier, day = plan[i]
        cid = f"C{i+1:03d}"
        name = names[i]

        if tier == "Tier1":
            cutoff = round(rng.uniform(6.0, 6.5), 2)
            # Heterogeneous mass recruiters: not every Day-1 company is
            # equally huge. 2 "mega" recruiters (idx 0,1 within Tier1)
            # dominate the batch; the rest are large but more modest.
            # This is a realism choice (real placement weeks have 1-2
            # standout mass recruiters, not eight equally massive ones) —
            # it also happens to bring total Day-1 demand down from ~466%
            # to a targeted ~160% of room capacity.
            tier1_idx = sum(1 for p in plan[:i] if p[0] == "Tier1")
            if tier1_idx < 2:
                shortlist_size = int(rng.integers(150, 221))
            else:
                shortlist_size = int(rng.integers(50, 111))
            panels = int(rng.integers(5, 9))
            duration = int(rng.choice([15, 20]))
        elif tier == "Tier2":
            cutoff = round(rng.uniform(6.8, 7.6), 2)
            shortlist_size = int(rng.integers(20, 60))
            panels = int(rng.integers(2, 5))
            duration = int(rng.choice([20, 25, 30]))
        else:  # Tier3
            cutoff = round(rng.uniform(7.8, 8.8), 2)
            shortlist_size = int(rng.integers(15, 55))
            panels = int(rng.integers(1, 3))
            duration = int(rng.choice([30, 40, 45]))

        rows.append({
            "company_id": cid,
            "name": name,
            "tier": tier,
            "day": day,
            "cgpa_cutoff": cutoff,
            "target_shortlist_size": shortlist_size,
            "panels": panels,
            "interview_duration_min": duration,
        })

    df = pd.DataFrame(rows)

    # Priority score: selectivity-driven (high cutoff + small shortlist -> high priority)
    cutoff_norm = (df["cgpa_cutoff"] - df["cgpa_cutoff"].min()) / (
        df["cgpa_cutoff"].max() - df["cgpa_cutoff"].min()
    )
    inv_size = 1 - (df["target_shortlist_size"] - df["target_shortlist_size"].min()) / (
        df["target_shortlist_size"].max() - df["target_shortlist_size"].min()
    )
    raw_priority = 0.7 * cutoff_norm + 0.3 * inv_size
    df["priority_tier"] = pd.cut(
        raw_priority, bins=5, labels=[1, 2, 3, 4, 5]
    ).astype(int)

    return df


def gen_shortlists(rng, students_df, companies_df):
    """
    Weighted sampling without replacement per company, restricted to
    eligible (cgpa >= cutoff) students. Weight = softmax-ish of
    attractiveness, so higher-attractiveness students are likelier to be
    picked by multiple companies -> realistic overlap.
    """
    records = []
    att = students_df["_attractiveness"].values
    sid = students_df["student_id"].values

    for _, comp in companies_df.iterrows():
        eligible_mask = students_df["cgpa"].values >= comp["cgpa_cutoff"]
        elig_idx = np.where(eligible_mask)[0]

        if len(elig_idx) == 0:
            continue

        k = min(comp["target_shortlist_size"], len(elig_idx))
        elig_att = att[elig_idx]
        # softmax weighting (temperature controls how much attractiveness
        # skews selection vs. pure randomness)
        temp = 1.5
        w = np.exp((elig_att - elig_att.max()) / temp)
        w = w / w.sum()

        chosen = rng.choice(elig_idx, size=k, replace=False, p=w)
        for idx in chosen:
            records.append({
                "company_id": comp["company_id"],
                "student_id": sid[idx],
            })

    return pd.DataFrame(records)


def gen_rooms(rng, n=NUM_ROOMS):
    rows = []
    for i in range(n):
        rid = f"R{i+1:02d}"
        # Assumption: 3 rooms are smaller "huddle" rooms only suitable for
        # 1-panel interviews (realistic building constraint); rest general.
        room_type = "huddle" if i < 3 else "standard"
        rows.append({"room_id": rid, "room_type": room_type})
    df = pd.DataFrame(rows)

    # Assumption: a small number of rooms have a planned unavailability
    # block (maintenance / AV setup) on a random day-slot — this feeds
    # realistic infeasibility into Phase 2, not just volume overload.
    unavail = []
    blocked_rooms = rng.choice(df["room_id"], size=2, replace=False)
    for rid in blocked_rooms:
        day = int(rng.integers(1, NUM_DAYS + 1))
        unavail.append({"room_id": rid, "day": day, "reason": "maintenance/AV setup"})
    unavail_df = pd.DataFrame(unavail)

    return df, unavail_df


def main(seed=42, outdir="data"):
    rng = np.random.default_rng(seed)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    students = gen_students(rng)
    companies = gen_companies(rng)
    shortlists = gen_shortlists(rng, students, companies)
    rooms, room_unavail = gen_rooms(rng)

    students_out = students.drop(columns=["_attractiveness"])

    students_out.to_csv(outdir / "students.csv", index=False)
    companies.to_csv(outdir / "companies.csv", index=False)
    shortlists.to_csv(outdir / "shortlists.csv", index=False)
    rooms.to_csv(outdir / "rooms.csv", index=False)
    room_unavail.to_csv(outdir / "room_unavailability.csv", index=False)

    return students_out, companies, shortlists, rooms, room_unavail


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--outdir", type=str, default="data")
    args = ap.parse_args()
    main(seed=args.seed, outdir=args.outdir)
    print("Data generated.")
