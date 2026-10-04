"""The weekly report: outages, downtime and uptime per device over a period.

Pure functions with no I/O, like the status logic, so the Phase 2 cloud
dashboard can build the same report from its own data.

Downtime comes from incidents, which start at the first failed check (not the
third), so it's the real time a device was unreachable. "Coverage" is how much
of the period NetMon was actually running and checking; uptime can only be
trusted as far as coverage goes, so the report always shows both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .alerts import format_duration
from .db import DeviceRow


@dataclass(frozen=True)
class Outage:
    device_id: int
    device_name: str
    device_type: str
    started_at: float
    resolved_at: float | None  # None: still down at the end of the period
    duration_s: float          # within the period


@dataclass
class DeviceReport:
    id: int
    name: str
    type: str
    outages: int = 0
    downtime_s: float = 0.0
    longest_s: float = 0.0
    uptime_pct: float | None = None    # None if NetMon never checked it in the period
    coverage_pct: float | None = None
    avg_latency_ms: float | None = None


@dataclass
class Report:
    start: float
    end: float
    devices: list[DeviceReport]
    outages: list[Outage]
    uptime_pct: float | None
    coverage_pct: float | None
    downtime_s: float
    previous_outages: int | None = None   # same-length period just before, for comparison
    previous_downtime_s: float | None = None
    site_name: str = ""
    worst: list[DeviceReport] = field(default_factory=list)
    watching_since: float = 0.0  # later than `start` when NetMon is newer than the period


def clipped(start: float, end: float | None, lo: float, hi: float) -> float:
    """Seconds of [start, end) that fall inside [lo, hi). end None = still going."""
    end = hi if end is None else end
    return max(0.0, min(end, hi) - max(start, lo))


def build_report(
    devices: list[DeviceRow],
    incidents: list[dict],
    stats: dict[int, dict],
    start: float,
    end: float,
    interval_s: float,
    previous_incidents: list[dict] | None = None,
    site_name: str = "",
) -> Report:
    """incidents: dicts with device_id, started_at, resolved_at overlapping [start, end).
    stats: device_id -> {"checks": n, "avg_latency_ms": x} for [start, end).
    previous_incidents: the same, for the period of equal length just before `start`.
    """
    by_id = {d.id: d for d in devices}
    reports = {d.id: DeviceReport(d.id, d.name, d.type) for d in devices}
    outages = []

    for inc in incidents:
        rep = reports.get(inc["device_id"])
        if rep is None:
            continue  # device no longer monitored
        dur = clipped(inc["started_at"], inc["resolved_at"], start, end)
        if dur <= 0:
            continue
        rep.outages += 1
        rep.downtime_s += dur
        rep.longest_s = max(rep.longest_s, dur)
        d = by_id[inc["device_id"]]
        resolved = inc["resolved_at"] if inc["resolved_at"] is not None and inc["resolved_at"] <= end else None
        outages.append(Outage(d.id, d.name, d.type, inc["started_at"], resolved, dur))

    watched_total = covered_total = down_total = 0.0
    for d in devices:
        rep = reports[d.id]
        # Only count the part of the period after the device was added.
        watched = end - max(start, d.created_at)
        s = stats.get(d.id)
        if watched <= 0 or not s or not s["checks"]:
            continue
        covered = min(watched, s["checks"] * interval_s)
        rep.coverage_pct = 100.0 * covered / watched
        # Uptime over the time NetMon was actually watching. Downtime is real
        # (from incidents) even when coverage is partial.
        rep.uptime_pct = max(0.0, 100.0 * (1 - rep.downtime_s / max(covered, rep.downtime_s, 1)))
        rep.avg_latency_ms = s["avg_latency_ms"]
        watched_total += watched
        covered_total += covered
        down_total += min(rep.downtime_s, covered)

    # Only compare with the week before if NetMon was watching all of it;
    # a partly watched week would undercount and make this week look worse.
    first_seen = min((d.created_at for d in devices), default=end)
    length = end - start
    prev_outages = prev_down = None
    if previous_incidents is not None and first_seen <= start - length:
        prev = [i for i in previous_incidents if i["device_id"] in reports
                and clipped(i["started_at"], i["resolved_at"], start - length, start) > 0]
        prev_outages = len(prev)
        prev_down = sum(clipped(i["started_at"], i["resolved_at"], start - length, start) for i in prev)

    device_list = sorted(reports.values(), key=lambda r: (-r.downtime_s, -r.outages, r.name.lower()))
    return Report(
        start=start, end=end, devices=device_list,
        outages=sorted(outages, key=lambda o: o.started_at, reverse=True),
        uptime_pct=None if not covered_total else 100.0 * (1 - down_total / covered_total),
        coverage_pct=None if not watched_total else 100.0 * covered_total / watched_total,
        downtime_s=sum(r.downtime_s for r in device_list),
        previous_outages=prev_outages, previous_downtime_s=prev_down,
        site_name=site_name,
        worst=[r for r in device_list if r.outages][:3],
        watching_since=max(start, first_seen),
    )


# ---------------------------------------------------------------- summary text

def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def summary_text(report: Report) -> tuple[str, str]:
    """(title, message) for the weekly phone notification."""
    where = f": {report.site_name}" if report.site_name else ""
    title = f"Weekly network report{where}"
    n = len(report.outages)
    if not report.devices:
        return title, "No devices are being monitored."
    if n == 0:
        msg = f"No outages in the last 7 days. All {plural(len(report.devices), 'device')} stayed up."
    else:
        msg = f"Last 7 days: {plural(n, 'outage')}, {format_duration(report.downtime_s)} down in total."
        for r in report.worst:
            msg += f"\n• {r.name}: {plural(r.outages, 'outage')} ({format_duration(r.downtime_s)})"
        others = len(report.devices) - len(report.worst)
        if others and all(not r.outages for r in report.devices[len(report.worst):]):
            msg += "\n• Everything else: no problems."
    if report.previous_outages is not None and report.previous_outages != n:
        diff = n - report.previous_outages
        word = "outage" if abs(diff) == 1 else "outages"
        msg += f"\n\nThat's {abs(diff)} {'more' if diff > 0 else 'fewer'} {word} than the week before."
    if report.watching_since - report.start > 3600:
        msg += f"\n\nNetMon only started watching {format_duration(report.end - report.watching_since)} ago."
    elif report.coverage_pct is not None and report.coverage_pct < 95:
        msg += (f"\n\nNote: NetMon was only running {report.coverage_pct:.0f}% of the week, "
                "so some outages may have been missed.")
    msg += "\n\nFull report: open the NetMon dashboard and tap \"Weekly report\"."
    return title, msg


def next_weekly(now: datetime, weekday: int = 0, hour: int = 9) -> datetime:
    """The next local `weekday` (0 = Monday) at `hour`:00 strictly after `now`."""
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    target += timedelta(days=(weekday - now.weekday()) % 7)
    if target <= now:
        target += timedelta(days=7)
    return target
