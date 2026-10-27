import helpers  # noqa: F401
import unittest

from virtual_monitor import config as cfg
from virtual_monitor import paths, systemd


class UnitTests(helpers.VmTestCase):
    def test_generated_units(self):
        config = cfg.load()
        config.devices["my-ipad"] = cfg.new_device(config, "my-ipad", 2388, 1668)
        units = systemd.desired_units(config, "/usr/bin/sunshine")
        name = "app-dev.lizardbyte.app.Sunshine.vm_my_ipad.service"
        self.assertIn(name, units)
        text = units[name]
        self.assertIn("BindsTo=vm-display@my-ipad.service", text)
        self.assertIn("XDG_CONFIG_HOME=", text)
        self.assertNotIn(paths.MAIN_SUNSHINE_UNIT, text)
        self.assertIn("Type=notify", units["vm-display@.service"])
        self.assertIn("StartLimitBurst=3", units["vm-display@.service"])

    def test_refuses_to_control_foreign_units(self):
        for unit in (paths.MAIN_SUNSHINE_UNIT, "gnome-virtual-monitor.service",
                     "app-dev.lizardbyte.app.Sunshine.Android.service", "pipewire.service"):
            with self.subTest(unit=unit), self.assertRaises(systemd.SystemdError):
                systemd.systemctl("stop", units=(unit,))
        self.assertEqual(self.systemctl_log(), [])

    def test_never_overwrites_files_it_did_not_write(self):
        paths.UNIT_DIR.mkdir(parents=True, exist_ok=True)
        foreign = paths.UNIT_DIR / "app-dev.lizardbyte.app.Sunshine.vm_x.service"
        foreign.write_text("[Unit]\nDescription=hand made\n")
        with self.assertRaisesRegex(systemd.SystemdError, "wasn't written by vm"):
            systemd.write_units({foreign.name: "new"})
        self.assertIn("hand made", foreign.read_text())
        with self.assertRaises(systemd.SystemdError):
            systemd.write_units({paths.MAIN_SUNSHINE_UNIT: "x"})


if __name__ == "__main__":
    unittest.main()
