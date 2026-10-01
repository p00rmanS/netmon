import asyncio

from netmon.alerts import (
    AlertConfig, AlertConfigError, AlertSender, build_alert, format_duration, load_alert_config,
    mark_late,
)
from netmon.checker import CheckResult
from netmon.config import DeviceConfig
from netmon.db import Database
from netmon.monitor import Event, Monitor

import pytest


# ------------------------------------------------------------ messages

def test_down_alert_has_tip_and_start_time():
    a = build_alert("Kitchen Printer", "printer", "10.0.0.5", Event.WENT_DOWN, at=1000.0,
                    started_at=940.0, site="Main St")
    assert a.title == "DOWN: Kitchen Printer at Main St"
    assert "10.0.0.5" in a.message
    assert "What to do:" in a.message and "paper" in a.message
    assert a.priority == 4


def test_up_alert_says_how_long_it_was_down():
    a = build_alert("Router", "router", "10.0.0.1", Event.CAME_UP, at=1000.0, started_at=1000.0 - 7 * 60)
    assert a.title == "Back UP: Router"
    assert "after 7 min" in a.message


@pytest.mark.parametrize("secs,text", [(5, "5 sec"), (90, "1 min"), (3600, "1 h"), (3900, "1 h 5 min")])
def test_format_duration(secs, text):
    assert format_duration(secs) == text


def test_mark_late_only_when_old():
    a = build_alert("R", "router", "1.1.1.1", Event.WENT_DOWN, at=1000.0, started_at=1000.0)
    assert mark_late(a, 1010.0) == a
    assert "Sent late" in mark_late(a, 1000.0 + 600).message


# ------------------------------------------------------------ config

def test_missing_file_means_alerts_off(tmp_path):
    assert not load_alert_config(tmp_path / "nope.yaml").enabled


def test_config_loads(tmp_path):
    p = tmp_path / "alerts.yaml"
    p.write_text("site_name: Cafe\nntfy_topic: my-topic_1\n", encoding="utf-8")
    cfg = load_alert_config(p)
    assert cfg.enabled and cfg.site_name == "Cafe" and cfg.ntfy_server == "https://ntfy.sh"


@pytest.mark.parametrize("body", ["ntfy_topic: has spaces in it\n", "webhook_url: ftp://x\n", "- a\n"])
def test_bad_config_rejected(tmp_path, body):
    p = tmp_path / "alerts.yaml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(AlertConfigError):
        load_alert_config(p)


# ------------------------------------------------------------ monitor -> alerts

@pytest.fixture
def monitor(tmp_path):
    db = Database(tmp_path / "t.db")
    rows = db.sync_devices([DeviceConfig("Printer", "printer", "10.0.0.2", "tcp", 9100)])
    events = []
    m = Monitor(db, rows, on_event=lambda *args: events.append(args))
    m.events = events
    yield m
    db.close()


def test_one_alert_per_outage_with_start_times(monitor):
    did = next(iter(monitor.devices))
    fail, ok = CheckResult(False, None), CheckResult(True, 2.0)
    for t, r in [(0, ok), (30, fail), (60, fail), (90, fail), (120, fail), (150, ok), (180, ok)]:
        monitor.record(did, r, float(t))
    assert [(e[3], e[4], e[5]) for e in monitor.events] == [
        (Event.WENT_DOWN, 90.0, 30.0),   # down at the 3rd failure, failing since 30
        (Event.CAME_UP, 150.0, 30.0),    # back up; outage started at 30
    ]


def test_broken_handler_does_not_break_monitoring(monitor):
    def boom(*args):
        raise RuntimeError("nope")
    monitor.on_event = boom
    did = next(iter(monitor.devices))
    for t in (0, 30, 60):
        monitor.record(did, CheckResult(False, None), float(t))
    assert monitor.devices[did].state.status.value == "DOWN"


# ------------------------------------------------------------ delivery

def test_sender_retries_until_delivered(monkeypatch):
    monkeypatch.setattr("netmon.alerts.RETRY_DELAYS_S", (0,))
    sent, attempts = [], []

    def flaky(cfg, alert):
        attempts.append(alert.title)
        if len(attempts) < 3:
            raise OSError("no internet")
        sent.append(alert.title)

    async def scenario():
        s = AlertSender(AlertConfig(ntfy_topic="t"), send=flaky)
        task = asyncio.create_task(s.run())
        s.on_event("Router", "router", "1.1.1.1", Event.WENT_DOWN, 0.0, 0.0)
        s.on_event("Router", "router", "1.1.1.1", Event.CAME_UP, 60.0, 0.0)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not s.queue:
                break
        task.cancel()
        return s

    s = asyncio.run(scenario())
    assert sent == ["DOWN: Router", "Back UP: Router"]  # in order, nothing lost
    assert not s.queue
