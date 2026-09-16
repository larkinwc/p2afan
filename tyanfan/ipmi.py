"""Thin ipmitool wrappers for BMC sensor reads and sensor-reading injection.

`ipmitool raw 0x04 0x2d <sensor>` (Get Sensor Reading) costs ~45 ms on this
host, versus ~1-2.6 s for `ipmitool sdr elist full`, so the control loop only
ever uses the raw form.

Response bytes: <reading> <status1> <status2> <status3>.
  status1 bit5 set  -> reading unavailable ("No Reading"); absent sensors
                       answer 00 e0 00 80.
Scaling measured on this firmware (rev 9.01):
  temperature sensors  1 degC per count
  SYS_FAN_n sensors    90 RPM per count
"""

from __future__ import annotations

import os
import subprocess

GET_SENSOR_READING = ("0x04", "0x2d")
SET_SENSOR_READING = ("0x04", "0x30")

FAN_RPM_PER_COUNT = 90
READING_UNAVAILABLE = 0x20

# Sensor numbers from `ipmitool sdr elist full` on this chassis.
SENSORS = {
    "CPU0_DTS": 0x01,
    "CPU1_DTS": 0x02,
    "LAN_Area": 0x05,
    "SYS_Air_Inlet": 0x06,
    "MB_Air_Inlet": 0x07,
    "SYS_Air_Outlet": 0x08,
    "PCH": 0x0A,
    "GPU0_Core0_TEMP": 0x20,
    "GPU1_Core0_TEMP": 0x22,
    "GPU2_Core0_TEMP": 0x24,
    "GPU3_Core0_TEMP": 0x26,
    "GPU4_Core0_TEMP": 0x28,
    "GPU5_Core0_TEMP": 0x2A,
    "GPU6_Core0_TEMP": 0x2C,
    "GPU7_Core0_TEMP": 0x2E,
}

FAN_SENSORS = {
    "SYS_FAN_1": 0x32,
    "SYS_FAN_2": 0x33,
    "SYS_FAN_3": 0x34,
    "SYS_FAN_4": 0x35,
    "SYS_FAN_5": 0x36,
    "SYS_FAN_6": 0x37,
}

DIMM_SENSORS = (0x41, 0x44, 0x4A, 0x4D, 0x50, 0x53, 0x56)


class IpmiError(RuntimeError):
    pass


def _argv(args: tuple[str, ...] | list[str]) -> list[str]:
    cmd = ["ipmitool", "raw", *args]
    if os.geteuid() != 0:
        return ["sudo", "-n", *cmd]
    return cmd


def _raw(args: list[str], timeout: float = 5.0) -> list[int]:
    proc = subprocess.run(
        _argv(args), capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise IpmiError(
            f"{' '.join(args)} failed rc={proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    return [int(tok, 16) for tok in proc.stdout.split()]


def read_raw(sensor: int, timeout: float = 5.0) -> int | None:
    """Return the raw reading byte, or None when the sensor has no reading."""
    try:
        data = _raw([*GET_SENSOR_READING, hex(sensor)], timeout=timeout)
    except (IpmiError, subprocess.SubprocessError, ValueError):
        return None
    if len(data) < 2:
        return None
    if data[1] & READING_UNAVAILABLE:
        return None
    return data[0]


def read_temp(sensor: int, timeout: float = 5.0) -> int | None:
    return read_raw(sensor, timeout=timeout)


def read_fan_rpm(sensor: int, timeout: float = 5.0) -> int | None:
    raw = read_raw(sensor, timeout=timeout)
    return None if raw is None else raw * FAN_RPM_PER_COUNT


def read_all_fans(timeout: float = 5.0) -> dict[str, int | None]:
    return {
        name: read_fan_rpm(num, timeout=timeout)
        for name, num in FAN_SENSORS.items()
    }


def set_sensor_reading(sensor: int, value: int, timeout: float = 5.0) -> None:
    """Set Sensor Reading (netfn 0x04 cmd 0x30).

    Operation byte 0x01 = "write the given value to the sensor reading byte".
    Raises IpmiError on a non-zero completion code.
    """
    value = max(0, min(255, int(value)))
    _raw(
        [
            *SET_SENSOR_READING,
            hex(sensor),
            "0x01",
            "0x00",
            hex(value),
            "0x00",
            "0x00",
            "0x00",
            "0x00",
            "0x00",
        ],
        timeout=timeout,
    )
