"""Detect the previous virtual-monitor setups. Strictly read-only.

Nothing here starts, stops, enables, edits or deletes anything. It only reads
unit files, `systemctl is-active/is-enabled` and /proc, then reports.

Shared by `install.sh` (before it changes anything), `vm doctor` and the
`vm enable` safety check.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

BLOCKER = "blocker"   # vm enable refuses while this is true
WARNING = "warning"   # will cause trouble later / needs a manual decision
INFO = "info"         # harmless leftovers worth knowing about

OLD_DAEMON_MODULE = "gnome_virtual_monitors"
MIGRATION_DOC = "docs/MIGRATION.md"


@dataclass
class Finding:
    level: str
    title: str
    detail: str = ""
    files: list[str] = field(default_factory=list)
    fix: list[str] = field(default_factory=list)   # commands for the user to run *manually*


def _systemctl_read(verb: str, unit: str) -> str:
    """Read-only systemctl query (is-active / is-enabled)."""
    assert verb in ("is-active", "is-enabled")
    try:
        result = subprocess.run(["systemctl", "--user", verb, unit],
                                capture_output=True, text=True, timeout=8, check=False)
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def unit_search_dirs() -> list[Path]:
    dirs = [paths.UNIT_DIR]
    for extra in ("/etc/systemd/user", "/usr/lib/systemd/user", "/usr/local/lib/systemd/user"):
        dirs.append(Path(extra))
    return dirs


def old_daemon_processes() -> list[int]:
    """PIDs of the upstream daemon running in this user's session (even outside systemd)."""
    pids = []
    uid = os.getuid()
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            if proc.stat().st_uid != uid:
                continue
            cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if f"-m {OLD_DAEMON_MODULE}" in cmdline or f"{OLD_DAEMON_MODULE}/__main__" in cmdline:
            if "--check-config" not in cmdline:
                pids.append(int(proc.name))
    return pids


def dropins_referencing_old_daemon() -> list[Path]:
    """Drop-ins/units (other than the old daemon itself) that pull in or wait for it."""
    hits = []
    pattern = re.compile(r"gnome-virtual-monitor(\.service|\.ready)")
    for base in unit_search_dirs():
        if not base.is_dir():
            continue
        for file in sorted(base.glob("*.service.d/*.conf")) + sorted(base.glob("*.service")):
            # The old daemon and its own iPad Sunshine are reported separately as part of
            # the old setup; this check is for *other* units that depend on it.
            if file.name in (paths.OLD_DAEMON_UNIT, paths.OLD_ANDROID_UNIT):
                continue
            try:
                if pattern.search(file.read_text(encoding="utf-8", errors="replace")):
                    hits.append(file)
            except OSError:
                continue
    return hits


def find_unit_file(unit: str) -> Path | None:
    for base in unit_search_dirs():
        candidate = base / unit
        if candidate.exists():
            return candidate
    return None


def detect() -> list[Finding]:
    findings: list[Finding] = []

    # 1. The upstream single-screen daemon.
    daemon_state = _systemctl_read("is-active", paths.OLD_DAEMON_UNIT)
    daemon_enabled = _systemctl_read("is-enabled", paths.OLD_DAEMON_UNIT)
    daemon_file = find_unit_file(paths.OLD_DAEMON_UNIT)
    pids = old_daemon_processes()
    if daemon_state in ("active", "activating", "reloading") or pids:
        findings.append(Finding(
            BLOCKER, "The old gnome-virtual-monitor daemon is running",
            "It rewrites GNOME's whole display layout and would fight vm over virtual screens. "
            "`vm enable` refuses to run until it is stopped.",
            [str(daemon_file)] if daemon_file else [],
            [f"# see {MIGRATION_DOC} first - your Mac Sunshine may depend on it",
             f"systemctl --user stop {paths.OLD_DAEMON_UNIT}"]
            + ([f"# also running outside systemd: pid {', '.join(map(str, pids))}"] if pids else [])))
    elif daemon_enabled in ("enabled", "enabled-runtime", "static", "linked"):
        findings.append(Finding(
            WARNING, "The old gnome-virtual-monitor daemon starts at login",
            f"It is {daemon_enabled}, so it will start again next login and block `vm enable`.",
            [str(daemon_file)] if daemon_file else [],
            [f"# see {MIGRATION_DOC}", f"systemctl --user disable {paths.OLD_DAEMON_UNIT}"]))
    elif daemon_file:
        findings.append(Finding(INFO, "Old gnome-virtual-monitor unit file is installed (inactive)",
                                files=[str(daemon_file)]))

    # 2. Drop-ins (notably on the normal/Mac Sunshine) that depend on the old daemon.
    for dropin in dropins_referencing_old_daemon():
        owner = dropin.parent.name.removesuffix(".d") if dropin.parent.name.endswith(".d") else dropin.name
        on_main = owner == paths.MAIN_SUNSHINE_UNIT
        findings.append(Finding(
            WARNING,
            f"{'Your normal (Mac) Sunshine' if on_main else owner} depends on the old daemon",
            "This file makes it start/wait for gnome-virtual-monitor.service. "
            + ("vm never edits your normal Sunshine or its drop-ins. If you retire the old daemon, "
               "this Sunshine would wait ~20s for it and then fail to start - so move this file "
               "yourself first, at a time a Sunshine restart is OK." if on_main else
               "vm will not change it."),
            [str(dropin)],
            [f"# manual, see {MIGRATION_DOC}:",
             f"mkdir -p ~/virtual-monitor-legacy && mv '{dropin}' ~/virtual-monitor-legacy/",
             "systemctl --user daemon-reload"]))

    # 3. The old dedicated "Android" Sunshine (the iPad one).
    android_state = _systemctl_read("is-active", paths.OLD_ANDROID_UNIT)
    android_file = find_unit_file(paths.OLD_ANDROID_UNIT)
    if android_state in ("active", "activating"):
        findings.append(Finding(
            WARNING, "The old iPad/Android Sunshine instance is running",
            "It is bound to the old daemon and is not used by vm.",
            [str(android_file)] if android_file else [],
            [f"systemctl --user stop {paths.OLD_ANDROID_UNIT}"]))
    elif android_file:
        findings.append(Finding(INFO, "Old iPad/Android Sunshine unit is installed (inactive)",
                                files=[str(android_file)]))

    # 4. The previous CLI (v1.x) and its shell function.
    old_cli = paths.HOME / "scripts" / "virtual-monitor"
    wrapper = paths.HOME / ".bashrc.d" / "40-virtual-monitor.sh"
    present = [str(p) for p in (old_cli, wrapper) if p.exists()]
    if present:
        findings.append(Finding(
            INFO, "Previous virtual-monitor CLI (v1) is installed",
            "Its `virtual-monitor enable --apply` restarts the old daemon. It is not used by vm.",
            present,
            [f"bash {old_cli}/uninstall.sh   # removes only its shell wrapper"]
            if (old_cli / "uninstall.sh").exists() else []))

    upstream_lib = paths.HOME / ".local" / "lib" / "gnome-wayland-virtual-monitors-sunshine"
    if upstream_lib.exists():
        findings.append(Finding(INFO, "Upstream daemon code is installed", files=[str(upstream_lib)]))
    return findings


def blockers(findings: list[Finding] | None = None) -> list[Finding]:
    return [f for f in (findings if findings is not None else detect()) if f.level == BLOCKER]


def old_android_active() -> bool:
    return _systemctl_read("is-active", paths.OLD_ANDROID_UNIT) in ("active", "activating")
