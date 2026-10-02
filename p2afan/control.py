"""Control loop, failsafe, watchdog, override detection, release."""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import socket
import time
import tomllib

from . import ipmi, pwm
from .ahb import DEFAULT_BDF, RUN_DIR, Ahb, bar_path, normalize_bdf
from .curve import Curve, Zone
from .sources import build as build_source

CONFIG_PATH = "/etc/p2afan/config.toml"
MAPPING_PATH = "/etc/p2afan/mapping.toml"
STATE_PATH = RUN_DIR + "/state.json"
BASELINE_PATH = RUN_DIR + "/baseline.json"

# Bounded so a stuck holder surfaces as a service failure instead of a hang.
DAEMON_LOCK_WAIT = 30.0

LOG = logging.getLogger("p2afan")

# Sensors written in inject mode: the factory curve's NVIDIA GPU inputs.
INJECT_SENSORS = (0x20, 0x22, 0x24, 0x26)

DEFAULTS = {
    "mode": "pwm",
    "pci_bdf": DEFAULT_BDF,
    "inject": False,
    "tick_seconds": 5.0,
    "min_duty_pct": 20.0,
    "failsafe_duty_pct": 100.0,
    "down_slew_pct_per_tick": 3.0,
    "hold_interval": 0.0,
    "source_fail_limit": 3,
    # Matches the firmware's own SYS_FAN_n lower-critical threshold (720 RPM).
    "min_fan_rpm": 720,
    "recover_ticks": 6,
    "failsafe_inject_c": 95.0,
    "write_channels": [],
}


def _number(value: object, name: str, minimum: float, maximum: float | None = None,
            *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if (not math.isfinite(result) or result < minimum
            or (positive and result <= minimum)
            or (maximum is not None and result > maximum)):
        raise ValueError(f"{name} is outside its safe range")
    return result


def _count(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _name(value: object, kind: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{kind} name must be a nonempty string")
    return value


class Config:
    def __init__(self, data: dict) -> None:
        if not isinstance(data, dict):
            raise ValueError("config must be a table")
        opts = dict(DEFAULTS)
        opts.update({key: data[key] for key in DEFAULTS if key in data})
        self.pci_bdf = normalize_bdf(opts["pci_bdf"])
        self.mode = opts["mode"]
        if self.mode not in ("pwm", "inject"):
            raise ValueError(f"mode must be 'pwm' or 'inject', got {self.mode!r}")
        if type(opts["inject"]) is not bool:
            raise ValueError("inject must be a boolean")
        self.inject = opts["inject"]
        self.tick_seconds = _number(opts["tick_seconds"], "tick_seconds", 0, positive=True)
        self.min_duty_pct = _number(opts["min_duty_pct"], "min_duty_pct", 0, 100)
        self.failsafe_duty_pct = _number(
            opts["failsafe_duty_pct"], "failsafe_duty_pct", self.min_duty_pct, 100)
        self.down_slew_pct_per_tick = _number(
            opts["down_slew_pct_per_tick"], "down_slew_pct_per_tick", 0, 100, positive=True)
        self.hold_interval = _number(opts["hold_interval"], "hold_interval", 0)
        self.source_fail_limit = _count(opts["source_fail_limit"], "source_fail_limit")
        self.min_fan_rpm = _count(opts["min_fan_rpm"], "min_fan_rpm")
        self.recover_ticks = _count(opts["recover_ticks"], "recover_ticks")
        self.failsafe_inject_c = _number(opts["failsafe_inject_c"], "failsafe_inject_c", 0, 255)
        channels = opts["write_channels"]
        if not isinstance(channels, list) or any(not isinstance(ch, str) for ch in channels):
            raise ValueError("write_channels must be an array of channel names")
        self.write_channels = [ch.upper() for ch in channels]
        if len(set(self.write_channels)) != len(self.write_channels):
            raise ValueError("write_channels contains duplicates")
        for ch in self.write_channels:
            if ch not in pwm.CHANNELS:
                raise ValueError(f"write_channels has unknown channel {ch!r}")
        zones = data.get("zone", [])
        if not isinstance(zones, list) or not zones:
            raise ValueError("config needs [[zone]] entries")
        self.zones = []
        zone_names: set[str] = set()
        source_names: set[str] = set()
        for spec in zones:
            if not isinstance(spec, dict):
                raise ValueError("zone must be a table")
            name = _name(spec.get("name"), "zone")
            if name in zone_names:
                raise ValueError(f"duplicate zone name {name!r}")
            zone_names.add(name)
            source_specs = spec.get("sources", [])
            if not isinstance(source_specs, list) or not source_specs:
                raise ValueError(f"zone {name!r} needs sources")
            sources = []
            for source_spec in source_specs:
                if not isinstance(source_spec, dict):
                    raise ValueError("source must be a table")
                source_name = _name(source_spec.get("name"), "source")
                if source_name in source_names:
                    raise ValueError(f"duplicate source name {source_name!r}")
                source_names.add(source_name)
                sources.append(build_source(source_spec))
            self.zones.append(Zone(
                name=name, sources=sources,
                curve=Curve(spec.get("curve", []), spec.get("hysteresis_c", 0.0)),
                critical_c=_number(spec.get("critical_c"), "critical_c", -273.15),
            ))
        highest_duty = max(duty for zone in self.zones for _, duty in zone.curve.points)
        if self.failsafe_duty_pct <= 0 or self.failsafe_duty_pct < highest_duty:
            raise ValueError("failsafe_duty_pct must be positive and cover every curve duty")
        if ((self.inject or self.mode == "inject")
                and self.failsafe_inject_c < max(zone.critical_c for zone in self.zones)):
            raise ValueError("failsafe_inject_c must reach every zone's critical_c")


def load_config(path: str = CONFIG_PATH) -> Config:
    with open(path, "rb") as fh:
        return Config(tomllib.load(fh))


def load_mapping(path: str = MAPPING_PATH) -> dict[str, list[str]]:
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return {}
    if "channels" not in data:
        raise ValueError("mapping needs a [channels] table")
    channels = data["channels"]
    if not isinstance(channels, dict):
        raise ValueError("mapping channels must be a table")
    result: dict[str, list[str]] = {}
    for channel, fans in channels.items():
        ch = channel.upper()
        if ch not in pwm.CHANNELS or ch in result:
            raise ValueError(f"unknown or duplicate mapping channel {channel!r}")
        if (not isinstance(fans, list)
                or any(not isinstance(fan, str) or fan not in ipmi.FAN_SENSORS for fan in fans)
                or len(set(fans)) != len(fans)):
            raise ValueError(f"mapping PWM{ch} must contain unique known fan names")
        result[ch] = list(fans)
    return result


def writable_channels(
    mapping: dict[str, list[str]],
    p: pwm.Pwm,
    explicit: list[str] | None = None,
) -> list[str]:
    """Decide which PWM channels the controller drives.

    Explicit `write_channels` from the config wins. Otherwise: every channel
    the mapping attributes at least one tach to, plus every other enabled
    channel the factory firmware is actively driving (duty > 0).

    On this chassis the mapping attributes fans to D/E/F only, while the
    factory also drives A/B/C at the same 0x33 idle with no tach of their own.
    Carrying A/B/C at the computed duty can only add airflow versus factory
    idle (the duty floor is the factory idle), and leaving them pinned at 20 %
    while D/E/F ramp would strand any untached header. Channels parked at duty
    0 (PWMG) and disabled channels (PWMH) are never written.
    """
    if explicit:
        chans = list(explicit)
    else:
        chans = [ch for ch, fans in mapping.items() if fans]
        chans += [
            ch
            for ch in p.enabled_channels()
            if ch not in chans and p.get_fall(ch) > 0
        ]
    return [ch for ch in pwm.ALL_CHANNELS if ch in chans and p.enabled(ch)]


def write_state(state: dict, path: str | None = None) -> None:
    path = STATE_PATH if path is None else path
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def read_state(path: str | None = None) -> dict | None:
    path = STATE_PATH if path is None else path
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def sd_notify(message: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode(), addr)
    except OSError as exc:  # pragma: no cover - best effort
        LOG.debug("sd_notify(%s) failed: %s", message, exc)


def slew(previous: float | None, target: float, down_step: float) -> float:
    """Rise immediately, fall at most `down_step` percent per tick."""
    if previous is None or target >= previous:
        return target
    return max(target, previous - down_step)


def _baseline_state() -> dict:
    """Read authoritative ownership, independently of injection heartbeats."""
    try:
        with open(BASELINE_PATH) as fh:
            state = json.load(fh)
    except FileNotFoundError:
        # Old status files carried ownership. Do not silently relatch a manual
        # or crashed daemon's duty when upgrading without a safe release.
        try:
            with open(STATE_PATH) as fh:
                legacy = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise RuntimeError("cannot read legacy pre-takeover baseline") from exc
        if not isinstance(legacy, dict):
            raise RuntimeError("invalid legacy state")
        if legacy.get("baseline_duty") or legacy.get("devices"):
            raise RuntimeError("legacy baseline; refusing to relatch an active takeover")
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError("cannot read pre-takeover baseline") from exc
    if not isinstance(state, dict) or not isinstance(state.get("devices", {}), dict):
        raise RuntimeError("invalid pre-takeover baseline state")
    # Unscoped state from older versions cannot safely be attributed to a device.
    if state.get("baseline_duty") and not state.get("devices"):
        raise RuntimeError("unscoped baseline; refusing to guess its PCI device")
    return state


def _active_baseline(state: dict, pci_bdf: str, *, required: bool = False) -> dict[str, int]:
    entry = state.get("devices", {}).get(pci_bdf)
    if entry is None:
        if required:
            raise RuntimeError(f"no active pre-takeover baseline for {pci_bdf}")
        return {}
    if not isinstance(entry, dict) or type(entry.get("active")) is not bool:
        raise RuntimeError(f"invalid baseline for {pci_bdf}")
    if not entry["active"]:
        if entry.get("baseline_duty") != {}:
            raise RuntimeError(f"invalid inactive baseline for {pci_bdf}")
        if required:
            raise RuntimeError(f"no active pre-takeover baseline for {pci_bdf}")
        return {}
    baseline = entry.get("baseline_duty")
    if (not isinstance(baseline, dict) or not baseline
            or any(ch not in pwm.CHANNELS or type(value) is not int or not 0 <= value <= 255
                   for ch, value in baseline.items())):
        raise RuntimeError(f"invalid active baseline for {pci_bdf}")
    return dict(baseline)


def capture_manual_baseline(p: pwm.Pwm, channels: list[str], pci_bdf: str) -> dict[str, int]:
    """Persist ownership before any duty write, retaining an active takeover's original."""
    pci_bdf = normalize_bdf(pci_bdf)
    if not channels or any(ch not in pwm.CHANNELS for ch in channels):
        raise ValueError("baseline capture needs valid PWM channels")
    for ch in channels:
        if not p.enabled(ch):
            raise pwm.ChannelDisabled(f"PWM{ch} is disabled; refusing takeover")
    state = _baseline_state()
    baseline = _active_baseline(state, pci_bdf)
    for ch in channels:
        if ch not in baseline:
            value = p.get_fall(ch)
            if type(value) is not int or not 0 <= value <= 255:
                raise RuntimeError(f"invalid live duty for PWM{ch}")
            baseline[ch] = value
    state.setdefault("devices", {})[pci_bdf] = {"active": True, "baseline_duty": baseline}
    write_state(state, BASELINE_PATH)
    return baseline


def _restore_baseline(p: pwm.Pwm, pci_bdf: str) -> dict[str, int]:
    state = _baseline_state()
    baseline = _active_baseline(state, pci_bdf, required=True)
    if any(not p.enabled(ch) for ch in baseline):
        raise RuntimeError("owned PWM channel is disabled; cannot safely release")
    applied = {}
    for ch, value in sorted(baseline.items()):
        p.set_fall(ch, value)
        applied[ch] = p.get_fall(ch)
        if applied[ch] != value:
            raise RuntimeError(f"PWM{ch} baseline restore did not stick")
    state["devices"][pci_bdf] = {"active": False, "baseline_duty": {}}
    write_state(state, BASELINE_PATH)
    return applied


def _inject_sensors(temp: float) -> list[int]:
    """Attempt every sensor and return failures, even if an earlier write fails."""
    raw = max(0, min(255, int(round(temp))))
    failures = []
    for sensor in INJECT_SENSORS:
        try:
            ipmi.set_sensor_reading(sensor, raw)
        except Exception as exc:
            failures.append(sensor)
            LOG.warning("inject sensor %#04x failed: %s", sensor, exc)
    return failures


class Controller:
    def __init__(self, config: Config, mapping: dict[str, list[str]]) -> None:
        self.cfg = config
        self.mapping = mapping
        self.uses_pwm = config.mode == "pwm"
        self.ahb: Ahb | None = None
        self.pwm: pwm.Pwm | None = None
        self.channels: list[str] = []
        self.baseline: dict[str, int] = {}
        try:
            if self.uses_pwm:
                # Inject mode never opens P2A, including emergency actuation.
                self.ahb = Ahb(bar_path(config.pci_bdf), lock_timeout=DAEMON_LOCK_WAIT)
                self.ahb.ensure_bridge()
                self.pwm = pwm.Pwm(self.ahb)
                self.channels = writable_channels(mapping, self.pwm, config.write_channels)
                if not self.channels:
                    raise RuntimeError("no writable PWM channels; check mapping.toml")
                self.baseline = capture_manual_baseline(
                    self.pwm, self.channels, config.pci_bdf)
            self.fans = self._watched_fans()
        except BaseException:
            self.close()
            raise
        self.current_pct: float | None = None
        self.overrides = 0
        self.failsafe = False
        self.cool_ticks = 0
        self.low_rpm_strikes: dict[str, int] = {}
        self.fan_fail_counts: dict[str, int] = {}
        self.injection_failures: list[int] = []
        self.ready = False
        LOG.info(
            "mode=%s inject=%s channels=%s baseline=%s watched_fans=%s",
            self.cfg.mode,
            self.cfg.inject,
            ",".join(self.channels) or "none",
            {k: hex(v) for k, v in self.baseline.items()} or "n/a",
            ",".join(self.fans) or "none",
        )


    def _watched_fans(self) -> list[str]:
        """Tachs used for the stall check.

        In pwm mode: only fans attributed to channels we drive, so a stall is
        attributable. In inject mode we drive no channel, so watch every fan
        the mapping knows about (all six here) since the BMC is the actuator.
        """
        fans: list[str] = []
        sources = self.channels if self.uses_pwm else list(self.mapping)
        for ch in sources:
            for fan in self.mapping.get(ch, []):
                if fan in ipmi.FAN_SENSORS and fan not in fans:
                    fans.append(fan)
        if not fans and not self.uses_pwm:
            fans = list(ipmi.FAN_SENSORS)
        return fans

    # -- actuation -------------------------------------------------------
    def apply_duty(self, pct: float) -> tuple[int, bool]:
        """Write `pct` to all mapped channels; return (byte, readback_ok)."""
        value = pwm.pct_to_byte(pct)
        ok = True
        for ch in self.channels:
            self.pwm.set_fall(ch, value)
        for ch in self.channels:
            got = self.pwm.get_fall(ch)
            if got != value:
                ok = False
                LOG.warning(
                    "PWM%s duty readback %#04x != requested %#04x (override?)",
                    ch,
                    got,
                    value,
                )
        if not ok:
            self.overrides += 1
        return value, ok

    def inject_temps(self, temp: float) -> None:
        self.injection_failures = _inject_sensors(temp)
        if self.injection_failures and not self.uses_pwm:
            raise RuntimeError(f"required sensor injection failed: {self.injection_failures}")

    def actuate(self, pct: float, temp: float | None, failsafe: bool) -> int:
        """Apply the decision through whichever actuator this mode uses.

        pwm mode writes the duty registers. inject mode instead feeds the
        BMC's own NVIDIA-GPU temperature sensors, so the factory curve ramps
        on behalf of a card the BMC cannot see; in failsafe it is fed
        `failsafe_inject_c` so the factory curve goes to its own maximum.
        """
        value = pwm.pct_to_byte(pct)
        if self.uses_pwm:
            value, _ = self.apply_duty(pct)
        if not self.uses_pwm or self.cfg.inject:
            feed = self.cfg.failsafe_inject_c if failsafe else temp
            if feed is None:
                if not self.uses_pwm:
                    raise RuntimeError("inject mode has no temperature to actuate")
            else:
                self.inject_temps(feed)
        return value

    def release(self) -> None:
        if not self.uses_pwm or self.pwm is None:
            LOG.info("inject mode: no duty registers were taken over")
            return
        applied = _restore_baseline(self.pwm, self.cfg.pci_bdf)
        self.baseline = {}
        LOG.info(
            "released fans to baseline duty %s; BMC thermal loop resumes",
            {k: hex(v) for k, v in applied.items()},
        )

    # -- loop ------------------------------------------------------------
    def tick(self) -> dict:
        reason: str | None = None
        critical = False
        zone_state: dict[str, dict] = {}
        targets: list[float] = []
        hottest: float | None = None

        for zone in self.cfg.zones:
            temp = zone.sample()
            if temp is None:
                reason = f"zone {zone.name} has no valid reading"
            stale = zone.stale_sources(self.cfg.source_fail_limit)
            if stale:
                reason = f"sources failing in {zone.name}: {','.join(stale)}"
            if temp is not None:
                if temp >= zone.critical_c:
                    critical = True
                    reason = f"zone {zone.name} at {temp:.1f}C >= critical {zone.critical_c:.0f}C"
                targets.append(zone.curve.duty_pct(temp))
                if hottest is None or temp > hottest:
                    hottest = temp
            zone_state[zone.name] = {
                "temp_c": temp,
                "critical_c": zone.critical_c,
                "duty_pct": zone.curve.raw_duty_pct(temp) if temp is not None else None,
                "readings": zone.last_readings,
            }

        # Decide fan health before actuation: a stalled fan must never see a
        # transient downward duty step before failsafe is reasserted.
        rpms = {name: ipmi.read_fan_rpm(ipmi.FAN_SENSORS[name]) for name in self.fans}
        fans_healthy = True
        for name, rpm in rpms.items():
            if rpm is None or not math.isfinite(rpm):
                rpms[name] = None
                fans_healthy = False
                self.fan_fail_counts[name] = min(
                    self.cfg.source_fail_limit, self.fan_fail_counts.get(name, 0) + 1)
                if self.fan_fail_counts[name] >= self.cfg.source_fail_limit:
                    reason = f"fan telemetry failing: {name}"
            else:
                self.fan_fail_counts[name] = 0
                if rpm < self.cfg.min_fan_rpm:
                    fans_healthy = False
                    self.low_rpm_strikes[name] = min(2, self.low_rpm_strikes.get(name, 0) + 1)
                else:
                    self.low_rpm_strikes[name] = 0
            if self.low_rpm_strikes.get(name, 0) >= 2:
                reason = f"fan stalled: {name}"

        if critical or reason is not None:
            if not self.failsafe:
                LOG.critical("entering failsafe: %s", reason)
            self.failsafe = True
            self.cool_ticks = 0
            duty_pct = self.cfg.failsafe_duty_pct
        else:
            if self.failsafe:
                self.cool_ticks = self.cool_ticks + 1 if fans_healthy else 0
                if self.cool_ticks >= self.cfg.recover_ticks:
                    LOG.warning(
                        "leaving failsafe after %d clean ticks", self.cool_ticks
                    )
                    self.failsafe = False
                    self.cool_ticks = 0
                    self.current_pct = self.cfg.failsafe_duty_pct
            if self.failsafe:
                duty_pct = self.cfg.failsafe_duty_pct
            else:
                target = max(targets) if targets else self.cfg.failsafe_duty_pct
                target = max(self.cfg.min_duty_pct, min(100.0, target))
                duty_pct = slew(
                    self.current_pct, target, self.cfg.down_slew_pct_per_tick
                )

        value = self.actuate(duty_pct, hottest, self.failsafe)
        self.current_pct = duty_pct

        state = {
            "ts": time.time(),
            "mode": self.cfg.mode,
            "pci_bdf": self.cfg.pci_bdf,
            "inject": self.cfg.inject,
            "duty_pct": round(duty_pct, 1),
            "duty_byte": value,
            "channels": self.channels,
            "zones": zone_state,
            "fan_rpm": rpms,
            "fan_fail_counts": dict(self.fan_fail_counts),
            "low_rpm_strikes": dict(self.low_rpm_strikes),
            "injection_failures": list(self.injection_failures),
            "failsafe": self.failsafe,
            "overrides": self.overrides,
            "baseline_duty": self.baseline,
        }
        write_state(state)
        if not self.ready:
            sd_notify("READY=1")
            self.ready = True
        sd_notify("WATCHDOG=1")
        sd_notify(
            "STATUS=duty %.0f%% (%#04x) failsafe=%s overrides=%d"
            % (duty_pct, value, self.failsafe, self.overrides)
        )
        return state

    def run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                state = self.tick()
            except Exception:
                LOG.exception("tick failed; forcing failsafe")
                try:
                    self.actuate(self.cfg.failsafe_duty_pct, None, True)
                except Exception:
                    LOG.exception("failsafe actuation failed")
                raise
            LOG.info(
                "duty %.0f%% (%#04x) %s%s",
                state["duty_pct"],
                state["duty_byte"],
                " ".join(
                    f"{z}={v['temp_c']}" for z, v in state["zones"].items()
                ),
                " FAILSAFE" if state["failsafe"] else "",
            )
            elapsed = time.monotonic() - started
            remaining = max(0.0, self.cfg.tick_seconds - elapsed)
            if self.uses_pwm and self.cfg.hold_interval > 0:
                deadline = time.monotonic() + remaining
                while True:
                    nap = min(self.cfg.hold_interval, deadline - time.monotonic())
                    if nap <= 0:
                        break
                    time.sleep(nap)
                    self.apply_duty(self.current_pct or self.cfg.min_duty_pct)
            elif remaining:
                time.sleep(remaining)

    def close(self) -> None:
        if self.ahb is not None:
            self.ahb.close()
            self.ahb = None


class _Terminated(BaseException):
    """SIGTERM arrived; unwind so the fans get released."""


def run_daemon(config_path: str = CONFIG_PATH, mapping_path: str = MAPPING_PATH,
               pci_bdf: str | None = None) -> int:
    cfg = load_config(config_path)
    if pci_bdf is not None:
        cfg.pci_bdf = normalize_bdf(pci_bdf)
    ctl = Controller(cfg, load_mapping(mapping_path))

    def _on_term(signum: int, _frame: object) -> None:
        raise _Terminated(signal.Signals(signum).name)

    previous = None
    try:
        previous = signal.signal(signal.SIGTERM, _on_term)
        ctl.run()
    except (KeyboardInterrupt, _Terminated) as exc:
        LOG.info("%s; releasing fans", exc.args[0] if exc.args else "interrupted")
        ctl.release()
        return 0
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        ctl.close()
    return 0


def release(mapping_path: str = MAPPING_PATH, config_path: str = CONFIG_PATH,
            pci_bdf: str | None = None) -> dict[str, int]:
    """Restore only owned channels; no guessed factory fallback is safe."""
    bdf = normalize_bdf(pci_bdf) if pci_bdf is not None else load_config(config_path).pci_bdf
    # Refuse missing/corrupt state before opening the bridge, then re-read
    # under its lock to serialize with manual commands and the daemon.
    _active_baseline(_baseline_state(), bdf, required=True)
    with Ahb(bar_path(bdf), lock_timeout=DAEMON_LOCK_WAIT) as ahb:
        ahb.ensure_bridge()
        return _restore_baseline(pwm.Pwm(ahb), bdf)


def force_failsafe(mapping_path: str = MAPPING_PATH, config_path: str = CONFIG_PATH,
                   pci_bdf: str | None = None) -> dict[str, int]:
    """Emergency actuation honors inject mode, with IPMI fallback if P2A fails."""
    duty = DEFAULTS["failsafe_duty_pct"]
    inject_c = DEFAULTS["failsafe_inject_c"]
    bdf = normalize_bdf(pci_bdf) if pci_bdf is not None else DEFAULT_BDF
    explicit: list[str] | None = None
    uses_pwm = False
    try:
        cfg = load_config(config_path)
        duty = cfg.failsafe_duty_pct
        inject_c = cfg.failsafe_inject_c
        explicit = cfg.write_channels
        uses_pwm = cfg.mode == "pwm"
        if pci_bdf is None:
            bdf = cfg.pci_bdf
    except Exception as exc:
        LOG.warning("config invalid (%s); trying its actuator identity with emergency defaults", exc)
        try:
            with open(config_path, "rb") as fh:
                raw = tomllib.load(fh)
            mode = raw.get("mode", DEFAULTS["mode"])
            if mode not in ("pwm", "inject"):
                raise ValueError("invalid emergency mode")
            target = normalize_bdf(pci_bdf if pci_bdf is not None else raw.get("pci_bdf", DEFAULT_BDF))
            uses_pwm = mode == "pwm"
            bdf = target
        except Exception:
            LOG.warning("no trustworthy actuator identity; using sensor injection without P2A")
    if not uses_pwm:
        failures = _inject_sensors(inject_c)
        if failures:
            raise RuntimeError(f"required failsafe sensor injection failed: {failures}")
        return {}
    try:
        mapping = load_mapping(mapping_path)
    except Exception as exc:
        LOG.warning("mapping unreadable (%s); using all driven channels", exc)
        mapping = {}
    value = pwm.pct_to_byte(duty)
    try:
        with Ahb(bar_path(bdf), lock_timeout=DAEMON_LOCK_WAIT) as ahb:
            ahb.ensure_bridge()
            p = pwm.Pwm(ahb)
            channels = writable_channels(mapping, p, explicit)
            if not channels:
                raise RuntimeError("no writable PWM channels for failsafe")
            try:
                capture_manual_baseline(p, channels, bdf)
            except Exception:
                # Emergency cooling must not depend on runtime-state health.
                # Keep corrupt state intact so a later release refuses to guess.
                LOG.exception("cannot preserve baseline; proceeding with emergency PWM cooling")
            applied = {}
            for ch in channels:
                p.set_fall(ch, value)
                applied[ch] = p.get_fall(ch)
                if applied[ch] != value:
                    raise RuntimeError(f"PWM{ch} failsafe duty did not stick")
        return applied
    except Exception as exc:
        LOG.critical("failsafe via P2A failed (%s); injecting %g C instead", exc, inject_c)
        failures = _inject_sensors(inject_c)
        if len(failures) == len(INJECT_SENSORS):
            raise RuntimeError("all emergency sensor injection attempts failed") from exc
        return {}


def stop_post(mapping_path: str = MAPPING_PATH, config_path: str = CONFIG_PATH,
              pci_bdf: str | None = None) -> int:
    result = os.environ.get("SERVICE_RESULT", "unknown")
    if result == "success":
        cfg = load_config(config_path)
        applied = {}
        if cfg.mode == "pwm":
            bdf = normalize_bdf(pci_bdf) if pci_bdf is not None else cfg.pci_bdf
            state = _baseline_state()
            baseline = _active_baseline(state, bdf)
            entry = state.get("devices", {}).get(bdf)
            if baseline or entry is None:
                applied = release(mapping_path, config_path, bdf)
        LOG.info("clean stop (%s); restored duty %s", result, applied)
    else:
        applied = force_failsafe(mapping_path, config_path, pci_bdf)
        LOG.critical(
            "unclean exit (SERVICE_RESULT=%s); forced failsafe duty %s",
            result,
            applied,
        )
    return 0
