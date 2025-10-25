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


def send_frame(bus, arb_id, data_bytes, delay=0.05):
    """Send one CAN frame (max 8 bytes)."""
    if len(data_bytes) > 8:
        data_bytes = data_bytes[:8]
    msg = can.Message(arbitration_id=arb_id,
                      data=bytes(data_bytes),
                      is_extended_id=False)
    bus.send(msg)
    log.info(f"Sent frame COB=0x{arb_id:X}, data={data_bytes.hex(' ')}")
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
def send_heartbeat(bus, node_id):
    """Send heartbeat (0x700 + NodeID)."""
    send_frame(bus, 0x700 + node_id, bytes([0x05]))  # 0x05 means Operational


def send_timestamp(bus):
    """Send Time Stamp (COB-ID 0x100)."""
    now = datetime.now(timezone.utc)
    seconds = int(now.timestamp())
    msec = int(now.microsecond / 1000)  # convert to ms (0-999)
    data = seconds.to_bytes(4, "little") + msec.to_bytes(2, "little")
    send_frame(bus, 0x100, data)


def send_emcy(bus, node_id, error_code=0x1000, error_reg=0x01):
    """Send Emergency (EMCY)."""
    data = error_code.to_bytes(2, "little") + bytes([error_reg]) + b"\x00\x00\x00\x00\x00"
    send_frame(bus, 0x80 + node_id, data)


# ---------------- Main ----------------
def main(interface="vcan0", count=5, delay:int=0, eds_path=None,
         enable_log=False, with_timestamp=False, with_emcy=False):

    if enable_log:
        enable_logging()

    bus = can.interface.Bus(channel=interface, interface="socketcan")

    pdos = parse_pdos_from_eds(eds_path) if eds_path else []
    sdos = parse_sdos_from_eds(eds_path, pdos) if eds_path else []
    node_id = get_node_id_from_eds(eds_path) if eds_path else 1

    for i in tqdm(range(count), desc="Sending frames"):
        # Heartbeat
        send_heartbeat(bus, node_id)

        # Time Stamp (if enabled)
        if with_timestamp:
            send_timestamp(bus)

        # EMCY (if enabled, every 5th cycle for demo)
        if with_emcy and (i % 5 == 0):
            send_emcy(bus, node_id)

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
            sdo_resp = bytes([
                0x4B if default <= 0xFF else 0x4F,
                idx & 0xFF,
                (idx >> 8) & 0xFF,
                sub,
            ]) + default.to_bytes(4, "little", signed=False)
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
    args = parser.parse_args()
    main(args.interface, args.count, args.delay, args.eds, args.log,
         args.with_timestamp, args.with_emcy)
