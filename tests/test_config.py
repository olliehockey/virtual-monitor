import helpers  # noqa: F401  (sets up temp paths)
import unittest

from virtual_monitor import config as cfg
from virtual_monitor import paths


class ConfigTests(helpers.VmTestCase):
    def test_round_trip_and_defaults(self):
        config = cfg.load()
        dev = cfg.new_device(config, "ipad", 2388, 1668)
        self.assertEqual((dev.width, dev.height, dev.scale, dev.port), (2388, 1668, 2.0, 48100))
        config.devices["ipad"] = dev
        cfg.save(config)
        again = cfg.load()
        self.assertEqual(again.devices["ipad"], dev)

    def test_ports_are_spaced_and_avoid_main_sunshine(self):
        config = cfg.load()
        for name in ("a", "b", "c"):
            config.devices[name] = cfg.new_device(config, name, 1920, 1080)
        ports = [d.port for d in config.devices.values()]
        self.assertEqual(ports, [48100, 48200, 48300])
        with self.assertRaisesRegex(cfg.ConfigError, "normal Sunshine"):
            cfg.update_device(config, "a", port=47990)
        with self.assertRaisesRegex(cfg.ConfigError, "overlapping ports"):
            cfg.update_device(config, "a", port=48210)

    def test_validation_messages(self):
        config = cfg.load()
        for bad, pattern in ((dict(width=100), "width"), (dict(scale=9), "scale"),
                             (dict(position="up"), "position"), (dict(refresh=500), "refresh")):
            size = {"width": 2388, "height": 1668, **bad}
            with self.subTest(bad=bad), self.assertRaisesRegex(cfg.ConfigError, pattern):
                cfg.new_device(config, "x", size.pop("width"), size.pop("height"), **size)
        with self.assertRaisesRegex(cfg.ConfigError, "must start with a letter"):
            cfg.new_device(config, "1pad", 1920, 1080)

    def test_refuses_to_reuse_the_normal_sunshine_config(self):
        config = cfg.load()
        with self.assertRaisesRegex(cfg.ConfigError, "normal Sunshine"):
            cfg.new_device(config, "x", 2388, 1668, sunshine_home=str(paths.HOME / ".config"))

    def test_default_scale(self):
        self.assertEqual(cfg.default_scale(2420, 1668), 2.0)
        self.assertEqual(cfg.default_scale(1920, 1080), 1.0)

    def test_slugify(self):
        self.assertEqual(cfg.slugify("Ollie's iPad Pro"), "ollie-s-ipad-pro")
        self.assertEqual(cfg.slugify("123"), "device-123")


if __name__ == "__main__":
    unittest.main()
