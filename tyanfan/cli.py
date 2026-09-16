"""tyanfan command line entrypoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time

from . import control, ipmi, pwm
from .ahb import Ahb, BridgeBusy, BridgeUnavailable

FLASH_BASE = 0x20000000
FLASH_SIZE = 0x1000000
WINDOW_SIZE = 0x10000


def _log_setup(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )


def _need_root() -> None:
    if os.geteuid() != 0:
        raise SystemExit("tyanfan: must run as root (needs PCI resource mmap)")


def _session(args: argparse.Namespace | None = None) -> Ahb:
    """Open a P2A session, waiting a bounded time for the daemon's lock."""
    _need_root()
    timeout = getattr(args, "lock_wait", 3.0) if args is not None else 3.0
    ahb = Ahb(lock_timeout=timeout)
    try:
        ahb.ensure_bridge()
    except BridgeUnavailable:
        ahb.close()
        raise
    return ahb


# -- commands ------------------------------------------------------------
def cmd_pwm_dump(args: argparse.Namespace) -> int:
    with _session(args) as ahb:
        p = pwm.Pwm(ahb)
        print(f"# ASPEED PWM/tach block @ {pwm.PWM_BASE:#010x} via P2A")
        print(f"# p2a window (BAR1+0xf004) on entry = {ahb.orig_window:#010x}")
        for name, off, value in p.dump():
            print(f"{name:<13} +{off:#04x}  {value:#010x}")
        print("# channel  enabled  rise  fall  duty%")
        for ch in pwm.ALL_CHANNELS:
            print(
                f"PWM{ch}       {'yes' if p.enabled(ch) else 'no ':>5}"
                f"   {p.get_rise(ch):#04x}  {p.get_fall(ch):#04x}"
                f"  {pwm.byte_to_pct(p.get_fall(ch)):5.1f}"
            )
        print(
            "# tach channels enabled: "
            + ",".join(str(i) for i in p.tach_channels())
        )
    return 0


def cmd_sensors(args: argparse.Namespace) -> int:
    print("# name                 sensor  value")
    for name, num in ipmi.SENSORS.items():
        value = ipmi.read_temp(num)
        print(f"{name:<20} {num:#04x}    " + ("n/a" if value is None else f"{value} C"))
    for name, num in ipmi.FAN_SENSORS.items():
        rpm = ipmi.read_fan_rpm(num)
        print(f"{name:<20} {num:#04x}    " + ("n/a" if rpm is None else f"{rpm} RPM"))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    with _session(args) as ahb:
        p = pwm.Pwm(ahb)
        for ch in pwm.parse_channels(args.channels):
            print(
                f"PWM{ch} fall={p.get_fall(ch):#04x} "
                f"duty={pwm.byte_to_pct(p.get_fall(ch)):.1f}% "
                f"enabled={p.enabled(ch)}"
            )
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    with _session(args) as ahb:
        p = pwm.Pwm(ahb)
        chans = pwm.parse_channels(args.channels, default=p.enabled_channels())
        value = pwm.pct_to_byte(args.pct)
        for ch in chans:
            p.set_fall(ch, value)
        for ch in chans:
            print(f"PWM{ch} fall={p.get_fall(ch):#04x} ({pwm.byte_to_pct(p.get_fall(ch)):.1f}%)")
    return 0


def cmd_set_raw(args: argparse.Namespace) -> int:
    value = int(args.value, 0)
    with _session(args) as ahb:
        p = pwm.Pwm(ahb)
        chans = pwm.parse_channels(args.channels, default=p.enabled_channels())
        for ch in chans:
            p.set_fall(ch, value)
        for ch in chans:
            print(f"PWM{ch} fall={p.get_fall(ch):#04x}")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    _need_root()
    applied = control.release()
    print("restored: " + " ".join(f"PWM{c}={v:#04x}" for c, v in applied.items()))
    return 0


def cmd_failsafe(args: argparse.Namespace) -> int:
    _need_root()
    applied = control.force_failsafe()
    print("failsafe: " + " ".join(f"PWM{c}={v:#04x}" for c, v in applied.items()))
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    _need_root()
    path = args.out
    if os.path.exists(path) and not args.force:
        raise SystemExit(f"tyanfan map: {path} exists; pass --force to overwrite")
    with _session(args) as ahb:
        p = pwm.Pwm(ahb)
        channels = [ch for ch in pwm.parse_channels(args.channels, default=list("ABCDEFG")) if p.enabled(ch)]
        base_fall = {ch: p.get_fall(ch) for ch in pwm.ALL_CHANNELS}
        print(f"# baseline duty: " + " ".join(f"PWM{c}={v:#04x}" for c, v in base_fall.items()))
        base_rpm = ipmi.read_all_fans()
        print("# baseline rpm: " + " ".join(f"{k}={v}" for k, v in base_rpm.items()))
        probe = pwm.pct_to_byte(args.pct)
        result: dict[str, list[str]] = {ch: [] for ch in pwm.ALL_CHANNELS}
        try:
            for ch in channels:
                p.set_fall(ch, probe)
                time.sleep(args.settle)
                rpms = ipmi.read_all_fans()
                p.set_fall(ch, base_fall[ch])
                hits = []
                for fan, rpm in rpms.items():
                    ref = base_rpm.get(fan)
                    if rpm is not None and ref is not None and rpm - ref >= args.threshold:
                        hits.append(fan)
                result[ch] = hits
                print(
                    f"PWM{ch} -> {probe:#04x}: "
                    + " ".join(
                        f"{k}={v}({'+' if (v or 0) - (base_rpm.get(k) or 0) >= 0 else ''}{(v or 0) - (base_rpm.get(k) or 0)})"
                        for k, v in rpms.items()
                    )
                    + f"  attributed={hits or '[]'}"
                )
                time.sleep(args.settle)
        finally:
            for ch, value in base_fall.items():
                if p.enabled(ch):
                    p.set_fall(ch, value)

    claimed: dict[str, str] = {}
    lines = ["# generated by `tyanfan map`", "[channels]"]
    for ch in pwm.ALL_CHANNELS:
        fans = result.get(ch, [])
        for fan in fans:
            if fan in claimed:
                print(
                    f"WARNING: {fan} responded to both PWM{claimed[fan]} and PWM{ch}",
                    file=sys.stderr,
                )
            else:
                claimed[fan] = ch
        if ch in channels or fans:
            lines.append(f"{ch} = [" + ", ".join(f'"{f}"' for f in fans) + "]")
            if not fans:
                print(f"WARNING: PWM{ch} drives no monitored tach", file=sys.stderr)
    unclaimed = [f for f in ipmi.FAN_SENSORS if f not in claimed]
    if unclaimed:
        print(f"WARNING: unattributed fans: {','.join(unclaimed)}", file=sys.stderr)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"wrote {path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = control.read_state()
    if state is None:
        print("no state file at " + control.STATE_PATH)
        return 1
    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0
    age = time.time() - state.get("ts", 0)
    print(f"mode        {state.get('mode')} (inject={state.get('inject')})")
    print(f"duty        {state.get('duty_pct')}% ({state.get('duty_byte', 0):#04x})")
    print(f"channels    {','.join(state.get('channels', []))}")
    print(f"failsafe    {state.get('failsafe')}")
    print(f"overrides   {state.get('overrides')}")
    print(f"state age   {age:.1f}s")
    for name, zone in (state.get("zones") or {}).items():
        readings = " ".join(
            f"{k}={v}" for k, v in (zone.get("readings") or {}).items()
        )
        print(
            f"zone {name:<10} temp={zone.get('temp_c')} "
            f"curve={zone.get('duty_pct')}%  {readings}"
        )
    print(
        "fans        "
        + " ".join(f"{k}={v}" for k, v in (state.get("fan_rpm") or {}).items())
    )
    print(
        "baseline    "
        + " ".join(
            f"PWM{k}={v:#04x}" for k, v in (state.get("baseline_duty") or {}).items()
        )
    )
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    _need_root()
    return control.run_daemon(args.config, args.mapping)


def cmd_stop_post(args: argparse.Namespace) -> int:
    _need_root()
    return control.stop_post(args.mapping)


def cmd_dump_flash(args: argparse.Namespace) -> int:
    _need_root()
    out = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    started = time.monotonic()
    with _session(args) as ahb:
        with open(out, "wb") as fh:
            for offset in range(0, FLASH_SIZE, WINDOW_SIZE):
                fh.write(ahb.read_block(FLASH_BASE + offset, WINDOW_SIZE))
                if offset and offset % (WINDOW_SIZE * 32) == 0:
                    print(
                        f"  {offset // 1024:>6} KiB  "
                        f"{time.monotonic() - started:5.1f}s",
                        file=sys.stderr,
                    )
            fh.flush()
            os.fsync(fh.fileno())
        first = ahb.read_block(FLASH_BASE, WINDOW_SIZE)
        last = ahb.read_block(FLASH_BASE + FLASH_SIZE - WINDOW_SIZE, WINDOW_SIZE)
    data = open(out, "rb").read()
    print(f"size   {len(data)} bytes in {time.monotonic() - started:.1f}s")
    print(f"sha256 {hashlib.sha256(data).hexdigest()}")
    stable = data[:WINDOW_SIZE] == first and data[-WINDOW_SIZE:] == last
    print(f"stable {stable} (re-read of first/last 64 KiB matches)")
    return 0 if stable and len(data) == FLASH_SIZE else 1


def cmd_ahb_read(args: argparse.Namespace) -> int:
    with _session(args) as ahb:
        addr = int(args.addr, 0)
        for i in range(args.count):
            print(f"{addr + 4 * i:#010x}: {ahb.read32(addr + 4 * i):#010x}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tyanfan", description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument(
        "--lock-wait",
        type=float,
        default=3.0,
        metavar="SECONDS",
        help="seconds to wait for the P2A lock held by a running daemon",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pwm-dump", help="dump ASPEED PWM/tach registers").set_defaults(
        func=cmd_pwm_dump
    )
    sub.add_parser("sensors", help="read all known BMC sensors").set_defaults(
        func=cmd_sensors
    )

    p = sub.add_parser("get", help="read duty of PWM channels")
    p.add_argument("--channels")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("set", help="set duty percent on PWM channels")
    p.add_argument("pct", type=float)
    p.add_argument("--channels")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("set-raw", help="set raw fall byte on PWM channels")
    p.add_argument("value")
    p.add_argument("--channels")
    p.set_defaults(func=cmd_set_raw)

    sub.add_parser("release", help="restore baseline duty, BMC resumes").set_defaults(
        func=cmd_release
    )
    sub.add_parser("failsafe", help="drive mapped channels to failsafe duty").set_defaults(
        func=cmd_failsafe
    )

    p = sub.add_parser("map", help="discover channel -> fan mapping")
    p.add_argument("--out", default=control.MAPPING_PATH)
    p.add_argument("--channels")
    p.add_argument("--pct", type=float, default=60.0)
    p.add_argument("--settle", type=float, default=10.0)
    p.add_argument("--threshold", type=int, default=400)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("status", help="show last daemon state")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("daemon", help="run the control loop")
    p.add_argument("--config", default=control.CONFIG_PATH)
    p.add_argument("--mapping", default=control.MAPPING_PATH)
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("stop-post", help="systemd ExecStopPost handler")
    p.add_argument("--mapping", default=control.MAPPING_PATH)
    p.set_defaults(func=cmd_stop_post)

    p = sub.add_parser("dump-flash", help="dump the 16 MiB BMC SPI flash over P2A")
    p.add_argument("--out", default="/opt/tyanfan/re/flash-16m.bin")
    p.set_defaults(func=cmd_dump_flash)

    p = sub.add_parser("ahb-read", help="read AHB dwords")
    p.add_argument("addr")
    p.add_argument("--count", type=int, default=1)
    p.set_defaults(func=cmd_ahb_read)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _log_setup(args.verbose)
    try:
        return args.func(args)
    except BridgeBusy as exc:
        raise SystemExit(f"tyanfan: {exc}") from None
    except BrokenPipeError:
        # Piped into head/less: drop stdout so Python does not re-raise at exit.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
