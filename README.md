# p2afan

Host-side fan controller for ASPEED AST2400/AST2500 BMCs, driven by **any**
temperature source.

Your BMC decides fan speed from the sensors *it* can see. If you add hardware it
cannot read — a GPU it has no thermal probe for, an accelerator, an NVMe shelf,
anything behind a riser — that hardware contributes nothing to the fan curve and
the vendor firmware will happily idle the fans while it cooks.

`p2afan` fixes that from the host side. It writes the AST2400 PWM duty registers
directly over the ASPEED **PCIe-to-AHB (P2A) bridge**, computing duty from
temperature sources you configure: BMC sensors over IPMI, any `hwmon` node, any
PCI device's `hwmon`, or the stdout of an arbitrary command.

It never disables a PWM channel and never touches the PWM control or clock
registers, so **the BMC's own thermal loop stays alive underneath** and resumes
authority the instant the duty bytes are restored. There is no firmware
modification, no flashing, and no kernel module.

Verified on a **Tyan FT77C-B7079 / S7079GM2NR-N** (AST2400, Tyan firmware 9.01,
eight GPUs, six chassis fans). See [`docs/bmc-re.md`](docs/bmc-re.md) for the
full reverse-engineering writeup.

> [!WARNING]
> This drives the fans of a live machine by poking BMC registers from the host.
> Read [Safety](#safety) before running it on anything you care about. The
> measured values in this repo are from one chassis; re-derive yours with
> `p2afan pwm-dump` and `p2afan map`.

## Does it work on my machine?

You need an ASPEED BMC whose VGA function exposes BAR1, with the P2A bridge
enabled and unlocked. Check in three commands:

```sh
lspci -d 1a03:                      # find the ASPEED function, e.g. 0c:00.0
sudo p2afan pwm-dump                # should print live PWM/tach registers
sudo p2afan sensors                 # should print BMC temps and fan RPM
```

If `pwm-dump` prints plausible registers (a non-zero `CTRL` with bit 0 set, duty
bytes that match your fans' idle), you're in business. If AHB reads come back as
`0x00000000`/`0xffffffff`, your BMC has locked P2A and this tool cannot reach it.

## Install

No packaging, no dependencies — Python 3.11+ standard library only (`tomllib`).

```sh
sudo git clone https://github.com/<you>/p2afan /opt/p2afan
sudo mkdir -p /etc/p2afan
sudo cp /opt/p2afan/config/config.toml /etc/p2afan/config.toml
sudo /opt/p2afan/bin/p2afan map              # discover channel -> fan mapping
sudo ln -s /opt/p2afan/systemd/p2afan.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now p2afan
```

Edit `/etc/p2afan/config.toml` first if your chassis differs from the reference
one — in particular the PCI address of the ASPEED function in `p2afan/ahb.py`
(`BAR`) and the zone curves.

## Configuration

Zones are independent curves; the **highest** duty any zone asks for wins.
Within a zone, the **hottest** valid source wins. Nothing is ever averaged.

```toml
mode = "pwm"                 # "pwm" writes duty registers; "inject" feeds BMC sensors
tick_seconds = 5.0
min_duty_pct = 20            # never quieter than the factory idle
failsafe_duty_pct = 100
down_slew_pct_per_tick = 3   # rise instantly, fall gently
write_channels = []          # empty = auto-detect from mapping.toml + factory-driven

[[zone]]
name = "accelerator"
critical_c = 85              # immediate failsafe, bypassing curve and slew
hysteresis_c = 3             # suppress hunting around a knee
curve = [[50, 30], [65, 50], [78, 80], [85, 100]]
sources = [
  { kind = "ipmi", name = "GPU0",  sensor = 0x20 },
  { kind = "pci",  name = "V340",  bdf = "0000:41:00.0" },
  { kind = "hwmon", name = "NVME", path = "/sys/class/hwmon/hwmon3/temp1_input" },
  { kind = "exec", name = "CUSTOM", command = ["/usr/local/bin/mytemp"] },
]
```

| Source kind | Reads | Use for |
|---|---|---|
| `ipmi` | `ipmitool raw 0x04 0x2d <sensor>` (~45 ms) | anything the BMC already sees |
| `hwmon` | a `temp*_input` file | CPU package, NVMe, board sensors |
| `pci` | hottest `hwmon` input under a PCI device | **any card the BMC cannot see** |
| `exec` | first float on a command's stdout | everything else, including remote probes |

Every source returns "no reading" rather than `0` on failure, and a zone with no
valid reading goes straight to failsafe. Cold and unknown are never confused.

## CLI

```
p2afan pwm-dump          # PWM/tach registers + per-channel duty
p2afan sensors           # every known BMC sensor: temps and fan RPM
p2afan get               # current duty per channel
p2afan set <pct>         # write duty directly (stop the service first)
p2afan map               # discover channel -> fan mapping, write mapping.toml
p2afan status [--json]   # last daemon state; needs no P2A lock
p2afan release           # restore pre-takeover duty; BMC resumes control
p2afan failsafe          # slam every owned channel to failsafe duty
p2afan dump-flash        # dump the BMC SPI flash over P2A
p2afan ahb-read <addr>   # raw AHB dwords, for further RE
```

The daemon holds an exclusive lock on the P2A window for its lifetime (the AHB
window register is global state, so even reads must be serialised). Register
commands therefore fail after `--lock-wait` seconds while it runs; `p2afan
status` reads a JSON state file and never needs the lock.

## How it works

```
temperature sources          decision                     actuator
─────────────────────        ─────────                    ────────
ipmi / hwmon / pci / exec →  max per zone                 BAR1+0xf004 = AHB window base
                             piecewise-linear curve   →   BAR1+0x10000 = 64 KiB porthole
                             hysteresis + slew limit      → AHB 0x1E786000 DUTY regs
                             failsafe overrides all       (fall byte only, rise stays 0)
```

The ASPEED VGA function's BAR1 contains a bridge: write an AHB address to offset
`0xf004` and a 64 KiB window at offset `0x10000` maps that part of the BMC's
internal bus. Point it at `0x1E786000` and you have the PWM/tach controller;
point it at `0x20000000` and you have the SPI flash (which is how `dump-flash`
works). Access goes through the PCI sysfs resource, so `CONFIG_STRICT_DEVMEM`
does not apply.

Each duty register holds two channels, `[fall 15:8][rise 7:0]` per half. With
rise = 0, duty = `fall / 256`. Writes are read-modify-write on one 16-bit half so
the sibling channel is never disturbed.

## Safety

Designed so that every failure mode ends in *more* airflow, not less:

- **Duty floor** defaults to the factory idle, so the controller can only add
  airflow versus stock unless you deliberately lower `min_duty_pct`.
- **Critical trip** — any source at or above its zone's `critical_c` goes to
  `failsafe_duty_pct` on the same tick, bypassing curve and slew.
- **Sensor loss** — a zone with no valid reading, or a source failing 3 ticks
  running, trips failsafe.
- **Stall detection** — a mapped fan below `min_fan_rpm` for two ticks trips
  failsafe and logs CRITICAL.
- **Watchdog** — `Type=notify` with `WatchdogSec=30`; a hung loop is killed by
  systemd and lands in the unclean-exit path below.
- **Clean stop / SIGTERM** → restores the pre-takeover duty; the BMC resumes.
- **Crash, SIGKILL, watchdog kill** → `ExecStopPost` drives every owned channel
  to failsafe duty and logs CRITICAL.
- **Crash-restart** → the baseline is read from the boot-scoped state file, not
  from the live registers, so a restart after a failsafe cannot latch 100 % as
  "factory" and strand your fans there.
- **Never written**: PWM `CTRL`, `CTRL_EXT`, `CLK_CTRL`, `TYPE*`, channels parked
  at duty 0, and disabled channels.

## Reference platform results

Tyan FT77C-B7079, AST2400, firmware 9.01, six chassis fans on PWMD/E/F:

| Duty | Total fan RPM | Note |
|---|---|---|
| `0x33` 20 % | 18 540 | factory idle |
| `0x4c` 30 % | ~22 000 | typical `p2afan` idle |
| `0x99` 60 % | ~31 000 | |
| `0xff` 100 % | 38 790 | **2.09× factory airflow** |

Spin-up is audible in ~2 s and settled in ~4.5 s. Duty writes held for 60 s and
30 s tests with zero reverts — the BMC does not fight back on this firmware. If
yours does, set `hold_interval` to re-assert faster than it corrects.

## Limitations

- `mode = "inject"` (feed the BMC's own sensors instead of writing duty) is
  implemented but **inoperable on Tyan firmware 9.01**: every SDR record reports
  capabilities `0x68`, i.e. reading-not-settable, so `Set Sensor Reading` returns
  completion code `0x80`. It ships for firmware that does permit it, and it never
  touches the P2A bridge so it remains usable if a BMC locks P2A.
- The PCI address of the ASPEED function is a constant in `p2afan/ahb.py`.
- Tested on AST2400 only. AST2500 uses the same PWM block and P2A layout and
  should work; AST2600 moves things and is untested.

## License

MIT — see [LICENSE](LICENSE).
