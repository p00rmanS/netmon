"""Device check functions: ping and TCP connect.

Pure network code with no database or web imports, so the Phase 2 on-site
agent can reuse it as-is. Every check returns a CheckResult and never raises
for an unreachable device; failures are data, not exceptions.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

TIMEOUT_S = 2.0


@dataclass(frozen=True)
class CheckResult:
    is_up: bool
    latency_ms: float | None  # None when the check failed
    error: str | None = None  # short reason, for logs/debugging


def _fail(reason: str) -> CheckResult:
    return CheckResult(is_up=False, latency_ms=None, error=reason)


# ---------------------------------------------------------------- TCP

async def check_tcp(ip: str, port: int, timeout: float = TIMEOUT_S) -> CheckResult:
    start = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except asyncio.TimeoutError:
        return _fail("timeout")
    except OSError as e:
        return _fail(e.strerror or type(e).__name__)
    latency = (time.perf_counter() - start) * 1000
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 1)
    except (OSError, asyncio.TimeoutError):
        pass
    return CheckResult(is_up=True, latency_ms=round(latency, 2))


# ---------------------------------------------------------------- ping

# None = not decided yet; True = icmplib works here; False = use system ping.
_icmplib_ok: bool | None = None


async def _ping_icmplib(ip: str, timeout: float) -> CheckResult:
    from icmplib import async_ping

    host = await async_ping(ip, count=1, timeout=timeout, privileged=False)
    if host.is_alive:
        return CheckResult(is_up=True, latency_ms=round(host.avg_rtt, 2))
    return _fail("no reply")


def _ping_command(ip: str, timeout: float) -> list[str]:
    if sys.platform == "win32":
        return ["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip]
    if sys.platform == "darwin":
        return ["ping", "-c", "1", "-W", str(int(timeout * 1000)), ip]  # macOS -W is ms
    return ["ping", "-c", "1", "-W", str(max(1, round(timeout))), ip]  # Linux -W is seconds


# Matches "time=12.3 ms", "time<1ms", "Zeit=4ms" etc. across OS locales.
_RTT_RE = re.compile(r"[=<]\s*([\d.,]+)\s*ms", re.IGNORECASE)


def parse_ping_output(output: str) -> float | None:
    """Return RTT in ms from system ping output, or None if there was no real reply.

    Requires 'ttl=' so that Windows' "Destination host unreachable" (which
    exits 0) is not mistaken for success.
    """
    for line in output.splitlines():
        if "ttl=" in line.lower():
            m = _RTT_RE.search(line)
            if m:
                return float(m.group(1).replace(",", "."))
    return None


async def _ping_subprocess(ip: str, timeout: float) -> CheckResult:
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = await asyncio.create_subprocess_exec(
        *_ping_command(ip, timeout),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **kwargs,
    )
    try:
        # Small grace period for process startup on top of ping's own timeout.
        out, _ = await asyncio.wait_for(proc.communicate(), timeout + 1)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return _fail("timeout")
    rtt = parse_ping_output(out.decode(errors="replace"))
    if rtt is None:
        return _fail("no reply")
    return CheckResult(is_up=True, latency_ms=rtt)


async def check_ping(ip: str, timeout: float = TIMEOUT_S) -> CheckResult:
    global _icmplib_ok
    if _icmplib_ok is not False:
        try:
            result = await _ping_icmplib(ip, timeout)
            _icmplib_ok = True
            return result
        except Exception as e:  # permission/socket errors: this OS won't let icmplib run
            if _icmplib_ok is None:
                log.warning("icmplib unavailable (%s); falling back to system ping", e)
                _icmplib_ok = False
            else:
                return _fail(f"icmplib error: {e}")
    return await _ping_subprocess(ip, timeout)


# ---------------------------------------------------------------- dispatch

async def run_check(check: str, ip: str, port: int | None = None) -> CheckResult:
    """Run one check by method name ('ping' or 'tcp')."""
    if check == "ping":
        return await check_ping(ip)
    if check == "tcp":
        if port is None:
            return _fail("tcp check without port")
        return await check_tcp(ip, port)
    return _fail(f"unknown check method {check!r}")
