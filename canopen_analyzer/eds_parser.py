#!/usr/bin/env python3
# ██╗ ██████╗ ████████╗ █████╗ ██████╗
# ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
# ██║██║   ██║   ██║   ███████║ █████╔╝
# ██║██║   ██║   ██║   ██╔══██║██╔═══╝
# ██║╚██████╔╝   ██║   ██║  ██║███████╗
# ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝
# Copyright (c) 2025 iota2 (iota2 Engineering Tools)
# Licensed under the MIT License. See LICENSE file in the project root for details.

"""!
@file eds_parser.py
@brief CANopen EDS (Electronic Data Sheet) parsing and PDO/name resolution utilities.
@details
This module implements the @ref eds_parser class, which is responsible for
parsing CANopen EDS (Electronic Data Sheet) files and extracting metadata
required by the CANopen Sniffer framework.

The parser focuses on **read-only metadata extraction** and is designed to be
robust against incomplete or non-compliant EDS files. Parsing errors are
handled gracefully and reported via logging rather than raising fatal
exceptions.

### Responsibilities
- Parse Object Dictionary entries and build a mapping of
  `(index, subindex) -> ParameterName`
- Parse RPDO/TPDO mapping objects (1Axx / 18xx) and construct
  COB-ID–to–mapping tables
- Provide human-readable PDO field names for display backends (TUI/CLI/GUI)
- Validate PDO mappings and emit warnings for missing ParameterName entries

### Design Notes
- This module does **not** modify EDS files; it is strictly a consumer.
- All parsing is performed using Python's ConfigParser in a tolerant mode
  to support real-world EDS variations.
- The parser integrates with:
  - @ref FrameProcessor for PDO/SDO decoding
  - @ref BusStats for enriched protocol visibility
  - Display backends for readable field labeling

### Threading Model
The parser itself is **not state-mutating after initialization** and is safe
to be shared across worker threads once constructed.

### Error Handling
- Malformed sections or invalid mapping entries are skipped
- Missing ParameterName definitions result in warnings, not failures
- All critical issues are logged using the module-scoped logger
"""

import re
import logging
import configparser

import analyzer_defs as analyzer_defs

class eds_parser:
    """! Parser for CANopen EDS (Electronic Data Sheet) files.
    @details
    Provides utilities to extract ParameterName mappings and PDO mappings
    from an EDS file. The parser stores a name map for object dictionary
    entries, a PDO map keyed by COB-ID, and optional COB name overrides
    for more readable PDO field names.
    @param eds_path Path to the EDS file to parse. If None, parsing is skipped
                    until an EDS path is provided (or methods are called directly).
    """

    def __init__(self, eds_path: str | None = None):
        """! Initialize EDS parser and optionally parse the given EDS file.
        @details
        If eds_path is provided the constructor attempts to build a name map,
        parse PDO mappings and log basic load information. Any parsing errors
        are caught and logged as warnings so that a malformed EDS does not
        crash the application.
        @param eds_path Path to the EDS file to load (hex/INI-style format).
        """

        ## Logger instance scoped to this parser.
        self.log = logging.getLogger(f"{analyzer_defs.APP_NAME}.{self.__class__.__name__}")

        ## Path to the EDS file supplied to this parser (or None).
        self.eds_path = eds_path

        ## Mapping of COB-ID -> list of (index, subindex, size) tuples.
        self.pdo_map = {}

        ## Mapping of (index, subindex) -> human-readable ParameterName.
        self.name_map = {}

        ## Mapping of COB-ID -> list of human-readable field names for that PDO.
        self.cob_name_overrides = {}

        if eds_path:
            try:
                self.name_map = self.build_name_map()
                self.pdo_map = self.parse_pdo_map()
                self.log_pdo_mapping_consistency()
                self.log.info(f"Loaded EDS: {self.eds_path} (pdo_map={len(self.pdo_map)}, names={len(self.name_map)})")
            except Exception as e:
                self.log.warning(f"Failed to parse EDS '{self.eds_path}': {e}")

    def build_name_map(self):
        """! Build a mapping from object dictionary entries to names.
        @details
        Parses the EDS/INI-style sections to find ParameterName entries for
        indexes and subindexes. Creates entries keyed by (index, subindex).
        If a subindex has no explicit ParameterName, the parent parameter name
        (index) is used; otherwise the parent is represented as "0x{index:04X}".
        Entries that include the word "highest" (case-insensitive) are treated
        as not providing a meaningful sub-ParameterName and the parent name is used.
        @return dict A mapping {(index:int, sub:int): "Parent.ParameterName" or "Parent"}.
        """
        name_map = {}
        cfg = configparser.ConfigParser(strict=False)
        cfg.optionxform = str
        cfg.read(self.eds_path)
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
            idx, sub = int(m.group(1), 16), int(m.group(2), 16)
            pname = cfg[sec].get("ParameterName", "").strip()
            parent = parents.get(idx, f"0x{idx:04X}")
            if pname and "highest" not in pname.lower():
                name_map[(idx, sub)] = f"{parent}.{pname}"
            else:
                name_map[(idx, sub)] = parent
        for idx, parent in parents.items():
            name_map.setdefault((idx, 0), parent)
        return name_map

    def parse_pdo_map(self):
        """! Parse PDO mapping entries from the EDS file.
        @details
        Scans for 1Axx (RPDO/TPDO mapping) sections and their subentries to
        extract the mapped Object Dictionary indices, subindexes and sizes.
        For each mapping found, the corresponding communication object (18xx)
        is checked for a COB-ID (sub1 DefaultValue) and the mapping is stored
        keyed by that COB-ID. Also builds a cob_name_overrides dictionary
        containing readable field names (using the name_map) for each COB-ID.
        @return dict Mapping of cob_id (int) -> list of (index:int, sub:int, size:int).
        """
        cfg = configparser.ConfigParser(strict=False)
        cfg.optionxform = str
        cfg.read(self.eds_path)
        pdo_map = {}
        cob_name_overrides = {}
        for sec in cfg.sections():
            if sec.upper().startswith("1A") and "SUB" not in sec.upper():
                try:
                    entries = []
                    subidx = 1
                    while f"{sec}sub{subidx}" in cfg:
                        raw = analyzer_defs.clean_int_with_comment(cfg[f"{sec}sub{subidx}"]["DefaultValue"])
                        index = (raw >> 16) & 0xFFFF
                        sub = (raw >> 8) & 0xFF
                        size = raw & 0xFF
                        entries.append((index, sub, size))
                        subidx += 1
                    comm_sec = sec.replace("1A", "18", 1)
                    comm_sub1 = f"{comm_sec}sub1"
                    if comm_sub1 in cfg:
                        cob_id = analyzer_defs.clean_int_with_comment(cfg[comm_sub1]["DefaultValue"])
                        pdo_map[cob_id] = entries
                        names = []
                        for (idx, sub, _) in entries:
                            pname = (self.name_map.get((idx, sub))
                                     or self.name_map.get((idx, 0))
                                     or f"0x{idx:04X}:{sub}")
                            names.append(pname)
                        cob_name_overrides[cob_id] = names
                except Exception:
                    continue
        self.cob_name_overrides = cob_name_overrides
        return pdo_map

    def log_pdo_mapping_consistency(self):
        """! Emit warnings for PDO mappings that lack ParameterName information.
        @details
        Iterates over the currently stored pdo_map and verifies that every mapped
        (index, subindex) has an entry in the name_map. If neither (index, subindex)
        nor (index, 0) is present, a warning is logged with the offending COB-ID
        and object reference. This helps identify EDS files missing ParameterName
        entries for mapped PDO fields.
        @return None
        """
        for cob_id, entries in self.pdo_map.items():
            for (idx, sub, _) in entries:
                if (idx, sub) not in self.name_map and (idx, 0) not in self.name_map:
                    self.log.warning(f"COB 0x{cob_id:03X} maps to 0x{idx:04X}:{sub}, no ParameterName")
