import unittest

from virtual_monitor.layout import (LAYOUT_LOGICAL, LAYOUT_PHYSICAL, LayoutError, Placement, parse_state,
                                    plan)


def mode(mid, w, h, hz=60.0, scales=(1.0, 1.25, 1.5, 1.75, 2.0), current=False):
    return (mid, w, h, hz, 1.0, list(scales), {"is-current": current} if current else {})


def raw_state(extra_monitors=(), logical_extra=(), layout_mode=LAYOUT_LOGICAL, primary_transform=0):
    monitors = [(("DP-1", "DEL", "U2720Q", "1"), [mode("2560x1440@60", 2560, 1440, current=True)],
                 {"display-name": "Dell 27\""})]
    monitors += list(extra_monitors)
    logical = [(0, 0, 1.0, primary_transform, True, [("DP-1", "DEL", "U2720Q", "1")], {})]
    logical += list(logical_extra)
    return (7, monitors, logical, {"layout-mode": layout_mode, "supports-changing-layout-mode": True})


def virtual(conn, w, h):
    return ((conn, "MetaVendor", "Virtual remote monitor", "0x01"), [mode(f"{w}x{h}@60", w, h)],
            {"display-name": "Virtual remote monitor"})


def rects(planned):
    return {p.connectors[0][0]: (p.x, p.y, p.width, p.height, p.scale) for p in planned}


class LayoutTests(unittest.TestCase):
    def test_right_of_primary_centered(self):
        state = parse_state(raw_state([virtual("Meta-0", 2388, 1668)]))
        out = rects(plan(state, [("Meta-0", Placement(2388, 1668, 60, 2.0, "right"))]))
        self.assertEqual(out["DP-1"], (0, 0, 2560, 1440, 1.0))
        self.assertEqual(out["Meta-0"], (2560, 303, 1194, 834, 2.0))

    def test_left_shifts_everything_but_keeps_relative_positions(self):
        state = parse_state(raw_state([virtual("Meta-0", 1600, 1200)]))
        out = rects(plan(state, [("Meta-0", Placement(1600, 1200, 60, 1.0, "left", "start"))]))
        self.assertEqual(out["Meta-0"][:2], (0, 0))
        self.assertEqual(out["DP-1"][:2], (1600, 0))

    def test_two_on_the_same_side_stack_outward(self):
        state = parse_state(raw_state([virtual("Meta-0", 1600, 1200), virtual("Meta-1", 1920, 1080)]))
        out = rects(plan(state, [("Meta-0", Placement(1600, 1200, 60, 1.0, "right")),
                                 ("Meta-1", Placement(1920, 1080, 60, 1.0, "right"))]))
        self.assertEqual(out["Meta-0"][0], 2560)
        self.assertEqual(out["Meta-1"][0], 2560 + 1600)

    def test_below_and_above(self):
        state = parse_state(raw_state([virtual("Meta-0", 2532, 1170), virtual("Meta-1", 1366, 768)]))
        out = rects(plan(state, [("Meta-0", Placement(2532, 1170, 60, 2.0, "below")),
                                 ("Meta-1", Placement(1366, 768, 60, 1.0, "above"))]))
        self.assertEqual(out["Meta-1"][1], 0)
        self.assertEqual(out["DP-1"][1], 768)
        self.assertEqual(out["Meta-0"][1], 768 + 1440)

    def test_unmanaged_screens_keep_their_place(self):
        hdmi = (("HDMI-1", "SAM", "X", "2"), [mode("1920x1080@60", 1920, 1080, current=True)], {})
        state = parse_state(raw_state([hdmi, virtual("Meta-0", 1600, 1200)],
                                      [(-1920, 0, 1.0, 0, False, [("HDMI-1", "SAM", "X", "2")], {})]))
        out = rects(plan(state, [("Meta-0", Placement(1600, 1200, 60, 1.0, "left"))]))
        # The vm screen goes beyond the existing left monitor; nothing overlaps.
        self.assertEqual(out["Meta-0"][0], 0)
        self.assertEqual(out["HDMI-1"][0], 1600)
        self.assertEqual(out["DP-1"][0], 1600 + 1920)

    def test_nearest_supported_scale_and_physical_mode(self):
        state = parse_state(raw_state([virtual("Meta-0", 2000, 1000)], layout_mode=LAYOUT_PHYSICAL))
        out = rects(plan(state, [("Meta-0", Placement(2000, 1000, 60, 1.9, "right"))]))
        self.assertEqual(out["Meta-0"][2:], (2000, 1000, 2.0))

    def test_rotated_primary_uses_rotated_size(self):
        state = parse_state(raw_state([virtual("Meta-0", 1000, 1000)], primary_transform=1))
        out = rects(plan(state, [("Meta-0", Placement(1000, 1000, 60, 1.0, "right"))]))
        self.assertEqual(out["DP-1"][2:4], (1440, 2560))
        self.assertEqual(out["Meta-0"][0], 1440)

    def test_missing_screen_is_an_error(self):
        state = parse_state(raw_state())
        with self.assertRaises(LayoutError):
            plan(state, [("Meta-9", Placement(1000, 1000, 60, 1.0))])

    def test_dbus_shape(self):
        state = parse_state(raw_state([virtual("Meta-0", 1600, 1200)]))
        planned = plan(state, [("Meta-0", Placement(1600, 1200, 60, 1.0))])
        self.assertEqual(planned[1].as_dbus(), (2560, 120, 1.0, 0, False, [("Meta-0", "1600x1200@60", {})]))


if __name__ == "__main__":
    unittest.main()
