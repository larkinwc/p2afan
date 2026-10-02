"""Pure-logic regression tests: no P2A bridge, no ipmitool, no hardware.

These cover the paths where a silent bug under-cools eight GPUs: curve
interpolation, hysteresis, percent->duty-byte conversion, the duty
read-modify-write that must not disturb the sibling channel, and the
"no reading" sensor decode that must never look like 0 degrees C.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from p2afan import ipmi, pwm  # noqa: E402
from p2afan.control import slew, writable_channels  # noqa: E402
from p2afan.curve import Curve, Zone  # noqa: E402
from p2afan.sources import build  # noqa: E402


class TestCurve(unittest.TestCase):
    def curve(self, hysteresis_c=0.0):
        return Curve([(50, 30), (65, 50), (78, 80), (85, 100)], hysteresis_c)

    def test_clamps_below_and_above(self):
        c = self.curve()
        self.assertEqual(c.raw_duty_pct(0), 30)
        self.assertEqual(c.raw_duty_pct(50), 30)
        self.assertEqual(c.raw_duty_pct(85), 100)
        self.assertEqual(c.raw_duty_pct(120), 100)

    def test_interpolates_interior(self):
        c = self.curve()
        self.assertAlmostEqual(c.raw_duty_pct(57.5), 40.0)
        self.assertAlmostEqual(c.raw_duty_pct(65), 50.0)
        self.assertAlmostEqual(c.raw_duty_pct(71.5), 65.0)
        self.assertAlmostEqual(c.raw_duty_pct(81.5), 90.0)

    def test_unsorted_points_are_ordered(self):
        c = Curve([(85, 100), (50, 30)])
        self.assertAlmostEqual(c.raw_duty_pct(67.5), 65.0)

    def test_duplicate_temperatures_rejected(self):
        with self.assertRaises(ValueError):
            Curve([(50, 30), (50, 60)])

    def test_hysteresis_suppresses_small_drop(self):
        c = self.curve(hysteresis_c=3)
        self.assertAlmostEqual(c.duty_pct(70), 61.538, places=3)
        # 1.5 C drop is inside the band: hold the previous duty.
        self.assertAlmostEqual(c.duty_pct(68.5), 61.538, places=3)
        # 4 C drop clears the band: follow the curve down.
        self.assertAlmostEqual(c.duty_pct(66), 52.308, places=3)

    def test_hysteresis_never_delays_a_rise(self):
        c = self.curve(hysteresis_c=10)
        low = c.duty_pct(60)
        self.assertAlmostEqual(low, 43.333, places=3)
        # A rise inside the hysteresis band must still raise duty at once.
        self.assertAlmostEqual(c.duty_pct(60.5), 44.0, places=3)


class TestSlew(unittest.TestCase):
    def test_rises_immediately(self):
        self.assertEqual(slew(20, 100, 3), 100)

    def test_falls_by_at_most_one_step(self):
        self.assertEqual(slew(100, 20, 3), 97)

    def test_does_not_overshoot_target(self):
        self.assertEqual(slew(21, 20, 3), 20)

    def test_first_tick_takes_target(self):
        self.assertEqual(slew(None, 45, 3), 45)


class TestDutyBytes(unittest.TestCase):
    def test_pct_to_byte_boundaries(self):
        self.assertEqual(pwm.pct_to_byte(0), 0x00)
        self.assertEqual(pwm.pct_to_byte(20), 0x33)
        self.assertEqual(pwm.pct_to_byte(40), 0x66)
        self.assertEqual(pwm.pct_to_byte(90), 0xE6)
        self.assertEqual(pwm.pct_to_byte(100), 0xFF)

    def test_pct_to_byte_clamps_out_of_range(self):
        self.assertEqual(pwm.pct_to_byte(-10), 0x00)
        self.assertEqual(pwm.pct_to_byte(140), 0xFF)

    def test_byte_to_pct_round_trip(self):
        self.assertEqual(pwm.byte_to_pct(0x33), 20.0)
        self.assertEqual(pwm.byte_to_pct(0xFF), 100.0)


class TestApplyFall(unittest.TestCase):
    def test_low_half_write_preserves_sibling(self):
        self.assertEqual(pwm.apply_fall(0x33003300, False, 0x66), 0x33006600)

    def test_high_half_write_preserves_sibling(self):
        self.assertEqual(pwm.apply_fall(0x33003300, True, 0x66), 0x66003300)

    def test_rise_is_zeroed_only_in_target_half(self):
        self.assertEqual(pwm.apply_fall(0x3344_3355, False, 0x10), 0x33441000)
        self.assertEqual(pwm.apply_fall(0x3344_3355, True, 0x10), 0x10003355)

    def test_value_is_clamped_to_a_byte(self):
        self.assertEqual(pwm.apply_fall(0, False, 999), 0x0000FF00)
        self.assertEqual(pwm.apply_fall(0, False, -5), 0x00000000)



class FakeProc:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class TestIpmiDecode(unittest.TestCase):
    def read(self, stdout, fn=ipmi.read_raw, sensor=0x20):
        with mock.patch("p2afan.ipmi.subprocess.run", return_value=FakeProc(stdout)):
            return fn(sensor)

    def test_no_reading_decodes_to_none_not_zero(self):
        self.assertIsNone(self.read("00 e0 00 80"))
        self.assertIsNone(self.read("00 e0 00 80", ipmi.read_temp))
        self.assertIsNone(self.read("22 e0 00 80", ipmi.read_temp))

    def test_valid_temperature(self):
        self.assertEqual(self.read("22 c0 00 00", ipmi.read_temp), 34)

    def test_fan_rpm_scaling(self):
        self.assertEqual(self.read("22 c0 00 00", ipmi.read_fan_rpm), 3060)
        self.assertEqual(self.read("24 c0 00 00", ipmi.read_fan_rpm), 3240)
        self.assertEqual(self.read("20 c0 00 00", ipmi.read_fan_rpm), 2880)

    def test_failed_ipmitool_is_none(self):
        with mock.patch(
            "p2afan.ipmi.subprocess.run", return_value=FakeProc("", returncode=1)
        ):
            self.assertIsNone(ipmi.read_raw(0x20))


class TestZone(unittest.TestCase):
    class Src:
        def __init__(self, name, values):
            self.name = name
            self.values = list(values)

        def read(self):
            return self.values.pop(0) if self.values else None

    def test_zone_takes_hottest_valid_reading(self):
        zone = Zone(
            "gpu",
            [self.Src("a", [40]), self.Src("b", [None]), self.Src("c", [71])],
            Curve([(50, 30), (85, 100)]),
            85,
        )
        self.assertEqual(zone.sample(), 71)

    def test_all_sources_dead_gives_none_and_counts_failures(self):
        zone = Zone(
            "gpu",
            [self.Src("a", [None, None, None])],
            Curve([(50, 30), (85, 100)]),
            85,
        )
        for _ in range(3):
            self.assertIsNone(zone.sample())
        self.assertEqual(zone.stale_sources(3), ["a"])

    def test_recovery_clears_failure_count(self):
        zone = Zone("gpu", [self.Src("a", [None, None, 50])], Curve([(50, 30)]), 85)
        zone.sample()
        zone.sample()
        zone.sample()
        self.assertEqual(zone.stale_sources(2), [])


class TestSourceBuild(unittest.TestCase):

    def test_unknown_kind_and_missing_name_rejected(self):
        for kind in ("telepathy", [], {}):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                build({"kind": kind, "name": "x"})
        with self.assertRaises(ValueError):
            build({"kind": "ipmi", "sensor": 1})

    def test_exec_source_parses_first_float(self):
        src = build({"kind": "exec", "name": "s", "command": ["/bin/echo", "80.5 C"]})
        self.assertEqual(src.read(), 80.5)

    def test_exec_source_failure_is_none(self):
        src = build({"kind": "exec", "name": "s", "command": ["/bin/false"]})
        self.assertIsNone(src.read())


class FakePwm:
    """Stand-in for p2afan.pwm.Pwm reflecting this chassis's factory state."""

    def __init__(self, enabled="ABCDEFG", falls=None):
        self.enabled_set = set(enabled)
        self.falls = falls if falls is not None else {
            ch: 0x33 if ch in "ABCDEF" else 0 for ch in pwm.ALL_CHANNELS
        }

    def enabled(self, ch):
        return ch in self.enabled_set

    def enabled_channels(self):
        return [c for c in pwm.ALL_CHANNELS if c in self.enabled_set]

    def get_fall(self, ch):
        return self.falls[ch]


class TestWritableChannels(unittest.TestCase):
    mapping = {
        "A": [],
        "B": [],
        "C": [],
        "D": ["SYS_FAN_2", "SYS_FAN_5"],
        "E": ["SYS_FAN_3", "SYS_FAN_6"],
        "F": ["SYS_FAN_1", "SYS_FAN_4"],
        "G": [],
    }

    def test_auto_covers_mapped_plus_factory_driven(self):
        # A-C carry no tach but are factory-driven at 0x33, so they must ramp
        # with D/E/F; G is parked at 0 and H is disabled, so both stay alone.
        self.assertEqual(
            writable_channels(self.mapping, FakePwm()),
            ["A", "B", "C", "D", "E", "F"],
        )

    def test_never_writes_disabled_or_parked_channels(self):
        chans = writable_channels(self.mapping, FakePwm())
        self.assertNotIn("G", chans)
        self.assertNotIn("H", chans)

    def test_explicit_list_wins_but_still_respects_enable_bit(self):
        self.assertEqual(
            writable_channels(self.mapping, FakePwm(), ["F", "D", "H"]),
            ["D", "F"],
        )

    def test_no_mapping_file_falls_back_to_factory_driven(self):
        self.assertEqual(
            writable_channels({}, FakePwm()), ["A", "B", "C", "D", "E", "F"]
        )

    def test_mapped_channel_is_written_even_when_currently_parked(self):
        falls = dict(FakePwm().falls)
        falls["D"] = 0x00
        self.assertIn("D", writable_channels(self.mapping, FakePwm(falls=falls)))




if __name__ == "__main__":
    unittest.main()
