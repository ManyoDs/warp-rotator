from __future__ import annotations

import asyncio
import socket
import unittest

from tests.fake_socks5 import FakeSocks5, TargetServer
from wgpool.config import ProbeSettings, ProxySettings
from wgpool.http_proxy import HttpProxyServer
from wgpool.pool import Pool, Upstream
from wgpool.socks5 import http_get_via_socks5


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class HttpProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.target = TargetServer()
        self.target_port = await self.target.start()
        self.socks_a = FakeSocks5()
        self.socks_b = FakeSocks5()
        self.port_a = await self.socks_a.start()
        self.port_b = await self.socks_b.start()

    async def asyncTearDown(self):
        await self.target.stop()
        await self.socks_a.stop()
        await self.socks_b.stop()

    def make_pool(self, ports, strategy="round", max_fails=2, allowed=""):
        pool = Pool(
            ProxySettings(strategy=strategy, max_fails=max_fails, fail_timeout=60, allowed_ips=allowed),
            ProbeSettings(enabled=False),
        )
        for index, port in enumerate(ports):
            pool.add(
                Upstream(name="u%d" % index, socks_port=port, state="running", enabled=True)
            )
        return pool

    async def start_proxy(self, pool):
        server = HttpProxyServer(pool)
        await server.start("127.0.0.1", 0)
        self.addAsyncCleanup(server.stop)
        return server

    async def http_get(self, proxy_port):
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        request = (
            "GET http://127.0.0.1:%d/ HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
            % (self.target_port, self.target_port)
        )
        writer.write(request.encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout=5)
        writer.close()
        return data

    async def test_round_robin_distributes_connections(self):
        pool = self.make_pool([self.port_a, self.port_b])
        server = await self.start_proxy(pool)
        for _ in range(4):
            response = await self.http_get(server.port)
            self.assertIn(b"hello-wgpool", response)
        self.assertEqual(self.socks_a.connections, 2)
        self.assertEqual(self.socks_b.connections, 2)
        self.assertEqual(self.socks_a.targets[0][1], self.target_port)

    async def test_connect_tunnel(self):
        pool = self.make_pool([self.port_a])
        server = await self.start_proxy(pool)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(
            ("CONNECT 127.0.0.1:%d HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
             % (self.target_port, self.target_port)).encode()
        )
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        self.assertIn(b"200 Connection Established", head)
        writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        await writer.drain()
        body = await asyncio.wait_for(reader.read(), timeout=5)
        self.assertIn(b"hello-wgpool", body)
        writer.close()

    async def test_failover_to_healthy_upstream(self):
        dead_port = free_port()
        pool = self.make_pool([dead_port, self.port_a], max_fails=1)
        server = await self.start_proxy(pool)
        response = await self.http_get(server.port)
        self.assertIn(b"hello-wgpool", response)
        self.assertEqual(self.socks_a.connections, 1)
        self.assertEqual(pool.get("u0").fail_count, 0)
        self.assertGreater(pool.get("u0").dead_until, 0)

    async def test_client_allowlist_blocks_others(self):
        pool = self.make_pool([self.port_a], allowed="10.0.0.0/8")
        server = await self.start_proxy(pool)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(
            ("GET http://127.0.0.1:%d/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n" % self.target_port).encode()
        )
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout=5)
        self.assertIn(b"403", data)
        self.assertEqual(self.socks_a.connections, 0)
        writer.close()

    async def test_http_probe_helper_returns_status_and_body(self):
        status, body = await http_get_via_socks5(
            "127.0.0.1",
            self.port_a,
            "http://127.0.0.1:%d/" % self.target_port,
            timeout=3,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, "hello-wgpool")

    async def test_pool_url_probe_marks_upstream_healthy(self):
        ip_target = TargetServer(body=b"203.0.113.55")
        ip_port = await ip_target.start()
        self.addAsyncCleanup(ip_target.stop)
        probe = ProbeSettings(
            enabled=True,
            target="http://127.0.0.1:%d/" % ip_port,
            timeout=3,
        )
        pool = Pool(ProxySettings(), probe)
        upstream = pool.add(
            Upstream(name="probe", socks_port=self.port_a, state="running", enabled=True)
        )
        elapsed = await pool.probe_once(upstream)
        self.assertIsNotNone(elapsed)
        self.assertTrue(upstream.healthy)
        self.assertEqual(upstream.egress_ip, "203.0.113.55")
        self.assertEqual(upstream.last_probe_ms, round(elapsed, 1))


if __name__ == "__main__":
    unittest.main()
