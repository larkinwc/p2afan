"""Safety regressions using persisted temporary state and fake actuators only."""

import argparse
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from p2afan import cli, control, ipmi, pwm
from p2afan.ahb import DEFAULT_BDF
from p2afan.curve import Curve, Zone


OTHER_BDF = "0000:0d:00.0"
FAN = next(iter(ipmi.FAN_SENSORS))


def config_data(**options):
    data = {
        "write_channels": ["A"],
        "zone": [{"name": "gpu", "critical_c": 85,
                  "curve": [[30, 20], [85, 100]],
                  "sources": [{"kind": "ipmi", "name": "GPU", "sensor": 0x20}]}],
    }
    data.update(options)
    return data


class FakePwm:
    def __init__(self, bdf):
        self.bdf = bdf
        self.falls = {ch: 0x33 if ch in "ABCDEF" else 0 for ch in pwm.ALL_CHANNELS}
        self.writes = []
        self.enabled_set = set("ABCDEFG")

    def enabled(self, ch):
        return ch in self.enabled_set

    def enabled_channels(self):
        return [ch for ch in pwm.ALL_CHANNELS if self.enabled(ch)]

    def get_fall(self, ch):
        return self.falls[ch]

    def set_fall(self, ch, value):
        # Every write, including the first, must already have durable ownership.
        with open(control.BASELINE_PATH) as fh:
            entry = json.load(fh)["devices"][self.bdf]
        if not entry["active"] or ch not in entry["baseline_duty"]:
            raise AssertionError("write before baseline persistence")
        self.writes.append((ch, value))
        self.falls[ch] = value


class FakeAhb:
    def __init__(self, bar, hardware):
        self.bdf = bar.split("/")[-2]
        self.hardware = hardware
        self.closed = False

    def ensure_bridge(self):
        pass

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class SafetyCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "baseline.json")
        self.telemetry_path = os.path.join(self.temp.name, "state.json")
        self.hardware = {bdf: FakePwm(bdf) for bdf in (DEFAULT_BDF, OTHER_BDF)}
        self.handles = []
        self.cfg_path = os.path.join(self.temp.name, "config.toml")
        with open(self.cfg_path, "w") as fh:
            fh.write('pci_bdf = "0c:00.0"\nwrite_channels = ["A"]\n'
                     '[[zone]]\nname = "gpu"\ncritical_c = 85\ncurve = [[30,20],[85,100]]\n'
                     'sources = [{kind="ipmi", name="GPU", sensor=32}]\n')
        patches = [mock.patch.object(control, "STATE_PATH", self.telemetry_path),
                   mock.patch.object(control, "BASELINE_PATH", self.path),
                   mock.patch.object(control, "Ahb", side_effect=self.open_bridge),
                   mock.patch.object(pwm, "Pwm", side_effect=lambda handle: self.hardware[handle.bdf]),
                   mock.patch.object(ipmi, "read_fan_rpm", return_value=2000),
                   mock.patch.object(ipmi, "read_temp", return_value=35),
                   mock.patch.object(ipmi, "set_sensor_reading"),
                   mock.patch.object(control, "sd_notify")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def open_bridge(self, bar, **kwargs):
        handle = FakeAhb(bar, self.hardware)
        self.handles.append(handle)
        return handle

    def controller(self, **options):
        ctl = control.Controller(control.Config(config_data(**options)), {"A": [FAN]})
        self.addCleanup(ctl.close)
        return ctl

    def state(self):
        with open(self.path) as fh:
            return json.load(fh)

    def release(self, bdf=DEFAULT_BDF):
        return control.release(config_path=self.cfg_path, pci_bdf=bdf)

    def test_cli_repeated_manual_sets_release_and_new_takeover(self):
        args = argparse.Namespace(bdf=None, config=self.cfg_path, channels="A", pct=70,
                                  value="0x99", lock_wait=0)
        with mock.patch.object(cli, "_session", side_effect=lambda args: self.open_bridge(
                "/sys/bus/pci/devices/" + DEFAULT_BDF + "/resource1")), contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_set(args)
            cli.cmd_set_raw(args)
            self.assertEqual(self.hardware[DEFAULT_BDF].falls["A"], 0x99)
            self.assertEqual(self.release(), {"A": 0x33})
            self.assertFalse(self.state()["devices"][DEFAULT_BDF]["active"])
            # A new BMC setpoint after release must become the next original.
            self.hardware[DEFAULT_BDF].falls["A"] = 0x45
            cli.cmd_set(args)
            self.assertEqual(self.release(), {"A": 0x45})
        self.assertEqual(self.hardware[DEFAULT_BDF].falls["B"], 0x33)

    def test_manual_then_daemon_and_crash_restart_preserve_original(self):
        p = self.hardware[DEFAULT_BDF]
        control.capture_manual_baseline(p, ["A"], DEFAULT_BDF)
        p.set_fall("A", 0x90)
        ctl = self.controller()
        ctl.tick()
        ctl.close()  # Simulated crash: no clean release.
        p.set_fall("A", 0xFF)  # ExecStopPost emergency duty.
        restarted = self.controller()
        restarted.tick()
        restarted.release()
        self.assertEqual(p.falls["A"], 0x33)
        self.assertEqual({ch for ch, value in p.writes}, {"A"})

    def test_baselines_are_device_scoped_and_channel_owned(self):
        for bdf, original in ((DEFAULT_BDF, 0x40), (OTHER_BDF, 0x60)):
            p = self.hardware[bdf]
            p.falls["A"] = original
            control.capture_manual_baseline(p, ["A"], bdf)
            p.set_fall("A", 0xFF)
        self.assertEqual(self.release(), {"A": 0x40})
        self.assertEqual(self.hardware[OTHER_BDF].falls["A"], 0xFF)
        self.assertEqual(self.release(OTHER_BDF), {"A": 0x60})
        self.assertEqual(self.hardware[DEFAULT_BDF].falls["B"], 0x33)

    def test_missing_and_corrupt_release_state_refuse_without_writes(self):
        invalid_states = [None, "broken json", [], {"baseline_duty": {"A": 51}},
                          {"devices": {DEFAULT_BDF: {"active": True,
                                                     "baseline_duty": {"A": 999}}}},
                          {"devices": {OTHER_BDF: {"active": True,
                                                  "baseline_duty": {"A": 51}}}}]
        for state in invalid_states:
            with self.subTest(state=state):
                if state is not None:
                    with open(self.path, "w") as fh:
                        fh.write(state if isinstance(state, str) else json.dumps(state))
                with self.assertRaises(RuntimeError):
                    self.release()
                self.assertEqual(self.handles, [])
                self.assertEqual(self.hardware[DEFAULT_BDF].writes, [])

    def test_baseline_persistence_failure_prevents_first_write_and_closes(self):
        with mock.patch.object(control, "write_state", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.controller()
        self.assertTrue(self.handles[-1].closed)
        self.assertEqual(self.hardware[DEFAULT_BDF].writes, [])

    def test_bridge_probe_constructor_failure_closes_handle(self):
        with mock.patch.object(FakeAhb, "ensure_bridge", side_effect=RuntimeError("locked")):
            with self.assertRaises(RuntimeError):
                self.controller()
        self.assertTrue(self.handles[-1].closed)

    def test_stall_survives_missing_rpm_and_recovery_needs_healthy_fans(self):
        ctl = self.controller(recover_ticks=2)
        readings = [100, None, 100, 100, 100, None, 2000, 2000]
        with mock.patch.object(ipmi, "read_fan_rpm", side_effect=readings):
            ctl.tick()
            ctl.tick()
            self.assertEqual(ctl.low_rpm_strikes[FAN], 1)
            for _ in range(4):
                state = ctl.tick()
                self.assertTrue(state["failsafe"])
                self.assertEqual(state["duty_byte"], 255)
                self.assertEqual(self.hardware[DEFAULT_BDF].writes[-1], ("A", 255))
            self.assertTrue(ctl.tick()["failsafe"])
            self.assertFalse(ctl.tick()["failsafe"])
        self.assertEqual(ctl.low_rpm_strikes[FAN], 0)

    def test_missing_fan_telemetry_has_separate_bounded_failure_limit(self):
        ctl = self.controller(source_fail_limit=3)
        with mock.patch.object(ipmi, "read_fan_rpm", return_value=None):
            self.assertFalse(ctl.tick()["failsafe"])
            self.assertFalse(ctl.tick()["failsafe"])
            for _ in range(5):
                self.assertTrue(ctl.tick()["failsafe"])
                self.assertEqual(ctl.fan_fail_counts[FAN], 3)
        self.assertEqual(ctl.low_rpm_strikes.get(FAN, 0), 0)

    def test_required_injection_attempts_every_sensor_and_never_notifies_ready(self):
        ctl = self.controller(mode="inject")
        with mock.patch.object(ipmi, "set_sensor_reading", side_effect=RuntimeError("offline")) as inject:
            with self.assertRaises(RuntimeError):
                ctl.tick()
            self.assertEqual([call.args[0] for call in inject.call_args_list], list(control.INJECT_SENSORS))
        self.assertFalse(ctl.ready)
        self.assertNotIn(mock.call("READY=1"), control.sd_notify.call_args_list)
        self.assertEqual(self.handles, [])

    def test_optional_mirror_failure_preserves_pwm_and_exposes_failure_state(self):
        ctl = self.controller(inject=True)
        with mock.patch.object(ipmi, "set_sensor_reading", side_effect=RuntimeError("offline")):
            state = ctl.tick()
        self.assertEqual(state["injection_failures"], list(control.INJECT_SENSORS))
        self.assertEqual(self.hardware[DEFAULT_BDF].falls["A"], state["duty_byte"])
        self.assertTrue(ctl.ready)

    def test_failsafe_malformed_mapping_still_actuates_configured_device(self):
        path = os.path.join(self.temp.name, "bad-mapping.toml")
        with open(path, "w") as fh:
            fh.write('[channels]\nA = "not-an-array"\n')
        self.hardware[OTHER_BDF].falls["A"] = 0x42
        result = control.force_failsafe(path, self.cfg_path, OTHER_BDF)
        self.assertEqual(result, {"A": 255})
        self.assertEqual(self.hardware[DEFAULT_BDF].writes, [])
        self.assertEqual(self.release(OTHER_BDF), {"A": 0x42})

    def test_inject_emergency_never_opens_bridge_and_partial_required_failure_raises(self):
        cfg = control.Config(config_data(mode="inject"))
        def fail_one(sensor, value):
            if sensor == control.INJECT_SENSORS[0]:
                raise RuntimeError("one sensor offline")
        with mock.patch.object(control, "load_config", return_value=cfg), mock.patch.object(
                ipmi, "set_sensor_reading", side_effect=fail_one) as inject:
            with self.assertRaises(RuntimeError):
                control.force_failsafe(config_path=self.cfg_path)
            self.assertEqual([call.args[0] for call in inject.call_args_list], list(control.INJECT_SENSORS))
        self.assertEqual(self.handles, [])

    def test_emergency_fallback_all_injections_failing_raises(self):
        with mock.patch.object(control, "Ahb", side_effect=RuntimeError("bridge gone")), mock.patch.object(
                ipmi, "set_sensor_reading", side_effect=RuntimeError("offline")) as inject:
            with self.assertRaisesRegex(RuntimeError, "all emergency"):
                control.force_failsafe(config_path=self.cfg_path)
            self.assertEqual([call.args[0] for call in inject.call_args_list], list(control.INJECT_SENSORS))

    def test_injection_heartbeat_cannot_overwrite_concurrent_manual_baseline(self):
        ctl = self.controller(mode="inject")
        p = self.hardware[DEFAULT_BDF]
        original_write_state = control.write_state
        def capture_before_telemetry_replace(state, path=None):
            if path is None:
                # Interleave takeover after the heartbeat has constructed its
                # stale status snapshot, immediately before replacing it.
                control.capture_manual_baseline(p, ["A"], DEFAULT_BDF)
                p.set_fall("A", 255)
            return original_write_state(state, path)
        with mock.patch.object(control, "write_state", side_effect=capture_before_telemetry_replace):
            ctl.tick()
        with open(self.telemetry_path) as fh:
            self.assertEqual(json.load(fh)["mode"], "inject")
        self.assertEqual(self.release(), {"A": 0x33})
        self.assertFalse(self.state()["devices"][DEFAULT_BDF]["active"])

    def test_legacy_unscoped_telemetry_refuses_relatched_takeover(self):
        with open(self.telemetry_path, "w") as fh:
            json.dump({"baseline_duty": {"A": 0x33}}, fh)
        p = self.hardware[DEFAULT_BDF]
        p.falls["A"] = 255
        with self.assertRaises(RuntimeError):
            control.capture_manual_baseline(p, ["A"], DEFAULT_BDF)
        with self.assertRaises(RuntimeError):
            self.release()
        self.assertEqual(p.writes, [])
        self.assertFalse(os.path.exists(self.path))

    def test_disabled_manual_channel_refuses_before_any_write_or_ownership(self):
        args = argparse.Namespace(bdf=DEFAULT_BDF, config=self.cfg_path,
                                  channels="A,H", pct=70, lock_wait=0)
        with mock.patch.object(cli, "_session", side_effect=lambda args: self.open_bridge(
                "/sys/bus/pci/devices/" + DEFAULT_BDF + "/resource1")):
            with self.assertRaises(pwm.ChannelDisabled):
                cli.cmd_set(args)
        self.assertEqual(self.hardware[DEFAULT_BDF].writes, [])
        self.assertFalse(os.path.exists(self.path))

    def test_failed_release_retains_original_for_retry(self):
        p = self.hardware[DEFAULT_BDF]
        control.capture_manual_baseline(p, ["A"], DEFAULT_BDF)
        p.set_fall("A", 255)
        original_write = p.set_fall
        def ignored_restore(ch, value):
            if value != 0x33:
                original_write(ch, value)
        with mock.patch.object(p, "set_fall", side_effect=ignored_restore):
            with self.assertRaises(RuntimeError):
                self.release()
        self.assertTrue(self.state()["devices"][DEFAULT_BDF]["active"])
        self.assertEqual(self.release(), {"A": 0x33})

    def test_run_daemon_override_actuates_and_releases_only_selected_device(self):
        self.hardware[OTHER_BDF].falls["A"] = 0x42
        observed = []
        def one_tick(ctl):
            observed.append(ctl.tick())
            raise KeyboardInterrupt
        with mock.patch.object(control.Controller, "run", one_tick), mock.patch.object(
                control.signal, "signal", return_value=control.signal.SIG_DFL):
            control.run_daemon(self.cfg_path, os.path.join(self.temp.name, "missing.toml"), OTHER_BDF)
        self.assertEqual(observed[0]["pci_bdf"], OTHER_BDF)
        self.assertEqual(self.hardware[OTHER_BDF].falls["A"], 0x42)
        self.assertEqual(self.hardware[DEFAULT_BDF].writes, [])
        self.assertFalse(self.state()["devices"][OTHER_BDF]["active"])
        self.assertTrue(self.handles[-1].closed)
        writes_before = list(self.hardware[OTHER_BDF].writes)
        with mock.patch.dict(os.environ, {"SERVICE_RESULT": "success"}):
            control.stop_post(config_path=self.cfg_path, pci_bdf=OTHER_BDF)
        self.assertEqual(self.hardware[OTHER_BDF].writes, writes_before)
        with self.assertRaises(RuntimeError):
            self.release(OTHER_BDF)

    def test_stop_post_honors_custom_config_and_device_across_emergency_and_release(self):
        with open(self.cfg_path) as fh:
            text = fh.read()
        with open(self.cfg_path, "w") as fh:
            fh.write(text.replace("0c:00.0", "0d:00.0"))
        self.hardware[OTHER_BDF].falls["A"] = 0x42
        with mock.patch.dict(os.environ, {"SERVICE_RESULT": "exit-code"}):
            control.stop_post(config_path=self.cfg_path)
        self.assertEqual(self.hardware[OTHER_BDF].falls["A"], 255)
        with mock.patch.dict(os.environ, {"SERVICE_RESULT": "success"}):
            control.stop_post(config_path=self.cfg_path)
        self.assertEqual(self.hardware[OTHER_BDF].falls["A"], 0x42)
        self.assertEqual(self.hardware[DEFAULT_BDF].writes, [])

    def test_emergency_pwm_ignores_corrupt_or_unwritable_baseline_state(self):
        p = self.hardware[DEFAULT_BDF]
        def emergency_write(ch, value):
            p.writes.append((ch, value))
            p.falls[ch] = value
        with open(self.path, "w") as fh:
            fh.write("corrupt state")
        with mock.patch.object(p, "set_fall", side_effect=emergency_write):
            self.assertEqual(control.force_failsafe(config_path=self.cfg_path), {"A": 255})
        with self.assertRaises(RuntimeError):
            self.release()
        os.unlink(self.path)
        p.falls["A"] = 0x33
        with mock.patch.object(control, "write_state", side_effect=OSError("disk full")), mock.patch.object(
                p, "set_fall", side_effect=emergency_write):
            self.assertEqual(control.force_failsafe(config_path=self.cfg_path), {"A": 255})
        self.assertEqual(p.falls["A"], 255)
        self.assertEqual(ipmi.set_sensor_reading.call_args_list, [])

    def test_invalid_curve_does_not_hide_valid_emergency_pwm_identity(self):
        with open(self.cfg_path) as fh:
            text = fh.read()
        with open(self.cfg_path, "w") as fh:
            fh.write(text.replace("0c:00.0", "0d:00.0").replace("[85,100]", "[85,-1]"))
        result = control.force_failsafe(config_path=self.cfg_path)
        self.assertEqual(result, {ch: 255 for ch in "ABCDEF"})
        self.assertEqual(self.hardware[DEFAULT_BDF].writes, [])
        self.assertEqual(self.hardware[OTHER_BDF].falls["A"], 255)

    def test_unknown_emergency_identity_never_guesses_pwm_device(self):
        with open(self.cfg_path, "w") as fh:
            fh.write("malformed toml")
        control.force_failsafe(config_path=self.cfg_path)
        self.assertEqual(self.handles, [])
        self.assertEqual([call.args[0] for call in ipmi.set_sensor_reading.call_args_list],
                         list(control.INJECT_SENSORS))


class TestValidation(unittest.TestCase):
    def test_emergency_duty_never_lowers_normal_curve_demand(self):
        for options in ({"min_duty_pct": 0, "failsafe_duty_pct": 0},
                        {"failsafe_duty_pct": 80}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                control.Config(config_data(**options))

    def test_injection_emergency_reaches_critical_threshold(self):
        for options in ({"mode": "inject", "failsafe_inject_c": 84},
                        {"inject": True, "failsafe_inject_c": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                control.Config(config_data(**options))
        high = config_data(mode="inject", failsafe_inject_c=255)
        high["zone"][0]["critical_c"] = 256
        with self.assertRaises(ValueError):
            control.Config(high)

    def test_invalid_safety_fields_rejected(self):
        invalid = {"tick_seconds": [0, -1, float("nan"), float("inf"), True],
                   "hold_interval": [-1, float("inf")],
                   "min_duty_pct": [-1, 101, float("nan")],
                   "failsafe_duty_pct": [19, 101, float("inf")],
                   "down_slew_pct_per_tick": [0, -1, 101, float("nan")],
                   "source_fail_limit": [0, -1, True, 2.5],
                   "recover_ticks": [0, True, 1.5],
                   "min_fan_rpm": [0, True, 720.5],
                   "failsafe_inject_c": [-1, 256, float("nan")],
                   "write_channels": ["A", ["Z"], ["A", "a"], [1]],
                   "pci_bdf": ["bad", "0c:20.0"]}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    control.Config(config_data(**{key: value}))

    def test_invalid_zone_shapes_names_and_thresholds_rejected(self):
        good = config_data()["zone"][0]
        invalid = [None, "gpu", [{}], [None], [good, copy.deepcopy(good)]]
        for key, value in (("name", ""), ("name", 1), ("critical_c", float("nan")),
                           ("hysteresis_c", -1), ("sources", "GPU"),
                           ("sources", [None]), ("sources", []),
                           ("sources", [dict(good["sources"][0], name="")]),
                           ("sources", good["sources"] * 2)):
            invalid.append([dict(good, **{key: value})])
        for zones in invalid:
            with self.subTest(zones=zones), self.assertRaises(ValueError):
                control.Config(config_data(zone=zones))

    def test_invalid_curve_shapes_ranges_and_decreasing_duty_rejected(self):
        for points in (None, [], [[30]], [[30, 20, 1]], [[30, float("nan")]],
                       [[float("inf"), 20]], [[30, -1]], [[30, 101]],
                       [[30, 80], [85, 20]], [[30, True]]):
            with self.subTest(points=points), self.assertRaises(ValueError):
                Curve(points)
        for hysteresis in (-1, float("nan"), float("inf"), True):
            with self.subTest(hysteresis=hysteresis), self.assertRaises(ValueError):
                Curve([[30, 20]], hysteresis)

    def test_mapping_rejects_invalid_shapes_channels_and_fans(self):
        for text in ('A = []', 'channels = []', '[channels]\nZ = []', '[channels]\nA = "SYS_FAN_1"',
                     '[channels]\nA = ["unknown"]', '[channels]\nA = [1]',
                     '[channels]\nA = ["SYS_FAN_1", "SYS_FAN_1"]',
                     '[channels]\nA = []\na = []'):
            with self.subTest(text=text), tempfile.NamedTemporaryFile(mode="w") as fh:
                fh.write(text)
                fh.flush()
                with self.assertRaises(ValueError):
                    control.load_mapping(fh.name)

    def test_nonfinite_source_readings_are_failures_not_temperatures(self):
        source = mock.Mock(name="source")
        source.name = "GPU"
        source.read.side_effect = [float("nan"), float("inf"), 40]
        zone = Zone("gpu", [source], Curve([[30, 20]]), 85)
        self.assertIsNone(zone.sample())
        self.assertIsNone(zone.sample())
        self.assertEqual(zone.stale_sources(2), ["GPU"])
        self.assertEqual(zone.sample(), 40)
        self.assertEqual(zone.stale_sources(2), [])


if __name__ == "__main__":
    unittest.main()
