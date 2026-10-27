"""`python -m virtual_monitor _display <id>`: keep one GNOME virtual screen alive.

Run by systemd (vm-display@<id>.service, Type=notify):

1. Take the layout lock, wait out the teardown cool-down, check the screen limit.
2. Ask Mutter's ScreenCast API for a virtual monitor (RecordVirtual).
3. Consume its PipeWire stream with a tiny GStreamer pipeline so it stays alive.
4. Find the new connector, place it beside the main screen without moving
   anything else (verify first, then apply *temporarily* - never saved to
   monitors.xml), write the ready file, notify systemd.
5. On stop: drop the stream, close the session, record the teardown time and
   close the gap left behind. If GNOME closes the screen on its own, exit 1
   (systemd retries a few times, then gives up - it never loops forever).

Every D-Bus call has a timeout; every wait is bounded.
"""
from __future__ import annotations

import signal
import sys
import time

from . import config as cfg
from . import mutter, runtime
from .layout import LayoutError

DBUS_TIMEOUT_MS = 10_000
EXIT_FAILED = 1
EXIT_CONFIG = 2      # systemd: RestartPreventExitStatus - retrying won't help
EXIT_REFUSED = 3


def log(message: str) -> None:
    print(message, flush=True)


class Refused(Exception):
    pass


class ScreenDaemon:
    def __init__(self, device: cfg.Device, config: cfg.Config):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gio, GLib, Gst
        self.Gio, self.GLib, self.Gst = Gio, GLib, Gst
        Gst.init(None)
        self.device = device
        self.config = config
        self.loop = GLib.MainLoop()
        self.session = None
        self.stream = None
        self.pipeline = None
        self.node_id: int | None = None
        self.connector: str | None = None
        self.exit_code = 0
        self.stopping = False

    # ---------------------------------------------------------------- helpers
    def _call(self, proxy, method: str, args=None):
        return proxy.call_sync(method, args, self.Gio.DBusCallFlags.NONE, DBUS_TIMEOUT_MS, None)

    def _proxy(self, path: str, interface: str):
        return self.Gio.DBusProxy.new_for_bus_sync(
            self.Gio.BusType.SESSION, self.Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
            "org.gnome.Mutter.ScreenCast", path, interface, None)

    def _pump_until(self, condition, timeout: float, what: str) -> None:
        context = self.GLib.MainContext.default()
        deadline = time.monotonic() + timeout
        while not condition():
            if self.exit_code:
                raise RuntimeError(f"failed while waiting for {what}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"timed out after {timeout:g}s waiting for {what}")
            while context.pending():
                context.iteration(False)
            time.sleep(0.05)

    def _pump_for(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        self._pump_until(lambda: time.monotonic() >= deadline, seconds + 1, "a pause")

    def _fail(self, message: str) -> None:
        print(message, file=sys.stderr, flush=True)
        self.exit_code = self.exit_code or EXIT_FAILED
        if self.loop.is_running():
            self.loop.quit()

    # ---------------------------------------------------------------- start
    def start(self) -> None:
        d = self.device
        with runtime.layout_lock(timeout=40):
            others = [s for s in runtime.live_screens() if s.device_id != d.id]
            if len(others) >= self.config.max_screens:
                raise Refused(f"{len(others)} virtual screens are already on (max_screens = "
                              f"{self.config.max_screens}). Disable one first.")
            runtime.wait_for_cooldown()
            state = mutter.get_state()
            before = state.virtual_connectors()

            screencast = self._proxy("/org/gnome/Mutter/ScreenCast", "org.gnome.Mutter.ScreenCast")
            session_path = self._call(screencast, "CreateSession",
                                      self.GLib.Variant("(a{sv})", ({},))).unpack()[0]
            self.session = self._proxy(session_path, "org.gnome.Mutter.ScreenCast.Session")
            self.session.connect("g-signal", self._on_session_signal)
            stream_path = self._call(self.session, "RecordVirtual", self.GLib.Variant("(a{sv})", ({
                "is-platform": self.GLib.Variant("b", True),
                "cursor-mode": self.GLib.Variant("u", 1),  # embedded
            },))).unpack()[0]
            self.stream = self._proxy(stream_path, "org.gnome.Mutter.ScreenCast.Stream")
            self.stream.connect("g-signal", self._on_stream_signal)
            self._call(self.session, "Start")
            log(f"Requested virtual screen {d.width}x{d.height}@{d.refresh} for {d.name}")

            self._pump_until(lambda: self.pipeline is not None, 15, "the PipeWire stream")
            self._pump_until(lambda: self._find_connector(before), 15, "GNOME to add the screen")
            log(f"Screen {self.connector} appeared")

            last_error: Exception | None = None
            summary = ""
            for _attempt in range(12):
                try:
                    summary = mutter.relayout(self.config, extra=(d.id, self.connector))
                    break
                except (LayoutError, self.GLib.Error, RuntimeError) as exc:
                    last_error = exc
                    self._pump_for(0.5)
            else:
                raise RuntimeError(f"could not place the screen: {last_error}")
            runtime.write_ready(d.id, self.connector)
        log(f"Layout applied: {summary}")
        runtime.sd_notify(f"READY=1\nSTATUS={d.width}x{d.height} on {self.connector}")

    def _find_connector(self, before: set[str]) -> bool:
        try:
            state = mutter.get_state()
        except self.GLib.Error:
            return False
        d = self.device
        for connector, monitor in state.monitors.items():
            if connector in before or not monitor.virtual:
                continue
            if any(m.width == d.width and m.height == d.height for m in monitor.modes):
                self.connector = connector
                return True
        return False

    # ---------------------------------------------------------------- signals
    def _on_stream_signal(self, _proxy, _sender, signal_name, parameters) -> None:
        if signal_name != "PipeWireStreamAdded" or self.node_id is not None:
            return
        d = self.device
        self.node_id = parameters.unpack()[0]
        try:
            pipeline = self.Gst.parse_launch(
                f"pipewiresrc path={self.node_id} do-timestamp=true ! "
                f"video/x-raw,width={d.width},height={d.height},max-framerate={d.refresh}/1 ! "
                "queue max-size-buffers=1 leaky=downstream ! fakesink sync=false")
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message::error", self._on_pipeline_error)
            if pipeline.set_state(self.Gst.State.PLAYING) == self.Gst.StateChangeReturn.FAILURE:
                pipeline.set_state(self.Gst.State.NULL)
                raise RuntimeError("GStreamer could not start (is pipewire-gstreamer installed?)")
            self.pipeline = pipeline
            log(f"PipeWire node {self.node_id} connected")
        except (self.GLib.Error, RuntimeError) as exc:
            self._fail(f"Could not consume the virtual screen stream: {exc}")

    def _on_pipeline_error(self, _bus, message) -> None:
        if self.stopping:
            return
        error, debug = message.parse_error()
        self._fail(f"Virtual screen stream failed: {error.message} ({debug})")

    def _on_session_signal(self, _proxy, _sender, signal_name, _parameters) -> None:
        if signal_name == "Closed" and not self.stopping:
            self._fail("GNOME closed the virtual screen (was 'Stop Sharing' clicked?).")

    # ---------------------------------------------------------------- run / stop
    def run(self) -> int:
        self.GLib.unix_signal_add(self.GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._quit)
        self.GLib.unix_signal_add(self.GLib.PRIORITY_DEFAULT, signal.SIGINT, self._quit)
        try:
            self.start()
        except Refused as exc:
            print(f"Refused: {exc}", file=sys.stderr, flush=True)
            self.exit_code = EXIT_REFUSED
        except Exception as exc:  # noqa: BLE001 - everything goes to the journal
            self._fail(f"Could not create the virtual screen: {exc}")
        if not self.exit_code:
            self.loop.run()
        self.stop()
        return self.exit_code

    def _quit(self) -> bool:
        self.stopping = True
        runtime.sd_notify("STOPPING=1")
        self.loop.quit()
        return self.GLib.SOURCE_REMOVE

    def stop(self) -> None:
        """Always safe to call; releases everything this process created."""
        self.stopping = True
        had_screen = self.connector is not None
        runtime.clear_ready(self.device.id)
        if self.pipeline is not None:
            try:
                self.pipeline.set_state(self.Gst.State.NULL)
            finally:
                self.pipeline = None
        if self.session is not None:
            try:
                self.session.call_sync("Stop", None, self.Gio.DBusCallFlags.NONE, 3000, None)
            except self.GLib.Error:
                pass  # already gone; Mutter also drops it when this process exits
            self.session = None
            runtime.mark_teardown()
        if had_screen:
            self._tidy_remaining_screens()

    def _tidy_remaining_screens(self) -> None:
        """Close the gap this screen leaves so the other vm screens stay put."""
        try:
            self._pump_for(1.0)  # let Mutter drop our monitor first
            with runtime.layout_lock(timeout=8):
                if runtime.live_screens():
                    log(f"Tidied layout: {mutter.relayout(cfg.load())}")
        except Exception as exc:  # noqa: BLE001 - best effort during shutdown
            print(f"Note: could not tidy the remaining screens: {exc}", file=sys.stderr, flush=True)


def main(device_id: str) -> int:
    try:
        config = cfg.load()
        device = config.get(device_id)
    except cfg.ConfigError as exc:
        print(f"vm: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        daemon = ScreenDaemon(device, config)
    except (ImportError, ValueError) as exc:
        print(f"vm: missing GNOME Python bindings ({exc}). On Fedora: "
              "sudo dnf install python3-gobject gstreamer1 pipewire-gstreamer", file=sys.stderr)
        return EXIT_CONFIG
    return daemon.run()
