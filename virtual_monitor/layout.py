"""Pure layout planning for Mutter's DisplayConfig API (no GNOME needed; unit-tested).

Rules:
* Screens vm doesn't manage (your real monitors, other tools' virtual
  screens) keep their current position, scale and mode.
* vm screens are placed in the order they were switched on, each next to
  your main screen on its configured side. If that side is taken, it goes
  beyond whatever is already there, so nothing overlaps.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

LAYOUT_LOGICAL = 1
LAYOUT_PHYSICAL = 2


@dataclass(frozen=True)
class Mode:
    id: str
    width: int
    height: int
    refresh: float
    preferred_scale: float
    supported_scales: tuple[float, ...]
    current: bool = False


@dataclass(frozen=True)
class Monitor:
    connector: str
    display_name: str
    modes: tuple[Mode, ...]
    virtual: bool

    @property
    def current_mode(self) -> Mode | None:
        return next((m for m in self.modes if m.current), None)


@dataclass(frozen=True)
class LogicalMonitor:
    x: int
    y: int
    scale: float
    transform: int
    primary: bool
    connectors: tuple[str, ...]


@dataclass(frozen=True)
class DisplayState:
    serial: int
    monitors: dict[str, Monitor]
    logical: tuple[LogicalMonitor, ...]
    layout_mode: int
    supports_changing_layout_mode: bool = False

    def virtual_connectors(self) -> set[str]:
        return {c for c, m in self.monitors.items() if m.virtual}


@dataclass(frozen=True)
class Placement:
    """What a vm device wants."""
    width: int
    height: int
    refresh: float
    scale: float
    position: str = "right"
    alignment: str = "center"


@dataclass
class Planned:
    x: int
    y: int
    width: int   # logical size
    height: int
    scale: float
    transform: int
    primary: bool
    connectors: list[tuple[str, str]] = field(default_factory=list)  # (connector, mode id)

    def as_dbus(self) -> tuple:
        return (self.x, self.y, self.scale, self.transform, self.primary,
                [(c, m, {}) for c, m in self.connectors])


class LayoutError(RuntimeError):
    pass


# --------------------------------------------------------------------------- parsing

def is_virtual(connector: str, display_name: str) -> bool:
    return connector.startswith("Meta-") or display_name == "Virtual remote monitor"


def parse_state(raw: tuple) -> DisplayState:
    """Turn org.gnome.Mutter.DisplayConfig.GetCurrentState() output into dataclasses."""
    serial, raw_monitors, raw_logical, props = raw
    monitors: dict[str, Monitor] = {}
    for spec, modes, mprops in raw_monitors:
        connector = str(spec[0])
        display_name = str(mprops.get("display-name", spec[2] if len(spec) > 2 else ""))
        parsed_modes = tuple(
            Mode(id=str(m[0]), width=int(m[1]), height=int(m[2]), refresh=float(m[3]),
                 preferred_scale=float(m[4]), supported_scales=tuple(float(s) for s in m[5]),
                 current=bool(m[6].get("is-current", False)))
            for m in modes)
        virtual = is_virtual(connector, display_name) or str(spec[2]) == "Virtual remote monitor"
        monitors[connector] = Monitor(connector, display_name, parsed_modes, virtual)
    logical = tuple(
        LogicalMonitor(x=int(lm[0]), y=int(lm[1]), scale=float(lm[2]), transform=int(lm[3]),
                       primary=bool(lm[4]), connectors=tuple(str(s[0]) for s in lm[5]))
        for lm in raw_logical)
    return DisplayState(
        serial=int(serial), monitors=monitors, logical=logical,
        layout_mode=int(props.get("layout-mode", LAYOUT_LOGICAL)),
        supports_changing_layout_mode=bool(props.get("supports-changing-layout-mode", False)))


# --------------------------------------------------------------------------- helpers

def choose_mode(modes: tuple[Mode, ...], width: int, height: int, refresh: float) -> Mode:
    candidates = [m for m in modes if m.width == width and m.height == height]
    if not candidates:
        raise LayoutError(f"No {width}x{height} mode available")
    return min(candidates, key=lambda m: abs(m.refresh - refresh))


def choose_scale(mode: Mode, wanted: float) -> float:
    if not mode.supported_scales:
        return 1.0
    return min(mode.supported_scales, key=lambda s: abs(s - wanted))


def logical_size(width: int, height: int, scale: float, transform: int, layout_mode: int) -> tuple[int, int]:
    if transform % 2 == 1:  # 90 / 270 degrees (and flipped variants)
        width, height = height, width
    if layout_mode == LAYOUT_LOGICAL:
        return math.floor(width / scale + 0.5), math.floor(height / scale + 0.5)
    return width, height


def _align(container_start: int, container_len: int, child_len: int, alignment: str) -> int:
    if alignment == "start":
        return container_start
    if alignment == "end":
        return container_start + container_len - child_len
    return container_start + round((container_len - child_len) / 2)


def _overlap_1d(a_start: int, a_len: int, b_start: int, b_len: int) -> bool:
    return a_start < b_start + b_len and b_start < a_start + a_len


def _overlaps(a: Planned, b: Planned) -> bool:
    return _overlap_1d(a.x, a.width, b.x, b.width) and _overlap_1d(a.y, a.height, b.y, b.height)


def place(anchor: Planned, placed: list[Planned], width: int, height: int,
          position: str, alignment: str) -> tuple[int, int]:
    if position in ("right", "left"):
        y = _align(anchor.y, anchor.height, height, alignment)
        row = [p for p in placed if _overlap_1d(p.y, p.height, y, height)] or [anchor]
        x = max(p.x + p.width for p in row) if position == "right" else min(p.x for p in row) - width
        return x, y
    if position in ("below", "above"):
        x = _align(anchor.x, anchor.width, width, alignment)
        col = [p for p in placed if _overlap_1d(p.x, p.width, x, width)] or [anchor]
        y = max(p.y + p.height for p in col) if position == "below" else min(p.y for p in col) - height
        return x, y
    raise LayoutError(f"Unknown position {position!r}")


# --------------------------------------------------------------------------- planning

def plan(state: DisplayState, managed: list[tuple[str, Placement]]) -> list[Planned]:
    """Full layout: keep unmanaged screens, (re)place vm screens in order.

    `managed` is [(connector, placement)] for every running vm screen, oldest first.
    """
    managed_connectors = {c for c, _ in managed}
    placed: list[Planned] = []
    for lm in state.logical:
        if any(c in managed_connectors for c in lm.connectors):
            continue
        connectors = []
        size = None
        for connector in lm.connectors:
            monitor = state.monitors.get(connector)
            mode = monitor.current_mode if monitor else None
            if mode is None:
                raise LayoutError(f"Screen {connector} has no current mode")
            connectors.append((connector, mode.id))
            size = size or logical_size(mode.width, mode.height, lm.scale, lm.transform, state.layout_mode)
        placed.append(Planned(lm.x, lm.y, size[0], size[1], lm.scale, lm.transform, lm.primary, connectors))
    if not placed:
        raise LayoutError("No physical screen found to place virtual screens next to")
    anchor = next((p for p in placed if p.primary), placed[0])
    if not any(p.primary for p in placed):
        anchor.primary = True

    for connector, want in managed:
        monitor = state.monitors.get(connector)
        if monitor is None:
            raise LayoutError(f"Virtual screen {connector} has disappeared")
        mode = choose_mode(monitor.modes, want.width, want.height, want.refresh)
        scale = choose_scale(mode, want.scale)
        width, height = logical_size(mode.width, mode.height, scale, 0, state.layout_mode)
        x, y = place(anchor, placed, width, height, want.position, want.alignment)
        placed.append(Planned(x, y, width, height, scale, 0, False, [(connector, mode.id)]))

    min_x = min(p.x for p in placed)
    min_y = min(p.y for p in placed)
    for p in placed:
        p.x -= min_x
        p.y -= min_y
    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            if _overlaps(a, b):
                raise LayoutError(f"Planned screens overlap: {a.connectors} / {b.connectors}")
    return placed


def apply_properties(state: DisplayState) -> dict:
    """Extra ApplyMonitorsConfig properties (plain Python; caller wraps in GLib.Variant)."""
    return {"layout-mode": state.layout_mode} if state.supports_changing_layout_mode else {}
