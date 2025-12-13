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
@file canopen_sniffer.py
@brief CANopen sniffer main module and EDS helper utilities.
@details
Provides the core sniffer thread that listens on a CAN interface (SocketCAN
or PCAN), frame processing pipelines that classify and enrich frames, and
helpers for EDS (Electronic Data Sheet) parsing.

Key components documented:
 - eds_parser: builds name maps and PDO mappings used by UIs to resolve
   object dictionary indices into human-readable parameter names.
 - can_sniffer: threading class which reads raw CAN frames and pushes them to
   processing queues.
 - frame_processor: consumes frames, decodes CANopen content and updates
   statistics and output sinks (TUI/GUI/CSV).

Resource management: sockets and files are opened lazily and closed during
graceful shutdown. Parsing is resilient to incomplete/invalid EDS files and
will log warnings rather than raising for non-critical inconsistencies.
"""

import copy
import os
import re
import sys
import csv
import time
import struct
import logging
import argparse
import configparser
import signal

from datetime import datetime, timedelta, UTC
from dataclasses import dataclass, field
from collections import Counter, deque

import threading
import queue


import can
from can import exceptions as can_exceptions
import canopen

import sniffer_defs as sniffer_defs
from bus_stats import bus_stats
from display_cli import display_cli
from display_tui import display_tui
from display_gui import display_gui

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
        self.log = logging.getLogger(self.__class__.__name__)

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
                        raw = sniffer_defs.clean_int_with_comment(cfg[f"{sec}sub{subidx}"]["DefaultValue"])
                        index = (raw >> 16) & 0xFFFF
                        sub = (raw >> 8) & 0xFF
                        size = raw & 0xFF
                        entries.append((index, sub, size))
                        subidx += 1
                    comm_sec = sec.replace("1A", "18", 1)
                    comm_sub1 = f"{comm_sec}sub1"
                    if comm_sub1 in cfg:
                        cob_id = sniffer_defs.clean_int_with_comment(cfg[comm_sub1]["DefaultValue"])
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



class can_sniffer(threading.Thread):
    """! CAN bus sniffer thread.
    @brief Threaded CAN sniffer which reads frames from a socketcan interface,
           optionally exports raw frames to CSV and pushes frames to a processing queue.
    @details
    The sniffer opens a `socketcan` interface, receives `can.Message` frames,
    enqueues them on `raw_frame` for downstream processing, and optionally writes
    raw frames to a CSV file for offline analysis. The thread supports a graceful
    shutdown via `stop()`. Logging is performed on a per-instance logger.
    """

    def __init__(self, interface: str, raw_frame: queue.Queue = None, export: bool = False):
        """! Initialize CAN sniffer thread and open resources.
        @details
        The constructor opens the socketcan Bus and attempts to connect a
        CANopen Network (non-fatal). If CSV export is enabled, the CSV file
        and writer are created and a header row is persisted.
        @param interface CAN interface name as string (e.g., "can0" or "vcan0").
        @param raw_frame `queue.Queue` instance to push received frames for processing.
        @param export If True, enable CSV export of raw frames to a file.
        """
        super().__init__(daemon=True)

        ## Queue used to push raw frames for downstream processing.
        self.raw_frame = raw_frame or queue.Queue()

        ## Thread stop event used to signal the run loop to exit.
        self._stop_event = threading.Event()

        ## Logger instance for this sniffer.
        self.log = logging.getLogger(self.__class__.__name__)

        ## CAN interface name used by the sniffer.
        self.interface = interface

        ## Flag indicating whether CSV export is enabled.
        self.export = export

        ## CSV file name used when export is enabled.
        self.export_filename = f"{sniffer_defs.APP_NAME}_raw.csv"

        ## File object for CSV export (or None if not exporting).
        self.export_file = None

        ## csv.writer instance used to write CSV rows (or None).
        self.export_writer = None

        ## Export serial number (incremented for each exported row).
        self.export_serial_number = 1

        if self.export:
            try:
                self.export_file = open(self.export_filename, "w", newline="")
                self.export_writer = csv.writer(self.export_file)
                self.export_writer.writerow(
                    ["S.No.", "Time", "COB-ID", "Error", "Raw"]
                )
                # persist header
                try:
                    self.export_file.flush()
                    os.fsync(self.export_file.fileno())
                except Exception:
                    pass
                self.log.info(f"CSV export enabled → {self.export_filename}")
            except Exception as e:
                self.log.exception("Failed to open CSV export file: %s", e)
                self.export = False

        # Open CAN socket
        try:
            ## CAN bus instance with configuration loading.
            self.bus = can.interface.Bus(channel=interface, interface="socketcan")
            self.log.info(f"CAN socket opened on {interface}")
        except Exception as e:
            self.log.exception("Failed to open CAN interface %s: %s", interface, e)
            raise

        ## Optional CANopen.Network instance (connected if possible).
        self.network = canopen.Network()
        try:
            self.network.connect(channel=interface, interface="socketcan")
            self.log.info(f"Connected Network on {interface}")
        except Exception:
            self.log.warning("Network connection failed (not critical)")

    # --- CSV export helper ---
    def save_frame_to_csv(self, cob: int, error: bool, raw: str):
        """! Save a received CAN frame (raw view) to the CSV export file.
        @details
        Writes a single CSV row with a serial number, timestamp, COB-ID,
        error flag and raw payload. Periodically flushes and fsyncs the file
        according to `defs.FSYNC_EVERY`.
        @param cob COB-ID as integer of the CAN frame.
        @param error Boolean indicating whether the frame is an error frame.
        @param raw Hex string representation of the payload.
        @return None.
        """
        if not self.export_writer:
            return
        try:
            self.export_writer.writerow([
                self.export_serial_number,
                sniffer_defs.now_str(),
                f"0x{cob:03X}",
                error,
                raw
            ])
            self.export_serial_number += 1
            # flush and fsync periodically
            try:
                self.export_file.flush()
                if (self.export_serial_number % sniffer_defs.FSYNC_EVERY) == 0:
                    os.fsync(self.export_file.fileno())
            except Exception:
                pass
        except Exception as e:
            self.log.error("CSV export failed: %s", e)


    # --- message handling ---
    def handle_message(self, msg: can.Message):
        """! Handle a received CAN message.
        @details
        Extracts arbitration id, raw payload and error flag, builds a small
        frame dictionary containing a timestamp and pushes it to `raw_frame`.
        Also logs the raw frame and triggers CSV export if enabled.
        @param msg The `can.Message` instance received from the bus.
        """
        # Total received data
        cob = msg.arbitration_id
        raw = msg.data
        error = msg.is_error_frame

        frame = {"time": time.time(), "cob": cob, "error": error, "raw": raw}
        # Push frame to queue
        self.raw_frame.put(frame)

        self.log.debug(f"Raw frame: [{sniffer_defs.now_str()}] [0x{cob:03X}] [{error}] [{sniffer_defs.bytes_to_hex(raw)}]")

        # Export to CSV
        self.save_frame_to_csv(cob, error, sniffer_defs.bytes_to_hex(raw))

    def run(self):
        """! Main loop of the sniffer thread.
        @details
        Continuously receives frames from the CAN bus using a short timeout,
        handles interrupt-like exceptions gracefully, and delegates message
        processing to `handle_message`. On exit, CSV file and bus resources
        are closed/shutdown cleanly.
        """
        self.log.info("Sniffer thread started (interface=%s)", self.interface)
        recv_timeout = 0.1

        try:
            while not self._stop_event.is_set():
                try:
                    msg = self.bus.recv(timeout=recv_timeout)
                except (InterruptedError, KeyboardInterrupt):
                    # signal interruption — re-check stop flag and continue/exit
                    if self._stop_event.is_set():
                        break
                    continue
                except can_exceptions.CanOperationError as e:
                    # Happens when the underlying socket is closed during shutdown.
                    # If we are stopping, treat silently; otherwise warn and break.
                    if self._stop_event.is_set():
                        self.log.debug("CanOperationError during shutdown: %s", e)
                        break
                    self.log.warning("CAN operation error (recv): %s", e)
                    break
                except OSError as e:
                    # OSError like "Bad file descriptor" can also occur when socket is closed.
                    if self._stop_event.is_set():
                        self.log.debug("OSError during shutdown: %s", e)
                        break
                    self.log.warning("OSError from CAN recv: %s", e)
                    # short sleep but wake on stop
                    if self._stop_event.wait(0.2):
                        break
                    continue
                except Exception as e:
                    # Unexpected error — log at debug level and backoff to avoid tight loop.
                    # Do not log full traceback when shutdown is in progress to avoid noisy output.
                    if self._stop_event.is_set():
                        self.log.debug("Unexpected exception during shutdown: %s", e)
                        break
                    self.log.exception("Unexpected error in CAN recv")
                    if self._stop_event.wait(0.2):
                        break
                    continue

                # Received message, handle it
                if msg:
                    try:
                        self.handle_message(msg)
                    except Exception:
                        self.log.exception("Exception while handling message")
        finally:
            # Always attempt to flush/close CSV (if any) and shutdown bus safely.
            if getattr(self, "export_file", None):
                try:
                    try:
                        self.export_file.flush()
                        os.fsync(self.export_file.fileno())
                    except Exception:
                        pass
                    self.export_file.close()
                    self.log.info("Raw CSV export file closed")
                except Exception:
                    self.log.exception("Failed to close raw CSV file")

            # shutdown bus
            try:
                if getattr(self, "bus", None) is not None:
                    # bus.shutdown() may raise if socket is already closed; ignore such errors
                    try:
                        self.bus.shutdown()
                    except Exception:
                        pass
                    self.log.info("CAN bus shutdown completed")
            except Exception:
                self.log.exception("Exception while shutting down CAN bus")

            self.log.info("Sniffer thread exiting")

    def stop(self, shutdown_bus: bool = True):
        """! Request the sniffer thread to stop and optionally shutdown the bus.
        @details
        Signals the run loop to exit via the internal `_stop_event` and attempts
        to shutdown the underlying CAN bus if requested.
        @param shutdown_bus If True, call `bus.shutdown()` when stopping.
        """
        self._stop_event.set()
        self.log.debug("Stop requested for sniffer thread")
        if shutdown_bus:
            try:
                if getattr(self, "bus", None) is not None:
                    # bus.shutdown() may raise "Bad file descriptor" if socket already closed;
                    # that's fine — we swallow exceptions here.
                    self.bus.shutdown()
                    self.log.debug("bus.shutdown() called from stop()")
            except Exception as e:
                self.log.debug("bus.shutdown() raised during stop(): %s", e)



class process_frame(threading.Thread):
    """! Processor thread that consumes CAN frames and updates statistics.
    @brief Consumes frames produced by the CAN sniffer, classifies them,
           updates @ref bus_stats, optionally exports processed rows to CSV, and
           handles SDO/SDO-response bookkeeping using an EDS map.
    @details
    The processor reads frame dictionaries from `raw_frame`, performs:
      - frame classification (NMT, SYNC, EMCY, TIME, PDO, SDO_REQ, SDO_RES, HB, UNKNOWN),
      - top-talker and node tracking,
      - SDO request/response timing and success/abort accounting,
      - payload-size accounting,
      - optional CSV export of decoded/processed rows.
    The thread is stoppable via `stop()` and will close CSV resources on exit.
    """

    def __init__(self, stats: bus_stats, raw_frame: queue.Queue, processed_frame: queue.Queue, eds_map: eds_parser, export: bool = False):
        """! Initialize the processor thread.
        @details
        The constructor stores references to required helpers, initializes a
        stop event and logging, sets up CSV export if requested, and ensures
        statistics collection start time is set.
        @param stats Instance of @ref bus_stats used to record statistics.
        @param raw_frame `queue.Queue` providing raw frames (dict) from the sniffer.
        @param processed_frame `queue.Queue` instance to push processed frames for display.
        @param eds_map @ref eds_parser providing name_map lookups.
        @param export If True, enable CSV export of processed frames.
        """
        super().__init__(daemon=True)

        ## Queue from which raw frame dictionaries are consumed.
        self.raw_frame = raw_frame

        ## Queue from which raw frame dictionaries are consumed.
        self.processed_frame = processed_frame

        ## Internal event used to signal the run loop to stop.
        self._stop_event = threading.Event()

        ## Logger instance scoped to this processor.
        self.log = logging.getLogger(self.__class__.__name__)

        ## EDS map/parser used to resolve (index, subindex) -> name strings.
        self.eds_map = eds_map

        ## Reference to the bus_stats instance used for recording metrics.
        self.stats = stats
        self.stats.set_start_time()

        ## Flag indicating whether processed CSV export is enabled.
        self.export = export

        ## Output filename for processed CSV export.
        self.export_filename = f"{sniffer_defs.APP_NAME}_processed.csv"

        ## File object for processed CSV export (or None).
        self.export_file = None

        ## csv.writer instance for processed CSV rows (or None).
        self.export_writer = None

        ## Serial number for exported rows (increments each write).
        self.export_serial_number = 1

        if self.export:
            try:
                self.export_file = open(self.export_filename, "w", newline="")
                self.export_writer = csv.writer(self.export_file)
                self.export_writer.writerow(
                    ["S.No.", "Time", "Type", "COB-ID", "Index", "Sub", "Name", "Raw", "Decoded"]
                )
                try:
                    self.export_file.flush()
                    os.fsync(self.export_file.fileno())
                except Exception:
                    pass
                self.log.info(f"CSV export enabled → {self.export_filename}")
            except Exception as e:
                self.log.exception("Failed to open CSV export file: %s", e)
                self.export = False

    def save_frame(self, cob: int, ftype: sniffer_defs.frame_type, index: int, sub: int, name: str, raw: str, decoded: str):
        """! Save a processed CANopen frame for downstream use or logging.
        @details
        Constructs a dictionary representing a fully decoded CANopen frame and appends it
        to the internal list of processed frames. Each stored frame includes timestamp,
        COB-ID, frame type, Object Dictionary indices, and decoded payload.
        A debug log entry is also generated with formatted frame details.
        @param cob      The CANopen COB-ID of the frame.
        @param ftype    The frame type as an instance of @ref defs.frame_type.
        @param index    The CANopen Object Dictionary index associated with the frame.
        @param sub      The Object Dictionary subindex.
        @param name     Human-readable parameter name resolved via the EDS file.
        @param raw      Raw frame data represented as a hexadecimal or byte string.
        @param decoded  Decoded frame payload in human-readable form.
        """

        frame = {
            "time": sniffer_defs.now_str(),
            "cob": cob,
            "type": ftype,
            "index": index,
            "sub": sub,
            "name": name,
            "raw": raw,
            "decoded": decoded
        }

        self.log.debug(
            "Processed frame: [%s] [%s] [0x%03X] [0x%04X] [0x%02X] [%s] [%s] [%s]",
            sniffer_defs.now_str(), ftype.name, cob, index, sub, name, raw, decoded
        )

        # push frame to queue
        self.processed_frame.put(frame)

    def save_frame_to_csv(self, cob: int, ftype: sniffer_defs.frame_type, index: int, sub: int, name: str, raw: str, decoded: str):
        """! Save a processed frame row to the processed CSV file.
        @details
        Writes a CSV row with serial number, timestamp, frame classification,
        OD address, name, raw hex payload and decoded value. Periodically flushes
        and `fsyncs` the file according to `defs.FSYNC_EVERY`.
        @param cob COB-ID of the frame.
        @param ftype defs.frame_type enumeration value describing frame class.
        @param index Object dictionary index (for SDO/decoded frames).
        @param sub Object dictionary subindex.
        @param name Human-readable name for the mapped OD entry (from EDS).
        @param raw Hex string of the raw payload.
        @param decoded Human-readable decoded payload (or empty string).
        """
        if not self.export_writer:
            return
        try:
            self.export_writer.writerow([
                self.export_serial_number,
                sniffer_defs.now_str(),
                ftype.name if isinstance(ftype, sniffer_defs.frame_type) else str(ftype),
                f"0x{cob:03X}",
                f"0x{index:04X}",
                f"0x{sub:02X}",
                name,
                raw,
                decoded
            ])
            self.export_serial_number += 1
            try:
                self.export_file.flush()
                if (self.export_serial_number % sniffer_defs.FSYNC_EVERY) == 0:
                    os.fsync(self.export_file.fileno())
            except Exception:
                pass
        except Exception as e:
            self.log.error("CSV export failed: %s", e)

    def save_processed_frame(self, cob: int, ftype: sniffer_defs.frame_type, index: int, sub: int, name: str, raw: str, decoded: str):
        """! Save a fully processed CANopen frame in memory and export it to CSV.
        @details
        Converts the raw and decoded payloads into hexadecimal string representations if necessary,
        then delegates the storage of the processed frame to @ref save_frame and its CSV export
        to @ref save_frame_to_csv.
        This function ensures consistent formatting for both in-memory data and CSV output.
        @param cob      The CANopen COB-ID of the processed frame.
        @param ftype    The frame type as an instance of @ref defs.frame_type.
        @param index    The Object Dictionary index associated with the frame.
        @param sub      The Object Dictionary subindex.
        @param name     Human-readable parameter name resolved from the EDS map.
        @param raw      Raw frame data in bytes or string format.
        @param decoded  Decoded frame payload, which may be a string or byte sequence.
        """

        # Render decoded possibly already a string — only hex raw bytes
        raw_hex = sniffer_defs.bytes_to_hex(raw)
        decoded_hex = decoded if isinstance(decoded, str) else sniffer_defs.bytes_to_hex(decoded)

        # Save frame for downstream use
        self.save_frame(cob, ftype, index, sub, name, raw_hex, decoded_hex)

        # Export to CSV
        self.save_frame_to_csv(cob, ftype, index, sub, name, raw_hex, decoded_hex)

    def run(self):
        """! Main processing loop.
        @details
        Non-blocking, interruptable loop that pulls frame dicts from `raw_frame`,
        classifies frames, updates `self.stats`, resolves names via `self.eds_map`,
        decodes simple SDO payloads (expedited/data), exports CSV rows (if enabled),
        and logs the processed frame details. Ensures resources are closed on exit.
        """
        self.log.info("Processor thread started")
        get_timeout = 0.1

        try:
            while not self._stop_event.is_set():
                try:
                    frame = self.raw_frame.get(timeout=get_timeout)
                except queue.Empty:
                    continue

                # Extract fields (defensive)
                cob = frame.get("cob")
                error = frame.get("error")
                raw = frame.get("raw")

                # top talkers
                try:
                    self.stats.count_talker(cob)
                except Exception:
                    self.log.warning("count_talker failed for cob=%s", cob)

                # nodes seen (extract node id)
                try:
                    node_id = cob & 0x7F
                    if 1 <= node_id <= 127:
                        self.stats.add_node(node_id)
                except Exception:
                    pass

                # frame distribution (use enums, not names)
                ftype = sniffer_defs.frame_type.UNKNOWN
                try:
                    if cob == 0x000:
                        ftype = sniffer_defs.frame_type.NMT
                        self.stats.increment_frame(sniffer_defs.frame_type.NMT)
                    elif cob == 0x080:
                        ftype = sniffer_defs.frame_type.SYNC
                        self.stats.increment_frame(sniffer_defs.frame_type.SYNC)
                    elif 0x080 <= cob <= 0x0FF:
                        ftype = sniffer_defs.frame_type.EMCY
                        self.stats.increment_frame(sniffer_defs.frame_type.EMCY)
                    elif 0x100 <= cob <= 0x17F:
                        ftype = sniffer_defs.frame_type.TIME
                        self.stats.increment_frame(sniffer_defs.frame_type.TIME)
                    elif 0x180 <= cob <= 0x4FF:
                        ftype = sniffer_defs.frame_type.PDO
                        self.stats.increment_frame(sniffer_defs.frame_type.PDO)
                    elif 0x580 <= cob <= 0x5FF:
                        ftype = sniffer_defs.frame_type.SDO_RES
                        self.stats.increment_frame(sniffer_defs.frame_type.SDO_RES)
                    elif 0x600 <= cob <= 0x67F:
                        ftype = sniffer_defs.frame_type.SDO_REQ
                        self.stats.increment_frame(sniffer_defs.frame_type.SDO_REQ)
                    elif 0x700 <= cob <= 0x7FF:
                        ftype = sniffer_defs.frame_type.HB
                        self.stats.increment_frame(sniffer_defs.frame_type.HB)
                    else:
                        ftype = sniffer_defs.frame_type.UNKNOWN
                        self.stats.increment_frame(sniffer_defs.frame_type.UNKNOWN)
                except Exception:
                    self.log.warning("Error while classifying frame cob=%s", cob)

                # detect error frames (python-can: is_error_frame)
                if error:
                    try:
                        self.stats._stats.error.last_time = sniffer_defs.now_str()
                        self.stats._stats.error.last_frame = raw
                    except Exception:
                        pass
                    self.log.warning("Error frame detected: %s", raw)

                # SDO request (client->server)
                if ftype == sniffer_defs.frame_type.SDO_REQ and raw and len(raw) >= 4:
                    try:
                        index = raw[2] << 8 | raw[1]
                        sub = raw[3]
                        self.stats.update_sdo_request_time(index, sub)
                        name = self.eds_map.name_map.get((index, sub), f"0x{index:04X}:{sub}")

                        # Save the frame
                        self.save_processed_frame(cob, ftype, index, sub, name, raw, decoded="")
                    except Exception:
                        self.log.warning("Malformed SDO request frame while recording req time")

                # SDO response (server->client)
                elif ftype == sniffer_defs.frame_type.SDO_RES:
                    if raw and len(raw) >= 4:
                        index = raw[2] << 8 | raw[1]
                        sub = raw[3]
                    else:
                        index, sub = 0, 0

                    # detect abort
                    if raw and raw[0] == 0x80:
                        self.stats.increment_sdo_abort()
                        decoded = "ABORT"
                        payload_len = 0
                    else:
                        # assume expedited/data in bytes 4+
                        self.stats.increment_sdo_success()
                        payload = raw[4:] if raw and len(raw) > 4 else b""
                        payload_len = len(payload)
                        if payload_len:
                            val = int.from_bytes(payload, "little")
                            decoded = str(val)
                        else:
                            decoded = ""

                    try:
                        self.stats.increment_payload(sniffer_defs.frame_type.SDO_RES, payload_len)
                    except Exception:
                        pass

                    # update response latency if request recorded
                    self.stats.update_sdo_response_time(index, sub)

                    # Get name from EDS map
                    name = self.eds_map.name_map.get((index, sub), f"0x{index:04X}:{sub}")

                    # Save the frame
                    self.save_processed_frame(cob, ftype, index, sub, name, raw, decoded)

                # PDO frame
                elif ftype == sniffer_defs.frame_type.PDO:
                    payload_len = len(raw)
                    self.stats.increment_payload(sniffer_defs.frame_type.PDO, payload_len)
                    if cob in self.eds_map.pdo_map:
                        entries = self.eds_map.pdo_map[cob]
                        offset = 0
                        for (index, sub, size) in entries:
                            size_bytes = max(1, size // 8)
                            chunk = raw[offset:offset + size_bytes]
                            offset += size_bytes
                            try:
                                if size_bytes == 4:
                                    decoded = struct.unpack("<f", chunk)[0]
                                else:
                                    decoded = int.from_bytes(chunk, "little") if chunk else 0
                            except Exception:
                                decoded = int.from_bytes(chunk, "little") if chunk else 0

                            name = self.eds_map.name_map.get((index, sub), f"0x{index:04X}:{sub}")

                            # Save the frame
                            self.save_processed_frame(cob, ftype, index, sub, name, raw, decoded)
                    else:
                        decoded = "No reference in EDS"
                        # Save the frame
                        self.save_processed_frame(cob, ftype, index=0, sub=0, name="", raw=raw, decoded=decoded)

                # TIME frame
                elif ftype == sniffer_defs.frame_type.TIME:
                    # CiA-301 TIME: 4 bytes = ms after midnight (LE), 2 bytes = days since 1984-01-01 (LE)
                    try:
                        if raw and len(raw) >= 6:
                            ms = int.from_bytes(raw[0:4], "little")
                            days = int.from_bytes(raw[4:6], "little")

                            # compute time-of-day safely (wrap ms into 24 h)
                            tod_ms = ms % 86_400_000
                            hours = tod_ms // 3_600_000
                            minutes = (tod_ms % 3_600_000) // 60_000
                            seconds = (tod_ms % 60_000) // 1000
                            ms_rem = tod_ms % 1000
                            tod = f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms_rem:03d}"

                            # convert days since 1984-01-01 → date (with sanity check)
                            base = datetime(1984, 1, 1, tzinfo=UTC)
                            derived_date = (base + timedelta(days=days)).date()

                            current_year = datetime.now(UTC).year
                            if 1990 <= derived_date.year <= current_year + 1:
                                date_str = derived_date.isoformat()
                            else:
                                date_str = f"{derived_date.isoformat()} (likely-invalid)"

                            decoded = f"[{date_str} {tod}], Days={days}"
                        else:
                            decoded = "Malformed (need ≥ 6 bytes)"
                    except Exception as e:
                        decoded = f"Decode error ({e})"

                    # Save processed frame
                    self.save_processed_frame(cob, ftype, index=0, sub=0, name="TIME", raw=raw, decoded=decoded)

                # Emergency (EMCY) frame — generic decoding (no vendor-specific interpretation)
                elif ftype == sniffer_defs.frame_type.EMCY:
                    # EMCY format (generic): bytes 0..1 = 16-bit error code (LE),
                    # byte 2 = error register (bitfield), bytes 3..7 = manufacturer-specific bytes (raw hex)
                    try:
                        if raw and len(raw) >= 3:
                            # 0..1 = 16-bit error code (little-endian)
                            error_code = int.from_bytes(raw[0:2], "little")
                            # byte 2 = error register (bitfield)
                            error_reg = raw[2]
                            # bytes 3..7 = up to 5 bytes manufacturer-specific
                            manuf_bytes = raw[3:8] if len(raw) > 3 else b""

                            # error register as 8-bit binary string (MSB..LSB)
                            err_bits = f"{error_reg:08b}"

                            # manufact bytes -> printable ASCII (replace non-printable with '.'),
                            # strip trailing NULs for neatness
                            def bytes_to_printable(b: bytes) -> str:
                                if not b:
                                    return ""
                                s = "".join((chr(x) if 32 <= x <= 126 else ".") for x in b)
                                # strip trailing dots that came from NULs (0x00)
                                s = s.rstrip(".")
                                return s

                            manuf_ascii = bytes_to_printable(manuf_bytes)

                            # final compact output: hex error code, binary error register, manuf ASCII
                            decoded = f"[0x{error_code:04X}], reg=0x{error_reg:02X}[{err_bits}], manuf={manuf_ascii}"
                        else:
                            decoded = "Malformed (need >=3 bytes)"
                    except Exception as e:
                        decoded = f"Decode error ({e})"

                    self.save_processed_frame(cob, ftype, index=0, sub=0, name="EMCY", raw=raw, decoded=decoded)

                # Heartbeat (HB) frame
                elif ftype == sniffer_defs.frame_type.HB:
                    # Heartbeat: single status byte. COB-ID = 0x700 + nodeID
                    try:
                        if raw and len(raw) >= 1:
                            state = raw[0]
                            state_map = {
                                0x00: "Bootup",
                                0x04: "Stopped",
                                0x05: "Operational",
                                0x7F: "Pre-operational",
                            }
                            node = cob & 0x7F
                            decoded = f"Node={node}, state=0x{state:02X} [{state_map.get(state, 'Unknown')}]"
                        else:
                            decoded = "Malformed (need >=1 byte)"
                    except Exception as e:
                        decoded = f"Decode error ({e})"

                    self.save_processed_frame(cob, ftype, index=0, sub=0, name="HB", raw=raw, decoded=decoded)

                # Other frames type
                else:
                    self.save_processed_frame(cob, ftype, index=0, sub=0, name="", raw=raw, decoded="")

                # optionally mark task done if using task tracking
                try:
                    self.raw_frame.task_done()
                except Exception:
                    pass

        finally:
            if self.export_file:
                try:
                    try:
                        self.export_file.flush()
                        os.fsync(self.export_file.fileno())
                    except Exception:
                        pass
                    self.export_file.close()
                    self.log.info("Processed CSV export file closed")
                except Exception:
                    self.log.exception("Failed to close processed CSV file")
            self.log.info("Processor thread exiting")

    def stop(self):
        """! Request the processor thread to stop.
        @details
        Signals the internal stop event so the processing loop exits at the
        next opportunity. This method does not block waiting for thread exit;
        call `join()` on the thread object if synchronous shutdown is required.
        """
        self._stop_event.set()
        self.log.debug("Stop requested for processor thread")


def main():
    """! Main entry point for the CANopen bus sniffer application.
    @details
    This function initializes the CANopen sniffer and frame processor threads,
    handles command-line arguments, and ensures a graceful shutdown on exit
    or when the user presses Ctrl+C. It supports both CLI and GUI modes, and
    optionally enables CSV export and detailed logging.

    The main steps include:
      - Parsing command-line arguments.
      - Initializing the EDS parser and CANopen statistics.
      - Creating and launching sniffer and processor threads.
      - Handling SIGINT/SIGTSTP for controlled shutdown.
      - Joining and cleaning up threads and CAN resources before exit.
    """

    ## Command-line argument parser setup.
    p = argparse.ArgumentParser()
    p.add_argument("--interface", default=sniffer_defs.DEFAULT_INTERFACE, help="CAN interface (default: {defs.DEFAULT_INTERFACE})")
    p.add_argument("--mode", default="cli", choices=["cli", "tui", "gui"], help="enable cli or gui mode (default: cli)")
    p.add_argument("--bitrate", type=int, default=sniffer_defs.DEFAULT_CAN_BIT_RATE, help="CAN bitrate (default: {defs.DEFAULT_CAN_BIT_RATE})")
    p.add_argument("--eds", help="EDS file path (optional)")
    p.add_argument("--fixed", action="store_true", help="update rows instead of scrolling")
    p.add_argument("--export", action="store_true", help="export received frames to CSV")
    p.add_argument("--log", action="store_true", help="enable logging")
    args = p.parse_args()

    ## Enable logging if requested.
    if args.log:
        sniffer_defs.enable_logging()

    ## Parse and load EDS mapping for object dictionary and PDOs.
    eds_map = eds_parser(args.eds)

    sniffer_defs.log.debug(f"Decoded PDO map: {eds_map.pdo_map}")
    sniffer_defs.log.debug(f"Decoded NAME map: {eds_map.name_map}")

    ## Check if user passed the desired bitrate else use default.
    if args.bitrate:
        bitrate = args.bitrate
    else:
        bitrate = sniffer_defs.DEFAULT_CAN_BIT_RATE

    sniffer_defs.log.info(f"Configured CAN bitrate : {bitrate}")

    ## Initialize bus statistics and reset counters.
    stats = bus_stats(bitrate=bitrate)
    stats.reset()

    ## Shared queue for communication between sniffer and processor threads.
    raw_frame = queue.Queue()

    # Shared queue for processed frames
    processed_frame = queue.Queue()

    ## Create CAN sniffer thread for raw CAN frame capture.
    sniffer = can_sniffer(interface=args.interface,
                          raw_frame=raw_frame,
                          export=args.export)

    ## Create frame processor thread for classification and stats update.
    processor = process_frame(stats=stats,
                              raw_frame=raw_frame,
                              processed_frame=processed_frame,
                              eds_map=eds_map,
                              export=args.export)

    ## Start background threads.
    sniffer.start()
    processor.start()

    # create chosen display thread
    display = None
    if args.mode == "cli":
        display = display_cli(stats=stats,
                             processed_frame=processed_frame,
                             fixed=args.fixed)
    elif args.mode == "tui":
        try:
            sniffer_defs.log.info("Loading TUI interface")
            display_tui.run_textual(stats, processed_frame, fixed=args.fixed)
        except Exception as e:
            sniffer_defs.log.exception("Failed to start Textual TUI: %s", e)
            # fallback to legacy CLI thread if textual unavailable
            display = display_cli(stats=stats, processed_frame=processed_frame, fixed=args.fixed)
    elif args.mode == "gui":
        display = display_gui(processed_frame=processed_frame)

    if display:
        display.start()

    ## Signal handler for graceful termination (Ctrl+C).
    def _stop_all(signum, frame):
        sniffer_defs.log.warning("Signal %s received — stopping threads...", signum)
        sniffer.stop(shutdown_bus=True)
        processor.stop()
        if display:
            display.stop()

    ## Register signal handlers.
    signal.signal(signal.SIGINT, _stop_all)   # Ctrl+C → graceful stop
    # Optionally map Ctrl+Z to graceful stop instead of suspend:
    signal.signal(signal.SIGTSTP, _stop_all)  # Ctrl+Z → graceful stop
    # or ignore suspend to avoid accidental backgrounding:
    # signal.signal(signal.SIGTSTP, signal.SIG_IGN)  # Ignore Ctrl+Z to prevent backgrounding

    try:
        ## Keep the main thread alive until both threads have exited.
        while True:
            time.sleep(1)
            # If both threads stopped, break
            if not sniffer.is_alive() and not processor.is_alive():
                break
    except KeyboardInterrupt:
        ## Fallback KeyboardInterrupt handler to stop all threads.
        sniffer_defs.log.info("KeyboardInterrupt received — shutting down")
        sniffer.stop(shutdown_bus=True)
        processor.stop()
    finally:
        ## Ensure both threads terminate and join gracefully.
        sniffer.join(timeout=2.0)
        processor.join(timeout=2.0)
        if display:
            display.join(timeout=2.0)

        ## Attempt final CAN bus shutdown if still open.
        try:
            if getattr(sniffer, "bus", None) is not None:
                sniffer.bus.shutdown()
        except Exception:
            pass
        sniffer_defs.log.info(f"Terminating {sniffer_defs.APP_NAME}...")

        # Shutdown logging now that threads have been joined\n"
        try:
            logging.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()