"""
tests/test_metrics.py — Phase 6 smoke tests for Phase 5's metrics.

Not testing exact values (those depend on the dataset and will drift as you
tweak generation params) -- testing that every metric returns a sane,
bounded number and never silently produces NaN/inf/negative garbage.
"""

import sys
from pathlib import Path
import math
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import store
from scheduler.core import run_scheduler
from scheduler.metrics import pct_scheduled, student_clash_count, room_utilization, avg_student_wait_time

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


def test_pct_scheduled_is_bounded(conn):
    overall = pct_scheduled(conn)
    assert 0.0 <= overall <= 100.0
    assert not math.isnan(overall)
    for day in [1, 2, 3, 4]:
        v = pct_scheduled(conn, day)
        assert 0.0 <= v <= 100.0


def test_student_clash_count_is_non_negative_int(conn):
    assert isinstance(student_clash_count(conn), int)
    assert student_clash_count(conn) >= 0
    for day in [1, 2, 3, 4]:
        assert student_clash_count(conn, day) >= 0


def test_room_utilization_is_bounded(conn):
    for day in [1, 2, 3, 4]:
        v = room_utilization(conn, day)
        assert 0.0 <= v <= 100.0, f"Day {day} room utilization {v} out of [0,100] bounds"
        assert not math.isnan(v)


def test_avg_wait_time_is_non_negative(conn):
    for day in [1, 2, 3, 4]:
        v = avg_student_wait_time(conn, day)
        assert v >= 0.0
        assert not math.isnan(v)


def test_metrics_survive_a_fully_empty_day(conn):
    """If a day somehow has zero scheduled interviews (e.g. after an
    aggressive replan), metrics must degrade gracefully to 0 -- not crash
    with a division by zero. Simulated by wiping one day's interviews."""
    conn.execute("DELETE FROM interviews WHERE day = 4")
    conn.commit()
    assert pct_scheduled(conn, day=4) == 0.0
    assert room_utilization(conn, day=4) == 0.0
    assert avg_student_wait_time(conn, day=4) == 0.0