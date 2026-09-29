import pytest

from netmon.checker import CheckResult
from netmon.config import DeviceConfig
from netmon.db import Database
from netmon.monitor import (
    DeviceState, Event, Monitor, Status, apply_check, restore_state,
)

UP = CheckResult(True, 5.0)
FAIL = CheckResult(False, None, "timeout")


def run(results, state=None):
    """Feed a sequence of True/False results; return final state and events."""
    state = state or DeviceState()
    events = []
    for i, ok in enumerate(results):
        state, ev = apply_check(state, ok, at=float(i * 30))
        events.append(ev)
    return state, events


# ------------------------------------------------------------ 3-failure rule

def test_new_device_is_unknown():
    assert DeviceState().status is Status.UNKNOWN


def test_one_failure_keeps_device_up():
    state, events = run([True, False])
    assert state.status is Status.UP
    assert state.consecutive_failures == 1
    assert events == [None, None]


def test_two_failures_keep_device_up():
    state, events = run([True, False, False])
    assert state.status is Status.UP
    assert events == [None, None, None]


def test_third_consecutive_failure_marks_down_and_opens_incident():
    state, events = run([True, False, False, False])
    assert state.status is Status.DOWN
    assert events == [None, None, None, Event.WENT_DOWN]


def test_incident_start_is_first_failure_of_streak():
    state, _ = run([True, False, False, False])
    assert state.first_failure_at == 30.0  # the 2nd check (index 1)


def test_non_consecutive_failures_never_mark_down():
    state, events = run([True, False, False, True, False, False, True, False])
    assert state.status is Status.UP
    assert Event.WENT_DOWN not in events


def test_further_failures_while_down_do_not_reopen_incident():
    state, events = run([True, False, False, False, False, False, False])
    assert state.status is Status.DOWN
    assert events.count(Event.WENT_DOWN) == 1
    assert state.first_failure_at == 30.0


def test_unknown_device_stays_unknown_until_threshold():
    state, events = run([False, False])
    assert state.status is Status.UNKNOWN
    state, ev = apply_check(state, False, at=60)
    assert state.status is Status.DOWN and ev is Event.WENT_DOWN


def test_unknown_device_first_success_is_up_without_event():
    state, events = run([True])
    assert state.status is Status.UP
    assert events == [None]


# ------------------------------------------------------------ recovery

def test_one_success_brings_down_device_back_up_and_closes_incident():
    state, events = run([False, False, False, True])
    assert state.status is Status.UP
    assert state.consecutive_failures == 0
    assert state.first_failure_at is None
    assert events[-1] is Event.CAME_UP


def test_success_while_up_emits_no_event():
    _, events = run([True, True, True])
    assert events == [None, None, None]


def test_success_resets_failure_count():
    state, _ = run([True, False, False, True, False, False])
    assert state.consecutive_failures == 2
    assert state.status is Status.UP
    assert state.first_failure_at == 120.0  # new streak starts after the reset


def test_flapping_opens_and_closes_one_incident_per_outage():
    seq = [False] * 3 + [True] + [False] * 3 + [True]
    _, events = run(seq)
    assert events.count(Event.WENT_DOWN) == 2
    assert events.count(Event.CAME_UP) == 2


# ------------------------------------------------------------ restore after restart

def test_restore_with_open_incident_is_down():
    s = restore_state(500.0, [(600.0, False), (570.0, False)], now=610, max_age_s=150)
    assert s.status is Status.DOWN
    assert s.first_failure_at == 500.0
    assert s.consecutive_failures >= 3


def test_restore_open_incident_even_if_history_is_stale():
    s = restore_state(500.0, [(600.0, False)], now=99_999, max_age_s=150)
    assert s.status is Status.DOWN


def test_restore_recent_up():
    s = restore_state(None, [(600.0, True)], now=610, max_age_s=150)
    assert s == DeviceState(Status.UP, 0, None)


def test_restore_recent_failures_counts_streak():
    s = restore_state(None, [(660.0, False), (630.0, False), (600.0, True)], now=670, max_age_s=150)
    assert s == DeviceState(Status.UP, 2, 630.0)


def test_restore_caps_streak_below_threshold_without_incident():
    checks = [(t, False) for t in (690.0, 660.0, 630.0, 600.0)]
    s = restore_state(None, checks, now=700, max_age_s=150)
    assert s.consecutive_failures == 2
    assert s.status is Status.UNKNOWN
    # next failure then opens an incident
    _, ev = apply_check(s, False, at=720)
    assert ev is Event.WENT_DOWN


def test_restore_stale_history_starts_unknown():
    s = restore_state(None, [(600.0, True)], now=10_000, max_age_s=150)
    assert s == DeviceState()


def test_restore_no_history():
    assert restore_state(None, [], now=1, max_age_s=150) == DeviceState()


# ------------------------------------------------------------ Monitor + database

@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def make_monitor(db):
    devices = db.sync_devices([DeviceConfig("Printer", "printer", "10.0.0.5", "tcp", 9100)])
    return Monitor(db, devices, interval_s=30), devices[0].id


def test_monitor_opens_and_closes_incident_in_db(db):
    mon, dev = make_monitor(db)
    mon.record(dev, UP, 0)
    mon.record(dev, FAIL, 30)
    mon.record(dev, FAIL, 60)
    assert db.recent_incidents(10) == []
    assert mon.record(dev, FAIL, 90) is Event.WENT_DOWN

    [inc] = db.recent_incidents(10)
    assert inc["started_at"] == 30 and inc["resolved_at"] is None

    assert mon.record(dev, UP, 120) is Event.CAME_UP
    [inc] = db.recent_incidents(10)
    assert inc["started_at"] == 30 and inc["resolved_at"] == 120


def test_monitor_stores_every_check_and_null_latency_on_failure(db):
    mon, dev = make_monitor(db)
    mon.record(dev, UP, 0)
    mon.record(dev, FAIL, 30)
    assert db.history(dev, 0) == [(0, True, 5.0), (30, False, None)]


def test_restart_mid_outage_continues_same_incident(db):
    mon, dev = make_monitor(db)
    for t in (0, 30, 60):
        mon.record(dev, FAIL, t)
    assert len(db.recent_incidents(10)) == 1

    # Simulate a restart: build a fresh Monitor from the same database.
    mon2 = Monitor(db, db.active_devices(), interval_s=30)
    assert mon2.devices[dev].state.status is Status.DOWN
    assert mon2.record(dev, FAIL, 90) is None  # no duplicate incident
    assert mon2.record(dev, UP, 120) is Event.CAME_UP

    [inc] = db.recent_incidents(10)
    assert inc["started_at"] == 0 and inc["resolved_at"] == 120


def test_db_failure_leaves_state_unchanged_so_transition_is_retried(db, monkeypatch):
    mon, dev = make_monitor(db)
    mon.record(dev, FAIL, 0)
    mon.record(dev, FAIL, 30)

    def broken(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "record_check", broken)
    with pytest.raises(RuntimeError):
        mon.record(dev, FAIL, 60)
    assert mon.devices[dev].state.status is not Status.DOWN

    monkeypatch.undo()
    assert mon.record(dev, FAIL, 90) is Event.WENT_DOWN
    assert len(db.recent_incidents(10)) == 1
