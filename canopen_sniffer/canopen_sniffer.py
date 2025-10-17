"""!
@file canopen_sniffer.py
@brief CANopen bus sniffer.
@details
This module defines default settings, constants, and enumerations used by
the CANopen bus sniffer application. It also sets up logging for the module.
"""

import os
import re
import csv
import time
import logging
import argparse
import configparser
import signal

from enum import Enum, auto
from datetime import datetime
from dataclasses import dataclass, field
from collections import Counter, deque

import threading
import queue

import can
import canopen


# --------------------------------------------------------------------------
# ----- Definitions -----
# --------------------------------------------------------------------------

## Application organization name.
APP_ORG = "iota2"

## Application name.
APP_NAME = "CANopen-Sniffer"

# --------------------------------------------------------------------------
# ----- Defaults -----
# --------------------------------------------------------------------------

## Default CAN interface to be loaded.
DEFAULT_INTERFACE = "vcan0"

## Default CAN bus baud rate (in bits per second).
DEFAULT_BAUD_RATE = 1000000

## Script file name used as a reference for other system-generated files.
FILENAME = os.path.splitext(os.path.basename(__file__))[0]

## Frequency of filesystem synchronization (every N rows).
## @details
## Setting this to 1 performs fsync after every row, which is safer but slower.
FSYNC_EVERY = 50


# --------------------------------------------------------------------------
# ----- Constants -----
# --------------------------------------------------------------------------

## Height of the data table in the CLI interface (number of rows).
CLI_DATA_TABLE_HEIGHT = 30

## Height of the protocol table in the CLI interface (number of rows).
CLI_PROTOCOL_TABLE_HEIGHT = 15

## Width of the graphs in the CLI interface (in characters).
CLI_GRAPH_WIDTH = 20

## Maximum number of CANopen frames to be cached.
MAX_FRAMES = 500


# --------------------------------------------------------------------------
# ----- Enumerations -----
# --------------------------------------------------------------------------
class frame_type(Enum):
    """! Types of CANopen messages.
    @details
        This enumeration defines the various message types that can appear
        on a CANopen network.
    """
    ## Emergency message.
    EMCY = 1

    ## Heartbeat message.
    HB = 2

    ## Network Management message.
    NMT = 3

    ## Process Data Object message.
    PDO = 4

    ## Service Data Object request message.
    SDO_REQ = 5

    ## Service Data Object response message.
    SDO_RES = 6

    ## CANopen synchronization message.
    SYNC = 7

    ## Timestamp message.
    TIME = 8

    ## Other or unknown message type.
    UNKNOWN = 9


# --------------------------------------------------------------------------
# ----- Logging -----
# --------------------------------------------------------------------------

## @brief Logger instance for the CANopen bus sniffer.
## @details
## If no handlers are attached by the application using this module,
## a NullHandler ensures that no warnings are emitted.
log = logging.getLogger(f"{FILENAME}")

## @brief Attaches a NullHandler to suppress warnings if no logging configuration is provided.
## @details
## This prevents "No handler found" errors when the module is imported
## without the application setting up its own logging configuration.
log.addHandler(logging.NullHandler())

def enable_logging():
    """! Enable System logging"""
    filename = f"{FILENAME}.log"
    logging.basicConfig(
        filename=filename,
        format="%(asctime)s [%(levelname)-8s] [%(name)-15s] %(message)s",
        filemode="w",            # overwrite instead of append
        level=logging.DEBUG,
        force=True               # overwrite any existing handlers
    )
    global log
    log = logging.getLogger(f"{FILENAME}")
    log.setLevel(logging.DEBUG)
    log.info(f"Logging enabled → {filename}")
    # Optionally also log to console:
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    log.addHandler(console)


# ----- helpers -----
def now_str() -> str:
    """! Return current time string.
    @return Time string.
    """
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

def bytes_to_hex(data) -> str:
    """! Convert bytes or bytearray to a space-separated hex string safely.
    @param data Byte stream.
    @return Converted string.
    """
    if data is None:
        return ""
    # if already a string, return it as-is
    if isinstance(data, str):
        return data
    # if it’s not iterable of ints (like None or empty), return empty
    try:
        return " ".join(f"{b:02X}" for b in data)
    except Exception:
        return str(data)


def clean_int_with_comment(val: str) -> int:
    """! Get value after splitting the string.
    @param val Input string.
    @return Splitted value as integer.
    """
    return int(val.split(";", 1)[0].strip(), 0)


class bus_stats:
    """! Container class for all CANopen bus statistics.
    @details
    This class organizes statistical data related to CANopen bus activity.
    It includes nested dataclasses for different categories of metrics such as
    frame counts, payload sizes, SDO transaction results, and error tracking.
    """

    @dataclass
    class frame_count:
        """! Tracks the number of frames by CANopen message type.
        @details
        Maintains counters for total frames and per-message-type counts.
        """

        ## Total number of CANopen frames received.
        total = int(0)

        ## Dictionary storing count per @ref frame_type.
        ## @details
        ## Keys correspond to message types (e.g., NMT, PDO, SDO, etc.).
        ## Values represent how many frames of each type have been received.
        counts : dict = field(default_factory=lambda: dict.fromkeys(frame_type, 0))


    @dataclass
    class payload_size:
        """! Tracks cumulative payload sizes for key CANopen message types.
        @details
        Used to compute data throughput for PDO and SDO messages.
        """

        ## Dictionary holding total payload size per frame type.
        ## @details
        ## Initialized for PDO and SDO response messages.
        sizes: dict = field(default_factory=lambda: {frame_type.PDO: 0, frame_type.SDO_RES: 0})


    @dataclass
    class sdo_stats:
        """! Records SDO (Service Data Object) communication statistics.
        @details
        Includes counts of successful and aborted transfers, and timing data
        for SDO request/response cycles.
        """

        ## Number of successful SDO transactions.
        success: int = 0

        ## Number of aborted or failed SDO transactions.
        abort: int = 0

        ## Timestamp dictionary for active SDO requests.
        ## @details
        ## Used to compute response latency when corresponding replies arrive.
        request_time: dict = field(default_factory=dict)

        ## Deque storing recent SDO response times for visualization.
        ## @details
        ## The maximum length is scaled by CLI graph width for CLI graph display.
        response_time: deque = field(default_factory=lambda: deque(maxlen=CLI_GRAPH_WIDTH * 5))


    @dataclass
    class error_stats:
        """! Tracks error-related information observed on the CANopen bus.
        @details
        Maintains time and frame references for the most recent errors seen per node.
        """

        ## Dictionary mapping node IDs to timestamps of last error.
        last_time: dict = field(default_factory=dict)

        ## Dictionary mapping node IDs to the last error frame received.
        last_frame: dict = field(default_factory=dict)


    @dataclass
    class stats_data:
        """! Consolidated data record for overall bus statistics.
        @details
        This dataclass aggregates multiple categories of bus-level statistics,
        including nodes, top talkers, and various measurement substructures.
        """

        ## Timestamp marking when statistics collection started.
        start_time: float = 0.0

        ## Set of node IDs currently active on the CANopen network.
        nodes: set = field(default_factory=set)

        ## Counter tracking nodes sending the most messages.
        top_talkers: Counter = field(default_factory=Counter)

        ## Reference to @ref bus_stats::frame_count data structure.
        frame_count: "bus_stats.frame_count" = field(default_factory=lambda: bus_stats.frame_count())

        ## Reference to @ref bus_stats::payload_size data structure.
        payload_size: "bus_stats.payload_size" = field(default_factory=lambda: bus_stats.payload_size())

        ## Reference to @ref bus_stats::sdo_stats data structure.
        sdo: "bus_stats.sdo_stats" = field(default_factory=lambda: bus_stats.sdo_stats())

        ## Reference to @ref bus_stats::error_stats data structure.
        error: "bus_stats.error_stats" = field(default_factory=lambda: bus_stats.error_stats())

    def __init__(self):
        """! Initialize the bus_stats object and its internal data structures.
        @details
        This constructor initializes synchronization primitives and prepares the
        root statistics container used to store CANopen bus statistics. A thread
        lock is used to protect concurrent access to the shared `_stats` data.
        A logger instance is also created for internal diagnostics and reporting.
        """
        ## Thread lock used to protect access to statistics data.
        self._lock = threading.Lock()

        ## Instance of the @ref bus_stats::stats_data structure holding all metrics.
        self._stats = self.stats_data()

        ## Logger instance used for reporting and debugging.
        self.log = logging.getLogger(self.__class__.__name__)

    # --------- Update helpers ---------
    def increment_frame(self, ftype: frame_type):
        """! Increment frame counters by FrameType.
        @param ftype Frame type @ref frame_type for incrementing its count.
        @return None.
        """
        with self._lock:
            self._stats.frame_count.total += 1
            self._stats.frame_count.counts[ftype] += 1

    def increment_payload(self, ftype: frame_type, size: int):
        """! Increment payload size counters for PDO/SDO frames
        @param ftype Frame type @ref frame_type to increment it's size.
        @param size payload size as integer.
        @exception KeyError : Payload size not tracked.
        """
        with self._lock:
            if ftype in self._stats.payload_size.sizes:
                self._stats.payload_size.sizes[ftype] += size
            else:
                raise KeyError(f"Payload size not tracked for {ftype}")

    def set_start_time(self):
        """! Sets the start time parameter of bus stats."""
        with self._lock:
            self._stats.start_time = time.time()

    def increment_sdo_success(self):
        """! Increment the SDO success counter."""
        with self._lock:
            self._stats.sdo.success += 1

    def increment_sdo_abort(self):
        """! Increment the SDO abort counter."""
        with self._lock:
            self._stats.sdo.abort += 1

    def update_sdo_request_time(self, index: int, sub: int):
        """! Update the SDO request message time to the deque.
        @param index Index of received message as integer.
        @param sub Sub index of received message as integer.
        """
        with self._lock:
            self._stats.sdo.request_time[(index, sub)] = time.time()
        log.debug(f"SDO request idx=0x{index:04X} sub={sub} recorded for latency measurement")

    def update_sdo_response_time(self, index: int, sub: int):
        """! Update the SDO response message time from the deque.
        @param index Index of received message as integer.
        @param sub Sub index of received message as integer.
        """
        with self._lock:
            resp_time = None
            key = (index, sub)
            req_ts = self._stats.sdo.request_time.pop(key, None)
            if req_ts:
                resp_time = time.time() - req_ts
                self._stats.sdo.response_time.append(resp_time)
                self.log.debug(f"SDO response latency for 0x{index:04X}:{sub} = {resp_time:.4f}s")

    def add_node(self, node_id: int):
        """! Add node to communicating nodes list.
        @param node_id Received Node id as integer.
        """
        with self._lock:
            self._stats.nodes.add(node_id)

    def count_talker(self, cob_id: int):
        """! Increment TopTalkers counter for a COB-ID.
        @param cob_id COB-ID as integer of top talker to be incremented.
        """
        with self._lock:
            self._stats.top_talkers[cob_id] += 1

    # --------- Getters ---------
    def get_frame_count(self, ftype: frame_type) -> int:
        """! Get counted frames.
        @param ftype Frame type @ref frame_type.
        @return Count of frames received as integer.
        """
        with self._lock:
            return self._stats.frame_count.counts[ftype]

    def get_total_frames(self) -> int:
        """! Get total frame count.
        @return Total counted frames as integer.
        """
        with self._lock:
            return self._stats.frame_count.total

    def get_snapshot(self) -> stats_data:
        """! Get snapshot of bus stats.
        @return Current bus stats @ref stats_data.
        """
        with self._lock:
            return self._stats

    def reset(self):
        """! Reset bus stats count."""
        with self._lock:
            self._stats.frame_count = self.frame_count()
            self._stats.payload_size = self.payload_size()
            self._stats.sdo = self.sdo_stats()
            self._stats.error = self.error_stats()
            self._stats.top_talkers.clear()
            self._stats.nodes.clear()

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
                        raw = clean_int_with_comment(cfg[f"{sec}sub{subidx}"]["DefaultValue"])
                        index = (raw >> 16) & 0xFFFF
                        sub = (raw >> 8) & 0xFF
                        size = raw & 0xFF
                        entries.append((index, sub, size))
                        subidx += 1
                    comm_sec = sec.replace("1A", "18", 1)
                    comm_sub1 = f"{comm_sec}sub1"
                    if comm_sub1 in cfg:
                        cob_id = clean_int_with_comment(cfg[comm_sub1]["DefaultValue"])
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
        @param raw_frame Optional queue.Queue instance to push received frames to.
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
        self.export_filename = f"{FILENAME}_raw.csv"

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
                    ["S.No.", "Time", "COB-ID", "Type", "Raw"]
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
        according to `FSYNC_EVERY`.
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
                now_str(),
                f"0x{cob:03X}",
                error,
                raw
            ])
            self.export_serial_number += 1
            # flush and fsync periodically
            try:
                self.export_file.flush()
                if (self.export_serial_number % FSYNC_EVERY) == 0:
                    os.fsync(self.export_file.fileno())
            except Exception:
                pass
            self.log.debug("CSV wrote raw row #%d", self.export_serial_number - 1)
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

        self.log.debug(f"Raw frame: [{now_str()}] [0x{cob:03X}] [{error}] [{bytes_to_hex(raw)}]")
        # --- export to CSV ---
        self.save_frame_to_csv(cob, error, bytes_to_hex(raw))

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
                    # check stop_event and continue/exit
                    if self._stop_event.is_set():
                        break
                    continue
                except OSError as e:
                    self.log.warning("CAN recv OSError: %s", e)
                    # short sleep but wake on stop
                    if self._stop_event.wait(0.2):
                        break
                    continue
                except Exception:
                    self.log.exception("Unexpected error in CAN recv")
                    if self._stop_event.wait(0.2):
                        break
                    continue

                if msg:
                    try:
                        self.handle_message(msg)
                    except Exception:
                        self.log.exception("Handling received message")

        finally:
            # close CSV file safely
            if self.export_file:
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
                    self.bus.shutdown()
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
                    self.bus.shutdown()
            except Exception:
                self.log.exception("Error calling bus.shutdown() during stop()")



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

    def __init__(self, stats: bus_stats, raw_frame: queue.Queue, eds_map, export: bool = False):
        """! Initialize the processor thread.
        @details
        The constructor stores references to required helpers, initializes a
        stop event and logging, sets up CSV export if requested, and ensures
        statistics collection start time is set.
        @param stats Instance of @ref bus_stats used to record statistics.
        @param raw_frame `queue.Queue` providing raw frames (dict) from the sniffer.
        @param eds_map EDS parser / map object providing name_map lookups.
        @param export If True, enable CSV export of processed frames.
        """
        super().__init__(daemon=True)

        ## Queue from which raw frame dictionaries are consumed.
        self.raw_frame = raw_frame

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
        self.export_filename = f"{FILENAME}_processed.csv"

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

    def save_frame_to_csv(self, cob: int, ftype: frame_type, index: int, sub: int, name: str, raw: str, decoded: str):
        """! Save a processed frame row to the processed CSV file.
        @details
        Writes a CSV row with serial number, timestamp, frame classification,
        OD address, name, raw hex payload and decoded value. Periodically flushes
        and fsyncs the file according to FSYNC_EVERY.
        @param cob COB-ID of the frame.
        @param ftype frame_type enumeration value describing frame class.
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
                now_str(),
                ftype.name if isinstance(ftype, frame_type) else str(ftype),
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
                if (self.export_serial_number % FSYNC_EVERY) == 0:
                    os.fsync(self.export_file.fileno())
            except Exception:
                pass
            self.log.debug("CSV wrote processed row #%d", self.export_serial_number - 1)
        except Exception as e:
            self.log.error("CSV export failed: %s", e)


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
                ts = frame.get("time")
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
                ftype = frame_type.UNKNOWN
                try:
                    if cob == 0x000:
                        ftype = frame_type.NMT
                        self.stats.increment_frame(frame_type.NMT)
                    elif cob == 0x080:
                        ftype = frame_type.SYNC
                        self.stats.increment_frame(frame_type.SYNC)
                    elif 0x080 <= cob <= 0x0FF:
                        ftype = frame_type.EMCY
                        self.stats.increment_frame(frame_type.EMCY)
                    elif 0x100 <= cob <= 0x17F:
                        ftype = frame_type.TIME
                        self.stats.increment_frame(frame_type.TIME)
                    elif 0x180 <= cob <= 0x4FF:
                        ftype = frame_type.PDO
                        self.stats.increment_frame(frame_type.PDO)
                    elif 0x580 <= cob <= 0x5FF:
                        ftype = frame_type.SDO_RES
                        self.stats.increment_frame(frame_type.SDO_RES)
                    elif 0x600 <= cob <= 0x67F:
                        ftype = frame_type.SDO_REQ
                        self.stats.increment_frame(frame_type.SDO_REQ)
                    elif 0x700 <= cob <= 0x7FF:
                        ftype = frame_type.HB
                        self.stats.increment_frame(frame_type.HB)
                    else:
                        ftype = frame_type.UNKNOWN
                        self.stats.increment_frame(frame_type.UNKNOWN)
                except Exception:
                    self.log.warning("Error while classifying frame cob=%s", cob)

                # detect error frames (python-can: is_error_frame)
                if error:
                    try:
                        self.stats._stats.error.last_time = now_str()
                        self.stats._stats.error.last_frame = raw
                    except Exception:
                        pass
                    self.log.warning("Error frame detected: %s", raw)

                index, sub = 0, 0
                name = ""
                decoded = ""
                # SDO request (client->server)
                if ftype == frame_type.SDO_REQ and raw and len(raw) >= 4:
                    try:
                        index = raw[2] << 8 | raw[1]
                        sub = raw[3]
                        self.stats.update_sdo_request_time(index, sub)
                        name = self.eds_map.name_map.get((index, sub), f"0x{index:04X}:{sub}")
                    except Exception:
                        self.log.warning("Malformed SDO request frame while recording req time")

                # SDO response (server->client)
                elif ftype == frame_type.SDO_RES:
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
                        self.stats.increment_payload(frame_type.SDO_RES, payload_len)
                    except Exception:
                        pass

                    # update response latency if request recorded
                    self.stats.update_sdo_response_time(index, sub)

                    name = self.eds_map.name_map.get((index, sub), f"0x{index:04X}:{sub}")

                # Build processed frame for possible downstream use / logging
                processed_frame = {
                    "time": now_str(),
                    "cob": cob,
                    "index": index,
                    "sub": sub,
                    "name": name,
                    "raw": raw,
                    "decoded": decoded,
                    "type": ftype
                }

                # Render decoded possibly already a string — only hex raw bytes
                raw_hex = bytes_to_hex(raw)
                decoded_logged = decoded if isinstance(decoded, str) else bytes_to_hex(decoded)

                self.log.debug(
                    "Processed frame: [%s] [%s] [0x%03X] [0x%04X] [0x%02X] [%s] [%s] [%s]",
                    now_str(), ftype.name, cob, index, sub, name, raw_hex, decoded_logged
                )

                # --- export to CSV ---
                self.save_frame_to_csv(cob, ftype, index, sub, name, raw_hex, decoded_logged)

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
    p.add_argument("--interface", default=DEFAULT_INTERFACE, help="CAN interface (default: {DEFAULT_INTERFACE})")
    p.add_argument("--mode", default="cli", choices=["cli", "gui"], help="enable cli or gui mode (default: cli)")
    p.add_argument("--bitrate", type=int, default=DEFAULT_BAUD_RATE, help="CAN bitrate (default: {DEFAULT_BAUD_RATE})")
    p.add_argument("--eds", help="EDS file path (optional)")
    p.add_argument("--fixed", action="store_true", help="update rows instead of scrolling")
    p.add_argument("--export", action="store_true", help="export received frames to CSV")
    p.add_argument("--log", action="store_true", help="enable logging")
    args = p.parse_args()

    ## Enable logging if requested.
    if args.log:
        enable_logging()

    ## Parse and load EDS mapping for object dictionary and PDOs.
    eds_map = eds_parser(args.eds)

    log.debug(f"Decoded PDO map: {eds_map.pdo_map}")
    log.debug(f"Decoded NAME map: {eds_map.name_map}")

    ## Initialize bus statistics and reset counters.
    stats = bus_stats()
    stats.reset()

    ## Shared queue for communication between sniffer and processor threads.
    raw_frame = queue.Queue()

    ## Create CAN sniffer thread for raw CAN frame capture.
    sniffer = can_sniffer(interface=args.interface,
                          raw_frame=raw_frame,
                          export=args.export)

    ## Create frame processor thread for classification and stats update.
    processor = process_frame(stats=stats,
                              raw_frame=raw_frame,
                              eds_map=eds_map,
                              export=args.export)

    ## Start background threads.
    sniffer.start()
    processor.start()

    ## Signal handler for graceful termination (Ctrl+C).
    def _stop_all(signum, frame):
        log.warning("Signal %s received — stopping threads...", signum)
        sniffer.stop(shutdown_bus=True)
        processor.stop()

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
        log.info("KeyboardInterrupt received — shutting down")
        sniffer.stop(shutdown_bus=True)
        processor.stop()
    finally:
        ## Ensure both threads terminate and join gracefully.
        sniffer.join(timeout=2.0)
        processor.join(timeout=2.0)

        ## Attempt final CAN bus shutdown if still open.
        try:
            if getattr(sniffer, "bus", None) is not None:
                sniffer.bus.shutdown()
        except Exception:
            pass
        log.info(f"Terminating {APP_NAME}...")


if __name__ == "__main__":
    main()

