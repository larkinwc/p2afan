#!/usr/bin/env python3
"""Step 3 evidence script: prove host-side PWM writes move the fans, and
characterise whether the BMC fights back.

Not part of the daemon. Run as root with the tyanfan service stopped:

    sudo PYTHONPATH=/opt/tyanfan python3 /opt/tyanfan/re/actuation_test.py

Sequence: snapshot duty registers -> raise every enabled channel A..F to 0x66
(40 %, always above the factory 0x33 idle, so this can only add airflow) ->
poll duty registers every 250 ms and fan RPM every 2 s for 60 s -> restore the
exact snapshot and confirm readback.
"""

from __future__ import annotations

import sys
import time

from tyanfan import ipmi, pwm
from tyanfan.ahb import Ahb

TARGET = 0x66
DURATION = 60.0
REG_POLL = 0.25
RPM_POLL = 2.0
CHANNELS = ("A", "B", "C", "D", "E", "F")


def main() -> int:
    with Ahb() as ahb:
        ahb.ensure_bridge()
        p = pwm.Pwm(ahb)
        snap = p.snapshot()
        chans = [ch for ch in CHANNELS if p.enabled(ch)]
        base_fall = {ch: p.get_fall(ch) for ch in pwm.ALL_CHANNELS}
        base_rpm = ipmi.read_all_fans()
        print("# duty snapshot: " + " ".join(f"{r:#04x}={v:#010x}" for r, v in snap.items()))
        print("# baseline fall: " + " ".join(f"{c}={v:#04x}" for c, v in base_fall.items()))
        print("# baseline rpm : " + " ".join(f"{k}={v}" for k, v in base_rpm.items()))
        print(f"# raising {','.join(chans)} to {TARGET:#04x} for {DURATION:.0f}s")

        reverted_at: float | None = None
        max_rpm = {k: (v or 0) for k, v in base_rpm.items()}
        t0 = time.monotonic()
        try:
            for ch in chans:
                p.set_fall(ch, TARGET)
            print(
                f"{'t':>6}  {'DUTY0':>10} {'DUTY1':>10} {'DUTY2':>10} {'DUTY3':>10}"
                "  fan rpm"
            )
            next_rpm = t0
            while True:
                now = time.monotonic()
                if now - t0 >= DURATION:
                    break
                regs = [p.read(r) for r in pwm.DUTY_REGS]
                falls = {ch: p.get_fall(ch) for ch in chans}
                bad = [ch for ch, v in falls.items() if v != TARGET]
                rpm_txt = ""
                if now >= next_rpm:
                    rpms = ipmi.read_all_fans()
                    for k, v in rpms.items():
                        if v is not None and v > max_rpm.get(k, 0):
                            max_rpm[k] = v
                    rpm_txt = " ".join(f"{k.split('_')[-1]}={v}" for k, v in rpms.items())
                    next_rpm = time.monotonic() + RPM_POLL
                if bad and reverted_at is None:
                    reverted_at = now - t0
                    print(
                        f"!! BMC override after {reverted_at:.2f}s: "
                        + " ".join(f"{c}={falls[c]:#04x}" for c in bad)
                    )
                if rpm_txt or bad:
                    print(
                        f"{now - t0:6.2f}  "
                        + " ".join(f"{r:#010x}" for r in regs)
                        + f"  {rpm_txt}"
                    )
                time.sleep(REG_POLL)
        finally:
            p.restore(snap)
            after = {r: p.read(r) for r in pwm.DUTY_REGS}
            print("# restored: " + " ".join(f"{r:#04x}={v:#010x}" for r, v in after.items()))
            ok = after == snap
            print(f"# restore_exact={ok}")
            time.sleep(12)
            final_rpm = ipmi.read_all_fans()
            print("# rpm after restore: " + " ".join(f"{k}={v}" for k, v in final_rpm.items()))

        print("# peak rpm: " + " ".join(f"{k}={v}" for k, v in max_rpm.items()))
        print(
            "# rpm delta: "
            + " ".join(
                f"{k}=+{max_rpm[k] - (base_rpm.get(k) or 0)}" for k in max_rpm
            )
        )
        if reverted_at is None:
            print("# RESULT: duty held for the full window; no BMC override observed")
        else:
            print(f"# RESULT: BMC reverted duty after {reverted_at:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
