from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

from wgpool.config import AppConfig, load_config, save_config
from wgpool.manager import Manager
from wgpool.pool import Upstream


class RotationConfigTests(unittest.TestCase):
    def test_rotation_interval_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pool.ini")
            cfg = load_config(path, validate=False)
            self.assertEqual(cfg.rotation.interval_minutes, 0)
            cfg.rotation.interval_minutes = 45
            save_config(cfg)
            loaded = load_config(path, validate=False)
            self.assertEqual(loaded.rotation.interval_minutes, 45)


class RotationLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_enabled_tunnel_restarts_on_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(path=os.path.join(tmp, "pool.ini"))
            cfg.rotation.interval_minutes = 0.01
            manager = Manager(cfg)
            manager.pool.add(Upstream(name="one.conf", socks_port=10800, state="running"))
            restarted = []

            class FakeSupervisor:
                async def restart_tunnel(self, name):
                    restarted.append(name)

            manager.supervisor = FakeSupervisor()
            task = asyncio.create_task(manager._rotation_loop())
            await asyncio.sleep(0.8)
            manager.request_stop()
            await asyncio.wait_for(task, timeout=1)
            self.assertEqual(restarted, ["one.conf"])

    async def test_zero_interval_does_not_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(path=os.path.join(tmp, "pool.ini"))
            manager = Manager(cfg)
            manager.pool.add(Upstream(name="one.conf", socks_port=10800, state="running"))
            restarted = []

            class FakeSupervisor:
                async def restart_tunnel(self, name):
                    restarted.append(name)

            manager.supervisor = FakeSupervisor()
            task = asyncio.create_task(manager._rotation_loop())
            await asyncio.sleep(0.05)
            manager.request_stop()
            await asyncio.wait_for(task, timeout=0.5)
            self.assertEqual(restarted, [])


if __name__ == "__main__":
    unittest.main()
