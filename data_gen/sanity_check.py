import pandas as pd
from pathlib import Path

DATA = Path("/home/claude/placement_scheduler/data")

students = pd.read_csv(DATA / "students.csv")
companies = pd.read_csv(DATA / "companies.csv")
shortlists = pd.read_csv(DATA / "shortlists.csv")
rooms = pd.read_csv(DATA / "rooms.csv")
room_unavail = pd.read_csv(DATA / "room_unavailability.csv")

print("=" * 60)
print("STUDENTS")
print("=" * 60)
print(f"Count: {len(students)}")
print(f"CGPA: mean={students.cgpa.mean():.2f} std={students.cgpa.std():.2f} "
      f"min={students.cgpa.min():.2f} max={students.cgpa.max():.2f}")
print("Branch distribution:")
print((students.branch.value_counts(normalize=True) * 100).round(1).to_string())

print()
print("=" * 60)
print("COMPANIES")
print("=" * 60)
print(f"Count: {len(companies)}")
print(companies.groupby("tier").agg(
    n=("company_id", "count"),
    avg_cutoff=("cgpa_cutoff", "mean"),
    avg_shortlist_target=("target_shortlist_size", "mean"),
    avg_panels=("panels", "mean"),
    avg_duration=("interview_duration_min", "mean"),
).round(2).to_string())
print("\nBy day:")
print(companies.day.value_counts().sort_index().to_string())

print()
print("=" * 60)
print("SHORTLISTS (the key realism check)")
print("=" * 60)
actual_per_company = shortlists.groupby("company_id").size()
merged = actual_per_company.rename("actual").to_frame().join(
    companies.set_index("company_id")["target_shortlist_size"]
)
print("Actual vs target shortlist size (should track closely; gaps happen "
      "when a company's cutoff leaves too few eligible students):")
print(f"  Total interviews demanded (rows in shortlists.csv): {len(shortlists)}")
print(f"  Avg actual shortlist size per company: {actual_per_company.mean():.1f}")
print(f"  Companies where actual < 90% of target: "
      f"{(merged['actual'] < 0.9 * merged['target_shortlist_size']).sum()} / {len(merged)}")

per_student = shortlists.groupby("student_id").size()
print(f"\nShortlists per student: mean={per_student.mean():.2f}, "
      f"median={per_student.median():.0f}, max={per_student.max()}, "
      f"students with 0 shortlists={len(students) - len(per_student)}")

# Overlap check: do high-CGPA students get more shortlists? (should be yes)
sw = students.set_index("student_id")["cgpa"]
per_student_cgpa = per_student.to_frame("num_shortlists").join(sw)
corr = per_student_cgpa["num_shortlists"].corr(per_student_cgpa["cgpa"])
print(f"Correlation(num_shortlists, cgpa): {corr:.2f}  "
      f"(want clearly positive -> confirms 'top students on many lists')")

print("\nTop 10 most-shortlisted students:")
top10 = per_student_cgpa.sort_values("num_shortlists", ascending=False).head(10)
print(top10.to_string())

print()
print("=" * 60)
print("ROOMS")
print("=" * 60)
print(f"Count: {len(rooms)} ({(rooms.room_type == 'standard').sum()} standard, "
      f"{(rooms.room_type == 'huddle').sum()} huddle)")
print("Planned unavailability blocks:")
print(room_unavail.to_string(index=False))

print()
print("=" * 60)
print("ROUGH FEASIBILITY CHECK (interview-minutes demanded vs. room-minutes available, per day)")
print("=" * 60)
DAY_MINUTES = 8 * 60 - 30  # 9am-5pm minus 30 min lunch buffer, per room
demand = shortlists.merge(companies[["company_id", "day", "interview_duration_min"]], on="company_id")
demand_per_day = demand.groupby("day")["interview_duration_min"].sum()

for day in sorted(companies.day.unique()):
    rooms_today = len(rooms) - (room_unavail.day == day).sum()
    capacity = rooms_today * DAY_MINUTES
    need = demand_per_day.get(day, 0)
    pct = 100 * need / capacity if capacity else float("nan")
    print(f"Day {day}: demand={need:6.0f} min | capacity={capacity:6.0f} min "
          f"({rooms_today} rooms x {DAY_MINUTES}m) | utilization={pct:5.1f}%"
          f"{'  <-- OVER CAPACITY' if pct > 100 else ''}")

print("\n(Note: this ignores the panel-count constraint, which is usually the "
      "tighter bottleneck since a company can only run as many parallel "
      "interviews as it has panels, regardless of room availability. Phase 2 "
      "scheduler enforces both room AND panel concurrency limits.)")
