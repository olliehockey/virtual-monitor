"""Filesystem locations. Each can be redirected with an env var (used by the tests)."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


HOME = Path.home()
CONFIG_DIR = _env_path("VM_CONFIG_DIR", HOME / ".config" / "virtual-monitor")
DEVICES_FILE = CONFIG_DIR / "devices.toml"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
INSTANCES_DIR = CONFIG_DIR / "instances"
STATE_DIR = _env_path("VM_STATE_DIR", HOME / ".local" / "state" / "virtual-monitor")
BACKUP_DIR = STATE_DIR / "config-backups"
UNIT_DIR = _env_path("VM_UNIT_DIR", HOME / ".config" / "systemd" / "user")
RUNTIME_DIR = _env_path(
    "VM_RUNTIME_DIR",
    Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "virtual-monitor",
)

# Directory containing the `virtual_monitor` package; units put it on PYTHONPATH.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def python_executable() -> str:
    """Absolute interpreter path for systemd units."""
    override = os.environ.get("VM_PYTHON")
    if override:
        return override
    for candidate in ("/usr/bin/python3", sys.executable, shutil.which("python3") or ""):
        if candidate and os.path.isabs(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "/usr/bin/python3"


# --- Things vm must never control or modify -----------------------------------
# Your normal ("Mac") Sunshine: unit, base port and config home.
MAIN_SUNSHINE_UNIT = "app-dev.lizardbyte.app.Sunshine.service"
MAIN_SUNSHINE_PORT = 47989


def protected_config_homes() -> set[Path]:
    """XDG config homes whose `sunshine/` folder belongs to the normal Sunshine."""
    homes = {HOME / ".config"}
    if os.environ.get("XDG_CONFIG_HOME"):
        homes.add(Path(os.environ["XDG_CONFIG_HOME"]).expanduser())
    return {h.resolve() for h in homes}


# --- The previous single-screen setup (upstream daemon + "Android" Sunshine) ---
OLD_DAEMON_UNIT = "gnome-virtual-monitor.service"
OLD_ANDROID_UNIT = "app-dev.lizardbyte.app.Sunshine.Android.service"
OLD_ANDROID_HOME = HOME / ".config" / "sunshine-instances" / "android"
# Upstream's installer adds this drop-in to the *normal* Sunshine so it waits
# for the old daemon. vm reports it but never edits it.
OLD_MAIN_DROPIN = UNIT_DIR / f"{MAIN_SUNSHINE_UNIT}.d" / "virtual-monitor.conf"
