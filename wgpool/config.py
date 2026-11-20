from __future__ import annotations

import base64
import configparser
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, field
from typing import List, Optional

from .util import atomic_write, ensure_dir, is_loopback, parse_networks

DEFAULT_CONFIG = """\
; wgpool configuration
; Set a web password first: python3 -m wgpool passwd

[proxy]
bind = 127.0.0.1
port = 8080
strategy = round
max_fails = 3
fail_timeout = 60
connect_timeout = 25
allowed_ips =

[probe]
enabled = true
interval = 30
target = https://api64.ipify.org/
timeout = 15

[rotation]
interval_minutes = 0

[exit]
ipv6_only = false

[wireproxy]
binary = wireproxy
config_dir = configs
base_socks_port = 10800

[web]
bind = 127.0.0.1
port = 9099
username = admin
password_hash =
session_ttl = 43200
tls_cert =
tls_key =

[tunnels]
disabled =
"""

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password tidak boleh kosong")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
        maxmem=SCRYPT_MAXMEM,
    )
    return "scrypt$%d$%d$%d$%s$%s" % (
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(stored: str, password: str) -> bool:
    if not stored or not password:
        return False
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


@dataclass
class ProxySettings:
    bind: str = "127.0.0.1"
    port: int = 8080
    strategy: str = "round"
    max_fails: int = 3
    fail_timeout: int = 60
    connect_timeout: float = 25.0
    allowed_ips: str = ""


@dataclass
class ProbeSettings:
    enabled: bool = True
    interval: int = 30
    target: str = "https://api64.ipify.org/"
    timeout: float = 15.0


@dataclass
class RotationSettings:
    # Zero disables scheduled tunnel restarts.
    interval_minutes: float = 0.0


@dataclass
class ExitSettings:
    ipv6_only: bool = False


@dataclass
class WireproxySettings:
    binary: str = "wireproxy"
    config_dir: str = "configs"
    base_socks_port: int = 10800


@dataclass
class WebSettings:
    bind: str = "127.0.0.1"
    port: int = 9099
    username: str = "admin"
    password_hash: str = ""
    session_ttl: int = 43200
    tls_cert: str = ""
    tls_key: str = ""


@dataclass
class AppConfig:
    path: str
    proxy: ProxySettings = field(default_factory=ProxySettings)
    probe: ProbeSettings = field(default_factory=ProbeSettings)
    rotation: RotationSettings = field(default_factory=RotationSettings)
    exit: ExitSettings = field(default_factory=ExitSettings)
    wireproxy: WireproxySettings = field(default_factory=WireproxySettings)
    web: WebSettings = field(default_factory=WebSettings)
    disabled: List[str] = field(default_factory=list)

    @property
    def base_dir(self) -> str:
        return os.path.dirname(os.path.abspath(self.path))

    @property
    def run_dir(self) -> str:
        return os.path.join(self.base_dir, "run")

    @property
    def configs_dir(self) -> str:
        path = self.wireproxy.config_dir
        if not os.path.isabs(path):
            path = os.path.join(self.base_dir, path)
        return os.path.abspath(path)

    @property
    def logs_dir(self) -> str:
        return os.path.join(self.run_dir, "logs")

    @property
    def wg_dir(self) -> str:
        return os.path.join(self.run_dir, "wg")

    @property
    def pid_path(self) -> str:
        return os.path.join(self.run_dir, "wgpool.pid")

    @property
    def ports_path(self) -> str:
        return os.path.join(self.run_dir, "ports.json")

    def prepare(self) -> None:
        ensure_dir(self.base_dir)
        ensure_dir(self.configs_dir)
        ensure_dir(self.run_dir)
        ensure_dir(self.logs_dir)
        ensure_dir(self.wg_dir)

    def allowed_networks(self):
        return parse_networks(self.proxy.allowed_ips.replace(",", " ").split())

    def validate(self) -> None:
        if self.proxy.strategy not in ("round", "rand"):
            raise ValueError("strategy harus 'round' atau 'rand'")
        if not (1 <= self.proxy.port <= 65535):
            raise ValueError("port proxy tidak valid")
        if not (1 <= self.web.port <= 65535):
            raise ValueError("port web tidak valid")
        if self.proxy.max_fails < 1:
            raise ValueError("max_fails minimal 1")
        if self.proxy.fail_timeout < 1:
            raise ValueError("fail_timeout minimal 1")
        if self.proxy.connect_timeout <= 0:
            raise ValueError("connect_timeout harus > 0")
        if self.probe.interval < 1:
            raise ValueError("interval probe minimal 1")
        if self.rotation.interval_minutes < 0 or self.rotation.interval_minutes > 10080:
            raise ValueError("rotation interval harus 0–10080 menit")
        if not (1 <= self.wireproxy.base_socks_port <= 65000):
            raise ValueError("base_socks_port tidak valid")
        self.allowed_networks()
        if self.probe.enabled:
            from urllib.parse import urlsplit

            if "://" in self.probe.target:
                parsed = urlsplit(self.probe.target)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    raise ValueError("probe target harus URL http(s) yang valid")
                if parsed.port is not None and not (1 <= parsed.port <= 65535):
                    raise ValueError("port probe tidak valid")
            else:
                from .util import parse_host_port

                parse_host_port(self.probe.target, 0)
        if not is_loopback(self.web.bind) and not self.web.password_hash:
            raise ValueError(
                "web bind non-localhost membutuhkan password. Jalankan: python3 -m wgpool passwd"
            )
        if bool(self.web.tls_cert) != bool(self.web.tls_key):
            raise ValueError("tls_cert dan tls_key harus diisi keduanya")


def _get(parser: configparser.ConfigParser, section: str, key: str, default: str) -> str:
    if parser.has_option(section, key):
        value = parser.get(section, key)
        if value is None:
            return default
        return value.strip()
    return default


def _get_bool(parser, section, key, default: bool) -> bool:
    raw = _get(parser, section, key, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


def _get_int(parser, section, key, default: int) -> int:
    raw = _get(parser, section, key, str(default))
    if raw == "":
        return default
    return int(float(raw))


def _get_float(parser, section, key, default: float) -> float:
    raw = _get(parser, section, key, str(default))
    if raw == "":
        return default
    return float(raw)


def load_config(path: str, validate: bool = True) -> AppConfig:
    path = os.path.abspath(path)
    parser = configparser.ConfigParser(interpolation=None)
    if not os.path.exists(path):
        atomic_write(path, DEFAULT_CONFIG)
    with open(path, "r", encoding="utf-8") as fh:
        parser.read_file(fh)

    cfg = AppConfig(
        path=path,
        proxy=ProxySettings(
            bind=_get(parser, "proxy", "bind", "127.0.0.1"),
            port=_get_int(parser, "proxy", "port", 8080),
            strategy=_get(parser, "proxy", "strategy", "round").lower(),
            max_fails=_get_int(parser, "proxy", "max_fails", 3),
            fail_timeout=_get_int(parser, "proxy", "fail_timeout", 60),
            connect_timeout=_get_float(parser, "proxy", "connect_timeout", 25.0),
            allowed_ips=_get(parser, "proxy", "allowed_ips", ""),
        ),
        probe=ProbeSettings(
            enabled=_get_bool(parser, "probe", "enabled", True),
            interval=_get_int(parser, "probe", "interval", 30),
            target=_get(parser, "probe", "target", "https://api64.ipify.org/"),
            timeout=_get_float(parser, "probe", "timeout", 15.0),
        ),
        rotation=RotationSettings(
            interval_minutes=_get_float(parser, "rotation", "interval_minutes", 0.0),
        ),
        exit=ExitSettings(
            ipv6_only=_get_bool(parser, "exit", "ipv6_only", False),
        ),
        wireproxy=WireproxySettings(
            binary=_get(parser, "wireproxy", "binary", "wireproxy"),
            config_dir=_get(parser, "wireproxy", "config_dir", "configs"),
            base_socks_port=_get_int(parser, "wireproxy", "base_socks_port", 10800),
        ),
        web=WebSettings(
            bind=_get(parser, "web", "bind", "127.0.0.1"),
            port=_get_int(parser, "web", "port", 9099),
            username=_get(parser, "web", "username", "admin"),
            password_hash=_get(parser, "web", "password_hash", ""),
            session_ttl=_get_int(parser, "web", "session_ttl", 43200),
            tls_cert=_get(parser, "web", "tls_cert", ""),
            tls_key=_get(parser, "web", "tls_key", ""),
        ),
        disabled=[
            item.strip()
            for item in _get(parser, "tunnels", "disabled", "").replace(",", " ").split()
            if item.strip()
        ],
    )
    if validate:
        cfg.validate()
    return cfg


def save_config(cfg: AppConfig, password_hash: Optional[str] = None) -> None:
    hash_value = cfg.web.password_hash if password_hash is None else password_hash
    lines = [
        "; wgpool configuration (dikelola oleh wgpool)",
        "",
        "[proxy]",
        "bind = %s" % cfg.proxy.bind,
        "port = %d" % cfg.proxy.port,
        "strategy = %s" % cfg.proxy.strategy,
        "max_fails = %d" % cfg.proxy.max_fails,
        "fail_timeout = %d" % cfg.proxy.fail_timeout,
        "connect_timeout = %g" % cfg.proxy.connect_timeout,
        "allowed_ips = %s" % cfg.proxy.allowed_ips,
        "",
        "[probe]",
        "enabled = %s" % ("true" if cfg.probe.enabled else "false"),
        "interval = %d" % cfg.probe.interval,
        "target = %s" % cfg.probe.target,
        "timeout = %g" % cfg.probe.timeout,
        "",
        "[rotation]",
        "interval_minutes = %g" % cfg.rotation.interval_minutes,
        "",
        "[exit]",
        "ipv6_only = %s" % ("true" if cfg.exit.ipv6_only else "false"),
        "",
        "[wireproxy]",
        "binary = %s" % cfg.wireproxy.binary,
        "config_dir = %s" % cfg.wireproxy.config_dir,
        "base_socks_port = %d" % cfg.wireproxy.base_socks_port,
        "",
        "[web]",
        "bind = %s" % cfg.web.bind,
        "port = %d" % cfg.web.port,
        "username = %s" % cfg.web.username,
        "password_hash = %s" % hash_value,
        "session_ttl = %d" % cfg.web.session_ttl,
        "tls_cert = %s" % cfg.web.tls_cert,
        "tls_key = %s" % cfg.web.tls_key,
        "",
        "[tunnels]",
        "disabled = %s" % ", ".join(sorted(cfg.disabled)),
        "",
    ]
    atomic_write(cfg.path, "\n".join(lines))
