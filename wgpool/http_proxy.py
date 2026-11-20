from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from . import socks5
from .pool import Pool, Upstream
from .util import format_host_port, parse_host_port

log = logging.getLogger("wgpool.proxy")

HEADER_LIMIT = 65536
HOP_BY_HOP = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "proxy-authorization",
    "proxy-authenticate",
    "te",
    "trailer",
    "upgrade",
}


class HttpProxyError(Exception):
    pass


def parse_head(head: bytes) -> Tuple[str, str, str, List[Tuple[str, str]]]:
    text = head.decode("latin-1")
    lines = text.split("\r\n")
    request_line = lines[0].strip()
    parts = request_line.split(" ")
    if len(parts) < 3:
        raise HttpProxyError("request line tidak valid")
    method, target, version = parts[0], parts[1], parts[2]
    headers: List[Tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        headers.append((key.strip(), value.strip()))
    return method, target, version, headers


def header_get(headers: List[Tuple[str, str]], name: str) -> Optional[str]:
    name = name.lower()
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            writer.write_eof()
        except (OSError, RuntimeError, NotImplementedError):
            pass


async def copy_exact(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, size: int) -> None:
    remaining = size
    while remaining > 0:
        chunk = await reader.read(min(65536, remaining))
        if not chunk:
            raise HttpProxyError("body client terputus")
        writer.write(chunk)
        await writer.drain()
        remaining -= len(chunk)


async def copy_chunked(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while True:
        line = await reader.readuntil(b"\r\n")
        writer.write(line)
        await writer.drain()
        size = int(line.split(b";")[0].strip() or b"0", 16)
        if size == 0:
            while True:
                trailer = await reader.readuntil(b"\r\n")
                writer.write(trailer)
                await writer.drain()
                if trailer == b"\r\n":
                    return
        await copy_exact(reader, writer, size + 2)


class HttpProxyServer:
    def __init__(self, pool: Pool):
        self.pool = pool
        self.server: Optional[asyncio.AbstractServer] = None
        self._closed = False

    @property
    def port(self) -> int:
        if self.server is None or not self.server.sockets:
            return 0
        return self.server.sockets[0].getsockname()[1]

    async def start(self, bind: str, port: int) -> None:
        self._closed = False
        self.server = await asyncio.start_server(
            self._handle, bind, port, limit=HEADER_LIMIT, backlog=256
        )
        log.info("HTTP proxy listening on %s", format_host_port(bind, self.port))

    async def stop(self) -> None:
        self._closed = True
        if self.server is not None:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), timeout=2)
            except Exception:  # noqa: BLE001
                log.debug("timeout menunggu proxy server tertutup")
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        client_ip = peer[0] if peer else ""
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
            await self._close(writer)
            return

        if not self.pool.client_allowed(client_ip):
            await self._send_error(writer, 403, "IP client tidak diizinkan")
            return

        try:
            method, target, _version, headers = parse_head(head)
        except HttpProxyError as exc:
            await self._send_error(writer, 400, str(exc))
            return

        try:
            if method.upper() == "CONNECT":
                await self._handle_connect(reader, writer, target)
            else:
                await self._handle_forward(reader, writer, method, target, headers)
        except HttpProxyError as exc:
            await self._send_error(writer, 502, str(exc))
        except (ConnectionError, asyncio.TimeoutError):
            await self._close(writer)
        except Exception:  # noqa: BLE001
            log.exception("error saat memproses permintaan proxy")
            await self._send_error(writer, 500, "internal proxy error")

    async def _open_upstream(self, host: str, port: int) -> Tuple[Upstream, asyncio.StreamReader, asyncio.StreamWriter]:
        tried = set()
        last_error: Optional[Exception] = None
        attempts = max(1, min(3, len(self.pool)))
        for _ in range(attempts):
            upstream = self.pool.pick(exclude=tried)
            if upstream is None:
                break
            tried.add(upstream.name)
            try:
                reader, writer = await socks5.socks5_connect(
                    "127.0.0.1",
                    upstream.socks_port,
                    host,
                    port,
                    timeout=self.pool.proxy.connect_timeout,
                )
                self.pool.mark_ok(upstream)
                log.debug("rute %s via %s", format_host_port(host, port), upstream.name)
                return upstream, reader, writer
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self.pool.mark_fail(upstream, str(exc))
        raise HttpProxyError(
            "semua tunnel gagal: %s" % last_error if last_error else "tidak ada tunnel aktif"
        )

    async def _handle_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: str
    ) -> None:
        host, port = parse_host_port(target, 443)
        upstream, up_reader, up_writer = await self._open_upstream(host, port)
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(
                pipe(reader, up_writer),
                pipe(up_reader, writer),
                return_exceptions=True,
            )
        finally:
            await socks5.close_writer(up_writer)
            await self._close(writer)
            log.debug("tunnel selesai via %s", upstream.name)

    async def _handle_forward(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        target: str,
        headers: List[Tuple[str, str]],
    ) -> None:
        parsed = urlsplit(target)
        if parsed.scheme and parsed.scheme.lower() != "http":
            raise HttpProxyError("hanya http yang didukung untuk request non-CONNECT")
        if parsed.scheme:
            host = parsed.hostname
            port = parsed.port or 80
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
        else:
            host_header = header_get(headers, "host")
            if not host_header:
                raise HttpProxyError("header Host tidak ada")
            host, port = parse_host_port(host_header, 80)
            path = target
        if not host:
            raise HttpProxyError("target tidak valid")

        upstream, up_reader, up_writer = await self._open_upstream(host, port)
        try:
            lines = ["%s %s HTTP/1.1" % (method, path)]
            has_host = False
            for key, value in headers:
                lower = key.lower()
                if lower in HOP_BY_HOP:
                    continue
                if lower == "host":
                    has_host = True
                lines.append("%s: %s" % (key, value))
            if not has_host:
                lines.append("Host: %s" % format_host_port(host, port))
            lines.append("Connection: close")
            up_writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            await up_writer.drain()

            transfer = (header_get(headers, "transfer-encoding") or "").lower()
            length = header_get(headers, "content-length")
            if "chunked" in transfer:
                await copy_chunked(reader, up_writer)
            elif length is not None:
                await copy_exact(reader, up_writer, int(length))
            try:
                up_writer.write_eof()
            except (OSError, RuntimeError, NotImplementedError):
                pass
            await pipe(up_reader, writer)
        finally:
            await socks5.close_writer(up_writer)
            await self._close(writer)

    async def _send_error(self, writer: asyncio.StreamWriter, status: int, message: str) -> None:
        reason = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway", 500: "Internal Server Error"}.get(
            status, "Error"
        )
        body = message.encode("utf-8")
        head = (
            "HTTP/1.1 %d %s\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n\r\n"
        ) % (status, reason, len(body))
        try:
            writer.write(head.encode("latin-1") + body)
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        await self._close(writer)

    async def _close(self, writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, OSError, RuntimeError):
            pass
