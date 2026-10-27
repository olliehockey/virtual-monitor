"""A fake org.gnome.Mutter.DisplayConfig on the session bus (for tests only).

Exposes the real method signatures, so GDBus rejects any call vm makes with a
wrong type signature. Writes every applied layout to $FAKE_MUTTER_LOG.
"""
import json
import os
import sys

from gi.repository import Gio, GLib

XML = """
<node>
  <interface name="org.gnome.Mutter.DisplayConfig">
    <method name="GetCurrentState">
      <arg name="serial" direction="out" type="u"/>
      <arg name="monitors" direction="out" type="a((ssss)a(siiddada{sv})a{sv})"/>
      <arg name="logical_monitors" direction="out" type="a(iiduba(ssss)a{sv})"/>
      <arg name="properties" direction="out" type="a{sv}"/>
    </method>
    <method name="ApplyMonitorsConfig">
      <arg name="serial" direction="in" type="u"/>
      <arg name="method" direction="in" type="u"/>
      <arg name="logical_monitors" direction="in" type="a(iiduba(ssa{sv}))"/>
      <arg name="properties" direction="in" type="a{sv}"/>
    </method>
  </interface>
</node>
"""

SERIAL = 42
SCALES = [1.0, 1.25, 1.5, 1.75, 2.0]


def mode(mid, w, h, current=False):
    props = {"is-current": GLib.Variant("b", True)} if current else {}
    return (mid, w, h, 60.0, 1.0, SCALES, props)


def state():
    monitors = [
        (("DP-1", "DEL", "U2720Q", "1"), [mode("2560x1440@60", 2560, 1440, True)],
         {"display-name": GLib.Variant("s", "Dell 27\"")}),
        (("Meta-0", "MetaVendor", "Virtual remote monitor", "0x000001"), [mode("2388x1668@60", 2388, 1668, True)],
         {"display-name": GLib.Variant("s", "Virtual remote monitor")}),
    ]
    logical = [(0, 0, 1.0, 0, True, [("DP-1", "DEL", "U2720Q", "1")], {})]
    props = {"layout-mode": GLib.Variant("u", 1), "supports-changing-layout-mode": GLib.Variant("b", True)}
    return GLib.Variant("(ua((ssss)a(siiddada{sv})a{sv})a(iiduba(ssss)a{sv})a{sv})",
                        (SERIAL, monitors, logical, props))


def on_call(conn, sender, path, iface, method, params, invocation):
    if method == "GetCurrentState":
        invocation.return_value(state())
    elif method == "ApplyMonitorsConfig":
        serial, how, layout, props = params.unpack()
        if serial != SERIAL:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied", "stale serial")
            return
        with open(os.environ["FAKE_MUTTER_LOG"], "a") as log:
            log.write(json.dumps({"method": how, "layout": layout, "props": props}) + "\n")
        invocation.return_value(None)


def main():
    node = Gio.DBusNodeInfo.new_for_xml(XML)
    loop = GLib.MainLoop()

    def acquired(conn, name):
        conn.register_object("/org/gnome/Mutter/DisplayConfig", node.interfaces[0], on_call, None, None)
        print("READY", flush=True)

    Gio.bus_own_name(Gio.BusType.SESSION, "org.gnome.Mutter.DisplayConfig", Gio.BusNameOwnerFlags.NONE,
                     acquired, None, lambda *a: sys.exit(1))
    loop.run()


if __name__ == "__main__":
    main()
