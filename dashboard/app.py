"""
Phase 4: Streamlit Dashboard
Features: Schedule view, conflict detection, one-click replan, and disruption injector.
"""

import streamlit as st


def main():
    st.set_page_config(page_title="Placement Scheduler", layout="wide")
    st.title("Campus Placement Scheduler & Dynamic Replan Engine")
    st.write("Welcome to the Placement Scheduler Dashboard.")


if __name__ == "__main__":
    main()

