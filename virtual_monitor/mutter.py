"""Talk to GNOME's org.gnome.Mutter.DisplayConfig (needs python3-gobject)."""
from __future__ import annotations

from . import config as cfg
from . import runtime
from .layout import DisplayState, LayoutError, Placement, Planned, apply_properties, parse_state, plan

DBUS_TIMEOUT_MS = 10_000


def _gio():
    import gi  # noqa: F401  (imported lazily so the CLI works without GNOME bits)
    from gi.repository import Gio, GLib
    return Gio, GLib


def _proxy():
    Gio, _ = _gio()
    return Gio.DBusProxy.new_for_bus_sync(
        Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
        "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
        "org.gnome.Mutter.DisplayConfig", None)


def get_state() -> DisplayState:
    Gio, _ = _gio()
    raw = _proxy().call_sync("GetCurrentState", None, Gio.DBusCallFlags.NONE, DBUS_TIMEOUT_MS, None).unpack()
    return parse_state(raw)


def apply(state: DisplayState, planned: list[Planned]) -> None:
    """Verify, then apply temporarily (virtual screens shouldn't be saved to monitors.xml)."""
    Gio, GLib = _gio()
    proxy = _proxy()
    props = {k: GLib.Variant("u", v) for k, v in apply_properties(state).items()}
    layout = [p.as_dbus() for p in planned]
    for method in (0, 1):  # 0 = verify, 1 = temporary
        proxy.call_sync(
            "ApplyMonitorsConfig",
            GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})", (state.serial, method, layout, props)),
            Gio.DBusCallFlags.NONE, DBUS_TIMEOUT_MS, None)


def managed_placements(config: cfg.Config, state: DisplayState,
                       extra: tuple[str, str] | None = None) -> list[tuple[str, Placement]]:
    """[(connector, placement)] for live vm screens, oldest first (+ one just created)."""
    entries: list[tuple[str, str]] = [(s.device_id, s.connector) for s in runtime.live_screens()]
    if extra and extra[0] not in {d for d, _ in entries}:
        entries.append(extra)
    out = []
    for device_id, connector in entries:
        device = config.devices.get(device_id)
        if device is None or connector not in state.monitors:
            continue
        out.append((connector, Placement(device.width, device.height, device.refresh, device.scale,
                                         device.position, device.alignment)))
    return out


def relayout(config: cfg.Config | None = None, extra: tuple[str, str] | None = None) -> str:
    """Re-place all running vm screens. Returns a one-line summary."""
    config = config or cfg.load()
    state = get_state()
    placed = plan(state, managed_placements(config, state, extra))
    apply(state, placed)
    return "; ".join(
        f"{'/'.join(c for c, _ in p.connectors)} at {p.x},{p.y} {p.width}x{p.height} scale {p.scale:g}"
        for p in placed)


__all__ = ["LayoutError", "apply", "get_state", "relayout"]
