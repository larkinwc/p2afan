"""Temperature providers.

Every provider exposes `name` and `read() -> float | None` in degrees C and
returns None on any failure. None never degrades to 0.0: the control loop must
be able to tell "cold" from "no idea", because the latter means failsafe.
"""

from __future__ import annotations

import glob
import re
import subprocess

from . import ipmi

_FLOAT = re.compile(r"-?\d+(?:\.\d+)?")


class Source:
    kind = "base"

    def __init__(self, name: str) -> None:
        self.name = name

    def read(self) -> float | None:  # pragma: no cover - interface
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"


class IpmiSensor(Source):
    """A BMC sensor read with `ipmitool raw 0x04 0x2d <sensor>`."""

    kind = "ipmi"

    def __init__(self, name: str, sensor: int, timeout: float = 5.0) -> None:
        super().__init__(name)
        self.sensor = int(sensor)
        self.timeout = timeout

    def read(self) -> float | None:
        try:
            value = ipmi.read_temp(self.sensor, timeout=self.timeout)
        except Exception:
            return None
        return None if value is None else float(value)


class Hwmon(Source):
    """Any hwmon temp*_input node, e.g. /sys/class/hwmon/hwmon3/temp1_input."""

    kind = "hwmon"

    def __init__(self, name: str, path: str) -> None:
        super().__init__(name)
        self.path = path

    def read(self) -> float | None:
        try:
            with open(self.path) as fh:
                return int(fh.read().strip()) / 1000.0
        except Exception:
            return None


class PciHwmon(Source):
    """Hottest hwmon input exported by an arbitrary PCI device.

    This is the any-PCIe-card path (AMD Radeon Pro V340 included) for cards the
    BMC cannot see but whose host driver exports hwmon.
    """

    kind = "pci"

    def __init__(self, name: str, bdf: str, label: str | None = None) -> None:
        super().__init__(name)
        self.bdf = bdf if ":" in bdf.split(".")[0] else bdf
        if self.bdf.count(":") == 1:
            self.bdf = "0000:" + self.bdf
        self.label = label

    def _inputs(self) -> list[str]:
        pattern = f"/sys/bus/pci/devices/{self.bdf}/hwmon/hwmon*/temp*_input"
        paths = sorted(glob.glob(pattern))
        if self.label is None:
            return paths
        kept = []
        for path in paths:
            label_path = path.replace("_input", "_label")
            try:
                with open(label_path) as fh:
                    if fh.read().strip() == self.label:
                        kept.append(path)
            except OSError:
                continue
        return kept

    def read(self) -> float | None:
        best: float | None = None
        for path in self._inputs():
            try:
                with open(path) as fh:
                    value = int(fh.read().strip()) / 1000.0
            except Exception:
                continue
            if best is None or value > best:
                best = value
        return best


class Exec(Source):
    """Run an arbitrary command; use the first float on stdout as degrees C.

    The escape hatch for any device or script that can report a temperature.
    """

    kind = "exec"

    def __init__(
        self, name: str, command: list[str] | str, timeout: float = 4.0
    ) -> None:
        super().__init__(name)
        self.command = [command] if isinstance(command, str) else list(command)
        self.timeout = timeout

    def read(self) -> float | None:
        try:
            proc = subprocess.run(
                self.command, capture_output=True, text=True, timeout=self.timeout
            )
            if proc.returncode != 0:
                return None
            match = _FLOAT.search(proc.stdout)
            return None if match is None else float(match.group(0))
        except Exception:
            return None


KINDS = {
    "ipmi": IpmiSensor,
    "hwmon": Hwmon,
    "pci": PciHwmon,
    "exec": Exec,
}


def build(spec: dict) -> Source:
    """Build a source from a config table: {kind = "...", name = "...", ...}."""
    spec = dict(spec)
    kind = spec.pop("kind", None)
    if kind not in KINDS:
        raise ValueError(f"unknown source kind {kind!r} (have {sorted(KINDS)})")
    name = spec.pop("name", None)
    if not name:
        raise ValueError(f"source of kind {kind!r} is missing a name")
    try:
        return KINDS[kind](name, **spec)
    except TypeError as exc:
        raise ValueError(f"bad options for source {name!r} ({kind}): {exc}") from exc
