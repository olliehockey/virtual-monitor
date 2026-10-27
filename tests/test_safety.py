"""End-to-end CLI behaviour against a fake systemctl: locks, refusals, rollback, ordering."""
import helpers
import json
import os
import threading
import time
import unittest

from virtual_monitor import cli, core, legacy, paths, runtime


def cli_help() -> str:
    return cli._epilog()


class SafetyTests(helpers.VmTestCase):
    def add_ipad(self):
        code, out, err = self.vm("add", "ipad", "ipad-pro-11")
        self.assertEqual(code, 0, err)

    def test_add_enable_status_disable(self):
        self.add_ipad()
        code, out, err = self.vm("enable", "ipad", "--no-wait")
        self.assertEqual(code, 0, err)
        self.assertIn("ipad on", out)
        code, out, _ = self.vm("list", "--json")
        self.assertEqual(json.loads(out)[0]["state"], "on")
        code, out, err = self.vm("disable", "ipad")
        self.assertEqual(code, 0, err)
        stops = [line for line in self.systemctl_log() if line.startswith("--user stop")]
        # Sunshine is stopped before its virtual screen.
        self.assertEqual(stops, ["--user stop app-dev.lizardbyte.app.Sunshine.vm_ipad.service",
                                 "--user stop vm-display@ipad.service"])

    def test_installer_style_add_never_enables_autostart(self):
        self.add_ipad()
        self.assertFalse(any(" enable " in f" {line} " for line in self.systemctl_log()))
        code, out, _ = self.vm("list", "--json")
        self.assertFalse(json.loads(out)[0]["autostart"])

    def test_failed_enable_is_rolled_back(self):
        self.add_ipad()
        (helpers.ROOT / "systemd" / "fail-start").write_text("1")
        code, out, err = self.vm("enable", "ipad", "--no-wait")
        self.assertEqual(code, 1)
        self.assertIn("Couldn't enable", err)
        self.assertIn("were stopped again", err.replace("Everything it started was stopped again", "were stopped again"))
        log = self.systemctl_log()
        self.assertIn("--user stop app-dev.lizardbyte.app.Sunshine.vm_ipad.service", log)
        self.assertIn("--user stop vm-display@ipad.service", log)

    def test_refuses_when_legacy_daemon_is_active(self):
        self.add_ipad()
        self.set_unit(paths.OLD_DAEMON_UNIT, "active")
        code, out, err = self.vm("enable", "ipad")
        self.assertEqual(code, core.EXIT_REFUSED)
        self.assertIn("old gnome-virtual-monitor daemon is running", err)
        self.assertFalse(any(line.startswith("--user start") for line in self.systemctl_log()))
        # And it never tried to stop the old daemon itself.
        self.assertFalse(any("gnome-virtual-monitor" in l and "is-active" not in l and "is-enabled" not in l
                             and not l.startswith("--user show") for l in self.systemctl_log()))

    def test_refuses_reused_instance_while_old_copy_runs(self):
        old = helpers.ROOT / "old-android"
        (old / "sunshine").mkdir(parents=True)
        (old / "sunshine" / "sunshine.conf").write_text("port = 48049\n")
        paths.DEVICES_FILE.write_text(paths.DEVICES_FILE.read_text() +
                                      f'\n[devices.ipad]\nwidth = 1600\nheight = 1200\nsunshine_home = "{old}"\n')
        self.set_unit(paths.OLD_ANDROID_UNIT, "active")
        code, out, err = self.vm("enable", "ipad")
        self.assertEqual(code, core.EXIT_REFUSED)
        self.assertIn("Two copies of one instance", err)
        self.assertEqual((old / "sunshine" / "sunshine.conf").read_text(), "port = 48049\n")

    def test_screen_limit(self):
        for name in ("a", "b", "c", "d"):
            self.assertEqual(self.vm("add", name, "1366x768")[0], 0)
        for name in ("a", "b", "c"):
            self.assertEqual(self.vm("enable", name, "--no-wait")[0], 0)
        code, out, err = self.vm("enable", "d", "--no-wait")
        self.assertEqual(code, core.EXIT_REFUSED)
        self.assertIn("max_screens", err)

    def test_concurrent_commands_are_rejected(self):
        self.add_ipad()
        started = threading.Event()

        def hold():
            with runtime.operation_lock("enable ipad"):
                started.set()
                time.sleep(1.0)
        t = threading.Thread(target=hold)
        t.start()
        started.wait()
        code, out, err = self.vm("disable", "ipad")
        t.join()
        self.assertEqual(code, core.EXIT_BUSY)
        self.assertIn("Another vm command is running", err)
        self.assertIn("vm enable ipad", err)

    def test_config_change_rolls_back_when_sync_fails(self):
        self.add_ipad()
        before = paths.DEVICES_FILE.read_text()

        def boom(*a, **k):
            raise core.VmError("simulated failure")
        self.patch(core, "sync", boom)
        code, out, err = self.vm("edit", "ipad", "--scale", "1.5")
        self.assertEqual(code, 1)
        self.assertEqual(paths.DEVICES_FILE.read_text(), before)

    def test_undo(self):
        self.add_ipad()
        self.vm("edit", "ipad", "--scale", "1.5")
        self.assertIn("scale = 1.5", paths.DEVICES_FILE.read_text())
        code, out, err = self.vm("undo", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("scale = 2.0", paths.DEVICES_FILE.read_text())
        code, out, err = self.vm("undo", "--yes")   # steps back again: before the add
        self.assertEqual(code, 0, err)
        self.assertNotIn("ipad", paths.DEVICES_FILE.read_text())

    def test_remove_running_device_stops_it_first(self):
        self.add_ipad()
        self.vm("enable", "ipad", "--no-wait")
        code, out, err = self.vm("remove", "ipad", "--yes")
        self.assertEqual(code, 0, err)
        log = self.systemctl_log()
        self.assertIn("--user stop vm-display@ipad.service", log)
        self.assertFalse((paths.UNIT_DIR / "app-dev.lizardbyte.app.Sunshine.vm_ipad.service").exists())

    def test_prompts_fail_cleanly_without_a_terminal(self):
        self.add_ipad()
        code, out, err = self.vm("remove", "ipad")
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)

    def test_main_sunshine_is_never_commanded(self):
        self.add_ipad()
        self.set_unit(paths.MAIN_SUNSHINE_UNIT, "active")
        for argv in (("enable", "ipad", "--no-wait"), ("status",), ("doctor",), ("disable", "--all"),
                     ("autostart", "ipad", "on"), ("remove", "ipad", "-y")):
            self.vm(*argv)
        for line in self.systemctl_log():
            if paths.MAIN_SUNSHINE_UNIT in line:
                self.assertTrue(line.startswith("--user show") or line.startswith("--user is-"), line)


class PairTests(helpers.VmTestCase):
    def test_pair_heals_duplicates_and_does_not_refuse(self):
        self.vm("add", "ipad", "ipad-pro-11")
        sent, healed = [], []
        self.patch(core, "port_open", lambda port, timeout=0.5: True)
        self.patch(core, "paired_clients", lambda device: ["iPad"])
        self.patch(core, "pair", lambda *a, **k: sent.append(a))
        self.patch(core, "heal_pairings", lambda device: healed.append(device.id) or 0)
        self.patch(core, "health", lambda *a, **k: [type("H", (), {"state": "on"})()])
        code, out, err = self.vm("pair", "ipad", "1234")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(sent), 1)
        self.assertEqual(healed, ["ipad"])

    def test_heal_removes_older_duplicates_via_api(self):
        from virtual_monitor import config as cfg, sunshine
        self.vm("add", "ipad", "ipad-pro-11")
        device = cfg.load().get("ipad")
        device.sunshine_dir.mkdir(parents=True, exist_ok=True)
        cert = "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----"
        (device.sunshine_dir / "sunshine_state.json").write_text(json.dumps({"root": {"named_devices": [
            {"name": "iPad", "cert": cert, "uuid": "u1", "enabled": "true"},
            {"name": "Mac", "cert": "OTHER", "uuid": "u2", "enabled": "true"},
            {"name": "ipad", "cert": cert.replace("\n", "\r\n"), "uuid": "u3", "enabled": "true"},
            {"name": "ipad", "cert": cert, "uuid": "u4", "enabled": "false"}]}}))
        calls = []
        self.patch(core, "port_open", lambda port, timeout=0.5: True)
        self.patch(sunshine.Api, "unpair", lambda self, uuid: calls.append(("unpair", uuid)))
        self.patch(sunshine.Api, "set_enabled", lambda self, uuid, on=True: calls.append(("enable", uuid)))
        self.assertEqual(core.heal_pairings(device), 2)
        self.assertEqual(calls, [("unpair", "u1"), ("unpair", "u3"), ("enable", "u4")])

    def test_no_duplicates_means_no_api_calls(self):
        from virtual_monitor import sunshine
        clients = [{"cert": "A", "uuid": "1"}, {"cert": "B", "uuid": "2"}]
        self.assertEqual(sunshine.duplicate_pairings(clients), ([], []))


class EaseTests(helpers.VmTestCase):
    def test_presets(self):
        code, out, err = self.vm("add", "iphone-13")
        self.assertEqual(code, 0, err)
        code, out, err = self.vm("add", "ipad", "ipad-pro-11")
        self.assertEqual(code, 0, err)
        code, out, err = self.vm("add", "mac", "macbook-air", "--position", "left")
        self.assertEqual(code, 0, err)
        data = {d["id"]: d for d in json.loads(self.vm("list", "--json")[1])}
        self.assertEqual((data["iphone-13"]["width"], data["iphone-13"]["height"]), (2532, 1170))
        self.assertEqual(data["iphone-13"]["name"], "iphone-13")
        self.assertEqual((data["ipad"]["width"], data["ipad"]["scale"]), (2420, 2.0))
        self.assertEqual(data["mac"]["position"], "left")
        code, out, err = self.vm("add", "tablet")
        self.assertEqual(code, 1)
        self.assertIn("ipad-pro-11", err)

    def test_presets_command_and_unknown_command(self):
        code, out, err = self.vm("presets")
        self.assertEqual(code, 0)
        self.assertIn("ipad-pro-11", out)
        code, out, err = self.vm("preset")
        self.assertEqual(code, 2)
        self.assertIn("did you mean presets", err)
        self.assertNotIn("_display", err)

    def test_custom_presets(self):
        code, out, err = self.vm("presets", "add", "galaxy-tab", "2560x1600", "--scale", "1.5", "--label", "Galaxy Tab")
        self.assertEqual(code, 0, err)
        self.assertIn("galaxy-tab", self.vm("presets")[1])
        self.assertIn("galaxy-tab", cli_help())
        code, out, err = self.vm("add", "tab", "galaxy-tab")
        self.assertEqual(code, 0, err)
        dev = json.loads(self.vm("list", "--json")[1])[0]
        self.assertEqual((dev["width"], dev["height"], dev["scale"]), (2560, 1600, 1.5))
        self.assertEqual(self.vm("presets", "add", "ipad-pro-11", "100x100")[0], 1)   # built-in name
        self.assertEqual(self.vm("presets", "rm", "ipad-pro-11")[0], 1)              # can't remove built-in
        self.assertEqual(self.vm("presets", "rm", "galaxy-tab")[0], 0)
        self.assertNotIn("galaxy-tab", self.vm("presets")[1])

    def test_disable_without_device_turns_everything_off(self):
        self.vm("add", "ipad", "ipad-pro-11")
        self.vm("enable", "--no-wait")          # only one device, so no name needed
        code, out, err = self.vm("disable")
        self.assertEqual(code, 0, err)
        self.assertIn("off: ipad", out)


class LegacyTests(helpers.VmTestCase):
    def test_dropin_is_reported_with_exact_path_and_left_alone(self):
        dropin_dir = paths.UNIT_DIR / f"{paths.MAIN_SUNSHINE_UNIT}.d"
        dropin_dir.mkdir(parents=True)
        dropin = dropin_dir / "virtual-monitor.conf"
        dropin.write_text("[Unit]\nWants=gnome-virtual-monitor.service\n")
        findings = legacy.detect()
        hit = [f for f in findings if str(dropin) in f.files]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0].level, legacy.WARNING)
        self.assertTrue(any("mv" in line for line in hit[0].fix))
        code, out, err = self.vm("doctor")
        self.assertIn(str(dropin), out)
        self.assertTrue(dropin.exists())
        self.assertIn("gnome-virtual-monitor", dropin.read_text())

    def test_old_android_unit_is_not_reported_as_a_stray_dependency(self):
        paths.UNIT_DIR.mkdir(parents=True, exist_ok=True)
        (paths.UNIT_DIR / paths.OLD_ANDROID_UNIT).write_text("[Unit]\nBindsTo=gnome-virtual-monitor.service\n")
        findings = legacy.detect()
        self.assertFalse([f for f in findings if "depends on the old daemon" in f.title])
        self.assertTrue([f for f in findings if f.title.startswith("Old iPad/Android Sunshine unit")])

    def test_clean_system_has_no_blockers(self):
        self.assertEqual(legacy.blockers(), [])

    def test_enabled_but_inactive_old_daemon_is_a_warning(self):
        self.set_unit(paths.OLD_DAEMON_UNIT, "inactive", enabled="enabled")
        levels = {f.title: f.level for f in legacy.detect()}
        self.assertEqual(levels["The old gnome-virtual-monitor daemon starts at login"], legacy.WARNING)


class CompletionTests(helpers.VmTestCase):
    def test_scripts_and_device_listing(self):
        self.vm("add", "ipad", "ipad-pro-11")
        code, out, _ = self.vm("_complete", "devices")
        self.assertEqual(out.split(), ["ipad"])
        for shell in ("bash", "zsh", "fish"):
            code, out, _ = self.vm("completion", shell)
            self.assertEqual(code, 0)
            self.assertIn("_complete devices", out)


if __name__ == "__main__":
    unittest.main()
