import time

import pytest
from fastapi.testclient import TestClient

from netmon.checker import CheckResult
from netmon.config import DeviceConfig, load_devices, save_devices
from netmon.main import create_app

from .test_api import client  # noqa: F401  (shared fixture)

PRINTER = {"name": "Bar Printer", "type": "printer", "ip": "10.0.0.9", "check": "tcp", "port": 9100}


def names(client):
    return [d["name"] for d in client.get("/api/devices").json()]


def device(client, name):
    return next(d for d in client.get("/api/devices").json() if d["name"] == name)


# ------------------------------------------------------------ add

def test_add_device_updates_yaml_and_dashboard(client):
    r = client.post("/api/devices", json=PRINTER)
    assert r.status_code == 201
    assert r.json()["name"] == "Bar Printer" and r.json()["status"] == "UNKNOWN"
    assert "Bar Printer" in names(client)
    saved = load_devices(client.cfg)
    assert saved[-1] == DeviceConfig("Bar Printer", "printer", "10.0.0.9", "tcp", 9100)


def test_add_duplicate_name_rejected(client):
    r = client.post("/api/devices", json={**PRINTER, "name": "Router"})
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


@pytest.mark.parametrize("bad, msg", [
    ({"ip": "10.0.0.999"}, "valid IP"),
    ({"type": "toaster"}, "'type'"),
    ({"port": None}, "port"),
    ({"name": "   "}, "'name'"),
    ({"name": "x" * 61}, "60 characters"),
])
def test_add_invalid_device_rejected_and_yaml_untouched(client, bad, msg):
    before = client.cfg.read_text(encoding="utf-8")
    r = client.post("/api/devices", json={**PRINTER, **bad})
    assert r.status_code == 400 and msg in r.json()["detail"]
    assert client.cfg.read_text(encoding="utf-8") == before


def test_ping_device_drops_port(client):
    client.post("/api/devices", json={**PRINTER, "check": "ping"})
    assert load_devices(client.cfg)[-1].port is None


# ------------------------------------------------------------ edit

def test_rename_keeps_id_and_history(client):
    router = device(client, "Router")
    client.monitor.record(router["id"], CheckResult(True, 1.5), time.time())

    r = client.put(f"/api/devices/{router['id']}",
                   json={"name": "Main Router", "type": "router", "ip": "10.0.0.1", "check": "ping"})
    assert r.status_code == 200
    assert r.json()["id"] == router["id"] and r.json()["status"] == "UP"
    assert names(client) == ["Main Router", "Printer"]
    assert len(client.get(f"/api/devices/{router['id']}/history").json()["points"]) == 1
    assert [d.name for d in load_devices(client.cfg)] == ["Main Router", "Printer"]


def test_rename_to_existing_name_rejected(client):
    router = device(client, "Router")
    r = client.put(f"/api/devices/{router['id']}",
                   json={"name": "Printer", "type": "router", "ip": "10.0.0.1", "check": "ping"})
    assert r.status_code == 400
    assert names(client) == ["Router", "Printer"]


def test_change_ip_keeps_state_and_updates_yaml(client):
    p = device(client, "Printer")
    client.monitor.record(p["id"], CheckResult(True, 2.0), time.time())
    r = client.put(f"/api/devices/{p['id']}", json={**PRINTER, "name": "Printer", "ip": "10.0.0.77"})
    assert r.json()["ip"] == "10.0.0.77" and r.json()["status"] == "UP"
    assert load_devices(client.cfg)[1].ip == "10.0.0.77"


def test_edit_unknown_device_404(client):
    assert client.put("/api/devices/999", json=PRINTER).status_code == 404


# ------------------------------------------------------------ delete

def test_delete_device_and_close_its_open_incident(client):
    p = device(client, "Printer")
    for t in (1, 2, 3):
        client.monitor.record(p["id"], CheckResult(False, None), time.time() - 100 + t)
    assert client.get("/api/incidents").json()[0]["ongoing"] is True

    assert client.delete(f"/api/devices/{p['id']}").status_code == 204
    assert names(client) == ["Router"]
    assert [d.name for d in load_devices(client.cfg)] == ["Router"]
    [inc] = client.get("/api/incidents").json()
    assert inc["ongoing"] is False  # not "ongoing" forever for a removed device


def test_re_adding_removed_device_restores_history(client):
    p = device(client, "Printer")
    client.monitor.record(p["id"], CheckResult(True, 2.0), time.time())
    client.delete(f"/api/devices/{p['id']}")
    r = client.post("/api/devices", json={**PRINTER, "name": "Printer"})
    assert r.json()["id"] == p["id"]
    assert len(client.get(f"/api/devices/{p['id']}/history").json()["points"]) == 1


def test_delete_last_device(client):
    for d in client.get("/api/devices").json():
        assert client.delete(f"/api/devices/{d['id']}").status_code == 204
    assert names(client) == []
    assert load_devices(client.cfg) == []


# ------------------------------------------------------------ test button

def test_check_endpoint_validates(client):
    r = client.post("/api/check", json={"ip": "nope", "check": "ping"})
    assert r.status_code == 400 and "valid IP" in r.json()["detail"]


def test_check_endpoint_runs_real_check(client):
    r = client.post("/api/check", json={"ip": "127.0.0.1", "check": "ping"})
    assert r.status_code == 200 and r.json()["is_up"] is True


# ------------------------------------------------------------ who may edit

def test_meta_reports_edit_permission(client):
    assert client.get("/api/meta").json() == {"can_edit": True}


def test_cross_site_request_blocked(client):
    r = client.post("/api/devices", json=PRINTER, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert "Bar Printer" not in names(client)


def test_same_site_origin_allowed(client):
    r = client.post("/api/devices", json=PRINTER, headers={"Origin": "http://localhost:8000"})
    assert r.status_code == 201


def test_other_computers_cannot_edit(tmp_path):
    cfg = tmp_path / "devices.yaml"
    cfg.write_text("devices:\n  - {name: R, type: router, ip: 10.0.0.1, check: ping}\n", encoding="utf-8")
    app = create_app(cfg, tmp_path / "t.db", start_background=False)  # default: localhost only
    with TestClient(app) as c:  # TestClient's address is "testclient", i.e. not this computer
        assert c.get("/api/meta").json() == {"can_edit": False}
        assert c.post("/api/devices", json=PRINTER).status_code == 403
        assert c.delete("/api/devices/1").status_code == 403
        assert c.post("/api/check", json={"ip": "127.0.0.1", "check": "ping"}).status_code == 403
        assert c.get("/api/devices").status_code == 200  # viewing still works


# ------------------------------------------------------------ yaml writing

def test_save_round_trips_awkward_names(tmp_path):
    path = tmp_path / "devices.yaml"
    devices = [
        DeviceConfig("Bar: #1 'Tap' \"room\"", "pos", "10.0.0.2", "ping"),
        DeviceConfig("yes", "ap", "10.0.0.3", "ping"),
        DeviceConfig("Café Printer ☕", "printer", "10.0.0.4", "tcp", 9100),
    ]
    save_devices(devices, path)
    assert load_devices(path) == devices
    assert path.read_text(encoding="utf-8").startswith("# Devices NetMon watches")
