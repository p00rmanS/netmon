"""Load and validate the device list from devices.yaml."""

from __future__ import annotations

import ipaddress
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


def _parse_device(raw: object, index: int) -> DeviceConfig:
    where = f"device #{index + 1}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a mapping with name/type/ip/check")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{where}: 'name' is required")
    name = name.strip()
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
    if not isinstance(raw_devices, list) or not raw_devices:
        raise ConfigError(f"{path.name} must contain a non-empty 'devices:' list")

    devices = [_parse_device(raw, i) for i, raw in enumerate(raw_devices)]

    seen: set[str] = set()
    for d in devices:
        if d.name in seen:
            raise ConfigError(f"duplicate device name {d.name!r}; names must be unique")
        seen.add(d.name)

    return devices
