from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from . import socks5
from .config import ProbeSettings, ProxySettings
from .util import ip_in_networks, parse_host_port, parse_networks

log = logging.getLogger("wgpool.pool")


@dataclass
class Upstream:
    name: str
    socks_port: int
    enabled: bool = True
    state: str = "stopped"
    endpoint: str = ""
    proc: Optional[asyncio.subprocess.Process] = None
    restart_count: int = 0
    fail_count: int = 0
    dead_until: float = 0.0
    last_error: str = ""
    started_at: float = 0.0
    healthy: Optional[bool] = None
    last_probe: float = 0.0
    last_probe_ms: Optional[float] = None
    egress_ip: str = ""
    log_path: str = ""
    _backoff: float = field(default=1.0, repr=False)

    def available(self, now: float, probe_enabled: bool) -> bool:
        if not self.enabled or self.state != "running":
            return False
        if now < self.dead_until:
            return False
        if probe_enabled and self.healthy is False:
            return False
        return True

    def snapshot(self) -> dict:
        now = time.time()
        return {
            "name": self.name,
            "socks_port": self.socks_port,
            "enabled": self.enabled,
            "state": self.state,
            "endpoint": self.endpoint,
            "restarts": self.restart_count,
            "fails": self.fail_count,
            "healthy": self.healthy,
            "uptime": int(now - self.started_at) if self.state == "running" and self.started_at else 0,
            "last_error": self.last_error,
            "last_probe_ms": self.last_probe_ms,
            "egress_ip": self.egress_ip,
            "dead_for": max(0, int(self.dead_until - now)),
        }


class Pool:
    def __init__(self, proxy: ProxySettings, probe: ProbeSettings):
        self.proxy = proxy
        self.probe = probe
        self.upstreams: Dict[str, Upstream] = {}
        self._rr = 0
        self._allowed = parse_networks(proxy.allowed_ips.replace(",", " ").split())
        self._probe_task: Optional[asyncio.Task] = None
        self._probe_stop: Optional[asyncio.Event] = None

    def configure(self, proxy: ProxySettings, probe: ProbeSettings) -> None:
        self.proxy = proxy
        self.probe = probe
        self._allowed = parse_networks(proxy.allowed_ips.replace(",", " ").split())

    def client_allowed(self, ip: str) -> bool:
        return ip_in_networks(ip, self._allowed)

    def add(self, upstream: Upstream) -> Upstream:
        self.upstreams[upstream.name] = upstream
        return upstream

    def remove(self, name: str) -> Optional[Upstream]:
        return self.upstreams.pop(name, None)

    def get(self, name: str) -> Optional[Upstream]:
        return self.upstreams.get(name)

    def names(self) -> List[str]:
        return sorted(self.upstreams)

    def __len__(self) -> int:
        return len(self.upstreams)

    def pick(self, exclude: Optional[Iterable[str]] = None, name: Optional[str] = None) -> Optional[Upstream]:
        if name:
            return self.upstreams.get(name)
        excluded = set(exclude or ())
        now = time.time()
        candidates = [
            u
            for u in self.upstreams.values()
            if u.name not in excluded and u.available(now, self.probe.enabled)
        ]
        if not candidates:
            candidates = [
                u
                for u in self.upstreams.values()
                if u.name not in excluded and u.enabled and u.state == "running"
            ]
        if not candidates:
            candidates = [u for u in self.upstreams.values() if u.name not in excluded and u.state == "running"]
        if not candidates:
            return None
        if self.proxy.strategy == "rand":
            return random.choice(candidates)
        index = self._rr % len(candidates)
        self._rr += 1
        return candidates[index]

    def mark_fail(self, upstream: Upstream, error: str = "") -> None:
        upstream.fail_count += 1
        if error:
            upstream.last_error = error
        if upstream.fail_count >= max(1, self.proxy.max_fails):
            upstream.dead_until = time.time() + max(1, self.proxy.fail_timeout)
            upstream.healthy = False
            log.warning(
                "upstream %s ditandai mati selama %ss setelah %d kegagalan: %s",
                upstream.name,
                self.proxy.fail_timeout,
                upstream.fail_count,
                error,
            )
            upstream.fail_count = 0

    def mark_ok(self, upstream: Upstream) -> None:
        upstream.fail_count = 0
        upstream.dead_until = 0.0
        if upstream.healthy is False:
            upstream.healthy = True

    async def probe_once(self, upstream: Upstream) -> Optional[float]:
        started = time.monotonic()
        writer = None
        try:
            if "://" in self.probe.target:
                status, body = await socks5.http_get_via_socks5(
                    "127.0.0.1", upstream.socks_port, self.probe.target, timeout=self.probe.timeout
                )
                if status < 200 or status >= 300:
                    raise socks5.Socks5Error("probe HTTP status %d" % status)
                if not body:
                    raise socks5.Socks5Error("probe HTTP response kosong")
                try:
                    upstream.egress_ip = str(ipaddress.ip_address(body.strip()))
                except ValueError as exc:
                    raise socks5.Socks5Error("probe response bukan alamat IP") from exc
            else:
                host, port = parse_host_port(self.probe.target, 0)
                _, writer = await socks5.socks5_connect(
                    "127.0.0.1", upstream.socks_port, host, port, timeout=self.probe.timeout
                )
                upstream.egress_ip = ""
            elapsed = (time.monotonic() - started) * 1000.0
            upstream.healthy = True
            upstream.last_probe = time.time()
            upstream.last_probe_ms = round(elapsed, 1)
            self.mark_ok(upstream)
            return elapsed
        except Exception as exc:  # noqa: BLE001
            upstream.healthy = False
            upstream.last_probe = time.time()
            upstream.last_probe_ms = None
            upstream.egress_ip = ""
            upstream.last_error = str(exc)
            log.debug("probe %s gagal: %s", upstream.name, exc)
            return None
        finally:
            if writer is not None:
                await socks5.close_writer(writer)

    async def probe_all(self) -> None:
        targets = [u for u in self.upstreams.values() if u.state == "running" and u.enabled]
        if not targets:
            return
        await asyncio.gather(*(self.probe_once(u) for u in targets), return_exceptions=True)

    def start_probe_loop(self) -> None:
        self.stop_probe_loop()
        if not self.probe.enabled:
            return
        self._probe_stop = asyncio.Event()
        self._probe_task = asyncio.create_task(self._probe_loop(self._probe_stop))

    def stop_probe_loop(self) -> None:
        if self._probe_stop is not None:
            self._probe_stop.set()
        if self._probe_task is not None:
            self._probe_task.cancel()
        self._probe_stop = None
        self._probe_task = None

    async def _probe_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if not any(u.state == "running" and u.enabled for u in self.upstreams.values()):
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2)
                except asyncio.TimeoutError:
                    continue
            try:
                await self.probe_all()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("probe loop error")
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(1, self.probe.interval))
            except asyncio.TimeoutError:
                continue

    def snapshots(self) -> List[dict]:
        return [self.upstreams[name].snapshot() for name in self.names()]

    def running_count(self) -> int:
        return sum(1 for u in self.upstreams.values() if u.state == "running" and u.enabled)

    def pick_for_display(self) -> Optional[Upstream]:
        return self.pick()

    @staticmethod
    def sequence(items: Sequence[Upstream]) -> List[str]:
        return [item.name for item in items]
