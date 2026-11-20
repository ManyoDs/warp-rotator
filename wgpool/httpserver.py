from __future__ import annotations

import asyncio
import email.utils
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

log = logging.getLogger("wgpool.web")

HEADER_LIMIT = 65536
BODY_LIMIT = 1024 * 1024
REASONS = {
    200: "OK",
    204: "No Content",
    302: "Found",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


@dataclass
class Request:
    method: str
    target: str
    version: str
    path: str
    query: str
    headers: List[Tuple[str, str]]
    body: bytes
    remote: str

    def header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        name = name.lower()
        for key, value in self.headers:
            if key.lower() == name:
                return value
        return default

    def query_params(self) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for pair in self.query.split("&"):
            if not pair:
                continue
            key, _, value = pair.partition("=")
            result[unquote(key)] = unquote(value.replace("+", " "))
        return result

    def json(self) -> dict:
        if not self.body:
            return {}
        data = json.loads(self.body.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("body JSON harus object")
        return data


@dataclass
class Response:
    status: int = 200
    headers: List[Tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    after: Optional[Callable[[], Awaitable[None]]] = None

    @classmethod
    def json(cls, payload, status: int = 200, headers: Optional[List[Tuple[str, str]]] = None) -> "Response":
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs = [("Content-Type", "application/json; charset=utf-8")] + list(headers or [])
        return cls(status=status, headers=hdrs, body=body)

    @classmethod
    def text(cls, payload: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> "Response":
        return cls(status=status, headers=[("Content-Type", content_type)], body=payload.encode("utf-8"))

    @classmethod
    def redirect(cls, location: str) -> "Response":
        return cls(status=302, headers=[("Location", location)], body=b"")


def _parse_head(head: bytes) -> Tuple[str, str, str, List[Tuple[str, str]]]:
    text = head.decode("latin-1")
    lines = text.split("\r\n")
    parts = lines[0].strip().split(" ")
    if len(parts) != 3:
        raise ValueError("request line tidak valid")
    method, target, version = parts
    headers: List[Tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        key, sep, value = line.partition(":")
        if sep:
            headers.append((key.strip(), value.strip()))
    return method.upper(), target, version, headers


async def read_request(reader: asyncio.StreamReader, remote: str) -> Optional[Request]:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
        return None
    try:
        method, target, version, headers = _parse_head(head)
    except ValueError:
        return None

    length = 0
    for key, value in headers:
        if key.lower() == "content-length":
            try:
                length = int(value)
            except ValueError:
                return None
    if length < 0 or length > BODY_LIMIT:
        return None
    body = b""
    if length:
        try:
            body = await asyncio.wait_for(reader.readexactly(length), timeout=30)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            return None

    split = urlsplit(target)
    return Request(
        method=method,
        target=target,
        version=version,
        path=unquote(split.path or "/"),
        query=split.query,
        headers=headers,
        body=body,
        remote=remote,
    )


class HttpServer:
    def __init__(self, handler: Callable[[Request], Awaitable[Response]]):
        self.handler = handler
        self.server: Optional[asyncio.AbstractServer] = None

    @property
    def port(self) -> int:
        if self.server is None or not self.server.sockets:
            return 0
        return self.server.sockets[0].getsockname()[1]

    async def start(self, bind: str, port: int, ssl_ctx=None) -> None:
        self.server = await asyncio.start_server(
            self._handle, bind, port, limit=HEADER_LIMIT, ssl=ssl_ctx, backlog=128
        )

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), timeout=2)
            except Exception:  # noqa: BLE001
                log.debug("timeout menunggu web server tertutup")
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        remote = peer[0] if peer else ""
        try:
            while True:
                request = await read_request(reader, remote)
                if request is None:
                    break
                try:
                    response = await self.handler(request)
                except Exception:  # noqa: BLE001
                    log.exception("handler web error")
                    response = Response.json({"error": "internal server error"}, status=500)
                keep_alive = False
                await self._write(writer, request, response)
                if response.after is not None:
                    try:
                        await response.after()
                    except Exception:  # noqa: BLE001
                        log.exception("callback after-response error")
                if not keep_alive:
                    break
        except (ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError, RuntimeError):
                pass

    async def _write(self, writer: asyncio.StreamWriter, request: Request, response: Response) -> None:
        reason = REASONS.get(response.status, "OK")
        headers = list(response.headers)
        headers.append(("Content-Length", str(len(response.body))))
        headers.append(("Connection", "close"))
        headers.append(("Date", email.utils.formatdate(usegmt=True)))
        headers.append(("Server", "wgpool"))
        lines = ["HTTP/1.1 %d %s" % (response.status, reason)]
        lines.extend("%s: %s" % (key, value) for key, value in headers)
        payload = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        if request.method != "HEAD":
            payload += response.body
        try:
            writer.write(payload)
            await writer.drain()
        except (ConnectionError, OSError):
            pass
