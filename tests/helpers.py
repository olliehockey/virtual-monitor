"""Point every vm path at a temp dir and put fake systemctl/journalctl first on PATH.

Import this before anything from virtual_monitor.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="vm-tests-"))
FAKES = Path(__file__).resolve().parent / "fakes"
os.environ.update({
    "VM_CONFIG_DIR": str(ROOT / "config"),
    "VM_STATE_DIR": str(ROOT / "state"),
    "VM_UNIT_DIR": str(ROOT / "units"),
    "VM_RUNTIME_DIR": str(ROOT / "run"),
    "FAKE_STATE": str(ROOT / "systemd"),
    "PATH": f"{FAKES}:{os.environ.get('PATH', '')}",
    "NO_COLOR": "1",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from virtual_monitor import cli, core, paths  # noqa: E402

# Real GNOME isn't available in tests: pretend Mutter answers.
core_preflight_mutter = None


class VmTestCase(unittest.TestCase):
    def setUp(self):
        for sub in ("config", "state", "units", "run", "systemd"):
            shutil.rmtree(ROOT / sub, ignore_errors=True)
        (ROOT / "systemd").mkdir()
        (ROOT / "config").mkdir()
        (paths.DEVICES_FILE).write_text(f'[settings]\nsunshine = "{FAKES / "sunshine"}"\n')
        self._patches = []
        self.patch(core, "_mutter_check", lambda: None)
        self.patch(core, "_ports_in_use", lambda device: [])

    def tearDown(self):
        for obj, name, old in reversed(self._patches):
            setattr(obj, name, old)
        pids = ROOT / "systemd" / "pids"
        if pids.exists():
            for pid in pids.read_text().split():
                try:
                    os.kill(int(pid), 9)
                except OSError:
                    pass

    def patch(self, obj, name, value):
        self._patches.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def vm(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def systemctl_log(self) -> list[str]:
        log = ROOT / "systemd" / "log"
        return log.read_text().splitlines() if log.exists() else []

    def set_unit(self, unit: str, active: str = "active", enabled: str | None = None):
        (ROOT / "systemd" / f"{unit}.active").write_text(active)
        if enabled:
            (ROOT / "systemd" / f"{unit}.enabled").write_text(enabled)
