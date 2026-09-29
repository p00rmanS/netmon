"""Load, validate and save the device list in devices.yaml."""

from __future__ import annotations

import contextlib
import ipaddress
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

DEVICE_TYPES = {"router", "switch", "ap", "printer", "pos", "internet"}
CHECK_METHODS = {"ping", "tcp"}

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "devices.yaml"


class ConfigError(ValueError):
    """Raised when devices.yaml is missing or invalid."""


@dataclass(frozen=True)
class DeviceConfig:
    name: str
    type: str
    ip: str
    check: str
    port: int | None = None


def parse_device(raw: object, index: int) -> DeviceConfig:
    where = f"device #{index + 1}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a mapping with name/type/ip/check")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{where}: 'name' is required")
    name = " ".join(name.split())  # trim and collapse whitespace/newlines
    if len(name) > 60:
        raise ConfigError(f"{where}: 'name' must be 60 characters or fewer")
    where = f"device '{name}'"

    dtype = raw.get("type")
    if dtype not in DEVICE_TYPES:
        raise ConfigError(f"{where}: 'type' must be one of {sorted(DEVICE_TYPES)}, got {dtype!r}")

    ip = raw.get("ip")
    try:
        ip = str(ipaddress.ip_address(str(ip).strip()))
    except ValueError:
        raise ConfigError(f"{where}: 'ip' is not a valid IP address: {ip!r}") from None

    check = raw.get("check")
    if check not in CHECK_METHODS:
        raise ConfigError(f"{where}: 'check' must be one of {sorted(CHECK_METHODS)}, got {check!r}")

    port = raw.get("port")
    if check == "tcp":
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError(f"{where}: tcp checks need a 'port' between 1 and 65535")
    else:
        port = None  # ping ignores port

    return DeviceConfig(name=name, type=dtype, ip=ip, check=check, port=port)


def load_devices(path: str | Path = DEFAULT_CONFIG_PATH) -> list[DeviceConfig]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path.name} is not valid YAML: {e}") from None

    raw_devices = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(raw_devices, list):
        raise ConfigError(f"{path.name} must contain a 'devices:' list")

    devices = [parse_device(raw, i) for i, raw in enumerate(raw_devices)]
    check_unique_names(devices)
    return devices


def check_unique_names(devices: list[DeviceConfig]) -> None:
    seen: set[str] = set()
    for d in devices:
        if d.name in seen:
            raise ConfigError(f"duplicate device name {d.name!r}; names must be unique")
        seen.add(d.name)


HEADER = """\
# Devices NetMon watches. Edit from the dashboard ("Manage devices") or by
# hand here. After a hand edit, restart NetMon.
#
#   type:  router | switch | ap | printer | pos | internet
#   check: ping | tcp   (tcp needs a port; 9100 for most receipt printers)
#
# Each device's name must be unique: it's how NetMon matches this file
# to the history it has already stored.

"""


class _IndentDumper(yaml.SafeDumper):
    """Indent list items under 'devices:' the way people write them by hand."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def save_devices(devices: list[DeviceConfig], path: str | Path = DEFAULT_CONFIG_PATH) -> None:
    """Write the device list to devices.yaml atomically.

    Writes to a temp file first and then swaps it in, so a crash mid-write
    can never leave a half-written config behind.
    """
    check_unique_names(devices)
    items = []
    for d in devices:
        item = {"name": d.name, "type": d.type, "ip": d.ip, "check": d.check}
        if d.check == "tcp":
            item["port"] = d.port
        items.append(item)
    body = yaml.dump(
        {"devices": items}, Dumper=_IndentDumper, sort_keys=False, allow_unicode=True,
        default_flow_style=False,
    )
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".devices-", suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(HEADER + body)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
