"""FastAPI app, startup/shutdown, and the `python -m netmon.main` entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import re
import shutil
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .alerts import (
    DEFAULT_ALERTS_PATH, Alert, AlertConfigError, AlertSender, build_alert, build_started_alert,
    deliver, load_alert_config,
)
from .checker import run_check
from .config import (
    DEFAULT_CONFIG_PATH, ConfigError, DeviceConfig, check_unique_names, load_devices,
    parse_device, save_devices,
)
from .db import Database
from .monitor import CHECK_INTERVAL_S, DeviceRuntime, Event, Monitor
from .report import Report, build_report, next_weekly, summary_text

log = logging.getLogger("netmon")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
DEFAULT_DB_PATH = PROJECT_ROOT / "netmon.db"
DEFAULT_LOG_PATH = PROJECT_ROOT / "netmon.log"

# Clients allowed to change the device list: only the NetMon computer itself.
LOCAL_HOSTS = ("127.0.0.1", "::1")
LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}
EDIT_FORBIDDEN = (
    "Devices can only be changed on the NetMon computer itself. "
    "Open http://localhost:8000 there."
)


class DeviceIn(BaseModel):
    name: str
    type: str
    ip: str
    check: str
    port: int | None = None


class CheckIn(BaseModel):
    ip: str
    check: str
    port: int | None = None


def iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat()


def device_json(rt: DeviceRuntime, now: float) -> dict:
    d, s = rt.device, rt.state
    return {
        "id": d.id,
        "name": d.name,
        "type": d.type,
        "ip": d.ip,
        "check_method": d.check_method,
        "port": d.port,
        "status": s.status.value,
        "consecutive_failures": s.consecutive_failures,
        "last_latency_ms": rt.last_latency_ms,
        "last_checked": iso(rt.last_checked),
        # Computed server-side so a phone with a wrong clock still shows the right age.
        "seconds_since_check": None if rt.last_checked is None else round(now - rt.last_checked, 1),
        "failing_since": iso(s.first_failure_at),
        "seconds_failing": None if s.first_failure_at is None else round(now - s.first_failure_at, 1),
    }


def form_error(e: ConfigError) -> HTTPException:
    """Turn "device 'X': 'ip' is not valid..." into "'ip' is not valid..." for the form."""
    return HTTPException(400, re.sub(r"^device (#\d+|'.*?'): ", "", str(e)))


def parse_body(body: DeviceIn) -> DeviceConfig:
    try:
        return parse_device(body.model_dump(), 0)
    except ConfigError as e:
        raise form_error(e) from None


def create_app(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    db_path: str | Path = DEFAULT_DB_PATH,
    interval_s: float = CHECK_INTERVAL_S,
    start_background: bool = True,
    edit_hosts: tuple[str, ...] = LOCAL_HOSTS,
    alerts_path: str | Path | None = None,
) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        devices_cfg = load_devices(config_path)
        alert_cfg = load_alert_config(alerts_path) if alerts_path else None
        db = Database(db_path)
        devices = db.sync_devices(devices_cfg)
        sender = AlertSender(alert_cfg) if alert_cfg and alert_cfg.enabled else None
        app.state.sender, app.state.started_at = sender, time.time()
        app.state.site_name = alert_cfg.site_name if alert_cfg else ""
        monitor = Monitor(db, devices, interval_s=interval_s,
                          on_event=sender.on_event if sender else None)
        app.state.db, app.state.monitor = db, monitor
        log.info("monitoring %d devices every %ss (db: %s)", len(devices), interval_s, db_path)
        log.info("alerts: %s", "on" if sender else "off (no alerts.yaml)")

        tasks = []
        if start_background:
            tasks = [asyncio.create_task(monitor.run()), asyncio.create_task(monitor.run_retention())]
            if sender:
                if alert_cfg.notify_on_start:
                    sender.send(build_started_alert(len(devices), time.time(), alert_cfg.site_name))
                tasks.append(asyncio.create_task(sender.run()))
                if alert_cfg.weekly_summary:
                    tasks.append(asyncio.create_task(weekly_summaries(app, sender)))
        try:
            yield
        finally:
            for t in tasks + list(extra_tasks):
                t.cancel()
            await asyncio.gather(*tasks, *extra_tasks, return_exceptions=True)
            db.close()

    app = FastAPI(title="NetMon", lifespan=lifespan)
    edit_lock = asyncio.Lock()
    extra_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ read-only

    @app.get("/api/devices")
    async def list_devices():
        now = time.time()
        return [device_json(rt, now) for rt in app.state.monitor.snapshot()]

    @app.get("/api/devices/{device_id}/history")
    async def device_history(device_id: int, hours: float = Query(24, gt=0, le=168)):
        db: Database = app.state.db
        device = db.get_device(device_id)
        if device is None:
            raise HTTPException(404, "device not found")
        rows = db.history(device_id, time.time() - hours * 3600)
        return {
            "device_id": device_id,
            "name": device.name,
            "hours": hours,
            "points": [
                {"t": iso(ts), "is_up": ok, "latency_ms": lat} for ts, ok, lat in rows
            ],
        }

    @app.get("/api/incidents")
    async def incidents(limit: int = Query(50, ge=1, le=500)):
        now = time.time()
        out = []
        for inc in app.state.db.recent_incidents(limit):
            end = inc["resolved_at"] if inc["resolved_at"] is not None else now
            out.append({
                "id": inc["id"],
                "device_id": inc["device_id"],
                "device_name": inc["device_name"],
                "device_type": inc["device_type"],
                "started_at": iso(inc["started_at"]),
                "resolved_at": iso(inc["resolved_at"]),
                "ongoing": inc["resolved_at"] is None,
                "duration_s": round(end - inc["started_at"], 1),
            })
        return out

    @app.get("/api/report")
    async def report(days: float = Query(7, gt=0, le=7)):
        """Outages, downtime and uptime per device. At most 7 days: older checks are deleted."""
        return report_json(make_report(app, time.time(), days))

    @app.get("/api/health")
    async def health(response: Response):
        """Is NetMon itself working? 503 if the check loop has stopped running."""
        now = time.time()
        last = app.state.monitor.last_cycle_at
        # Allow a full interval plus the 2 s check timeout, twice over, before calling it stuck.
        since = last if last is not None else app.state.started_at
        ok = now - since < 2 * (interval_s + 5) + 10
        if not ok:
            response.status_code = 503
        sender = app.state.sender
        return {
            "ok": ok,
            "uptime_s": round(now - app.state.started_at),
            "last_check_cycle": iso(last),
            "devices": len(app.state.monitor.devices),
            "alerts": "off" if sender is None else "on",
            "alerts_waiting": 0 if sender is None else len(sender.queue),
        }

    # ------------------------------------------------------------ editing devices

    def is_local(request: Request) -> bool:
        host = request.client.host if request.client else ""
        if host not in edit_hosts:
            return False
        # Browsers send Origin on cross-site POST/PUT/DELETE; reject other sites
        # so a web page you visit can't change your devices behind your back.
        origin = request.headers.get("origin")
        return not origin or urlsplit(origin).hostname in LOCAL_NAMES

    def require_local(request: Request) -> None:
        if not is_local(request):
            raise HTTPException(403, EDIT_FORBIDDEN)

    def load_current() -> list[DeviceConfig]:
        try:
            return load_devices(config_path)
        except ConfigError as e:
            raise HTTPException(409, f"devices.yaml has a problem; fix it by hand first: {e}") from None

    def find_index(current: list[DeviceConfig], device_id: int) -> int:
        row = app.state.db.get_device(device_id)
        if row is not None and row.active:
            for i, c in enumerate(current):
                if c.name == row.name:
                    return i
        raise HTTPException(404, "device not found")

    def apply(new_list: list[DeviceConfig]) -> None:
        """Sync the database and live monitor to a saved device list."""
        rows = app.state.db.sync_devices(new_list)
        to_check = app.state.monitor.reload(rows)
        if to_check and start_background:
            task = asyncio.create_task(app.state.monitor.check_all(to_check))
            extra_tasks.add(task)
            task.add_done_callback(extra_tasks.discard)

    def runtime_by_name(name: str) -> dict:
        now = time.time()
        for rt in app.state.monitor.snapshot():
            if rt.device.name == name:
                return device_json(rt, now)
        raise HTTPException(500, "device saved but not found")

    @app.get("/api/meta")
    async def meta(request: Request):
        return {"can_edit": is_local(request)}

    @app.post("/api/devices", status_code=201)
    async def add_device(body: DeviceIn, request: Request):
        require_local(request)
        new = parse_body(body)
        async with edit_lock:
            current = load_current()
            if any(c.name == new.name for c in current):
                raise HTTPException(400, f"a device named {new.name!r} already exists")
            current.append(new)
            save_devices(current, config_path)
            apply(current)
            return runtime_by_name(new.name)

    @app.put("/api/devices/{device_id}")
    async def update_device(device_id: int, body: DeviceIn, request: Request):
        require_local(request)
        new = parse_body(body)
        async with edit_lock:
            current = load_current()
            idx = find_index(current, device_id)
            old_name = current[idx].name
            current[idx] = new
            try:
                check_unique_names(current)
            except ConfigError:
                raise HTTPException(400, f"a device named {new.name!r} already exists") from None

            renamed = new.name != old_name
            if renamed:
                # Rename in the database first so the device keeps its history.
                try:
                    app.state.db.rename_device(device_id, new.name)
                except ValueError as e:
                    raise HTTPException(400, str(e)) from None
            try:
                save_devices(current, config_path)
            except Exception:
                if renamed:
                    app.state.db.rename_device(device_id, old_name)
                raise
            apply(current)
            return runtime_by_name(new.name)

    @app.delete("/api/devices/{device_id}", status_code=204)
    async def delete_device(device_id: int, request: Request):
        require_local(request)
        async with edit_lock:
            current = load_current()
            del current[find_index(current, device_id)]
            save_devices(current, config_path)
            apply(current)
        return Response(status_code=204)

    @app.post("/api/check")
    async def test_check(body: CheckIn, request: Request):
        """Try a check once without saving anything (the "Test" button)."""
        require_local(request)
        try:
            cfg = parse_device({"name": "test", "type": "router", **body.model_dump()}, 0)
        except ConfigError as e:
            raise form_error(e) from None
        result = await run_check(cfg.check, cfg.ip, cfg.port)
        return {"is_up": result.is_up, "latency_ms": result.latency_ms, "error": result.error}

    # ------------------------------------------------------------ page

    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/report", include_in_schema=False)
    async def report_page():
        return FileResponse(STATIC_DIR / "report.html", headers={"Cache-Control": "no-cache"})

    return app


def make_report(app: FastAPI, end: float, days: float) -> Report:
    db: Database = app.state.db
    start = end - days * 86400
    return build_report(
        db.active_devices(), db.incidents_overlapping(start, end), db.check_stats(start, end),
        start, end, app.state.monitor.interval_s,
        previous_incidents=db.incidents_overlapping(start - (end - start), start),
        site_name=app.state.site_name,
    )


def report_json(r: Report) -> dict:
    return {
        "site_name": r.site_name,
        "start": iso(r.start),
        "watching_since": iso(r.watching_since),
        "end": iso(r.end),
        "uptime_pct": r.uptime_pct,
        "coverage_pct": r.coverage_pct,
        "outage_count": len(r.outages),
        "downtime_s": round(r.downtime_s),
        "previous_outage_count": r.previous_outages,
        "previous_downtime_s": None if r.previous_downtime_s is None else round(r.previous_downtime_s),
        "devices": [{
            "id": d.id, "name": d.name, "type": d.type, "outages": d.outages,
            "downtime_s": round(d.downtime_s), "longest_s": round(d.longest_s),
            "uptime_pct": d.uptime_pct, "coverage_pct": d.coverage_pct,
            "avg_latency_ms": d.avg_latency_ms,
        } for d in r.devices],
        "outages": [{
            "device_id": o.device_id, "device_name": o.device_name, "device_type": o.device_type,
            "started_at": iso(o.started_at), "resolved_at": iso(o.resolved_at),
            "ongoing": o.resolved_at is None, "duration_s": round(o.duration_s),
        } for o in r.outages],
    }


async def weekly_summaries(app: FastAPI, sender: AlertSender) -> None:
    """Every Monday at 9:00 (this computer's time), send last week's summary."""
    while True:
        wake = next_weekly(datetime.now()).timestamp()
        await asyncio.sleep(max(1.0, wake - time.time()))
        try:
            now = time.time()
            title, message = summary_text(await asyncio.to_thread(make_report, app, now, 7))
            sender.send(Alert(title, message, priority=3, tags=("bar_chart",), created_at=now))
        except Exception:
            log.exception("weekly summary failed")
        await asyncio.sleep(60)  # never send twice for the same Monday


def send_test_alert(cfg) -> None:
    if not cfg.enabled:
        raise SystemExit("Alerts are off: copy alerts.example.yaml to alerts.yaml and fill it in.")
    now = time.time()
    alert = build_alert("Test printer", "printer", "192.168.1.50", Event.WENT_DOWN, now, now - 90,
                        cfg.site_name)
    alert = replace(alert, title="TEST: " + alert.title)
    try:
        deliver(cfg, alert)
    except OSError as e:
        raise SystemExit(f"Couldn't send the test alert: {e}")
    print("Test alert sent. Check your phone.")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m netmon.main", description="NetMon network monitor")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="path to devices.yaml")
    p.add_argument("--db", default=str(DEFAULT_DB_PATH), help="path to the SQLite database")
    p.add_argument("--alerts", default=str(DEFAULT_ALERTS_PATH), help="path to alerts.yaml")
    p.add_argument("--log-file", default=str(DEFAULT_LOG_PATH),
                   help="where to keep the log (kept small automatically); '' to turn off")
    p.add_argument("--test-alert", action="store_true",
                   help="send one test alert using alerts.yaml, then exit")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this computer only)")
    p.add_argument("--lan", action="store_true",
                   help="listen on all network interfaces so phones on the same Wi-Fi can open the dashboard")
    p.add_argument("--interval", type=float, default=CHECK_INTERVAL_S, help=argparse.SUPPRESS)
    args = p.parse_args()

    # No console when started in the background (pythonw on Windows), so log to the file only.
    handlers: list[logging.Handler] = [logging.StreamHandler()] if sys.stderr else []
    if args.log_file:
        # Three files of 1 MB at most, so it can run for months without filling the disk.
        handlers.append(logging.handlers.RotatingFileHandler(
            args.log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Path(args.config)
    example = PROJECT_ROOT / "devices.example.yaml"
    if not config.exists() and example.exists():
        shutil.copyfile(example, config)  # first run: start from the example list
        print(f"Created {config.name} from the example. Edit it, or use 'Manage devices' on the dashboard.")
    try:
        load_devices(args.config)  # fail fast with a readable message
    except ConfigError as e:
        raise SystemExit(f"Problem in devices.yaml: {e}")
    try:
        alert_cfg = load_alert_config(args.alerts)
    except AlertConfigError as e:
        raise SystemExit(f"Problem in alerts.yaml: {e}")
    if args.test_alert:
        send_test_alert(alert_cfg)
        return
    host = "0.0.0.0" if args.lan else args.host
    app = create_app(args.config, args.db, interval_s=args.interval, alerts_path=args.alerts)
    print(f"\nNetMon dashboard: http://localhost:{args.port}"
          + ("  (also reachable from other devices on your network)" if args.lan else "") + "\n")
    uvicorn.run(app, host=host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
