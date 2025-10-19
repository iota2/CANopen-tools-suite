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
CANopen Sniffer — Features & Run Instructions
=============================================

Features:
- PyQt5 GUI: scrolling, searchable table of CAN frames (PDO, SDO, Heartbeat, Emergency, Time)
- EDS/OD-aware decoding:
    * Uses canopen.LocalNode metadata when available
    * Parses EDS INI-style file as fallback
    * Builds hierarchical names like Parent.Child (e.g., ImpedanceMeasurement.channel1)
    * Shows Data Type, Index, Subindex, Raw Data and Decoded Data
    * Applies engineering units & scaling (toggleable)
- Fixed vs Sequential display mode
- Reversible filtering (filter applies to buffer and keeps updating matching rows)
- Pause, Clear, Filter controls
- Copy selected rows and Copy as CSV (context menu)
- Export -> Export Data to CSV (All or Filtered)
- Export -> Export Histogram CSV (COB-ID counts, color codes)
- Histogram (per-COB counts) and Frame-rate sparkline (live)
- Deterministic per-COB colors
- Decoded Data column truncated to 40 chars (tooltips & details contain full text)
- Row color coding by frame Type
- Persisted user preferences via QSettings
- Unit tests (pytest) and simulator (`sim_can_frames.py`) for `vcan0`

Quick start:
1. Install dependencies:
   pip install python-can canopen PyQt5 pytest

2. (Linux) Create a virtual CAN interface for testing:
   sudo modprobe vcan
   sudo ip link add dev vcan0 type vcan
   sudo ip link set dev vcan0 up

3. Run GUI:
   python canopen_bus_sniffer_gui.py --eds sample_device.eds --interface vcan0

4. Simulate frames (in another terminal):
   python sim_can_frames.py --interface vcan0

5. Run unit tests:
   pytest -q

Files:
- canopen_bus_sniffer.py      # main GUI application
- sample_device.eds       # sample EDS used for testing
- sim_can_frames.py       # simulator for vcan0
- tests/test_sniffer.py   # pytest-based unit tests
"""

import sys
import os
import re
import time
import struct
import json
import csv
import configparser
import hashlib
import argparse
from collections import deque, defaultdict
from typing import Optional, Tuple, List, Dict, Any

import can
import canopen
try:
    from can.io.pcap import PcapWriter   # newer versions
except ImportError:
    try:
        from can.interfaces.pcap import PcapWriter   # older versions
    except ImportError:
        PcapWriter = None
from PyQt5 import QtWidgets, QtCore, QtGui


import logging
logging.basicConfig(
    filename="canopen_bus_sniffer_gui.log",
    filemode="w",
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s"
)

# ---------------- constants ----------------
APP_ORG = "iota2"
APP_NAME = "CANopenSnifferGUI"
DECODED_CHAR_LIMIT = 40
BUFFER_MAX = 20000
LOAD_WINDOW = 5.0

# Full CiA301 datatype map
CANOPEN_DATATYPE_MAP = {
    0x0001: "BOOLEAN",
    0x0002: "INTEGER8",
    0x0003: "INTEGER16",
    0x0004: "INTEGER32",
    0x0005: "UNSIGNED8",
    0x0006: "UNSIGNED16",
    0x0007: "UNSIGNED32",
    0x0008: "REAL32",
    0x0009: "REAL64",
    0x000A: "VISIBLE_STRING",
    0x000B: "OCTET_STRING",
    0x000C: "UNICODE_STRING",
    0x000D: "TIME_OF_DAY",
    0x000E: "TIME_DIFFERENCE",
    0x000F: "DOMAIN",
}

# ---------------- utilities ----------------
def color_for_cob(cob: int) -> QtGui.QColor:
    """Deterministic color per COB ID."""
    h = hashlib.sha1(str(cob).encode("utf-8")).digest()
    r, g, b = h[0], h[1], h[2]
    r = 100 + (r % 156)
    g = 80 + (g % 176)
    b = 60 + (b % 196)
    return QtGui.QColor(r, g, b)

def color_for_type(ftype: str) -> QtGui.QColor:
    if ftype == "PDO":
        return QtGui.QColor(220, 240, 255)
    if ftype == "SDO":
        return QtGui.QColor(220, 255, 220)
    if ftype in ("Heartbeat", "Emergency", "Time"):
        return QtGui.QColor(255, 250, 200)
    return QtGui.QColor(245, 245, 245)

def bytes_to_hex_str(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)

# ---------------- ODVariableMapper ----------------
class ODVariableMapper:
    """
    Map OD index/subindex -> name, type, unit, factor.
    Constructor kept compatible: (local_node: Optional[canopen.LocalNode], eds_path: Optional[str])
    """

    def __init__(self, local_node: Optional[Any], eds_path: Optional[str] = None, csv_file: str = "od_changes.csv"):
        self.node = local_node
        self.eds_path = eds_path
        self.csv_file = csv_file

        # Maps: (index, sub) -> values
        self._index_to_name: Dict[Tuple[int, int], str] = {}
        self._index_to_type: Dict[Tuple[int, int], str] = {}
        self._index_to_unit: Dict[Tuple[int, int], Optional[str]] = {}
        self._index_to_factor: Dict[Tuple[int, int], Optional[float]] = {}
        self._values: Dict[Tuple[int, int], Tuple[Any, Optional[bytes]]] = {}

        # Load from canopen LocalNode.sdo first (when provided)
        if self.node is not None:
            self._parse_localnode_sdo()

        # Fallback: parse EDS (robust numeric section handling)
        if eds_path:
            try:
                self._parse_eds_manual(eds_path)
            except Exception:
                # parsing EDS should not crash; swallow errors
                pass

    # -------------------- LocalNode parsing --------------------
    def _parse_localnode_sdo(self):
        """
        Read names/types/units/factor from a canopen.LocalNode.sdo object.
        Keep parent (index,0) as object name whenever available — do not let sub0 override a real parent.
        """
        try:
            for idx, entry in self.node.sdo.items():
                # If entry has a top-level name, record it as parent (index,0) unless it's a 'highest sub-index' placeholder
                parent_name = getattr(entry, "name", None)
                if parent_name:
                    pn = str(parent_name).strip()
                    if pn and "highest sub-index" not in pn.lower():
                        self._index_to_name[(idx, 0)] = pn

                # If entry is sub-indexed (dict-like), iterate subentries
                if hasattr(entry, "__getitem__"):
                    for sub, subentry in entry.items():
                        try:
                            sname = getattr(subentry, "name", None)
                            if sname:
                                sname = str(sname).strip()
                                # skip 'Highest sub-index supported' from being used as child name if desired
                                if "highest sub-index" in sname.lower():
                                    # record existence but do not use for naming parent
                                    self._values.setdefault((idx, sub), (None, None))
                                else:
                                    self._index_to_name[(idx, sub)] = sname

                            # datatype detection
                            dtype_obj = getattr(subentry, "data_type", None) or getattr(subentry, "datatype", None) or getattr(subentry, "type", None)
                            dtype_name = None
                            if dtype_obj is not None:
                                # if it's an object with .name or a number
                                if isinstance(dtype_obj, int):
                                    dtype_name = CANOPEN_DATATYPE_MAP.get(dtype_obj, f"0x{dtype_obj:04X}")
                                else:
                                    dtype_name = getattr(dtype_obj, "name", None) or str(dtype_obj)
                            if dtype_name:
                                self._index_to_type[(idx, sub)] = str(dtype_name).upper()

                            # unit/factor if present
                            unit = getattr(subentry, "unit", None) or getattr(subentry, "unit_name", None)
                            if unit:
                                self._index_to_unit[(idx, sub)] = str(unit)
                            factor = getattr(subentry, "factor", None)
                            if factor is not None:
                                try:
                                    self._index_to_factor[(idx, sub)] = float(factor)
                                except Exception:
                                    pass

                            # ensure value slot
                            self._values.setdefault((idx, sub), (None, None))
                        except Exception:
                            continue
                else:
                    # single object entry: ensure parent is registered
                    self._values.setdefault((idx, 0), (None, None))
        except Exception:
            # Non-fatal
            pass

    # -------------------- EDS parsing --------------------
    def _parse_eds_manual(self, eds_path: str):
        """
        Robust EDS parser: splits sections manually to accept numeric section headers like [6005] and [6005sub1].
        Stores ParameterName for (index,0) from [6005] and for (index,sub) from [6005subX].
        Skips sub0 entries that are 'Highest sub-index supported' so they don't become the parent name.
        """
        with open(eds_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        # Split into sections: produce list like ["", "6005", "body1", "6005sub1", "body2", ...]
        parts = re.split(r"\r?\n\[(.+?)\]\r?\n", "\n" + content)
        for i in range(1, len(parts), 2):
            sec = parts[i].strip()
            body = parts[i + 1]

            cfg = configparser.ConfigParser(strict=False)
            cfg.optionxform = str
            try:
                cfg.read_string("[X]\n" + body)
            except Exception:
                continue
            opts = cfg["X"]

            # helper to get option case-insensitively (ignoring whitespace)
            def _get_opt(key: str):
                key_cmp = key.strip().lower().replace(" ", "")
                for k, v in opts.items():
                    if k.strip().lower().replace(" ", "") == key_cmp:
                        return v
                return None

            # parse index/sub from section name robustly
            m = re.match(r'^\s*(?:0x)?([0-9A-Fa-f]+)(?:[^\S\r\n]*[sS][uU][bB]\s*(?:0x)?([0-9A-Fa-f]+))?\s*$', sec)
            if m:
                idx = int(m.group(1), 16)
                sub = int(m.group(2), 16) if m.group(2) else 0
            else:
                # fallback: try simple split
                if "sub" in sec.lower():
                    try:
                        base, subtxt = sec.lower().split("sub", 1)
                        idx = int(base.strip(), 0)
                        sub = int(subtxt.strip(), 0)
                    except Exception:
                        continue
                else:
                    try:
                        idx = int(sec.strip(), 0)
                        sub = 0
                    except Exception:
                        continue

            pname = _get_opt("ParameterName")
            dtval = _get_opt("DataType")
            unit = _get_opt("Unit") or _get_opt("UnitName")
            factor = _get_opt("Factor")

            # store names: ensure parent (idx,0) is object name from [idx] section (and do not allow sub0 placeholder to override)
            if pname:
                pname_s = pname.strip()
                if sub == 0:
                    # Only set parent if it's not a 'Highest sub-index supported' placeholder
                    if "highest sub-index" not in pname_s.lower():
                        self._index_to_name[(idx, 0)] = pname_s
                else:
                    # sub > 0 -> normal child entry
                    if "highest sub-index" not in pname_s.lower():
                        self._index_to_name[(idx, sub)] = pname_s

            # store datatype if present
            if dtval:
                try:
                    dt_code = int(str(dtval).strip(), 0)
                    self._index_to_type[(idx, sub)] = CANOPEN_DATATYPE_MAP.get(dt_code, f"0x{dt_code:04X}")
                except Exception:
                    # maybe dtval is a name already
                    self._index_to_type[(idx, sub)] = str(dtval).upper()

            if unit:
                self._index_to_unit[(idx, sub)] = unit.strip()

            if factor:
                try:
                    self._index_to_factor[(idx, sub)] = float(factor)
                except Exception:
                    pass

            self._values.setdefault((idx, sub), (None, None))

    # -------------------- update/log --------------------
    def update_value(self, index: int, subindex: int, value, raw_data: Optional[bytes]):
        key = (index, subindex)
        old = self._values.get(key, (None, None))
        new = (value, raw_data)
        self._values[key] = new
        if old != new:
            self.log_od_change(key, value, raw_data)

    def log_od_change(self, key: Tuple[int, int], value, raw_data: Optional[bytes]):
        try:
            with open(self.csv_file, "a", newline="") as f:
                w = csv.writer(f)
                raw_hex = bytes_to_hex_str(raw_data or b"")
                name = self.get_full_name(key[0], key[1])
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), name, value, raw_hex])
        except Exception:
            pass

    # -------------------- lookup helpers --------------------
    def get_full_name(self, index: int, sub: int) -> str:
        """
        Return a human-friendly name:
         - sub == 0 => object ParameterName (e.g. TemperatureMeasurement) or hex fallback
         - sub > 0  => Parent.Child (e.g. TemperatureMeasurement.channel1) if parent exists, otherwise child or hex fallback
         - Skip using sub0 (Highest sub-index supported) as parent
        """
        parent = self._index_to_name.get((index, 0))
        child = self._index_to_name.get((index, sub))

        # direct parent (object)
        if sub == 0:
            return parent or f"0x{index:04X}"

        # ignore placeholder children
        if child and "highest sub-index" in child.lower():
            child = None

        if parent and child:
            return f"{parent}.{child}"
        if child:
            return child
        if parent:
            return f"{parent}.sub{sub}"
        return f"0x{index:04X}:{sub}"

    def get_type(self, index: int, sub: int) -> Optional[str]:
        # prefer exact entry, then fallback to parent (index,0), else None
        t = self._index_to_type.get((index, sub))
        if t:
            return t
        return self._index_to_type.get((index, 0))

    def get_unit(self, index: int, sub: int) -> Optional[str]:
        u = self._index_to_unit.get((index, sub))
        if u:
            return u
        return self._index_to_unit.get((index, 0))

    def get_factor(self, index: int, sub: int) -> Optional[float]:
        f = self._index_to_factor.get((index, sub))
        if f is not None:
            return f
        return self._index_to_factor.get((index, 0))

    # -------------------- decoding --------------------
    def decode_value(self, index: int, sub: int, raw: Optional[bytes], apply_units: bool = True, decimals: int = 3):
        """
        Return a reasonably-decoded Python value or string for display:
         - DOMAIN / OCTET / VISIBLE_STRING => hex or text when sensible
         - REAL32 / REAL64 => float
         - INTEGER/UNSIGNED => ints
         - BOOLEAN => bool/int
         - fallback => hex string
        """
        if not raw:
            return ""

        dtype = self.get_type(index, sub)
        unit = self.get_unit(index, sub)
        factor = self.get_factor(index, sub)

        # Normalize dtype to uppercase string
        if dtype:
            dtype_up = str(dtype).upper()
        else:
            dtype_up = None

        # Domain & raw containers -> show hex
        if dtype_up in ("DOMAIN", "OCTET_STRING", "OCTETSTRING"):
            return bytes_to_hex_str(raw)

        if dtype_up in ("VISIBLE_STRING", "UNICODE_STRING", "STRING"):
            # try decode to text if printable
            try:
                s = raw.decode("utf-8", errors="replace")
                # if mostly printable, return text; otherwise hex
                printable_ratio = sum(1 for ch in s if 32 <= ord(ch) < 127) / max(1, len(s))
                if printable_ratio > 0.6:
                    return s
                return bytes_to_hex_str(raw)
            except Exception:
                return bytes_to_hex_str(raw)

        try:
            if dtype_up in ("BOOLEAN",):
                b = bool(raw[0])
                return int(b)  # show 0/1 for table clarity
            if dtype_up in ("INTEGER8", "INT8"):
                return int.from_bytes(raw[:1], "little", signed=True)
            if dtype_up in ("INTEGER16", "INT16"):
                return int.from_bytes(raw[:2], "little", signed=True)
            if dtype_up in ("INTEGER32", "INT32"):
                return int.from_bytes(raw[:4], "little", signed=True)
            if dtype_up in ("UNSIGNED8", "UINT8"):
                return int.from_bytes(raw[:1], "little", signed=False)
            if dtype_up in ("UNSIGNED16", "UINT16"):
                return int.from_bytes(raw[:2], "little", signed=False)
            if dtype_up in ("UNSIGNED32", "UINT32"):
                return int.from_bytes(raw[:4], "little", signed=False)
            if dtype_up in ("REAL32", "FLOAT"):
                if len(raw) >= 4:
                    val = struct.unpack("<f", raw[:4])[0]
                else:
                    val = int.from_bytes(raw, "little", signed=False)
                if apply_units and factor is not None:
                    val = val * factor
                return round(val, decimals)
            if dtype_up in ("REAL64", "DOUBLE"):
                if len(raw) >= 8:
                    val = struct.unpack("<d", raw[:8])[0]
                else:
                    val = int.from_bytes(raw, "little", signed=False)
                if apply_units and factor is not None:
                    val = val * factor
                return round(val, decimals)

            # fallback heuristics
            if len(raw) == 4:
                try:
                    f = struct.unpack("<f", raw)[0]
                    return round(f, decimals)
                except Exception:
                    pass
            if len(raw) == 8:
                try:
                    d = struct.unpack("<d", raw)[0]
                    return round(d, decimals)
                except Exception:
                    pass

            # try int
            try:
                return int.from_bytes(raw, "little", signed=False)
            except Exception:
                return bytes_to_hex_str(raw)
        except Exception:
            return bytes_to_hex_str(raw)

# ---------------- parse PDO mapping from EDS ----------------
def parse_pdo_sections_from_eds(eds_path: str):
    cfg = configparser.ConfigParser(strict=False)
    cfg.optionxform = str
    try:
        cfg.read(eds_path)
    except Exception:
        return [], []
    def get_section_value(section: str, keys: List[str]):
        if section not in cfg:
            return None
        for k in keys:
            if k in cfg[section]:
                return cfg[section].get(k)
        for alt in ("DefaultValue", "Default", "Value", "Defaultvalue"):
            if alt in cfg[section]:
                return cfg[section].get(alt)
        return None

    def parse_group(comm_base: str, map_base: str, limit: int):
        arr = []
        for n in range(limit):
            comm = f"{comm_base}{n}sub1"
            map_prefix = f"{map_base}{n}sub"
            try:
                v = get_section_value(comm, ["DefaultValue", "Default", "Value"])
                if v is None:
                    # try other naming variants
                    alt = f"{comm_base}{n}"
                    v = get_section_value(alt, ["DefaultValue", "Default", "Value"])
                    if v is None:
                        break
                cob_id = int(v, 0)
            except Exception:
                break
            mapping = []
            subidx = 1
            while True:
                map_key = f"{map_prefix}{subidx}"
                if map_key not in cfg:
                    break
                raw = get_section_value(map_key, ["DefaultValue", "Default", "Value"])
                if raw is None:
                    break
                try:
                    raw_int = int(raw, 0)
                except Exception:
                    break
                index = (raw_int >> 16) & 0xFFFF
                sub = (raw_int >> 8) & 0xFF
                size = raw_int & 0xFF
                mapping.append((index, sub, size))
                subidx += 1
            arr.append((cob_id, mapping))
        return arr

    rpdos = parse_group("140", "160", 512)
    tpdos = parse_group("180", "1A0", 512)
    return rpdos, tpdos

def _clean_int_with_comment(val: str) -> int:
    if val is None:
        raise ValueError("None passed to _clean_int_with_comment")
    core = str(val).split(";", 1)[0].strip()
    return int(core, 0)

def build_name_map(eds_path: str) -> dict:
    name_map = {}
    cfg = configparser.ConfigParser(strict=False)
    cfg.optionxform = str
    cfg.read(eds_path)
    parents = {}
    for sec in cfg.sections():
        m = re.match(r'^(?:0x)?([0-9A-Fa-f]+)$', sec)
        if m:
            idx = int(m.group(1), 16)
            pname = cfg[sec].get("ParameterName", "").strip()
            if pname:
                parents[idx] = pname
    for sec in cfg.sections():
        m = re.match(r'^(?:0x)?([0-9A-Fa-f]+)sub([0-9A-Fa-f]+)$', sec, re.I)
        if not m:
            continue
        idx = int(m.group(1), 16); sub = int(m.group(2), 16)
        pname = cfg[sec].get("ParameterName", "").strip()
        parent = parents.get(idx, f"0x{idx:04X}")
        if pname and "highest" not in pname.lower():
            name_map[(idx, sub)] = f"{parent}.{pname}"
        else:
            name_map[(idx, sub)] = parent
    for idx, parent in parents.items():
        name_map.setdefault((idx, 0), parent)
    return name_map

def parse_pdo_map(eds_path: str, name_map: dict):
    cfg = configparser.ConfigParser(strict=False)
    cfg.optionxform = str
    cfg.read(eds_path)
    pdo_map = {}
    cob_name_overrides = {}
    for sec in cfg.sections():
        su = sec.upper()
        if su.startswith("1A") and "SUB" not in su:
            try:
                entries = []
                subidx = 1
                while True:
                    map_sec = f"{sec}sub{subidx}"
                    if map_sec not in cfg:
                        break
                    raw = cfg[map_sec].get("DefaultValue", cfg[map_sec].get("Value", ""))
                    raw_int = _clean_int_with_comment(raw)
                    index = (raw_int >> 16) & 0xFFFF
                    sub = (raw_int >> 8) & 0xFF
                    size = raw_int & 0xFF
                    entries.append((index, sub, size))
                    subidx += 1
                comm_sec = sec.replace("1A", "18", 1)
                comm_sub1 = f"{comm_sec}sub1"
                if comm_sub1 in cfg:
                    v = cfg[comm_sub1].get("DefaultValue", cfg[comm_sub1].get("Value", ""))
                    cob_id = _clean_int_with_comment(v)
                    pdo_map[cob_id] = entries
                    names = []
                    for (idx, sub, _) in entries:
                        pname = name_map.get((idx, sub)) or name_map.get((idx, 0)) or f"0x{idx:04X}:{sub}"
                        names.append(pname)
                    cob_name_overrides[cob_id] = names
            except Exception:
                continue
    return pdo_map, cob_name_overrides

# ---------------- decode helper ----------------
def decode_using_od(odmap: Optional[ODVariableMapper], index: int, sub: int, raw: bytes,
                    apply_units: bool = True, decimals: int = 3) -> Tuple[str, Optional[int], Optional[int]]:
    if odmap is None:
        try:
            if len(raw) == 4:
                v = struct.unpack("<f", raw)[0]
                return (f"{v:.{decimals}f}", index, sub)
            if len(raw) == 8:
                v = struct.unpack("<d", raw)[0]
                return (f"{v:.{decimals}f}", index, sub)
            return (str(int.from_bytes(raw, "little")) if raw else "", index, sub)
        except Exception:
            return (bytes_to_hex_str(raw), index, sub)
    try:
        tname = odmap.get_type(index, sub)
        unit = odmap.get_unit(index, sub)
        factor = odmap.get_factor(index, sub)
        if tname:
            t = str(tname).upper()
            if "BOOLEAN" in t:
                b = bool(int.from_bytes(raw[:1], "little")) if raw else False
                s = str(int(b))
                if apply_units and unit:
                    s += f" {unit}"
                return (s, index, sub)
            if "UNSIGNED8" in t or "UINT8" in t:
                n = int.from_bytes(raw[:1], "little", signed=False) if raw else 0
                if apply_units and factor:
                    n = n * factor
                s = f"{n}" if isinstance(n, int) else f"{n:.{decimals}f}"
                if apply_units and unit:
                    s += f" {unit}"
                return (s, index, sub)
            if "UNSIGNED16" in t or "UINT16" in t:
                n = int.from_bytes(raw[:2], "little", signed=False) if raw else 0
                if apply_units and factor:
                    n = n * factor
                s = f"{n}"
                if apply_units and unit:
                    s += f" {unit}"
                return (s, index, sub)
            if "UNSIGNED32" in t or "UINT32" in t:
                n = int.from_bytes(raw[:4], "little", signed=False) if raw else 0
                if apply_units and factor:
                    n = n * factor
                s = f"{n}"
                if apply_units and unit:
                    s += f" {unit}"
                return (s, index, sub)
            if "INTEGER8" in t or "INT8" in t:
                n = int.from_bytes(raw[:1], "little", signed=True) if raw else 0
                return (str(n), index, sub)
            if "INTEGER16" in t or "INT16" in t:
                n = int.from_bytes(raw[:2], "little", signed=True) if raw else 0
                return (str(n), index, sub)
            if "INTEGER32" in t or "INT32" in t:
                n = int.from_bytes(raw[:4], "little", signed=True) if raw else 0
                return (str(n), index, sub)
            if "REAL32" in t or "FLOAT" in t:
                if len(raw) >= 4:
                    val = struct.unpack("<f", raw[:4])[0]
                else:
                    val = int.from_bytes(raw, "little", signed=False) if raw else 0
                if apply_units and factor:
                    val *= factor
                s = f"{val:.{decimals}f}"
                if apply_units and unit:
                    s += f" {unit}"
                return (s, index, sub)
            if "REAL64" in t or "DOUBLE" in t:
                if len(raw) >= 8:
                    val = struct.unpack("<d", raw[:8])[0]
                else:
                    val = int.from_bytes(raw, "little", signed=False) if raw else 0
                if apply_units and factor:
                    val *= factor
                s = f"{val:.{decimals}f}"
                if apply_units and unit:
                    s += f" {unit}"
                return (s, index, sub)
            if "VISIBLE_STRING" in t or "STRING" in t:
                try:
                    return (raw.decode("utf-8", errors="replace"), index, sub)
                except Exception:
                    return (bytes_to_hex_str(raw), index, sub)
            if "DOMAIN" in t or "OCTET" in t:
                try:
                    s = raw.decode("utf-8")
                    if all(32 <= ord(ch) < 127 for ch in s):
                        return (s, index, sub)
                except Exception:
                    pass
                return (bytes_to_hex_str(raw), index, sub)
        # fallback heuristics
        if len(raw) == 4:
            try:
                f = struct.unpack("<f", raw)[0]
                return (f"{f:.{decimals}f}", index, sub)
            except Exception:
                pass
        if len(raw) == 8:
            try:
                d = struct.unpack("<d", raw)[0]
                return (f"{d:.{decimals}f}", index, sub)
            except Exception:
                pass
        try:
            n = int.from_bytes(raw, "little")
            return (str(n), index, sub)
        except Exception:
            return (bytes_to_hex_str(raw), index, sub)
    except Exception:
        try:
            return (bytes_to_hex_str(raw), index, sub)
        except Exception:
            return ("<decode error>", index, sub)

# ---------------- CAN Worker (QThread) ----------------
class CANWorker(QtCore.QThread):
    message_received = QtCore.pyqtSignal(dict)  # emits {"type":"frame", "msg":{...}} or {"type":"error"...}
    def __init__(self, channel: str = "can0", bustype: str = "socketcan"):
        super().__init__()
        self.channel = channel
        self.bustype = bustype
        self._stop = False
        self.bus: Optional[can.Bus] = None

    @staticmethod
    def make_bus(channel, bustype="socketcan"):
        import inspect, can
        if "interface" in inspect.signature(can.interface.Bus).parameters:
            return can.interface.Bus(channel=channel, interface=bustype)
        else:
            return can.interface.Bus(channel=channel, bustype=bustype)

    def run(self):
        try:
            self.bus = self.make_bus(self.channel, self.bustype)
        except Exception as e:
            self.message_received.emit({"type":"error", "text": str(e)})
            return
        while not self._stop:
            msg = None
            try:
                msg = self.bus.recv(timeout=1.0)
            except Exception:
                pass
            if msg is None:
                continue
            payload = {"arbitration_id": msg.arbitration_id, "data": bytes(msg.data), "timestamp": getattr(msg, "timestamp", time.time())}
            self.message_received.emit({"type":"frame", "msg": payload})
        try:
            if self.bus is not None:
                self.bus.shutdown()
        except Exception:
            pass

    def stop(self):
        self._stop = True
        self.wait(2000)

# ---------------- GUI Widgets ----------------
class RateSparkline(QtWidgets.QWidget):
    def __init__(self, title: str = "Frame Rate (last 5s)", window_sec: float = LOAD_WINDOW, parent=None):
        super().__init__(parent)
        self.title = title
        self.window_sec = window_sec
        self.timestamps = deque()
        self.setMinimumHeight(140)

    def push(self, ts):
        self.timestamps.append(ts)
        cutoff = ts - self.window_sec
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()
        self.update()

    def paintEvent(self, ev):
        painter = QtGui.QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QtGui.QColor(255,255,255))
        painter.setPen(QtGui.QPen(QtGui.QColor(33,33,33)))
        painter.drawText(rect.left()+6, rect.top()+16, self.title)
        plot = rect.adjusted(6, 22, -6, -6)
        painter.setPen(QtGui.QPen(QtGui.QColor(200,200,200)))
        painter.drawRect(plot.adjusted(0,0,-1,-1))
        if not self.timestamps:
            painter.end(); return
        width = max(4, plot.width()); height = plot.height()
        now = self.timestamps[-1]
        bins = width
        arr = [0]*bins
        for ts in self.timestamps:
            rel = (ts - (now - self.window_sec)) / self.window_sec
            idx = int(rel*(bins-1))
            if 0 <= idx < bins:
                arr[idx] += 1
        maxv = max(arr) if arr else 1
        path = QtGui.QPainterPath()
        for i, v in enumerate(arr):
            x = plot.left() + i
            y = plot.bottom() - (v/maxv)*(height-4) - 2
            if i == 0:
                path.moveTo(x,y)
            else:
                path.lineTo(x,y)
        painter.setPen(QtGui.QPen(QtGui.QColor(30,120,200), 1.5))
        painter.drawPath(path)
        painter.end()

class COBHistogram(QtWidgets.QWidget):
    def __init__(self, title: str = "COB-ID Histogram (last 5s)", window_sec: float = LOAD_WINDOW, parent=None):
        super().__init__(parent)
        self.title = title
        self.window_sec = window_sec
        self.timestamps_by_cob: Dict[int, deque] = defaultdict(deque)
        self.setMinimumHeight(220)

    def push(self, ts: float, cob: Optional[int]):
        if cob is None:
            return
        dq = self.timestamps_by_cob[cob]
        dq.append(ts)
        cutoff = ts - self.window_sec
        for k in list(self.timestamps_by_cob.keys()):
            d = self.timestamps_by_cob[k]
            while d and d[0] < cutoff:
                d.popleft()
            if not d:
                try:
                    del self.timestamps_by_cob[k]
                except KeyError:
                    pass
        self.update()

    def paintEvent(self, ev):
        painter = QtGui.QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QtGui.QColor(255,255,255))
        painter.setPen(QtGui.QPen(QtGui.QColor(33,33,33)))
        painter.drawText(rect.left()+6, rect.top()+16, self.title)
        plot = rect.adjusted(12, 34, -12, -12)
        painter.setPen(QtGui.QPen(QtGui.QColor(200,200,200)))
        painter.drawRect(plot.adjusted(0,0,-1,-1))
        cob_list = sorted(self.timestamps_by_cob.keys())
        if not cob_list:
            painter.end(); return
        counts = [len(self.timestamps_by_cob[c]) for c in cob_list]
        maxv = max(counts) if counts else 1
        gap = 8
        total_gap = gap*(len(cob_list)+1)
        bar_width = max(12, (plot.width() - total_gap)//len(cob_list))
        x = plot.left() + gap
        for i, cob in enumerate(cob_list):
            cnt = counts[i]
            h = int((cnt/maxv) * (plot.height()-40)) if maxv>0 else 0
            bar_rect = QtCore.QRect(x, plot.bottom()-h-24, bar_width, h)
            color = color_for_cob(cob)
            painter.fillRect(bar_rect, color)
            painter.setPen(QtGui.QPen(QtGui.QColor(80,80,80)))
            painter.drawRect(bar_rect)
            label = f"0x{cob:03X}"
            painter.drawText(x, plot.bottom()-18, bar_width, 16, QtCore.Qt.AlignCenter, label)
            x += bar_width + gap
        painter.end()

    def export_counts_to_csv(self, path: str):
        try:
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["COB-ID", "Count", "Color"])
                for cob in sorted(self.timestamps_by_cob.keys()):
                    color = color_for_cob(cob)
                    color_hex = f"#{color.red():02X}{color.green():02X}{color.blue():02X}"
                    w.writerow([f"0x{cob:03X}", len(self.timestamps_by_cob[cob]), color_hex])
            return True
        except Exception:
            return False

# ---------------- MainWindow ----------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, eds_path: Optional[str], channel: str):
        super().__init__()
        self.setWindowTitle("CANopen Sniffer")
        # Always start in full screen maximized mode
        self.showMaximized()
        self.settings = QtCore.QSettings(APP_ORG, APP_NAME)

        # CANopen local/remote nodes
        self.local_node = None
        self.remote_node = None
        if eds_path:
            try:
                self.local_node = canopen.LocalNode(0x01, eds_path)
                self.remote_node = canopen.RemoteNode(0x02, eds_path)
            except Exception:
                self.local_node = None
                self.remote_node = None

        # OD mapper
        self.od_mapper = ODVariableMapper(self.local_node, eds_path) if (self.local_node or eds_path) else None

        # parse pdo mapping from EDS
        self.pdo_tx_map: Dict[int, List[Tuple[int,int,int]]] = {}
        self.pdo_rx_map: Dict[int, List[Tuple[int,int,int]]] = {}
        if eds_path:
            try:
                rpdos, tpdos = parse_pdo_sections_from_eds(eds_path)
                for (cob, mapping) in tpdos:
                    self.pdo_tx_map[cob] = mapping
                for (cob, mapping) in rpdos:
                    self.pdo_rx_map[cob] = mapping
            except Exception:
                pass

        self.name_map = {}             # (index, sub) -> ParameterName string
        self.pdo_map = {}              # cob_id -> [(index, sub, size_bits), ...]
        self.cob_name_overrides = {}   # cob_id -> [ParameterName strings in mapping order]
        if eds_path:
            try:
                try:
                    nm = build_name_map(eds_path)
                    pm, pm_names = parse_pdo_map(eds_path, nm)
                    if nm:
                        self.name_map.update(nm)
                    if pm:
                        self.pdo_map.update(pm)
                        for cob_id, mapping in pm.items():
                            if cob_id not in self.pdo_tx_map:
                                self.pdo_tx_map[cob_id] = mapping

                        for cob_id, mapping in pm.items():
                            self.pdo_tx_map[cob_id] = mapping
                    if pm_names:
                        self.cob_name_overrides.update(pm_names)

                except Exception:
                    pass
            except Exception:
                pass

        logging.info("EDS loaded: %s", eds_path)
        logging.info("name_map entries: %d", len(self.name_map))
        logging.info("pdo_map entries: %d", len(self.pdo_map))
        logging.info("cob_name_overrides keys: %s", list(self.cob_name_overrides.keys()))


        # central layout
        central = QtWidgets.QWidget(); self.setCentralWidget(central)
        vmain = QtWidgets.QVBoxLayout(central)

        # top toolbar controls
        toolbar = QtWidgets.QHBoxLayout()
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.clear_btn = QtWidgets.QPushButton("Clear")
        self.filter_edit = QtWidgets.QLineEdit(); self.filter_edit.setPlaceholderText("Filter (comma separated COBs or substring)")
        self.clear_filter_btn = QtWidgets.QPushButton("Clear Filter")
        self.mode_combo = QtWidgets.QComboBox(); self.mode_combo.addItems(["Fixed", "Sequential"])
        self.mode_combo.setMinimumContentsLength(12)
        self.mode_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        self.decimals_spin = QtWidgets.QSpinBox(); self.decimals_spin.setRange(0,8); self.decimals_spin.setValue(3)

        toolbar.addWidget(self.pause_btn); toolbar.addWidget(self.clear_btn)
        toolbar.addWidget(QtWidgets.QLabel("Filters:")); toolbar.addWidget(self.filter_edit); toolbar.addWidget(self.clear_filter_btn)
        toolbar.addWidget(QtWidgets.QLabel("Mode:")); toolbar.addWidget(self.mode_combo)
        toolbar.addStretch()
        toolbar.addWidget(QtWidgets.QLabel("Decimal Points:")); toolbar.addWidget(self.decimals_spin)
        vmain.addLayout(toolbar)

        # ----- CAN Traces Table -----
        can_layout = QtWidgets.QVBoxLayout()
        can_label = QtWidgets.QLabel("CAN Traces")
        can_label.setStyleSheet("font-weight: bold; font-size: 14px; padding: 4px;")
        can_layout.addWidget(can_label)

        self.table = QtWidgets.QTableWidget(0, 11)
        headers = ["Time", "Node ID", "COB-ID", "Type", "Name", "Index", "Subindex", "Data Type", "Raw Data", "Decoded Data", "Count"]
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self.on_table_double_click)
        self.table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        can_layout.addWidget(self.table)
        vmain.addLayout(can_layout)

        # ----- SDO Data Table -----
        sdo_layout = QtWidgets.QVBoxLayout()
        sdo_label = QtWidgets.QLabel("SDO Data")
        sdo_label.setStyleSheet("font-weight: bold; font-size: 14px; padding: 4px;")
        sdo_layout.addWidget(sdo_label)

        self.sdo_table = QtWidgets.QTableWidget(0, 11)
        self.sdo_table.setHorizontalHeaderLabels(headers)
        self.sdo_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.sdo_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.sdo_table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.sdo_table.customContextMenuRequested.connect(self.context_menu_sdo)
        sdo_layout.addWidget(self.sdo_table)
        vmain.addLayout(sdo_layout)

        # graphs area
        graph_widget = QtWidgets.QWidget()
        gh = QtWidgets.QHBoxLayout(graph_widget)
        self.spark = RateSparkline("Frame Rate (last 5s)", window_sec=LOAD_WINDOW)
        self.hist = COBHistogram("COB-ID Histogram (last 5s)", window_sec=LOAD_WINDOW)
        gh.addWidget(self.spark, 1); gh.addWidget(self.hist, 1)
        vmain.addWidget(graph_widget)

        # status bar: follow indicator + load stats
        self.status = QtWidgets.QStatusBar(); self.setStatusBar(self.status)
        self.follow_label = QtWidgets.QLabel("")  # shows follow mode
        self.load_label = QtWidgets.QLabel("Load: 0.00/s")
        self.status.addPermanentWidget(self.follow_label)
        self.status.addPermanentWidget(self.load_label)

        # legend dock
        self.legend_dock = QtWidgets.QDockWidget("COB Legend", self)
        self.legend_list = QtWidgets.QListWidget()
        self.legend_dock.setWidget(self.legend_list)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.legend_dock)

        # remote-control dock (SDO/PDO send)
        self.ctrl_dock = QtWidgets.QDockWidget("Remote SDO/PDO Control", self)
        self.ctrl_widget = QtWidgets.QWidget()
        ctrl_layout = QtWidgets.QVBoxLayout(self.ctrl_widget)

        # --- SDO send ---
        sdo_box = QtWidgets.QGroupBox("Send SDO (expedited)")
        sdo_layout = QtWidgets.QFormLayout()
        self.sdo_send_node = QtWidgets.QLineEdit("0x02")  # Node ID input
        self.sdo_index_edit = QtWidgets.QLineEdit("0x6000")
        self.sdo_sub_edit = QtWidgets.QLineEdit("0x00")
        self.sdo_value_edit = QtWidgets.QLineEdit("1")
        self.sdo_size_combo = QtWidgets.QComboBox()
        self.sdo_size_combo.addItems(["1", "2", "4", "8"])
        self.sdo_send_btn = QtWidgets.QPushButton("Send SDO")
        sdo_layout.addRow("Node ID:", self.sdo_send_node)
        sdo_layout.addRow("Index (hex):", self.sdo_index_edit)
        sdo_layout.addRow("Subindex (hex):", self.sdo_sub_edit)
        sdo_layout.addRow("Value (int/hex):", self.sdo_value_edit)
        sdo_layout.addRow("Size (bytes):", self.sdo_size_combo)
        sdo_layout.addRow("", self.sdo_send_btn)
        sdo_box.setLayout(sdo_layout)
        ctrl_layout.addWidget(sdo_box)

        # --- SDO receive ---
        recv_box = QtWidgets.QGroupBox("Receive SDO (upload request)")
        recv_layout = QtWidgets.QFormLayout()
        self.sdo_recv_node = QtWidgets.QLineEdit("0x02")
        self.sdo_recv_index = QtWidgets.QLineEdit("0x6000")
        self.sdo_recv_sub = QtWidgets.QLineEdit("0x00")
        self.sdo_recv_btn = QtWidgets.QPushButton("Receive SDO")
        recv_layout.addRow("Node ID:", self.sdo_recv_node)
        recv_layout.addRow("Index (hex):", self.sdo_recv_index)
        recv_layout.addRow("Subindex (hex):", self.sdo_recv_sub)
        recv_layout.addRow("", self.sdo_recv_btn)
        recv_box.setLayout(recv_layout)
        ctrl_layout.addWidget(recv_box)

        # --- PDO send (with repeat) ---
        pdo_box = QtWidgets.QGroupBox("Send raw PDO")
        pdo_layout = QtWidgets.QFormLayout()
        self.pdo_cob_edit = QtWidgets.QLineEdit("0x181")
        self.pdo_data_edit = QtWidgets.QLineEdit("00 00 00 00 00 00 00 00")

        # Interval + repeat
        self.pdo_interval_spin = QtWidgets.QSpinBox()
        self.pdo_interval_spin.setRange(1, 100000)
        self.pdo_interval_spin.setValue(1000)  # default 1000 ms
        self.pdo_repeat_chk = QtWidgets.QCheckBox("Repeat")
        self.pdo_send_btn = QtWidgets.QPushButton("Send PDO")
        self.pdo_stop_btn = QtWidgets.QPushButton("Stop")
        self.pdo_stop_btn.setEnabled(False)

        pdo_layout.addRow("COB-ID (hex):", self.pdo_cob_edit)
        pdo_layout.addRow("Data (hex bytes):", self.pdo_data_edit)
        pdo_layout.addRow("Interval (ms):", self.pdo_interval_spin)
        pdo_layout.addRow("", self.pdo_repeat_chk)
        pdo_layout.addRow("", self.pdo_send_btn)
        pdo_layout.addRow("", self.pdo_stop_btn)

        pdo_box.setLayout(pdo_layout)
        ctrl_layout.addWidget(pdo_box)

        ctrl_layout.addStretch()
        self.ctrl_widget.setLayout(ctrl_layout)
        self.ctrl_dock.setWidget(self.ctrl_widget)
        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, self.ctrl_dock)

        # PDO repeat timer
        self.pdo_timer = QtCore.QTimer(self)
        self.pdo_timer.timeout.connect(self.on_send_pdo)

        # menu: Export & View
        menubar = self.menuBar()
        export_menu = menubar.addMenu("Export")
        act_export_csv = QtWidgets.QAction("Export Data to CSV", self)
        act_export_hist = QtWidgets.QAction("Export Histogram CSV", self)
        act_export_json = QtWidgets.QAction("Export Data to JSON", self)
        act_export_pcap = QtWidgets.QAction("Export Data to PCAP", self)
        export_menu.addAction(act_export_csv)
        export_menu.addAction(act_export_json)
        export_menu.addAction(act_export_pcap)
        export_menu.addSeparator()
        export_menu.addAction(act_export_hist)

        view_menu = menubar.addMenu("View")
        options_menu = view_menu.addMenu("Options")
        self.show_special_action = QtWidgets.QAction("Show special frames", self, checkable=True)
        self.show_special_action.setChecked(True)  # default enabled
        self.apply_units_action = QtWidgets.QAction("Apply unit scaling", self, checkable=True)
        self.apply_units_action.setChecked(True)   # default enabled
        self.show_dtype_action = QtWidgets.QAction("Show Data Type column", self, checkable=True)
        self.show_dtype_action.setChecked(True)    # default enabled
        self.sdo_autopop_action = QtWidgets.QAction("Auto-populate SDO Table", self, checkable=True)
        self.sdo_autopop_action.setChecked(False)  # default disabled
        self.sdo_autopop_action.toggled.connect(self.toggle_sdo_autopop)
        options_menu.addAction(self.show_special_action)
        options_menu.addAction(self.apply_units_action)
        options_menu.addAction(self.show_dtype_action)
        options_menu.addAction(self.sdo_autopop_action)
        follow_menu = view_menu.addMenu("Follow")
        self.clear_follow_action = QtWidgets.QAction("Clear Follow", self)
        follow_menu.addAction(self.clear_follow_action)

        # connect signals
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.clear_btn.clicked.connect(self.clear_table)
        self.clear_filter_btn.clicked.connect(self.clear_filter)
        self.filter_edit.textChanged.connect(self.on_filter_changed)
        self.mode_combo.currentIndexChanged.connect(lambda *_: None)
        self.decimals_spin.valueChanged.connect(lambda *_: self.rebuild_table())
        self.sdo_send_btn.clicked.connect(self.on_send_sdo)
        self.sdo_recv_btn.clicked.connect(self.on_recv_sdo)
        self.pdo_send_btn.clicked.connect(self.on_send_pdo_clicked)
        self.pdo_stop_btn.clicked.connect(self.on_stop_pdo)
        act_export_csv.triggered.connect(self.export_csv_dialog)
        act_export_hist.triggered.connect(self.export_hist_csv)
        act_export_json.triggered.connect(self.export_json)
        act_export_pcap.triggered.connect(self.export_pcap)
        self.show_special_action.triggered.connect(self.apply_special_visibility_filter)
        self.show_dtype_action.triggered.connect(self.toggle_dtype_column)
        self.apply_units_action.triggered.connect(lambda _: self.rebuild_table())
        self.clear_follow_action.triggered.connect(self.clear_follow)

        # worker thread
        self.worker = CANWorker(channel=channel)
        self.worker.message_received.connect(self.on_can_message)
        self.worker.start()

        # data buffers and states
        self.buffer_frames: List[dict] = []
        self.pause = False
        self.timestamps = deque()
        self.peak_rate = 0.0
        self.last_values_by_cob: Dict[int, str] = {}
        self.follow_mode: Optional[dict] = None
        self.cob_seen: set = set()

        # autosize columns and set decoded width fixed
        header = self.table.horizontalHeader()
        for c in range(0,9):
            header.setSectionResizeMode(c, QtWidgets.QHeaderView.ResizeToContents)
        fm = self.table.fontMetrics()
        approx_char_width = fm.horizontalAdvance("W")
        decoded_px = approx_char_width * DECODED_CHAR_LIMIT
        self.table.setColumnWidth(9, decoded_px)
        header.setSectionResizeMode(9, QtWidgets.QHeaderView.Fixed)
        header.setStretchLastSection(False)
        # SDO table same configuration
        sh = self.sdo_table.horizontalHeader()
        for c in range(0,9):
            sh.setSectionResizeMode(c, QtWidgets.QHeaderView.ResizeToContents)
        self.sdo_table.setColumnWidth(9, decoded_px)
        sh.setSectionResizeMode(9, QtWidgets.QHeaderView.Fixed)
        sh.setStretchLastSection(False)
        # Map of (index, sub) -> row for Fixed-mode updates in SDO table
        self.sdo_row_map: Dict[Tuple[int,int], int] = {}

        # Pre-populate SDO table only if option is enabled
        if self.sdo_autopop_action.isChecked():
            self.populate_sdo_table()

    def toggle_sdo_autopop(self, enabled: bool):
        if enabled:
            self.populate_sdo_table()
        else:
            # clear table if disabling auto-populate
            self.sdo_table.setRowCount(0)
            self.sdo_row_map.clear()

    def populate_sdo_table(self):
        if not self.od_mapper:
            return
        try:
            keys = sorted(self.od_mapper._values.keys())
            for (idx, sub) in keys:
                if (idx, sub) in self.sdo_row_map:
                    continue
                r = self.sdo_table.rowCount()
                self.sdo_table.insertRow(r)
                ftype = "SDO"
                def mkitem(text, bold=False, center=False):
                    it = QtWidgets.QTableWidgetItem(str(text) if text is not None else "")
                    if bold:
                        f = it.font(); f.setBold(True); it.setFont(f)
                    if center:
                        it.setTextAlignment(QtCore.Qt.AlignCenter)
                    it.setBackground(color_for_type(ftype))
                    return it

                name = self.od_mapper.get_full_name(idx, sub)
                dtype = self.od_mapper.get_type(idx, sub) or ""
                self.sdo_table.setItem(r, 0, mkitem(""))               # Time (empty initially)
                self.sdo_table.setItem(r, 1, mkitem(""))               # Node
                self.sdo_table.setItem(r, 2, mkitem(""))               # COB-ID
                self.sdo_table.setItem(r, 3, mkitem(ftype, center=True))
                self.sdo_table.setItem(r, 4, mkitem(name))
                self.sdo_table.setItem(r, 5, mkitem(f"0x{idx:04X}", center=True))
                self.sdo_table.setItem(r, 6, mkitem(f"0x{sub:02X}", center=True))
                self.sdo_table.setItem(r, 7, mkitem(dtype))
                self.sdo_table.setItem(r, 8, mkitem(""))               # Raw
                self.sdo_table.setItem(r, 9, mkitem("", bold=True))    # Decoded (empty)
                self.sdo_row_map[(idx, sub)] = r
        except Exception:
            pass

        # restore settings
        self.load_settings()
        QtCore.QTimer.singleShot(0, self.showMaximized)
        # periodic status update
        self.status_timer = QtCore.QTimer(self)
        self.status_timer.timeout.connect(self.update_load_label)
        self.status_timer.start(1000)

    # -------------- settings ----------------
    def load_settings(self):
        s = self.settings
        show_spec = s.value("options/show_special", True, type=bool)
        apply_units = s.value("options/apply_units", True, type=bool)
        show_dtype = s.value("options/show_dtype", True, type=bool)
        decimals = s.value("options/decimals", 3, type=int)
        mode = s.value("options/mode", "Fixed")
        idx = 0 if mode == "Fixed" else 1
        self.show_special_action.setChecked(show_spec)
        self.apply_units_action.setChecked(apply_units)
        self.show_dtype_action.setChecked(show_dtype)
        self.decimals_spin.setValue(decimals)
        self.mode_combo.setCurrentIndex(idx)
        self.table.horizontalHeader().setSectionHidden(7, not show_dtype)
        self.sdo_table.horizontalHeader().setSectionHidden(7, not show_dtype)

    def save_settings(self):
        s = self.settings
        s.setValue("options/show_special", self.show_special_action.isChecked())
        s.setValue("options/apply_units", self.apply_units_action.isChecked())
        s.setValue("options/show_dtype", self.show_dtype_action.isChecked())
        s.setValue("options/decimals", self.decimals_spin.value())
        s.setValue("options/mode", "Fixed" if self.mode_combo.currentIndex()==0 else "Sequential")
        s.sync()

    # -------------- utilities ----------------
    def update_load_stats(self, timestamp: float, cob: Optional[int]):
        now = timestamp
        self.timestamps.append(now)
        cutoff = now - LOAD_WINDOW
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()
        current_rate = len(self.timestamps)/max(0.0001, LOAD_WINDOW)
        if current_rate > self.peak_rate:
            self.peak_rate = current_rate
        self.spark.push(now)
        if cob is not None:
            self.hist.push(now, cob)

    def update_legend(self, cob: int, name: Optional[str] = None):
        if cob in self.cob_seen:
            return
        self.cob_seen.add(cob)
        display_name = name or "<Unknown>"
        item = QtWidgets.QListWidgetItem(f"0x{cob:03X} — {display_name}")
        color = color_for_cob(cob)
        pix = QtGui.QPixmap(16,16)
        pix.fill(color)
        item.setIcon(QtGui.QIcon(pix))
        self.legend_list.addItem(item)

    # -------------- CAN message processing ----------------
    def on_can_message(self, payload: dict):
        if payload.get("type") == "error":
            QtWidgets.QMessageBox.critical(self, "CAN error", payload.get("text",""))
            return
        if self.pause:
            return
        msg = payload["msg"]
        cob = msg["arbitration_id"]
        data: bytes = msg["data"]
        ts = msg["timestamp"]
        ts_h = time.strftime("%H:%M:%S", time.localtime(ts))
        ms = int((ts * 1000) % 1000)
        time_str = f"{ts_h}.{ms:03d}"
        raw_hex = bytes_to_hex_str(data)

        # update stats & legend
        self.update_load_stats(ts, cob)
        name_for_legend = None
        if self.od_mapper:
            mapping = self.pdo_tx_map.get(cob) or self.pdo_tx_map.get(cob & ~0x7F)
            if mapping and mapping:
                idx0, sub0, _ = mapping[0]
                name_for_legend = self.od_mapper.get_full_name(idx0, sub0)
        self.update_legend(cob, name_for_legend)

        # build frame dict (default)
        frame = {
            "time": time_str,
            "node": (cob & 0x7F),
            "cob": cob,
            "type": "RAW",
            "name": "<Unknown>",
            "index_list": [],
            "sub_list": [],
            "dtype": "",
            "raw": raw_hex,
            "decoded": raw_hex,
        }

        apply_units = self.apply_units_action.isChecked()
        decimals = self.decimals_spin.value()

        # classify special frames
        if 0x080 <= cob <= 0x0FF:
            frame["type"] = "Emergency"; frame["name"] = "Emergency"
            if len(data) >= 2:
                err_code = int.from_bytes(data[0:2], "little")
                frame["decoded"] = f"0x{err_code:04X}"
        elif 0x100 <= cob <= 0x17F:
            frame["type"] = "Time"; frame["name"] = "Time"
            if len(data) >= 4:
                try:
                    frame["decoded"] = str(int.from_bytes(data[0:4], "little"))
                except Exception:
                    frame["decoded"] = raw_hex
        elif 0x700 <= cob <= 0x77F:
            frame["type"] = "Heartbeat"; frame["name"] = "Heartbeat"
            if len(data) >= 1:
                state_map = {0x00:"Boot-up",0x04:"Stopped",0x05:"Operational",0x7F:"Pre-operational"}
                frame["decoded"] = f"{state_map.get(data[0],'Unknown')} (0x{data[0]:02X})"
            else:
                frame["decoded"] = "no state byte"

        # SDO (responses) or special-case 0x601 treated as SDO-like per request
        elif 0x580 <= cob <= 0x5FF or cob == 0x601:
            # Handle SDO frames separately: do not add them to CAN trace table, only to SDO response table
            frame["type"] = "SDO"
            if len(data) >= 4:
                index = data[1] | (data[2] << 8)
                sub = data[3]
                payload_bytes = data[4:]
                dtype = self.od_mapper.get_type(index, sub) if self.od_mapper else None
                dec, _, _ = decode_using_od(self.od_mapper, index, sub, payload_bytes, apply_units, decimals)
                # update mapper
                try:
                    val = None
                    if payload_bytes:
                        if len(payload_bytes) == 1:
                            val = int.from_bytes(payload_bytes, "little", signed=False)
                        elif len(payload_bytes) == 2:
                            val = int.from_bytes(payload_bytes, "little", signed=False)
                        elif len(payload_bytes) == 4:
                            if (self.od_mapper.get_type(index, sub) or "").upper().find("REAL") >= 0:
                                val = struct.unpack("<f", payload_bytes[:4])[0]
                            else:
                                val = int.from_bytes(payload_bytes[:4], "little", signed=False)
                        elif len(payload_bytes) == 8:
                            val = struct.unpack("<d", payload_bytes[:8])[0]
                    if self.od_mapper:
                        self.od_mapper.update_value(index, sub, val, payload_bytes)
                except Exception:
                    pass

                # append only to SDO response table (not CAN traces)
                self.append_sdo_response(time_str, cob, "SDO",
                                         self.od_mapper.get_full_name(index, sub) if self.od_mapper else f"0x{index:04X}:{sub}",
                                         [f"0x{index:04X}"], [f"0x{sub:02X}"],
                                         dtype or "", payload_bytes, dec)

        # PDO handling
        elif 0x180 <= cob <= 0x4FF:
            frame["type"] = "PDO"
            mapping = None
            if cob in self.pdo_tx_map:
                mapping = self.pdo_tx_map[cob]
            else:
                base = cob & ~0x7F
                if base in self.pdo_tx_map:
                    mapping = self.pdo_tx_map[base]

            if mapping:
                offset = 0; parts = []; names = []; idxs = []; subs = []; dtypes = []
                for (index, sub, bits) in mapping:
                    size = (bits + 7) // 8
                    field = data[offset:offset+size]; offset += size
                    if index == 0x0000:
                        continue
                    fullname = None
                    try:
                        if getattr(self, "name_map", None):
                            fullname = self.name_map.get((index, sub))
                    except Exception:
                        fullname = None
                    try:
                        if not fullname and getattr(self, "cob_name_overrides", None):
                            overrides = self.cob_name_overrides.get(cob) or self.cob_name_overrides.get(cob & ~0x7F)
                            if overrides:
                                try:
                                    pos = len(names)
                                except Exception:
                                    pos = 0
                                if 0 <= pos < len(overrides):
                                    fullname = overrides[pos]
                    except Exception:
                        pass
                    if not fullname and getattr(self, "od_mapper", None):
                        try:
                            fullname = self.od_mapper.get_full_name(index, sub)
                        except Exception:
                            fullname = None
                    if not fullname:
                        fullname = f"0x{index:04X}:{sub}"

                    logging.debug("COB=0x%X idx=0x%04X sub=0x%02X -> name='%s'", cob, index, sub, fullname)

                    dec, _, _ = decode_using_od(self.od_mapper, index, sub, field, apply_units, decimals)
                    names.append(fullname)
                    parts.append(dec)
                    idxs.append(f"0x{index:04X}"); subs.append(f"0x{sub:02X}")
                    dtype = self.od_mapper.get_type(index, sub) if self.od_mapper else None
                    dtypes.append(dtype or "")
                    if self.od_mapper:
                        try:
                            if len(field) == 4 and (self.od_mapper.get_type(index, sub) or "").upper().find("REAL") >= 0:
                                val = struct.unpack("<f", field[:4])[0]
                            elif len(field) == 4:
                                val = int.from_bytes(field[:4], "little", signed=False)
                            elif len(field) == 2:
                                val = int.from_bytes(field[:2], "little", signed=False)
                            elif len(field) == 1:
                                val = int.from_bytes(field[:1], "little", signed=False)
                            else:
                                val = field.hex()
                            self.od_mapper.update_value(index, sub, val, field)
                        except Exception:
                            pass

                if len(names) > 1:
                    # create one frame per variable
                    for fullname, dec, idx, sub, dtype in zip(names, parts, idxs, subs, dtypes):
                        single_frame = frame.copy()
                        single_frame["name"] = fullname
                        single_frame["index"] = idx
                        single_frame["sub"] = sub
                        single_frame["decoded"] = dec
                        single_frame["index_list"] = [idx]
                        single_frame["sub_list"] = [sub]
                        single_frame["dtype"] = dtype or ""
                        self.buffer_frames.append(single_frame)
                        if len(self.buffer_frames) > BUFFER_MAX:
                            self.buffer_frames.pop(0)
                        if self.frame_matches_filter(single_frame) and self.frame_matches_follow(single_frame):
                            self.insert_or_update_row(single_frame)
                else:
                    # single variable PDO (or mapping gave only one entry)
                    frame["name"] = names[0] if names else "<Unknown>"
                    frame["index"] = idxs[0] if idxs else ""
                    frame["sub"]   = subs[0] if subs else ""
                    frame["decoded"] = parts[0] if parts else raw_hex
                    frame["index_list"] = idxs
                    frame["sub_list"] = subs
                    frame["dtype"] = dtypes[0] if dtypes else ""
                    self.buffer_frames.append(frame)
                    if len(self.buffer_frames) > BUFFER_MAX:
                        self.buffer_frames.pop(0)
                    if self.frame_matches_filter(frame) and self.frame_matches_follow(frame):
                        self.insert_or_update_row(frame)
            else:
                if len(data) == 4:
                    try:
                        frame["decoded"] = f"{struct.unpack('<f', data)[0]:.{decimals}f}"
                    except Exception:
                        frame["decoded"] = raw_hex

        # push to buffer and display for non-PDO frames
        if frame["type"] != "PDO":
            self.buffer_frames.append(frame)
            if len(self.buffer_frames) > BUFFER_MAX:
                self.buffer_frames.pop(0)
            if self.frame_matches_filter(frame) and self.frame_matches_follow(frame):
                self.insert_or_update_row(frame)

    def append_sdo_response(self, time_str: str, cob: int, ftype: str, name: str,
                             idx_list: List[str], sub_list: List[str], dtype: str,
                             raw_bytes: bytes, decoded: str):
        """
        Add or update a row in the SDO response table. In Fixed mode (when possible)
        SDO rows represent unique (index,subindex) and are updated in-place.
        In Sequential mode, rows are appended as before.
        """
        # Determine index/subindex integers if available
        idx_int = None; sub_int = None
        try:
            if idx_list and len(idx_list) > 0:
                idx_int = int(idx_list[0], 0)
            if sub_list and len(sub_list) > 0:
                sub_int = int(sub_list[0], 0)
        except Exception:
            idx_int = sub_int = None

        mode = self.mode_combo.currentText() if hasattr(self, "mode_combo") else "Sequential"
        # Choose row: if Fixed and we can key by (idx,sub) then reuse that row
        r = None
        if mode == "Fixed" and (idx_int is not None) and (sub_int is not None):
            key = (idx_int, sub_int)
            # If row already exists for this OD key, update it
            if key in getattr(self, "sdo_row_map", {}):
                r = self.sdo_row_map[key]
            else:
                r = self.sdo_table.rowCount(); self.sdo_table.insertRow(r)
                self.sdo_row_map[key] = r
        else:
            r = self.sdo_table.rowCount(); self.sdo_table.insertRow(r)

        node_id = cob - 0x580 if 0x580 <= cob <= 0x5FF else (cob & 0x7F)
        # helper to set/update a cell on row r
        def setcell(col, text, bold=False, center=False):
            it = self.sdo_table.item(r, col)
            if it is None:
                it = QtWidgets.QTableWidgetItem()
                self.sdo_table.setItem(r, col, it)
            it.setText(str(text) if text is not None else "")
            if bold:
                f = it.font(); f.setBold(True); it.setFont(f)
            if center:
                it.setTextAlignment(QtCore.Qt.AlignCenter)
            # keep coloring consistent
            it.setBackground(color_for_type(ftype))

        setcell(0, time_str)
        setcell(1, str(node_id), bold=True, center=True)
        setcell(2, f"0x{cob:03X}", center=True)
        setcell(3, ftype, center=True)
        # If we pre-populated this row with OD name, don't overwrite unless name provided
        setcell(4, name)
        setcell(5, ", ".join(idx_list), center=True)
        setcell(6, ", ".join(sub_list), center=True)
        setcell(7, dtype)
        setcell(8, bytes_to_hex_str(raw_bytes))
        decoded_shown = decoded if len(decoded) <= DECODED_CHAR_LIMIT else decoded[:DECODED_CHAR_LIMIT-1] + "…"
        setcell(9, decoded_shown, bold=True)
        item = self.sdo_table.item(r, 9)
        if item and decoded_shown != decoded:
            item.setToolTip(decoded)

        # --- Handle Count column ---
        shown_value = decoded
        try:
            if dtype.upper() == "BOOLEAN":
                shown_value = str(raw_bytes[0])
            elif dtype.upper() in ("UNSIGNED8", "INTEGER8"):
                shown_value = str(raw_bytes[0])
            elif dtype.upper() in ("UNSIGNED16", "INTEGER16"):
                shown_value = str(int.from_bytes(raw_bytes[:2], "little", signed="INTEGER" in dtype.upper()))
            elif dtype.upper() in ("UNSIGNED32", "INTEGER32"):
                shown_value = str(int.from_bytes(raw_bytes[:4], "little", signed="INTEGER" in dtype.upper()))
            elif dtype.upper() in ("UNSIGNED64", "INTEGER64"):
                shown_value = str(int.from_bytes(raw_bytes[:8], "little", signed="INTEGER" in dtype.upper()))
            else:
                shown_value = decoded
        except Exception:
            shown_value = decoded

        shown_trim = shown_value if len(shown_value) <= DECODED_CHAR_LIMIT else shown_value[:DECODED_CHAR_LIMIT-1] + "…"
        setcell(9, shown_trim, bold=True)
        item = self.sdo_table.item(r, 9)
        if item and shown_trim != shown_value:
            item.setToolTip(shown_value)

        # --- Handle Count column ---
        count_item = self.sdo_table.item(r, 10)
        if count_item is None:
            count_item = QtWidgets.QTableWidgetItem("1")
            count_item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.sdo_table.setItem(r, 10, count_item)
        else:
            try:
                val = int(count_item.text())
            except ValueError:
                val = 0
            count_item.setText(str(val + 1))

    def frame_matches_filter(self, frame: dict) -> bool:
        txt = self.filter_edit.text().strip()
        if not txt:
            return True
        wanted_ids = set()
        substrs = []
        for tok in txt.split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                wanted_ids.add(int(tok, 0))
            except Exception:
                substrs.append(tok.lower())
        cob = frame.get("cob")
        if wanted_ids:
            if cob in wanted_ids or ((cob & ~0x7F) in wanted_ids):
                return True
            return False
        if substrs:
            hay = f"{frame.get('name','')} {frame.get('decoded','')} {frame.get('raw','')} {frame.get('type','')} 0x{cob:X}".lower()
            return any(s in hay for s in substrs)
        return True

    def frame_matches_follow(self, frame: dict) -> bool:
        if not self.follow_mode:
            return True
        if self.follow_mode.get("type") == "node":
            return frame.get("node") == self.follow_mode.get("value")
        if self.follow_mode.get("type") == "index":
            idxs = frame.get("index_list", [])
            subs = frame.get("sub_list", [])
            for i_text, s_text in zip(idxs, subs):
                try:
                    ix = int(i_text, 0); sb = int(s_text, 0)
                except Exception:
                    continue
                if ix == self.follow_mode.get("index") and sb == self.follow_mode.get("sub"):
                    return True
            return False
        return True

    def insert_or_update_row(self, frame: dict):
        mode = self.mode_combo.currentText()
        cob = frame["cob"]
        if mode == "Fixed":
            cob_text = f"0x{cob:03X}".lower()
            idx_text = frame.get("index", "")
            sub_text = frame.get("sub", "")

            # Look for an exact match (COB + index + sub)
            for r in range(self.table.rowCount()):
                cob_item = self.table.item(r, 2)
                idx_item = self.table.item(r, 5)
                sub_item = self.table.item(r, 6)
                if not cob_item or cob_item.text().lower() != cob_text:
                    continue
                if idx_item and sub_item and idx_item.text() == idx_text and sub_item.text() == sub_text:
                    self.update_row(r, frame)
                    # --- Increment Count column ---
                    count_item = self.table.item(r, 10)
                    if count_item is None:
                        count_item = QtWidgets.QTableWidgetItem("1")
                        count_item.setTextAlignment(QtCore.Qt.AlignCenter)
                        self.table.setItem(r, 10, count_item)
                    else:
                        try:
                            val = int(count_item.text())
                        except ValueError:
                            val = 0
                        count_item.setText(str(val + 1))
                    return

            # If no exact match, insert a new row
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.set_row(row, frame)
            # Initialize Count column
            count_item = QtWidgets.QTableWidgetItem("1")
            count_item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.table.setItem(row, 10, count_item)
        else:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.set_row(row, frame)
            # Initialize Count column for sequential rows
            count_item = QtWidgets.QTableWidgetItem("1")
            count_item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.table.setItem(row, 10, count_item)

        if self.table.rowCount() > 50000:
            self.table.removeRow(0)

    def set_row(self, row: int, frame: dict):
        bg = color_for_type(frame["type"])
        def mkitem(text, bold=False, center=False):
            it = QtWidgets.QTableWidgetItem(str(text))
            if bold:
                f = it.font(); f.setBold(True); it.setFont(f)
            it.setBackground(bg)
            if center:
                it.setTextAlignment(QtCore.Qt.AlignCenter)
            return it
        self.table.setItem(row, 0, mkitem(frame["time"]))
        node_text = str(frame["node"]) if frame["type"] != "PDO" else ""
        it_node = mkitem(node_text, bold=(node_text!=""), center=True)
        self.table.setItem(row, 1, it_node)
        self.table.setItem(row, 2, mkitem(f"0x{frame['cob']:03X}", center=True))
        self.table.setItem(row, 3, mkitem(frame["type"], center=True))
        self.table.setItem(row, 4, mkitem(frame.get("name","")))
        self.table.setItem(row, 5, mkitem(", ".join(frame.get("index_list", [])), center=True))
        self.table.setItem(row, 6, mkitem(", ".join(frame.get("sub_list", [])), center=True))
        self.table.setItem(row, 7, mkitem(frame.get("dtype","")))
        self.table.setItem(row, 8, mkitem(frame.get("raw","")))
        decoded_full = frame.get("decoded","")
        decoded_shown = decoded_full if len(decoded_full)<=DECODED_CHAR_LIMIT else decoded_full[:DECODED_CHAR_LIMIT-1] + "…"
        dec_item = mkitem(decoded_shown, bold=True)
        if decoded_shown != decoded_full:
            dec_item.setToolTip(decoded_full)
        self.table.setItem(row, 9, dec_item)

        # --- Handle Count column for CAN-Traces ---
        count_item = self.table.item(row, 10)
        mode = self.mode_combo.currentText()
        if mode == "Fixed":
            if count_item is None:
                count_item = QtWidgets.QTableWidgetItem("1")
                count_item.setTextAlignment(QtCore.Qt.AlignCenter)
                self.table.setItem(row, 10, count_item)
            else:
                try:
                    val = int(count_item.text())
                except ValueError:
                    val = 0
                count_item.setText(str(val + 1))
        else:
            # Sequential mode → always start at 1
            count_item = QtWidgets.QTableWidgetItem("1")
            count_item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.table.setItem(row, 10, count_item)

        for c in (1,2,3,5,6):
            it = self.table.item(row, c)
            if it: it.setTextAlignment(QtCore.Qt.AlignCenter)

    def update_row(self, row: int, frame: dict):
        def settext(col, text, bold=False, center=False):
            it = self.table.item(row, col)
            if it is None:
                it = QtWidgets.QTableWidgetItem()
                self.table.setItem(row, col, it)
            it.setText(str(text))
            if bold:
                f = it.font(); f.setBold(True); it.setFont(f)
            if center:
                it.setTextAlignment(QtCore.Qt.AlignCenter)
        settext(0, frame["time"])
        node_text = str(frame["node"]) if frame["type"] != "PDO" else ""
        settext(1, node_text, bold=(node_text!=""), center=True)
        settext(2, f"0x{frame['cob']:03X}", center=True)
        settext(3, frame["type"], center=True)
        settext(4, frame.get("name",""))
        settext(5, ", ".join(frame.get("index_list", [])), center=True)
        settext(6, ", ".join(frame.get("sub_list", [])), center=True)
        settext(7, frame.get("dtype",""))
        settext(8, frame.get("raw",""))
        full_dec = frame.get("decoded","")
        shown = full_dec if len(full_dec)<=DECODED_CHAR_LIMIT else full_dec[:DECODED_CHAR_LIMIT-1]+"…"
        settext(9, shown, bold=True)
        item = self.table.item(row, 9)
        if item and len(full_dec) > len(shown):
            item.setToolTip(full_dec)
        old = self.last_values_by_cob.get(frame["cob"])
        if old != frame.get("decoded",""):
            self._flash_row(row)
        self.last_values_by_cob[frame["cob"]] = frame.get("decoded","")

    def _flash_row(self, row: int):
        highlight = QtGui.QColor(255,235,160)
        original = []
        for col in range(self.table.columnCount()):
            it = self.table.item(row, col)
            if it:
                original.append((col, it.background()))
                it.setBackground(highlight)
        timer = QtCore.QTimer(self)
        def restore():
            for col, brush in original:
                it = self.table.item(row, col)
                if it:
                    it.setBackground(brush)
            timer.stop()
        timer.setSingleShot(True)
        timer.timeout.connect(restore)
        timer.start(500)

    # -------------- UI actions ----------------
    def toggle_pause(self):
        self.pause = not self.pause
        self.pause_btn.setText("Resume" if self.pause else "Pause")

    def clear_table(self):
        self.table.setRowCount(0)
        self.sdo_table.setRowCount(0)
        self.buffer_frames.clear()
        self.pause = False
        self.timestamps.clear()
        self.peak_rate = 0.0
        self.last_values_by_cob.clear()
        self.follow_mode = None
        self.cob_seen.clear()
        self.legend_list.clear()
        # clear SDO mapping if present
        try:
            self.sdo_row_map.clear()
        except Exception:
            self.sdo_row_map = {}
        # Reset counts by clearing Count column
        for t in (self.table, self.sdo_table):
            for r in range(t.rowCount()):
                item = t.item(r, 10)
                if item:
                    item.setText("0")

    def clear_filter(self):
        self.filter_edit.clear()
        self.rebuild_table()

    def on_filter_changed(self, txt):
        self.rebuild_table()

    def rebuild_table(self):
        # rebuild visible rows from buffer applying filter and follow
        self.table.setRowCount(0)
        for frame in self.buffer_frames:
            if self.frame_matches_filter(frame) and self.frame_matches_follow(frame):
                self.insert_or_update_row(frame)
        self.save_settings()

    def apply_special_visibility_filter(self):
        show = self.show_special_action.isChecked()
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 3)
            if not it: continue
            t = it.text()
            if t in ("Heartbeat","Emergency","Time"):
                self.table.setRowHidden(r, not show)

    def toggle_dtype_column(self):
        show = self.show_dtype_action.isChecked()
        self.table.horizontalHeader().setSectionHidden(7, not show)
        self.sdo_table.horizontalHeader().setSectionHidden(7, not show)

    # -------------- follow mode ----------------
    def set_follow_node(self, node_id: int):
        self.follow_mode = {"type":"node","value":node_id}
        self.follow_label.setText(f"FOLLOW: Node {node_id}")
        self.rebuild_table()

    def set_follow_index(self, index: int, sub: int):
        self.follow_mode = {"type":"index","index":index,"sub":sub}
        self.follow_label.setText(f"FOLLOW: 0x{index:04X}:{sub:02X}")
        self.rebuild_table()

    def clear_follow(self):
        self.follow_mode = None
        self.follow_label.setText("")
        self.rebuild_table()

    # -------------- context menu & copy ----------------
    def context_menu(self, pos):
        sel_rows = sorted(set(idx.row() for idx in self.table.selectedIndexes()))
        menu = QtWidgets.QMenu(self)
        act_copy = menu.addAction("Copy Selected Details")
        act_copy_csv = menu.addAction("Copy Selected as CSV")
        act_follow_node = menu.addAction("Follow Node (from first selected)")
        act_follow_index = menu.addAction("Follow Index/Subindex (from first selected)")
        act = menu.exec_(self.table.viewport().mapToGlobal(pos))
        if act == act_copy:
            blocks = [self.format_row_details(r) for r in sel_rows]
            QtWidgets.QApplication.clipboard().setText("\n\n".join(blocks))
        elif act == act_copy_csv:
            lines = []
            for r in sel_rows:
                vals = [self.table.item(r, c).text() if self.table.item(r, c) else "" for c in range(self.table.columnCount())]
                lines.append(",".join(vals))
            QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        elif act == act_follow_node and sel_rows:
            r = sel_rows[0]
            node_item = self.table.item(r, 1)
            if node_item and node_item.text():
                try:
                    nid = int(node_item.text(), 0)
                    self.set_follow_node(nid)
                except Exception:
                    pass
        elif act == act_follow_index and sel_rows:
            r = sel_rows[0]
            idx_item = self.table.item(r, 5)
            sub_item = self.table.item(r, 6)
            if idx_item and sub_item:
                try:
                    idxs = idx_item.text().split(",")[0].strip()
                    subs = sub_item.text().split(",")[0].strip()
                    ix = int(idxs, 0); sb = int(subs, 0)
                    self.set_follow_index(ix, sb)
                except Exception:
                    pass

    def context_menu_sdo(self, pos):
        sel_rows = sorted(set(idx.row() for idx in self.sdo_table.selectedIndexes()))
        menu = QtWidgets.QMenu(self)
        act_copy = menu.addAction("Copy Selected SDO Details")
        act_copy_csv = menu.addAction("Copy Selected SDO as CSV")
        act_follow_node = menu.addAction("Follow Node (from first selected)")
        act_follow_index = menu.addAction("Follow Index/Subindex (from first selected)")
        act = menu.exec_(self.sdo_table.viewport().mapToGlobal(pos))
        if act == act_copy:
            blocks = []
            for r in sel_rows:
                vals = [self.sdo_table.item(r, c).text() if self.sdo_table.item(r, c) else "" for c in range(self.sdo_table.columnCount())]
                ts, node, cob, ftype, name, idx, sub, dtype, raw, dec = vals
                blocks.append(f"Time: {ts}\nCOB-ID: {cob}\nNode: {node}\nType: {ftype}\nName: {name}\nData Type: {dtype}\nIndex: {idx}\nSubindex: {sub}\nRaw Value: {raw}\nDecoded: {dec}")
            QtWidgets.QApplication.clipboard().setText("\n\n".join(blocks))
        elif act == act_copy_csv:
            lines = []
            for r in sel_rows:
                vals = [self.sdo_table.item(r, c).text() if self.sdo_table.item(r, c) else "" for c in range(self.sdo_table.columnCount())]
                lines.append(",".join(vals))
            QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        elif act == act_follow_node and sel_rows:
            r = sel_rows[0]
            node_item = self.sdo_table.item(r, 1)
            if node_item and node_item.text():
                try:
                    nid = int(node_item.text(), 0)
                    self.set_follow_node(nid)
                except Exception:
                    pass
        elif act == act_follow_index and sel_rows:
            r = sel_rows[0]
            idx_item = self.sdo_table.item(r, 5)
            sub_item = self.sdo_table.item(r, 6)
            if idx_item and sub_item:
                try:
                    idxs = idx_item.text().split(",")[0].strip()
                    subs = sub_item.text().split(",")[0].strip()
                    ix = int(idxs, 0); sb = int(subs, 0)
                    self.set_follow_index(ix, sb)
                except Exception:
                    pass

    # -------------- double click row ----------------
    def on_table_double_click(self, row: int, col: int):
        txt = self.format_row_details(row)
        dlg = QtWidgets.QMessageBox(self)
        dlg.setWindowTitle("Frame Details")
        dlg.setText(txt)
        copy_btn = dlg.addButton("Copy Details", QtWidgets.QMessageBox.AcceptRole)
        dlg.addButton("Close", QtWidgets.QMessageBox.RejectRole)
        dlg.exec_()
        if dlg.clickedButton() == copy_btn:
            QtWidgets.QApplication.clipboard().setText(txt)

    def format_row_details(self, row: int) -> str:
        vals = [self.table.item(row, c).text() if self.table.item(row, c) else "" for c in range(self.table.columnCount())]
        ts, node, cob, ftype, name, idx, sub, dtype, raw, dec = vals
        dec_item = self.table.item(row, 9)
        full_dec = dec_item.toolTip() if dec_item and dec_item.toolTip() else dec
        return (f"Time: {ts}\nCOB-ID: {cob}\nNode: {node}\nType: {ftype}\nName: {name}\nData Type: {dtype}\nIndex: {idx}\nSubindex: {sub}\nRaw Value: {raw}\nDecoded: {full_dec}")

    # -------------- export functions ----------------
    def export_csv_dialog(self):
        dlg = QtWidgets.QMessageBox(self)
        dlg.setWindowTitle("Export CSV")
        dlg.setText("Save all logs?\nSelect Yes, to save all logs in buffer.\nSelect No, to save only filtered logs.")
        yes = dlg.addButton(QtWidgets.QMessageBox.Yes)
        no = dlg.addButton(QtWidgets.QMessageBox.No)
        dlg.addButton(QtWidgets.QMessageBox.Cancel)
        dlg.exec_()
        btn = dlg.clickedButton()
        if btn == yes:
            rows = range(self.table.rowCount())
        elif btn == no:
            rows = [i for i in range(self.table.rowCount()) if not self.table.isRowHidden(i)]
        else:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save CSV", "sniffer.csv", "CSV Files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                headers = [self.table.horizontalHeaderItem(c).text() for c in range(self.table.columnCount())]
                w.writerow(headers)
                for r in rows:
                    row_vals = []
                    for c in range(self.table.columnCount()):
                        itm = self.table.item(r, c)
                        if itm is None:
                            row_vals.append("")
                        else:
                            if c == 9 and itm.toolTip():
                                row_vals.append(itm.toolTip())
                            else:
                                row_vals.append(itm.text())
                    w.writerow(row_vals)
            QtWidgets.QMessageBox.information(self, "Export", f"CSV written: {path}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Export Error", f"Failed to save CSV: {e}")

    def export_hist_csv(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Histogram CSV", "histogram.csv", "CSV Files (*.csv)")
        if not path:
            return
        ok = self.hist.export_counts_to_csv(path)
        if ok:
            QtWidgets.QMessageBox.information(self, "Export", f"Histogram saved to {path}")
        else:
            QtWidgets.QMessageBox.warning(self, "Export", "Failed to save histogram CSV")

    def export_json(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save JSON", "sniffer.json", "JSON Files (*.json)")
        if not path:
            return
        try:
            dlg = QtWidgets.QMessageBox(self)
            dlg.setWindowTitle("Export JSON")
            dlg.setText("Save all logs?\nSelect Yes, to save all logs in buffer.\nSelect No, to save only filtered logs.")
            yes = dlg.addButton(QtWidgets.QMessageBox.Yes)
            no = dlg.addButton(QtWidgets.QMessageBox.No)
            dlg.addButton(QtWidgets.QMessageBox.Cancel)
            dlg.exec_()
            btn = dlg.clickedButton()
            if btn == yes:
                frames = list(self.buffer_frames)
            elif btn == no:
                frames = []
                for r in range(self.table.rowCount()):
                    if self.table.isRowHidden(r):
                        continue
                    frames.append(self._row_to_frame_dict(r))
            else:
                return
            with open(path, "w") as f:
                json.dump(frames, f, indent=2)
            QtWidgets.QMessageBox.information(self, "Export", f"JSON saved: {path}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Export Error", f"Failed to save JSON: {e}")

    def export_pcap(self):
        if PcapWriter is None:
            QtWidgets.QMessageBox.warning(self, "Export Error",
                                        "PCAP export is not available in your python-can version.")
            return

        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save PCAP", "sniffer.pcap", "PCAP Files (*.pcap)")
        if not path:
            return

        try:
            dlg = QtWidgets.QMessageBox(self)
            dlg.setWindowTitle("Export PCAP")
            dlg.setText("Save all logs?\nSelect Yes, to save all logs in buffer.\nSelect No, to save only filtered logs.")
            yes = dlg.addButton(QtWidgets.QMessageBox.Yes)
            no = dlg.addButton(QtWidgets.QMessageBox.No)
            dlg.addButton(QtWidgets.QMessageBox.Cancel)
            dlg.exec_()
            btn = dlg.clickedButton()

            if btn == yes:
                frames = list(self.buffer_frames)
            elif btn == no:
                frames = []
                for r in range(self.table.rowCount()):
                    if self.table.isRowHidden(r):
                        continue
                    frames.append(self._row_to_frame_dict(r))
            else:
                return

            writer = PcapWriter(path, append=False, sync=True)
            for f in frames:
                arb = f.get("cob", 0)
                raw_hex = f.get("raw", "")
                data = bytes(int(x, 16) for x in raw_hex.split() if x)
                msg = can.Message(arbitration_id=arb, data=data,
                                is_extended_id=False, timestamp=time.time())
                writer.write(msg)
            writer.flush()
            writer.close()
            QtWidgets.QMessageBox.information(self, "Export", f"PCAP saved: {path}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Export Error", f"Failed to save PCAP: {e}")


    def _row_to_frame_dict(self, r: int) -> dict:
        vals = [self.table.item(r, c).text() if self.table.item(r, c) else "" for c in range(self.table.columnCount())]
        ts, node, cob, ftype, name, idx, sub, dtype, raw, dec = vals
        dec_item = self.table.item(r, 9)
        full_dec = dec_item.toolTip() if dec_item and dec_item.toolTip() else dec
        return {
            "time": ts,
            "node": node,
            "cob": cob,
            "type": ftype,
            "name": name,
            "index": idx,
            "subindex": sub,
            "data_type": dtype,
            "raw": raw,
            "decoded": full_dec
        }

    # -------------- remote control sending ----------------
    def on_send_sdo(self):
        idx_txt = self.sdo_index_edit.text().strip()
        sub_txt = self.sdo_sub_edit.text().strip()
        val_txt = self.sdo_value_edit.text().strip()
        size = int(self.sdo_size_combo.currentText())
        try:
            node = int(self.sdo_send_node.text(), 0)
            idx = int(idx_txt, 0)
            sub = int(sub_txt, 0)
            if val_txt.lower().startswith("0x"):
                val = int(val_txt, 16)
            else:
                val = int(val_txt, 0)
            data = val.to_bytes(size, "little")
            # Send raw SDO-like frame to 0x600 + node (user/device must handle actual SDO protocol)
            cob = 0x600 + node
            payload = bytearray(8)
            payload[0] = 0x23 if size == 4 else 0x2B if size == 2 else 0x2F if size == 1 else 0x23
            payload[1] = idx & 0xFF
            payload[2] = (idx>>8) & 0xFF
            payload[3] = sub & 0xFF
            for i in range(min(size, 4)):
                payload[4+i] = data[i]
            bus = can.interface.Bus(channel=self.worker.channel, bustype="socketcan")
            msg = can.Message(arbitration_id=cob, data=bytes(payload), is_extended_id=False)
            bus.send(msg)
            QtWidgets.QMessageBox.information(self, "SDO", f"Sent SDO-like message to node 0x{node:02X}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "SDO Error", f"Invalid input or CAN error: {e}")

    def on_send_pdo(self):
        try:
            cob_txt = self.pdo_cob_edit.text().strip()
            data_txt = self.pdo_data_edit.text().strip()
            cob = int(cob_txt, 0)
            bytes_list = bytes(int(x, 16) for x in data_txt.split() if x)

            bus = can.interface.Bus(channel=self.worker.channel, bustype="socketcan")
            msg = can.Message(arbitration_id=cob, data=bytes_list, is_extended_id=False)
            bus.send(msg)

            # Instead of popup → update status bar
            if self.pdo_repeat_chk.isChecked() and not self.pdo_timer.isActive():
                interval = self.pdo_interval_spin.value()
                self.pdo_timer.start(interval)
                self.pdo_send_btn.setEnabled(False)
                self.pdo_stop_btn.setEnabled(True)
                self.status.showMessage(f"Repeating PDO 0x{cob:03X} every {interval} ms", 2000)
            elif not self.pdo_repeat_chk.isChecked():
                self.status.showMessage(f"PDO 0x{cob:03X} sent", 2000)

        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "PDO Error", f"Failed to send PDO: {e}")

    def on_send_pdo_clicked(self):
        """Handle PDO send with optional repeat mode."""
        if self.pdo_repeat_chk.isChecked():
            interval = self.pdo_interval_spin.value()
            self.pdo_timer.start(interval)
            self.pdo_send_btn.setEnabled(False)
            self.pdo_stop_btn.setEnabled(True)
            self.status.showMessage(f"Repeating PDO @ {interval} ms", 0)
        else:
            self.on_send_pdo()

    def on_stop_pdo(self):
        """Stop repeat PDO sending."""
        self.pdo_timer.stop()
        self.pdo_send_btn.setEnabled(True)
        self.pdo_stop_btn.setEnabled(False)
        self.status.clearMessage()
        self.status.showMessage("PDO repeat stopped", 2000)

    def on_recv_sdo(self):
        try:
            node = int(self.sdo_recv_node.text(), 0)
            idx = int(self.sdo_recv_index.text(), 0)
            sub = int(self.sdo_recv_sub.text(), 0)
            cob = 0x600 + node
            payload = bytearray(8)
            payload[0] = 0x40  # SDO initiate upload request
            payload[1] = idx & 0xFF
            payload[2] = (idx>>8) & 0xFF
            payload[3] = sub & 0xFF
            bus = can.interface.Bus(channel=self.worker.channel, bustype="socketcan")
            msg = can.Message(arbitration_id=cob, data=bytes(payload), is_extended_id=False)
            bus.send(msg)
            QtWidgets.QMessageBox.information(self, "SDO Receive", f"Upload request sent to node 0x{node:02X}")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "SDO Receive Error", f"{e}")

    # -------------- status update ----------------
    def update_load_label(self):
        cur = len(self.timestamps)/max(0.0001, LOAD_WINDOW) if self.timestamps else 0.0
        self.load_label.setText(f"Load: current={cur:.2f}/s peak={self.peak_rate:.2f}/s")
        self.save_settings()

    # -------------- cleanup ----------------
    def closeEvent(self, event):
        self.save_settings()
        try:
            self.worker.stop()
        except Exception:
            pass
        event.accept()

# ---------------- main ----------------
def main():
    parser = argparse.ArgumentParser(description="CANopen Sniffer")
    parser.add_argument("--eds", help="EDS file path (optional)", default=None)
    parser.add_argument("--interface", help="socketcan interface", default="can0")
    args = parser.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName(APP_ORG)
    app.setApplicationName(APP_NAME)
    w = MainWindow(args.eds, args.interface)
    w.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
