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

## Requirements

| Need | Why | Check |
|---|---|---|
| ASPEED BMC (AST2400/2500) | the PWM block and P2A bridge live in it | `lspci -d 1a03:` shows a VGA function |
| P2A bridge enabled and unlocked | the only host path to the BMC's bus | `p2afan pwm-dump` (below) |
| root | BAR1 via PCI sysfs is mode `0600` | — |
| Python **3.11–3.14** (source install only) | stdlib-only runtime; selected `python3` must be 3.11+ | `python3 -V` |
| `ipmitool` + `/dev/ipmi0` | BMC sensors, fan mapping, RPM checks, and injection; needed even with a standalone binary | `sudo ipmitool sdr elist full` |

On Debian/Ubuntu:

```sh
sudo apt-get install -y ipmitool
sudo modprobe ipmi_si ipmi_devintf      # creates /dev/ipmi0
```

`git` is only needed to clone. The `ast` DRM driver may stay bound — it does not
conflict, because access goes through the PCI sysfs resource rather than
`/dev/mem`.

## Does it work on my machine?

Find the ASPEED **VGA** function (not the AST1150 PCI-to-PCI bridge that sits
in front of it) and point `p2afan` at it:

```sh
lspci -d 1a03:
# 0b:00.0 PCI bridge: ... AST1150 PCI-to-PCI Bridge      <- not this one
# 0c:00.0 VGA compatible controller: ASPEED Graphics ...  <- this one
```

Set `pci_bdf = "0000:0c:00.0"` in `/etc/p2afan/config.toml` to your ASPEED
VGA function's address. No source or service-unit edits are needed. The default
is the reference chassis address; short addresses such as `0c:00.0` are
normalized to domain `0000`. A one-command override is available:

```sh
sudo p2afan --bdf 0000:0c:00.0 pwm-dump
```

Then inspect the registers and sensors (no duty writes; register reads still
repoint the shared P2A window):

```sh
sudo p2afan pwm-dump     # PWM/tach registers + per-channel duty
sudo p2afan sensors      # BMC temps and fan RPM
```

You are in business if `pwm-dump` prints a non-zero `CTRL` with bit 0 set
(`CLK_EN`) and duty bytes consistent with your fans' idle noise. **Save that
output** — it is your factory baseline and the thing you restore to.

If AHB reads come back as all-`0x00000000` or all-`0xffffffff`, the bridge is
disabled or locked and `p2afan` cannot reach your BMC. If `pwm-dump` raises
`BridgeUnavailable: PWM clock disabled`, this BMC does not drive fans from this
block at all.

One thing that surprises people later: **`pwm-dump` needs the P2A lock even
though it only reads.** Every access re-points one shared AHB window register,
so reads must be serialised too. Once the service is running it owns that lock,
and register commands fail with `P2A bridge lock … held by another process`.
That is expected — use `p2afan status` (lock-free) for routine checks, and stop
the service for anything that touches registers:

| Lock-free, any time | Needs the lock (stop the service first) |
|---|---|
| `status`, `sensors` | `pwm-dump`, `get`, `set`, `set-raw`, `release`, `failsafe`, `map`, `dump-flash`, `ahb-read` |

## Install

Install only from a local, inspected checkout or extracted release archive.
There is no network download in the installer and no curl-to-root command.
Both package types use the same installer:

```sh
git clone https://github.com/larkinwc/p2afan
cd p2afan
sudo ./install.sh
```

It installs the CLI at `/usr/local/bin/p2afan`, the payload at `/opt/p2afan`,
and the unit at `/etc/systemd/system/p2afan.service`. It **never enables or
starts the service**. Live installation requires root, Linux, systemd, and
`ipmitool`; source installation also requires `python3` 3.11+. The standalone
Linux x86_64 binary needs no system Python but still needs `ipmitool` for
BMC sensors, fan mapping and RPM checks. If your distro's Python is older
(Ubuntu 22.04 defaults to 3.10), use the standalone binary or provide a
supported `python3` on both your shell and systemd service `PATH`, using a
systemd override. The installer does not change your system interpreter.

**A fresh install creates only `/etc/p2afan/config.toml.example`, not an active
config.** Existing `config.toml`, `mapping.toml`, and examples are preserved.
Copy the example explicitly, then edit it before any actuation:

```sh
sudo cp /etc/p2afan/config.toml.example /etc/p2afan/config.toml
sudoedit /etc/p2afan/config.toml
p2afan --version
p2afan check-config
```

The sample is for a Tyan B7079: its GPU sensors are `0x20`–`0x2e`, CPU DTS
`0x01`/`0x02`, and outlet `0x08`. Its limits are not universal. Cross-check
every sensor against your own SDR, set `pci_bdf`, derive critical limits, and
set `min_duty_pct` to your measured safe idle. `check-config` validates config
and mapping **without opening the P2A bridge, reading sensors, executing
temperature commands, or changing fan duty**. A missing mapping is a warning,
not proof your hardware is mapped or safe.

Then map which PWM channel drives which fan:

```sh
sudo p2afan map
cat /etc/p2afan/mapping.toml
p2afan check-config
```

> [!CAUTION]
> `map` is disruptive: it raises each channel to 60 % in turn (~20 s each).
> Expect **2–3 minutes of loud fans**. It restores entry duty after every probe
> and at the end. Run on an idle machine with the service stopped. It refuses
> to overwrite `/etc/p2afan/mapping.toml` without `--force`.

Optionally prove actuation before handing over to systemd:

```sh
sudo p2afan set 40
sudo p2afan get
sudo p2afan release      # restores the captured pre-takeover duty
```

Finally, explicitly enable and start after checking your configuration:

```sh
sudo systemctl enable --now p2afan
sudo p2afan status
journalctl -u p2afan -f
```

`status` should show your expected duty, `failsafe false`, and `overrides 0`.
A climbing `overrides` count means the BMC is rewriting duty registers; set
`hold_interval` (e.g. `0.25`) to reassert faster than it corrects.

### Releases and local builds

Tagged releases, when published, provide `p2afan-<version>-source.tar.gz`,
`p2afan-<version>-linux-x86_64.tar.gz`, and `SHA256SUMS` on the
[releases page](https://github.com/larkinwc/p2afan/releases). These instructions
do not imply an artifact has already been published. Download locally, verify
the available files with `sha256sum --ignore-missing -c SHA256SUMS`, inspect,
extract, and run `sudo ./install.sh` inside the extracted directory. A checksum
detects corruption; it is not an independent publisher signature.

The standalone bundle is built on **Ubuntu 22.04 x86_64 / glibc 2.35** and
requires Linux x86_64 with glibc 2.35 or newer; it is not a musl/Alpine binary.
Source code supports Python 3.11–3.14 and is not restricted to x86_64.

Build locally (no publishing):

```sh
python3 scripts/build_release.py --kind source
# For binary/all, build on Ubuntu 22.04 x86_64 with CPython 3.11:
python3 -m venv .venv-build
. .venv-build/bin/activate
python -m pip install -r scripts/requirements-build.txt
python scripts/build_release.py                # both archives + SHA256SUMS in dist/
```

PyInstaller and its build dependencies are pinned in
`scripts/requirements-build.txt`. Building on a newer glibc raises the effective
binary baseline; use the documented build host. CI runs hardware-free safety
tests and CLI/config/staged-installer smoke checks on Python 3.11–3.14.
Every push and PR also builds the standalone binary on Ubuntu 22.04 and smokes
both extracted archives outside the checkout without `PYTHONPATH`. Verified
archives/checksums are retained as CI artifacts. Publishing is tag-only:
a `v<version>` tag must equal `p2afan.__version__`, and the release job uploads
the bundles verified by that run only after all safety/package jobs pass.

For a non-root install rehearsal, with no host changes or `systemctl` calls:

```sh
DESTDIR="$(mktemp -d)" ./install.sh
# Or exercise the CLI plus fresh install and preserved-config upgrade:
python3 scripts/smoke_distribution.py .
```

### Stopped upgrades

The installer refuses to upgrade while the service is active, activating, or
stopping. Do not replace files underneath a running daemon. While the old
installation is still intact:

```sh
sudo systemctl stop p2afan
sudo p2afan pwm-dump       # confirm the original baseline was restored
```

**Upgrading from 1.0:** its clean stop leaves a nonempty, device-unscoped
`/run/p2afan/state.json`. Version 1.1 refuses that legacy baseline rather than
guessing which PCI device it belongs to. After stopping the **old** daemon,
compare the live duty from `pwm-dump` with your recorded factory/pre-takeover
baseline. Only once restoration is confirmed and no takeover is active, move
the legacy state aside:

```sh
sudo mv -n /run/p2afan/state.json /run/p2afan/state-v1.0.json
```

Do not move state while a daemon or manual takeover is active, or when baseline
restoration is uncertain. Keep the archive for diagnosis. If that archive name
already exists, choose an unused name; `mv -n` must not leave the original state
in place before you proceed. Current device-scoped ownership in
`/run/p2afan/baseline.json` needs no migration; preserve it through upgrades.

Then install from the inspected new package directory:

```sh
sudo ./install.sh
p2afan --version
p2afan check-config
sudo systemctl start p2afan
```

Config and mapping are never overwritten; review new example/settings and
release notes manually. Stop any manually launched daemon too: the installer
checks systemd's service state, not arbitrary processes. Installation reloads
the unit but never starts or enables it, even on an upgrade.

### Removal

Stop using the still-installed CLI/unit so baseline restoration can complete,
then inspect the duty before removing code:

```sh
sudo systemctl disable --now p2afan
sudo p2afan pwm-dump
sudo rm /etc/systemd/system/p2afan.service /usr/local/bin/p2afan
sudo systemctl daemon-reload
sudo rm -rf /opt/p2afan
```

This intentionally **preserves `/etc/p2afan/config.toml`, the mapping, and any
examples**, plus boot-scoped recovery state. A clean stop restores the captured
baseline; if restoration fails, investigate before removing the recovery tool.

## Configuration

Zones are independent curves; the **highest** duty any zone asks for wins.
Within a zone, the **hottest** valid source wins. Nothing is ever averaged.

```toml
pci_bdf = "0000:0c:00.0"     # ASPEED VGA function, not the upstream PCI bridge
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
p2afan --version         # package version
p2afan check-config [--mapping PATH] # validate only; no sensors/register writes
p2afan --config PATH check-config   # alternate config (default /etc/p2afan/config.toml)
p2afan --bdf BDF pwm-dump # override configured ASPEED PCI address
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

Failure handling prioritizes emergency cooling, but cannot guarantee airflow
when the bridge, BMC actuator, or fans themselves fail:

- **Duty floor** — configured `min_duty_pct` bounds the daemon's requested duty.
  The sample's 20 % is reference-only, not an automatically measured idle;
  derive a safe floor for your chassis. Manual `set` is an explicit override.
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
- **Crash-restart** — active takeover baselines are persisted before duty writes
  and scoped to the boot and PCI device. Restart after failsafe cannot latch
  100 % as "factory". Manual `set`/`set-raw` also capture a baseline first.
  Ownership lives in `/run/p2afan/baseline.json`, separately from status telemetry
  in `/run/p2afan/state.json`; injection heartbeats cannot overwrite it.
- **No guessed restore** — `release` requires the captured baseline for this
  boot/device; missing or legacy unscoped state is refused rather than applying
  reference-chassis duty bytes. Ownership clears only after verified restore.
- **Finite validation** — config values, curves and source specifications are
  validated before hardware opens. Non-finite temperature readings are missing
  data, never cold temperatures.
  Failsafe duty must be positive and at least every configured curve's maximum;
  when injection is enabled, its failsafe temperature must reach every zone's
  critical threshold and fit the 8-bit sensor range.
- **Tach loss** — sustained missing fan telemetry trips failsafe independently
  of low-RPM strikes; missing readings do not reset stall evidence. Recovery
  requires consecutive healthy readings.
- **Actuator failures** — required sensor injection failures prevent readiness
  and fail the daemon. Optional PWM-mode mirroring errors appear in `status` as
  `inject errors` without discarding successful PWM control. Invalid mapping or
  unwritable baseline storage cannot block emergency PWM writes. If full config
  validation fails, emergency control uses a valid raw actuator identity with
  safe defaults; without a trustworthy identity it attempts IPMI injection, not
  a guessed PCI device. Injection fallback is unavailable on Tyan firmware 9.01.
- **Register scope** — PWM `CTRL`, `CTRL_EXT`, `CLK_CTRL`, and `TYPE*` are never
  written. Auto-selected daemon channels exclude disabled and duty-zero
  channels; explicit channel selections, manual writes, and mapping can target
  enabled duty-zero channels.

## Reference platform results

Tyan FT77C-B7079, AST2400, firmware 9.01, six chassis fans on PWMD/E/F:

| Duty | Total fan RPM | Note |
|---|---|---|
| `0x33` 20 % | 18 540 | factory idle |
| `0x4c` 30 % | ~22 000 | typical `p2afan` idle |
| `0x99` 60 % | ~31 000 | |
| `0xff` 100 % | 38 790 | **2.09× reported total RPM**, not a measured airflow ratio |

Spin-up is audible in ~2 s and settled in ~4.5 s. Duty writes held for 60 s and
30 s tests with zero reverts — the BMC does not fight back on this firmware. If
yours does, set `hold_interval` to re-assert faster than it corrects.

The hard-coded BMC sensor catalogue, **90 RPM per raw fan count** conversion,
and 1 °C per temperature count are reference-firmware assumptions, not generic
SDR decoding. Validate sensor IDs and scaling against your own SDR and measured
tach readings before relying on fan mapping, stall detection, or injection.
Changing config sensor IDs alone does not change the fan catalogue/scaling.

## Limitations

- `mode = "inject"` (feed the BMC's own sensors instead of writing duty) is
  implemented but **inoperable on Tyan firmware 9.01**: every SDR record reports
  capabilities `0x68`, i.e. reading-not-settable, so `Set Sensor Reading` returns
  completion code `0x80`. It ships for firmware that does permit it, and it never
  touches the P2A bridge so it remains usable if a BMC locks P2A.
- Tested on AST2400 only. AST2500 uses the same PWM block and P2A layout and
  should work; AST2600 moves things and is untested.

## Release notes

### 1.1.0

- Safe local source/binary installer, preserved configuration/mapping, explicit
  example activation, staged-install support, and stopped-only service upgrades.
- Versioned source and standalone Linux x86_64 bundles, pinned PyInstaller
  build dependencies, glibc 2.35 baseline, PR binary verification and tag-only publishing.
- Configurable ASPEED PCI BDF, CLI `--config`/`--bdf`, `--version`, and
  non-actuating `check-config`; systemd no longer assumes one PCI address.
- Validated configuration/source/curve inputs and finite temperature handling;
  corrected fan telemetry loss/recovery handling.
- Boot/device-scoped baseline capture before takeover, including manual writes;
  restore refuses unknown state instead of guessing reference factory duty.
  Upgrades from 1.0 require the stopped, verified legacy-state migration above.
- Required injection failures no longer report healthy readiness; optional
  mirroring failures are visible. Emergency PWM cooling tolerates malformed
  mapping and unavailable baseline storage.
- Authoritative ownership is separate from status telemetry, preventing
  injection-mode heartbeat races with manual takeovers.

## License

MIT — see [LICENSE](LICENSE).
