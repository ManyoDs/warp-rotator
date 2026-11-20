from __future__ import annotations

import asyncio
import ipaddress
import ssl
from typing import Tuple
from urllib.parse import urlsplit

SOCKS5_VERSION = 0x05
CMD_CONNECT = 0x01
ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x03
ATYP_IPV6 = 0x04

REPLY_MESSAGES = {
    0x00: "succeeded",
    0x01: "general SOCKS server failure",
    0x02: "connection not allowed by ruleset",
    0x03: "network unreachable",
    0x04: "host unreachable",
    0x05: "connection refused",
    0x06: "TTL expired",
    0x07: "command not supported",
    0x08: "address type not supported",
}


class Socks5Error(Exception):
    pass


def _build_address(dest_host: str, dest_port: int) -> bytes:
    if not (0 <= dest_port <= 65535):
        raise Socks5Error("port tujuan tidak valid")
    host = (dest_host or "").strip().strip("[]")
    try:
        addr = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        try:
            encoded = host.encode("idna")
        except UnicodeError as exc:
            raise Socks5Error("host tidak valid: %s" % dest_host) from exc
        if not encoded or len(encoded) > 255:
            raise Socks5Error("host terlalu panjang")
        return bytes([ATYP_DOMAIN, len(encoded)]) + encoded + dest_port.to_bytes(2, "big")
    if addr.version == 4:
        return bytes([ATYP_IPV4]) + addr.packed + dest_port.to_bytes(2, "big")
    return bytes([ATYP_IPV6]) + addr.packed + dest_port.to_bytes(2, "big")


async def _read_reply(reader: asyncio.StreamReader, timeout: float) -> None:
    head = await asyncio.wait_for(reader.readexactly(4), timeout)
    if head[0] != SOCKS5_VERSION:
        raise Socks5Error("versi SOCKS tidak didukung")
    if head[1] != 0:
        raise Socks5Error(REPLY_MESSAGES.get(head[1], "kode 0x%02x" % head[1]))
    atyp = head[3]
    if atyp == ATYP_IPV4:
        await asyncio.wait_for(reader.readexactly(4 + 2), timeout)
    elif atyp == ATYP_IPV6:
        await asyncio.wait_for(reader.readexactly(16 + 2), timeout)
    elif atyp == ATYP_DOMAIN:
        length = (await asyncio.wait_for(reader.readexactly(1), timeout))[0]
        await asyncio.wait_for(reader.readexactly(length + 2), timeout)
    else:
        raise Socks5Error("alamat balasan SOCKS tidak dikenal")


async def socks5_connect(
    proxy_host: str,
    proxy_port: int,
    dest_host: str,
    dest_port: int,
    timeout: float = 10.0,
) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a TCP connection to dest via a no-auth SOCKS5 proxy.

    The destination hostname is sent as a domain (not resolved locally), so DNS
    resolution happens on the SOCKS5 side (inside the WireGuard tunnel).
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy_host, proxy_port), timeout
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise Socks5Error("gagal konek ke proxy SOCKS5 %s:%d: %s" % (proxy_host, proxy_port, exc)) from exc

    try:
        writer.write(bytes([SOCKS5_VERSION, 0x01, 0x00]))
        await asyncio.wait_for(writer.drain(), timeout)
        greeting = await asyncio.wait_for(reader.readexactly(2), timeout)
        if greeting[0] != SOCKS5_VERSION:
            raise Socks5Error("proxy bukan SOCKS5")
        if greeting[1] != 0x00:
            raise Socks5Error("proxy SOCKS5 membutuhkan autentikasi")

        request = bytes([SOCKS5_VERSION, CMD_CONNECT, 0x00]) + _build_address(dest_host, dest_port)
        writer.write(request)
        await asyncio.wait_for(writer.drain(), timeout)
        await _read_reply(reader, timeout)
        return reader, writer
    except asyncio.IncompleteReadError as exc:
        writer.close()
        raise Socks5Error("proxy SOCKS5 menutup koneksi lebih awal") from exc
    except asyncio.TimeoutError as exc:
        writer.close()
        raise Socks5Error("timeout SOCKS5 ke %s" % dest_host) from exc
    except Socks5Error:
        writer.close()
        raise
    except Exception as exc:  # noqa: BLE001 - surface as Socks5Error
        writer.close()
        raise Socks5Error("kesalahan SOCKS5: %s" % exc) from exc


async def close_writer(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except (OSError, RuntimeError):
        pass


async def _upgrade_tls(writer: asyncio.StreamWriter, hostname: str, timeout: float) -> None:
    context = ssl.create_default_context()
    loop = asyncio.get_running_loop()
    if hasattr(writer, "start_tls"):
        await asyncio.wait_for(
            writer.start_tls(context, server_hostname=hostname, ssl_handshake_timeout=timeout),
            timeout,
        )
        return

    # Compatibility path for Python versions before StreamWriter.start_tls.
    protocol = writer._protocol
    transport = writer.transport
    new_transport = await asyncio.wait_for(
        loop.start_tls(
            transport,
            protocol,
            context,
            server_side=False,
            server_hostname=hostname,
            ssl_handshake_timeout=timeout,
        ),
        timeout,
    )
    writer._transport = new_transport
    protocol._replace_transport(new_transport)


async def http_get_via_socks5(
    proxy_host: str,
    proxy_port: int,
    url: str,
    timeout: float = 10.0,
    max_response_bytes: int = 65536,
) -> Tuple[int, str]:
    """Fetch a small HTTP(S) response through SOCKS5; preserves remote DNS."""
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https") or not parsed.hostname:
        raise Socks5Error("URL probe harus http:// atau https:// dan punya hostname")
    host = parsed.hostname
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    reader, writer = await socks5_connect(proxy_host, proxy_port, host, port, timeout)
    try:
        if scheme == "https":
            await _upgrade_tls(writer, host, timeout)
        host_header = host
        if ":" in host and not host.startswith("["):
            host_header = "[%s]" % host
        default_port = 443 if scheme == "https" else 80
        if port != default_port:
            host_header += ":%d" % port
        request = (
            "GET %s HTTP/1.1\r\nHost: %s\r\n"
            "User-Agent: wgpool/0.1\r\nAccept: text/plain,*/*;q=0.1\r\n"
            "Connection: close\r\n\r\n"
        ) % (path, host_header)
        writer.write(request.encode("ascii"))
        await asyncio.wait_for(writer.drain(), timeout)
        response = bytearray()
        while len(response) < max_response_bytes:
            chunk = await asyncio.wait_for(reader.read(min(4096, max_response_bytes - len(response))), timeout)
            if not chunk:
                break
            response.extend(chunk)
        raw = bytes(response)
        head, separator, body = raw.partition(b"\r\n\r\n")
        if not separator:
            raise Socks5Error("respons HTTP probe tidak lengkap")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = status_line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            raise Socks5Error("status HTTP probe tidak valid")
        return int(parts[1]), body.decode("utf-8", "replace").strip()
    finally:
        await close_writer(writer)
