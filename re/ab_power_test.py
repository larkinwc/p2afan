#!/usr/bin/env python3
"""On-site A/B test: factory idle duty vs 100 % duty, with tach evidence.

Run with the p2afan service stopped:

    sudo PYTHONPATH=/opt/p2afan python3 /opt/p2afan/re/ab_power_test.py

Phases: NORMAL (restore whatever duty was there on entry) -> FULL (0xff) ->
NORMAL again. Only ever raises airflow above the entry duty, and the entry
duty is restored exactly, so the BMC resumes control unchanged.
"""

from __future__ import annotations

import sys
import time

from p2afan import ipmi, pwm
from p2afan.ahb import Ahb

CHANNELS = ("A", "B", "C", "D", "E", "F")
SAMPLE = 2.0
PHASES = (
    ("NORMAL (entry duty)", None, 20.0),
    ("FULL POWER (100 %)", 0xFF, 30.0),
    ("NORMAL (restored)", None, 30.0),
)


def sample(tag: str, p: pwm.Pwm, t0: float) -> dict[str, int | None]:
    rpm = ipmi.read_all_fans()
    falls = {ch: p.get_fall(ch) for ch in CHANNELS}
    duty = set(falls.values())
    duty_txt = (
        f"{next(iter(duty)):#04x} ({pwm.byte_to_pct(next(iter(duty))):.0f}%)"
        if len(duty) == 1
        else " ".join(f"{c}={v:#04x}" for c, v in falls.items())
    )
    total = sum(v for v in rpm.values() if v)
    print(
        f"{time.monotonic() - t0:6.1f}s  {tag:<20} duty={duty_txt:<14} "
        + " ".join(f"{k.split('_')[-1]}={v}" for k, v in rpm.items())
        + f"  sum={total}"
    )
    sys.stdout.flush()
    return rpm


def main() -> int:
    with Ahb() as ahb:
        ahb.ensure_bridge()
        p = pwm.Pwm(ahb)
        snap = p.snapshot()
        entry = {ch: p.get_fall(ch) for ch in CHANNELS}
        print(f"# entry duty: " + " ".join(f"{c}={v:#04x}" for c, v in entry.items()))
        print(f"# PWMG={p.get_fall('G'):#04x} (untouched), PWMH disabled")
        t0 = time.monotonic()
        peaks: dict[str, dict[str, int]] = {}
        try:
            for label, target, duration in PHASES:
                print(f"\n### {label} ###")
                sys.stdout.flush()
                for ch in CHANNELS:
                    p.set_fall(ch, entry[ch] if target is None else target)
                best: dict[str, int] = {}
                deadline = time.monotonic() + duration
                while time.monotonic() < deadline:
                    rpm = sample(label.split()[0], p, t0)
                    for k, v in rpm.items():
                        if v is not None and v > best.get(k, 0):
                            best[k] = v
                    time.sleep(SAMPLE)
                peaks[label] = best
        finally:
            p.restore(snap)
            after = {r: p.read(r) for r in pwm.DUTY_REGS}
            print(
                "\n# restored: "
                + " ".join(f"{r:#04x}={v:#010x}" for r, v in after.items())
                + f"  exact={after == snap}"
            )

        print("\n# ---- summary ----")
        norm = peaks.get("NORMAL (entry duty)", {})
        full = peaks.get("FULL POWER (100 %)", {})
        print(f"# {'fan':<12}{'normal':>9}{'full':>9}{'delta':>9}{'ratio':>8}")
        for fan in sorted(ipmi.FAN_SENSORS):
            n, f = norm.get(fan, 0), full.get(fan, 0)
            ratio = f"{f / n:.2f}x" if n else "n/a"
            print(f"# {fan:<12}{n:>9}{f:>9}{f - n:>9}{ratio:>8}")
        ns, fs = sum(norm.values()), sum(full.values())
        print(f"# {'TOTAL':<12}{ns:>9}{fs:>9}{fs - ns:>9}{f'{fs / ns:.2f}x' if ns else '':>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
