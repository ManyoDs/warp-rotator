from __future__ import annotations

import os
import tempfile
import unittest

from wgpool.config import AppConfig, load_config
from wgpool.manager import Manager
from wgpool.pool import Upstream


class GlobalExitModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_ipv6_toggle_persists_and_restarts_all_tunnels(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(path=os.path.join(tmp, "pool.ini"))
            cfg.web.bind = "127.0.0.1"
            manager = Manager(cfg)
            manager.pool.add(Upstream(name="warp1.conf", socks_port=10800, state="running"))
            manager.pool.add(Upstream(name="warp2.conf", socks_port=10801, state="running"))
            restarted = []

            class FakeSupervisor:
                def config_files(self):
                    return []

                async def restart_tunnel(self, name):
                    restarted.append(name)

            manager.supervisor = FakeSupervisor()
            result = await manager.apply_settings({"exit": {"ipv6_only": True}})
            self.assertTrue(result["exit"]["ipv6_only"])
            self.assertEqual(restarted, ["warp1.conf", "warp2.conf"])
            self.assertTrue(load_config(cfg.path, validate=False).exit.ipv6_only)

    async def test_restart_all_skips_disabled_and_rotates_sequentially(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(path=os.path.join(tmp, "pool.ini"))
            cfg.web.bind = "127.0.0.1"
            manager = Manager(cfg)
            manager.pool.add(Upstream(name="warp1.conf", socks_port=10800, state="running"))
            manager.pool.add(Upstream(name="warp2.conf", socks_port=10801, state="running"))
            manager.pool.add(Upstream(name="off.conf", socks_port=10802, state="disabled", enabled=False))
            restarted = []

            class FakeSupervisor:
                async def restart_tunnel(self, name):
                    restarted.append(name)
                    manager.pool.get(name).state = "running"

            manager.supervisor = FakeSupervisor()
            result = await manager.restart_all()
            self.assertEqual(restarted, ["warp1.conf", "warp2.conf"])
            self.assertEqual([item["state"] for item in result["restarted"]], ["running", "running"])


if __name__ == "__main__":
    unittest.main()
