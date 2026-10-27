import helpers  # noqa: F401
import json
import unittest
from unittest import mock

from virtual_monitor import config as cfg
from virtual_monitor import sunshine


class FakeApi(sunshine.Api):
    def __init__(self, responses):
        device = cfg.device_from_dict("ipad", {"width": 1600, "height": 1200, "port": 48100})
        super().__init__(device, ("vm", "pw"))
        self.responses = list(responses)
        self.sent = []

    def request(self, method, path, body=None, timeout=10):
        self.sent.append((method, path, body))
        return self.responses.pop(0)


class SunshineTests(helpers.VmTestCase):
    def test_merge_conf_keeps_user_lines(self):
        existing = "port = 1\n# mine\nmin_log_level = debug\nencoder = nvenc\n"
        out = sunshine.merge_conf(existing, {"port": "48100", "capture": "portal"})
        self.assertIn("port = 48100", out)
        self.assertIn("min_log_level = debug", out)
        self.assertIn("# mine", out)
        self.assertNotIn("encoder", out)  # removed from vm config -> back to auto
        self.assertIn("capture = portal", out)

    def test_pin_new_api_uses_pending_pairing_id(self):
        api = FakeApi([sunshine.ApiResult(200, {"pairings": [{"id": "a" * 32, "name": "iPad"}]}),
                       sunshine.ApiResult(200, {"status": True})])
        self.assertTrue(api.submit_pin("1234", "iPad"))
        self.assertEqual(api.sent[-1], ("POST", "/api/pin", {"pairing_id": "a" * 32, "pin": "1234", "name": "iPad"}))

    def test_pin_old_api(self):
        api = FakeApi([sunshine.ApiResult(404, {"error": "not found"}), sunshine.ApiResult(200, {"status": "true"})])
        self.assertTrue(api.submit_pin("0042", "iPad"))
        self.assertEqual(api.sent[-1], ("POST", "/api/pin", {"pin": "0042", "name": "iPad"}))

    def test_pin_uses_newest_waiting_request(self):
        api = FakeApi([sunshine.ApiResult(200, {"pairings": [{"id": "a" * 32}, {"id": "b" * 32}]}),
                       sunshine.ApiResult(200, {"status": True})])
        self.assertTrue(api.submit_pin("1234", "iPad"))
        self.assertEqual(api.sent[-1][2]["pairing_id"], "b" * 32)

    def test_pin_validation(self):
        with self.assertRaises(sunshine.SunshineError):
            FakeApi([]).submit_pin("12a4", "x")

    def test_credentials_file_is_private(self):
        sunshine.save_credentials("ipad", "vm", "secret")
        mode = sunshine.paths.CREDENTIALS_FILE.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertEqual(sunshine.credentials("ipad"), ("vm", "secret"))
        self.assertEqual(json.loads(sunshine.paths.CREDENTIALS_FILE.read_text())["ipad"]["password"], "secret")


if __name__ == "__main__":
    unittest.main()
