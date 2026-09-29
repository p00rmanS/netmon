"""FastAPI app, startup/shutdown, and the `python -m netmon.main` entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from .config import DEFAULT_CONFIG_PATH, ConfigError, load_devices
from .db import Database
from .monitor import CHECK_INTERVAL_S, Monitor

log = logging.getLogger("netmon")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
DEFAULT_DB_PATH = PROJECT_ROOT / "netmon.db"


def iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat()


def create_app(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    db_path: str | Path = DEFAULT_DB_PATH,
    interval_s: float = CHECK_INTERVAL_S,
    start_background: bool = True,
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
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            db.close()

    app = FastAPI(title="NetMon", lifespan=lifespan)

    @app.get("/api/devices")
    async def list_devices():
        now = time.time()
        out = []
        for rt in app.state.monitor.snapshot():
            d, s = rt.device, rt.state
            out.append({
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
            })
        return out

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
