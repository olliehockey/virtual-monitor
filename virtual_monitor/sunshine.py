"""Per-device Sunshine instances: config files, Web UI credentials, pairing API."""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .config import Config, Device, atomic_write


class SunshineError(Exception):
    pass


# --------------------------------------------------------------------------- binary

def find_sunshine(config: Config) -> str:
    if config.sunshine_bin:
        return config.sunshine_bin
    for candidate in ("/usr/bin/sunshine", "/usr/local/bin/sunshine", shutil.which("sunshine") or ""):
        if candidate and os.access(candidate, os.X_OK):
            return os.path.realpath(candidate) if candidate.startswith("/usr/local") else candidate
    return "/usr/bin/sunshine"


# --------------------------------------------------------------------------- config files

def managed_settings(device: Device) -> dict[str, str]:
    host = socket.gethostname().split(".")[0] or "linux"
    settings = {
        "sunshine_name": f"{host} - {device.name}",
        "port": str(device.port),
        "capture": "portal",
        "origin_web_ui_allowed": "pc",
        "upnp": "disabled",
        "system_tray": "disabled",
        "stream_audio": "enabled" if device.audio else "disabled",
    }
    if device.encoder:
        settings["encoder"] = device.encoder
    return settings


def merge_conf(existing: str, wanted: dict[str, str]) -> str:
    """Set vm's keys in a sunshine.conf, keeping any other lines the user added."""
    out: list[str] = []
    done: set[str] = set()
    for line in existing.splitlines():
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        key = match.group(1) if match else None
        if key in wanted:
            if key not in done:
                out.append(f"{key} = {wanted[key]}")
                done.add(key)
            continue
        if key == "encoder" and "encoder" not in wanted:
            continue  # encoder was removed from vm config -> back to auto
        out.append(line)
    if not existing.strip():
        out = ["# Written by vm (virtual-monitor). Lines you add are kept; vm only manages its own keys."]
    for key, value in wanted.items():
        if key not in done:
            out.append(f"{key} = {value}")
    return "\n".join(out).rstrip("\n") + "\n"


APPS_JSON = {
    "env": {"PATH": "$(PATH):$(HOME)/.local/bin"},
    "apps": [{"name": "Desktop", "image-path": "desktop.png"}],
}


def write_instance_files(device: Device) -> None:
    """Create/refresh sunshine.conf and apps.json for a vm-managed instance."""
    if not device.managed:
        return  # reused instance: never rewrite the user's own config
    directory = device.sunshine_dir
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(device.xdg_config_home, 0o700)
    os.chmod(directory, 0o700)
    conf = directory / "sunshine.conf"
    try:
        existing = conf.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    updated = merge_conf(existing, managed_settings(device))
    if updated != existing:
        atomic_write(conf, updated, 0o600)
    apps = directory / "apps.json"
    if not apps.exists():
        atomic_write(apps, json.dumps(APPS_JSON, indent=2) + "\n", 0o600)


# --------------------------------------------------------------------------- credentials

def load_credentials() -> dict[str, dict[str, str]]:
    try:
        data = json.loads(paths.CREDENTIALS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_credentials(device_id: str, username: str, password: str) -> None:
    data = load_credentials()
    data[device_id] = {"username": username, "password": password}
    atomic_write(paths.CREDENTIALS_FILE, json.dumps(data, indent=2) + "\n", 0o600)


def forget_credentials(device_id: str) -> None:
    data = load_credentials()
    if data.pop(device_id, None) is not None:
        atomic_write(paths.CREDENTIALS_FILE, json.dumps(data, indent=2) + "\n", 0o600)


def credentials(device_id: str) -> tuple[str, str] | None:
    entry = load_credentials().get(device_id)
    if isinstance(entry, dict) and entry.get("username") and entry.get("password"):
        return entry["username"], entry["password"]
    return None


def ensure_credentials(device: Device, sunshine_bin: str) -> tuple[str, str] | None:
    """Give a new vm-managed instance a Web UI login so pairing can be automatic."""
    existing = credentials(device.id)
    if existing or not device.managed:
        return existing
    if not os.access(sunshine_bin, os.X_OK):
        return None
    username, password = "vm", secrets.token_urlsafe(18)
    env = {**os.environ, "XDG_CONFIG_HOME": str(device.xdg_config_home), "SUNSHINE_MIGRATE_CONFIG": "0"}
    try:
        result = subprocess.run([sunshine_bin, "--creds", username, password], env=env,
                                capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    save_credentials(device.id, username, password)
    return username, password


# --------------------------------------------------------------------------- Web API

@dataclass
class ApiResult:
    status: int
    data: object


class Api:
    """Tiny client for one instance's local Web API (https://localhost:<port+1>)."""

    def __init__(self, device: Device, creds: tuple[str, str] | None = None, host: str = "localhost"):
        self.device = device
        self.creds = creds or credentials(device.id)
        self.base = f"https://{host}:{device.web_port}"
        self._ssl = ssl.create_default_context()
        self._ssl.check_hostname = False
        self._ssl.verify_mode = ssl.CERT_NONE  # Sunshine uses a self-signed cert on localhost

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 10) -> ApiResult:
        if not self.creds:
            raise SunshineError("NO_CREDENTIALS")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        token = base64.b64encode(f"{self.creds[0]}:{self.creds[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ssl) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        except (urllib.error.URLError, OSError) as exc:
            raise SunshineError(f"Can't reach Sunshine on port {self.device.web_port}: "
                                f"{getattr(exc, 'reason', exc)}") from exc
        try:
            payload = json.loads(raw.decode() or "null")
        except ValueError:
            payload = raw.decode(errors="replace")
        if status == 401:
            raise SunshineError("BAD_CREDENTIALS")
        return ApiResult(status, payload)

    def reachable(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.device.web_port), timeout=1):
                return True
        except OSError:
            return False

    def wait_until_up(self, timeout: float = 30) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.reachable():
                return True
            time.sleep(0.5)
        return False

    def clients(self) -> list[dict] | None:
        """Paired Moonlight clients, or None if this Sunshine version can't say."""
        result = self.request("GET", "/api/clients/list")
        if result.status != 200 or not isinstance(result.data, dict):
            return None
        named = result.data.get("named_certs")
        return named if isinstance(named, list) else None

    def unpair_all(self) -> None:
        result = self.request("POST", "/api/clients/unpair-all", {})
        if result.status != 200:
            raise SunshineError(f"Sunshine refused to unpair ({result.status}): {result.data}")

    def unpair(self, uuid: str) -> None:
        result = self.request("POST", "/api/clients/unpair", {"uuid": uuid})
        if result.status != 200:
            raise SunshineError(f"Sunshine refused to unpair ({result.status}): {result.data}")

    def set_enabled(self, uuid: str, enabled: bool = True) -> None:
        result = self.request("POST", "/api/clients/update", {"uuid": uuid, "enabled": enabled})
        if result.status != 200:
            raise SunshineError(f"Sunshine refused the update ({result.status}): {result.data}")

    def pending_pairings(self) -> list[dict] | None:
        """New Sunshine (2026.10+) lists waiting Moonlight requests; older returns None."""
        result = self.request("GET", "/api/pin")
        if result.status != 200 or not isinstance(result.data, dict) or "pairings" not in result.data:
            return None
        return [p for p in result.data["pairings"] if isinstance(p, dict)]

    def submit_pin(self, pin: str, client_name: str, wait: float = 60) -> bool:
        if not re.fullmatch(r"\d{4}", pin):
            raise SunshineError("The PIN is the 4 digits Moonlight shows")
        client_name = client_name.strip()[:128] or self.device.name
        pending = self.pending_pairings()
        if pending is None:  # older Sunshine: PIN applies to the request in progress
            result = self.request("POST", "/api/pin", {"pin": pin, "name": client_name}, timeout=wait + 30)
        else:
            deadline = time.monotonic() + wait
            while not pending and time.monotonic() < deadline:
                time.sleep(1)
                pending = self.pending_pairings() or []
            if not pending:
                raise SunshineError("Moonlight isn't waiting to pair. Tap the computer in Moonlight "
                                    "first, then enter the PIN it shows.")
            pairing = pending[-1]  # newest request
            result = self.request("POST", "/api/pin", {
                "pairing_id": pairing.get("id", ""), "pin": pin, "name": client_name}, timeout=wait + 30)
        if result.status != 200:
            detail = result.data.get("error") if isinstance(result.data, dict) else result.data
            raise SunshineError(f"Sunshine rejected the request ({result.status}): {detail}")
        return bool(isinstance(result.data, dict) and result.data.get("status") in (True, "true"))


# --------------------------------------------------------------------------- network

def lan_addresses() -> list[str]:
    """IPv4 addresses Moonlight can use, best guess first."""
    found: list[str] = []
    try:  # address of the default route (no packets are sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=5, check=False).stdout
        for match in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/", out):
            if match.group(1) not in found:
                found.append(match.group(1))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return [ip for ip in found if not ip.startswith("127.")]


def portal_token(device: Device) -> Path:
    return device.sunshine_dir / "portal_token"


def stored_clients(device: Device) -> list[dict]:
    """Paired clients as Sunshine saved them (name, cert, uuid, enabled). Read-only."""
    try:
        data = json.loads((device.sunshine_dir / "sunshine_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    clients = (data.get("root") or {}).get("named_devices") or []
    return [c for c in clients if isinstance(c, dict) and c.get("uuid") and c.get("cert")]


def duplicate_pairings(clients: list[dict]) -> tuple[list[str], list[str]]:
    """(uuids to remove, uuids to re-enable).

    Sunshine only admits a client whose certificate matches exactly ONE enabled
    entry, so pairing the same Moonlight twice locks it out ("client certificate
    identity is not enabled"). Keep the newest entry per certificate.
    """
    newest: dict[str, dict] = {}
    remove: list[str] = []
    for client in clients:  # stored oldest first
        key = "".join(str(client["cert"]).split())
        if key in newest:
            remove.append(newest[key]["uuid"])
        newest[key] = client
    reenable = [c["uuid"] for c in newest.values()
                if c["uuid"] not in remove and str(c.get("enabled", True)).lower() == "false"] if remove else []
    return remove, reenable
