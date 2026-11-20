from __future__ import annotations

import json
import os
import tempfile
import unittest

from wgpool.api import WebApp
from wgpool.config import AppConfig, WebSettings, hash_password
from wgpool.httpserver import Request


def make_request(method, path, query="", body=b"", headers=None, remote="1.2.3.4"):
    return Request(
        method=method,
        target=path + ("?" + query if query else ""),
        version="HTTP/1.1",
        path=path,
        query=query,
        headers=headers or [],
        body=body,
        remote=remote,
    )


class StubManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.calls = []
        self._settings = {
            "proxy": {"strategy": "round"},
            "probe": {},
            "wireproxy": {},
            "web": {},
            "password_set": True,
        }

    def status(self):
        return {"version": "test", "tunnels": [], "proxy": {"port": 8080}}

    def settings(self):
        return self._settings

    async def apply_settings(self, data):
        self.calls.append(data)
        return self._settings

    def list_configs(self):
        return []

    def get_config(self, name):
        return "[Interface]\n"

    async def add_config(self, name, text):
        return {"ok": True}

    async def update_config(self, name, text):
        return {"ok": True}

    async def delete_config(self, name):
        return {"ok": True}

    def logs(self, name, lines):
        return ["line1"]

    async def tunnel_action(self, name, action):
        return {"ok": True}

    async def test_egress(self, name):
        return {"tunnel": "a", "ip": "1.2.3.4"}


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = AppConfig(
            path=os.path.join(self.tmp.name, "pool.ini"),
            web=WebSettings(bind="0.0.0.0", username="admin", password_hash=hash_password("secret1")),
        )
        self.manager = StubManager(cfg)
        self.app = WebApp(self.manager)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def login(self, password="secret1", remote="1.2.3.4"):
        response = await self.app.handle(
            make_request(
                "POST",
                "/api/login",
                body=json.dumps({"username": "admin", "password": password}).encode(),
                remote=remote,
            )
        )
        cookie = None
        for key, value in response.headers:
            if key.lower() == "set-cookie":
                cookie = value.split(";")[0]
        return response, cookie

    async def test_requires_login(self):
        response = await self.app.handle(make_request("GET", "/api/status"))
        self.assertEqual(response.status, 401)

    async def test_login_wrong_password(self):
        response, cookie = await self.login("salah")
        self.assertEqual(response.status, 401)
        self.assertIsNone(cookie)

    async def test_login_and_status(self):
        response, cookie = await self.login()
        self.assertEqual(response.status, 200)
        self.assertTrue(cookie)
        status = await self.app.handle(
            make_request("GET", "/api/status", headers=[("Cookie", cookie)])
        )
        self.assertEqual(status.status, 200)

    async def test_mutation_requires_csrf_header(self):
        _, cookie = await self.login()
        response = await self.app.handle(
            make_request(
                "POST",
                "/api/tunnels/a/restart",
                headers=[("Cookie", cookie)],
            )
        )
        self.assertEqual(response.status, 403)
        ok = await self.app.handle(
            make_request(
                "POST",
                "/api/tunnels/a/restart",
                headers=[("Cookie", cookie), ("X-WGPool", "1")],
            )
        )
        self.assertEqual(ok.status, 200)

    async def test_apply_settings(self):
        _, cookie = await self.login()
        body = json.dumps({"proxy": {"strategy": "rand"}}).encode()
        response = await self.app.handle(
            make_request(
                "PUT",
                "/api/settings",
                body=body,
                headers=[("Cookie", cookie), ("X-WGPool", "1"), ("Content-Type", "application/json")],
            )
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.manager.calls[-1]["proxy"]["strategy"], "rand")

    async def test_rate_limit_after_failures(self):
        remote = "9.9.9.9"
        for _ in range(5):
            response, _ = await self.login("salah", remote=remote)
            self.assertEqual(response.status, 401)
        response, _ = await self.login("salah", remote=remote)
        self.assertEqual(response.status, 429)

    async def test_static_index_served(self):
        response = await self.app.handle(make_request("GET", "/"))
        self.assertEqual(response.status, 200)
        self.assertIn(b"wgpool", response.body)

    async def test_unknown_api_is_404(self):
        _, cookie = await self.login()
        response = await self.app.handle(
            make_request("GET", "/api/nope", headers=[("Cookie", cookie)])
        )
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
