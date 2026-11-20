from __future__ import annotations

import asyncio
import ipaddress


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            writer.write_eof()
        except Exception:  # noqa: BLE001
            pass


class FakeSocks5:
    """Minimal no-auth SOCKS5 server that forwards to the requested target."""

    def __init__(self) -> None:
        self.server = None
        self.connections = 0
        self.targets = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.port

    @property
    def port(self) -> int:
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_writer = None
        try:
            _ver, nmethods = await reader.readexactly(2)
            await reader.readexactly(nmethods)
            writer.write(b"\x05\x00")
            await writer.drain()

            head = await reader.readexactly(4)
            atyp = head[3]
            if atyp == 0x01:
                host = str(ipaddress.IPv4Address(await reader.readexactly(4)))
            elif atyp == 0x03:
                length = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(length)).decode("utf-8", "replace")
            elif atyp == 0x04:
                host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
            else:
                return
            port = int.from_bytes(await reader.readexactly(2), "big")
            self.connections += 1
            self.targets.append((host, port))

            try:
                upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
            except OSError:
                writer.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                return

            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            await asyncio.gather(
                pipe(reader, upstream_writer),
                pipe(upstream_reader, writer),
                return_exceptions=True,
            )
        except Exception:  # noqa: BLE001
            pass
        finally:
            if upstream_writer is not None:
                try:
                    upstream_writer.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass


class TargetServer:
    """Tiny HTTP server used as the destination in proxy tests."""

    def __init__(self, body: bytes = b"hello-wgpool") -> None:
        self.body = body
        self.server = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        except Exception:  # noqa: BLE001
            pass
        try:
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(self.body)
                + self.body
            )
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
