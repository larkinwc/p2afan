"""Control loop, failsafe, watchdog, override detection, release."""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import time
import tomllib

from . import ipmi, pwm
from .ahb import RUN_DIR, Ahb
from .curve import Curve, Zone
from .sources import build as build_source

CONFIG_PATH = "/etc/p2afan/config.toml"
MAPPING_PATH = "/etc/p2afan/mapping.toml"
STATE_PATH = RUN_DIR + "/state.json"

# Bounded so a stuck holder surfaces as a service failure instead of a hang.
DAEMON_LOCK_WAIT = 30.0

LOG = logging.getLogger("p2afan")

# Sensors written in inject mode: the factory curve's NVIDIA GPU inputs.
INJECT_SENSORS = (0x20, 0x22, 0x24, 0x26)

DEFAULTS = {
    "mode": "pwm",
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


class Config:
    def __init__(self, data: dict) -> None:
        opts = dict(DEFAULTS)
        for key in DEFAULTS:
            if key in data:
                opts[key] = data[key]
        self.mode = str(opts["mode"])
        if self.mode not in ("pwm", "inject"):
            raise ValueError(f"mode must be 'pwm' or 'inject', got {self.mode!r}")
        self.inject = bool(opts["inject"])
        self.tick_seconds = float(opts["tick_seconds"])
        self.min_duty_pct = float(opts["min_duty_pct"])
        self.failsafe_duty_pct = float(opts["failsafe_duty_pct"])
        self.down_slew_pct_per_tick = float(opts["down_slew_pct_per_tick"])
        self.hold_interval = float(opts["hold_interval"])
        self.source_fail_limit = int(opts["source_fail_limit"])
        self.min_fan_rpm = int(opts["min_fan_rpm"])
        self.recover_ticks = int(opts["recover_ticks"])
        self.failsafe_inject_c = float(opts["failsafe_inject_c"])
        self.write_channels = [str(c).upper() for c in opts["write_channels"]]
        for ch in self.write_channels:
            if ch not in pwm.CHANNELS:
                raise ValueError(f"write_channels has unknown channel {ch!r}")
        zones = data.get("zone", [])
        if not zones:
            raise ValueError("config has no [[zone]] entries")
        self.zones = [
            Zone(
                name=z["name"],
                sources=[build_source(s) for s in z.get("sources", [])],
                curve=Curve(
                    [tuple(p) for p in z["curve"]],
                    hysteresis_c=float(z.get("hysteresis_c", 0.0)),
                ),
                critical_c=float(z["critical_c"]),
            )
            for z in zones
        ]
        for zone in self.zones:
            if not zone.sources:
                raise ValueError(f"zone {zone.name!r} has no sources")


def load_config(path: str = CONFIG_PATH) -> Config:
    with open(path, "rb") as fh:
        return Config(tomllib.load(fh))


def load_mapping(path: str = MAPPING_PATH) -> dict[str, list[str]]:
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return {}
    return {
        ch.upper(): list(fans)
        for ch, fans in (data.get("channels") or {}).items()
        if ch.upper() in pwm.CHANNELS
    }


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


def write_state(state: dict, path: str = STATE_PATH) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def read_state(path: str = STATE_PATH) -> dict | None:
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


def choose_baseline(
    live: dict[str, int], prev_state: dict | None
) -> dict[str, int]:
    """Pick the duty bytes to hand back to the BMC on release.

    The live registers are the factory values only on a cold start. After a
    crash-restart they are whatever ExecStopPost left behind (failsafe 0xff),
    so latching them would make the next clean stop strand the fans at 100 %.
    /run/p2afan/state.json is boot-scoped, so a baseline recorded there came
    from a run that started before us and is the better answer; a reboot wipes
    it and the live registers are genuinely factory again.
    """
    recorded = (prev_state or {}).get("baseline_duty")
    if not isinstance(recorded, dict) or not recorded:
        return dict(live)
    out = {}
    for ch, value in live.items():
        try:
            out[ch] = int(recorded[ch])
        except (KeyError, TypeError, ValueError):
            out[ch] = value
    return out


class Controller:
    def __init__(self, config: Config, mapping: dict[str, list[str]]) -> None:
        self.cfg = config
        self.mapping = mapping
        self.uses_pwm = config.mode == "pwm"
        self.ahb: Ahb | None = None
        self.pwm: pwm.Pwm | None = None
        self.channels: list[str] = []
        self.baseline: dict[str, int] = {}
        if self.uses_pwm:
            # Inject mode deliberately never touches the bridge: it is the
            # fallback for a BMC that has locked P2A, so requiring the bridge
            # there would defeat its purpose.
            self.ahb = Ahb(lock_timeout=DAEMON_LOCK_WAIT)
            self.ahb.ensure_bridge()
            self.pwm = pwm.Pwm(self.ahb)
            self.channels = writable_channels(
                mapping, self.pwm, config.write_channels
            )
            if not self.channels:
                raise RuntimeError("no writable PWM channels; check mapping.toml")
            self.baseline = self._capture_baseline()
        self.fans = self._watched_fans()
        self.current_pct: float | None = None
        self.overrides = 0
        self.failsafe = False
        self.cool_ticks = 0
        self.low_rpm_strikes: dict[str, int] = {}
        self.ready = False
        LOG.info(
            "mode=%s inject=%s channels=%s baseline=%s watched_fans=%s",
            self.cfg.mode,
            self.cfg.inject,
            ",".join(self.channels) or "none",
            {k: hex(v) for k, v in self.baseline.items()} or "n/a",
            ",".join(self.fans) or "none",
        )

    def _capture_baseline(self) -> dict[str, int]:
        live = {ch: self.pwm.get_fall(ch) for ch in pwm.ALL_CHANNELS}
        baseline = choose_baseline(live, read_state())
        if baseline != live:
            LOG.warning(
                "inheriting pre-takeover baseline %s from %s (live regs are %s)",
                {k: hex(v) for k, v in baseline.items()},
                STATE_PATH,
                {k: hex(v) for k, v in live.items()},
            )
        return baseline

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
        raw = max(0, min(255, int(round(temp))))
        for sensor in INJECT_SENSORS:
            try:
                ipmi.set_sensor_reading(sensor, raw)
            except Exception as exc:
                LOG.warning("inject sensor %#04x failed: %s", sensor, exc)

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
            if feed is not None:
                self.inject_temps(feed)
        return value

    def release(self) -> None:
        if not self.uses_pwm or self.pwm is None:
            LOG.info("inject mode: no duty registers were taken over")
            return
        for ch, value in self.baseline.items():
            if self.pwm.enabled(ch):
                self.pwm.set_fall(ch, value)
        LOG.info(
            "released fans to baseline duty %s; BMC thermal loop resumes",
            {k: hex(v) for k, v in self.baseline.items()},
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

        if critical or reason is not None:
            if not self.failsafe:
                LOG.critical("entering failsafe: %s", reason)
            self.failsafe = True
            self.cool_ticks = 0
            duty_pct = self.cfg.failsafe_duty_pct
        else:
            if self.failsafe:
                self.cool_ticks += 1
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

        rpms = {name: ipmi.read_fan_rpm(ipmi.FAN_SENSORS[name]) for name in self.fans}
        for name, rpm in rpms.items():
            if rpm is not None and rpm < self.cfg.min_fan_rpm:
                self.low_rpm_strikes[name] = self.low_rpm_strikes.get(name, 0) + 1
                if self.low_rpm_strikes[name] >= 2 and not self.failsafe:
                    LOG.critical(
                        "%s stalled at %d RPM for 2 ticks; forcing failsafe", name, rpm
                    )
                    self.failsafe = True
                    self.cool_ticks = 0
                    duty_pct = self.cfg.failsafe_duty_pct
                    value = self.actuate(duty_pct, hottest, True)
                    self.current_pct = duty_pct
            else:
                self.low_rpm_strikes[name] = 0

        state = {
            "ts": time.time(),
            "mode": self.cfg.mode,
            "inject": self.cfg.inject,
            "duty_pct": round(duty_pct, 1),
            "duty_byte": value,
            "channels": self.channels,
            "zones": zone_state,
            "fan_rpm": rpms,
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


def run_daemon(config_path: str = CONFIG_PATH, mapping_path: str = MAPPING_PATH) -> int:
    cfg = load_config(config_path)
    ctl = Controller(cfg, load_mapping(mapping_path))

    def _on_term(signum: int, _frame: object) -> None:
        raise _Terminated(signal.Signals(signum).name)

    previous = signal.signal(signal.SIGTERM, _on_term)
    try:
        ctl.run()
    except (KeyboardInterrupt, _Terminated) as exc:
        LOG.info("%s; releasing fans", exc.args[0] if exc.args else "interrupted")
        ctl.release()
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)
        ctl.close()
    return 0


def release(mapping_path: str = MAPPING_PATH) -> dict[str, int]:
    """Restore the pre-takeover duty bytes and hand control back to the BMC."""
    state = read_state()
    baseline = None
    if state and isinstance(state.get("baseline_duty"), dict):
        baseline = {
            ch: int(v)
            for ch, v in state["baseline_duty"].items()
            if ch in pwm.CHANNELS
        }
    if not baseline:
        baseline = dict(pwm.FACTORY_FALL)
        LOG.warning("no runtime baseline; restoring factory duty bytes")
    with Ahb(lock_timeout=DAEMON_LOCK_WAIT) as ahb:
        ahb.ensure_bridge()
        p = pwm.Pwm(ahb)
        applied = {}
        for ch, value in sorted(baseline.items()):
            if p.enabled(ch):
                p.set_fall(ch, value)
                applied[ch] = p.get_fall(ch)
    return applied


def force_failsafe(mapping_path: str = MAPPING_PATH) -> dict[str, int]:
    """Drive every channel we own to failsafe duty.

    If the bridge is gone (or busy), fall back to injecting a hot reading into
    the BMC's GPU sensors so the factory curve ramps instead of nothing at all.
    """
    duty = 100.0
    inject_c = DEFAULTS["failsafe_inject_c"]
    explicit: list[str] | None = None
    try:
        cfg = load_config()
        duty = cfg.failsafe_duty_pct
        inject_c = cfg.failsafe_inject_c
        explicit = cfg.write_channels
    except Exception:
        LOG.warning("config unreadable; using 100%% failsafe on all driven channels")
    value = pwm.pct_to_byte(duty)
    mapping = load_mapping(mapping_path)
    try:
        with Ahb(lock_timeout=DAEMON_LOCK_WAIT) as ahb:
            ahb.ensure_bridge()
            p = pwm.Pwm(ahb)
            applied = {}
            for ch in writable_channels(mapping, p, explicit):
                p.set_fall(ch, value)
                applied[ch] = p.get_fall(ch)
        return applied
    except Exception as exc:
        LOG.critical("failsafe via P2A failed (%s); injecting %g C instead", exc, inject_c)
        for sensor in INJECT_SENSORS:
            try:
                ipmi.set_sensor_reading(sensor, int(inject_c))
            except Exception as inner:
                LOG.critical("failsafe injection on %#04x failed: %s", sensor, inner)
        return {}


def stop_post(mapping_path: str = MAPPING_PATH) -> int:
    result = os.environ.get("SERVICE_RESULT", "unknown")
    if result == "success":
        applied = release(mapping_path)
        LOG.info("clean stop (%s); restored duty %s", result, applied)
    else:
        applied = force_failsafe(mapping_path)
        LOG.critical(
            "unclean exit (SERVICE_RESULT=%s); forced failsafe duty %s",
            result,
            applied,
        )
    return 0
