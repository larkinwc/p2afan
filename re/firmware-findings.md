# Tyan S7079 / FT77C-B7079 BMC firmware findings (AST2400, Tyan FW 9.01)

Source: `/opt/tyanfan/re/flash-16m.bin` — 16777216 bytes,
sha256 `d25c058c336f0f946dd256e76eeec701854f256328501332de6142c0d4acf2cd`.
All offsets are byte offsets into this file (= flash physical addresses for a 16 MiB image).
Extraction root on this host: `/opt/tyanfan/re/extracted/`
(`cramfs.img` = carve of `flash-16m.bin[0xA50040:]`,
`jffs_*.jffs2` = carved JFFS2 slices, `cramfs_root/` = partial 7z salvage,
`jffs_*_out/` = jefferson outputs, `tiny/` = hand-picked intact small files).

## 1. Flash layout

| Offset range | Size | Content / evidence |
|---|---|---|
| `0x000000–0x02FFFF` | 192 KiB | U-Boot + board boot code. ARM vectors at 0 (`ea000014 e59ff014…`); CRC32 tables at `0x2409C`/`0x2C78C` (binwalk); `U-Boot 1.1.6 (May 31 2018 - 14:41:09)` string at `0x245AC`; U-Boot env at `0x30000` (`bootcmd=bootfmh`, `bootdelay=1`, `baudrate=38400`). |
| `0x040000–0x0BFFFF` | 512 KiB | `conf` region. `$MODULE$` header at `0x40000` (ver `0x00400701`, len `0x80000`, off `0x40000`, name `conf`); header payload area is `0xFF`; live JFFS2 config nodes at `0x50000–0x6FFFF` (see §5). Block `0x04` is 99.9 % `0xFF`. Region ends exactly where `bkupconf` starts (`0x40000+0x80000 = 0xC0000`). |
| `0x0C0000–0x13FFFF` | 512 KiB | `bkupconf` region. `$MODULE$` header at `0xC0000` (len `0x80000`, off `0xC0000`, name `bkupconf`); JFFS2 nodes in blocks `0x0D` (62 nodes) and `0x11` (244 nodes); block `0x0A` has 48 nodes. Ends exactly at `0x140000`. |
| `0x140000–0xA3FFFF` | 9 MiB | `lmedia` region. `$MODULE$` header at `0x140000` (len `0x900000`, off `0x140000`, name `lmedia`); `0x140000+0x900000 = 0xA40000` = `root` header, so all four headers chain exactly. Real JFFS2 data at the head (`0x150000` block: CLEANMARKER + dense `e001`/`e002` nodes; `0x130000` block: 13 nodes). Blocks `0x16–0xA3` are CLEANMARKER-only: each contains exactly one JFFS2 node, a `0x2003` CLEANMARKER (`85 19 03 20 0c 00 00 00 b1 b0 1e e4`) at block offset 0, rest `0xFF` — i.e. erased/JFFS2-ready blocks, no live data. Last live JFFS2 node in flash is in block `0xA3`. |
| `0xA40000–0xA4FFFF` | 64 KiB | `root` module preamble. `$MODULE$` header at `0xA40000` (len field `0xE30000`, off `0xA40000`, name `root`; NOTE the length overruns the 16 MiB flash end — stale/oversize field, see below). First 64 B are high-entropy compressed metadata (`db5c7e6a…`), remainder mostly `0xFF`. |
| `0xA50000–0xFFFFFF` | ~5.9 MiB | Firmware image proper. uImage magic `0x27051956` at `0xA50000` (hdr: Linux/ARM, type 3, comp 0, data-size `0xE15000`); payload at `0xA50040` is cramfs (`Compressed ROMFS`, magic `0x28CD3D45`, name field `Compressed`, flags `0x3`). Declared cramfs size `0xE15000` (14766080 B) also overruns flash end (`0xA50040+0xE15000 = 0x1865040 > 0x1000000`); only ~5.9 MiB are physically present. cramfs metadata + small files are intact (7z lists 2096 files / 108 folders); large multi-block binaries are truncated (see §5). Single gzip member at `0xA66F3C` (`1f 8b 08…`) inside the cramfs data area. No squashfs (`hsqs`), no `-rom1fs-`, no `Linux version` string anywhere in the dump. |
| JFFS2 census (whole dump) | — | `e001` (inode) 227, `e002` (dirent) 420, `0x2003` (CLEANMARKER) 151. Live data only in blocks `0x05,0x06,0x0A,0x0D,0x11,0x13,0x15`; everything else CLEANMARKER-only or `0xFF`. |
| NVRAM/config | — | No separate NVRAM partition found. Persistent config = JFFS2 `conf`/`bkupconf` regions (passwd/shadow/ssh keys/network, `SDR*.dat`, `IPMIConfig.dat`, `SEL.dat` — see §5). |

`$MODULE$` header format (justified by the four instances at `0x40000/0xC0000/0x140000/0xA40000`):
`$MODULE$` + u32LE ver `0x00400701` + u32LE length + u32LE flash offset + 4 B flags +
NUL-terminated name at +24 (`conf`, `bkupconf`, `lmedia`, `root`). Length/offset verify
by exact chaining (`conf` ends at `bkupconf`, `bkupconf` at `lmedia`, `lmedia` at `root`).
The `0xFF80`/`0xFFF8` `$MODULE$` hits inside the U-Boot area are false positives
(swalloed by surrounding `00 ff 00 ff 33 cc…` padding/test pattern).
Platform: kernel `2.6.28.10-ami` (`.ko` vermagic), ARM EABI5, Sourcery G++ 2010.09-50
toolchain, AMI stack, board string `BMC1/ast2300evb_ami` (config paths).

## 2. Fan-control logic: binary, path, curve

**Owning daemon: `/usr/local/bin/IPMIMain` (114484 B in cramfs listing).**
Proof: `/etc/init.d/ipmistack` (survived extraction intact) starts exactly one IPMI
process: `/usr/local/bin/IPMIMain --daemonize --reg-with-procmgr`. This is the AMI
IPMI stack main daemon; all sensor polling, SDR serving, and thermal/PWM decisions
run inside it or its plugins. A `strings` sweep of the whole dump for
`fan|pwm|duty|thermal|smartfan|curve|trip|threshold|lookup` finds **no**
temperature→duty table and no fan daemon name other than the PWM/tach pieces below
(the only `Trip` hit is the timezone `Tripoli`, the only `lookup` is a `killall`
command line, both cramfs dirent areas).

**PWM/tach write path (fully evidenced):**
- Kernel: `lib/modules/generic/misc/pwmtach.ko` (8592 B, survived intact:
`description=PwmTach Common driver`, `author=American Megatrends Inc`,
exports `pwmtach_open/release/ioctl`) + `pwmtach_hw.ko` (6904 B, survived intact:
`description=AST2100/2050/2200/2150/2300/2400 PWM & Fan Tach controller driver`,
`ast_pwmtach_{read,write}_reg`, `enable/disable_{pwm,fantach}_control`,
`set_counterresolution`). So duty is written by MMIO through this driver
(`request memory region` / `ioremap`), i.e. the same `0x1E786000` block the host
reaches over P2A — consistent with the live-measured factory duty bytes.
- Device nodes (cramfs dirent + dump strings at `0xA50A94`/`0xA54304`):
`/dev/pwmtach0` (plus `/dev/i2c0–i2c15`, `/dev/gpio0`, `/dev/mem`, `/dev/mtd0–10`).
- Userspace helpers: `/usr/local/bin/pwmtachtool` (17048 B per cramfs listing),
`libpwmtach.so.1.5.0`, `libmmap.so.1.4.0`, `libi2c.so.1`, `libpmbus.so.1.10`,
and CLI probes `i2c-test`, `smb-test`, `pmbus-test` — all present in the cramfs
listing but truncated to 0 B on extract (large multi-block files, see §5).
- Fan topology contract (literal strings inside the intact `pwmtach.ko`,
file offset measurable via `strings tiny2/…/pwmtach.ko`):
`Fan Map Table not configured for device.`,
`Fan Map Table for %d : (FANNUM, PWMNUM, TACHNUM)`,
`ENTRY[%d] = (%d, %d, %d)`,
`Fan Properties Table for %d : (FANNUM, MINRPM, MAXRPM, PULSES/REV)`,
`Dutycycle value in % should be between 1 to 100.`
So each fan maps (fan#, PWM channel#, tach channel#) with min/max RPM and
pulses/rev, and duty is programmed in percent — the table contents themselves
are passed at runtime (likely by IPMIMain via ioctl), not baked into the `.ko`.

**Temperature→duty curve: UNCERTAIN (binary truncated).**
`IPMIMain`, `pwmtachtool`, `adviserd`, `cimeventmon`, `libpwmtach.so*` all extract
as 0-byte files — only files ≤ 1 cramfs block (or resident in early blocks)
survived (401/2096 non-empty). The numeric curve (thresholds, duty steps,
hysteresis) therefore could not be recovered; it most plausibly lives inside
`IPMIMain` (114 KB, spans many blocks) or its sensor config, neither of which
survived. What *was* recovered instead: the full SDR sensor definitions that the
curve operates on (see §3) and the PWM driver contract above. SecondOpinion: the
live-measured factory behavior (stable `0x33` = 20 % duty on PWMA–F across a
6 s window, see plan §Step 1) is consistent with a slow polling loop in IPMIMain
rather than continuous rewrites.

## 3. GPU temperature acquisition path; why an AMD V340 reads "No Reading"

**SDR evidence (hard data, fully extracted — two identical copies: cramfs
`etc/defconfig/BMC1/ast2300evb_ami/SDRGPU.dat` and JFFS2
`jffs_conf_out/BMC1/ast2300evb_ami/SDRGPU.dat`, 5920 B, `cmp` identical):**
The file defines 16 GPU sensors. Each record is
`… 51 01 3a 20 00 <SNUM> 07 00 7f 68 01 01 00 02 00 22 10 10 00 01 … <NAME> …`
with sensor numbers exactly matching the live BMC:

| Sensor | SNUM byte | Record header file offset |
|---|---|---|
| GPU0_Core0_TEMP | `0x20` | `0x894` |
| GPU0_Core1_TEMP | `0x21` | `0x8E4` |
| GPU1_Core0_TEMP | `0x22` | `0x934` |
| … (Core0 even `0x20+2k`, Core1 odd) … | … | … |
| GPU7_Core0_TEMP | `0x2E` | `0xCF4` |
| GPU7_Core1_TEMP | `0x2F` | `0xD44` |

All 16 share identical pre-name bytes `26 26 ff 00 75 96 65 80 a0 90 00…`
vs CPU sensors (`26 26 7f 80 75 55…`): the `ff 00` where CPUs carry `7f 80`
is the threshold-mask area — i.e. **GPU sensors ship with no thresholds
programmed** (`ff` = unspecified). Entity byte is `0x07` (processor) for all.
Sensor numbers for the other live sensors decode the same way
(e.g. `CPU0_DTS_Temp` SNUM `0x01` at file offset `0x34`).

**Acquisition mechanism: PARTIALLY UNCERTAIN.**
What is established: the 16 SDR entries are *definitions only* — a reading
appears only if IPMIMain's reader backend successfully polls that GPU. Live
ground truth shows even the factory NVIDIA CMP 170HX cards populate only the
`Core0` twin (`0x20,0x22,…,0x2E` valid; `Core1` `0x21,0x23,…` permanently
"No Reading", i.e. `status1 & 0x20`). So the reader resolves at most one
thermal source per GPU slot. Which bus/address/protocol it speaks (candidates:
per-GPU SMBus thermal probe via `/dev/i2c*`, PMBus via `libpmbus.so`, or a
satellite-controller/IPMB path — `IPMI.conf` programs primary IPMB bus 6,
secondary 1, NM IPMB bus 1, BMC slave `0x20`) could NOT be pinned down: the
binaries that implement it (`IPMIMain`, `libipmipar`, `libpmbus`) did not
survive extraction, and the dump's ASCII strings contain no `nvidia|GPU|peci|
NVML` references at all (only the three `SDRGPU.dat` filename hits at
`0x5504C/0x11A930/0xA51A38`, all dirent/filename records, never content).

**Why an AMD Radeon Pro V340 would read "No Reading":** the `GPUk_CoreN_TEMP`
readings are not generic PCIe-device thermometers — each is an SDR entry whose
value is filled exclusively by the factory reader's per-slot probe, which only
succeeds for the device the firmware was built for (the NVIDIA GA100 cards the
chassis ships with; even those answer only the `Core0` instance). A V340 does
not answer that NVIDIA-specific probe, so the sensor reading byte is never
updated and stays invalid (`status1 & 0x20` set → `ipmitool` prints
"No Reading"). Concretely: nothing on the host or in the SDR makes a new card
appear — covering a V340 requires either a host-side loop (P2A PWM writes or an
`Exec`/`PciHwmon` source) or feeding the existing `0x20…0x2E` entries via
`Set Sensor Reading`, not reading them. UNCERTAIN (needs the unrecovered
IPMIMain binary): the exact probe bytes, I2C bus, and slave address.

## 4. Absence of OEM fan IPMI commands

**Firmware-side dispatch table: NOT recovered — UNCERTAIN with partial support.**
`IPMIMain` (114484 B, the binary that owns the netfn→handler table in the AMI
stack) extracts as 0 B (§5), as do the OEM/CIM provider libs
(`libCIMOEMHooks.so.1.125.0`, `libOEMProviderHelper.so.1.47.0`,
`libcmpiOSBase_OEMProfile.so.1.8.0`). A whole-dump ASCII sweep finds no
`netfn|oem.*cmd|dispatch|Get Fan|Set Fan|Fan Control|Fan Speed` strings at all;
the only `OEM` hits (dump offsets `350609, 1117401, 10838918–10841338`) are
CIM-provider *filenames* in cramfs dirent/metadata areas
(`libCIMOEMHooks.so*`, `libOEMProviderHelper.so*`, `libcmpiOSBase_OEMProfile*`),
not IPMI command tables. `IPMI.conf` advertises stock AMI interfaces only
(DCMI `1`, HPM `1`, Group Extension `1`; APML/OPMA/SSICB/BT/SMBus `0`) and no
Tyan fan extension. So the firmware image yields **no evidence of any OEM fan
command**, consistent with — but not an independent proof of — the live result
(all OEM netfns `0x2c…0x3e` → `0xC1 Invalid command`). The corroboration stands
on the live sweep (prior ground truth, deliberately not re-run here); the
on-flash dispatch table location remains unidentified because its container
binary is truncated.

## 5. Extraction route and rootfs tree

**Route that worked: carve-then-extract, per filesystem.**
- JFFS2: `pip install --break-system-packages jefferson` (v0.4.7, lands in
`~/.local/bin`, not on PATH). Running it on the *whole* 16 MiB dump yields
**0 inodes** (autodetect reads the `0xFF` first bytes as big-endian and scans
past the data). Carving the dense JFFS2 slices first, then jefferson **per
slice, works**:
`jffs_conf.jffs2` = dump[`0x50000:0x70000`] → 95 files (passwd/shadow/ssh host
keys/network configs + `BMC1/ast2300evb_ami/{SDR1.dat,SDRGPU.dat}`);
`jffs_11.jffs2` = dump[`0x110000:0x120000`] → 89 files (same profile, second
copy); `jffs_d.jffs2` = dump[`0xD0000:0xE0000`] → 12 files incl. live
`SDR.dat` (2879 B), `SEL.dat`, `IPMIConfig.dat`; `jffs_13.jffs2` →
`bioscfg.conf`, `snmpd.conf`.
- cramfs: carved `cramfs.img` = dump[`0xA50040:`] (5963712 B). Kernel cramfs
mount fails (custom host kernel: `cramfs` absent from `/proc/filesystems`;
`dmesg` mount error 32), `fsck.cramfs` reports `file length too short`
(declared `0xE15000` > bytes present), `binwalk -e` was not needed —
**7z (`7-Zip 23.01`) reads the truncated image directly**: lists 2096 files /
108 folders, extracts all single-block/small files intact (401 non-empty,
incl. `/etc/init.d/ipmistack`, `IPMI.conf`, `SDR*.dat`, both `pwmtach*.ko`,
all of `/etc`, `/lib`, zoneinfo) but writes large multi-block files
(`IPMIMain`, `pwmtachtool`, `adviserd`, `*.so*`) as **0-byte stubs**
(1667 sub-item errors; e.g. `usr/local/bin/IPMIMain` listed 114484 B,
extracted 0 B). A from-scratch python extractor (`cramfsx.py`, kept in this
dir) decoded the on-disk format empirically — dir-entry `namelen` counts
**4-byte words** (`Rmount`+NUL→2, `ash`+NUL→1; entry stride `12+4·namelen`),
file block-table pointer = entry field `>> 4`, table entries are raw byte
offsets of zlib blocks (verified: `Association.db` blocks decompress) — but
full-tree auto-extraction still stalls on later inodes, so 7z's partial tree
plus targeted jefferson slices are the shipped artifact set.
- Kernel `mtdram`+`jffs2` mount route: not attempted (unneeded after jefferson
succeeded).
- Rootfs top level (from 7z listing): `bin bkupconf boot conf dev etc home
info initrd lib lib64 mnt proc root sbin selinux sys tmp usr var`;
`dev` carries `i2c0–i2c15 gpio0 mem mtd0–10`; `usr/local/bin` holds the AMI
app set (`IPMIMain adviserd cimeventmon pwmtachtool i2c-test smb-test
pmbus-test …`); `usr/local/lib` the plugin `.so`s; `etc/defconfig/BMC1/
ast2300evb_ami/` the factory `IPMI.conf` + `SDR{,1,GPU}.dat`.

## UNCERTAIN list (what was tried)

1. Numeric temperature→duty curve (thresholds/duty steps/hysteresis): no
table found via `strings -t x` sweeps (`fan|pwm|duty|thermal|smartfan|
curve|trip|threshold|lookup|netfn|oem`) nor in any extracted config; container
binary truncated. Tried: full-dump strings (narrow + wide encodings),
`grep -aob` symbol hunts, JFFS2 config grep, cramfs salvage grep.
2. GPU probe bus/address/protocol: no `nvidia|GPU|peci|NVML` content strings;
implementing binaries truncated. SDR gives numbers/entity/threshold-absence
only.
3. On-flash IPMI dispatch table location and the OEM command list: container
binary truncated; no table-shaped string/struct identified (would need
`IPMIMain` disassembly — recommended follow-up once a full image is obtained).
4. cramfs full auto-extract: format cracked (word-count namelen, `>>4` block
table, raw zlib offsets) but `cramfsx.py` still fails on later inodes; the
7z partial tree + jefferson slices cover every question except 1–3's missing
binaries.
5. Pre-`0xA50000` 64 KiB (`0xA40000–0xA4FFFF`, high-entropy + `0xFF`) and the
oversize `root`/`cramfs` length fields (`0xE30000`/`0xE15000` both overrun
flash end): purpose/meaning not determined; reported factually, not decoded.
