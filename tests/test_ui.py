from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SettingsUiTests(unittest.TestCase):
    def test_removed_allowlist_field_is_not_read_during_settings_save(self):
        html = (ROOT / "wgpool" / "web" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "wgpool" / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('name="proxy.allowed_ips"', html)
        self.assertNotIn('getField("proxy.allowed_ips")', js)


if __name__ == "__main__":
    unittest.main()
