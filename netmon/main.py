"""FastAPI app, startup/shutdown, and the `python -m netmon.main` entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .checker import run_check
from .config import (
    DEFAULT_CONFIG_PATH, ConfigError, DeviceConfig, check_unique_names, load_devices,
    parse_device, save_devices,
)
from .db import Database
from .monitor import CHECK_INTERVAL_S, DeviceRuntime, Monitor

log = logging.getLogger("netmon")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
DEFAULT_DB_PATH = PROJECT_ROOT / "netmon.db"

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
) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        devices_cfg = load_devices(config_path)
        db = Database(db_path)
        devices = db.sync_devices(devices_cfg)
        monitor = Monitor(db, devices, interval_s=interval_s)
        app.state.db, app.state.monitor = db, monitor
        log.info("monitoring %d devices every %ss (db: %s)", len(devices), interval_s, db_path)

        tasks = []
        if start_background:
            tasks = [asyncio.create_task(monitor.run()), asyncio.create_task(monitor.run_retention())]
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

    return app


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m netmon.main", description="NetMon network monitor")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="path to devices.yaml")
    p.add_argument("--db", default=str(DEFAULT_DB_PATH), help="path to the SQLite database")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this computer only)")
    p.add_argument("--lan", action="store_true",
                   help="listen on all network interfaces so phones on the same Wi-Fi can open the dashboard")
    p.add_argument("--interval", type=float, default=CHECK_INTERVAL_S, help=argparse.SUPPRESS)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        load_devices(args.config)  # fail fast with a readable message
    except ConfigError as e:
        raise SystemExit(f"Problem in devices.yaml: {e}")
    host = "0.0.0.0" if args.lan else args.host
    app = create_app(args.config, args.db, interval_s=args.interval)
    print(f"\nNetMon dashboard: http://localhost:{args.port}"
          + ("  (also reachable from other devices on your network)" if args.lan else "") + "\n")
    uvicorn.run(app, host=host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
