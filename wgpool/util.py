from __future__ import annotations

import ipaddress
import os
import re
import tempfile
import time
from typing import Iterable, List, Optional, Tuple

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.conf$")


def now() -> float:
    return time.time()


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def atomic_write(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    ensure_dir(directory)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def safe_config_name(name: str) -> bool:
    return bool(_NAME_RE.match(name)) and ".." not in name


def sanitize_config_name(name: str) -> Optional[str]:
    name = (name or "").strip()
    if not name:
        return None
    if "/" in name or "\\" in name or ".." in name:
        return None
    if not name.lower().endswith(".conf"):
        name += ".conf"
    if not safe_config_name(name):
        return None
    return name


def parse_host_port(value: str, default_port: int = 0) -> Tuple[str, int]:
    """Parse ``host:port`` including bracketed IPv6, or ``host`` with a default port."""
    value = (value or "").strip()
    if not value:
        raise ValueError("alamat kosong")
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            raise ValueError("IPv6 tidak valid: " + value)
        host = value[1:end]
        rest = value[end + 1 :]
        if rest.startswith(":"):
            return host, int(rest[1:])
        return host, default_port
    if value.count(":") > 1:
        # bare IPv6 without port
        return value, default_port
    if ":" in value:
        host, _, port = value.rpartition(":")
        return host, int(port)
    return value, default_port


def format_host_port(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        return "[%s]:%d" % (host, port)
    return "%s:%d" % (host, port)


def read_last_lines(path: str, limit: int = 200) -> List[str]:
    limit = max(1, min(int(limit), 5000))
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    return [line.rstrip("\n") for line in lines[-limit:]]


def is_loopback(host: str) -> bool:
    host = (host or "").strip()
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def parse_networks(values: Iterable[str]) -> List[ipaddress._BaseNetwork]:
    nets = []
    for raw in values:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            raise ValueError("CIDR tidak valid: " + raw)
    return nets


def ip_in_networks(ip: str, nets: List[ipaddress._BaseNetwork]) -> bool:
    if not nets:
        return True
    try:
        addr = ipaddress.ip_address((ip or "").split("%")[0])
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in nets)


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return "%dd %dh %dm" % (days, hours, minutes)
    if hours:
        return "%dh %dm" % (hours, minutes)
    if minutes:
        return "%dm %ds" % (minutes, secs)
    return "%ds" % secs
