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
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
    global log
    log = logging.getLogger("simulator")


# ---------------- Helpers ----------------
def clean_int(val: str) -> int:
    """Convert string to int, stripping comments (; ...)."""
    return int(val.split(";")[0].strip(), 0)


def parse_pdos_from_eds(eds_path):
    """Parse all TPDO COB-IDs and mapping entries from EDS."""
    cfg = configparser.ConfigParser(strict=False, delimiters=("="))
    cfg.optionxform = str
    cfg.read(eds_path)

    pdos = []
    for sec in cfg.sections():
        if sec.startswith("180"):  # TPDO comm
            map_sec = sec.replace("180", "1A0")
            if map_sec in cfg:
                try:
                    cob_id = clean_int(cfg[f"{sec}sub1"]["DefaultValue"])
                    num_mapped = clean_int(cfg[f"{map_sec}sub0"]["DefaultValue"])
                    mappings = []
                    for sub in range(1, num_mapped + 1):
                        raw = clean_int(cfg[f"{map_sec}sub{sub}"]["DefaultValue"])
                        index = (raw >> 16) & 0xFFFF
                        subidx = (raw >> 8) & 0xFF
                        size = raw & 0xFF
                        mappings.append((index, subidx, size))
                        log.debug(
                            f"Parsed PDO {sec}: COB=0x{cob_id:X}, "
                            f"index=0x{index:04X}, sub={subidx}, size={size}"
                        )
                    pdos.append((cob_id, mappings))
                except Exception as e:
                    log.warning(f"Failed parsing {map_sec}: {e}")
    return pdos


def parse_sdos_from_eds(eds_path, pdos):
    """Parse all SDO variables from EDS [2000..9FFF], excluding PDO-mapped ones."""
    cfg = configparser.ConfigParser(strict=False, delimiters=("="))
    cfg.optionxform = str
    cfg.read(eds_path)

    mapped_pairs = {(idx, sub) for _, mappings in pdos for (idx, sub, _) in mappings}
    mapped_indices = {idx for _, mappings in pdos for (idx, _, _) in mappings}

    sdos = []
    for sec in cfg.sections():
        try:
            if not re.match(r'^(?:0x)?[0-9A-Fa-f]+(?:sub[0-9A-Fa-f]+)?$', sec):
                continue

            if "sub" in sec:
                base, sub = sec.split("sub")
                idx = int(base, 16)
                subidx = int(sub, 0)
            else:
                idx = int(sec, 16)
                subidx = 0

            if not (0x2000 <= idx <= 0x9FFF):
                continue

            if (idx, subidx) in mapped_pairs or idx in mapped_indices:
                continue

            if "DefaultValue" in cfg[sec]:
                val = clean_int(cfg[sec]["DefaultValue"])
                sdos.append((idx, subidx, val))
        except Exception:
            continue
    return sdos


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
    log.info(f"Sent frame COB=0x{arb_id:X}, data={data_bytes.hex(' ')}, error={error}")
    time.sleep(delay)


def get_node_id_from_eds(eds_path):
    """Extract Node ID from EDS file (fallback = 1)."""
    cfg = configparser.ConfigParser(strict=False, delimiters=("="))
    cfg.optionxform = str
    cfg.read(eds_path)

    for section in cfg.sections():
        if section.lower() in ("devicecomissioning", "devicecom", "communication"):
            for key in cfg[section]:
                if key.lower() in ("nodeid", "node_id", "node_id_defaultvalue", "defaultnodeid"):
                    try:
                        return int(cfg[section][key].split(";")[0].strip(), 0)
                    except Exception:
                        continue
    return 1


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
    send_frame(bus, 0x80 + node_id, data, error=error_frame)


# ---------------- Main ----------------
def main(interface="vcan0", count=5, delay:int=0, eds_path=None,
         enable_log=False, with_timestamp=False, with_emcy=False, with_err=False):

    if enable_log:
        enable_logging()

    bus = can.interface.Bus(channel=interface, interface="socketcan")

    pdos = parse_pdos_from_eds(eds_path) if eds_path else []
    sdos = parse_sdos_from_eds(eds_path, pdos) if eds_path else []
    node_id = get_node_id_from_eds(eds_path) if eds_path else 1
    manuf_bytes = get_manufacturer_from_eds(eds_path) if eds_path else None

    for i in tqdm(range(count), desc="Sending frames"):

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

        # PDOs
        for cob_id, mappings in pdos:
            data_bytes = b""
            for (idx, subidx, size) in mappings:
                val = float((i + idx + subidx) % 200)
                if size == 0x20:
                    data_bytes += struct.pack("<f", val)
                elif size == 0x10:
                    data_bytes += int(val).to_bytes(2, "little")
                elif size == 0x08:
                    data_bytes += int(val).to_bytes(1, "little")
                else:
                    data_bytes += b"\x00" * (size // 8)
            send_frame(bus, cob_id, data_bytes)

        # SDOs
        for (idx, sub, default) in sdos:
            if i % 10 == 0:
                cycles = i // 10
            val = default + cycles
            sdo_resp = bytes([
                0x4B if val <= 0xFF else 0x4F,
                idx & 0xFF,
                (idx >> 8) & 0xFF,
                sub,
            ]) + int(val).to_bytes(4, "little", signed=False)
            send_frame(bus, 0x580 + node_id, sdo_resp)

        time.sleep(delay / 1000)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", default="vcan0", help="socketcan interface (default: vcan0)")
    parser.add_argument("--count", type=int, default=5, help="number of update cycles to send")
    parser.add_argument("--delay", type=int, default=0, help="enable delay in milli-seconds between CAN frames (default: 0)")
    parser.add_argument("--eds", help="EDS file path")
    parser.add_argument("--log", action="store_true", help="enable logging to canopen_frame_simulator.log")
    parser.add_argument("--with-timestamp", action="store_true", help="send Time Stamp (0x100)")
    parser.add_argument("--with-emcy", action="store_true", help="send Emergency (0x80 + NodeID)")
    parser.add_argument("--with-err", action="store_true", help="send ERROR-flag on EMCY frames (is_error_frame) to simulate bus error")
    args = parser.parse_args()
    main(args.interface, args.count, args.delay, args.eds, args.log,
         args.with_timestamp, args.with_emcy, args.with_err)