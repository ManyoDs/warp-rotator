from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from typing import Optional

from . import __version__
from .config import AppConfig, hash_password, load_config, save_config
from .util import parse_host_port

log = logging.getLogger("wgpool")


def default_config_path() -> str:
    return os.path.abspath(os.environ.get("WGPOOL_CONFIG", "pool.ini"))


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        out = subprocess.check_output(
            ["ps", "-o", "stat=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return not out.decode("ascii", "replace").strip().startswith("Z")
    except Exception:  # noqa: BLE001
        return True


def _read_pid(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    if host in ("0.0.0.0", ""):
        target = "127.0.0.1"
    elif host == "::":
        target = "::1"
    else:
        target = host
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return True
    except OSError:
        return False


def cmd_init(args) -> int:
    path = os.path.abspath(args.config or default_config_path())
    if os.path.exists(path) and not args.force:
        print("config sudah ada: %s (gunakan --force untuk menimpa)" % path)
        return 1
    if args.force:
        from .config import DEFAULT_CONFIG
        from .util import atomic_write

        atomic_write(path, DEFAULT_CONFIG)
    cfg = load_config(path, validate=False)
    save_config(cfg)
    cfg.prepare()
    print("config dibuat: %s" % path)
    print("langkah berikutnya: python3 -m wgpool passwd")
    return 0


def cmd_passwd(args) -> int:
    path = os.path.abspath(args.config or default_config_path())
    cfg = load_config(path, validate=False)
    password = getpass.getpass("password baru: ")
    confirm = getpass.getpass("ulangi password: ")
    if password != confirm:
        print("password tidak sama", file=sys.stderr)
        return 1
    if len(password) < 6:
        print("password minimal 6 karakter", file=sys.stderr)
        return 1
    cfg.web.password_hash = hash_password(password)
    save_config(cfg)
    print("password web diperbarui (%s)" % path)
    return 0


def cmd_start(args) -> int:
    path = os.path.abspath(args.config or default_config_path())
    try:
        cfg = load_config(path, validate=True)
    except ValueError as exc:
        if "password" in str(exc):
            print("password web belum diset. Jalankan: python3 -m wgpool passwd", file=sys.stderr)
        else:
            print("config tidak valid: %s" % exc, file=sys.stderr)
        return 2
    if not cfg.web.password_hash:
        print("password web belum diset. Jalankan: python3 -m wgpool passwd", file=sys.stderr)
        return 2

    pid = _read_pid(cfg.pid_path)
    if pid and _pid_alive(pid):
        print("wgpool sudah jalan (pid %d)" % pid, file=sys.stderr)
        return 1

    cfg.prepare()
    with open(cfg.pid_path, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))

    from .manager import Manager

    async def _run() -> None:
        manager = Manager(cfg)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, manager.request_stop)
            except (NotImplementedError, RuntimeError):
                try:
                    signal.signal(sig, lambda *_args, m=manager: m.request_stop())
                except (ValueError, OSError):
                    pass
        try:
            await manager.run()
        finally:
            try:
                os.unlink(cfg.pid_path)
            except OSError:
                pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    finally:
        try:
            os.unlink(cfg.pid_path)
        except OSError:
            pass
    return 0


def cmd_stop(args) -> int:
    path = os.path.abspath(args.config or default_config_path())
    cfg = load_config(path, validate=False)
    pid = _read_pid(cfg.pid_path)
    if not pid:
        print("wgpool tidak berjalan (pid file tidak ada)")
        return 0
    if not _pid_alive(pid):
        print("pid file basi, dihapus")
        try:
            os.unlink(cfg.pid_path)
        except OSError:
            pass
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print("gagal mengirim SIGTERM: %s" % exc, file=sys.stderr)
        return 1
    deadline = time.time() + 15
    while time.time() < deadline:
        if not _pid_alive(pid):
            print("wgpool berhenti")
            return 0
        time.sleep(0.2)
    print("wgpool masih berjalan setelah 15 detik (pid %d)" % pid, file=sys.stderr)
    return 1


def cmd_status(args) -> int:
    path = os.path.abspath(args.config or default_config_path())
    try:
        cfg = load_config(path, validate=False)
    except Exception as exc:  # noqa: BLE001
        print("gagal membaca config: %s" % exc, file=sys.stderr)
        return 2
    pid = _read_pid(cfg.pid_path)
    running = bool(pid and _pid_alive(pid))
    print("config     : %s" % path)
    print("status     : %s" % ("running (pid %d)" % pid if running else "berhenti"))
    print("proxy      : %s:%d %s" % (cfg.proxy.bind, cfg.proxy.port, "OK" if _tcp_open(cfg.proxy.bind, cfg.proxy.port) else "-"))
    print("web        : %s:%d %s" % (cfg.web.bind, cfg.web.port, "OK" if _tcp_open(cfg.web.bind, cfg.web.port) else "-"))
    print("strategy   : %s" % cfg.proxy.strategy)
    configs = []
    if os.path.isdir(cfg.configs_dir):
        configs = sorted(n for n in os.listdir(cfg.configs_dir) if n.endswith(".conf"))
    print("tunnel     : %d config" % len(configs))
    for name in configs:
        print("  - %s%s" % (name, " (disabled)" if name in cfg.disabled else ""))
    return 0 if running else 1


def cmd_configtest(args) -> int:
    path = os.path.abspath(args.config or default_config_path())
    cfg = load_config(path, validate=False)
    cfg.prepare()
    from .manager import Manager

    async def _run() -> int:
        manager = Manager(cfg)
        await asyncio.to_thread(manager.supervisor.sync)
        failures = 0
        for name in manager.pool.names():
            upstream = manager.pool.get(name)
            try:
                _, conf = manager.supervisor.generate(upstream)
                ok, output = await manager.supervisor._validate_binary(conf)
            except Exception as exc:  # noqa: BLE001
                ok, output = False, str(exc)
            if ok:
                print("OK      %s -> 127.0.0.1:%d" % (name, upstream.socks_port))
            else:
                failures += 1
                print("FAIL    %s: %s" % (name, output.splitlines()[0] if output else "unknown"))
        return 0 if failures == 0 else 1

    return asyncio.run(_run())


def cmd_probe(args) -> int:
    path = os.path.abspath(args.config or default_config_path())
    cfg = load_config(path, validate=False)
    from .socks5 import close_writer, http_get_via_socks5, socks5_connect

    is_url = "://" in cfg.probe.target
    if not is_url:
        host, port = parse_host_port(cfg.probe.target, 0)
    configs = []
    if os.path.isdir(cfg.configs_dir):
        configs = sorted(n for n in os.listdir(cfg.configs_dir) if n.endswith(".conf"))
    ports = {}
    try:
        import json

        with open(cfg.ports_path, "r", encoding="utf-8") as fh:
            ports = json.load(fh)
    except (OSError, ValueError):
        pass

    async def _run() -> int:
        failures = 0
        for name in configs:
            socks_port = ports.get(name)
            if not socks_port:
                print("SKIP    %s (port belum dialokasikan)" % name)
                continue
            started = time.monotonic()
            writer = None
            try:
                if is_url:
                    status, body = await asyncio.wait_for(
                        http_get_via_socks5(
                            "127.0.0.1", int(socks_port), cfg.probe.target, timeout=cfg.probe.timeout
                        ),
                        timeout=cfg.probe.timeout * 2,
                    )
                    if status < 200 or status >= 300:
                        raise RuntimeError("HTTP %d" % status)
                    detail = body.strip()
                else:
                    _, writer = await socks5_connect(
                        "127.0.0.1", int(socks_port), host, port, timeout=cfg.probe.timeout
                    )
                    detail = "TCP CONNECT"
                ms = (time.monotonic() - started) * 1000
                print("OK      %-30s %6.1f ms  %s" % (name, ms, detail))
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL    %-30s %s" % (name, exc))
            finally:
                if writer is not None:
                    await close_writer(writer)
        return 0 if failures == 0 else 1

    return asyncio.run(_run())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wgpool", description="Multi-WireGuard HTTP proxy pool")
    parser.add_argument("-c", "--config", help="path ke pool.ini", default=None)
    parser.add_argument("-v", "--version", action="version", version="wgpool " + __version__)
    parser.add_argument("--debug", action="store_true", help="log debug")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", help="path ke pool.ini", default=argparse.SUPPRESS)
    common.add_argument("--debug", action="store_true", default=argparse.SUPPRESS, help="log debug")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", parents=[common], help="buat pool.ini default")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_passwd = sub.add_parser("passwd", parents=[common], help="set password web UI")
    p_passwd.set_defaults(func=cmd_passwd)

    p_start = sub.add_parser("start", parents=[common], help="jalankan proxy + web (foreground)")
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", parents=[common], help="hentikan wgpool")
    p_stop.set_defaults(func=cmd_stop)

    p_status = sub.add_parser("status", parents=[common], help="tampilkan status")
    p_status.set_defaults(func=cmd_status)

    p_test = sub.add_parser("configtest", parents=[common], help="validasi semua config WireGuard")
    p_test.set_defaults(func=cmd_configtest)

    p_probe = sub.add_parser("probe", parents=[common], help="tes koneksi tiap tunnel")
    p_probe.set_defaults(func=cmd_probe)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.debug)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
