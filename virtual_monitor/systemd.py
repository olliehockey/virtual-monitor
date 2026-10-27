"""systemd --user units for each device, plus a guarded systemctl wrapper.

Per device:
  vm-display@<id>.service                         the virtual screen (template)
  app-dev.lizardbyte.app.Sunshine.vm_<id>.service  its dedicated Sunshine

The Sunshine unit name gives every instance its own Portal app id, so GNOME
keeps their screen-share grants apart. Stopping the screen also stops its
Sunshine (BindsTo=). vm refuses to run systemctl on any other unit.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .config import Config, Device

MARKER = ("# Managed by vm (virtual-monitor). Regenerated automatically - "
          "edit ~/.config/virtual-monitor/devices.toml instead.")
DISPLAY_PREFIX = "vm-display@"
DISPLAY_TEMPLATE = "vm-display@.service"
SUNSHINE_PREFIX = "app-dev.lizardbyte.app.Sunshine.vm_"


class SystemdError(Exception):
    pass


def display_unit(device_id: str) -> str:
    return f"{DISPLAY_PREFIX}{device_id}.service"


def sunshine_unit(device_id: str) -> str:
    return f"{SUNSHINE_PREFIX}{device_id.replace('-', '_')}.service"


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_display_template(python: str, package_root: Path) -> str:
    # Type=notify: `systemctl start` returns only once the screen exists and is placed.
    # Strict restart limits: a crash loop must never keep hammering Mutter.
    return f"""{MARKER}
[Unit]
Description=vm virtual screen (%i)
After=graphical-session.target pipewire.service
PartOf=graphical-session.target
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=notify
NotifyAccess=main
Environment={_quote(f"PYTHONPATH={package_root}")}
ExecStart={_quote(python)} -m virtual_monitor _display %i
TimeoutStartSec=60
TimeoutStopSec=15
Restart=on-failure
RestartSec=10
# 2 = bad config, 3 = refused by a safety check: retrying can't help.
RestartPreventExitStatus=2 3
"""


def render_sunshine_unit(device: Device, python: str, package_root: Path, sunshine_bin: str) -> str:
    return f"""{MARKER}
[Unit]
Description=Sunshine for {device.name} (vm)
BindsTo={display_unit(device.id)}
After={display_unit(device.id)} xdg-desktop-portal.service graphical-session.target
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
Environment={_quote(f"XDG_CONFIG_HOME={device.xdg_config_home}")}
Environment=SUNSHINE_MIGRATE_CONFIG=0
Environment={_quote(f"PYTHONPATH={package_root}")}
ExecStartPre={_quote(python)} -m virtual_monitor _prestart {device.id}
ExecStart={_quote(sunshine_bin)}
TimeoutStartSec=45
TimeoutStopSec=15
Restart=on-failure
RestartSec=10
UMask=0077

[Install]
WantedBy=graphical-session.target
"""


def desired_units(config: Config, sunshine_bin: str) -> dict[str, str]:
    python = paths.python_executable()
    units = {DISPLAY_TEMPLATE: render_display_template(python, paths.PACKAGE_ROOT)}
    for device in config.devices.values():
        units[sunshine_unit(device.id)] = render_sunshine_unit(
            device, python, paths.PACKAGE_ROOT, sunshine_bin)
    return units


def snapshot_units(unit_dir: Path | None = None) -> dict[str, str]:
    """Contents of every vm-managed unit file (for rollback)."""
    unit_dir = unit_dir or paths.UNIT_DIR
    out = {}
    for pattern in (DISPLAY_TEMPLATE, f"{SUNSHINE_PREFIX}*.service"):
        for file in unit_dir.glob(pattern):
            try:
                text = file.read_text(encoding="utf-8")
            except OSError:
                continue
            if text.startswith(MARKER):
                out[file.name] = text
    return out


def write_units(units: dict[str, str], unit_dir: Path | None = None) -> tuple[list[str], list[str]]:
    """Write changed units and delete vm units no longer wanted. Returns (changed, removed)."""
    unit_dir = unit_dir or paths.UNIT_DIR
    unit_dir.mkdir(parents=True, exist_ok=True)
    changed, removed = [], []
    for name, content in units.items():
        if not (name == DISPLAY_TEMPLATE or name.startswith(SUNSHINE_PREFIX)):
            raise SystemdError(f"Refusing to write a unit vm doesn't own: {name}")
        target = unit_dir / name
        try:
            current = target.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current == content:
            continue
        if current is not None and not current.startswith(MARKER):
            raise SystemdError(f"{target} exists and wasn't written by vm; not overwriting it")
        tmp = target.with_name(f".{name}.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)
        changed.append(name)
    for stale in unit_dir.glob(f"{SUNSHINE_PREFIX}*.service"):
        if stale.name in units:
            continue
        try:
            if stale.read_text(encoding="utf-8").startswith(MARKER):
                stale.unlink()
                removed.append(stale.name)
        except OSError:
            pass
    return changed, removed


# --------------------------------------------------------------------------- systemctl

def is_ours(unit: str) -> bool:
    return unit.startswith(DISPLAY_PREFIX) or unit.startswith(SUNSHINE_PREFIX)


def _assert_ours(unit: str) -> None:
    if unit == paths.MAIN_SUNSHINE_UNIT:
        raise SystemdError("Refusing to control your normal Sunshine service")
    if not is_ours(unit):
        raise SystemdError(f"Refusing to control a unit vm doesn't own: {unit}")


def systemctl(*args: str, units: tuple[str, ...] = (), timeout: float = 60,
              check: bool = True) -> subprocess.CompletedProcess:
    """`systemctl --user ARGS UNITS` with a hard timeout. Only vm's own units allowed."""
    for unit in units:
        _assert_ours(unit)
    cmd = ["systemctl", "--user", *args, *units]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise SystemdError("systemctl not found - vm needs a systemd user session") from exc
    except subprocess.TimeoutExpired as exc:
        raise SystemdError(f"timed out after {timeout:g}s: {' '.join(cmd)}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SystemdError(detail or f"{' '.join(cmd)} failed (exit {result.returncode})")
    return result


def daemon_reload() -> None:
    systemctl("daemon-reload", timeout=30)


@dataclass(frozen=True)
class UnitState:
    active: str = "inactive"   # active | activating | deactivating | inactive | failed | unknown
    sub: str = ""
    enabled: str = ""
    result: str = ""
    since: str = ""


def states(units: list[str]) -> dict[str, UnitState]:
    """Read-only state for many units (any unit) in one call."""
    if not units:
        return {}
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show",
             "--property=Id,ActiveState,SubState,UnitFileState,Result,StateChangeTimestamp",
             "--", *units],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {u: UnitState(active="unknown") for u in units}
    blocks = [b for b in result.stdout.strip().split("\n\n") if b.strip()]
    out: dict[str, UnitState] = {}
    for unit, block in zip(units, blocks):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        out[unit] = UnitState(props.get("ActiveState", "unknown"), props.get("SubState", ""),
                              props.get("UnitFileState", ""), props.get("Result", ""),
                              props.get("StateChangeTimestamp", ""))
    for unit in units:
        out.setdefault(unit, UnitState(active="unknown"))
    return out
