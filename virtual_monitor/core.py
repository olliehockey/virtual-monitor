"""Operations behind every `vm` command.

Safety model
------------
* One changing command at a time (runtime.operation_lock); a second one fails
  fast with "busy" instead of interleaving.
* Every systemctl / D-Bus / network call has a timeout.
* `enable` runs preflight checks first and refuses (exit 3) rather than risk
  a fight over GNOME's displays: legacy daemon running, port in use, screen
  limit reached, no GNOME session, Sunshine missing...
* A failed `enable` is rolled back: its Sunshine and screen are stopped
  in order, and the error comes with the last log lines.
* `disable` stops Sunshine first (so it closes its screen capture cleanly),
  then the virtual screen.
* Config changes are transactions: devices.toml is backed up, units are
  regenerated, and on any failure both are restored. `vm undo` reverts the
  last change.
* Only vm's own units/instances are ever touched. The normal (Mac) Sunshine,
  its drop-ins, config, credentials and runtime state are never modified.
"""
from __future__ import annotations

import errno
import os
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg
from . import legacy, paths, runtime, sunshine, systemd

EXIT_ERROR = 1
EXIT_REFUSED = 3
EXIT_BUSY = 4
MAX_BACKUPS = 30


class VmError(Exception):
    code = EXIT_ERROR

    def __init__(self, message: str, hint: str = "", details: str = ""):
        super().__init__(message)
        self.hint = hint
        self.details = details


class Refused(VmError):
    """A safety check said no. Nothing was changed."""
    code = EXIT_REFUSED


class Busy(VmError):
    code = EXIT_BUSY


@contextmanager
def operation(description: str, wait: float = 0):
    try:
        with runtime.operation_lock(description, timeout=wait):
            yield
    except runtime.Busy as exc:
        raise Busy(str(exc)) from None


# =========================================================================== config transactions

def _backup_current() -> Path | None:
    if not paths.DEVICES_FILE.exists():
        return None
    paths.BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns()}"  # sorts in creation order
    target = paths.BACKUP_DIR / f"{stamp}.toml"
    shutil.copy2(paths.DEVICES_FILE, target)
    for old in list_backups()[MAX_BACKUPS:]:
        old.unlink(missing_ok=True)
    return target


def list_backups() -> list[Path]:
    """Newest first. Files ending .undone.toml are kept for reference but never restored by undo."""
    if not paths.BACKUP_DIR.is_dir():
        return []
    return sorted((p for p in paths.BACKUP_DIR.glob("*.toml") if not p.name.endswith(".undone.toml")),
                  reverse=True)


@contextmanager
def config_change():
    """Snapshot devices.toml + vm units; restore both if anything inside fails."""
    old_text = paths.DEVICES_FILE.read_text(encoding="utf-8") if paths.DEVICES_FILE.exists() else None
    old_units = systemd.snapshot_units()
    _backup_current()
    try:
        yield
    except BaseException:
        try:
            if old_text is None:
                paths.DEVICES_FILE.unlink(missing_ok=True)
            else:
                cfg.atomic_write(paths.DEVICES_FILE, old_text)
            current = systemd.snapshot_units()
            for name in current.keys() - old_units.keys():
                (paths.UNIT_DIR / name).unlink(missing_ok=True)
            for name, text in old_units.items():
                if current.get(name) != text:
                    cfg.atomic_write(paths.UNIT_DIR / name, text)
            systemd.daemon_reload()
        except Exception as exc:  # noqa: BLE001
            raise VmError("A change failed AND restoring the previous config failed",
                          hint=f"Restore manually from {paths.BACKUP_DIR} (newest file), then `vm sync`.",
                          details=str(exc)) from exc
        raise


def sync(config: cfg.Config | None = None) -> cfg.Config:
    """Make Sunshine instance files + systemd units match devices.toml.

    Devices that disappeared from the config are disabled (in safe order)
    before their unit files are removed, so nothing is ever orphaned.
    """
    config = config or cfg.load()
    sunshine_bin = sunshine.find_sunshine(config)
    wanted = systemd.desired_units(config, sunshine_bin)
    for name in systemd.snapshot_units():
        if name.startswith(systemd.SUNSHINE_PREFIX) and name not in wanted:
            device_id = name[len(systemd.SUNSHINE_PREFIX):-len(".service")].replace("_", "-")
            _stop_units(device_id, ignore_errors=True)
            systemd.systemctl("disable", units=(name,), check=False, timeout=20)
    for device in config.devices.values():
        sunshine.write_instance_files(device)
        sunshine.ensure_credentials(device, sunshine_bin)
    changed, removed = systemd.write_units(wanted)
    if changed or removed:
        systemd.daemon_reload()
    return config


def undo() -> Path:
    backups = list_backups()
    if not backups:
        raise VmError("Nothing to undo - no config backups yet")
    newest = backups[0]
    text = newest.read_text(encoding="utf-8")
    import tomllib
    try:
        tomllib.loads(text)
        candidate = cfg.load(newest)
    except (cfg.ConfigError, ValueError) as exc:
        raise VmError(f"The latest backup ({newest.name}) is invalid; not restoring it", details=str(exc))
    with config_change():
        cfg.atomic_write(paths.DEVICES_FILE, text)
        sync(candidate)
    # The snapshot config_change() took is the state we just undid; mark it so
    # a second `vm undo` steps further back instead of redoing.
    latest = list_backups()[0]
    latest.rename(latest.with_name(latest.stem + ".undone.toml"))
    newest.unlink(missing_ok=True)
    return newest


# =========================================================================== status / health

@dataclass
class Health:
    device: cfg.Device
    state: str                    # off | starting | on | stopping | failed
    autostart: bool = False
    connector: str = ""
    screen_unit: str = "inactive"
    sunshine_unit: str = "inactive"
    placed: bool | None = None    # is the screen part of GNOME's layout? (None = unknown)
    stream_port_open: bool | None = None
    web_port_open: bool | None = None
    share_granted: bool | None = None
    paired: list[str] | None = None
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.device
        return {
            "id": d.id, "name": d.name, "state": self.state, "autostart": self.autostart,
            "width": d.width, "height": d.height, "refresh": d.refresh, "scale": d.scale,
            "position": d.position, "alignment": d.alignment, "port": d.port, "web_port": d.web_port,
            "connector": self.connector or None, "screen_unit": self.screen_unit,
            "sunshine_unit": self.sunshine_unit, "placed": self.placed,
            "stream_port_open": self.stream_port_open, "web_port_open": self.web_port_open,
            "share_granted": self.share_granted, "paired_clients": self.paired, "problems": self.problems,
            "managed_instance": d.managed,
        }


def port_open(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _summarise(device: cfg.Device, disp: systemd.UnitState, sun: systemd.UnitState) -> tuple[str, str]:
    if "failed" in (disp.active, sun.active):
        which = "virtual screen" if disp.active == "failed" else "Sunshine"
        return "failed", f"{which} failed"
    if "deactivating" in (disp.active, sun.active):
        return "stopping", ""
    if "activating" in (disp.active, sun.active):
        return "starting", ""
    if disp.active == "active" and sun.active == "active":
        return "on", ""
    if disp.active == "active":
        return "starting", "screen is on but Sunshine isn't running"
    return "off", ""


def health(config: cfg.Config | None = None, deep: bool = False,
           only: str | None = None) -> list[Health]:
    """State of every device. deep=True also probes ports, GNOME layout and pairing."""
    config = config or cfg.load()
    devices = [config.get(only)] if only else list(config.devices.values())
    units = [u for d in devices for u in (systemd.display_unit(d.id), systemd.sunshine_unit(d.id))]
    states = systemd.states(units)
    live = {s.device_id: s for s in runtime.live_screens()}
    layout_connectors: set[str] | None = None
    if deep and any(d.id in live for d in devices):
        try:
            from . import mutter
            state = mutter.get_state()
            layout_connectors = {c for lm in state.logical for c in lm.connectors}
        except Exception:  # noqa: BLE001 - unknown is fine
            layout_connectors = None

    out = []
    for d in devices:
        disp, sun = states[systemd.display_unit(d.id)], states[systemd.sunshine_unit(d.id)]
        state, problem = _summarise(d, disp, sun)
        h = Health(d, state, sun.enabled == "enabled", screen_unit=disp.active, sunshine_unit=sun.active)
        if problem:
            h.problems.append(problem)
        screen = live.get(d.id)
        if screen:
            h.connector = screen.connector
        if deep and state in ("on", "starting", "failed"):
            if screen and layout_connectors is not None:
                h.placed = screen.connector in layout_connectors
                if not h.placed:
                    h.problems.append(f"screen {screen.connector} exists but isn't in GNOME's layout "
                                      "(run `vm enable` again to fix)")
            h.stream_port_open = port_open(d.port)
            h.web_port_open = port_open(d.web_port)
            token = sunshine.portal_token(d)
            if screen:
                try:
                    h.share_granted = token.exists() and token.stat().st_mtime >= screen.started - 1
                except OSError:
                    h.share_granted = None
            if sun.active == "active" and not h.stream_port_open:
                h.problems.append("Sunshine isn't accepting connections yet - if GNOME is showing a "
                                  "'share screen' dialog, pick this device's screen")
            if h.web_port_open:
                h.paired = paired_clients(d)
        out.append(h)
    return out


def paired_clients(device: cfg.Device) -> list[str] | None:
    try:
        clients = sunshine.Api(device).clients()
    except sunshine.SunshineError:
        return None
    return None if clients is None else [str(c.get("name", "?")) for c in clients]


# =========================================================================== enable / disable

def _ports_in_use(device: cfg.Device) -> list[int]:
    busy = []
    for port in (device.port - 5, device.port, device.port + 1, device.port + 21):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("0.0.0.0", port))
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE:
                    busy.append(port)
    return busy


def _session_environment() -> dict[str, str]:
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True,
                             text=True, timeout=8, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def _mutter_check() -> str | None:
    """None if GNOME's display API answers (bounded by a D-Bus timeout)."""
    try:
        from . import mutter
        mutter.get_state()
    except ImportError:
        return ("Python GObject bindings are missing: sudo dnf install python3-gobject "
                "gstreamer1 pipewire-gstreamer")
    except Exception as exc:  # noqa: BLE001
        return f"GNOME's display service (Mutter) isn't answering: {exc}"
    return None


def preflight(device: cfg.Device, config: cfg.Config) -> list[str]:
    """Reasons `enable` must not run. Empty list = safe to proceed. Read-only."""
    reasons: list[str] = []
    for finding in legacy.blockers():
        reasons.append(f"{finding.title}. {finding.detail}\n    Run `vm doctor` for the exact files "
                       f"and a manual migration procedure.")
    if not device.managed and legacy.old_android_active():
        reasons.append(f"{device.name} reuses the old iPad/Android Sunshine config, and that old "
                       f"instance ({paths.OLD_ANDROID_UNIT}) is still running. Two copies of one "
                       "instance must never run. Stop the old one first (see `vm doctor`).")
    sunshine_bin = sunshine.find_sunshine(config)
    if not os.access(sunshine_bin, os.X_OK):
        reasons.append(f"Sunshine isn't installed at {sunshine_bin}. Fedora: "
                       "sudo dnf copr enable lizardbyte/stable && sudo dnf install Sunshine")
    env = _session_environment()
    if env and not env.get("WAYLAND_DISPLAY"):
        reasons.append("The systemd user session has no WAYLAND_DISPLAY - is a GNOME Wayland session "
                       "logged in on this machine?")
    problem = _mutter_check()
    if problem:
        reasons.append(problem)
    live = [s for s in runtime.live_screens() if s.device_id != device.id]
    if len(live) >= config.max_screens:
        reasons.append(f"{len(live)} virtual screens are already on ({', '.join(s.device_id for s in live)}); "
                       f"the limit is max_screens = {config.max_screens}. Disable one first.")
    busy = _ports_in_use(device)
    if busy:
        reasons.append(f"Port(s) {', '.join(map(str, busy))} for {device.name} are already in use by another "
                       f"program. Pick another with `vm edit {device.id} --port <n>` "
                       f"(see what's using it: ss -ltnp | grep {busy[0]}).")
    return reasons


def _stop_units(device_id: str, ignore_errors: bool = False) -> None:
    """Ordered, bounded stop: Sunshine first (releases its capture), then the screen."""
    errors = []
    for unit in (systemd.sunshine_unit(device_id), systemd.display_unit(device_id)):
        try:
            systemd.systemctl("stop", units=(unit,), timeout=30)
        except systemd.SystemdError as exc:
            errors.append(f"{unit}: {exc}")
    if errors and not ignore_errors:
        raise VmError("Could not stop cleanly: " + "; ".join(errors),
                      hint=f"Check `vm logs {device_id}`. systemd force-stops units after 15s.")


@dataclass
class EnableResult:
    device: cfg.Device
    already_on: bool = False
    accepting: bool = False      # Sunshine is listening -> Moonlight can connect


def enable(device_id: str, wait: float = 45, progress=lambda msg: None) -> EnableResult:
    with operation(f"enable {device_id}"):
        config = cfg.load()
        device = config.get(device_id)
        current = health(config, only=device_id)[0]
        if current.state == "on":
            _repair_running(device)
            return EnableResult(device, already_on=True, accepting=port_open(device.port))
        if current.state in ("starting", "stopping"):
            raise Busy(f"{device.name} is {current.state} right now; try again in a few seconds")

        progress("Running safety checks")
        reasons = preflight(device, config)
        if reasons:
            raise Refused(f"Not enabling {device.name}:\n  - " + "\n  - ".join(reasons))

        sync(config)
        units = (systemd.display_unit(device.id), systemd.sunshine_unit(device.id))
        systemd.systemctl("reset-failed", units=units, check=False, timeout=15)
        progress("Creating the virtual screen and starting Sunshine")
        try:
            # Starting Sunshine pulls in its screen (BindsTo). The screen unit is
            # Type=notify, so this returns only once the screen is created and placed.
            systemd.systemctl("start", units=(systemd.sunshine_unit(device.id),), timeout=90)
            st = systemd.states(list(units))
            if any(st[u].active not in ("active", "activating") for u in units):
                raise systemd.SystemdError("a unit stopped right after starting")
        except KeyboardInterrupt:
            progress("Interrupted - rolling back")
            _stop_units(device.id, ignore_errors=True)
            raise
        except systemd.SystemdError as exc:
            logs = recent_logs(device.id, 15)
            progress("Failed - rolling back")
            _stop_units(device.id, ignore_errors=True)
            raise VmError(f"Couldn't enable {device.name}: {exc}",
                          hint=f"Everything it started was stopped again. Full logs: vm logs {device.id}",
                          details=logs) from exc

    result = EnableResult(device)
    if wait > 0:
        progress("Waiting for Sunshine (GNOME may ask which screen to share)")
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if port_open(device.port):
                result.accepting = True
                break
            if health(only=device_id)[0].state in ("off", "failed"):
                raise VmError(f"{device.name} stopped while starting",
                              hint=f"vm logs {device.id}", details=recent_logs(device.id, 15))
            time.sleep(0.5)
    if result.accepting:
        heal_pairings(device)
    return result


def _repair_running(device: cfg.Device) -> None:
    """For an already-on device: re-place its screen if GNOME dropped it, fix duplicate pairings."""
    screen = runtime.live_screen(device.id)
    if screen:
        try:
            from . import mutter
            state = mutter.get_state()
            if screen.connector not in {c for lm in state.logical for c in lm.connectors}:
                with runtime.layout_lock(timeout=20):
                    mutter.relayout()
        except Exception:  # noqa: BLE001 - best effort
            pass
    heal_pairings(device)


def disable(device_id: str) -> bool:
    """Returns False if it was already off."""
    with operation(f"disable {device_id}"):
        config = cfg.load()
        device = config.get(device_id)
        if health(config, only=device_id)[0].state == "off":
            return False
        _stop_units(device.id)
        return True


def disable_all() -> list[str]:
    config = cfg.load()
    stopped = []
    for h in health(config):
        if h.state != "off" and disable(h.device.id):
            stopped.append(h.device.id)
    return stopped


def set_autostart(device_id: str, on: bool) -> None:
    with operation(f"autostart {device_id}"):
        config = sync()
        config.get(device_id)
        systemd.systemctl("enable" if on else "disable", units=(systemd.sunshine_unit(device_id),), timeout=20)


def relayout() -> str:
    from . import mutter
    with operation("relayout"), runtime.layout_lock(timeout=20):
        return mutter.relayout()


# =========================================================================== device profiles

def add_device(device_id: str, width: int, height: int, **fields) -> cfg.Device:
    with operation(f"add {device_id}"), config_change():
        config = cfg.load()
        device = cfg.new_device(config, device_id, width, height, **fields)
        config.devices[device_id] = device
        cfg.save(config)
        sync(config)
        return device


def add_preset(key: str, width: int, height: int, scale: float | None = None, label: str | None = None) -> dict:
    with operation(f"presets add {key}"), config_change():
        config = cfg.load()
        if key in config.presets:
            raise VmError(f"You already have a preset called '{key}'", hint=f"vm presets rm {key} first")
        raw = {"name": label or key, "width": width, "height": height}
        if scale is not None:
            raw["scale"] = scale
        config.presets[key] = cfg.preset_from_dict(key, raw)
        cfg.save(config)
        return config.presets[key]


def remove_preset(key: str) -> None:
    with operation(f"presets rm {key}"), config_change():
        config = cfg.load()
        if key in cfg.PRESETS:
            raise VmError(f"'{key}' is built in and can't be removed")
        if key not in config.presets:
            raise VmError(f"No preset called '{key}'", hint="vm presets lists them")
        del config.presets[key]
        cfg.save(config)


def edit_device(device_id: str, **fields) -> tuple[cfg.Device, bool]:
    """Returns (device, is_on_with_changes) - the caller decides whether to restart."""
    with operation(f"edit {device_id}"), config_change():
        config = cfg.load()
        before = config.get(device_id)
        after = cfg.update_device(config, device_id, **fields)
        config.devices[device_id] = after
        cfg.save(config)
        sync(config)
    on = health(only=device_id)[0].state != "off"
    return after, on and after != before


def remove_device(device_id: str, delete_data: bool = False) -> cfg.Device:
    with operation(f"remove {device_id}"), config_change():
        config = cfg.load()
        device = config.get(device_id)
        _stop_units(device.id)
        systemd.systemctl("disable", units=(systemd.sunshine_unit(device.id),), check=False, timeout=20)
        del config.devices[device_id]
        cfg.save(config)
        sync(config)
    if delete_data:
        if device.managed and device.xdg_config_home.is_dir() \
                and paths.INSTANCES_DIR in device.xdg_config_home.resolve().parents:
            shutil.rmtree(device.xdg_config_home)
        sunshine.forget_credentials(device_id)
    return device


# =========================================================================== pairing

def connect_addresses(device: cfg.Device) -> list[str]:
    return [f"{ip}:{device.port}" for ip in sunshine.lan_addresses()] or [f"<this-pc-ip>:{device.port}"]


def pair(device_id: str, pin: str, client_name: str = "",
         creds: tuple[str, str] | None = None, wait: float = 60) -> None:
    device = cfg.load().get(device_id)
    api = sunshine.Api(device, creds)
    if not port_open(device.web_port):
        raise VmError(f"{device.name}'s Sunshine isn't reachable on port {device.web_port}",
                      hint=f"Is it enabled? `vm status {device_id}`")
    try:
        ok = api.submit_pin(pin, client_name or device.name, wait=wait)
    except sunshine.SunshineError as exc:
        text = str(exc)
        if text in ("NO_CREDENTIALS", "BAD_CREDENTIALS"):
            raise VmError(f"vm doesn't have a working Sunshine login for {device.name}",
                          hint=f"Pair in the browser instead: https://localhost:{device.web_port}") from exc
        raise VmError(text) from exc
    if not ok:
        raise VmError("Pairing failed: wrong PIN, or Moonlight stopped waiting.",
                      hint="In Moonlight, tap the computer again to get a fresh PIN.")
    time.sleep(0.5)  # let Sunshine save the new entry
    heal_pairings(device)
    if creds:
        sunshine.save_credentials(device_id, *creds)


def heal_pairings(device: cfg.Device) -> int:
    """Silently remove duplicate pairings that make Sunshine reject a client. Returns how many."""
    remove, reenable = sunshine.duplicate_pairings(sunshine.stored_clients(device))
    if not remove or not port_open(device.web_port):
        return 0
    api = sunshine.Api(device)
    fixed = 0
    try:
        for uuid in remove:
            api.unpair(uuid)
            fixed += 1
        for uuid in reenable:
            api.set_enabled(uuid, True)
    except sunshine.SunshineError:
        pass  # best effort; `vm unpair` is the manual fallback
    return fixed


def unpair(device_id: str, everything: bool = True) -> int:
    """Forget paired Moonlight clients on this device's own Sunshine. Returns how many."""
    device = cfg.load().get(device_id)
    if not port_open(device.web_port):
        raise VmError(f"{device.name}'s Sunshine isn't running", hint=f"vm enable {device_id}")
    api = sunshine.Api(device)
    try:
        clients = api.clients() or []
        api.unpair_all()
    except sunshine.SunshineError as exc:
        if str(exc) in ("NO_CREDENTIALS", "BAD_CREDENTIALS"):
            raise VmError(f"vm doesn't have a working Sunshine login for {device.name}",
                          hint=f"Use the browser: https://localhost:{device.web_port}") from exc
        raise VmError(str(exc)) from exc
    return len(clients)


# =========================================================================== systemd hooks

def prestart(device_id: str) -> int:
    """ExecStartPre of a device's Sunshine: wait for its screen; drop a stale share grant.

    Only touches this device's own instance directory.
    """
    device = cfg.load().get(device_id)
    deadline = time.monotonic() + 20
    screen = runtime.live_screen(device_id)
    while screen is None and time.monotonic() < deadline:
        time.sleep(0.25)
        screen = runtime.live_screen(device_id)
    if screen is None:
        print(f"vm: the virtual screen for {device.name} isn't ready", flush=True)
        return 1
    token = sunshine.portal_token(device)
    try:
        if token.exists() and token.stat().st_mtime < screen.started:
            # The screen was recreated; the saved GNOME share grant points at a
            # screen that no longer exists and Sunshine would fail to start.
            token.replace(token.with_name("portal_token.old"))
            print("vm: screen was recreated; GNOME will ask which screen to share", flush=True)
    except OSError as exc:
        print(f"vm: could not reset the share grant: {exc}", flush=True)
    return 0


def recent_logs(device_id: str, lines: int = 30) -> str:
    cmd = ["journalctl", "--user", "--no-pager", "-o", "short", "-n", str(lines),
           "-u", systemd.display_unit(device_id), "-u", systemd.sunshine_unit(device_id)]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


# =========================================================================== doctor

@dataclass
class Check:
    level: str          # ok | blocker | warning | info
    title: str
    detail: str = ""
    files: list[str] = field(default_factory=list)
    fix: list[str] = field(default_factory=list)


def _install_hint(fedora: str, ubuntu: str) -> str:
    return ubuntu if shutil.which("apt") and not shutil.which("dnf") else fedora


def doctor() -> list[Check]:
    checks: list[Check] = []
    add = checks.append
    env = _session_environment()
    wayland = bool(env.get("WAYLAND_DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    desktop = env.get("XDG_CURRENT_DESKTOP") or os.environ.get("XDG_CURRENT_DESKTOP", "")
    add(Check("ok" if wayland and "GNOME" in desktop else "blocker",
              f"GNOME Wayland session (desktop={desktop or '?'}, wayland={'yes' if wayland else 'no'})",
              "" if wayland else "vm needs to run inside a logged-in GNOME Wayland session."))

    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        add(Check("ok", "Python GObject + GStreamer bindings"))
        add(Check("ok" if Gst.ElementFactory.find("pipewiresrc") else "blocker", "GStreamer PipeWire plugin",
                  fix=[_install_hint("sudo dnf install pipewire-gstreamer", "sudo apt install gstreamer1.0-pipewire")]))
    except (ImportError, ValueError):
        add(Check("blocker", "Python GObject + GStreamer bindings",
                  fix=[_install_hint("sudo dnf install python3-gobject gstreamer1 pipewire-gstreamer",
                                     "sudo apt install python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-pipewire")]))

    try:
        from . import mutter
        state = mutter.get_state()
        virtual = sorted(state.virtual_connectors())
        add(Check("ok", f"Mutter display API: {len(state.monitors)} screen(s), layout mode "
                        f"{'logical' if state.layout_mode == 1 else 'physical'}",
                  f"virtual now: {', '.join(virtual)}" if virtual else ""))
        if state.layout_mode != 1:
            add(Check("info", "Fractional scaling (e.g. 1.5) is off in GNOME",
                      "Whole-number scales work. For 1.25/1.5/1.75, enable GNOME's fractional scaling.",
                      fix=["gsettings set org.gnome.mutter experimental-features \"['scale-monitor-framebuffer']\"",
                           "# then log out and back in"]))
        ours = {s.connector for s in runtime.live_screens()}
        foreign = [c for c in virtual if c not in ours]
        if foreign:
            add(Check("warning", f"Virtual screens not created by vm: {', '.join(foreign)}",
                      "Another tool (old daemon, remote desktop, screen sharing) owns them. vm leaves them "
                      "where they are."))
    except ImportError:
        add(Check("blocker", "Mutter display API", "can't check without the Python GObject bindings (above)"))
    except Exception as exc:  # noqa: BLE001
        add(Check("blocker", "Mutter display API", f"not reachable: {exc}"))

    portal = systemd.states(["xdg-desktop-portal.service", "xdg-desktop-portal-gnome.service"])
    ok = all(s.active in ("active", "inactive") for s in portal.values())  # D-Bus activated on demand
    add(Check("ok" if ok else "warning", "Screen-share portal (xdg-desktop-portal + -gnome)",
              ", ".join(f"{u}: {s.active}" for u, s in portal.items()),
              fix=[] if ok else ["systemctl --user restart xdg-desktop-portal.service"]))

    try:
        config = cfg.load()
        add(Check("ok", f"Config: {len(config.devices)} device(s) in {paths.DEVICES_FILE}"))
    except cfg.ConfigError as exc:
        add(Check("blocker", "Config", str(exc), [str(paths.DEVICES_FILE)], ["vm undo   # previous version"]))
        config = cfg.Config()
    sunshine_bin = sunshine.find_sunshine(config)
    add(Check("ok" if os.access(sunshine_bin, os.X_OK) else "blocker", f"Sunshine binary: {sunshine_bin}",
              fix=["see README step 2 (Install Sunshine)"]))

    main = systemd.states([paths.MAIN_SUNSHINE_UNIT])[paths.MAIN_SUNSHINE_UNIT]
    add(Check("info", f"Normal (Mac) Sunshine: {main.active} - read-only, vm never controls it"))

    for finding in legacy.detect():
        add(Check(finding.level, finding.title, finding.detail, finding.files, finding.fix))

    expected = systemd.desired_units(config, sunshine_bin)
    actual = systemd.snapshot_units()
    if any(actual.get(name) != text for name, text in expected.items()):
        add(Check("warning", "systemd units are out of date", fix=["vm sync"]))
    for device in config.devices.values():
        if sunshine.credentials(device.id) is None:
            add(Check("warning", f"{device.id}: vm doesn't know its Sunshine login, so `vm pair` can't work",
                      fix=[f"vm remove {device.id} --delete-data   # then add it again"]))
        busy = _ports_in_use(device) if health(config, only=device.id)[0].state == "off" else []
        if busy:
            add(Check("warning", f"{device.id}: port(s) {', '.join(map(str, busy))} already in use",
                      fix=[f"vm edit {device.id} --port <another>"]))
    return checks
