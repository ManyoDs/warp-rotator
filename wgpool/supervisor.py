from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import time
from typing import Dict, List, Optional, Tuple

from .config import AppConfig
from .pool import Pool, Upstream
from .util import atomic_write, ensure_dir, safe_config_name

log = logging.getLogger("wgpool.supervisor")

VALID_STATES = ("stopped", "starting", "running", "backoff", "disabled", "invalid")


def scan_wg_sections(text: str) -> List[Tuple[str, List[Tuple[str, str]]]]:
    sections: List[Tuple[str, List[Tuple[str, str]]]] = []
    current: Optional[List[Tuple[str, str]]] = None
    current_name = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_name = line[1:-1].strip().lower()
            current = []
            sections.append((current_name, current))
            continue
        if "=" in line and current is not None:
            key, _, value = line.partition("=")
            current.append((key.strip(), value.strip()))
    return sections


def validate_wg_text(text: str) -> Tuple[bool, str]:
    sections = scan_wg_sections(text)
    interfaces = [sec for name, sec in sections if name == "interface"]
    peers = [sec for name, sec in sections if name == "peer"]
    if len(interfaces) != 1:
        return False, "harus ada tepat satu section [Interface]"
    if not peers:
        return False, "harus ada minimal satu section [Peer]"
    iface = {k.lower(): v for k, v in interfaces[0]}
    if not iface.get("privatekey"):
        return False, "[Interface] wajib punya PrivateKey"
    if not iface.get("address"):
        return False, "[Interface] wajib punya Address"
    for index, peer in enumerate(peers, 1):
        keys = {k.lower(): v for k, v in peer}
        if not keys.get("publickey"):
            return False, "[Peer] #%d wajib punya PublicKey" % index
    return True, "ok"


def normalize_wg_text(text: str, ip_mode: str = "custom") -> str:
    """Normalize generated WG config without modifying the uploaded source."""
    ip_mode = (ip_mode or "custom").lower()
    allowed = {"custom": None, "dual": "0.0.0.0/0, ::/0", "ipv6": "::/0"}
    if ip_mode not in allowed:
        raise ValueError("ip_mode harus custom, dual, atau ipv6")
    peers = sum(1 for name, _entries in scan_wg_sections(text) if name == "peer")
    if ip_mode != "custom" and peers != 1:
        raise ValueError("mode IP otomatis hanya didukung untuk satu [Peer]; gunakan Custom")

    out: List[str] = []
    section = ""
    peer_allowed_written = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            if section == "peer" and ip_mode != "custom" and not peer_allowed_written:
                out.append("AllowedIPs = " + allowed[ip_mode])
                peer_allowed_written = True
            section = line[1:-1].strip().lower()
            out.append(raw.rstrip())
            continue
        if section == "interface" and "=" in line:
            key = line.split("=", 1)[0].strip().lower()
            if key == "listenport":
                continue
        if section == "peer" and ip_mode != "custom" and "=" in line:
            key = line.split("=", 1)[0].strip().lower()
            if key == "allowedips":
                if not peer_allowed_written:
                    out.append("AllowedIPs = " + allowed[ip_mode])
                    peer_allowed_written = True
                continue
        out.append(raw.rstrip())
    if section == "peer" and ip_mode != "custom" and not peer_allowed_written:
        out.append("AllowedIPs = " + allowed[ip_mode])
    return "\n".join(out) + "\n"


def peer_endpoint(text: str) -> str:
    for name, entries in scan_wg_sections(text):
        if name != "peer":
            continue
        for key, value in entries:
            if key.lower() == "endpoint":
                return value
    return ""


class PortAllocator:
    def __init__(self, path: str, base: int):
        self.path = path
        self.base = base
        self.mapping: Dict[str, int] = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self.mapping = {str(k): int(v) for k, v in data.items()}
        except (OSError, ValueError):
            self.mapping = {}

    def save(self) -> None:
        atomic_write(self.path, json.dumps(self.mapping, indent=2, sort_keys=True) + "\n")

    def sync(self, names: List[str]) -> Dict[str, int]:
        wanted = set(names)
        changed = False
        for name in list(self.mapping):
            if name not in wanted:
                del self.mapping[name]
                changed = True
        used = set(self.mapping.values())
        free = [n for n in names if n not in self.mapping]
        for name in sorted(free):
            port = self.base
            while port in used or port > 65535:
                port += 1
            self.mapping[name] = port
            used.add(port)
            changed = True
        if changed:
            self.save()
        return dict(self.mapping)

    def reallocate(self, names: List[str], base: int) -> Dict[str, int]:
        self.base = base
        self.mapping = {}
        used: set = set()
        for name in sorted(names):
            port = base
            while port in used or port > 65535:
                port += 1
            self.mapping[name] = port
            used.add(port)
        self.save()
        return dict(self.mapping)


class Supervisor:
    def __init__(self, cfg: AppConfig, pool: Pool):
        self.cfg = cfg
        self.pool = pool
        self._tasks: Dict[str, asyncio.Task] = {}
        self._stops: Dict[str, asyncio.Event] = {}
        self.ports = PortAllocator(cfg.ports_path, cfg.wireproxy.base_socks_port)

    # ------------------------------------------------------------------ setup
    def config_files(self) -> List[str]:
        if not os.path.isdir(self.cfg.configs_dir):
            return []
        names = [
            name
            for name in os.listdir(self.cfg.configs_dir)
            if name.lower().endswith(".conf") and safe_config_name(name)
        ]
        return sorted(names)

    def read_config(self, name: str) -> str:
        path = os.path.join(self.cfg.configs_dir, name)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def write_config(self, name: str, text: str) -> None:
        if not safe_config_name(name):
            raise ValueError("nama config tidak valid")
        path = os.path.join(self.cfg.configs_dir, name)
        atomic_write(path, text if text.endswith("\n") else text + "\n")

    def delete_config_file(self, name: str) -> None:
        path = os.path.join(self.cfg.configs_dir, name)
        if os.path.exists(path):
            os.unlink(path)

    def sync(self) -> Dict[str, int]:
        """Create Upstream objects for every config file, allocate local ports."""
        names = self.config_files()
        mapping = self.ports.sync(names)
        for name in names:
            if self.pool.get(name) is not None:
                continue
            text = self.read_config(name)
            upstream = Upstream(
                name=name,
                socks_port=mapping[name],
                enabled=name not in set(self.cfg.disabled),
                endpoint=peer_endpoint(text),
                log_path=os.path.join(self.cfg.logs_dir, name + ".log"),
            )
            if not upstream.enabled:
                upstream.state = "disabled"
            self.pool.add(upstream)
        for name in list(self.pool.names()):
            if name not in names:
                self.pool.remove(name)
        return mapping

    async def start_all(self) -> None:
        self.sync()
        for name in self.pool.names():
            upstream = self.pool.get(name)
            if upstream and upstream.enabled:
                self.start_tunnel(name)

    # ------------------------------------------------------------------ paths
    def _paths(self, name: str) -> Tuple[str, str]:
        wg_path = os.path.join(self.cfg.wg_dir, name)
        conf_path = os.path.join(self.cfg.run_dir, "wireproxy-" + name)
        return wg_path, conf_path

    def generate(self, upstream: Upstream) -> Tuple[str, str]:
        text = self.read_config(upstream.name)
        ok, message = validate_wg_text(text)
        if not ok:
            raise ValueError(message)
        wg_path, conf_path = self._paths(upstream.name)
        mode = "ipv6" if self.cfg.exit.ipv6_only else "custom"
        atomic_write(wg_path, normalize_wg_text(text, mode))
        conf = (
            "WGConfig = %s\n\n[Socks5]\nBindAddress = 127.0.0.1:%d\n"
            % (wg_path, upstream.socks_port)
        )
        atomic_write(conf_path, conf)
        return wg_path, conf_path

    def _command(self) -> List[str]:
        return shlex.split(self.cfg.wireproxy.binary)

    def _binary_available(self) -> bool:
        cmd = self._command()
        if not cmd:
            return False
        if os.path.isabs(cmd[0]) or os.sep in cmd[0]:
            return os.path.exists(cmd[0])
        return shutil.which(cmd[0]) is not None

    async def _validate_binary(self, conf_path: str) -> Tuple[bool, str]:
        if not self._binary_available():
            return False, "binary wireproxy tidak ditemukan: %s" % self.cfg.wireproxy.binary
        cmd = self._command() + ["-n", "-c", conf_path]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            return False, "configtest wireproxy timeout"
        except OSError as exc:
            return False, "gagal menjalankan wireproxy: %s" % exc
        output = (out or b"").decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            return False, output or "configtest gagal (kode %s)" % proc.returncode
        return True, output

    async def validate_text(self, text: str, ip_mode: str = "custom") -> Tuple[bool, str]:
        ok, message = validate_wg_text(text)
        if not ok:
            return False, message
        name = "_validate.conf"
        wg_path = os.path.join(self.cfg.wg_dir, name)
        conf_path = os.path.join(self.cfg.run_dir, "wireproxy-" + name)
        try:
            normalized = normalize_wg_text(text, ip_mode)
        except ValueError as exc:
            return False, str(exc)
        atomic_write(wg_path, normalized)
        atomic_write(
            conf_path,
            "WGConfig = %s\n\n[Socks5]\nBindAddress = 127.0.0.1:1\n" % wg_path,
        )
        try:
            return await self._validate_binary(conf_path)
        finally:
            for path in (wg_path, conf_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    # -------------------------------------------------------------- lifecycle
    def start_tunnel(self, name: str) -> None:
        upstream = self.pool.get(name)
        if upstream is None:
            raise KeyError(name)
        if not upstream.enabled:
            upstream.state = "disabled"
            return
        if name in self._tasks and not self._tasks[name].done():
            return
        stop = asyncio.Event()
        self._stops[name] = stop
        task = asyncio.create_task(self._run(upstream, stop), name="tunnel:" + name)
        self._tasks[name] = task
        task.add_done_callback(lambda _t, n=name: self._tasks.pop(n, None))

    async def stop_tunnel(self, name: str, timeout: float = 8.0) -> None:
        stop = self._stops.pop(name, None)
        if stop is not None:
            stop.set()
        upstream = self.pool.get(name)
        if upstream is not None and upstream.proc is not None:
            await self._terminate(upstream.proc)
        task = self._tasks.get(name)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
            except Exception:  # noqa: BLE001
                pass
        if upstream is not None:
            upstream.proc = None
            if upstream.state != "disabled":
                upstream.state = "stopped"

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            pass

    async def _sleep(self, seconds: float, stop: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _run(self, upstream: Upstream, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            if self.pool.get(upstream.name) is not upstream:
                return
            if not upstream.enabled:
                upstream.state = "disabled"
                return
            try:
                _, conf_path = self.generate(upstream)
            except (ValueError, OSError) as exc:
                upstream.state = "invalid"
                upstream.last_error = str(exc)
                log.error("config %s tidak valid: %s", upstream.name, exc)
                return

            ok, output = await self._validate_binary(conf_path)
            if stop.is_set():
                return
            if not ok:
                upstream.state = "invalid"
                upstream.last_error = output
                log.error("configtest %s gagal: %s", upstream.name, output)
                return

            upstream.state = "starting"
            ensure_dir(self.cfg.logs_dir)
            log_path = upstream.log_path or os.path.join(self.cfg.logs_dir, upstream.name + ".log")
            upstream.log_path = log_path
            log_file = open(log_path, "ab", buffering=0)
            try:
                cmd = self._command() + ["-c", conf_path]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=log_file,
                    stderr=log_file,
                )
            except OSError as exc:
                log_file.close()
                upstream.state = "backoff"
                upstream.last_error = "gagal spawn wireproxy: %s" % exc
                attempt += 1
                await self._sleep(min(2 ** attempt, 30), stop)
                continue

            upstream.proc = proc
            upstream.state = "running"
            upstream.started_at = time.time()
            upstream.healthy = None
            upstream.egress_ip = ""
            upstream.last_error = ""
            log.info("tunnel %s jalan (pid %s) socks=127.0.0.1:%d", upstream.name, proc.pid, upstream.socks_port)

            started = time.monotonic()
            returncode = await proc.wait()
            log_file.close()
            upstream.proc = None
            if stop.is_set() or self.pool.get(upstream.name) is not upstream:
                break
            ran_for = time.monotonic() - started
            if ran_for > 60:
                attempt = 0
            upstream.restart_count += 1
            upstream.state = "backoff"
            upstream.healthy = False
            upstream.last_error = "wireproxy keluar (kode %s)" % returncode
            attempt += 1
            delay = min(2 ** attempt, 30)
            log.warning(
                "tunnel %s berhenti (kode %s), restart dalam %ss",
                upstream.name,
                returncode,
                delay,
            )
            await self._sleep(delay, stop)

        if self.pool.get(upstream.name) is upstream:
            upstream.proc = None
            if upstream.enabled and upstream.state != "invalid":
                upstream.state = "stopped"

    async def restart_tunnel(self, name: str) -> None:
        upstream = self.pool.get(name)
        if upstream is None:
            raise KeyError(name)
        await self.stop_tunnel(name)
        if upstream.enabled:
            self.start_tunnel(name)
        else:
            upstream.state = "disabled"

    def set_enabled(self, name: str, enabled: bool) -> None:
        upstream = self.pool.get(name)
        if upstream is None:
            raise KeyError(name)
        upstream.enabled = enabled
        if enabled:
            if name in self.cfg.disabled:
                self.cfg.disabled.remove(name)
        else:
            if name not in self.cfg.disabled:
                self.cfg.disabled.append(name)
        if enabled:
            self.start_tunnel(name)
        else:
            asyncio.create_task(self.stop_tunnel(name))

    async def add(self, name: str, text: str, validate_binary: bool = True) -> Upstream:
        ok, message = validate_wg_text(text)
        if not ok:
            raise ValueError(message)
        if validate_binary and self._binary_available():
            valid, output = await self.validate_text(
                text, ip_mode="ipv6" if self.cfg.exit.ipv6_only else "custom"
            )
            if not valid:
                raise ValueError(output)
        self.write_config(name, text)
        mapping = self.ports.sync(self.config_files())
        upstream = Upstream(
            name=name,
            socks_port=mapping[name],
            enabled=True,
            endpoint=peer_endpoint(text),
            log_path=os.path.join(self.cfg.logs_dir, name + ".log"),
        )
        self.pool.add(upstream)
        self.start_tunnel(name)
        return upstream

    async def update(self, name: str, text: str) -> Upstream:
        upstream = self.pool.get(name)
        if upstream is None:
            raise KeyError(name)
        ok, message = validate_wg_text(text)
        if not ok:
            raise ValueError(message)
        if self._binary_available():
            valid, output = await self.validate_text(
                text, ip_mode="ipv6" if self.cfg.exit.ipv6_only else "custom"
            )
            if not valid:
                raise ValueError(output)
        self.write_config(name, text)
        upstream.endpoint = peer_endpoint(text)
        await self.restart_tunnel(name)
        return upstream

    async def remove(self, name: str) -> None:
        await self.stop_tunnel(name)
        self.pool.remove(name)
        self.delete_config_file(name)
        self.ports.sync(self.config_files())

    async def reallocate(self) -> None:
        mapping = self.ports.reallocate(self.config_files(), self.cfg.wireproxy.base_socks_port)
        names = list(self.pool.names())
        for name in names:
            await self.stop_tunnel(name)
        for name in names:
            upstream = self.pool.get(name)
            if upstream is not None:
                upstream.socks_port = mapping[name]
        for name in names:
            upstream = self.pool.get(name)
            if upstream is not None and upstream.enabled:
                self.start_tunnel(name)

    async def stop_all(self) -> None:
        names = list(self.pool.names())
        await asyncio.gather(*(self.stop_tunnel(name) for name in names), return_exceptions=True)
