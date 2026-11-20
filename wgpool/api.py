from __future__ import annotations

import logging
import mimetypes
import os
import secrets
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

from .config import verify_password
from .httpserver import Request, Response

log = logging.getLogger("wgpool.api")

COOKIE_NAME = "wgpool_session"
MAX_LOGIN_FAILS = 5
LOGIN_LOCK_SECONDS = 30

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def _cookie_parse(value: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for part in (value or "").split(";"):
        key, sep, val = part.strip().partition("=")
        if sep:
            result[key.strip()] = val.strip()
    return result


class WebApp:
    def __init__(self, manager):
        self.manager = manager
        self.sessions: Dict[str, float] = {}
        self.login_attempts: Dict[str, Tuple[int, float, float]] = {}
        self.web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

    # ------------------------------------------------------------------ util
    @property
    def cfg(self):
        return self.manager.cfg

    def _session_for(self, req: Request) -> Optional[str]:
        cookies = _cookie_parse(req.header("cookie", "") or "")
        token = cookies.get(COOKIE_NAME)
        if not token:
            return None
        expiry = self.sessions.get(token)
        if expiry is None:
            return None
        if expiry < time.time():
            self.sessions.pop(token, None)
            return None
        return token

    def _cleanup(self) -> None:
        now = time.time()
        for token in [t for t, exp in self.sessions.items() if exp < now]:
            self.sessions.pop(token, None)
        for ip, (_count, until, updated) in list(self.login_attempts.items()):
            if (until and until < now) or now - updated > 300:
                self.login_attempts.pop(ip, None)

    def _origin_ok(self, req: Request) -> bool:
        origin = req.header("origin")
        if not origin:
            return True
        host = req.header("host", "") or ""
        return urlsplit(origin).netloc == host

    def _static(self, path: str) -> Optional[Response]:
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        if "/" in name or "\\" in name or name.startswith("."):
            return None
        full = os.path.abspath(os.path.join(self.web_dir, name))
        if not full.startswith(os.path.abspath(self.web_dir) + os.sep) and full != os.path.abspath(
            os.path.join(self.web_dir, "index.html")
        ):
            return None
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as fh:
            body = fh.read()
        content_type = MIME.get(os.path.splitext(full)[1].lower()) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        return Response(status=200, headers=[("Content-Type", content_type), ("Cache-Control", "no-store")], body=body)

    # --------------------------------------------------------------- routing
    async def handle(self, req: Request) -> Response:
        self._cleanup()
        path = req.path

        if path == "/api/login" and req.method == "POST":
            return self._login(req)
        if path == "/api/logout" and req.method == "POST":
            return self._logout(req)

        if path.startswith("/api/"):
            if self._session_for(req) is None:
                return Response.json({"error": "belum login"}, status=401)
            if req.method in ("POST", "PUT", "DELETE"):
                if req.header("x-wgpool") != "1" or not self._origin_ok(req):
                    return Response.json({"error": "permintaan ditolak (CSRF)"}, status=403)
            try:
                return await self._api(req, path)
            except KeyError:
                return Response.json({"error": "tidak ditemukan"}, status=404)
            except ValueError as exc:
                return Response.json({"error": str(exc)}, status=400)
            except PermissionError as exc:
                return Response.json({"error": str(exc)}, status=403)
            except Exception:  # noqa: BLE001
                log.exception("API error pada %s %s", req.method, path)
                return Response.json({"error": "internal server error"}, status=500)

        static = self._static(path)
        if static is not None:
            return static
        if not path.startswith("/api/"):
            index = self._static("/index.html")
            if index is not None and "." not in os.path.basename(path):
                return index
        return Response.json({"error": "tidak ditemukan"}, status=404)

    async def _api(self, req: Request, path: str) -> Response:
        segments = [seg for seg in path.strip("/").split("/") if seg]
        if segments[:2] == ["api", "status"] and req.method == "GET":
            return Response.json(self.manager.status())
        if segments[:2] == ["api", "settings"]:
            if req.method == "GET":
                return Response.json(self.manager.settings())
            if req.method == "PUT":
                return Response.json(await self.manager.apply_settings(req.json()))
        if segments[:2] == ["api", "configs"]:
            if len(segments) == 2:
                if req.method == "GET":
                    return Response.json({"configs": self.manager.list_configs()})
                if req.method == "POST":
                    name = req.query_params().get("name", "")
                    text = req.body.decode("utf-8")
                    return Response.json(await self.manager.add_config(name, text), status=201)
            if len(segments) == 3:
                name = segments[2]
                if req.method == "GET":
                    return Response.json({"name": name, "content": self.manager.get_config(name)})
                if req.method == "PUT":
                    return Response.json(await self.manager.update_config(name, req.body.decode("utf-8")))
                if req.method == "DELETE":
                    return Response.json(await self.manager.delete_config(name))
        if segments == ["api", "tunnels", "restart-all"] and req.method == "POST":
            return Response.json(await self.manager.restart_all())
        if segments[:2] == ["api", "tunnels"] and len(segments) >= 4:
            name = segments[2]
            action = segments[3]
            if action == "logs" and req.method == "GET":
                lines = int(req.query_params().get("lines", "200"))
                return Response.json({"name": name, "lines": self.manager.logs(name, lines)})
            if req.method == "POST" and action in ("restart", "enable", "disable"):
                return Response.json(await self.manager.tunnel_action(name, action))
        if segments[:2] == ["api", "test-egress"] and req.method == "POST":
            name = req.query_params().get("tunnel") or None
            try:
                return Response.json(await self.manager.test_egress(name))
            except ValueError as exc:
                return Response.json({"error": str(exc)}, status=502)
        raise KeyError(path)

    # ---------------------------------------------------------------- login
    def _login(self, req: Request) -> Response:
        ip = req.remote
        fails, locked_until, updated = self.login_attempts.get(ip, (0, 0.0, 0.0))
        now = time.time()
        if locked_until > now:
            return Response.json(
                {"error": "terlalu banyak percobaan, coba lagi dalam %d detik" % int(locked_until - now)},
                status=429,
            )
        try:
            data = req.json()
        except (ValueError, UnicodeDecodeError):
            return Response.json({"error": "body tidak valid"}, status=400)
        username = str(data.get("username", ""))
        password = str(data.get("password", ""))
        web = self.cfg.web
        if username != web.username or not verify_password(web.password_hash, password):
            if updated and now - updated > 60:
                fails = 0
            fails += 1
            if fails >= MAX_LOGIN_FAILS:
                self.login_attempts[ip] = (0, now + LOGIN_LOCK_SECONDS, now)
            else:
                self.login_attempts[ip] = (fails, 0.0, now)
            return Response.json({"error": "username atau password salah"}, status=401)
        self.login_attempts.pop(ip, None)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + max(60, web.session_ttl)
        cookie = "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (
            COOKIE_NAME,
            token,
            max(60, web.session_ttl),
        )
        if web.tls_cert:
            cookie += "; Secure"
        return Response.json({"ok": True, "username": username}, headers=[("Set-Cookie", cookie)])

    def _logout(self, req: Request) -> Response:
        token = self._session_for(req)
        if token:
            self.sessions.pop(token, None)
        cookie = "%s=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" % COOKIE_NAME
        return Response.json({"ok": True}, headers=[("Set-Cookie", cookie)])
