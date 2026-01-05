#!/usr/bin/env python3
# ██╗ ██████╗ ████████╗ █████╗ ██████╗
# ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
# ██║██║   ██║   ██║   ███████║ █████╔╝
# ██║██║   ██║   ██║   ██╔══██║██╔═══╝
# ██║╚██████╔╝   ██║   ██║  ██║███████╗
# ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝
# Copyright (c) 2025 iota2 (iota2 Engineering Tools)
# Licensed under the MIT License. See LICENSE file in the project root for details.

"""
CANopenSimulator - Create and send CAN messages
===============================================

Frame simulator aligned with EDS content:

Features:
 - Parse TPDO mappings dynamically from an EDS file
 - Send mapped PDOs with auto-generated float/int values
 - Send only unmapped OD entries (>0x6000) as SDOs
 - Send heartbeat frames (0x700 + NodeID) from EDS
 - Optional Time Stamp (0x100)
 - Optional Emergency (0x80 + NodeID)
 - If no EDS provided, fallback to demo frames
 - Optional logging to `canopen_frame_simulator.log`

Usage examples:
---------------
# Run with virtual CAN and fallback demo frames
python simulcanopen_frame_simulatorate_can_frames.py --interface vcan0 --count 10

# Run with EDS-defined PDO/SDO mappings and TimeStamp
python canopen_frame_simulator.py --interface vcan0 --count 50 --eds sample_device.eds --with-timestamp

# Run with EMCY injection
python simulate_cancanopen_frame_simulator_frames.py --interface vcan0 --count 20 --eds sample_device.eds --with-emcy
"""

import time
import struct
import can
import argparse
import configparser
import logging
import re
from tqdm import tqdm
from datetime import datetime, timezone

# ---------------- Logging ----------------
log = logging.getLogger("simulator")
log.addHandler(logging.NullHandler())  # disabled by default


def enable_logging():
    """Enable logging to canopen_frame_simulator.log"""
    logging.basicConfig(
        filename="canopen_frame_simulator.log",
        filemode="w",
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
    global log
    log = logging.getLogger("simulator")


# ---------------- Helpers ----------------
def clean_int(val: str) -> int:
    """Convert string to int, stripping comments (; ...)."""
    return int(val.split(";")[0].strip(), 0)


# ---------------- EDS Services ----------------
def parse_tpdos_from_eds(eds_path):
    """Parse TPDO COB-IDs and mapping entries from EDS (CiA-301 compliant)."""
    cfg = configparser.ConfigParser(strict=False, delimiters=("="))
    cfg.optionxform = str
    cfg.read(eds_path)

    tpdos = []

    for n in range(0, 4):
        comm_sec = f"180{n}"
        map_sec = f"1A0{n}"

        if comm_sec not in cfg or map_sec not in cfg:
            continue

        try:
            # COB-ID from [180Xsub1]
            cob_sec = f"{comm_sec}sub1"
            if cob_sec not in cfg:
                raise KeyError(f"{cob_sec} missing")

            cob_id = clean_int(cfg[cob_sec]["DefaultValue"])

            # number of mapped entries from [1A0Xsub0]
            map0_sec = f"{map_sec}sub0"
            if map0_sec not in cfg:
                raise KeyError(f"{map0_sec} missing")

            num_mapped = clean_int(cfg[map0_sec]["DefaultValue"])

            mappings = []
            for sub in range(1, num_mapped + 1):
                mapn_sec = f"{map_sec}sub{sub}"
                if mapn_sec not in cfg:
                    raise KeyError(f"{mapn_sec} missing")

                raw = clean_int(cfg[mapn_sec]["DefaultValue"])

                index = (raw >> 16) & 0xFFFF
                subidx = (raw >> 8) & 0xFF
                size_bits = raw & 0xFF

                mappings.append((index, subidx, size_bits))

            tpdos.append((cob_id, mappings))

            log.info(
                f"Parsed TPDO {comm_sec}: COB=0x{cob_id:X}, mappings={len(mappings)}"
            )

        except Exception as e:
            log.warning(f"Failed parsing TPDO {comm_sec}: {e}")

    return tpdos


def parse_rpdos_from_eds(eds_path):
    """Parse RPDO COB-IDs and mapping entries from EDS (CiA-301 compliant)."""
    cfg = configparser.ConfigParser(strict=False, delimiters=("="))
    cfg.optionxform = str
    cfg.read(eds_path)

    rpdos = []

    for n in range(0, 4):
        comm_sec = f"140{n}"
        map_sec = f"160{n}"

        if comm_sec not in cfg or map_sec not in cfg:
            continue

        try:
            # COB-ID is in [140Xsub1].DefaultValue
            cob_sec = f"{comm_sec}sub1"
            if cob_sec not in cfg:
                raise KeyError(f"{cob_sec} missing")

            cob_id = clean_int(cfg[cob_sec]["DefaultValue"])

            # Number of mapped objects in [160Xsub0].DefaultValue
            map0_sec = f"{map_sec}sub0"
            if map0_sec not in cfg:
                raise KeyError(f"{map0_sec} missing")

            num_mapped = clean_int(cfg[map0_sec]["DefaultValue"])

            mappings = []
            for sub in range(1, num_mapped + 1):
                mapn_sec = f"{map_sec}sub{sub}"
                if mapn_sec not in cfg:
                    raise KeyError(f"{mapn_sec} missing")

                raw = clean_int(cfg[mapn_sec]["DefaultValue"])

                index = (raw >> 16) & 0xFFFF
                subidx = (raw >> 8) & 0xFF
                size_bits = raw & 0xFF

                mappings.append((index, subidx, size_bits))

            rpdos.append((cob_id, mappings))

            log.info(
                f"Parsed RPDO {comm_sec}: COB=0x{cob_id:X}, mappings={len(mappings)}"
            )

        except Exception as e:
            log.warning(f"Failed parsing RPDO {comm_sec}: {e}")

    return rpdos


def parse_sdos_from_eds(eds_path):
    """Parse OD entries with AccessType and DataType for SDO handling."""
    cfg = configparser.ConfigParser(strict=False, delimiters=("=",))
    cfg.optionxform = str
    cfg.read(eds_path)

    sdo_db = {}

    for sec in cfg.sections():
        try:
            if not re.fullmatch(r"(0x)?[0-9A-Fa-f]+(sub[0-9A-Fa-f]+)?", sec):
                continue

            if "sub" in sec:
                base, sub = sec.split("sub")
                idx = int(base, 16)
                subidx = int(sub, 0)
            else:
                idx = int(sec, 16)
                subidx = 0

            access = cfg[sec].get("AccessType", "UNKNOWN").strip().lower()
            if access == "unknown":
                continue

            dtype = int(cfg[sec].get("DataType", "0"), 0)

            default_raw = cfg[sec].get("DefaultValue", "").strip()

            # ---------- FIX START ----------
            if default_raw == "":
                value = None
            elif dtype == 0x09:  # VISIBLE_STRING
                value = default_raw
            elif dtype in (0x0A, 0x0B):  # OCTET / UNICODE STRING
                value = default_raw.encode("ascii", errors="replace")
            elif dtype == 0x0F:  # DOMAIN
                value = None  # DOMAIN content handled at runtime
            else:
                value = clean_int(default_raw)
            # ---------- FIX END ----------

            sdo_db[(idx, subidx)] = {
                "value": value,
                "access": access,
                "datatype": dtype,
            }

        except Exception as e:
            log.debug(f"Skipping OD entry {sec}: {e}")
            continue

    return sdo_db



def is_segmented_type(dtype: int) -> bool:
    """Check if datatype requires data to be sent in expedite or segmented mode."""
    return dtype in (0x09, 0x0A, 0x0B, 0x0F)  # strings + DOMAIN


def get_node_id_from_eds(eds_path, default=0x01):
    cfg = configparser.ConfigParser(strict=False)
    cfg.read(eds_path)

    for sec in cfg.sections():
        if sec.lower() in ("devicecommissioning", "devicecomissioning", "communication"):
            for key in cfg[sec]:
                if key.lower() == "nodeid":
                    try:
                        nid = int(cfg[sec][key].split(";")[0], 0)
                        if 1 <= nid <= 127:
                            return nid
                    except Exception:
                        pass

    return default


# ---------------- CANopen Services ----------------
def get_manufacturer_from_eds(eds_path):
    """Extract a short manufacturer string from EDS. Return up to 5 ASCII bytes.
    If none found or file missing, return None.
    """
    if not eds_path:
        return None
    try:
        cfg = configparser.ConfigParser(strict=False, delimiters=("="))
        cfg.optionxform = str
        cfg.read(eds_path)
        # look for common keys in likely sections
        for sec in cfg.sections():
            for key in cfg[sec]:
                if key.lower() in ("manufacturer", "manufacturername", "vendor", "vendorname"):
                    raw = cfg[sec][key].split(";")[0].strip()
                    if raw:
                        b = raw.encode('ascii', errors='replace')[:5]
                        if len(b) < 5:
                            b = b.ljust(5, b"\x00")
                        return b
    except Exception:
        return None
    return None


def send_heartbeat(bus, node_id):
    """Send heartbeat (0x700 + NodeID)."""

    log.debug(f"[HB] cob={0x700 + node_id} data={bytes([0x05])}")
    send_frame(bus, 0x700 + node_id, bytes([0x05]))  # 0x05 means Operational


def send_timestamp(bus):
    """Send Time Stamp (COB-ID 0x100) in CiA-301 format:
       4 bytes LE = milliseconds after midnight,
       2 bytes LE = days since 1984-01-01.
    """
    # use UTC so logs are consistent and deterministic
    now = datetime.now(timezone.utc)

    # milliseconds after midnight
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ms_after_midnight = int((now - midnight).total_seconds() * 1000) + int(now.microsecond / 1000)

    # days since 1984-01-01
    base = datetime(1984, 1, 1, tzinfo=timezone.utc)
    days_since_base = (now.date() - base.date()).days

    # guard (ms must fit in 4 bytes, days in 2 bytes)
    ms_after_midnight = ms_after_midnight % 86_400_000  # ensure inside a day
    days_since_base = max(0, min(days_since_base, 0xFFFF))

    data = ms_after_midnight.to_bytes(4, "little") + days_since_base.to_bytes(2, "little")
    log.debug(f"[TIME] cob={0x100} data={data.hex()}")
    send_frame(bus, 0x100, data)


def send_emcy(bus, node_id, error_code=0x1000, error_reg=0x01, manuf_bytes=None, error_frame=False):
    """Send Emergency (EMCY).
    manuf_bytes: optional bytes (<=5) to include as manufacturer-specific data. If None, 'DUMMY' used.
    error_frame: if True, send as an error-frame (is_error_frame=True)
    """
    if manuf_bytes is None:
        manuf = b'DUMMY'[:5]
    else:
        manuf = bytes(manuf_bytes)[:5]
        if len(manuf) < 5:
            manuf = manuf.ljust(5, b"\x00")
    data = int(error_code).to_bytes(2, "little") + bytes([int(error_reg) & 0xFF]) + manuf
    if len(data) < 8:
        data = data + b"\x00" * (8 - len(data))

    log.debug(f"[EMCY] cob={0x80 + node_id} data={data.hex()}")
    send_frame(bus, 0x80 + node_id, data, error=error_frame)


# ---------------- CAN Bus Operations ----------------
def send_frame(bus, arb_id, data_bytes, delay=0.05, error=False):
    """Send one CAN frame (max 8 bytes).
    If error=True, set is_error_frame so receivers can detect a bus error.
    """
    if len(data_bytes) > 8:
        data_bytes = data_bytes[:8]
    msg = can.Message(arbitration_id=arb_id,
                      data=bytes(data_bytes),
                      is_extended_id=False,
                      is_error_frame=bool(error))
    bus.send(msg)
    time.sleep(delay)


def handle_sdo_request(bus, msg, node_id, sdo_db):
    if msg.arbitration_id != (0x600 + node_id):
        return

    data = msg.data
    if len(data) < 4:
        return

    cs = data[0]
    index = data[1] | (data[2] << 8)
    sub = data[3]

    key = (index, sub)
    entry = sdo_db.get(key)

    # Object not SDO-visible
    if entry is None:
        send_sdo_abort(bus, node_id, index, sub, 0x06020000)  # object does not exist
        return

    access = entry["access"]

    # ---------------- READ ----------------
    if cs == 0x40:
        if access == "wo":
            send_sdo_abort(bus, node_id, index, sub, 0x06010001)
            return

        value = entry["value"]
        resp = bytearray(8)
        resp[0] = 0x43
        resp[1] = data[1]
        resp[2] = data[2]
        resp[3] = sub
        resp[4:8] = int(value).to_bytes(4, "little", signed=False)

        bus.send(can.Message(
            arbitration_id=0x580 + node_id,
            data=bytes(resp),
            is_extended_id=False
        ))
        log.info(f"SDO READ  idx=0x{index:04X} sub={sub} → {value}")

    # ---------------- WRITE ----------------
    elif cs in (0x2F, 0x2B, 0x23):
        if access == "ro":
            send_sdo_abort(bus, node_id, index, sub, 0x06010002)
            return

        size = {0x2F: 1, 0x2B: 2, 0x23: 4}[cs]
        value = int.from_bytes(data[4:4+size], "little")
        # Update local database
        entry["value"] = value

        resp = bytearray(8)
        resp[0] = 0x60
        resp[1] = data[1]
        resp[2] = data[2]
        resp[3] = sub

        bus.send(can.Message(
            arbitration_id=0x580 + node_id,
            data=bytes(resp),
            is_extended_id=False
        ))
        log.info(f"SDO WRITE idx=0x{index:04X} sub={sub} ← {value}")


def send_sdo_abort(bus, node_id, index, sub, abort_code):
    resp = bytearray(8)
    resp[0] = 0x80
    resp[1] = index & 0xFF
    resp[2] = (index >> 8) & 0xFF
    resp[3] = sub
    resp[4:8] = int(abort_code).to_bytes(4, "little")

    bus.send(can.Message(
        arbitration_id=0x580 + node_id,
        data=bytes(resp),
        is_extended_id=False
    ))
    log.info(
        f"SDO ABORT idx=0x{index:04X} sub={sub} code=0x{abort_code:08X}"
    )


def send_segmented_sdo(bus, node_id, index, sub, payload: bytes):
    """
    Send a segmented SDO download (CiA-301 compliant).
    """
    cob_tx = 0x600 + node_id

    size = len(payload)   # <-- FIX: define size before use

    # ---- Initiate segmented download ----
    init = bytearray(8)
    init[0] = 0x21  # initiate segmented download, size indicated
    init[1] = index & 0xFF
    init[2] = (index >> 8) & 0xFF
    init[3] = sub
    init[4:8] = size.to_bytes(4, "little")

    bus.send(can.Message(
        arbitration_id=cob_tx,
        data=bytes(init),
        is_extended_id=False
    ))

    # ---- Segments ----
    toggle = 0
    offset = 0

    while offset < size:
        chunk = payload[offset:offset + 7]
        offset += len(chunk)

        last = offset >= size
        pad = 7 - len(chunk)

        cs = (toggle << 4) | (pad << 1) | int(last)
        seg = bytes([cs]) + chunk + b"\x00" * pad

        bus.send(can.Message(
            arbitration_id=cob_tx,
            data=seg,
            is_extended_id=False
        ))

        toggle ^= 1


def handle_rpdo(msg, rpdos, sdo_db):
    for cob_id, mappings in rpdos:
        if msg.arbitration_id != cob_id:
            continue

        data = msg.data
        offset = 0

        for (idx, sub, size_bits) in mappings:
            size_bytes = size_bits // 8
            raw = data[offset:offset + size_bytes]
            val = int.from_bytes(raw, "little")

            key = (idx, sub)
            if key in sdo_db:
                sdo_db[key]["value"] = val

            offset += size_bytes

        log.info(f"RPDO RX COB=0x{cob_id:X} → " + ", ".join(val))


# ---------------- Main ----------------
def main(interface="vcan0", node_id=0x00, count=5, delay:int=0, eds_path=None, enable_log=False,
         with_timestamp=False, with_emcy=False, with_err=False, only_rx=False, only_tx=False):

    if enable_log:
        enable_logging()

    bus = can.interface.Bus(channel=interface, interface="socketcan")

    tpdos = parse_tpdos_from_eds(eds_path) if eds_path else []
    rpdos = parse_rpdos_from_eds(eds_path) if eds_path else []
    sdo_db = parse_sdos_from_eds(eds_path) if eds_path else {}

    # Extract Node ID
    if not node_id or node_id == 0x00:
        node_id = get_node_id_from_eds(eds_path, default=0x01) if eds_path else 0x01
    manuf_bytes = get_manufacturer_from_eds(eds_path) if eds_path else None

    # Define once, not inside the loop
    BASE_DOMAIN = bytes.fromhex(
        "00 11 22 33 44 55 66 77 88 99 AA BB CC DD EE FF"
    )

    for i in tqdm(range(count), desc="Sending frames"):

        if only_tx == False:
            # RX handling (SDO + RPDO)
            try:
                msg = bus.recv(timeout=0.0)
                if msg:
                    handle_sdo_request(bus, msg, node_id, sdo_db)
                    handle_rpdo(msg, rpdos, sdo_db)
            except Exception:
                pass

        if only_rx == False:
            # Heartbeat
            send_heartbeat(bus, node_id)

            # Time Stamp (if enabled)
            if with_timestamp:
                send_timestamp(bus)

            # EMCY (if enabled): send every cycle. Every 10th iteration increment error_code and shift error register bit.
            if with_emcy:
                # base error code increments every 10th iteration
                cycles = i // 10
                error_code = 0x1000 + cycles
                # compute err_reg as a single bit that shifts every 10th iteration
                # bit_pos cycles through 0..7
                bit_pos = cycles % 8
                err_reg = (1 << bit_pos) & 0xFF
                send_emcy(bus, node_id, error_code=error_code, error_reg=err_reg, manuf_bytes=manuf_bytes, error_frame=with_err)

            # TPDOs
            for cob_id, mappings in tpdos:
                data_bytes = b""
                for (idx, subidx, size) in mappings:
                    # base value increments every 10th iteration
                    cycles = i // 10
                    val = float(((idx + subidx)/1000 + cycles + (cycles / 10)))

                    if size == 0x20:
                        data_bytes += struct.pack("<f", val)
                    elif size == 0x10:
                        data_bytes += int(val).to_bytes(2, "little")
                    elif size == 0x08:
                        data_bytes += int(val).to_bytes(1, "little")
                    else:
                        data_bytes += b"\x00" * (size // 8)

                    log.debug(f"[TPDO] cob={cob_id} idx=0x{idx:04X} sub={subidx} data={data_bytes.hex()}")

                send_frame(bus, cob_id, data_bytes)

            # SDOs
            for (idx, sub), entry in sdo_db.items():

                # Simulate ony R) and RW objects
                if entry["access"] != "ro" and entry["access"] != "rw":
                    continue

                # Exclude PDO communication & mapping objects
                if 0x1400 <= idx <= 0x1BFF:
                    continue

                dtype = entry["datatype"]

                # ---------- BOOLEAN ----------
                if dtype == 0x01:  # BOOLEAN
                    # Initialize if missing
                    if entry["value"] is None:
                        entry["value"] = 0

                    # Toggle every 10th iteration
                    if i % 10 == 0 and i != 0:
                        entry["value"] ^= 1  # toggle 0 <-> 1

                    val = entry["value"] & 0x01

                    sdo_req = bytes([
                        0x2F,                 # expedited, 1 byte
                        idx & 0xFF,
                        (idx >> 8) & 0xFF,
                        sub,
                        val,
                        0x00, 0x00, 0x00
                    ])

                    log.debug(
                        f"[SDO BOOL] cob=0x{0x600 + node_id:03X} "
                        f"idx=0x{idx:04X} sub={sub} val={val}"
                    )
                    send_frame(bus, 0x600 + node_id, sdo_req)
                    continue

                # ---------- DOMAIN ----------
                if dtype == 0x0F:
                    domain_len = len(BASE_DOMAIN)

                    # Rotate by 1 byte every 10th iteration (pure rotation)
                    shift = (i // 10) % domain_len
                    payload = BASE_DOMAIN[shift:] + BASE_DOMAIN[:shift]

                    log.debug(
                        f"[SDO DOMAIN] idx=0x{idx:04X} sub={sub} "
                        f"shift={shift} data={payload.hex(' ')}"
                    )
                    send_segmented_sdo(bus, node_id, idx, sub, payload)
                    continue

                # ---------- VISIBLE_STRING ----------
                if dtype == 0x09:
                    # Default from EDS or fallback
                    value = entry["value"] or "Iteration: 1"

                    # Increment number every 10th iteration
                    if i % 10 == 0 and i != 0:
                        m = re.search(r"(\d+)$", value)
                        if m:
                            num = int(m.group(1)) + 1
                            value = re.sub(r"(\d+)$", str(num), value)
                            entry["value"] = value

                    payload = value.encode("ascii", errors="replace")

                    log.debug(
                        f"[SDO VISIBLE_STRING] idx=0x{idx:04X} sub={sub:02X} "
                        f"val='{value}'"
                    )
                    send_segmented_sdo(bus, node_id, idx, sub, payload)
                    continue

                # ---------- OTHER SEGMENTED TYPES ----------
                if is_segmented_type(dtype):
                    payload = entry.get("payload")
                    if payload is None:
                        payload = b"\x00" * 16
                        entry["payload"] = payload

                    log.debug(
                        f"[SDO SEG] idx=0x{idx:04X} sub={sub} len={len(payload)}"
                    )
                    send_segmented_sdo(bus, node_id, idx, sub, payload)
                    continue

                # ---------- SCALAR TYPES ----------
                if entry["value"] is None:
                    entry["value"] = 1

                # Increment scalar every 10th iteration
                if i % 10 == 0 and i != 0:
                    entry["value"] += 1

                val = entry["value"]

                if val <= 0xFF:
                    cs = 0x2F
                    data = val.to_bytes(1, "little")
                    pad = b"\x00" * 3
                elif val <= 0xFFFFFFFF:
                    cs = 0x23
                    data = val.to_bytes(4, "little")
                    pad = b""
                else:
                    continue

                sdo_req = bytes([
                    cs,
                    idx & 0xFF,
                    (idx >> 8) & 0xFF,
                    sub,
                ]) + data + pad

                log.debug(
                    f"[SDO REQ] cob=0x{0x600 + node_id:03X} "
                    f"idx=0x{idx:04X} sub={sub} val={val}"
                )
                send_frame(bus, 0x600 + node_id, sdo_req)

        time.sleep(delay / 1000)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", default="vcan0", help="socketcan interface (default: vcan0)")
    parser.add_argument("--node-id", help="Node ID to be simulated")
    parser.add_argument("--count", type=int, default=5, help="number of update cycles to send")
    parser.add_argument("--delay", type=int, default=0, help="enable delay in milli-seconds between CAN frames (  default: 0)")
    parser.add_argument("--eds", help="EDS file path")
    parser.add_argument("--log", action="store_true", help="enable logging to canopen_frame_simulator.log")
    parser.add_argument("--with-timestamp", action="store_true", help="send Time Stamp (0x100)")
    parser.add_argument("--with-emcy", action="store_true", help="send Emergency (0x80 + NodeID)")
    parser.add_argument("--with-err", action="store_true", help="send ERROR-flag on EMCY frames (is_error_frame) to simulate bus error")
    parser.add_argument("--only-rx", action="store_true", help="simulate only messages reception")
    parser.add_argument("--only-tx", action="store_true", help="simulate only messages transmission")
    args = parser.parse_args()
    main(args.interface, int(args.node_id), args.count, args.delay, args.eds, args.log,
         args.with_timestamp, args.with_emcy, args.with_err, args.only_rx, args.only_tx,)