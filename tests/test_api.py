import time

import pytest
from fastapi.testclient import TestClient

from netmon.checker import CheckResult
from netmon.main import create_app


@pytest.fixture
def client(tmp_path):
    cfg = tmp_path / "devices.yaml"
    cfg.write_text(
        "devices:\n"
        "  - {name: Router, type: router, ip: 10.0.0.1, check: ping}\n"
        "  - {name: Printer, type: printer, ip: 10.0.0.2, check: tcp, port: 9100}\n",
        encoding="utf-8",
    )
    app = create_app(cfg, tmp_path / "t.db", start_background=False, edit_hosts=("testclient",))
    with TestClient(app) as c:
        c.monitor = app.state.monitor
        c.db = app.state.db
        c.cfg = cfg
        yield c


def test_devices_start_unknown(client):
    devices = client.get("/api/devices").json()
    assert [d["name"] for d in devices] == ["Router", "Printer"]
    assert all(d["status"] == "UNKNOWN" and d["last_checked"] is None for d in devices)


def test_down_device_history_and_incident(client):
    now = time.time()
    printer = next(d for d in client.get("/api/devices").json() if d["name"] == "Printer")
    pid = printer["id"]
    client.monitor.record(pid, CheckResult(True, 3.0), now - 120)
    for t in (now - 90, now - 60, now - 30):
        client.monitor.record(pid, CheckResult(False, None), t)

    d = next(d for d in client.get("/api/devices").json() if d["id"] == pid)
    assert d["status"] == "DOWN"
    assert d["consecutive_failures"] == 3
    assert d["last_latency_ms"] is None
    assert 25 < d["seconds_since_check"] < 60

    hist = client.get(f"/api/devices/{pid}/history?hours=24").json()
    assert [p["is_up"] for p in hist["points"]] == [True, False, False, False]
    assert hist["points"][0]["latency_ms"] == 3.0

    [inc] = client.get("/api/incidents").json()
    assert inc["device_name"] == "Printer" and inc["ongoing"] is True
    assert 85 < inc["duration_s"] < 120

    client.monitor.record(pid, CheckResult(True, 2.0), now)
    [inc] = client.get("/api/incidents").json()
    assert inc["ongoing"] is False and inc["resolved_at"] is not None
    assert inc["duration_s"] == pytest.approx(90, abs=0.2)


def test_history_unknown_device_is_404(client):
    assert client.get("/api/devices/999/history").status_code == 404


def test_history_rejects_bad_hours(client):
    assert client.get("/api/devices/1/history?hours=0").status_code == 422
    assert client.get("/api/devices/1/history?hours=1000").status_code == 422


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_health_ok_then_stuck(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True and body["devices"] == 2 and body["alerts"] == "off"
    client.monitor.last_cycle_at = time.time() - 3600  # loop hasn't run for an hour
    r = client.get("/api/health")
    assert r.status_code == 503 and r.json()["ok"] is False
