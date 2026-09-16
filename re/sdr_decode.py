#!/usr/bin/env python3
"""Decode an `ipmitool sdr dump` file.

Evidence tool for docs/bmc-re.md: prints per-sensor owner, entity, sensor
type, the Sensor Initialization / Capabilities bits (which say whether the
firmware lets anyone write the reading), and the linearization coefficients
that give the RPM-per-count and degC-per-count scaling.

    python3 re/sdr_decode.py /tmp/sdr.bin [name-substring ...]
"""

from __future__ import annotations

import sys

SENSOR_TYPES = {0x01: "Temperature", 0x02: "Voltage", 0x03: "Current", 0x04: "Fan"}
UNITS = {0: "unspecified", 1: "degC", 4: "Volts", 5: "Amps", 18: "RPM"}


def records(blob: bytes):
    off = 0
    while off + 5 <= len(blob):
        rec_id = blob[off] | (blob[off + 1] << 8)
        version, rtype, rlen = blob[off + 2], blob[off + 3], blob[off + 4]
        body = blob[off + 5 : off + 5 + rlen]
        if len(body) < rlen:
            break
        yield rec_id, version, rtype, body
        off += 5 + rlen


def twos(value: int, bits: int) -> int:
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def decode_full(body: bytes) -> dict:
    owner, owner_lun, number = body[0], body[1], body[2]
    entity_id, entity_inst = body[3], body[4]
    init, caps = body[5], body[6]
    stype, reading_type = body[7], body[8]
    units1, units2, units3 = body[15], body[16], body[17]
    linear = body[18]
    m = body[19] | ((body[20] & 0xC0) << 2)
    tolerance = body[20] & 0x3F
    b = body[21] | ((body[22] & 0xC0) << 2)
    r_exp = twos((body[23] >> 4) & 0x0F, 4)
    b_exp = twos(body[23] & 0x0F, 4)
    name_len = body[42] & 0x1F
    name = body[43 : 43 + name_len].decode("ascii", "replace")
    return {
        "name": name,
        "number": number,
        "owner": owner,
        "owner_lun": owner_lun,
        "entity": (entity_id, entity_inst),
        "init": init,
        "caps": caps,
        "settable": bool(caps & 0x80),
        "scanning_enabled": bool(init & 0x40),
        "event_gen_enabled": bool(init & 0x80),
        "init_scanning": bool(init & 0x01),
        "sensor_type": stype,
        "reading_type": reading_type,
        "units": (units1, units2, units3),
        "linear": linear,
        "m": twos(m, 10),
        "b": twos(b, 10),
        "r_exp": r_exp,
        "b_exp": b_exp,
    }


def decode_compact(body: bytes) -> dict:
    """Compact Sensor Record (type 0x02): no linearization, name at body[26]."""
    name_len = body[26] & 0x1F
    return {
        "name": body[27 : 27 + name_len].decode("ascii", "replace"),
        "number": body[2],
        "owner": body[0],
        "owner_lun": body[1],
        "entity": (body[3], body[4]),
        "init": body[5],
        "caps": body[6],
        "settable": bool(body[6] & 0x80),
        "scanning_enabled": bool(body[5] & 0x40),
        "init_scanning": bool(body[5] & 0x01),
        "sensor_type": body[7],
        "reading_type": body[8],
        "units": (body[14], body[15], body[16]),
        "m": 1,
        "b": 0,
        "r_exp": 0,
        "b_exp": 0,
        "linear": None,
    }


def decode_event_only(body: bytes) -> dict:
    """Event-Only Record (type 0x03): no reading scaling, name at body[12]."""
    name_len = body[11] & 0x1F
    return {
        "name": body[12 : 12 + name_len].decode("ascii", "replace"),
        "number": body[2],
        "owner": body[0],
        "owner_lun": body[1],
        "entity": (body[3], body[4]),
        "init": None,
        "caps": None,
        "settable": None,
        "scanning_enabled": None,
        "init_scanning": None,
        "sensor_type": body[5],
        "reading_type": body[6],
        "units": (None, None, None),
        "m": 1,
        "b": 0,
        "r_exp": 0,
        "b_exp": 0,
        "linear": None,
    }


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "/tmp/sdr.bin"
    wanted = [a.lower() for a in argv[2:]]
    blob = open(path, "rb").read()
    print(f"# {path}: {len(blob)} bytes")
    counts: dict[int, int] = {}
    for rec_id, _version, rtype, body in records(blob):
        counts[rtype] = counts.get(rtype, 0) + 1
        if rtype == 0x01 and len(body) >= 43:
            d = decode_full(body)
        elif rtype == 0x02 and len(body) >= 27:
            d = decode_compact(body)
        elif rtype == 0x03 and len(body) >= 12:
            d = decode_event_only(body)
        else:
            continue
        if wanted and not any(w in d["name"].lower() for w in wanted):
            continue
        per_count = d["m"] * (10 ** d["r_exp"])
        print(
            f"id={rec_id:#06x} t{rtype:02x} {d['name']:<20} num={d['number']:#04x} "
            f"owner={d['owner']:#04x}.{d['owner_lun'] & 3} "
            f"entity={d['entity'][0]:#04x}/{d['entity'][1]:#04x} "
            f"type={SENSOR_TYPES.get(d['sensor_type'], hex(d['sensor_type']))} "
            f"unit={UNITS.get(d['units'][1], d['units'][1])} "
            f"M={d['m']} B={d['b']} Rexp={d['r_exp']} Bexp={d['b_exp']} "
            f"per_count={per_count:g} "
            + (
                f"init={d['init']:#04x}(scanning={d['scanning_enabled']}) "
                f"caps={d['caps']:#04x}(settable={d['settable']})"
                if d["init"] is not None
                else "event-only record: no reading/scaling fields"
            )
        )
    print("# record type census: " + ", ".join(f"{k:#04x}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
