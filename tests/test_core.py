from __future__ import annotations

import os
import tempfile
import unittest

from wgpool.config import hash_password, load_config, save_config, verify_password
from wgpool.pool import Pool, Upstream
from wgpool.config import ProbeSettings, ProxySettings
from wgpool.supervisor import PortAllocator, normalize_wg_text, validate_wg_text
from wgpool.util import sanitize_config_name

WG_VALID = """\
[Interface]
PrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
Address = 10.2.0.2/32
DNS = 10.2.0.1
ListenPort = 51820

[Peer]
PublicKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
Endpoint = 192.0.2.1:51820
AllowedIPs = 0.0.0.0/0
"""


class ConfigTextTests(unittest.TestCase):
    def test_valid_wg(self):
        ok, message = validate_wg_text(WG_VALID)
        self.assertTrue(ok, message)

    def test_missing_private_key(self):
        text = WG_VALID.replace("PrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", "")
        ok, _ = validate_wg_text(text)
        self.assertFalse(ok)

    def test_missing_peer(self):
        text = WG_VALID.split("[Peer]")[0]
        ok, _ = validate_wg_text(text)
        self.assertFalse(ok)

    def test_two_interfaces(self):
        ok, _ = validate_wg_text(WG_VALID + "\n[Interface]\nAddress = 1.2.3.4/32\n")
        self.assertFalse(ok)

    def test_normalize_removes_listen_port_only_in_interface(self):
        out = normalize_wg_text(WG_VALID)
        self.assertNotIn("ListenPort", out.split("[Peer]")[0])
        self.assertIn("[Peer]", out)
        self.assertIn("Endpoint", out)

    def test_ipv6_only_rewrites_peer_allowed_ips(self):
        out = normalize_wg_text(WG_VALID, "ipv6")
        self.assertIn("AllowedIPs = ::/0", out)
        self.assertNotIn("0.0.0.0/0", out)

    def test_dual_stack_rewrites_peer_allowed_ips(self):
        out = normalize_wg_text(WG_VALID, "dual")
        self.assertIn("AllowedIPs = 0.0.0.0/0, ::/0", out)

    def test_custom_preserves_allowed_ips(self):
        self.assertIn("AllowedIPs = 0.0.0.0/0", normalize_wg_text(WG_VALID, "custom"))

    def test_automatic_mode_rejects_multiple_peers(self):
        peer_fields = WG_VALID.split("[Peer]\n", 1)[1]
        two_peers = WG_VALID + "\n[Peer]\n" + peer_fields
        with self.assertRaisesRegex(ValueError, "satu \\[Peer\\]"):
            normalize_wg_text(two_peers, "ipv6")

    def test_global_ipv6_setting_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pool.ini")
            cfg = load_config(path, validate=False)
            cfg.exit.ipv6_only = True
            save_config(cfg)
            loaded = load_config(path, validate=False)
            self.assertTrue(loaded.exit.ipv6_only)

    def test_sanitize_names(self):
        self.assertEqual(sanitize_config_name("vpn1"), "vpn1.conf")
        self.assertIsNone(sanitize_config_name("dir/vpn2.conf"))
        self.assertIsNone(sanitize_config_name("../evil.conf"))
        self.assertIsNone(sanitize_config_name("bad name.conf"))
        self.assertIsNone(sanitize_config_name(""))


class PasswordTests(unittest.TestCase):
    def test_hash_and_verify(self):
        stored = hash_password("rahasia123")
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertTrue(verify_password(stored, "rahasia123"))
        self.assertFalse(verify_password(stored, "salah"))
        self.assertFalse(verify_password("", "x"))


class PoolTests(unittest.TestCase):
    def make_pool(self, strategy="round", max_fails=2):
        pool = Pool(
            ProxySettings(strategy=strategy, max_fails=max_fails, fail_timeout=60),
            ProbeSettings(enabled=False),
        )
        for name in ("a", "b", "c"):
            pool.add(Upstream(name=name, socks_port=10800, state="running", enabled=True))
        return pool

    def test_round_robin_cycles(self):
        pool = self.make_pool()
        seen = [pool.pick().name for _ in range(6)]
        self.assertEqual(seen, ["a", "b", "c", "a", "b", "c"])

    def test_random_only_returns_candidates(self):
        pool = self.make_pool(strategy="rand")
        names = {pool.pick().name for _ in range(30)}
        self.assertTrue(names.issubset({"a", "b", "c"}))

    def test_mark_fail_skips_upstream(self):
        pool = self.make_pool(max_fails=2)
        a = pool.get("a")
        pool.mark_fail(a)
        self.assertEqual(a.fail_count, 1)
        self.assertEqual(a.dead_until, 0)
        pool.mark_fail(a)
        self.assertGreater(a.dead_until, 0)
        self.assertEqual(a.fail_count, 0)
        picks = {pool.pick().name for _ in range(10)}
        self.assertNotIn("a", picks)

    def test_skip_disabled_and_stopped(self):
        pool = self.make_pool()
        pool.get("a").enabled = False
        pool.get("b").state = "backoff"
        self.assertEqual(pool.pick().name, "c")

    def test_exclude_all_falls_back(self):
        pool = self.make_pool()
        self.assertIsNone(pool.pick(exclude={"a", "b", "c"}))


class PortAllocatorTests(unittest.TestCase):
    def test_stable_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            alloc = PortAllocator(os.path.join(tmp, "ports.json"), 11000)
            mapping = alloc.sync(["b.conf", "a.conf"])
            self.assertEqual(mapping["a.conf"], 11000)
            self.assertEqual(mapping["b.conf"], 11001)

            mapping = alloc.sync(["a.conf", "c.conf"])
            self.assertEqual(mapping["a.conf"], 11000)
            self.assertNotIn("b.conf", mapping)
            self.assertEqual(mapping["c.conf"], 11001)

            reloaded = PortAllocator(os.path.join(tmp, "ports.json"), 11000)
            self.assertEqual(reloaded.mapping, mapping)


if __name__ == "__main__":
    unittest.main()
