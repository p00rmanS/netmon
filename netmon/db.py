"""SQLite storage: schema, device sync, check/incident writes, and queries.

Timestamps are stored as Unix epoch seconds (REAL, UTC) so comparisons and
retention are simple numeric operations; the API converts them to ISO 8601.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import DeviceConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL UNIQUE,
    type         TEXT    NOT NULL,
    ip           TEXT    NOT NULL,
    check_method TEXT    NOT NULL,
    port         INTEGER,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS checks (
    id         INTEGER PRIMARY KEY,
    device_id  INTEGER NOT NULL REFERENCES devices(id),
    timestamp  REAL    NOT NULL,
    is_up      INTEGER NOT NULL,
    latency_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_checks_device_ts ON checks(device_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_checks_ts ON checks(timestamp);

CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES devices(id),
    started_at  REAL    NOT NULL,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_incidents_started ON incidents(started_at);
-- At most one open incident per device, enforced by the database itself.
CREATE UNIQUE INDEX IF NOT EXISTS idx_incidents_one_open
    ON incidents(device_id) WHERE resolved_at IS NULL;
"""


@dataclass(frozen=True)
class DeviceRow:
    id: int
    name: str
    type: str
    ip: str
    check_method: str
    port: int | None
    active: bool
    created_at: float


def _device_row(r: sqlite3.Row) -> DeviceRow:
    return DeviceRow(
        id=r["id"], name=r["name"], type=r["type"], ip=r["ip"],
        check_method=r["check_method"], port=r["port"],
        active=bool(r["active"]), created_at=r["created_at"],
    )


class Database:
    """Thin wrapper around one SQLite connection.

    The monitor loop and the API's worker threads share this connection, so
    every access goes through a lock. Writes are tiny; contention is negligible.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ devices

    def sync_devices(self, configs: Iterable[DeviceConfig], now: float | None = None) -> list[DeviceRow]:
        """Make the devices table match devices.yaml; return the active devices.

        Matched by name: new names are inserted, changed ones updated, and names
        no longer in the file are marked inactive (their history is kept).
        """
        now = time.time() if now is None else now
        configs = list(configs)
        names = [c.name for c in configs]
        with self._lock, self._conn:
            self._conn.execute("BEGIN")
            for c in configs:
                self._conn.execute(
                    """
                    INSERT INTO devices (name, type, ip, check_method, port, active, created_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        type = excluded.type, ip = excluded.ip,
                        check_method = excluded.check_method, port = excluded.port,
                        active = 1
                    """,
                    (c.name, c.type, c.ip, c.check, c.port, now),
                )
            placeholders = ",".join("?" * len(names))
            self._conn.execute(
                f"UPDATE devices SET active = 0 WHERE name NOT IN ({placeholders})", names
            )
            # A removed device can't recover, so don't leave its incident "ongoing" forever.
            self._conn.execute(
                """UPDATE incidents SET resolved_at = ?
                   WHERE resolved_at IS NULL
                     AND device_id IN (SELECT id FROM devices WHERE active = 0)""",
                (now,),
            )
        return self.active_devices()

    def rename_device(self, device_id: int, new_name: str) -> None:
        """Rename a device in place so it keeps its id and history.

        Raises ValueError if another device (even a removed one) has the name.
        """
        with self._lock, self._conn:
            self._conn.execute("BEGIN")
            clash = self._conn.execute(
                "SELECT active FROM devices WHERE name = ? AND id != ?", (new_name, device_id)
            ).fetchone()
            if clash is not None:
                which = "another device" if clash["active"] else "a previously removed device"
                raise ValueError(f"the name {new_name!r} is already used by {which}")
            self._conn.execute("UPDATE devices SET name = ? WHERE id = ?", (new_name, device_id))

    def active_devices(self) -> list[DeviceRow]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM devices WHERE active = 1 ORDER BY id").fetchall()
        return [_device_row(r) for r in rows]

    def get_device(self, device_id: int) -> DeviceRow | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        return _device_row(r) if r else None

    # ------------------------------------------------------------ writes

    def record_check(
        self,
        device_id: int,
        timestamp: float,
        is_up: bool,
        latency_ms: float | None,
        open_incident_at: float | None = None,
        close_incident_at: float | None = None,
    ) -> None:
        """Store one check result, and open/close an incident, in one transaction."""
        with self._lock, self._conn:
            self._conn.execute("BEGIN")
            self._conn.execute(
                "INSERT INTO checks (device_id, timestamp, is_up, latency_ms) VALUES (?, ?, ?, ?)",
                (device_id, timestamp, int(is_up), latency_ms if is_up else None),
            )
            if open_incident_at is not None:
                self._conn.execute(
                    """
                    INSERT INTO incidents (device_id, started_at)
                    SELECT ?, ? WHERE NOT EXISTS (
                        SELECT 1 FROM incidents WHERE device_id = ? AND resolved_at IS NULL)
                    """,
                    (device_id, open_incident_at, device_id),
                )
            if close_incident_at is not None:
                self._conn.execute(
                    "UPDATE incidents SET resolved_at = ? WHERE device_id = ? AND resolved_at IS NULL",
                    (close_incident_at, device_id),
                )

    def delete_checks_before(self, cutoff: float) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM checks WHERE timestamp < ?", (cutoff,))
        return cur.rowcount

    # ------------------------------------------------------------ reads

    def open_incident_start(self, device_id: int) -> float | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT started_at FROM incidents WHERE device_id = ? AND resolved_at IS NULL",
                (device_id,),
            ).fetchone()
        return r["started_at"] if r else None

    def recent_checks(self, device_id: int, limit: int) -> list[tuple[float, bool, float | None]]:
        """Newest-first (timestamp, is_up, latency_ms) tuples."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT timestamp, is_up, latency_ms FROM checks
                   WHERE device_id = ? ORDER BY timestamp DESC LIMIT ?""",
                (device_id, limit),
            ).fetchall()
        return [(r["timestamp"], bool(r["is_up"]), r["latency_ms"]) for r in rows]

    def history(self, device_id: int, since: float) -> list[tuple[float, bool, float | None]]:
        """Oldest-first (timestamp, is_up, latency_ms) tuples since a time."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT timestamp, is_up, latency_ms FROM checks
                   WHERE device_id = ? AND timestamp >= ? ORDER BY timestamp""",
                (device_id, since),
            ).fetchall()
        return [(r["timestamp"], bool(r["is_up"]), r["latency_ms"]) for r in rows]

    def recent_incidents(self, limit: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT i.id, i.device_id, d.name AS device_name, d.type AS device_type,
                          i.started_at, i.resolved_at
                   FROM incidents i JOIN devices d ON d.id = i.device_id
                   ORDER BY i.started_at DESC, i.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def incidents_overlapping(self, start: float, end: float) -> list[dict]:
        """Incidents (any device) that were ongoing at some point in [start, end)."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, device_id, started_at, resolved_at FROM incidents
                   WHERE started_at < ? AND (resolved_at IS NULL OR resolved_at > ?)
                   ORDER BY started_at""",
                (end, start),
            ).fetchall()
        return [dict(r) for r in rows]

    def check_stats(self, start: float, end: float) -> dict[int, dict]:
        """Per device: number of checks, and average latency of successful ones, in [start, end)."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT device_id, COUNT(*) AS checks, AVG(latency_ms) AS avg_latency_ms
                   FROM checks WHERE timestamp >= ? AND timestamp < ?
                   GROUP BY device_id""",
                (start, end),
            ).fetchall()
        return {r["device_id"]: {"checks": r["checks"], "avg_latency_ms": r["avg_latency_ms"]} for r in rows}
