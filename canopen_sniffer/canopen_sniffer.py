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
@brief CANopen bus sniffer.
@details
This module defines default settings, constants, and enumerations used by
the CANopen bus sniffer application. It also sets up logging for the module.
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

from enum import Enum, auto
from datetime import datetime, timedelta, UTC
from dataclasses import dataclass, field
from collections import Counter, deque

import threading
import queue

from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.text import Text
from rich import box

import can
from can import exceptions as can_exceptions
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

## Default CAN bus bit rate (in bits per second).
DEFAULT_CAN_BIT_RATE = 1000000

## Script file name used as a reference for other system-generated files.
FILENAME = os.path.splitext(os.path.basename(__file__))[0]

## Frequency of filesystem synchronization (every N rows).
## @details
## Setting this to 1 performs fsync after every row, which is safer but slower.
FSYNC_EVERY = 50

## Default Logging level.
## @details
## Set default log level to INFO.
LOG_LEVEL = logging.DEBUG

# --------------------------------------------------------------------------
# ----- Constants -----
# --------------------------------------------------------------------------

## Height of the data table in the CLI interface (number of rows).
CLI_DATA_TABLE_HEIGHT = 30

## Height of the protocol table in the CLI interface (number of rows).
CLI_PROTOCOL_TABLE_HEIGHT = 15

## Width of the graphs in the CLI interface (in characters).
CLI_GRAPH_WIDTH = 20

## Maximum number of values to be shown in Bus stats window.
MAX_STATS_SHOW = 5

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
## @brief Logger instance
## @details
## Default behavior: No console or file logs until explicitly enabled.
root_logger = logging.getLogger()
# Remove any inherited handlers to keep console quiet
for h in root_logger.handlers[:]:
    root_logger.removeHandler(h)
root_logger.setLevel(LOG_LEVEL)

## @brief Module-level convenience logger (will propagate to root_logger handler).
## @details
## Create logger instance.
log = logging.getLogger(f"{FILENAME}")
log.addHandler(logging.NullHandler())

def enable_logging():
    """! Enable file-only logging, enabled through argument."""
    filename = f"{FILENAME}.log"

    # Remove existing handlers (console) and configure file handler only.
    logging.basicConfig(
        filename=filename,
        format="%(asctime)s [%(levelname)-8s] [%(name)-15s] %(message)s",
        filemode="w",           # overwrite instead of append
        level=LOG_LEVEL,
        force=True,             # overwrite any existing handlers
    )

    # Do NOT add a StreamHandler here — we want file-only logging when enabled through argument.
    global log
    log = logging.getLogger(f"{FILENAME}")
    log.setLevel(LOG_LEVEL)
    log.info(f"Logging enabled → {filename}")


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
    class rates_stats:
        """! Records frame rate statistics (PDO, SDO, total, etc.)
        @details
        Tracks last update time and last frame counts for rate computations.
        """
        # List of rate keys we track. Keep this small & canonical.
        keys: list = field(default_factory=lambda: ['total', 'hb', 'emcy', 'pdo', 'sdo_res', 'sdo_req'])

        # Bus utilization (percent)
        bus_util_percent: float = 0.0

        # Peak frames per seconds
        peak_fps: float = 0.0

        # Last update timestamp
        last_update_time: float = field(default_factory=time.time)

        # Last observed cumulative frame counts for each key (will be populated in bus_stats.__init__)
        last_frame_counts: dict = field(default_factory=dict)

        # Rolling histories (dict of deques) — init empty here; bus_stats.__init__ will populate using CLI_GRAPH_WIDTH
        history: dict = field(default_factory=dict)

        # Latest numeric rates (dict) — init empty here; bus_stats.__init__ will populate
        latest: dict = field(default_factory=dict)


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

        ## Reference to @ref bus_stats::sdo_stats data structure.
        rates: "bus_stats.rates_stats" = field(default_factory=lambda: bus_stats.rates_stats())

        ## Reference to @ref bus_stats::error_stats data structure.
        error: "bus_stats.error_stats" = field(default_factory=lambda: bus_stats.error_stats())


    def __init__(self, bitrate: int = DEFAULT_CAN_BIT_RATE):
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

        ## CAN communication bit rate
        self.bitrate = bitrate

        # Reset rates status
        self._stats.rates.bus_util_percent = 0.0
        self._stats.rates.peak_fps = 0.0

        # Use canonical keys from the rates_stats dataclass
        keys = self._stats.rates.keys

        # Initialize rate dictionaries using dict.fromkeys()
        self._stats.rates.last_frame_counts = dict.fromkeys(keys, 0)
        self._stats.rates.latest = dict.fromkeys(keys, 0.0)

        # Initialize rolling histories (must use comprehension — new deque per key)
        self._stats.rates.history = {k: deque(maxlen=CLI_GRAPH_WIDTH) for k in keys}

        ## Logger instance used for reporting and debugging.
        self.log = logging.getLogger(self.__class__.__name__)

        # Timer for computing bus stats
        self._rate_interval = 1.0                # seconds, sampling period
        self._rate_sampler_stop = threading.Event()
        self._rate_sampler_thread = threading.Thread(target=self._rate_sampler, name="bus_stats-rate-sampler", daemon=True)
        self._rate_sampler_thread.start()

    # --------- Update helpers ---------
    def increment_frame(self, ftype: frame_type):
        """! Increment frame counters by FrameType.
        @param ftype Frame type @ref frame_type for incrementing its count.
        @return None.
        """
        with self._lock:
            self._stats.frame_count.total += 1
            self._stats.frame_count.counts[ftype] += 1
        # Update derived rates/history (time-gated inside update_rates)
        try:
            self.update_rates()
        except Exception:
            pass

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

    def update_rates(self, now: float = None, interval: float = 1.0):
        """! Compute frames/s rates by differencing cumulative counters.
        This method is time-gated (default ~1s) and appends values into the
        rolling rate_history stored inside the snapshot. It also updates
        snapshot.rates (latest numbers) and snapshot.bus_util_percent.
        Call this regularly or invoke after incrementing counters; it's safe
        to call frequently because it checks elapsed time internally.
        """
        if now is None:
            now = time.time()

        with self._lock:
            elapsed = now - getattr(self._stats.rates, "last_update_time", now)
            if elapsed <= 0 or elapsed < (interval * 0.9):
                return

            # collect current cumulative counts into a dict keyed same as rates.keys
            counts = {}
            counts['total'] = self._stats.frame_count.total
            counts['hb'] = self._stats.frame_count.counts.get(frame_type.HB, 0)
            counts['emcy'] = self._stats.frame_count.counts.get(frame_type.EMCY, 0)
            counts['pdo'] = self._stats.frame_count.counts.get(frame_type.PDO, 0)
            counts['sdo_res'] = self._stats.frame_count.counts.get(frame_type.SDO_RES, 0)
            counts['sdo_req'] = self._stats.frame_count.counts.get(frame_type.SDO_REQ, 0)

            # compute deltas and rates in a loop
            keys = self._stats.rates.keys
            # ensure history dict exists
            if not getattr(self._stats.rates, "history", None):
                width = getattr(self, "_rate_history_width", CLI_GRAPH_WIDTH)
                self._stats.rates.history = {k: deque(maxlen=width) for k in keys}

            rh = self._stats.rates.history  # should be dict of deques
            for k in keys:
                last = self._stats.rates.last_frame_counts.get(k, 0)
                cur = counts.get(k, 0)
                delta = cur - last
                rate = (delta / elapsed) if elapsed > 0 else 0.0

                # store latest and append to history (in-place)
                self._stats.rates.latest[k] = rate
                # append to history deque
                if k not in rh:
                    rh[k] = deque(maxlen=CLI_GRAPH_WIDTH)
                rh[k].append(rate)

                # update last count
                self._stats.rates.last_frame_counts[k] = cur

            # maintain peak explicitly
            # if "peak" not in rh:
            #     rh["peak"] = deque(maxlen=CLI_GRAPH_WIDTH)
            # rh["peak"].append(max(rh["peak"][-1] if rh["peak"] else 0.0, self._stats.rates.latest.get("total", 0.0)))
            # update peak_fps (single float) as max(prev_peak, max(history['total']) or latest total)
            try:
                prev_peak = float(getattr(self._stats.rates, "peak_fps", 0.0))
                # prefer max of history if available (reflects observed peaks over the kept window)
                if 'total' in rh and len(rh['total']) > 0:
                    hist_max = max(rh['total'])
                    new_peak = max(prev_peak, hist_max)
                else:
                    # fallback to latest total rate
                    cur_total_rate = float(self._stats.rates.latest.get('total', 0.0))
                    new_peak = max(prev_peak, cur_total_rate)
                self._stats.rates.peak_fps = new_peak
            except Exception:
                # be defensive: don't break rates update if peak logic fails
                try:
                    self._stats.rates.peak_fps = max(float(getattr(self._stats.rates, "peak_fps", 0.0)),
                                                    float(self._stats.rates.latest.get('total', 0.0)))
                except Exception:
                    pass

            # update timestamp
            self._stats.rates.last_update_time = now

            # compute bus util using stored external bitrate or default
            try:
                bitrate = self.bitrate

                # total frames observed in this snapshot (avoid zero)
                total_cnt = max(1, counts.get("total", 0))

                # derive avg payload bytes using stored payload_size totals (best-effort)
                pdo_payload = self._stats.payload_size.sizes.get(frame_type.PDO, 0)
                sdo_payload = self._stats.payload_size.sizes.get(frame_type.SDO_RES, 0) + self._stats.payload_size.sizes.get(frame_type.SDO_REQ, 0)

                # If payload_size stores cumulative bytes per type, compute average payload bytes per frame
                avg_payload_bytes = (pdo_payload + sdo_payload) / total_cnt if total_cnt else 0.0

                # rough estimate of bits on bus per frame: overhead + payload
                avg_frame_bits = max(64, int(avg_payload_bytes * 8 + 64))

                # use the most recent total frames/s rate
                rate_total = float(self._stats.rates.latest.get("total", 0.0))

                # compute utilization percentage
                util = (rate_total * avg_frame_bits) / max(1, bitrate) * 100.0

                # store in snapshot rates
                self._stats.rates.bus_util_percent = util
            except Exception:
                self._stats.rates.bus_util_percent = 0.0

    def get_snapshot(self) -> stats_data:
        """! Get snapshot of bus stats.
        @return Current bus stats @ref stats_data.
        """
        with self._lock:
            snap = copy.copy(self._stats)
            snap.rates = copy.copy(self._stats.rates)
            # convert each deque in history to a list
            snap.rates.history = {k: list(d) for k, d in self._stats.rates.history.items()}
            snap.rates.latest = dict(self._stats.rates.latest)
        return snap

    def reset(self):
        """! Reset bus stats count."""
        with self._lock:
            # Reset all core stats objects
            self._stats.frame_count = self.frame_count()
            self._stats.payload_size = self.payload_size()
            self._stats.sdo = self.sdo_stats()
            self._stats.error = self.error_stats()
            self._stats.top_talkers.clear()
            self._stats.nodes.clear()

            # Reinitialize the rates tracking structure
            self._stats.rates = self.rates_stats()

            # Use the canonical keys from rates_stats
            keys = self._stats.rates.keys

            # Reset all counters and rate data structures
            self._stats.rates.last_frame_counts = dict.fromkeys(keys, 0)
            self._stats.rates.latest = dict.fromkeys(keys, 0.0)
            self._stats.rates.history = {k: deque(maxlen=CLI_GRAPH_WIDTH) for k in keys}
            self._stats.rates.peak_fps = 0.0

            # Reset utilization and timestamps
            self._stats.rates.bus_util_percent = 0.0
            self._stats.rates.last_update_time = time.time()

            # Log reset completion
            self.log.info("Bus statistics and rate histories have been reset.")

    def _rate_sampler(self):
        """Background thread: periodically call update_rates() so rates get sampled even when no frames arrive."""
        self.log.debug("Rate sampler thread started (interval=%.3fs)", getattr(self, "_rate_interval", 1.0))
        # Use a monotonic clock for sleeping
        interval = getattr(self, "_rate_interval", 1.0)
        while not self._rate_sampler_stop.is_set():
            start = time.time()
            try:
                # pass explicit now and interval so update_rates uses correct elapsed
                self.update_rates(now=start, interval=interval)
            except Exception:
                # keep the sampler alive even if update_rates throws
                self.log.exception("Exception while sampling rates")
            # sleep accurately but wake early if stop requested
            elapsed = time.time() - start
            to_sleep = max(0.0, interval - elapsed)
            # wait with timeout so stop can be responsive
            self._rate_sampler_stop.wait(timeout=to_sleep)
        self.log.debug("Rate sampler thread exiting")

    def stop(self):
        """Stop background threads cleanly (call on application exit)."""
        try:
            self._rate_sampler_stop.set()
            if self._rate_sampler_thread and self._rate_sampler_thread.is_alive():
                self._rate_sampler_thread.join(timeout=1.0)
        except Exception:
            pass


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

        # Export to CSV
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

    def save_frame(self, cob: int, ftype: frame_type, index: int, sub: int, name: str, raw: str, decoded: str):
        """! Save a processed CANopen frame for downstream use or logging.
        @details
        Constructs a dictionary representing a fully decoded CANopen frame and appends it
        to the internal list of processed frames. Each stored frame includes timestamp,
        COB-ID, frame type, Object Dictionary indices, and decoded payload.
        A debug log entry is also generated with formatted frame details.
        @param cob      The CANopen COB-ID of the frame.
        @param ftype    The frame type as an instance of @ref frame_type.
        @param index    The CANopen Object Dictionary index associated with the frame.
        @param sub      The Object Dictionary subindex.
        @param name     Human-readable parameter name resolved via the EDS file.
        @param raw      Raw frame data represented as a hexadecimal or byte string.
        @param decoded  Decoded frame payload in human-readable form.
        """

        frame = {
            "time": now_str(),
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
            now_str(), ftype.name, cob, index, sub, name, raw, decoded
        )

        # push frame to queue
        self.processed_frame.put(frame)

    def save_frame_to_csv(self, cob: int, ftype: frame_type, index: int, sub: int, name: str, raw: str, decoded: str):
        """! Save a processed frame row to the processed CSV file.
        @details
        Writes a CSV row with serial number, timestamp, frame classification,
        OD address, name, raw hex payload and decoded value. Periodically flushes
        and `fsyncs` the file according to `FSYNC_EVERY`.
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
        except Exception as e:
            self.log.error("CSV export failed: %s", e)

    def save_processed_frame(self, cob: int, ftype: frame_type, index: int, sub: int, name: str, raw: str, decoded: str):
        """! Save a fully processed CANopen frame in memory and export it to CSV.
        @details
        Converts the raw and decoded payloads into hexadecimal string representations if necessary,
        then delegates the storage of the processed frame to @ref save_frame and its CSV export
        to @ref save_frame_to_csv.
        This function ensures consistent formatting for both in-memory data and CSV output.
        @param cob      The CANopen COB-ID of the processed frame.
        @param ftype    The frame type as an instance of @ref frame_type.
        @param index    The Object Dictionary index associated with the frame.
        @param sub      The Object Dictionary subindex.
        @param name     Human-readable parameter name resolved from the EDS map.
        @param raw      Raw frame data in bytes or string format.
        @param decoded  Decoded frame payload, which may be a string or byte sequence.
        """

        # Render decoded possibly already a string — only hex raw bytes
        raw_hex = bytes_to_hex(raw)
        decoded_hex = decoded if isinstance(decoded, str) else bytes_to_hex(decoded)

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

                # SDO request (client->server)
                if ftype == frame_type.SDO_REQ and raw and len(raw) >= 4:
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

                    # Get name from EDS map
                    name = self.eds_map.name_map.get((index, sub), f"0x{index:04X}:{sub}")

                    # Save the frame
                    self.save_processed_frame(cob, ftype, index, sub, name, raw, decoded)

                # PDO frame
                elif ftype == frame_type.PDO:
                    payload_len = len(raw)
                    self.stats.increment_payload(frame_type.PDO, payload_len)
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
                elif ftype == frame_type.TIME:
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
                elif ftype == frame_type.EMCY:
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
                elif ftype == frame_type.HB:
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


# -----------------------------
# Display thread implementations
# -----------------------------
class DisplayCLI(threading.Thread):
    """
    Rich-based CLI display thread that consumes processed_frame queue and renders
    Protocol, PDO, SDO tables plus Bus Stats in a live layout.

    NOTE: This DisplayCLI reads all rate/utility information from bus_stats snapshot
    (snapshot.rates.latest and snapshot.rates.history). It does not perform any local
    rate calculation or use bitrate directly.
    """

    def __init__(self, stats: bus_stats, processed_frame: queue.Queue, fixed: bool = False):
        super().__init__(daemon=True)
        self.processed_frame = processed_frame
        self.stats = stats
        self.fixed = fixed

        # rich console / live
        self.console = Console()
        self._stop_event = threading.Event()
        self.log = logging.getLogger(self.__class__.__name__)

        # internal storage for rendering
        self.PROTOCOL_TABLE_HEIGHT = CLI_PROTOCOL_TABLE_HEIGHT
        self.DATA_TABLE_HEIGHT = CLI_DATA_TABLE_HEIGHT
        self.GRAPH_WIDTH = CLI_GRAPH_WIDTH

        # Small per-display buffers used only for rendering rows (not for rate calc).
        self.proto_frames = deque(maxlen=MAX_FRAMES)
        self.pdo_frames = deque(maxlen=MAX_FRAMES)
        self.sdo_frames = deque(maxlen=MAX_FRAMES)

        self.fixed_proto = {}
        self.fixed_pdo = {}
        self.fixed_sdo = {}

    def sparkline(self, history, style="white"):
        """Create a compact sparkline Text from a numeric history sequence."""
        if not history:
            return ""
        # ensure we operate on a plain list of floats
        try:
            seq = list(history)[-self.GRAPH_WIDTH:]
            if not seq:
                return ""
            blocks = "▁▂▃▄▅▆▇█"
            mn, mx = min(seq), max(seq)
            span = mx - mn or 1.0
            chars = []
            for v in seq:
                try:
                    idx = int((float(v) - mn) / span * (len(blocks) - 1))
                except Exception:
                    idx = 0
                idx = max(0, min(idx, len(blocks) - 1))
                chars.append(blocks[idx])
            return Text("".join(chars), style=style)
        except Exception:
            return ""

    def build_bus_stats_table(self):
        """Build a Bus Stats table by querying latest stats snapshot (bus_stats owns all calculations)."""
        snapshot = self.stats.get_snapshot()

        metric_labels = [
            "State", "Active Nodes", "PDO Frames/s", "SDO Frames/s",
            "HB Frames/s", "EMCY Frames/s", "Total Frames/s", "Peak Frames/s",
            "Bus Util %", "Bus Idle %", "SDO OK/Abort",
            "SDO resp time", "Last Error Frame", "Top Talkers", "Frame Dist."
        ]
        # Max label length + padding
        metric_col_width = max(len(label) for label in metric_labels) + 2
        graph_col_width = self.GRAPH_WIDTH

        # Build table: Metric & Graph fixed width, Value expands
        t = Table(title="Bus Stats", expand=True, box=box.SQUARE, style="yellow")
        t.add_column("Metric", no_wrap=True, width=metric_col_width)
        t.add_column("Value", justify="right", ratio=1)  # fill remaining width
        t.add_column("Graph", justify="left", width=graph_col_width)

        # Basic fields
        total_frames = getattr(snapshot.frame_count, "total", 0)
        nodes = getattr(snapshot, "nodes", {}) or {}
        t.add_row("State", "Active" if total_frames else "Idle", "")
        t.add_row("Active Nodes", str(len(nodes)), f"[dim]{sorted(nodes)}[/]" if nodes else "")

        # Read rates and histories from snapshot.rates (structure provided by bus_stats)
        rates_latest = getattr(snapshot.rates, "latest", {}) if hasattr(snapshot, "rates") else {}
        rates_hist = getattr(snapshot.rates, "history", {}) if hasattr(snapshot, "rates") else {}

        # PDO
        pdo_val = float(rates_latest.get("pdo", 0.0)) if isinstance(rates_latest, dict) else 0.0
        pdo_hist = rates_hist.get("pdo", []) if isinstance(rates_hist, dict) else []
        t.add_row("PDO Frames/s", f"{pdo_val:.1f}", self.sparkline(pdo_hist, "green") if pdo_hist else "")

        # SDO (request + response)
        sdo_res = float(rates_latest.get("sdo_res", 0.0)) if isinstance(rates_latest, dict) else 0.0
        sdo_req = float(rates_latest.get("sdo_req", 0.0)) if isinstance(rates_latest, dict) else 0.0
        sdo_val = sdo_res + sdo_req
        # build combined history (elementwise sum when lengths match)
        sdo_hist_res = rates_hist.get("sdo_res", []) if isinstance(rates_hist, dict) else []
        sdo_hist_req = rates_hist.get("sdo_req", []) if isinstance(rates_hist, dict) else []
        sdo_hist = []
        try:
            if sdo_hist_res and sdo_hist_req and len(sdo_hist_res) == len(sdo_hist_req):
                sdo_hist = [a + b for a, b in zip(sdo_hist_res, sdo_hist_req)]
            elif sdo_hist_res:
                sdo_hist = list(sdo_hist_res)
            elif sdo_hist_req:
                sdo_hist = list(sdo_hist_req)
        except Exception:
            sdo_hist = list(sdo_hist_res) if sdo_hist_res else list(sdo_hist_req) if sdo_hist_req else []
        t.add_row("SDO Frames/s", f"{sdo_val:.1f}", self.sparkline(sdo_hist, "magenta") if sdo_hist else "")

        # Heart beat
        pdo_val = float(rates_latest.get("hb", 0.0)) if isinstance(rates_latest, dict) else 0.0
        pdo_hist = rates_hist.get("hb", []) if isinstance(rates_hist, dict) else []
        t.add_row("HB Frames/s", f"{pdo_val:.1f}", self.sparkline(pdo_hist, "cyan") if pdo_hist else "")

        # Emergency Messages
        pdo_val = float(rates_latest.get("emcy", 0.0)) if isinstance(rates_latest, dict) else 0.0
        pdo_hist = rates_hist.get("emcy", []) if isinstance(rates_hist, dict) else []
        t.add_row("EMCY Frames/s", f"{pdo_val:.1f}", self.sparkline(pdo_hist, "cyan") if pdo_hist else "")

        # Total frames/s
        total_val = float(rates_latest.get("total", 0.0)) if isinstance(rates_latest, dict) else 0.0
        total_hist = rates_hist.get("total", []) if isinstance(rates_hist, dict) else []
        t.add_row("Total Frames/s", f"{total_val:.1f}", self.sparkline(total_hist, "yellow") if total_hist else "")

        # Peak frames/s
        peak_val = float(getattr(snapshot.rates, "peak_fps", 0.0))
        t.add_row("Peak Frames/s", f"{peak_val:.1f}", "")

        # Bus utilization (computed by bus_stats)
        util = None
        if hasattr(snapshot, "rates") and hasattr(snapshot.rates, "bus_util_percent"):
            util = snapshot.rates.bus_util_percent
        elif hasattr(snapshot, "compute_bus_util"):
            try:
                util = snapshot.compute_bus_util()
            except Exception:
                util = None

        idle = max(0.0, 100.0 - util) if util is not None else 0.0
        util_hist = rates_hist.get("total", []) if isinstance(rates_hist, dict) else []
        t.add_row("Bus Util %", f"{util:.2f}%" if util is not None else "-", self.sparkline(util_hist, "grey") if util_hist else "")
        t.add_row("Bus Idle %", f"{idle:.2f}%" if util is not None else "-", "")

        # SDO stats & response time
        try:
            t.add_row("SDO OK/Abort", f"{snapshot.sdo.success}/{snapshot.sdo.abort}", "")
            avg_sdo_rt = (sum(snapshot.sdo.response_time) / len(snapshot.sdo.response_time)) if snapshot.sdo.response_time else 0.0
            t.add_row("SDO resp time", f"{avg_sdo_rt * 1000:.1f} ms", "")
        except Exception:
            t.add_row("SDO OK/Abort", "-", "")
            t.add_row("SDO resp time", "-", "")

        # Last error frame
        last_err = "-"
        try:
            if snapshot.error.last_time or snapshot.error.last_frame:
                last_err = f"[{snapshot.error.last_time}] <{snapshot.error.last_frame}>"
        except Exception:
            last_err = "-"
        t.add_row("Last Error Frame", last_err, "")

        # Top talkers
        try:
            top = snapshot.top_talkers.most_common(MAX_STATS_SHOW)
            top_str = ", ".join(f"0x{c:03X}:{cnt}" for c, cnt in top) if top else "-"
            t.add_row("Top Talkers", top_str, "")
        except Exception:
            t.add_row("Top Talkers", "-", "")

        # Frame distribution — show top-N kinds sorted by count (descending)
        try:
            counts = snapshot.frame_count.counts  # mapping: frame_type -> int
            # build list of (name, count) and sort by count desc
            items = sorted(((k.name, v) for k, v in counts.items()), key=lambda kv: kv[1], reverse=True)
            # choose how many to show inline
            shown = items[:MAX_STATS_SHOW]
            dist_pairs = ", ".join(f"{name}:{cnt}" for name, cnt in shown)
            if not dist_pairs:
                dist_pairs = "-"
        except Exception:
            dist_pairs = "-"
        t.add_row("Frame Dist.", dist_pairs, "")

        return t

    def render_tables(self):
        # Protocol Data
        t_proto = Table(title="Protocol Data", expand=True, box=box.SQUARE, style="cyan")
        t_proto.add_column("Time", no_wrap=True)
        t_proto.add_column("COB-ID", width=8)
        t_proto.add_column("Type", width=12)
        t_proto.add_column("Raw Data", no_wrap=True)
        t_proto.add_column("Decoded")
        t_proto.add_column("Count", width=6, justify="right")

        protos = list(self.fixed_proto.values())[-self.PROTOCOL_TABLE_HEIGHT:] if self.fixed else list(self.proto_frames)[-self.PROTOCOL_TABLE_HEIGHT:]
        while len(protos) < self.PROTOCOL_TABLE_HEIGHT:
            protos.append({"time": "", "cob": "", "type": "", "raw": "", "decoded": "", "count": ""})
        for p in protos:
            t_proto.add_row(p["time"], p["cob"], p["type"], p["raw"], p["decoded"], str(p.get("count", "")))

        # Bus Stats
        t_bus = self.build_bus_stats_table()

        # PDO table
        t_pdo = Table(title="PDO Data", expand=True, box=box.SQUARE, style="green")
        t_pdo.add_column("Time", no_wrap=True)
        t_pdo.add_column("COB-ID", width=8)
        t_pdo.add_column("Name")
        t_pdo.add_column("Index")
        t_pdo.add_column("Sub")
        t_pdo.add_column("Raw Data", no_wrap=True)
        t_pdo.add_column("Decoded")
        t_pdo.add_column("Count", width=6, justify="right")

        frames = list(self.fixed_pdo.values())[-self.DATA_TABLE_HEIGHT:] if self.fixed else list(self.pdo_frames)[-self.DATA_TABLE_HEIGHT:]
        while len(frames) < self.DATA_TABLE_HEIGHT:
            frames.append({"time": "", "cob": "", "name": "", "index": "", "sub": "", "raw": "", "decoded": "", "count": ""})
        for f in frames:
            decoded = Text(str(f.get("decoded", "")), style="bold green") if f.get("decoded") else ""
            t_pdo.add_row(f["time"], f["cob"], f.get("name", ""), f.get("index", ""), f.get("sub", ""), f.get("raw", ""), decoded, str(f.get("count", "")))

        # SDO table
        t_sdo = Table(title="SDO Data", expand=True, box=box.SQUARE, style="magenta")
        t_sdo.add_column("Time", no_wrap=True)
        t_sdo.add_column("COB-ID", width=8)
        t_sdo.add_column("Name")
        t_sdo.add_column("Index")
        t_sdo.add_column("Sub")
        t_sdo.add_column("Raw Data", no_wrap=True)
        t_sdo.add_column("Decoded")
        t_sdo.add_column("Count", width=6, justify="right")

        sdos = list(self.fixed_sdo.values())[-self.DATA_TABLE_HEIGHT:] if self.fixed else list(self.sdo_frames)[-self.DATA_TABLE_HEIGHT:]
        while len(sdos) < self.DATA_TABLE_HEIGHT:
            sdos.append({"time": "", "cob": "", "name": "", "index": "", "sub": "", "raw": "", "decoded": "", "count": ""})
        for s in sdos:
            decoded = Text(str(s.get("decoded", "")), style="bold magenta") if s.get("decoded") else ""
            t_sdo.add_row(s["time"], s["cob"], s.get("name", ""), s.get("index", ""), s.get("sub", ""), s.get("raw", ""), decoded, str(s.get("count", "")))

        # Grid layout (two columns)
        layout = Table.grid(expand=True)
        layout.add_row(t_proto, None, t_bus)
        layout.add_row(t_pdo, None, t_sdo)
        return layout

    def run(self):
        self.log.info("DisplayCLI (rich) started")
        # Use Live to update the complete dashboard
        with Live(console=self.console, refresh_per_second=5, screen=True) as live:
            try:
                # loop until stop requested
                while not self._stop_event.is_set():
                    # consume all available processed frames (non-blocking)
                    try:
                        while True:
                            pframe = self.processed_frame.get_nowait()
                            # pframe fields: time, cob (int), type (frame_type), index, sub, name, raw, decoded
                            t = pframe.get("time", now_str())
                            cob = pframe.get("cob", 0)
                            ftype = pframe.get("type")
                            idx = pframe.get("index", 0)
                            sub = pframe.get("sub", 0)
                            name = pframe.get("name", "")
                            raw = pframe.get("raw", "")
                            decoded = pframe.get("decoded", "")

                            # Format cob/index/sub as hex strings for display
                            cob_s = f"0x{cob:03X}" if isinstance(cob, int) else str(cob)
                            idx_s = f"0x{idx:04X}" if isinstance(idx, int) else str(idx)
                            sub_s = f"0x{sub:02X}" if isinstance(sub, int) else str(sub)

                            # classify into proto/pdo/sdo by type
                            type_name = ftype.name if isinstance(ftype, frame_type) else str(ftype)
                            if ftype == frame_type.PDO:
                                key = (cob, idx, sub)
                                row = {"time": t, "cob": cob_s, "name": name, "index": idx_s, "sub": sub_s, "raw": raw, "decoded": decoded, "count": 1}
                                if self.fixed:
                                    prev = self.fixed_pdo.get(key)
                                    if prev:
                                        row["count"] = prev.get("count", 1) + 1
                                    self.fixed_pdo[key] = row
                                else:
                                    self.pdo_frames.append(row)
                            elif ftype in (frame_type.SDO_REQ, frame_type.SDO_RES):
                                key = (cob, idx, sub)
                                row = {"time": t, "cob": cob_s, "name": name, "index": idx_s, "sub": sub_s, "raw": raw, "decoded": decoded, "count": 1}
                                if self.fixed:
                                    prev = self.fixed_sdo.get(key)
                                    if prev:
                                        row["count"] = prev.get("count", 1) + 1
                                    self.fixed_sdo[key] = row
                                else:
                                    self.sdo_frames.append(row)
                            else:
                                # protocol/other
                                ptype = type_name
                                row = {"time": t, "cob": cob_s, "type": ptype, "raw": raw, "decoded": decoded, "count": 1}
                                if self.fixed:
                                    key = (cob, ptype)
                                    prev = self.fixed_proto.get(key)
                                    if prev:
                                        row["count"] = prev.get("count", 1) + 1
                                    self.fixed_proto[key] = row
                                else:
                                    self.proto_frames.append(row)

                            try:
                                self.processed_frame.task_done()
                            except Exception:
                                pass
                    except queue.Empty:
                        # nothing to consume
                        pass

                    # render and push to live
                    live.update(self.render_tables())

                    # small sleep to reduce busy-loop
                    time.sleep(0.05)

            finally:
                self.log.info("DisplayCLI exiting")

    def stop(self):
        self._stop_event.set()
        self.log.debug("DisplayCLI stop requested")


class DisplayGUI(threading.Thread):
    """Placeholder GUI display thread — for now consume queue and log the frames.
       Replace this run() with actual Qt event integration later if needed.
    """

    def __init__(self, processed_frame: queue.Queue):
        super().__init__(daemon=True)
        self.processed_frame = processed_frame
        self._stop_event = threading.Event()
        self.log = logging.getLogger(self.__class__.__name__)

    def run(self):
        self.log.info("DisplayGUI started (placeholder)")
        get_timeout = 0.1
        try:
            while not self._stop_event.is_set():
                try:
                    pframe = self.processed_frame.get(timeout=get_timeout)
                except queue.Empty:
                    continue

                # Inside DisplayGUI.run(), where you currently do:
                self.log.info("GUI frame: type=%s cob=0x%03X name=%s raw=%s decoded=%s", ...)

                # Replace with:
                msg = (f"GUI frame: type={(pframe.get('type').name if isinstance(pframe.get('type'), frame_type) else str(pframe.get('type')))} "
                    f"cob=0x{pframe.get('cob'):03X} name={pframe.get('name')} raw={pframe.get('raw')} decoded={pframe.get('decoded')}")
                # print(msg)              # immediate console feedback
                self.log.info(msg)     # still log to logger (file/handlers)

                try:
                    self.processed_frame.task_done()
                except Exception:
                    pass

        finally:
            self.log.info("DisplayGUI exiting")

    def stop(self):
        self._stop_event.set()
        self.log.debug("DisplayGUI stop requested")


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
    p.add_argument("--bitrate", type=int, default=DEFAULT_CAN_BIT_RATE, help="CAN bitrate (default: {DEFAULT_CAN_BIT_RATE})")
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

    ## Check if user passed the desired bitrate else use default.
    if args.bitrate:
        bitrate = args.bitrate
    else:
        bitrate = DEFAULT_CAN_BIT_RATE

    log.info(f"Configured CAN bitrate : {bitrate}")

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

    # create chosen display thread
    if args.mode == "cli":
        display = DisplayCLI(stats=stats,
                             processed_frame=processed_frame,
                             fixed=args.fixed)
    else:
        display = DisplayGUI(processed_frame=processed_frame)

    ## Start background threads.
    sniffer.start()
    processor.start()
    display.start()

    ## Signal handler for graceful termination (Ctrl+C).
    def _stop_all(signum, frame):
        log.warning("Signal %s received — stopping threads...", signum)
        sniffer.stop(shutdown_bus=True)
        processor.stop()
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
        log.info("KeyboardInterrupt received — shutting down")
        sniffer.stop(shutdown_bus=True)
        processor.stop()
    finally:
        ## Ensure both threads terminate and join gracefully.
        sniffer.join(timeout=2.0)
        processor.join(timeout=2.0)
        display.join(timeout=2.0)

        ## Attempt final CAN bus shutdown if still open.
        try:
            if getattr(sniffer, "bus", None) is not None:
                sniffer.bus.shutdown()
        except Exception:
            pass
        log.info(f"Terminating {APP_NAME}...")

        # Shutdown logging now that threads have been joined\n"
        try:
            logging.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()

