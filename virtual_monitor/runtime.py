"""Runtime state in $XDG_RUNTIME_DIR/virtual-monitor.

* <id>.json        one per live virtual screen (connector, pid, start time)
* ops.lock         held by any `vm` command that changes something
* layout.lock      held while a screen is created/removed or the layout changes
* last-teardown    when a screen was last removed (for the cool-down)
"""
from __future__ import annotations

import fcntl
import json
import os
import socket
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import paths

# Give Mutter a moment between removing one virtual screen and creating another.
TEARDOWN_COOLDOWN = 2.0


class Busy(Exception):
    """Another vm operation holds the lock."""


@dataclass(frozen=True)
class LiveScreen:
    device_id: str
    connector: str
    pid: int
    started: float


def _dir() -> Path:
    paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    return paths.RUNTIME_DIR


def ready_file(device_id: str) -> Path:
    return paths.RUNTIME_DIR / f"{device_id}.json"


def write_ready(device_id: str, connector: str) -> None:
    target = _dir() / f"{device_id}.json"
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps({"connector": connector, "pid": os.getpid(), "started": time.time()}),
                    encoding="utf-8")
    os.replace(temp, target)


def clear_ready(device_id: str) -> None:
    try:
        data = json.loads(ready_file(device_id).read_text(encoding="utf-8"))
        if data.get("pid") not in (None, os.getpid()):
            return  # belongs to another (newer) process
    except (OSError, ValueError):
        pass
    ready_file(device_id).unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def live_screens() -> list[LiveScreen]:
    """Every running vm screen, oldest first. Stale files are removed."""
    screens: list[LiveScreen] = []
    if not paths.RUNTIME_DIR.is_dir():
        return screens
    for file in paths.RUNTIME_DIR.glob("*.json"):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            screen = LiveScreen(file.stem, str(data["connector"]), int(data["pid"]), float(data["started"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if _alive(screen.pid):
            screens.append(screen)
        else:
            file.unlink(missing_ok=True)
    return sorted(screens, key=lambda s: s.started)


def live_screen(device_id: str) -> LiveScreen | None:
    return next((s for s in live_screens() if s.device_id == device_id), None)


# --------------------------------------------------------------------------- locks

@contextmanager
def _flock(name: str, timeout: float, describe: str | None):
    path = _dir() / name
    handle = open(path, "a+")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.seek(0)
                    holder = handle.read().strip() or "another vm process"
                    raise Busy(holder) from None
                time.sleep(0.2)
        if describe is not None:
            handle.seek(0)
            handle.truncate()
            handle.write(describe)
            handle.flush()
        yield
    finally:
        handle.close()


@contextmanager
def operation_lock(description: str, timeout: float = 0):
    """One changing `vm` command at a time (enable/disable/add/edit/...)."""
    who = f"pid {os.getpid()}: vm {description}"
    try:
        with _flock("ops.lock", timeout, who):
            yield
    except Busy as exc:
        raise Busy(f"Another vm command is running ({exc}). Wait for it to finish and try again.") from None


@contextmanager
def layout_lock(timeout: float = 60):
    """Serialise screen creation/removal and layout changes across processes."""
    try:
        with _flock("layout.lock", timeout, None):
            yield
    except Busy:
        raise Busy("Timed out waiting for another virtual screen to finish changing") from None


def mark_teardown() -> None:
    try:
        (_dir() / "last-teardown").write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def wait_for_cooldown() -> None:
    try:
        last = float((paths.RUNTIME_DIR / "last-teardown").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    remaining = last + TEARDOWN_COOLDOWN - time.time()
    if 0 < remaining <= TEARDOWN_COOLDOWN:
        time.sleep(remaining)


def sd_notify(message: str) -> None:
    """Minimal sd_notify(3)."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
    except OSError as exc:
        print(f"sd_notify failed: {exc}", file=sys.stderr)
