"""ASPEED PCIe-to-AHB (P2A) bridge access from the host.

The ASPEED VGA function 0000:0c:00.0 exposes BAR1 = 128 KiB at c7000000.
Layout used here:

    0x0f000  P2A bridge enable (bit0)
    0x0f004  AHB window base (must be 64 KiB aligned)
    0x10000  64 KiB window onto AHB at the base programmed above

Access goes through the PCI sysfs resource file, so CONFIG_STRICT_DEVMEM does
not apply. Root is required (resource1 is mode 0600).
"""

from __future__ import annotations

import fcntl
import mmap
import os
import struct
import time

BAR = "/sys/bus/pci/devices/0000:0c:00.0/resource1"
SIZE = 0x20000
CFG_ENABLE = 0xF000
CFG_WINDOW = 0xF004
WINDOW = 0x10000
WINDOW_SIZE = 0x10000
WINDOW_MASK = WINDOW_SIZE - 1

RUN_DIR = "/run/p2afan"
LOCK_PATH = RUN_DIR + "/p2a.lock"

# Sanity-probe target: ASPEED PWM/tach controller.
PWM_BASE = 0x1E786000


class BridgeUnavailable(RuntimeError):
    """The P2A bridge is disabled, locked, or not answering sanely."""


class BridgeBusy(RuntimeError):
    """Another process holds the P2A lock."""


class Ahb:
    """Exclusive, lock-protected handle on the P2A bridge.

    Use as a context manager so the original AHB window base is restored and
    the advisory lock released even on error.
    """

    def __init__(self, bar: str = BAR, lock_timeout: float | None = None) -> None:
        self._bar = bar
        os.makedirs(RUN_DIR, mode=0o700, exist_ok=True)
        self._lock_fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self._acquire(lock_timeout)
        except BaseException:
            os.close(self._lock_fd)
            raise
        try:
            self._fd = os.open(bar, os.O_RDWR | os.O_SYNC)
        except OSError:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            raise
        try:
            self._map = mmap.mmap(self._fd, SIZE)
        except OSError:
            os.close(self._fd)
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            raise
        self._orig_window = self.read_cfg(CFG_WINDOW)
        self._window = self._orig_window

    def _acquire(self, timeout: float | None) -> None:
        """Take the P2A lock, bounded when a timeout is given.

        Mutual exclusion is mandatory even for reads: every access re-points
        the shared AHB window register, so two concurrent users would read or
        write each other's addresses. A bounded wait keeps CLI calls from
        hanging forever behind the long-lived daemon.
        """
        if timeout is None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            return
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BridgeBusy(
                        f"P2A bridge lock {LOCK_PATH} held by another process "
                        f"after {timeout:g}s; stop p2afan.service first "
                        f"(`p2afan status` needs no lock)"
                    ) from None
                time.sleep(0.05)

    # -- raw BAR helpers -------------------------------------------------
    def read_cfg(self, offset: int) -> int:
        return struct.unpack_from("<I", self._map, offset)[0]

    def write_cfg(self, offset: int, value: int) -> None:
        struct.pack_into("<I", self._map, offset, value & 0xFFFFFFFF)

    # -- AHB access ------------------------------------------------------
    def _select(self, addr: int) -> int:
        base = addr & 0xFFFF0000
        if base != self._window:
            self.write_cfg(CFG_WINDOW, base)
            # Read back to force the posted write out before using the window.
            self._window = self.read_cfg(CFG_WINDOW)
            if self._window != base:
                raise BridgeUnavailable(
                    f"window base {base:#010x} did not stick (read {self._window:#010x})"
                )
        return WINDOW + (addr & WINDOW_MASK)

    def read32(self, addr: int) -> int:
        return struct.unpack_from("<I", self._map, self._select(addr))[0]

    def write32(self, addr: int, value: int) -> None:
        struct.pack_into("<I", self._map, self._select(addr), value & 0xFFFFFFFF)

    def read_block(self, addr: int, length: int) -> bytes:
        """Read `length` bytes starting at `addr`, spanning windows as needed."""
        out = bytearray()
        while length:
            off = self._select(addr)
            chunk = min(length, WINDOW_SIZE - (addr & WINDOW_MASK))
            out += self._map[off : off + chunk]
            addr += chunk
            length -= chunk
        return bytes(out)

    # -- bridge state ----------------------------------------------------
    def ensure_bridge(self) -> None:
        if self.read_cfg(CFG_ENABLE) & 1 == 0:
            self.write_cfg(CFG_ENABLE, 1)
            if self.read_cfg(CFG_ENABLE) & 1 == 0:
                raise BridgeUnavailable("P2A enable bit will not set")
        ctrl = self.read32(PWM_BASE + 0x00)
        clk = self.read32(PWM_BASE + 0x04)
        for name, val in (("CTRL", ctrl), ("CLK_CTRL", clk)):
            if val in (0x00000000, 0xFFFFFFFF):
                raise BridgeUnavailable(
                    f"PWM {name} reads {val:#010x}; AHB reads are not landing"
                )
        if ctrl & 1 == 0:
            raise BridgeUnavailable(f"PWM clock disabled (CTRL={ctrl:#010x})")

    @property
    def orig_window(self) -> int:
        return self._orig_window

    def close(self) -> None:
        try:
            if self._window != self._orig_window:
                self.write_cfg(CFG_WINDOW, self._orig_window)
                self._window = self._orig_window
        finally:
            try:
                self._map.close()
            finally:
                try:
                    os.close(self._fd)
                finally:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                    os.close(self._lock_fd)

    def __enter__(self) -> "Ahb":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
