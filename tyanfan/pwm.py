"""ASPEED AST2400 PWM/tach duty control over the P2A bridge.

Register offsets and the duty field layout mirror the mainline driver
drivers/hwmon/aspeed-pwm-tacho.c. Each DUTYn register holds two channels:

    bits 15:0   first channel of the pair   (rise 7:0, fall 15:8)
    bits 31:16  second channel of the pair  (rise 23:16, fall 31:24)

With rise = 0, duty fraction = fall / 256. The factory idle on this chassis is
fall = 0x33 (20 %) on PWMA..PWMF and 0x00 on PWMG.

Only the DUTY registers are ever written. CTRL / CTRL_EXT / CLK_CTRL / TYPE*
are read-only for this project, which is what keeps the BMC's own thermal loop
alive underneath us: restoring the duty bytes hands authority straight back.
"""

from __future__ import annotations

from .ahb import Ahb

PWM_BASE = 0x1E786000

CTRL = 0x00
CLK_CTRL = 0x04
DUTY0 = 0x08
DUTY1 = 0x0C
TYPEM_CTRL = 0x10
TYPEM_CTRL1 = 0x14
TYPEN_CTRL = 0x18
TYPEN_CTRL1 = 0x1C
RESULT = 0x2C
CTRL_EXT = 0x40
CLK_CTRL_EXT = 0x44
DUTY2 = 0x48
DUTY3 = 0x4C
TYPEO_CTRL = 0x50
TYPEO_CTRL1 = 0x54

DUTY_REGS = (DUTY0, DUTY1, DUTY2, DUTY3)

DUMP_REGS = (
    ("CTRL", CTRL),
    ("CLK_CTRL", CLK_CTRL),
    ("DUTY0", DUTY0),
    ("DUTY1", DUTY1),
    ("TYPEM_CTRL", TYPEM_CTRL),
    ("TYPEM_CTRL1", TYPEM_CTRL1),
    ("TYPEN_CTRL", TYPEN_CTRL),
    ("TYPEN_CTRL1", TYPEN_CTRL1),
    ("RESULT", RESULT),
    ("CTRL_EXT", CTRL_EXT),
    ("CLK_CTRL_EXT", CLK_CTRL_EXT),
    ("DUTY2", DUTY2),
    ("DUTY3", DUTY3),
    ("TYPEO_CTRL", TYPEO_CTRL),
    ("TYPEO_CTRL1", TYPEO_CTRL1),
)

# channel -> (duty register, high half?, control register, enable bit)
CHANNELS: dict[str, tuple[int, bool, int, int]] = {
    "A": (DUTY0, False, CTRL, 8),
    "B": (DUTY0, True, CTRL, 9),
    "C": (DUTY1, False, CTRL, 10),
    "D": (DUTY1, True, CTRL, 11),
    "E": (DUTY2, False, CTRL_EXT, 8),
    "F": (DUTY2, True, CTRL_EXT, 9),
    "G": (DUTY3, False, CTRL_EXT, 10),
    "H": (DUTY3, True, CTRL_EXT, 11),
}

ALL_CHANNELS = tuple(CHANNELS)

# Factory duty bytes measured on this chassis, used as the last-resort
# restore target when no runtime state file is available.
FACTORY_FALL = {
    "A": 0x33,
    "B": 0x33,
    "C": 0x33,
    "D": 0x33,
    "E": 0x33,
    "F": 0x33,
    "G": 0x00,
    "H": 0x00,
}

FAN_NUM_EN_MASK = 0x00FF0000


class ChannelDisabled(RuntimeError):
    pass


def pct_to_byte(pct: float) -> int:
    return max(0, min(255, round(pct * 255 / 100)))


def byte_to_pct(value: int) -> float:
    return round(value * 100 / 255, 1)


class Pwm:
    def __init__(self, ahb: Ahb) -> None:
        self.ahb = ahb

    def read(self, offset: int) -> int:
        return self.ahb.read32(PWM_BASE + offset)

    def write(self, offset: int, value: int) -> None:
        self.ahb.write32(PWM_BASE + offset, value)

    def dump(self) -> list[tuple[str, int, int]]:
        return [(name, off, self.read(off)) for name, off in DUMP_REGS]

    def enabled(self, ch: str) -> bool:
        _, _, ctrl_reg, bit = CHANNELS[ch]
        return bool(self.read(ctrl_reg) & (1 << bit))

    def enabled_channels(self) -> list[str]:
        return [ch for ch in ALL_CHANNELS if self.enabled(ch)]

    def get_fall(self, ch: str) -> int:
        reg, high, _, _ = CHANNELS[ch]
        word = self.read(reg)
        half = (word >> 16) if high else (word & 0xFFFF)
        return (half >> 8) & 0xFF

    def get_rise(self, ch: str) -> int:
        reg, high, _, _ = CHANNELS[ch]
        word = self.read(reg)
        half = (word >> 16) if high else (word & 0xFFFF)
        return half & 0xFF

    def set_fall(self, ch: str, value: int, check_enabled: bool = True) -> None:
        if check_enabled and not self.enabled(ch):
            raise ChannelDisabled(f"PWM{ch} is disabled; refusing to write duty")
        reg, high, _, _ = CHANNELS[ch]
        self.write(reg, apply_fall(self.read(reg), high, value))

    def set_pct(self, ch: str, pct: float, check_enabled: bool = True) -> int:
        value = pct_to_byte(pct)
        self.set_fall(ch, value, check_enabled=check_enabled)
        return value

    def tach_channels(self) -> list[int]:
        fan_num_en = (self.read(CTRL) & FAN_NUM_EN_MASK) >> 16
        return [i for i in range(8) if fan_num_en & (1 << i)]

    def snapshot(self) -> dict[int, int]:
        return {reg: self.read(reg) for reg in DUTY_REGS}

    def restore(self, snap: dict[int, int]) -> None:
        for reg, value in snap.items():
            self.write(reg, value)


def apply_fall(word: int, high: bool, value: int) -> int:
    """Replace one channel's half of a DUTYn word with rise=0, fall=value.

    Pure function so the read-modify-write rule (never clobber the sibling
    channel) is unit-testable without hardware.
    """
    value = max(0, min(255, int(value))) & 0xFF
    half = value << 8
    if high:
        return ((word & 0x0000FFFF) | (half << 16)) & 0xFFFFFFFF
    return ((word & 0xFFFF0000) | half) & 0xFFFFFFFF


def parse_channels(spec: str | None, default: list[str] | None = None) -> list[str]:
    if not spec:
        return list(default) if default is not None else list(ALL_CHANNELS)
    out: list[str] = []
    for tok in spec.replace(",", " ").split():
        ch = tok.strip().upper()
        if ch not in CHANNELS:
            raise ValueError(f"unknown PWM channel {tok!r}")
        if ch not in out:
            out.append(ch)
    return out
