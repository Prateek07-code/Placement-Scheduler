"""
dashboard/app.py — Phase 4: Placement Coordinator Real-Time UI

Features:
  1. Live Schedule Viewer (Day & Filter controls, timeline formatting)
  2. Unscheduled & Conflict Triage Log (explicit reason codes)
  3. Interactive Disruption Injector (Custom & Benchmark Defense Scenario)
  4. Safe Two-Step Replan Engine (Preview -> Diff Review -> Commit)
  5. One-Click System Reset (Re-run base schedule from CSVs)
"""

import sys
import json
import pandas as pd
import streamlit as st
from pathlib import Path

# Ensure root directory is on Python path
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))

from db import store
from scheduler.core import run_scheduler, DAY_START, DAY_END
from scheduler.replan import replan, CHURN_THRESHOLD

DB_PATH = ROOT_DIR / "dashboard.db"
DATA_DIR = ROOT_DIR / "data"

# Page configuration
st.set_page_config(
    page_title="Placement Week Scheduler",
    page_icon="🗓️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

@st.cache_resource
def get_db():
    """Single cached connection, reused across reruns instead of reopened every click."""
    conn = store.get_connection(str(DB_PATH))
    store.init_schema(conn)
    return conn

def ensure_schedule_exists(conn):
    """Auto-builds the schedule on a genuinely empty DB, so the dashboard
    never crashes on a fresh checkout -- you don't have to remember to
    click Reset first."""
    n = pd.read_sql_query("SELECT COUNT(*) AS n FROM companies", conn)["n"].iloc[0]
    if n == 0:
        with st.spinner("First run: loading dataset and building initial schedule..."):
            store.load_static_data(conn, DATA_DIR)
            run_scheduler(conn)

def format_min(minutes):
    """Convert minutes from midnight (e.g. 540) to 12-hour AM/PM string."""
    hrs = int(minutes // 60)
    mins = int(minutes % 60)
    period = "AM" if hrs < 12 else "PM"
    display_hrs = hrs if hrs <= 12 else hrs - 12
    if display_hrs == 0:
        display_hrs = 12
    return f"{display_hrs}:{mins:02d} {period}"

def render_diff_summary(diff):
    """Render replan diff details for preview & confirmation."""
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Moved", len(diff["moved"]))
    col2.metric("Bumped (Lost Slot)", len(diff["bumped"]))
    col3.metric("Backfilled (New)", len(diff["backfilled"]))
    col4.metric("Withdrawn", len(diff["withdrawn"]))
    
    churn_pct = diff["churn_ratio"] * 100
    churn_color = "off" if diff["churn_ratio"] <= CHURN_THRESHOLD else "inverse"
    col5.metric("Churn Ratio", f"{churn_pct:.1f}%", delta=f"Threshold: {CHURN_THRESHOLD*100:.0f}%", delta_color=churn_color)

    if diff["warnings"]:
        for w in diff["warnings"]:
            st.warning(f"⚠️ **Warning:** {w}")
    else:
        st.success("✅ **Acceptable Churn:** Replan stays within operational disturbance limits.")

    st.subheader("📋 Affected Students to Notify")
    if diff["affected_students"]:
        st.info(f"**Total Students Affected:** {len(diff['affected_students'])}\n\n`" + ", ".join(diff["affected_students"]) + "`")
    else:
        st.write("No students affected.")

    # Breakdown Tables
    tabs = st.tabs(["Moved Interviews", "Bumped Interviews", "Backfilled Placements", "Withdrawn"])
    
    with tabs[0]:
        if diff["moved"]:
            moved_rows = []
            for m in diff["moved"]:
                moved_rows.append({
                    "Student": m["student_id"],
                    "Company": m["company_id"],
                    "Day": m["day"],
                    "Old Slot": f"Room {m['old']['room_id']} @ {format_min(m['old']['start_min'])}",
                    "New Slot": f"Room {m['new']['room_id']} @ {format_min(m['new']['start_min'])}"
                })
            st.dataframe(pd.DataFrame(moved_rows), use_container_width=True)
        else:
            st.write("No interviews moved.")

    with tabs[1]:
        if diff["bumped"]:
            bumped_rows = [{
                "Student": b["student_id"],
                "Company": b["company_id"],
                "Day": b["day"],
                "Previous Slot": f"Room {b['old']['room_id']} @ {format_min(b['old']['start_min'])}"
            } for b in diff["bumped"]]
            st.dataframe(pd.DataFrame(bumped_rows), use_container_width=True)
        else:
            st.write("No interviews bumped.")

    with tabs[2]:
        if diff["backfilled"]:
            backfill_rows = [{
                "Student": f["student_id"],
                "Company": f["company_id"],
                "Day": f["day"],
                "Assigned Slot": f"Room {f['new']['room_id']} @ {format_min(f['new']['start_min'])}"
            } for f in diff["backfilled"]]
            st.dataframe(pd.DataFrame(backfill_rows), use_container_width=True)
        else:
            st.write("No backfills executed.")

    with tabs[3]:
        if diff["withdrawn"]:
            st.dataframe(pd.DataFrame(diff["withdrawn"]), use_container_width=True)
        else:
            st.write("No withdrawals in this replan.")

# ---------------------------------------------------------------------------
# Streamlit Layout & Application Core
# ---------------------------------------------------------------------------

def main():
    st.title("🗓️ Mirai Labs Placement Week Operations Center")
    st.caption("Live Operations Dashboard & Intelligent Local-Repair Replan Engine")

    # Sidebar Controls
    st.sidebar.header("🕹️ System Controls")
    
    # Initialize DB if not existing
    conn = get_db()
    ensure_schedule_exists(conn)   # <-- add this line
    
    # System Reset / Initialize button
    if st.sidebar.button("🔄 Reset & Re-run Base Schedule", use_container_width=True, help="Wipes current schedule and re-executes initial greedy assignment."):
        with st.spinner("Initializing schema and running scheduler..."):
            store.init_schema(conn)
            store.load_static_data(conn, DATA_DIR)
            run_scheduler(conn)
            st.session_state.pop("preview_diff", None)
            st.session_state.pop("pending_disruptions", None)
            st.sidebar.success("Base schedule generated successfully!")
            st.rerun()

    st.sidebar.divider()
    
    # Main Navigation Tabs
    main_tab1, main_tab2, main_tab3, main_tab4 = st.tabs([
        "📊 Master Schedule & Metrics",
        "⚠️ Unscheduled & Conflicts",
        "🚨 Disruption Injector & Replan",
        "📜 Replan Audit History"
    ])

    # =========================================================================
    # TAB 1: MASTER SCHEDULE & METRICS
    # =========================================================================
    with main_tab1:
        st.header("Master Placement Schedule")
        
        # Day Selector & Filters
        f_col1, f_col2, f_col3 = st.columns([1, 2, 2])
        with f_col1:
            selected_day = st.selectbox("Select Placement Day", options=[1, 2, 3, 4], index=0)
        
        sched_df = store.get_schedule_df(conn, day=selected_day)
        unsched_df = store.get_unscheduled_df(conn, day=selected_day)
        companies_df = store.get_companies(conn, day=selected_day)
        rooms_df = store.get_rooms(conn)

        with f_col2:
            company_filter = st.multiselect("Filter by Company", options=companies_df["company_id"].tolist())
        with f_col3:
            room_filter = st.multiselect("Filter by Room", options=rooms_df["room_id"].tolist())

        # Top Metric Cards
        total_sched_day = len(sched_df)
        total_unsched_day = len(unsched_df)
        scheduled_pct = (total_sched_day / (total_sched_day + total_unsched_day) * 100) if (total_sched_day + total_unsched_day) > 0 else 0

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Scheduled Interviews", total_sched_day)
        m2.metric("Unscheduled / Logged", total_unsched_day)
        m3.metric("Schedule Completion Rate", f"{scheduled_pct:.1f}%")
        m4.metric("Active Companies Today", len(companies_df))

        st.divider()

        # Apply Filters to Schedule View
        view_df = sched_df.copy()
        if company_filter:
            view_df = view_df[view_df["company_id"].isin(company_filter)]
        if room_filter:
            view_df = view_df[view_df["room_id"].isin(room_filter)]

        if not view_df.empty:
            view_df["Start Time"] = view_df["start_min"].apply(format_min)
            view_df["End Time"] = view_df["end_min"].apply(format_min)
            
            # Sort the dataframe FIRST while 'start_min' still exists
            view_df = view_df.sort_values(["start_min", "room_id"])
            
            display_cols = ["interview_id", "company_id", "company_name", "student_id", "room_id", "panel_num", "Start Time", "End Time", "status"]
            st.dataframe(
                view_df[display_cols],
                use_container_width=True,
                height=450
            )
        else:
            st.info("No scheduled interviews found matching the active filters for this day.")

    # =========================================================================
    # TAB 2: UNSCHEDULED & CONFLICT LOGS
    # =========================================================================
    with main_tab2:
        st.header("Infeasibility & Unscheduled Reason Log")
        st.caption("Detailed diagnostic log explaining why interviews could not be scheduled.")

        u_day = st.selectbox("Select Day for Unscheduled Analysis", options=[1, 2, 3, 4], key="u_day_select")
        unsched_log = store.get_unscheduled_df(conn, day=u_day)

        if not unsched_log.empty:
            # Breakdown by Reason Code
            reason_counts = unsched_log["reason_code"].value_counts().to_dict()
            
            uc1, uc2, uc3 = st.columns(3)
            uc1.metric("Room Exhaustion (NO_ROOM)", reason_counts.get("NO_ROOM_FOR_COMPANY", 0))
            uc2.metric("Slot Exhaustion (CAPACITY)", reason_counts.get("COMPANY_CAPACITY_EXHAUSTED", 0))
            uc3.metric("Student Clash (TIME_CONFLICT)", reason_counts.get("STUDENT_TIME_CONFLICT", 0))

            st.subheader("Diagnostic Log Entries")
            st.dataframe(
                unsched_log[["log_id", "company_id", "student_id", "reason_code", "reason_detail", "logged_at"]],
                use_container_width=True
            )
        else:
            st.success("All interviews for this day were successfully scheduled! Zero infeasibilities logged.")

    # =========================================================================
    # TAB 3: DISRUPTION INJECTOR & REPLAN ENGINE
    # =========================================================================
    with main_tab3:
        st.header("Live Disruption Injection & Replan Core")
        st.caption("Simulate real-world disruptions and review targeted local repairs before committing.")

        st.subheader("1. Inject Disruption Scenario")
        
        inject_type = st.radio(
            "Select Disruption Mode",
            ["Preset Benchmark Defense Scenario", "Custom Single Disruption"],
            horizontal=True
        )

        disruptions_to_preview = []

        if inject_type == "Preset Benchmark Defense Scenario":
            st.warning("⚡ **Scenario:** Biggest Day-1 Recruiter 3h late + 1 Panel Drop + 15 Student Withdrawals.")
            if st.button("🚀 Load & Stage Benchmark Disruption", type="primary"):
                day1_sched = store.get_schedule_df(conn, day=1)
                if not day1_sched.empty:
                    biggest = day1_sched.groupby("company_id").size().idxmax()
                    panel_counts = day1_sched[day1_sched.company_id == biggest].groupby("panel_num").size()
                    dropped_panel = int(panel_counts.idxmax())
                    withdrawing_students = list(day1_sched.student_id.unique()[:15])

                    disruptions_to_preview = [
                        {"type": "company_late", "company_id": biggest, "day": 1, "hours_late": 3.0},
                        {"type": "panel_dropout", "company_id": biggest, "panel_num": dropped_panel, "day": 1}
                    ] + [
                        {"type": "student_withdraw", "student_id": s, "from_day": 1} for s in withdrawing_students
                    ]
                    
                    st.session_state["pending_disruptions"] = disruptions_to_preview
                    st.session_state.pop("preview_diff", None)
                    st.success(f"Staged compound disruption targeting {biggest} on Day 1.")
                else:
                    st.error("No schedule found on Day 1. Please click 'Reset & Re-run Base Schedule' in the sidebar first.")

        else:
            # Custom Disruption Form
            d_col1, d_col2 = st.columns(2)
            with d_col1:
                dis_kind = st.selectbox("Disruption Type", ["company_late", "panel_dropout", "room_unavailable", "student_withdraw"])
                dis_day = st.selectbox("Day of Disruption", [1, 2, 3, 4])
            
            with d_col2:
                all_comps = store.get_companies(conn)["company_id"].tolist()
                all_rooms = store.get_rooms(conn)["room_id"].tolist()
                
                if dis_kind == "company_late":
                    c_id = st.selectbox("Company", all_comps)
                    hrs = st.slider("Hours Late", 0.5, 5.0, 2.0, 0.5)
                    custom_dis = {"type": "company_late", "company_id": c_id, "day": dis_day, "hours_late": hrs}
                
                elif dis_kind == "panel_dropout":
                    c_id = st.selectbox("Company", all_comps)
                    company_row = store.get_companies(conn)
                    max_panels = int(company_row.loc[company_row.company_id == c_id, "panels"].iloc[0])
                    p_num = st.number_input("Panel Number", min_value=1, max_value=max_panels, value=1)
                    custom_dis = {"type": "panel_dropout", "company_id": c_id, "panel_num": int(p_num), "day": dis_day}
                
                elif dis_kind == "room_unavailable":
                    r_id = st.selectbox("Room", all_rooms)
                    custom_dis = {"type": "room_unavailable", "room_id": r_id, "day": dis_day}
                
                else:  # student_withdraw
                    s_id = st.text_input("Student ID (e.g. S0012)", value="S0012")
                    custom_dis = {"type": "student_withdraw", "student_id": s_id, "from_day": dis_day}

            if st.button("➕ Stage Custom Disruption"):
                st.session_state["pending_disruptions"] = [custom_dis]
                st.success(f"Staged custom disruption: {custom_dis}")

        st.divider()

        # Step 2: Preview & Execution
        st.subheader("2. Preview & Commit Replan")
        
        pending = st.session_state.get("pending_disruptions", None)
        if pending:
            st.json(pending, expanded=False)
            
            col_p1, col_p2 = st.columns([1, 1])
            with col_p1:
                if st.button("🔍 Run Dry-Run Replan Preview", type="secondary", use_container_width=True):
                    # Dry run with commit=False
                    preview_diff = replan(conn, pending, commit=False)
                    st.session_state["preview_diff"] = preview_diff

            if "preview_diff" in st.session_state:
                st.divider()
                st.markdown("### 📊 Replan Preview Results")
                render_diff_summary(st.session_state["preview_diff"])
                
                with col_p2:
                    if st.button("✅ Confirm & Commit Replan to Live Schedule", type="primary", use_container_width=True):
                        # Commit to live DB
                        final_diff = replan(conn, pending, commit=True)
                        st.session_state.pop("preview_diff", None)
                        st.session_state.pop("pending_disruptions", None)
                        st.balloons()
                        st.success("Replan committed to database successfully!")
                        st.rerun()
        else:
            st.info("No disruptions staged. Select or create a disruption above to trigger the replan engine.")

    # =========================================================================
    # TAB 4: REPLAN AUDIT HISTORY
    # =========================================================================
    with main_tab4:
        st.header("Replan Audit Log")
        st.caption("Historical log of all committed replan actions for governance and post-mortem analysis.")

        replan_history = pd.read_sql_query("SELECT * FROM replan_log ORDER BY replan_id DESC", conn)
        
        if not replan_history.empty:
            st.dataframe(replan_history, use_container_width=True)
        else:
            st.write("No replans have been committed yet.")

if __name__ == "__main__":
    main()