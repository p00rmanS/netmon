import time
from datetime import datetime

import pytest

from netmon.checker import CheckResult
from netmon.db import DeviceRow
from netmon.report import build_report, clipped, next_weekly, summary_text

DAY = 86400.0
START, END = 1_000_000.0, 1_000_000.0 + 7 * DAY
INTERVAL = 30.0
FULL = int(7 * DAY / INTERVAL)  # checks in a fully covered week


def dev(id, name, created_at=0.0, type="printer"):
    return DeviceRow(id, name, type, f"10.0.0.{id}", "ping", None, True, created_at)


def inc(device_id, started_at, resolved_at):
    return {"device_id": device_id, "started_at": started_at, "resolved_at": resolved_at}


def stats(*ids, checks=FULL, latency=5.0):
    return {i: {"checks": checks, "avg_latency_ms": latency} for i in ids}


# ------------------------------------------------------------ clipping

@pytest.mark.parametrize("start,end,expected", [
    (10, 20, 10),      # fully inside
    (-5, 5, 5),        # began before the period
    (95, 120, 5),      # ended after the period
    (90, None, 10),    # still going: counts to the end of the period
    (120, 130, 0),     # entirely after
])
def test_clipped(start, end, expected):
    assert clipped(start, end, 0, 100) == expected


# ------------------------------------------------------------ build_report

def test_quiet_week_is_100_percent():
    r = build_report([dev(1, "A"), dev(2, "B")], [], stats(1, 2), START, END, INTERVAL)
    assert r.uptime_pct == 100.0 and r.coverage_pct == 100.0
    assert r.outages == [] and r.downtime_s == 0


def test_downtime_and_uptime_per_device():
    incidents = [inc(1, START + DAY, START + DAY + 600), inc(1, START + 2 * DAY, START + 2 * DAY + 300)]
    r = build_report([dev(1, "Printer"), dev(2, "Router")], incidents, stats(1, 2), START, END, INTERVAL)
    p = next(d for d in r.devices if d.name == "Printer")
    assert (p.outages, p.downtime_s, p.longest_s) == (2, 900, 600)
    assert p.uptime_pct == pytest.approx(100 * (1 - 900 / (7 * DAY)))
    assert r.devices[0].name == "Printer"  # worst first
    assert r.downtime_s == 900
    # Overall uptime is across all device-time, so one device's outage counts half.
    assert r.uptime_pct == pytest.approx(100 * (1 - 900 / (14 * DAY)))


def test_outages_crossing_the_edges_only_count_inside():
    incidents = [inc(1, START - 3600, START + 600), inc(1, END - 300, None)]
    r = build_report([dev(1, "A")], incidents, stats(1), START, END, INTERVAL)
    assert r.downtime_s == 900
    ongoing = next(o for o in r.outages if o.resolved_at is None)
    assert ongoing.duration_s == 300


def test_outage_resolved_after_the_period_is_shown_as_ongoing_then():
    r = build_report([dev(1, "A")], [inc(1, END - 60, END + 60)], stats(1), START, END, INTERVAL)
    assert r.outages[0].resolved_at is None and r.outages[0].duration_s == 60


def test_device_added_midweek_is_judged_from_when_it_was_added():
    added = END - DAY
    r = build_report([dev(1, "New", created_at=added)], [], stats(1, checks=int(DAY / INTERVAL)),
                     START, END, INTERVAL)
    assert r.devices[0].coverage_pct == pytest.approx(100.0)


def test_partial_coverage_is_reported_and_uptime_uses_watched_time():
    half = FULL // 2
    r = build_report([dev(1, "A")], [inc(1, START + DAY, START + DAY + 3600)], stats(1, checks=half),
                     START, END, INTERVAL)
    assert r.coverage_pct == pytest.approx(50.0, abs=0.01)
    assert r.devices[0].uptime_pct == pytest.approx(100 * (1 - 3600 / (half * INTERVAL)))


def test_never_checked_device_has_no_uptime():
    r = build_report([dev(1, "A")], [], {}, START, END, INTERVAL)
    assert r.devices[0].uptime_pct is None and r.uptime_pct is None


def test_removed_devices_are_ignored():
    r = build_report([dev(1, "A")], [inc(99, START + 10, START + 20)], stats(1), START, END, INTERVAL)
    assert r.outages == []


def test_previous_week_comparison():
    prev = [inc(1, START - 2 * DAY, START - 2 * DAY + 60), inc(1, START - DAY, START - DAY + 60),
            inc(1, START - 10 * DAY, START - 10 * DAY + 60)]  # two weeks ago: not counted
    r = build_report([dev(1, "A")], [], stats(1), START, END, INTERVAL, previous_incidents=prev)
    assert (r.previous_outages, r.previous_downtime_s) == (2, 120)


# ------------------------------------------------------------ summary text

def test_summary_for_a_quiet_week():
    r = build_report([dev(1, "A"), dev(2, "B")], [], stats(1, 2), START, END, INTERVAL, site_name="Main St")
    title, msg = summary_text(r)
    assert title == "Weekly network report: Main St"
    assert "No outages" in msg and "2 devices" in msg


def test_summary_names_the_worst_devices_and_compares():
    incidents = [inc(1, START + 100, START + 700), inc(1, START + DAY, START + DAY + 60),
                 inc(2, START + 2 * DAY, START + 2 * DAY + 120)]
    r = build_report([dev(1, "Kitchen Printer"), dev(2, "Patio Wi-Fi", type="ap"), dev(3, "Router")],
                     incidents, stats(1, 2, 3), START, END, INTERVAL, previous_incidents=[])
    _, msg = summary_text(r)
    assert "3 outages, 13 min down in total" in msg
    assert "Kitchen Printer: 2 outages (11 min)" in msg
    assert "Patio Wi-Fi: 1 outage (2 min)" in msg
    assert "Everything else: no problems." in msg
    assert "3 more outages than the week before" in msg


def test_summary_warns_about_low_coverage():
    _, msg = summary_text(build_report([dev(1, "A")], [], stats(1, checks=FULL // 2), START, END, INTERVAL))
    assert "only running 50%" in msg


# ------------------------------------------------------------ scheduling

@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 10, 4, 15, 0), datetime(2026, 10, 5, 9, 0)),   # Sunday -> next day
    (datetime(2026, 10, 5, 8, 59), datetime(2026, 10, 5, 9, 0)),   # Monday before 9
    (datetime(2026, 10, 5, 9, 0), datetime(2026, 10, 12, 9, 0)),   # exactly 9: next week
    (datetime(2026, 10, 6, 7, 0), datetime(2026, 10, 12, 9, 0)),   # Tuesday
])
def test_next_weekly(now, expected):
    assert next_weekly(now) == expected


# ------------------------------------------------------------ through the API

def test_report_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from netmon.main import create_app
    cfg = tmp_path / "devices.yaml"
    cfg.write_text("devices:\n  - {name: P, type: printer, ip: 10.0.0.2, check: tcp, port: 9100}\n",
                   encoding="utf-8")
    app = create_app(cfg, tmp_path / "t.db", start_background=False)
    with TestClient(app) as c:
        did = c.get("/api/devices").json()[0]["id"]
        now = time.time()
        for i, ok in enumerate([True, False, False, False, True]):
            app.state.monitor.record(did, CheckResult(ok, 2.0 if ok else None), now - 150 + i * 30)
        body = c.get("/api/report?days=7").json()
        assert body["outage_count"] == 1
        assert body["devices"][0]["outages"] == 1 and body["devices"][0]["downtime_s"] == 90
        assert body["outages"][0]["ongoing"] is False
        assert c.get("/api/report?days=30").status_code == 422  # older checks don't exist
        assert c.get("/report").status_code == 200


def test_new_install_reports_from_when_it_started_and_skips_comparison():
    began = END - 2 * DAY
    r = build_report([dev(1, "A", created_at=began)], [], stats(1, checks=int(2 * DAY / INTERVAL)),
                     START, END, INTERVAL, previous_incidents=[])
    assert r.watching_since == began
    assert r.previous_outages is None  # no full week before to compare with
    _, msg = summary_text(r)
    assert "only started watching 2 h" not in msg and "started watching 48 h ago" in msg
    assert "than the week before" not in msg


def test_comparison_needs_the_whole_previous_week_watched():
    r = build_report([dev(1, "A", created_at=START - 3 * DAY)], [], stats(1), START, END, INTERVAL,
                     previous_incidents=[])
    assert r.previous_outages is None
