"""`vm` - command line interface."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap

from . import __version__
from . import config as cfg
from . import core, legacy, paths, sunshine, systemd


# --------------------------------------------------------------------------- output

class Out:
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"

    @classmethod
    def c(cls, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if cls.color else text


def bold(t: str) -> str: return Out.c("1", t)
def dim(t: str) -> str: return Out.c("2", t)
def green(t: str) -> str: return Out.c("32", t)
def red(t: str) -> str: return Out.c("31", t)
def yellow(t: str) -> str: return Out.c("33", t)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - len(re.sub(r"\033\[[0-9;]*m", "", text)))


STATE = {"on": lambda: green("● on"), "off": lambda: dim("○ off"), "starting": lambda: yellow("◐ starting"),
         "stopping": lambda: yellow("◑ stopping"), "failed": lambda: red("✕ failed")}


def ok() -> str:
    return green("✓")


def working(message: str) -> None:
    """Shown before steps that can take a while, so it never looks frozen."""
    if sys.stderr.isatty():
        print(dim(f"{message}…"), file=sys.stderr, flush=True)


def state_label(state: str) -> str:
    return STATE.get(state, lambda: state)()


def yesno(value: bool | None) -> str:
    return dim("?") if value is None else (green("yes") if value else red("no"))


def need_tty(flag: str) -> None:
    if not sys.stdin.isatty():
        raise core.VmError("No terminal to ask for input", hint=f"Pass {flag}.")


def confirm(question: str, flag: str = "--yes") -> bool:
    need_tty(flag)
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _size(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", text)
    if not match:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT, e.g. 2420x1668")
    return int(match.group(1)), int(match.group(2))


def _is_tailscale(ip: str) -> bool:
    parts = ip.split(".")
    return len(parts) == 4 and parts[0] == "100" and 64 <= int(parts[1]) <= 127


def addresses(device: cfg.Device) -> list[tuple[str, str]]:
    """[(label, ip:port)] - local network first, then Tailscale."""
    out = []
    for item in core.connect_addresses(device):
        ip = item.rsplit(":", 1)[0]
        label = "Tailscale" if ip[0].isdigit() and _is_tailscale(ip) else "same Wi-Fi"
        out.append((label, item))
    return sorted(out, key=lambda pair: pair[0] == "Tailscale")


def address(device: cfg.Device) -> str:
    return " or ".join(f"{a} ({label})" for label, a in addresses(device)) if len(addresses(device)) > 1 \
        else addresses(device)[0][1]


# --------------------------------------------------------------------------- commands

def cmd_list(args) -> int:
    rows = core.health(cfg.load())
    if args.json:
        print(json.dumps([h.as_dict() for h in rows], indent=2))
        return 0
    if not rows:
        print(f"No devices. Add one: {bold('vm add ipad 2420x1668')}")
        return 0
    w = max(8, *(len(h.device.id) + 2 for h in rows))
    print(dim(f"{'DEVICE':<{w}}{'STATE':<13}{'SIZE':<12}{'SCALE':<7}{'SIDE':<8}PORT"))
    for h in rows:
        d = h.device
        auto = dim("  autostart") if h.autostart else ""
        print(f"{d.id:<{w}}{pad(state_label(h.state), 13)}{d.resolution:<12}{d.scale:<7g}{d.position:<8}{d.port}{auto}")
    return 0


def cmd_status(args) -> int:
    rows = core.health(cfg.load(), deep=True, only=args.device)
    if args.json:
        print(json.dumps([h.as_dict() for h in rows], indent=2))
        return 0
    if not rows:
        print("No devices.")
    for h in rows:
        d = h.device
        print(f"{bold(d.id)}  {state_label(h.state)}  {d.resolution} @ {d.refresh} Hz, scale {d.scale:g}, {d.position}")
        if h.state != "off":
            paired = ", ".join(h.paired) if h.paired else yesno(None if h.paired is None else False)
            print(f"  layout {yesno(h.placed)}   sunshine {yesno(h.stream_port_open)}   "
                  f"shared {yesno(h.share_granted)}   paired {paired}")
        print(f"  moonlight {address(d)}")
        for problem in h.problems:
            print(f"  {yellow('!')} {problem}")
    for finding in legacy.blockers():
        print(red(f"✕ {finding.title}") + dim(" (vm doctor)"))
    return 0


def _enable(device_id: str, wait: float) -> None:
    working(f"starting {device_id} (up to a minute; pick its screen if GNOME asks)")
    result = core.enable(device_id, wait=wait)
    d = result.device
    if result.already_on:
        print(f"{d.id} is already on")
        return
    print(f"{ok()} {d.id} on  {dim(f'{d.resolution}, {d.position}')}  moonlight {address(d)}")
    if wait and not result.accepting:
        print(yellow(f"  pick the {d.resolution} screen in GNOME's share dialog"))


def _only_device(candidates: list[str], what: str) -> list[str]:
    if len(candidates) == 1:
        return candidates
    if not candidates:
        raise core.VmError(f"No devices {what}", hint="Add one: vm add ipad ipad-pro-11")
    raise core.VmError("Which device?", hint=f"One of: {', '.join(candidates)}")


def cmd_enable(args) -> int:
    devices = args.devices or _only_device(list(cfg.load().devices), "yet")
    for device_id in devices:
        _enable(device_id, 0 if args.no_wait else 45)
        device = cfg.load().get(device_id)
        if args.no_wait or args.no_pair or not sys.stdin.isatty() or not core.port_open(device.web_port):
            continue
        if core.paired_clients(device) == [] and sunshine.credentials(device.id):
            print()
            _pair_flow(device)
    return 0


def cmd_disable(args) -> int:
    if args.all or not args.devices:
        working("stopping everything")
        stopped = core.disable_all()
        print(f"{ok()} off: {', '.join(stopped)}" if stopped else "nothing was on")
        return 0
    for device_id in args.devices:
        cfg.load().get(device_id)
        working(f"stopping {device_id}")
        print(f"{ok()} {device_id} off" if core.disable(device_id) else f"{device_id} was already off")
    return 0


def _pair_flow(device: cfg.Device, pin: str | None = None, again: bool = False) -> None:
    creds = None
    if sunshine.credentials(device.id) is None:
        need_tty("vm login details")
        print(f"Sunshine login for {device.id} (from https://localhost:{device.web_port}):")
        creds = (input("  username: ").strip(), __import__("getpass").getpass("  password: "))
    if not pin:
        print("In Moonlight tap +, enter the address, then tap the new PC:")
        for label, item in addresses(device):
            print(f"  {bold(item):<24} {dim(label)}")
        need_tty("the PIN: vm pair DEVICE 1234")
        pin = input("PIN: ").strip()
    core.pair(device.id, pin, device.name, creds)
    print(f"{ok()} paired. In Moonlight Settings set Resolution → Full, then tap the PC → Desktop.")


def cmd_pair(args) -> int:
    config = cfg.load()
    device = config.get(args.device) if args.device else config.get(_only_device(list(config.devices), "yet")[0])
    if core.health(config, only=device.id)[0].state != "on":
        _enable(device.id, 45)
    if not core.port_open(device.web_port):
        raise core.VmError("Sunshine isn't ready yet",
                           hint=f"Pick the {device.resolution} screen in GNOME's share dialog, then try again.")
    core.heal_pairings(device)
    already = core.paired_clients(device)
    if already and not args.pin:
        print(dim(f"Already paired with {', '.join(already)}. Pairing again is safe; Ctrl-C if Moonlight already connects."))
    _pair_flow(device, args.pin)
    return 0


def cmd_unpair(args) -> int:
    device = cfg.load().get(args.device)
    if not args.yes and not confirm(f"Forget all Moonlight clients paired with {device.id}?"):
        return 1
    count = core.unpair(device.id)
    print(f"{ok()} unpaired {count}. In Moonlight delete this PC, then: vm pair {device.id}")
    return 0


def _fields(args) -> dict:
    fields = {k: getattr(args, k, None) for k in ("scale", "refresh", "position", "port")}
    if getattr(args, "audio", None) is not None:
        fields["audio"] = args.audio
    return fields


def _describe(d: cfg.Device) -> str:
    return f"{d.resolution} @ {d.refresh} Hz, scale {d.scale:g}, {d.position}, port {d.port}"


def _preset_or_size(text: str | None, name: str) -> tuple[int, int, str | None, float | None]:
    presets = cfg.load().all_presets()
    key = (text or name).lower()
    if key in presets:
        preset = presets[key]
        return preset["width"], preset["height"], preset["name"], preset.get("scale")
    if text is None:
        raise core.VmError("Give a size or a preset",
                           hint=f"vm add {name} 2420x1668   or one of: {', '.join(presets)}")
    try:
        width, height = _size(text)
    except argparse.ArgumentTypeError:
        raise core.VmError(f"'{text}' isn't a size or preset",
                           hint=f"Use WIDTHxHEIGHT or one of: {', '.join(presets)}") from None
    return width, height, None, None


def cmd_add(args) -> int:
    device_id = args.device if cfg.ID_RE.fullmatch(args.device) else cfg.slugify(args.device)
    width, height, preset_name, preset_scale = _preset_or_size(args.size, args.device)
    fields = _fields(args)
    if fields.get("scale") is None and preset_scale is not None:
        fields["scale"] = preset_scale
    if device_id != args.device:
        fields["name"] = args.device.strip()
    device = core.add_device(device_id, width, height, **fields)
    print(f"{ok()} added {bold(device.id)}  {dim(_describe(device))}")
    print(dim(f"  next: vm enable {device.id}"))
    return 0


def cmd_edit(args) -> int:
    fields = {k: v for k, v in _fields(args).items() if v is not None}
    if args.size:
        fields["width"], fields["height"], _, preset_scale = _preset_or_size(args.size, args.device)
        if "scale" not in fields and preset_scale is not None:
            fields["scale"] = preset_scale
    if not fields:
        raise core.VmError("Nothing to change", hint=f"e.g. vm edit {args.device} --scale 1.5")
    device, needs_restart = core.edit_device(args.device, **fields)
    print(f"{ok()} {device.id}  {dim(_describe(device))}")
    if needs_restart:
        if args.restart:
            working(f"stopping {device.id}")
            core.disable(device.id)
            _enable(device.id, 45)
        else:
            print(dim(f"  applies next enable (or now: vm edit {device.id} --restart)"))
    return 0


def cmd_remove(args) -> int:
    device = cfg.load().get(args.device)
    what = " and its pairing data" if args.delete_data else ""
    if not args.yes and not confirm(f"Remove {device.id}{what}?"):
        return 1
    working(f"stopping and removing {device.id}")
    core.remove_device(device.id, delete_data=args.delete_data)
    print(f"{ok()} removed {device.id}" + dim("  (vm undo to restore)"))
    return 0


def cmd_undo(args) -> int:
    backups = core.list_backups()
    if args.list:
        print("\n".join(str(b) for b in backups) or "no backups")
        return 0
    if not backups:
        raise core.VmError("Nothing to undo")
    if not args.yes and not confirm("Restore devices from before the last change?"):
        return 1
    core.undo()
    print(f"{ok()} restored")
    return 0


def cmd_presets(args) -> int:
    action = getattr(args, "action", None)
    if action == "add":
        width, height = args.preset_size
        preset = core.add_preset(args.preset, width, height, args.scale, args.label)
        print(f"{ok()} preset {bold(args.preset)}  {dim(f'{width}x{height}')}   use: vm add NAME {args.preset}")
        return 0
    if action in ("rm", "remove"):
        core.remove_preset(args.preset)
        print(f"{ok()} removed preset {args.preset}")
        return 0
    config = cfg.load()
    last = None
    for key, preset in config.all_presets().items():
        group = preset.get("group", "Yours")
        if group != last:
            print(("\n" if last else "") + bold(group))
            last = group
        size = f"{preset['width']}x{preset['height']}"
        print(f"  {key:<20}{size:<11}{dim(preset['name'])}")
    print(dim("\nuse: vm add NAME PRESET  ·  your own: vm presets add NAME WxH"))
    return 0


def cmd_web(args) -> int:
    device = cfg.load().get(args.device)
    url = f"https://localhost:{device.web_port}"
    creds = sunshine.credentials(device.id)
    print(url + (f"   user {creds[0]}  password {creds[1]}" if creds else ""))
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


def cmd_uninstall(args) -> int:
    script = paths.PACKAGE_ROOT / "uninstall.sh"
    if not script.exists():
        raise core.VmError("uninstall.sh not found", hint="Run uninstall.sh from the vm source folder")
    what = "vm, its screens and ALL devices and pairings" if args.purge else "vm (devices and pairings are kept)"
    if not args.yes and not confirm(f"Uninstall {what}?"):
        return 1
    return subprocess.call(["bash", str(script), "--yes", *(["--purge"] if args.purge else [])])


def cmd_autostart(args) -> int:
    core.set_autostart(args.device, args.state == "on")
    print(f"{ok()} {args.device} autostart {args.state}")
    return 0


def cmd_logs(args) -> int:
    cfg.load().get(args.device)
    cmd = ["journalctl", "--user", "--no-pager", "-o", "short-iso", "-n", str(args.lines),
           "-u", systemd.display_unit(args.device), "-u", systemd.sunshine_unit(args.device)]
    if args.follow:
        cmd = [c for c in cmd if c != "--no-pager"] + ["-f"]
    return subprocess.call(cmd)


LEVEL = {"ok": lambda: green("✓"), "blocker": lambda: red("✕"), "warning": lambda: yellow("!"),
         "info": lambda: dim("i")}


def cmd_doctor(args) -> int:
    checks = core.doctor()
    if args.json:
        print(json.dumps([c.__dict__ for c in checks], indent=2))
    else:
        for check in checks:
            if check.level == "info" and not args.verbose:
                continue
            print(f"{LEVEL[check.level]()} {check.title}")
            if check.level == "ok":
                continue
            if check.detail:
                print(textwrap.indent(textwrap.fill(check.detail, 86), "    "))
            for file in check.files:
                print(f"    {dim('file')} {file}")
            for line in check.fix:
                print(f"    {dim('$')} {line}")
        bad = [c for c in checks if c.level == "blocker"]
        print(red(f"\n{len(bad)} problem(s) block vm enable") if bad else green("\nready"))
    return core.EXIT_REFUSED if any(c.level == "blocker" for c in checks) else 0


def cmd_sync(args) -> int:
    with core.operation("sync"), core.config_change():
        core.sync()
    print(f"{ok()} synced")
    return 0


def cmd_relayout(args) -> int:
    print(core.relayout())
    return 0


def cmd_completion(args) -> int:
    from . import completion
    print(completion.SCRIPTS[args.shell])
    return 0


def cmd_complete(args) -> int:
    try:
        config = cfg.load()
        print("\n".join(config.all_presets() if args.what == "presets" else config.devices))
    except Exception:  # noqa: BLE001 - completion must never print errors
        pass
    return 0


def cmd_legacy_report(args) -> int:
    findings = legacy.detect()
    if args.json:
        print(json.dumps([f.__dict__ for f in findings]))
        return 0
    if not findings:
        print(f"{ok()} no older virtual-monitor setup found")
        return 0
    print("Found an older virtual-monitor setup (it will not be changed):")
    for f in findings:
        print(f"{LEVEL[f.level]()} {f.title}")
        for file in f.files:
            print(f"    {dim('file')} {file}")
    return 0


def cmd_uninstall_units(args) -> int:
    try:
        stopped = core.disable_all()
    except cfg.ConfigError:
        stopped = []
    for name in systemd.snapshot_units():
        if name.startswith(systemd.SUNSHINE_PREFIX):
            systemd.systemctl("stop", units=(name,), check=False, timeout=30)
            systemd.systemctl("disable", units=(name,), check=False, timeout=20)
        (paths.UNIT_DIR / name).unlink(missing_ok=True)
    systemd.daemon_reload()
    print(f"{ok()} removed vm's systemd units" + (f" (stopped {', '.join(stopped)})" if stopped else ""))
    return 0


# --------------------------------------------------------------------------- parser

EXIT_CODES = "exit codes: 0 ok · 1 error · 2 usage · 3 refused by a safety check · 4 busy"


def _epilog() -> str:
    try:
        presets = cfg.load().all_presets()
    except cfg.ConfigError:
        presets = cfg.PRESETS
    groups: dict[str, list[str]] = {}
    for key, preset in presets.items():
        groups.setdefault(preset.get("group", "Yours"), []).append(key)
    lines = []
    for group, keys in groups.items():
        wrapped = textwrap.wrap(" ".join(keys), 64, break_on_hyphens=False, break_long_words=False)
        lines.append(f"  {group + ':':<9}{wrapped[0]}")
        lines += [f"  {'':<9}{line}" for line in wrapped[1:]]
    return ("presets (sizes: vm presets · use: vm add ipad ipad-pro-11 · yours: vm presets add NAME WxH):\n"
            + "\n".join(lines) + f"\n\n{EXIT_CODES}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vm", description="Use other devices as extra GNOME screens.",
                                     epilog=_epilog(), formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"vm {__version__}")
    parser.add_argument("--no-color", action="store_true", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def cmd(name, help_text, func, aliases=()):
        p = sub.add_parser(name, help=help_text, description=help_text, aliases=list(aliases))
        p.set_defaults(func=func)
        return p

    def device_options(p):
        p.add_argument("--scale", type=float, help="GNOME scale (default: 2 for ≥2000px, else 1)")
        p.add_argument("--refresh", type=int, metavar="HZ", help="default 60")
        p.add_argument("--position", choices=cfg.POSITIONS, help="side of your main screen (default right)")
        p.add_argument("--port", type=int, help="Moonlight port (default: next free from 48100)")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--audio", dest="audio", action="store_true", default=None, help="stream sound")
        g.add_argument("--no-audio", dest="audio", action="store_false", help=argparse.SUPPRESS)

    p = cmd("list", "show devices", cmd_list, ["ls"])
    p.add_argument("--json", action="store_true")
    p = cmd("status", "show health of screens, Sunshine and pairing", cmd_status)
    p.add_argument("device", nargs="?")
    p.add_argument("--json", action="store_true")

    p = cmd("enable", "turn a device's screen on (and pair it the first time)", cmd_enable, ["on"])
    p.add_argument("devices", nargs="*", metavar="DEVICE")
    p.add_argument("--no-wait", action="store_true", help="don't wait for Sunshine")
    p.add_argument("--no-pair", action="store_true", help="don't offer to pair")
    p = cmd("disable", "turn screens off (all if no device given)", cmd_disable, ["off"])
    p.add_argument("devices", nargs="*", metavar="DEVICE")
    p.add_argument("--all", action="store_true", help=argparse.SUPPRESS)

    p = cmd("pair", "pair Moonlight with a device", cmd_pair)
    p.add_argument("device", nargs="?")
    p.add_argument("pin", nargs="?")
    p = cmd("unpair", "forget all Moonlight clients for a device", cmd_unpair)
    p.add_argument("device")
    p.add_argument("-y", "--yes", action="store_true")

    p = cmd("presets", "list presets, or add/remove your own", cmd_presets)
    pre = p.add_subparsers(dest="action", metavar="[add|rm]")
    q = pre.add_parser("add", help="save your own preset: vm presets add NAME WxH")
    q.add_argument("preset", metavar="NAME")
    q.add_argument("preset_size", type=_size, metavar="WxH")
    q.add_argument("--scale", type=float)
    q.add_argument("--label", help="description shown in the list")
    q = pre.add_parser("rm", aliases=["remove"], help="delete one of your presets")
    q.add_argument("preset", metavar="NAME")
    p = sub.add_parser("add", help="add a device", description="add a device",
                       epilog=_epilog().split("\n\nexit codes")[0] +
                              "\n\nexamples: vm add ipad ipad-pro-11 · vm add iphone-13 · vm add tv 1920x1080",
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    p.set_defaults(func=cmd_add)
    p.add_argument("device", help="name, e.g. ipad")
    p.add_argument("size", nargs="?", metavar="PRESET|WxH", help="preset name or resolution, e.g. 2420x1668")
    device_options(p)
    p = cmd("edit", "change a device", cmd_edit)
    p.add_argument("device")
    p.add_argument("size", metavar="PRESET|WxH", nargs="?")
    device_options(p)
    p.add_argument("--restart", action="store_true", help="apply now if it's on")
    p = cmd("remove", "remove a device", cmd_remove, ["rm"])
    p.add_argument("device")
    p.add_argument("--delete-data", action="store_true", help="also delete its pairing")
    p.add_argument("-y", "--yes", action="store_true")

    p = cmd("web", "open a device's Sunshine page (shows its login)", cmd_web)
    p.add_argument("device")
    p = cmd("logs", "show a device's logs", cmd_logs)
    p.add_argument("device")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("-n", "--lines", type=int, default=60)
    p = cmd("doctor", "check this PC", cmd_doctor)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--json", action="store_true")
    p = cmd("autostart", "turn a device on at login", cmd_autostart)
    p.add_argument("device")
    p.add_argument("state", choices=["on", "off"])
    p = cmd("uninstall", "remove vm from this PC", cmd_uninstall)
    p.add_argument("--purge", action="store_true", help="also delete devices and pairings")
    p.add_argument("-y", "--yes", action="store_true")
    p = cmd("undo", "undo the last device change", cmd_undo)
    p.add_argument("--list", action="store_true")
    p.add_argument("-y", "--yes", action="store_true")

    # Rarely needed / internal: not listed in --help.
    for name, func in (("sync", cmd_sync), ("relayout", cmd_relayout), ("_uninstall-units", cmd_uninstall_units)):
        sub.add_parser(name).set_defaults(func=func)
    p = sub.add_parser("completion")
    p.add_argument("shell", choices=["bash", "zsh", "fish"])
    p.set_defaults(func=cmd_completion)
    p = sub.add_parser("_complete")
    p.add_argument("what", nargs="?")
    p.set_defaults(func=cmd_complete)
    p = sub.add_parser("_legacy-report")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_legacy_report)
    p = sub.add_parser("_display")
    p.add_argument("device")
    p.set_defaults(func=lambda a: __import__("virtual_monitor.display", fromlist=["main"]).main(a.device))
    p = sub.add_parser("_prestart")
    p.add_argument("device")
    p.set_defaults(func=lambda a: core.prestart(a.device))
    return parser


def report_error(exc: Exception) -> None:
    print(red("✕ ") + str(exc), file=sys.stderr)
    if getattr(exc, "hint", ""):
        print(f"  {exc.hint}", file=sys.stderr)
    if getattr(exc, "details", ""):
        print(dim(textwrap.indent(exc.details.strip()[-2000:], "  │ ")), file=sys.stderr)


VISIBLE = ("list", "ls", "status", "enable", "on", "disable", "off", "pair", "unpair", "presets", "add", "edit",
           "remove", "rm", "web", "logs", "doctor", "autostart", "uninstall", "undo")
HIDDEN = ("sync", "relayout", "completion", "_uninstall-units", "_complete", "_legacy-report", "_display",
          "_prestart")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    first = next((a for a in argv if not a.startswith("-")), None)
    if first is not None and first not in VISIBLE + HIDDEN:
        import difflib
        close = difflib.get_close_matches(first, VISIBLE, n=1)
        print(red("✕ ") + f"unknown command '{first}'" + (f" - did you mean {bold(close[0])}?" if close else ""),
              file=sys.stderr)
        print("  vm --help lists commands", file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.no_color:
        Out.color = False
    func = getattr(args, "func", None)
    if func is None:
        args.json = False
        func = cmd_list
    try:
        return int(func(args) or 0)
    except core.VmError as exc:
        report_error(exc)
        return exc.code
    except cfg.ConfigError as exc:
        report_error(core.VmError(str(exc), hint=f"config: {paths.DEVICES_FILE}"))
        return core.EXIT_ERROR
    except (systemd.SystemdError, sunshine.SunshineError) as exc:
        report_error(exc)
        return core.EXIT_ERROR
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
