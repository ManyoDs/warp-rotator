from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import ssl
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional

from . import __version__
from .api import WebApp
from .config import AppConfig, hash_password, save_config
from .http_proxy import HttpProxyServer
from .httpserver import HttpServer
from .pool import Pool
from .socks5 import http_get_via_socks5
from .supervisor import Supervisor, normalize_wg_text
from .util import read_last_lines, safe_config_name

log = logging.getLogger("wgpool.manager")

MAX_CONFIG_BYTES = 256 * 1024


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    return int(float(value))


def _as_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


class Manager:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.pool = Pool(cfg.proxy, cfg.probe)
        self.supervisor = Supervisor(cfg, self.pool)
        self.webapp = WebApp(self)
        self.proxy_server: Optional[HttpProxyServer] = None
        self.web_server: Optional[HttpServer] = None
        self.stop_event = asyncio.Event()
        self.started_at = time.time()
        self._web_task: Optional[asyncio.Task] = None
        self._rotation_task: Optional[asyncio.Task] = None
        self._rotation_wakeup = asyncio.Event()

    # ---------------------------------------------------------------- start
    async def run(self) -> None:
        self.cfg.prepare()
        await self.supervisor.start_all()
        self.pool.start_probe_loop()
        self._start_rotation_loop()
        await self._start_proxy()
        await self._start_web()
        log.info("web UI  : http%s://%s", "s" if self.cfg.web.tls_cert else "", self._web_url())
        log.info("proxy   : http://%s", self._proxy_url())
        log.info("tunnel  : %d config", len(self.pool))
        await self.stop_event.wait()
        await self.shutdown()

    async def shutdown(self) -> None:
        log.info("mematikan wgpool...")
        self.stop_event.set()
        self._rotation_wakeup.set()
        if self._web_task is not None:
            self._web_task.cancel()
            self._web_task = None
        if self._rotation_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._rotation_task), timeout=20)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._rotation_task.cancel()
            self._rotation_task = None
        self.pool.stop_probe_loop()
        log.debug("menutup web UI")
        await self._stop_web()
        log.debug("menutup proxy")
        await self._stop_proxy()
        log.debug("menghentikan tunnel")
        await self.supervisor.stop_all()
        log.debug("shutdown selesai")

    def request_stop(self) -> None:
        self.stop_event.set()
        self._rotation_wakeup.set()

    def _proxy_url(self) -> str:
        port = self.proxy_server.port if self.proxy_server else self.cfg.proxy.port
        return "%s:%d" % (self.cfg.proxy.bind, port)

    def _web_url(self) -> str:
        port = self.web_server.port if self.web_server else self.cfg.web.port
        return "%s:%d" % (self.cfg.web.bind, port)

    # ---------------------------------------------------------------- proxy
    async def _start_proxy(self) -> None:
        server = HttpProxyServer(self.pool)
        await server.start(self.cfg.proxy.bind, self.cfg.proxy.port)
        self.proxy_server = server

    async def _stop_proxy(self) -> None:
        if self.proxy_server is not None:
            await self.proxy_server.stop()
            self.proxy_server = None

    async def _restart_proxy(self) -> None:
        await self._stop_proxy()
        await self._start_proxy()

    # ------------------------------------------------------------------ web
    def _ssl_context(self) -> Optional[ssl.SSLContext]:
        cert = self.cfg.web.tls_cert
        key = self.cfg.web.tls_key
        if not cert or not key:
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        return ctx

    async def _start_web(self) -> None:
        server = HttpServer(self.webapp.handle)
        await server.start(self.cfg.web.bind, self.cfg.web.port, self._ssl_context())
        self.web_server = server

    async def _stop_web(self) -> None:
        if self.web_server is not None:
            await self.web_server.stop()
            self.web_server = None

    async def _restart_web(self, prev_web) -> None:
        await self._stop_web()
        try:
            await self._start_web()
            log.info("web UI dibind ulang ke %s", self._web_url())
        except Exception:
            log.exception("gagal rebind web UI, memulihkan listener lama")
            self.cfg.web.bind = prev_web.bind
            self.cfg.web.port = prev_web.port
            self.cfg.web.tls_cert = prev_web.tls_cert
            self.cfg.web.tls_key = prev_web.tls_key
            save_config(self.cfg)
            await self._start_web()

    def _schedule_web_restart(self, prev_web) -> None:
        if self._web_task is not None:
            self._web_task.cancel()

        async def _go() -> None:
            await asyncio.sleep(0.6)
            try:
                await self._restart_web(prev_web)
            except Exception:  # noqa: BLE001
                log.exception("web UI tidak dapat dijalankan ulang")

        self._web_task = asyncio.create_task(_go())

    # --------------------------------------------------------------- status
    def status(self) -> dict:
        return {
            "version": __version__,
            "uptime": int(time.time() - self.started_at),
            "proxy": {
                "bind": self.cfg.proxy.bind,
                "port": self.proxy_server.port if self.proxy_server else self.cfg.proxy.port,
                "strategy": self.cfg.proxy.strategy,
                "running": len([u for u in self.pool.upstreams.values() if u.state == "running"]),
            },
            "web": {
                "bind": self.cfg.web.bind,
                "port": self.web_server.port if self.web_server else self.cfg.web.port,
                "tls": bool(self.cfg.web.tls_cert),
            },
            "settings": self.settings(),
            "tunnels": self.pool.snapshots(),
        }

    def settings(self) -> dict:
        cfg = self.cfg
        return {
            "proxy": {
                "bind": cfg.proxy.bind,
                "port": cfg.proxy.port,
                "strategy": cfg.proxy.strategy,
                "max_fails": cfg.proxy.max_fails,
                "fail_timeout": cfg.proxy.fail_timeout,
                "connect_timeout": cfg.proxy.connect_timeout,
                "allowed_ips": cfg.proxy.allowed_ips,
                "auth_enabled": cfg.proxy.auth_enabled,
                "auth_username": cfg.proxy.auth_username,
                "auth_password_set": bool(cfg.proxy.auth_password_hash),
            },
            "probe": {
                "enabled": cfg.probe.enabled,
                "interval": cfg.probe.interval,
                "target": cfg.probe.target,
                "timeout": cfg.probe.timeout,
            },
            "rotation": {
                "interval_minutes": cfg.rotation.interval_minutes,
                "enabled": cfg.rotation.interval_minutes > 0,
            },
            "exit": {"ipv6_only": cfg.exit.ipv6_only},
            "wireproxy": {
                "binary": cfg.wireproxy.binary,
                "base_socks_port": cfg.wireproxy.base_socks_port,
            },
            "web": {
                "bind": cfg.web.bind,
                "port": cfg.web.port,
                "username": cfg.web.username,
                "session_ttl": cfg.web.session_ttl,
                "tls": bool(cfg.web.tls_cert),
                "tls_cert": cfg.web.tls_cert,
                "tls_key": cfg.web.tls_key,
            },
            "password_set": bool(cfg.web.password_hash),
        }

    # --------------------------------------------------------------- configs
    def list_configs(self) -> List[dict]:
        result = []
        for name in self.supervisor.config_files():
            upstream = self.pool.get(name)
            try:
                size = os.path.getsize(os.path.join(self.cfg.configs_dir, name))
            except OSError:
                size = 0
            result.append(
                {
                    "name": name,
                    "size": size,
                    "endpoint": upstream.endpoint if upstream else "",
                    "socks_port": upstream.socks_port if upstream else 0,
                    "state": upstream.state if upstream else "stopped",
                    "enabled": upstream.enabled if upstream else True,
                }
            )
        return result

    def get_config(self, name: str) -> str:
        if not safe_config_name(name):
            raise ValueError("nama config tidak valid")
        if not os.path.exists(os.path.join(self.cfg.configs_dir, name)):
            raise KeyError(name)
        return self.supervisor.read_config(name)

    async def add_config(self, name: str, text: str) -> dict:
        if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise ValueError("config terlalu besar (maks 256 KB)")
        from .util import sanitize_config_name

        clean = sanitize_config_name(name)
        if not clean:
            raise ValueError("nama file harus berakhiran .conf dan hanya berisi huruf/angka/._-")
        if os.path.exists(os.path.join(self.cfg.configs_dir, clean)):
            raise ValueError("config dengan nama itu sudah ada")
        upstream = await self.supervisor.add(clean, text)
        return {"ok": True, "tunnel": upstream.snapshot()}

    async def update_config(self, name: str, text: str) -> dict:
        self.get_config(name)
        if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise ValueError("config terlalu besar (maks 256 KB)")
        upstream = await self.supervisor.update(name, text)
        return {"ok": True, "tunnel": upstream.snapshot()}

    async def delete_config(self, name: str) -> dict:
        self.get_config(name)
        await self.supervisor.remove(name)
        if name in self.cfg.disabled:
            self.cfg.disabled.remove(name)
        save_config(self.cfg)
        return {"ok": True}

    async def tunnel_action(self, name: str, action: str) -> dict:
        upstream = self.pool.get(name)
        if upstream is None:
            raise KeyError(name)
        if action == "restart":
            await self.supervisor.restart_tunnel(name)
        elif action == "enable":
            self.supervisor.set_enabled(name, True)
            save_config(self.cfg)
        elif action == "disable":
            self.supervisor.set_enabled(name, False)
            save_config(self.cfg)
        else:
            raise ValueError("aksi tidak dikenal")
        return {"ok": True, "tunnel": upstream.snapshot()}

    async def restart_all(self) -> dict:
        names = [
            name for name in self.pool.names()
            if self.pool.get(name) is not None and self.pool.get(name).enabled
        ]
        results = []
        for index, name in enumerate(names):
            upstream = self.pool.get(name)
            if upstream is None:
                continue
            try:
                await self.supervisor.restart_tunnel(name)
                # Let this tunnel establish before rotating the next one.
                deadline = asyncio.get_running_loop().time() + 25
                while upstream.state in ("stopped", "starting", "backoff"):
                    if asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(0.25)
                results.append({"name": name, "state": upstream.state})
            except Exception as exc:  # noqa: BLE001
                log.exception("restart all gagal untuk %s", name)
                results.append({"name": name, "state": upstream.state, "error": str(exc)})
            if index < len(names) - 1:
                await asyncio.sleep(1)
        return {"ok": True, "restarted": results}

    def logs(self, name: str, lines: int) -> List[str]:
        if not safe_config_name(name):
            raise ValueError("nama config tidak valid")
        return read_last_lines(os.path.join(self.cfg.logs_dir, name + ".log"), lines)

    # -------------------------------------------------------------- settings
    def _start_rotation_loop(self) -> None:
        self._rotation_wakeup.set()
        if self._rotation_task is None or self._rotation_task.done():
            self._rotation_task = asyncio.create_task(self._rotation_loop())

    async def _rotation_loop(self) -> None:
        while not self.stop_event.is_set():
            interval = self.cfg.rotation.interval_minutes * 60.0
            if interval <= 0:
                self._rotation_wakeup.clear()
                await self._rotation_wakeup.wait()
                continue
            self._rotation_wakeup.clear()
            try:
                await asyncio.wait_for(self._rotation_wakeup.wait(), timeout=interval)
                continue
            except asyncio.TimeoutError:
                pass

            names = [
                name for name in self.pool.names()
                if self.pool.get(name) is not None and self.pool.get(name).enabled
            ]
            # Stagger tunnel restarts so there is no full-pool outage.
            stagger = min(5.0, interval / max(1, len(names))) if names else 0.0
            for index, name in enumerate(names):
                if self.stop_event.is_set():
                    return
                if index:
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=stagger)
                        return
                    except asyncio.TimeoutError:
                        pass
                upstream = self.pool.get(name)
                if upstream is None or not upstream.enabled or upstream.state != "running":
                    continue
                log.info("rotasi terjadwal: restart %s", name)
                try:
                    await self.supervisor.restart_tunnel(name)
                except Exception:  # noqa: BLE001
                    log.exception("rotasi gagal untuk %s", name)

    async def apply_settings(self, data: Dict[str, Any]) -> dict:
        if not isinstance(data, dict):
            raise ValueError("body harus object JSON")
        old = (
            replace(self.cfg.proxy),
            replace(self.cfg.probe),
            replace(self.cfg.wireproxy),
            replace(self.cfg.web),
            replace(self.cfg.exit),
        )
        old_rotation = replace(self.cfg.rotation)
        old_hash = self.cfg.web.password_hash
        try:
            self._apply_settings_values(data)
            proxy_payload = data.get("proxy") or {}
            new_proxy_password = str(proxy_payload.get("new_password") or "")
            if new_proxy_password:
                if len(new_proxy_password) < 8:
                    raise ValueError("password proxy minimal 8 karakter")
                self.cfg.proxy.auth_password_hash = hash_password(new_proxy_password)
            if self.cfg.exit.ipv6_only and not old[4].ipv6_only:
                for name in self.supervisor.config_files():
                    normalize_wg_text(self.supervisor.read_config(name), "ipv6")
            new_password = str(data.get("new_password") or "")
            if new_password:
                if len(new_password) < 6:
                    raise ValueError("password minimal 6 karakter")
                self.cfg.web.password_hash = hash_password(new_password)
            self.cfg.validate()
        except Exception:
            self.cfg.proxy, self.cfg.probe, self.cfg.wireproxy, self.cfg.web, self.cfg.exit = old
            self.cfg.rotation = old_rotation
            self.cfg.web.password_hash = old_hash
            raise

        save_config(self.cfg)
        self.pool.configure(self.cfg.proxy, self.cfg.probe)

        probe_changed = old[1] != self.cfg.probe
        proxy_listener_changed = (old[0].bind, old[0].port) != (self.cfg.proxy.bind, self.cfg.proxy.port)
        web_listener_changed = (old[3].bind, old[3].port, old[3].tls_cert, old[3].tls_key) != (
            self.cfg.web.bind,
            self.cfg.web.port,
            self.cfg.web.tls_cert,
            self.cfg.web.tls_key,
        )
        password_changed = old_hash != self.cfg.web.password_hash
        rotation_changed = old_rotation != self.cfg.rotation
        exit_mode_changed = old[4] != self.cfg.exit
        base_changed = old[2].base_socks_port != self.cfg.wireproxy.base_socks_port
        binary_changed = old[2].binary != self.cfg.wireproxy.binary

        if probe_changed:
            self.pool.start_probe_loop()
        if rotation_changed:
            self._start_rotation_loop()
        if proxy_listener_changed:
            attempted = (self.cfg.proxy.bind, self.cfg.proxy.port)
            try:
                await self._restart_proxy()
            except Exception as exc:
                log.error("gagal bind proxy baru: %s", exc)
                self.cfg.proxy.bind = old[0].bind
                self.cfg.proxy.port = old[0].port
                self.pool.configure(self.cfg.proxy, self.cfg.probe)
                save_config(self.cfg)
                try:
                    await self._stop_proxy()
                    server = HttpProxyServer(self.pool)
                    await server.start(old[0].bind, old[0].port)
                    self.proxy_server = server
                except Exception:  # noqa: BLE001
                    log.exception("gagal memulihkan listener proxy lama")
                raise ValueError("gagal bind proxy di %s:%d: %s" % (
                    attempted[0], attempted[1], exc))
        if base_changed:
            await self.supervisor.reallocate()
        elif binary_changed or exit_mode_changed:
            for name in self.pool.names():
                await self.supervisor.restart_tunnel(name)
        if web_listener_changed:
            self._schedule_web_restart(old[3])
        if password_changed:
            self.webapp.sessions.clear()
        return self.settings()

    def _apply_settings_values(self, data: Dict[str, Any]) -> None:
        proxy = data.get("proxy") or {}
        probe = data.get("probe") or {}
        rotation = data.get("rotation") or {}
        exit_settings = data.get("exit") or {}
        wireproxy = data.get("wireproxy") or {}
        web = data.get("web") or {}
        for label, value in (("proxy", proxy), ("probe", probe), ("rotation", rotation), ("exit", exit_settings), ("wireproxy", wireproxy), ("web", web)):
            if not isinstance(value, dict):
                raise ValueError("bagian %s harus object" % label)

        if "bind" in proxy:
            self.cfg.proxy.bind = str(proxy["bind"]).strip() or "0.0.0.0"
        if "port" in proxy:
            self.cfg.proxy.port = _as_int(proxy["port"], self.cfg.proxy.port)
        if "strategy" in proxy:
            self.cfg.proxy.strategy = str(proxy["strategy"]).strip().lower()
        if "max_fails" in proxy:
            self.cfg.proxy.max_fails = _as_int(proxy["max_fails"], self.cfg.proxy.max_fails)
        if "fail_timeout" in proxy:
            self.cfg.proxy.fail_timeout = _as_int(proxy["fail_timeout"], self.cfg.proxy.fail_timeout)
        if "connect_timeout" in proxy:
            self.cfg.proxy.connect_timeout = _as_float(proxy["connect_timeout"], self.cfg.proxy.connect_timeout)
        if "allowed_ips" in proxy:
            self.cfg.proxy.allowed_ips = str(proxy["allowed_ips"]).strip()
        if "auth_enabled" in proxy:
            self.cfg.proxy.auth_enabled = _as_bool(proxy["auth_enabled"], self.cfg.proxy.auth_enabled)
        if "auth_username" in proxy:
            username = str(proxy["auth_username"])
            if any(char in username for char in "\r\n:"):
                raise ValueError("username proxy tidak boleh berisi titik dua atau baris baru")
            self.cfg.proxy.auth_username = username.strip()

        if "enabled" in probe:
            self.cfg.probe.enabled = _as_bool(probe["enabled"], self.cfg.probe.enabled)
        if "interval" in probe:
            self.cfg.probe.interval = _as_int(probe["interval"], self.cfg.probe.interval)
        if "target" in probe:
            self.cfg.probe.target = str(probe["target"]).strip()
        if "timeout" in probe:
            self.cfg.probe.timeout = _as_float(probe["timeout"], self.cfg.probe.timeout)

        if "interval_minutes" in rotation:
            self.cfg.rotation.interval_minutes = _as_float(
                rotation["interval_minutes"], self.cfg.rotation.interval_minutes
            )

        if "ipv6_only" in exit_settings:
            self.cfg.exit.ipv6_only = _as_bool(exit_settings["ipv6_only"], self.cfg.exit.ipv6_only)

        if "binary" in wireproxy:
            self.cfg.wireproxy.binary = str(wireproxy["binary"]).strip() or "wireproxy"
        if "base_socks_port" in wireproxy:
            self.cfg.wireproxy.base_socks_port = _as_int(
                wireproxy["base_socks_port"], self.cfg.wireproxy.base_socks_port
            )

        if "bind" in web:
            self.cfg.web.bind = str(web["bind"]).strip() or "0.0.0.0"
        if "port" in web:
            self.cfg.web.port = _as_int(web["port"], self.cfg.web.port)
        if "username" in web:
            self.cfg.web.username = str(web["username"]).strip() or "admin"
        if "session_ttl" in web:
            self.cfg.web.session_ttl = _as_int(web["session_ttl"], self.cfg.web.session_ttl)
        if "tls_cert" in web:
            self.cfg.web.tls_cert = str(web["tls_cert"]).strip()
        if "tls_key" in web:
            self.cfg.web.tls_key = str(web["tls_key"]).strip()

    # --------------------------------------------------------------- egress
    async def test_egress(self, name: Optional[str] = None) -> dict:
        if name:
            upstream = self.pool.get(name)
            if upstream is None:
                raise KeyError(name)
        else:
            upstream = self.pool.pick()
        if upstream is None:
            raise ValueError("tidak ada tunnel yang aktif")
        total_timeout = max(10.0, min(30.0, self.cfg.probe.timeout * 2.0))
        try:
            status, body = await asyncio.wait_for(
                http_get_via_socks5(
                    "127.0.0.1",
                    upstream.socks_port,
                    self.cfg.probe.target,
                    timeout=min(total_timeout, self.cfg.probe.timeout),
                ),
                timeout=total_timeout,
            )
            if status < 200 or status >= 300:
                raise ValueError("ipify mengembalikan HTTP %d" % status)
            ip = body.strip()
            try:
                ip = str(ipaddress.ip_address(ip))
            except ValueError as exc:
                raise ValueError("respons bukan alamat IP: %s" % ip[:80]) from exc
            upstream.egress_ip = ip
            upstream.healthy = True
            upstream.last_probe = time.time()
            return {"tunnel": upstream.name, "ip": ip}
        except Exception as exc:  # noqa: BLE001
            reason = str(exc) or ("timeout" if isinstance(exc, asyncio.TimeoutError) else type(exc).__name__)
            self.pool.mark_fail(upstream, reason)
            raise ValueError("gagal test egress via %s: %s" % (upstream.name, reason))
