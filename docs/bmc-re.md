# Tyan FT77C-B7079 (S7079GM2NR-N) BMC reverse engineering + `p2afan` notes

Host: `<host>`, Ubuntu 24.04, kernel `7.1.3-ga100bar1-tyan`.
BMC: ASPEED AST2400, Tyan firmware rev **9.01**, LAN `<bmc-ip>`.
All measurements below were taken on that machine; every number is reproducible
with the CLI in this repo.

## 0. Why this exists

The factory BMC ramps the six chassis fans from its own NVIDIA-GPU temperature
sensors (`GPU0..7_Core0_TEMP`). Any card the BMC cannot read — an AMD Radeon
Pro V340, for instance — contributes nothing to the fan decision, and there is
no OEM IPMI command on this firmware to override the duty. `p2afan` therefore
drives the AST2400 PWM duty registers **from the host** over the ASPEED
PCIe-to-AHB (P2A) bridge, using any temperature source you can name.

## 1. Host access to the BMC: the P2A bridge

The ASPEED VGA function `0000:0c:00.0` exposes BAR1 = 128 KiB at `c7000000`
(`/sys/bus/pci/devices/0000:0c:00.0/resource1`, size `0x20000`):

| BAR1 offset | Meaning | Value on this host |
|---|---|---|
| `0xf000` | P2A bridge enable (bit 0) | `0x00000001` — already enabled |
| `0xf004` | AHB window base (64 KiB aligned) | `0x1e6e0000` on entry |
| `0x10000`–`0x1ffff` | 64 KiB window onto AHB at that base | — |

`CONFIG_STRICT_DEVMEM=y` is irrelevant: this is the PCI sysfs resource, not
`/dev/mem`. `p2afan/ahb.py` mmaps it, takes an exclusive `flock` on
`/run/p2afan/p2a.lock` (mandatory even for reads — every access re-points the
shared window register), and restores `0xf004` on exit.

Measured P2A read throughput: **~0.3 MiB/s** (16 MiB flash dump in 54 s).

## 2. Fan hardware: ASPEED PWM/tach block at AHB `0x1E786000`

Factory state, captured before any write (`re/baseline/pwm-regs.txt`):

| Offset | Name | Value |
|---|---|---|
| `0x00` | `CTRL` | `0x00fc0f01` |
| `0x04` | `CLK_CTRL` | `0x9c33ff11` |
| `0x08` | `DUTY0` (PWMA/PWMB) | `0x33003300` |
| `0x0c` | `DUTY1` (PWMC/PWMD) | `0x33003300` |
| `0x10`/`0x14` | `TYPEM_CTRL`/`_CTRL1` | `0x10000001` / `0x10000000` |
| `0x18`/`0x1c` | `TYPEN_CTRL`/`_CTRL1` | `0x10000001` / `0x10000000` |
| `0x2c` | `RESULT` | `0x8000f1f3` (varies) |
| `0x40` | `CTRL_EXT` | `0x00000700` |
| `0x44` | `CLK_CTRL_EXT` | `0x0000ff00` |
| `0x48` | `DUTY2` (PWME/PWMF) | `0x33003300` |
| `0x4c` | `DUTY3` (PWMG/PWMH) | `0xff000000` |
| `0x50`/`0x54` | `TYPEO_CTRL`/`_CTRL1` | `0xfff700fe` / `0xac53b2cd` |

Decoded: `CLK_EN=1`; PWMA–PWMD enabled (`CTRL` bits 8–11), PWME–PWMG enabled
(`CTRL_EXT` bits 8–10), PWMH disabled; `FAN_NUM_EN = 0x00fc` → tach channels
2–7 enabled = the six `SYS_FAN_1..6` sensors.

Register offsets and the duty-field layout follow the mainline driver
`drivers/hwmon/aspeed-pwm-tacho.c`: each `DUTYn` holds two channels, the first
of the pair in bits `15:0` (rise `7:0`, fall `15:8`), the second in `31:16`.
With rise = 0, duty = fall/256, so factory `0x33` = 20 % and `0xff` = 100 %.

`p2afan` writes **only** the four `DUTY` registers, and only one 16-bit half
at a time (`pwm.apply_fall`). `CTRL`, `CTRL_EXT`, `CLK_CTRL` and the `TYPE*`
registers are never touched, which is why the BMC's own thermal loop stays
alive underneath and resumes authority the moment the duty bytes are restored.

## 3. Override behaviour (Step 3 measurement)

`re/actuation_test.py`, log in `re/baseline/actuation-test.log`: every enabled
channel A–F raised from `0x33` to `0x66` (40 %) and held for 60 s while duty
registers were polled every 250 ms and fan RPM every 2 s.

- **The BMC never fought back.** `DUTY0/1/2` read `0x66006600` continuously for
  the full 60 s; zero reverts observed.
- RPM response was immediate: all six fans rose within 2.2 s and settled by
  ~4.5 s at +990 to +1080 RPM (3060→4140, 3240→4320, 3150→4230, 2880→3870,
  2970→3960, 2880→3870).
- Restore was exact (`restore_exact=True`) and RPM returned to the baseline
  values within 12 s.

**Chosen configuration: `mode = "pwm"`, `hold_interval = 0.0`** — the duty is
re-asserted once per 5 s control tick, which is sufficient because nothing
rewrites it. `hold_interval > 0` exists for a firmware that does fight back.

Duty→RPM response measured across the range: `0x33` (20 %) → 2880–3240 RPM,
`0x4c` (30 %) → 3330–3780, `0x66` (40 %) → 3870–4320, `0x99` (60 %) →
4860–5400, `0xe6` (90 %) → 5850–6660, `0xff` (100 %) → 6120–6930.

## 4. Channel → fan mapping (Step 4 measurement)

`p2afan map` raised one channel at a time to `0x99` (60 %) for 10 s and
attributed any fan gaining ≥ 400 RPM (`re/baseline/map.log`):

| Channel | Fans | Evidence |
|---|---|---|
| PWMA | none | no tach moved (±0 RPM) |
| PWMB | none | no tach moved |
| PWMC | none | no tach moved |
| PWMD | `SYS_FAN_2`, `SYS_FAN_5` | +2160, +1980 RPM |
| PWME | `SYS_FAN_3`, `SYS_FAN_6` | +2070, +1980 RPM |
| PWMF | `SYS_FAN_1`, `SYS_FAN_4` | +2160, +1980 RPM |
| PWMG | none | parked at fall `0x00` by the factory |
| PWMH | — | disabled in `CTRL_EXT` |

So all six monitored fans are attributed to exactly one channel each, on three
channels. PWMA–PWMC are **enabled and factory-driven at the same `0x33`** but
have no tach of their own; PWMG is enabled yet parked at duty 0.

`p2afan` therefore drives **A–F**: the three mapped channels plus the three
factory-driven untached ones (`control.writable_channels`). Rationale: leaving
A–C pinned at 20 % while D–F ramp would strand any untached header, and since
the duty floor equals the factory idle, carrying them can only add airflow
versus stock. PWMG (parked) and PWMH (disabled) are never written. Override
this with `write_channels` in `config.toml` if you want narrower behaviour.

## 5. BMC sensor access from the host

`ipmitool raw 0x04 0x2d <sensor#>` (Get Sensor Reading) averages **45 ms**;
`ipmitool sdr elist full` takes 0.9–2.6 s, so the control loop only uses the
raw form. Response is `<reading> <status1> <status2> <status3>`; a reading is
valid iff `status1 & 0x20 == 0` (absent sensors answer `00 e0 00 80`).
Temperatures are 1 °C per count; `SYS_FAN_n` is **90 RPM per count**
(`0x22`→3060, `0x24`→3240, `0x23`→3150, `0x20`→2880 — exact).

Sensor numbers: `CPU0_DTS 0x01`, `CPU1_DTS 0x02`, `LAN_Area 0x05`,
`SYS_Air_Inlet 0x06`, `MB_Air_Inlet 0x07`, `SYS_Air_Outlet 0x08`, `PCH 0x0a`,
DIMMs `0x41,0x44,0x4a,0x4d,0x50,0x53,0x56`, `GPU0..7_Core0_TEMP` =
`0x20,0x22,0x24,0x26,0x28,0x2a,0x2c,0x2e` (the `Core1` twins read "No
Reading"), `SYS_FAN_1..6` = `0x32..0x37`.

## 6. No OEM fan API exists on this firmware

Live probes first:

- `ipmitool raw 0x2e 0x05 …` and `0x2e 0x06 …` (the S8036/S8030 fan commands):
  `rsp=0xc1 Invalid command`.
- OEM netfn sweep `0x2c,0x2e,0x30,0x32,0x34,0x36,0x38,0x3a,0x3c,0x3e` with
  cmd `0x00`: `0xc1` for every one.
- `ipmitool i2c bus=0..3` (Master Write-Read, netfn `0x06` cmd `0x52`) is
  implemented but rejects every tested bus/address, so there is no I2C route
  to a fan chip either.

Then the **firmware's own dispatch tables**, which beats probing. `Get NetFn
Support` (netfn `0x06` cmd `0x09`, channel `0x0e`) returns:

```
02 6f 00 c0 a7 00 00 00 00 00 00 00 00 00 00 00 00
```

Byte 0 is the channel; the next 4 bytes are LUN 0's 32-bit mask over NetFn
*pairs* (bit n = netfn `2n`/`2n+1`). `6f 00 c0 a7` decodes to supported pairs
`0x00/01` (Chassis), `0x02/03` (Bridge), `0x04/05` (S/E), `0x06/07` (App),
`0x0a/0b` (Storage), `0x0c/0d` (Transport), and OEM `0x2c/2d`, `0x2e/2f`,
`0x30/31`, `0x32/33`, `0x34/35`, `0x3a/3b`, `0x3e/3f`. So the OEM netfns are
*registered* — the earlier `0xc1` sweep meant "cmd `0x00` is not a command in
that netfn", not "netfn absent".

`Get Command Support` (netfn `0x06` cmd `0x0a`) then enumerates commands per
netfn as a 128-bit mask in which **a clear bit means supported** (verified
against known-working commands, see below):

| NetFn | Mask | Supported commands |
|---|---|---|
| `0x2e` (OEM, IANA `ff ff ff`) | `ff…ff f0 ff…` | **`0x40, 0x41, 0x42, 0x43` only** |
| `0x30` | all `ff` | **none** |
| `0x34` | `f9 3f fe ff … fe … fd` | `0x01,0x02,0x0e,0x0f,0x10,0x40,0x61` |
| `0x3a` | `01 f8 ff ff f9 …` | `0x01`–`0x0a`, `0x21`, `0x22` |
| `0x3e` | `ff 7f fc ff …` | `0x0f,0x10,0x11` |
| `0x04` (S/E) | `f8 ff 00 ff 00 10 fe ff…` | `0x00,0x01,0x02`, `0x10`–`0x17`, `0x20`–`0x27`, `0x28`–`0x2b`, `0x2d`–`0x2f`, `0x30` |

The bit polarity is confirmed by netfn `0x04`: `0x2d` (Get Sensor Reading) and
`0x30` (Set Sensor Reading) are both clear-bit/"supported" and both do answer
on this box, while `0x2c` is set/"unsupported".

**`0x2e` carries exactly four commands, `0x40`–`0x43`, and neither `0x05` nor
`0x06`.** That is firmware-table proof that the documented Tyan fan API is
absent here, matching the live `0xc1`. The purpose of `0x2e 0x40`–`0x43` is
UNCERTAIN: identifying them would mean invoking unknown OEM commands on a live
BMC that is thermally responsible for eight GPUs, so it was not attempted.

This is why the P2A bridge is the actuator.

## 7. `Set Sensor Reading` injection: implemented but non-functional

`Set Sensor Reading` (netfn `0x04` cmd `0x30`) **exists** — a zero-length
request returns `0xc7 Request data length invalid`, not `0xc1` — but writing a
value to the GPU sensors fails:

```
$ ipmitool raw 0x04 0x30 0x20 0x01 0x00 0x50 0x00 0x00 0x00 0x00 0x00
Unable to send RAW command (channel=0x0 netfn=0x4 lun=0x0 cmd=0x30 rsp=0x80): Unknown (0x80)
```

Identical device-specific completion code `0x80` for `0x20`, `0x22`, `0x24`,
`0x26`, and the readings never change (`0x04 0x2d 0x20` still returned the
BMC's own `0x21` = 33 °C afterwards).

**Root cause, straight from the SDR.** Every Full Sensor Record in the live
repository carries sensor capabilities `caps = 0x68` = `0110_1000`:

| caps bits | Meaning here |
|---|---|
| bit 7 = 0 | **sensor reading is NOT settable** |
| bit 6 = 1 | auto re-arm |
| bits 5:4 = `10` | hysteresis readable, not settable |
| bits 3:2 = `10` | thresholds readable, not settable |

So `0x80` is the firmware refusing a write the SDR never advertised as legal —
not a transient error, and not something a request-format change can fix.
Decode it yourself with `re/sdr_decode.py` (see §9.3).

**Result: inject mode cannot actuate on firmware 9.01.** It is implemented and
shipped (`mode = "inject"`, or `inject = true` alongside `mode = "pwm"`) for a
firmware that does accept it, and it deliberately never opens the P2A bridge so
it remains usable if a future firmware locks P2A — but the shipped default is
`mode = "pwm"`, which is proven. Verified behaviour in inject mode: the duty
registers stayed at the factory `0x33003300` (the daemon really does not write
them), the daemon logged the `0x80` rejections, and fans stayed at ~3000 RPM.

## 8. Flash dump

`p2afan dump-flash` points the P2A window at the FMC CE0 memory-mapped region
(`0x20000000`, FMC `0x1e620000` reg0 = `0x801f000a`, CE0 ctrl = `0xb0641`) and
reads 256 × 64 KiB windows.

- `re/flash-16m.bin`, **16777216 bytes**, 54.1 s,
  sha256 `d25c058c336f0f946dd256e76eeec701854f256328501332de6142c0d4acf2cd`.
- Re-reading the first and last 64 KiB after the dump matched byte-for-byte
  (`stable True`), so the BMC did not write flash mid-dump.
- Size of the CE0 *window* read: 16 MiB, because `0x21000000` mirrors offset 0.

**Caveat: this dump is only the lower half of the firmware.** The carved
rootfs (§9.2) declares 14.77 MB in its cramfs superblock but only 5.96 MB is
present before the dump ends, and the FMC segment register `0x1e620030` =
`0x44400000` decodes to a CE0 range of `0x20000000`–`0x21ffffff` (32 MiB),
while CE0 control `0x1e620010` = `0x000b0641` selects read opcode `0x0b`
(3-byte-address FAST READ). A 32 MiB device read with 3-byte addressing loses
A24, which is exactly why `0x21000000` aliases back to offset 0. CE1
(`0x22000000`) and CE2 read as `0x00000000`/`0x12121212`, so there is no second
chip — the upper 16 MiB of a single 32 MiB device is simply unreachable without
switching the FMC to 4-byte addressing.

Reprogramming the FMC of a BMC that is actively booted from that flash was
judged too risky on this production machine, so it was not attempted. To get
the upper half safely, dump from inside the BMC (`ssh root@<bmc-ip>`, see
§9.5) or read the chip offline.

The binary is git-ignored (`re/.gitignore`); regenerate it with the command
above if needed.

## 9. Firmware layout and fan logic

### 9.1 Flash layout

The four Tyan `$MODULE$` headers chain exactly — each one's
`offset + length` lands on the next header — which is what validates the
decode. Header format: `"$MODULE$"` + u32LE version `0x00400701` + u32LE
length + u32LE flash offset + 4 flag bytes + NUL-terminated name at +24.

| Offset | Contents | Evidence |
|---|---|---|
| `0x000000`–`0x02ffff` | U-Boot + board boot code | ARM vectors at 0 (`ea000014 e59ff014`, little-endian ARM branch); CRC32 tables at `0x02409c`/`0x02c78c`; `U-Boot 1.1.6 (May 31 2018 - 14:41:09)` at `0x0245ac` |
| `0x030000` | U-Boot environment | `bootcmd=bootfmh`, `bootdelay=1`, `baudrate=38400` |
| `0x040000`–`0x0bffff` | `$MODULE$` **conf**, len `0x80000` | live JFFS2 config nodes `0x050000`–`0x06ffff`; first node `0x050058` |
| `0x0c0000`–`0x13ffff` | `$MODULE$` **bkupconf**, len `0x80000` | JFFS2 nodes in blocks `0x0d` (62) and `0x11` (244) |
| `0x140000`–`0xa3ffff` | `$MODULE$` **lmedia**, len `0x900000` | live nodes only at the head; blocks `0x16`–`0xa3` hold a single `0x2003` CLEANMARKER (`85 19 03 20 0c 00 00 00 b1 b0 1e e4`) then `0xff` — erased, JFFS2-ready |
| `0xa40000`–`0xa4ffff` | `$MODULE$` **root**, len field `0xe30000` (overruns the 16 MiB window) | 64 B of high-entropy metadata then `0xff` |
| `0xa50000` | uImage header: Linux/ARM, type 3, comp 0, data size `0xe15000` | magic `0x27051956` |
| `0xa50040` | cramfs superblock: magic `0x28cd3d45`, size `0x00e15000`, `Compressed ROMFS`, flags `0x3`, 8343 blocks, 2205 files | first 64 bytes of `re/extracted/cramfs.img` |
| `0xa50040`–`0xffffff` | cramfs rootfs data, **truncated at the window end** (5 963 712 of 14 766 080 bytes) | see §8 caveat |

JFFS2 node census over the whole image: `0xe001` 227, `0xe002` 420, `0x2003`
(cleanmarker) 151; live nodes only in blocks `0x05,0x06,0x0a,0x0d,0x11,0x13,
0x15`. There is **no separate NVRAM partition**: persistent state lives in the
`conf`/`bkupconf` JFFS2 regions (`passwd`, `shadow`, ssh host keys, network
config, `SDR.dat`, `SDRGPU.dat`, `IPMIConfig.dat`, `SEL.dat`).

Platform strings: kernel `2.6.28.10-ami` (module vermagic), ARM EABI5, Sourcery
G++ 2010.09-50, board id `BMC1/ast2300evb_ami`.

### 9.2 Rootfs extraction

Two routes, both needed:

- **cramfs** — carve at `0xa50040` and mount it read-only:
  ```
  sudo mount -o loop,ro -t cramfs re/extracted/cramfs.img /mnt/bmcroot
  ```
  This works despite the truncation, and the whole directory tree is
  browsable. `fsck.cramfs` refuses the image (`file length too short`), and
  `7z` also lists it (2096 files / 108 folders) and extracts the small files.
  Any file whose data lands past `0xffffff` fails: `EIO` when mounted, 0-byte
  stubs via `7z`. That is every large userspace binary — `usr/local/bin/IPMIMain`
  (114 484 B listed), `pwmtachtool` (17 048 B), `adviserd`,
  `usr/local/lib/libpwmtach.so.1.5.0`,
  `usr/local/lib/ipmi/ast2300evb_ami/libipmipar.so.1.56.0`, the CIM/OEM
  provider `.so`s — 401 of 2096 files came out non-empty.
- **JFFS2** — `pip install --break-system-packages jefferson` (0.4.7). Run on
  the whole dump it finds 0 inodes; carve the dense slices first and it works
  per slice: `dump[0x50000:0x70000]` → 95 files (incl.
  `BMC1/ast2300evb_ami/SDR1.dat`, `SDRGPU.dat`), `dump[0x110000:0x120000]` → 89
  files (second copy), `dump[0xd0000:0xe0000]` → 12 files incl. the live
  `SDR.dat` (2879 B), `SEL.dat`, `IPMIConfig.dat`.

What survived and matters: all of `/etc` (including `/etc/init.d/ipmistack`),
`IPMI.conf`, `SDR*.dat`, both `pwmtach*.ko`, and `info/ast2300evb.PRJ`.

### 9.3 What drives the fans in the factory firmware

This is an **AMI MegaRAC SPX** stack (`lib/ld-2.11.3.so`, glibc 2.11.3, kernel
`2.6.28.10-ami`, `usr/local/lib/ipmi/ast2300evb_ami/`).

**The owning daemon is `/usr/local/bin/IPMIMain`.** `/etc/init.d/ipmistack`
(recovered intact) starts exactly one IPMI process:
`/usr/local/bin/IPMIMain --daemonize --reg-with-procmgr`. Sensor polling, SDR
service and the thermal/PWM decision all live in that process or its plugins;
there is no separate `fancontrol`-style binary anywhere in the tree.

The actuation path below it is AMI's `pwmtach` pair, not anything Tyan-specific:

- `lib/modules/generic/misc/pwmtach_hw.ko` —
  `description=AST2100/2050/2200/2150/2300/2400 PWM & Fan Tach controller
  driver`, `author=American Megatrends Inc.` Exported symbols include
  `ast_pwmtach_set_dutycycle`, `ast_pwmtach_get_dutycycle`,
  `ast_pwmtach_set_prescale`, `ast_pwmtach_enable_pwm_control`,
  `ast_pwmtach_trigger_read_fanspeed`, `ast_pwmtach_get_current_speed`,
  `ast_pwmtach_set_tach_property`. **This is the driver writing the very
  registers `p2afan` writes** — `ast_pwmtach_set_dutycycle` is the factory
  path to `0x1E786000 + 0x08/0x0c/0x48/0x4c`.
- `lib/modules/generic/misc/pwmtach.ko` — `PwmTach Common driver, (c) 2009
  American Megatrends Inc.`, registers char device `pwmtach`
  (`/dev/pwmtach0`, also `lib/udev/devices/pwmtach0`). Its strings expose the
  platform tables:
  - `Fan Map Table for %d : (FANNUM, PWMNUM, TACHNUM)` — the firmware's own
    equivalent of our `mapping.toml`, i.e. fan → PWM channel → tach channel.
  - `Fan Properties Table for %d : (FANNUM, MINRPM, MAXRPM, PULSES/REV)`
  - `Fan Map Table not configured for device.`, `ENTRY[%d] = (%d, %d, %d)`,
    `get_pwm_number: Incorrect fan_number passed`, `trigger read fan speed
    failed`, and `Dutycycle value in % should be between 1 to 100.` — so duty
    is programmed in **percent** at this interface and the tables are supplied
    at runtime (by `IPMIMain` via ioctl), not baked into the `.ko`.
- `usr/local/lib/libpwmtach.so.1.5.0` + `usr/local/bin/pwmtachtool` — the
  userspace API and CLI over that device (contents unreadable, see §9.2).
  Neighbours `libi2c.so.1`, `libpmbus.so.1.10`, `libmmap.so.1.4.0` and the
  probes `i2c-test`, `smb-test`, `pmbus-test` show the same process also owns
  the I2C/PMBus sensor side. `/dev` carries `i2c0`–`i2c15`, `gpio0`, `mem`,
  `mtd0`–`mtd10`, `pwmtach0`.
- `info/ast2300evb.PRJ` (readable, 38 809 bytes) pins the feature set:
  `CONFIG_SPX_FEATURE_FAN_PROFILE=YES`, `CONFIG_SPX_pwmtach-1.12.0=YES`,
  `CONFIG_SPX_pwmtach_hw-1.11.0=YES`, `CONFIG_SPX_libpwmtach-1.5.0=YES`,
  `CONFIG_SPX_pwmtachtool-1.6.0=YES`, and notably
  `CONFIG_SPX_pwmtach_hw_1_11_0_1_Add_New_No_Fan_Value_Condition-1.0.0=YES`.
  "Fan Profile" is AMI's name for the temperature→duty curve feature.

**The numeric curve itself is UNCERTAIN.** In MegaRAC SPX the Fan Profile is
configured into `IPMIMain`/its platform library, and both `IPMIMain` (114 KB)
and `libipmipar.so.1.56.0` (791 KB) are among the files whose data sits in the
unreachable upper half of the flash. A whole-dump `strings` sweep for
`fan|pwm|duty|thermal|smartfan|curve|trip|threshold|lookup` turns up no
temperature→duty table, and neither the recovered `conf` partition nor
`IPMI.conf` contains one. Recovering it needs a full 32 MiB image and then
disassembly of `IPMIMain`.

What *is* available is the firmware's own limit set, read live from the SDR
(`ipmitool sensor get`), which is what the shipped `config.toml` is calibrated
against:

| Sensor | Firmware upper critical | `p2afan` zone critical |
|---|---|---|
| `GPU0..7_Core0_TEMP` | 87 °C | 85 °C (`gpu`) |
| `CPU0/1_DTS_Temp` | 85 °C | 83 °C (`cpu`) |
| `SYS_Air_Outlet` | 70 °C | 60 °C (`exhaust`) |
| `SYS_FAN_1..6` | lower critical 720 RPM | `min_fan_rpm = 720` |

The SDR also confirms the scaling the daemon relies on, independently of the
empirical measurements in §5: `SYS_FAN_n` have `M = 90, R_exp = 0` → exactly
**90 RPM per count**, and every temperature sensor has `M = 1` → **1 °C per
count**. Reproduce with `python3 re/sdr_decode.py /tmp/sdr.bin` after
`ipmitool sdr dump /tmp/sdr.bin`.

### 9.4 How the firmware gets GPU temperatures, and why a V340 reads "No Reading"

Two independent sources agree: the factory sensor-definition file recovered
from flash, and the live SDR repository.

**From flash** — `etc/defconfig/BMC1/ast2300evb_ami/SDRGPU.dat` (5920 B, and a
byte-identical copy in the `conf` JFFS2 partition at
`BMC1/ast2300evb_ami/SDRGPU.dat`) defines exactly sixteen GPU sensors whose
numbers match the live BMC: `GPU0_Core0_TEMP` `0x20` at file offset `0x894`,
`GPU0_Core1` `0x21`, … `GPU7_Core0` `0x2e`, `GPU7_Core1` `0x2f` (Core0 even at
`0x20 + 2k`, Core1 odd). All sixteen share identical pre-name bytes
(`26 26 ff 00 75 96 65 80 a0 90 00…`) which differ from the CPU sensors'
(`26 26 7f 80 75 55…`) in the threshold-mask area — i.e. the shipped GPU
definitions carry **no programmed thresholds**, while the live repository does
report an upper critical of 87 °C for the `Core0` sensors. Where that 87 °C is
programmed (the runtime `SDR.dat` in the `conf` partition, or firmware startup
code) is UNCERTAIN.

**From the live SDR repository** (5138 bytes, 70 full + 4 compact + 27
event-only + 1 device-locator + 1 OEM record):

- All sixteen `GPU<n>_Core<0|1>_TEMP` sensors (`0x20`–`0x2f`) exist as
  **static Full Sensor Records** with `owner = 0x20` (the BMC itself, LUN 0),
  `entity = 0x07/0x00` (System Board), sensor type `0x01` Temperature,
  `M = 1`, `init = 0x7f` (scanning enabled on power-up).
- `owner = 0x20` is the decisive bit: these are **not** satellite-controller
  sensors reached over IPMB and they are **not** reported by the card. The BMC
  polls them itself and publishes them as board sensors.
- The records are pre-allocated per GPU slot, two per slot, regardless of what
  is installed: with all eight GA100 cards present, the eight `Core0` sensors
  read 32–37 °C while all eight `Core1` twins are permanently "No Reading"
  (`status1 & 0x20` set). A sensor whose SDR exists but whose poll target does
  not answer simply stays unavailable — that is the firmware's "No Reading",
  and AMI even ships a patch named
  `pwmtach_hw…Add_New_No_Fan_Value_Condition` for the analogous fan case.
- Therefore an **AMD Radeon Pro V340 in one of those slots reads "No Reading"
  for the same reason `Core1` does today**: the BMC polls a fixed,
  NVIDIA-specific thermal target per slot, and a card that does not answer that
  poll leaves the static sensor unpopulated. Nothing in the SDR is
  card-provided, so there is no path by which a non-NVIDIA card could ever
  populate it, and `Set Sensor Reading` cannot fill it either (§7:
  `caps = 0x68`, not settable).
- **UNCERTAIN: the exact bus/address/mux.** The concrete I2C bus, device
  address and any PCA954x mux walk live in the sensor-poll code inside
  `IPMIMain`/`libipmipar.so`/`libpmbus.so`, all in the unreachable flash half.
  The dump's ASCII contains no `nvidia`, `peci` or `NVML` content strings at
  all — the only `GPU` hits are the three `SDRGPU.dat` filename records at
  `0x5504c`, `0x11a930`, `0xa51a38`. Host-side corroboration is blocked too:
  `ipmitool i2c` (Master Write-Read) is implemented but rejects every tested
  bus/address, so the GPU I2C segments are private to the BMC. `IPMI.conf`
  programs primary IPMB bus 6, secondary 1, NM IPMB bus 1, BMC slave `0x20`,
  and advertises only stock AMI interfaces (DCMI 1, HPM 1, Group Extension 1;
  APML/OPMA/SSICB/BT/SMBus 0) — no Tyan fan extension. The single OEM SDR
  record (type `0xc0`, id `0x0067`) carries manufacturer ID `0x000157`
  (IANA 343, Intel) and payload `0d 01 2c 60 19 18 1a 1b 00…`; `0x2c` and
  `0x60` are plausible I2C addresses but that reading is speculation and is not
  claimed here.

The practical consequence is the whole reason this project exists, and it is
why the shipped config gives the V340 two independent paths: the `exhaust` zone
(`SYS_Air_Outlet`) covers it with no telemetry at all, and a `pci`/`exec`
source covers it precisely once the host can read it.

### 9.5 If the P2A bridge is ever lost

The BMC also runs OpenSSH 6.0p1 Debian-4 on port 22 (host keys `ssh-rsa`,
`ssh-dss`; auth `publickey,password`), with 80/443/623 open, and IPMI users
`root` (id 2) and a second vendor account (id 3). Set a password from the host with
`ipmitool user set password 2`, log in with
`ssh -oHostKeyAlgorithms=+ssh-rsa root@<bmc-ip>`, and the same registers
are reachable through the BMC's own `/dev/mem`. This is also the safe route to
dump the upper half of the flash (§8). Not required for normal operation.


## 10. Operating notes

```
p2afan pwm-dump            # ASPEED PWM/tach registers + per-channel duty
p2afan sensors             # every known BMC sensor, temps and fan RPM
p2afan get / set <pct>     # read / write duty directly (service must be stopped)
p2afan map                 # regenerate /etc/p2afan/mapping.toml
p2afan status [--json]     # last daemon state, needs no P2A lock
p2afan release             # restore pre-takeover duty, BMC resumes control
p2afan failsafe            # slam every driven channel to failsafe duty
p2afan dump-flash          # 16 MiB BMC SPI flash over P2A
p2afan ahb-read <addr>     # raw AHB dwords, for further RE
```

- The daemon holds the P2A lock for its lifetime, so register-touching CLI
  commands fail after `--lock-wait` seconds (default 3) with a clear message.
  `p2afan status` reads `/run/p2afan/state.json` and never needs the lock.
- `systemctl stop p2afan` restores the factory duty bytes via `ExecStopPost`;
  any unclean exit (signal, watchdog, non-zero exit) drives every owned channel
  to `failsafe_duty_pct` instead and logs CRITICAL.
- `/run/p2afan/state.json` carries `baseline_duty`, the pre-takeover duty. It
  is boot-scoped, and the daemon prefers it over the live registers at startup
  precisely so a crash-restart cannot latch the failsafe `0xff` as "baseline"
  and later strand the fans at 100 %.
- Adding a source for a new card is configuration, not code:
  `{ kind = "pci", name = "V340", bdf = "0000:41:00.0" }` once a driver exports
  hwmon, or `{ kind = "exec", name = "V340", command = ["/usr/local/bin/v340temp"] }`
  for anything else.
