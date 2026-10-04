"""Alerts: tell a person when a device goes down or comes back.

Message building (build_alert) is pure and has no I/O, so it's easy to test
and can be reused by the Phase 2 agent. AlertSender delivers alerts in the
background and keeps retrying, because the most important outage, the
internet, is exactly when an alert can't get out. Queued alerts are sent as
soon as the connection returns, marked as late.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import yaml

from .monitor import Event

log = logging.getLogger(__name__)

DEFAULT_ALERTS_PATH = Path(__file__).resolve().parent.parent / "alerts.yaml"
SEND_TIMEOUT_S = 10
RETRY_DELAYS_S = (15, 30, 60, 120, 300)  # then every 5 minutes
MAX_QUEUED = 200
LATE_AFTER_S = 120  # a delivered alert older than this says it's late

# What a restaurant manager can try, by device type. Plain words, no jargon.
TIPS = {
    "router": "Unplug the router's power for 10 seconds, plug it back in, and give it 3 minutes. "
              "Card payments and online orders won't work until it's back.",
    "switch": "Check the switch has power (lights on). If its lights are off or all blinking, "
              "unplug it for 10 seconds and plug it back in.",
    "ap": "Wi-Fi in that area is probably out. Check the access point has power, "
          "then unplug it for 10 seconds and plug it back in.",
    "printer": "Check the printer is on, has paper, and its lid is closed. If it is, "
               "switch it off and on. Until then, read orders from the POS screen.",
    "pos": "Check the tablet is on, charged, and connected to the Wi-Fi. "
           "If it still won't connect, restart it.",
    "internet": "The internet connection is down. Restart the router (unplug 10 seconds). "
                "If it's still down after 5 minutes, call your internet provider. "
                "Card readers with offline mode can keep taking payments.",
}


@dataclass(frozen=True)
class Alert:
    title: str
    message: str
    priority: int  # ntfy scale: 3 = normal, 4 = high
    tags: tuple[str, ...]
    created_at: float


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} sec"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def build_alert(
    name: str, device_type: str, ip: str, event: Event, at: float,
    started_at: float | None, site: str = "",
) -> Alert:
    """Build the alert for one status change. started_at: when the failures began."""
    where = f" at {site}" if site else ""
    if event is Event.WENT_DOWN:
        since = started_at if started_at is not None else at
        tip = TIPS.get(device_type, "")
        return Alert(
            title=f"DOWN: {name}{where}",
            message=f"{name} ({ip}) has not responded since {clock(since)}."
                    + (f"\n\nWhat to do: {tip}" if tip else ""),
            priority=4,
            tags=("red_circle",),
            created_at=at,
        )
    outage = f" after {format_duration(at - started_at)}" if started_at is not None else ""
    return Alert(
        title=f"Back UP: {name}{where}",
        message=f"{name} ({ip}) is responding again{outage}.",
        priority=3,
        tags=("green_circle",),
        created_at=at,
    )


def build_started_alert(device_count: int, at: float, site: str = "") -> Alert:
    """Sent when NetMon starts, so a reboot or power cut at the site doesn't go unnoticed."""
    where = f" at {site}" if site else ""
    return Alert(
        title=f"NetMon started{where}",
        message=f"NetMon started at {clock(at)} and is watching {device_count} devices. "
                "If you didn't restart it, the power or the NetMon computer may have gone off.",
        priority=2,
        tags=("information_source",),
        created_at=at,
    )


def mark_late(alert: Alert, now: float) -> Alert:
    """Note on an alert that it couldn't be sent when it happened."""
    if now - alert.created_at < LATE_AFTER_S:
        return alert
    return replace(alert, message=alert.message + (
        f"\n\n(Sent late: this happened at {clock(alert.created_at)}, "
        "but NetMon couldn't reach the internet until now.)"
    ))


# ---------------------------------------------------------------- config

class AlertConfigError(ValueError):
    """Raised when alerts.yaml is invalid."""


@dataclass(frozen=True)
class AlertConfig:
    site_name: str = ""
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    webhook_url: str = ""  # Slack or Discord incoming webhook
    notify_on_start: bool = True
    weekly_summary: bool = True  # Monday 9:00 summary of the past week

    @property
    def enabled(self) -> bool:
        return bool(self.ntfy_topic or self.webhook_url)


def load_alert_config(path: str | Path = DEFAULT_ALERTS_PATH) -> AlertConfig:
    """Read alerts.yaml. A missing file just means alerts are off."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return AlertConfig()
    except yaml.YAMLError as e:
        raise AlertConfigError(f"{path.name} is not valid YAML: {e}") from None
    if not isinstance(data, dict):
        raise AlertConfigError(f"{path.name} must be a list of settings like 'ntfy_topic: ...'")

    def text(key: str, default: str = "") -> str:
        value = data.get(key)
        if value is None:
            return default
        if not isinstance(value, (str, int)):
            raise AlertConfigError(f"{path.name}: '{key}' must be text")
        return str(value).strip()

    def flag(key: str) -> bool:
        value = data.get(key, True)
        if not isinstance(value, bool):
            raise AlertConfigError(f"{path.name}: '{key}' must be true or false")
        return value

    cfg = AlertConfig(
        notify_on_start=flag("notify_on_start"),
        weekly_summary=flag("weekly_summary"),
        site_name=text("site_name"),
        ntfy_topic=text("ntfy_topic"),
        ntfy_server=text("ntfy_server", AlertConfig.ntfy_server).rstrip("/"),
        webhook_url=text("webhook_url"),
    )
    for key, url in (("ntfy_server", cfg.ntfy_server), ("webhook_url", cfg.webhook_url)):
        if url and not url.startswith(("https://", "http://")):
            raise AlertConfigError(f"{path.name}: '{key}' must start with https://")
    if cfg.ntfy_topic and (len(cfg.ntfy_topic) > 64 or not cfg.ntfy_topic.replace("-", "").replace("_", "").isalnum()):
        raise AlertConfigError(
            f"{path.name}: 'ntfy_topic' may only use letters, numbers, - and _ (max 64)"
        )
    return cfg


# ---------------------------------------------------------------- delivery

def _post_json(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "NetMon"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=SEND_TIMEOUT_S) as resp:
        resp.read()


def deliver(cfg: AlertConfig, alert: Alert) -> None:
    """Send one alert to every configured channel. Raises if any channel fails."""
    errors = []
    if cfg.ntfy_topic:
        try:
            _post_json(cfg.ntfy_server, {
                "topic": cfg.ntfy_topic, "title": alert.title, "message": alert.message,
                "priority": alert.priority, "tags": list(alert.tags),
            })
        except Exception as e:
            errors.append(f"ntfy: {e}")
    if cfg.webhook_url:
        text = f"*{alert.title}*\n{alert.message}"
        try:
            # Slack reads "text", Discord reads "content"; each ignores the other.
            _post_json(cfg.webhook_url, {"text": text, "content": text})
        except Exception as e:
            errors.append(f"webhook: {e}")
    if errors:
        raise OSError("; ".join(errors))


class AlertSender:
    """Queue alerts and deliver them in order, retrying until they get through."""

    def __init__(self, cfg: AlertConfig, send=None):
        self.cfg = cfg
        self._send = send or deliver
        self.queue: deque[Alert] = deque(maxlen=MAX_QUEUED)
        self._wake = asyncio.Event()

    def send(self, alert: Alert) -> None:
        """Queue an alert for delivery."""
        if len(self.queue) == self.queue.maxlen:
            log.warning("alert queue full; dropping the oldest alert")
        self.queue.append(alert)
        self._wake.set()

    def on_event(self, name: str, device_type: str, ip: str, event: Event,
                 at: float, started_at: float | None) -> None:
        """Monitor callback: called once per status change."""
        self.send(build_alert(name, device_type, ip, event, at, started_at, self.cfg.site_name))

    async def run(self) -> None:
        attempt = 0
        while True:
            if not self.queue:
                self._wake.clear()
                await self._wake.wait()
                continue
            queued = self.queue[0]
            alert = mark_late(queued, time.time())
            try:
                await asyncio.to_thread(self._send, self.cfg, alert)
            except Exception as e:
                delay = RETRY_DELAYS_S[min(attempt, len(RETRY_DELAYS_S) - 1)]
                attempt += 1
                log.warning("couldn't send alert %r (%s); retrying in %ss", alert.title, e, delay)
                await asyncio.sleep(delay)
                continue
            log.info("alert sent: %s", alert.title)
            if self.queue and self.queue[0] is queued:  # may have been dropped if the queue overflowed
                self.queue.popleft()
            attempt = 0
