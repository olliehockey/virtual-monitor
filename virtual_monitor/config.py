"""Device profiles (~/.config/virtual-monitor/devices.toml): load, validate, save."""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import paths

ID_RE = re.compile(r"[a-z][a-z0-9-]{0,30}\Z")
POSITIONS = ("right", "left", "above", "below")
ALIGNMENTS = ("start", "center", "end")
FIRST_PORT = 48100
PORT_STEP = 100
# A Sunshine instance on base port P uses ports P-5 .. P+21.
PORT_SPAN_BELOW = 5
PORT_SPAN_ABOVE = 21

# Built-in device sizes: native pixels, landscape. `vm add NAME PRESET`.
def _p(group: str, name: str, width: int, height: int) -> dict:
    return {"group": group, "name": name, "width": width, "height": height}


PRESETS: dict[str, dict] = {
    "iphone-13-mini":     _p("iPhone", "iPhone 12/13 mini", 2340, 1080),
    "iphone-13":          _p("iPhone", "iPhone 12/13/13 Pro/14", 2532, 1170),
    "iphone-13-pro-max":  _p("iPhone", "iPhone 12/13 Pro Max, 14 Plus", 2778, 1284),
    "iphone-15":          _p("iPhone", "iPhone 14 Pro/15/15 Pro/16", 2556, 1179),
    "iphone-15-pro-max":  _p("iPhone", "iPhone 14 Pro Max/15 Plus/15 Pro Max/16 Plus", 2796, 1290),
    "iphone-16-pro":      _p("iPhone", "iPhone 16 Pro/17/17 Pro", 2622, 1206),
    "iphone-16-pro-max":  _p("iPhone", "iPhone 16 Pro Max/17 Pro Max", 2868, 1320),
    "ipad-mini":          _p("iPad", "iPad mini (6th/7th gen)", 2266, 1488),
    "ipad":               _p("iPad", "iPad (10th gen, A16)", 2360, 1640),
    "ipad-air-11":        _p("iPad", "iPad Air 11\" (and Air 4/5)", 2360, 1640),
    "ipad-air-13":        _p("iPad", "iPad Air 13\"", 2732, 2048),
    "ipad-pro-11":        _p("iPad", "iPad Pro 11\" (M4 and later)", 2420, 1668),
    "ipad-pro-11-2018":   _p("iPad", "iPad Pro 11\" (2018-2022)", 2388, 1668),
    "ipad-pro-13":        _p("iPad", "iPad Pro 13\" (M4 and later)", 2752, 2064),
    "ipad-pro-12-9":      _p("iPad", "iPad Pro 12.9\" (2018-2022)", 2732, 2048),
    # MacBooks: the area below the notch, which is what full-screen Moonlight uses.
    "macbook-air":        _p("MacBook", "MacBook Air 13\" (M1 and later)", 2560, 1600),
    "macbook-air-15":     _p("MacBook", "MacBook Air 15\"", 2880, 1800),
    "macbook-pro-14":     _p("MacBook", "MacBook Pro 14\" (M1 Pro and later)", 3024, 1890),
    "macbook-pro-16":     _p("MacBook", "MacBook Pro 16\" (M1 Pro and later)", 3456, 2160),
}


class ConfigError(Exception):
    """A user-facing configuration problem."""


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    width: int
    height: int
    refresh: int = 60
    scale: float = 1.0
    position: str = "right"
    alignment: str = "center"
    port: int = FIRST_PORT
    audio: bool = False
    encoder: str = ""
    # Reuse an existing Sunshine config home (keeps its pairing) instead of a
    # vm-managed one. vm then never rewrites that instance's sunshine.conf.
    sunshine_home: str = ""

    @property
    def managed(self) -> bool:
        return not self.sunshine_home

    @property
    def xdg_config_home(self) -> Path:
        if self.sunshine_home:
            return Path(self.sunshine_home).expanduser()
        return paths.INSTANCES_DIR / self.id

    @property
    def sunshine_dir(self) -> Path:
        return self.xdg_config_home / "sunshine"

    @property
    def web_port(self) -> int:
        return self.port + 1

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"


DEFAULT_MAX_SCREENS = 3


@dataclass
class Config:
    devices: dict[str, Device] = field(default_factory=dict)
    sunshine_bin: str = ""
    max_screens: int = DEFAULT_MAX_SCREENS
    presets: dict[str, dict] = field(default_factory=dict)   # your own, from [presets.NAME]

    def all_presets(self) -> dict[str, dict]:
        return {**PRESETS, **self.presets}

    def get(self, device_id: str) -> Device:
        try:
            return self.devices[device_id]
        except KeyError:
            known = ", ".join(self.devices) or "none yet"
            raise ConfigError(f"No device called '{device_id}'. Known devices: {known}") from None


# --------------------------------------------------------------------------- validation

def _int(raw: dict, key: str, lo: int, hi: int, label: str, default=None) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ConfigError(f"{label}.{key} must be a whole number from {lo} to {hi}")
    return value


def read_external_port(sunshine_dir: Path) -> int | None:
    conf = sunshine_dir / "sunshine.conf"
    try:
        text = conf.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"(?m)^\s*port\s*=\s*(\d+)\s*(?:#.*)?$", text)
    return int(match.group(1)) if match else None


def device_from_dict(device_id: str, raw: dict) -> Device:
    if not ID_RE.fullmatch(device_id):
        raise ConfigError(
            f"Device id '{device_id}' must start with a letter and use only a-z, 0-9 and '-' (max 31)")
    if not isinstance(raw, dict):
        raise ConfigError(f"[devices.{device_id}] must be a table")
    name = str(raw.get("name", device_id)).strip()
    if not 1 <= len(name) <= 60 or not name.isprintable():
        raise ConfigError(f"{device_id}.name must be 1-60 printable characters")
    scale = raw.get("scale", 1.0)
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) \
            or not 0.5 <= scale <= 4.0:
        raise ConfigError(f"{device_id}.scale must be between 0.5 and 4.0")
    position = raw.get("position", "right")
    if position not in POSITIONS:
        raise ConfigError(f"{device_id}.position must be one of: {', '.join(POSITIONS)}")
    alignment = raw.get("alignment", "center")
    if alignment not in ALIGNMENTS:
        raise ConfigError(f"{device_id}.alignment must be one of: {', '.join(ALIGNMENTS)}")
    audio = raw.get("audio", False)
    if not isinstance(audio, bool):
        raise ConfigError(f"{device_id}.audio must be true or false")
    encoder = str(raw.get("encoder", "")).strip()
    if encoder and not re.fullmatch(r"[a-z0-9_]+", encoder):
        raise ConfigError(f"{device_id}.encoder looks invalid (examples: nvenc, vaapi, software)")
    sunshine_home = str(raw.get("sunshine_home", "")).strip()

    port = raw.get("port")
    if sunshine_home:
        external = read_external_port(Path(sunshine_home).expanduser() / "sunshine")
        if external is not None:
            port = external
    if port is None:
        port = FIRST_PORT
    port = _int({"port": port}, "port", 1029, 65514, device_id)

    return Device(
        id=device_id,
        name=name,
        width=_int(raw, "width", 320, 8192, device_id),
        height=_int(raw, "height", 320, 8192, device_id),
        refresh=_int(raw, "refresh", 24, 240, device_id, 60),
        scale=float(scale),
        position=position,
        alignment=alignment,
        port=port,
        audio=audio,
        encoder=encoder,
        sunshine_home=sunshine_home,
    )


def ports_clash(a: int, b: int) -> bool:
    """True if two Sunshine base ports would share any port in their families."""
    lo_a, hi_a = a - PORT_SPAN_BELOW, a + PORT_SPAN_ABOVE
    lo_b, hi_b = b - PORT_SPAN_BELOW, b + PORT_SPAN_ABOVE
    return not (hi_a < lo_b or hi_b < lo_a)


def validate(config: Config) -> None:
    protected = paths.protected_config_homes()
    seen: list[Device] = []
    for device in config.devices.values():
        if device.sunshine_home:
            home = device.xdg_config_home.resolve()
            if home in protected or device.sunshine_dir.resolve() in {p / "sunshine" for p in protected}:
                raise ConfigError(
                    f"{device.id}.sunshine_home points at your normal Sunshine's config. "
                    "vm only manages its own dedicated instances.")
        if ports_clash(device.port, paths.MAIN_SUNSHINE_PORT):
            raise ConfigError(
                f"{device.id}.port {device.port} overlaps your normal Sunshine (47989). Pick another port.")
        for other in seen:
            if ports_clash(device.port, other.port):
                raise ConfigError(
                    f"{device.id} and {other.id} use overlapping ports ({device.port} / {other.port}). "
                    f"Keep base ports at least {PORT_SPAN_BELOW + PORT_SPAN_ABOVE + 1} apart.")
            if device.sunshine_home and device.xdg_config_home == other.xdg_config_home:
                raise ConfigError(f"{device.id} and {other.id} share a Sunshine config folder")
        seen.append(device)


def next_free_port(config: Config) -> int:
    port = FIRST_PORT
    while any(ports_clash(port, d.port) for d in config.devices.values()) \
            or ports_clash(port, paths.MAIN_SUNSHINE_PORT):
        port += PORT_STEP
    if port > 65514:
        raise ConfigError("No free port range left")
    return port


def preset_from_dict(key: str, raw: dict) -> dict:
    if not ID_RE.fullmatch(key):
        raise ConfigError(f"Preset name '{key}' must start with a letter and use only a-z, 0-9 and '-'")
    if key in PRESETS:
        raise ConfigError(f"'{key}' is a built-in preset; pick another name")
    if not isinstance(raw, dict):
        raise ConfigError(f"[presets.{key}] must be a table")
    preset = {"name": str(raw.get("name", key)).strip() or key,
              "width": _int(raw, "width", 320, 8192, f"presets.{key}"),
              "height": _int(raw, "height", 320, 8192, f"presets.{key}")}
    if "scale" in raw:
        scale = raw["scale"]
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not 0.5 <= scale <= 4.0:
            raise ConfigError(f"presets.{key}.scale must be between 0.5 and 4.0")
        preset["scale"] = float(scale)
    return preset


# --------------------------------------------------------------------------- load / save

def load(path: Path | None = None) -> Config:
    path = path or paths.DEVICES_FILE
    if not path.exists():
        return Config()
    try:
        with path.open("rb") as stream:
            root = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Can't read {path}: {exc}") from exc
    settings = root.get("settings", {})
    if not isinstance(settings, dict):
        raise ConfigError("[settings] must be a table")
    raw_devices = root.get("devices", {})
    if not isinstance(raw_devices, dict):
        raise ConfigError("[devices.<id>] tables expected")
    raw_presets = root.get("presets", {})
    if not isinstance(raw_presets, dict):
        raise ConfigError("[presets.<name>] tables expected")
    config = Config(
        devices={key: device_from_dict(key, value) for key, value in raw_devices.items()},
        sunshine_bin=str(settings.get("sunshine", "")).strip(),
        max_screens=_int(settings, "max_screens", 1, 8, "settings", DEFAULT_MAX_SCREENS),
        presets={key: preset_from_dict(key, value) for key, value in raw_presets.items()},
    )
    validate(config)
    return config


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)  # JSON strings are valid TOML basic strings


HEADER = """\
# vm device profiles. Edit with `vm add/edit/remove` or by hand (then run `vm sync`).
# Each device = one virtual GNOME screen + its own dedicated Sunshine instance.
# `vm undo` restores the version before the last change.
#
#   width/height  pixels sent to the device (use its native resolution)
#   refresh       Hz;  scale  GNOME scale (2.0 = "200%")
#   position      right | left | above | below  (relative to your main screen)
#   alignment     start | center | end
#   port          Sunshine base port; Moonlight connects to <this-pc-ip>:<port>
#   audio         stream sound to this device (default false)
#   sunshine_home reuse an existing Sunshine config home (keeps its pairing)
#
# [settings]
#   sunshine      path to the sunshine binary (default: auto-detect)
#   max_screens   refuse to enable more virtual screens than this (default 3)
#
# [presets.NAME]  your own presets (`vm presets add NAME WxH`): name, width, height, scale
"""


def dumps(config: Config) -> str:
    out = [HEADER]
    settings = []
    if config.sunshine_bin:
        settings.append(f"sunshine = {_toml_value(config.sunshine_bin)}")
    if config.max_screens != DEFAULT_MAX_SCREENS:
        settings.append(f"max_screens = {config.max_screens}")
    if settings:
        out.append("[settings]\n" + "\n".join(settings) + "\n")
    for key, preset in config.presets.items():
        lines = [f"[presets.{key}]"] + [f"{k} = {_toml_value(v)}" for k, v in preset.items()]
        out.append("\n".join(lines) + "\n")
    defaults = Device(id="x", name="x", width=1, height=1)
    for device in config.devices.values():
        lines = [f"[devices.{device.id}]"]
        for key in ("name", "width", "height", "refresh", "scale", "position", "alignment", "port"):
            lines.append(f"{key} = {_toml_value(getattr(device, key))}")
        for key in ("audio", "encoder", "sunshine_home"):
            value = getattr(device, key)
            if value != getattr(defaults, key):
                lines.append(f"{key} = {_toml_value(value)}")
        out.append("\n".join(lines) + "\n")
    return "\n".join(out)


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save(config: Config, path: Path | None = None) -> None:
    validate(config)
    text = dumps(config)
    # Round-trip check: never write a file we can't read back identically.
    reparsed = tomllib.loads(text)
    for key, device in config.devices.items():
        if device_from_dict(key, reparsed["devices"][key]) != device:
            raise ConfigError(f"Internal error: {key} did not round-trip; nothing saved")
    for key, preset in config.presets.items():
        if preset_from_dict(key, reparsed["presets"][key]) != preset:
            raise ConfigError(f"Internal error: preset {key} did not round-trip; nothing saved")
    atomic_write(path or paths.DEVICES_FILE, text)


def default_scale(width: int, height: int) -> float:
    """High-density screens (tablets, phones, retina laptops) get 200%, others 100%."""
    return 2.0 if max(width, height) >= 2000 else 1.0


def new_device(config: Config, device_id: str, width: int, height: int, **overrides) -> Device:
    if device_id in config.devices:
        raise ConfigError(f"'{device_id}' already exists")
    base: dict = {"name": device_id, "width": width, "height": height,
                  "scale": default_scale(width, height), "port": next_free_port(config)}
    base.update({k: v for k, v in overrides.items() if v is not None})
    device = device_from_dict(device_id, base)
    validate(replace(config, devices={**config.devices, device_id: device}))
    return device


def update_device(config: Config, device_id: str, **changes) -> Device:
    current = config.get(device_id)
    raw = {k: getattr(current, k) for k in (
        "name", "width", "height", "refresh", "scale", "position", "alignment", "port",
        "audio", "encoder", "sunshine_home")}
    raw.update({k: v for k, v in changes.items() if v is not None})
    device = device_from_dict(device_id, raw)
    trial = replace(config, devices={**config.devices, device_id: device})
    validate(trial)
    return device


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug or not slug[0].isalpha():
        slug = "device-" + slug if slug else "device"
    return slug[:31].rstrip("-")


__all__ = [
    "ALIGNMENTS", "POSITIONS", "PRESETS", "Config", "ConfigError", "Device", "dumps", "load",
    "default_scale", "new_device", "next_free_port", "ports_clash", "replace", "save", "slugify", "update_device",
]
