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
@file bus_stats.py
@brief Thread-safe CANopen bus statistics aggregation and rate computation.
@details
This module implements the @ref bus_stats class, which is responsible for
collecting, aggregating, and exposing real-time statistics about CANopen
traffic observed on the bus.

It tracks frame distributions, node activity, payload sizes, SDO timing,
error frames, and rolling rate histories used by CLI, TUI, and GUI displays.

### Responsibilities
- Maintain cumulative frame counters by CANopen message type
- Track active nodes and top talkers
- Compute rolling frames-per-second rates
- Estimate bus utilization and idle percentage
- Track SDO request/response success, aborts, and latency
- Provide immutable snapshot views for display backends

### Design Notes
- All state updates are protected internally to allow safe concurrent access.
- Consumers should only interact with statistics via snapshot objects.
- No CAN or protocol parsing logic exists in this module.

### Threading Model
The statistics engine is **thread-safe** and designed to be updated from
background worker threads while being read concurrently by UI threads.

### Error Handling
Invalid updates are ignored gracefully; statistics collection never raises
fatal exceptions during runtime.
"""

import copy
import time
import logging

from dataclasses import dataclass, field
from collections import Counter, deque

import threading

import analyzer_defs as analyzer_defs

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

        ## Dictionary storing count per @ref defs.frame_type.
        ## @details
        ## Keys correspond to message types (e.g., NMT, PDO, SDO, etc.).
        ## Values represent how many frames of each type have been received.
        counts : dict = field(default_factory=lambda: dict.fromkeys(analyzer_defs.frame_type, 0))


    @dataclass
    class payload_size:
        """! Tracks cumulative payload sizes for key CANopen message types.
        @details
        Used to compute data throughput for PDO and SDO messages.
        """

        ## Dictionary holding total payload size per frame type.
        ## @details
        ## Initialized for PDO and SDO response messages.
        sizes: dict = field(default_factory=lambda: {analyzer_defs.frame_type.PDO: 0, analyzer_defs.frame_type.SDO_RES: 0})


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
        response_time: deque = field(default_factory=lambda: deque(maxlen=analyzer_defs.STATS_GRAPH_WIDTH * 5))


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

        # Rolling histories (dict of deques) — init empty here; bus_stats.__init__ will populate using defs.STATS_GRAPH_WIDTH
        history: dict = field(default_factory=dict)

        # Latest numeric rates (dict) — init empty here; bus_stats.__init__ will populate
        latest: dict = field(default_factory=dict)

        ## Human-readable bus state ("Active" or "Idle")
        bus_state: str = "Idle"

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

        ## Last-seen timestamp per node (used for inactivity detection).
        node_last_seen: dict = field(default_factory=dict)

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


    def __init__(self, bitrate: int = analyzer_defs.DEFAULT_CAN_BIT_RATE):
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
        self._stats.rates.history = {k: deque(maxlen=analyzer_defs.STATS_GRAPH_WIDTH) for k in keys}

        ## Logger instance used for reporting and debugging.
        self.log = logging.getLogger(f"{analyzer_defs.APP_NAME}.{self.__class__.__name__}")

        # Timer for computing bus stats
        self._rate_interval = 1.0                # seconds, sampling period
        self._rate_sampler_stop = threading.Event()
        self._rate_sampler_thread = threading.Thread(target=self._rate_sampler, name="bus_stats-rate-sampler", daemon=True)
        self._rate_sampler_thread.start()

    # --------- Update helpers ---------
    def increment_frame(self, ftype: analyzer_defs.frame_type):
        """! Increment frame counters by FrameType.
        @param ftype Frame type @ref defs.frame_type for incrementing its count.
        @return None.
        """

        with self._lock:
            self._stats.frame_count.total += 1
            self._stats.frame_count.counts[ftype] += 1

    def increment_payload(self, ftype: analyzer_defs.frame_type, size: int):
        """! Increment payload size counters for PDO/SDO frames
        @param ftype Frame type @ref defs.frame_type to increment it's size.
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
        analyzer_defs.log.debug(f"SDO request idx=0x{index:04X} sub={sub} recorded for latency measurement")

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
        """! Add or refresh a communicating node.
        @param node_id Received Node id as integer.
        """

        now = time.time()
        with self._lock:
            self._stats.nodes.add(node_id)
            self._stats.node_last_seen[node_id] = now

    def count_talker(self, cob_id: int):
        """! Increment TopTalkers counter for a COB-ID.
        @param cob_id COB-ID as integer of top talker to be incremented.
        """

        with self._lock:
            self._stats.top_talkers[cob_id] += 1

    # --------- Getters ---------
    def get_frame_count(self, ftype: analyzer_defs.frame_type) -> int:
        """! Get counted frames.
        @param ftype Frame type @ref defs.frame_type.
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

            # Prune inactive nodes
            now_ts = now
            inactive = []

            for node_id, last_seen in self._stats.node_last_seen.items():
                if (now_ts - last_seen) > analyzer_defs.NODE_INACTIVE_TIMEOUT:
                    inactive.append(node_id)

            for node_id in inactive:
                self._stats.node_last_seen.pop(node_id, None)
                self._stats.nodes.discard(node_id)

            # Update bus state based on active nodes
            if self._stats.nodes:
                self._stats.rates.bus_state = "Active"
            else:
                self._stats.rates.bus_state = "Idle"

            # collect current cumulative counts into a dict keyed same as rates.keys
            counts = {}
            counts['total'] = self._stats.frame_count.total
            counts['hb'] = self._stats.frame_count.counts.get(analyzer_defs.frame_type.HB, 0)
            counts['emcy'] = self._stats.frame_count.counts.get(analyzer_defs.frame_type.EMCY, 0)
            counts['pdo'] = self._stats.frame_count.counts.get(analyzer_defs.frame_type.PDO, 0)
            counts['sdo_res'] = self._stats.frame_count.counts.get(analyzer_defs.frame_type.SDO_RES, 0)
            counts['sdo_req'] = self._stats.frame_count.counts.get(analyzer_defs.frame_type.SDO_REQ, 0)

            # compute deltas and rates in a loop
            keys = self._stats.rates.keys
            # ensure history dict exists
            if not getattr(self._stats.rates, "history", None):
                width = getattr(self, "_rate_history_width", analyzer_defs.STATS_GRAPH_WIDTH)
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
                    rh[k] = deque(maxlen=analyzer_defs.STATS_GRAPH_WIDTH)
                rh[k].append(rate)

                # update last count
                self._stats.rates.last_frame_counts[k] = cur

            # maintain peak explicitly
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
                pdo_payload = self._stats.payload_size.sizes.get(analyzer_defs.frame_type.PDO, 0)
                sdo_payload = self._stats.payload_size.sizes.get(analyzer_defs.frame_type.SDO_RES, 0) + self._stats.payload_size.sizes.get(analyzer_defs.frame_type.SDO_REQ, 0)

                # If payload_size stores cumulative bytes per type, compute average payload bytes per frame
                avg_payload_bytes = (pdo_payload + sdo_payload) / total_cnt if total_cnt else 0.0

                # rough estimate of bits on bus per frame: overhead + payload
                avg_frame_bits = max(64, int(avg_payload_bytes * 8 + 64))

                # use the most recent total frames/s rate
                rate_total = float(self._stats.rates.latest.get("total", 0.0))

                # Reset bus utilization for idle bus
                if not self._stats.nodes:
                    # No active nodes → bus is idle
                    self._stats.rates.bus_util_percent = 0.0
                else:
                    util = (rate_total * avg_frame_bits) / max(1, bitrate) * 100.0
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
            self._stats.rates.history = {k: deque(maxlen=analyzer_defs.STATS_GRAPH_WIDTH) for k in keys}
            self._stats.rates.peak_fps = 0.0

            # Reset utilization and timestamps
            self._stats.rates.bus_util_percent = 0.0
            self._stats.rates.last_update_time = time.time()

            # Log reset completion
            self.log.info("Bus statistics and rate histories have been reset.")

    def _rate_sampler(self):
        """! Background thread: periodically call update_rates() so rates get sampled even when no frames arrive."""

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
        """! Stop background threads cleanly (call on application exit)."""

        try:
            self._rate_sampler_stop.set()
            if self._rate_sampler_thread and self._rate_sampler_thread.is_alive():
                self._rate_sampler_thread.join(timeout=1.0)
        except Exception:
            pass

