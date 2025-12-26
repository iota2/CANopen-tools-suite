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
@file display_cli.py
@brief Rich-based CLI display backend for the CANopen Analyzer.
@details
This module provides a non-interactive, terminal-based display backend using
the Rich library. It renders live protocol tables, PDO/SDO views, and bus
statistics in a continuously updating dashboard.

### Responsibilities
- Consume processed CANopen frames from a shared queue
- Render protocol, PDO, and SDO tables
- Display live bus statistics and rolling graphs
- Support both scrolling and fixed-row display modes

### Design Notes
- Intended for headless operation or SSH usage.
- Does not perform any protocol decoding or rate calculations.
- All statistics are read from immutable snapshots provided by @ref bus_stats.

### Threading Model
Runs as a dedicated daemon thread, independently of sniffer and processor
threads.

### Error Handling
Display rendering errors are handled gracefully to avoid terminating the UI.
"""

import time
import logging

from collections import deque

import threading
import queue

from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.text import Text
from rich import box

from bus_stats import bus_stats
import analyzer_defs as analyzer_defs

class display_cli(threading.Thread):
    """! Rich-based CLI display thread that consumes processed_frame queue and renders
    Protocol, PDO, SDO tables plus Bus Stats in a live layout.
    @note
    This reads all rate/utility information from bus_stats snapshot. It does not perform any local
    rate calculation or use bitrate directly.
    """

    def __init__(self, stats: bus_stats, processed_frame: queue.Queue, fixed: bool = False):
        """! Initialize CLI based CANopen display.
        @details
        This thread initializes and launches the CLI application that renders
        all live CANopen monitoring tables (Protocol, PDO, SDO, Bus Stats). It sets
        up shared state used by the UI update loop, including statistics, frame
        queues, and the fixed/scrolling display mode.
        @param stats The shared stats object providing real-time bus statistics and rate histories.
        @param processed_frame Queue delivering processed CANopen frames from the background sniffer thread.
        @param fixed When True, tables operate in fixed-index mode; otherwise they show scrolling entries.
        @return None
        """

        super().__init__(daemon=True)

        ## Private instance for pointing to incoming processed frames.
        self.processed_frame = processed_frame

        ## Private instance for pointing to incoming bus stats.
        self.stats = stats

        ## Private instance for pointing to incoming flag whether to keep display in fixed mode or not.
        self.fixed = fixed

        ## Rich console instance for display.
        self.console = Console()

        ## Logger instance for CLI display.
        self.log = logging.getLogger(f"{analyzer_defs.APP_NAME}.{self.__class__.__name__}")

        self._stop_event = threading.Event()

        ## Protocol data buffer used only for rendering rows (not for rate calc).
        self.proto_frames = deque(maxlen=analyzer_defs.MAX_FRAMES)

        ## PDO data buffer used only for rendering rows (not for rate calc).
        self.pdo_frames = deque(maxlen=analyzer_defs.MAX_FRAMES)

        ## SDO data buffer used only for rendering rows (not for rate calc).
        self.sdo_frames = deque(maxlen=analyzer_defs.MAX_FRAMES)

        ## Protocol data dict keys -> rows mapping for fixed mode
        self.fixed_proto = {}

        ## PDO data dict keys -> rows mapping for fixed mode
        self.fixed_pdo = {}

        ## SDO data dict keys -> rows mapping for fixed mode
        self.fixed_sdo = {}

    def sparkline(self, history, style="white"):
        """! Create a compact sparkline Text from a numeric history sequence."""

        if not history:
            return ""
        # ensure we operate on a plain list of floats
        try:
            seq = list(history)[-analyzer_defs.STATS_GRAPH_WIDTH:]
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
        """! Build a Bus Stats table by querying latest stats snapshot (bus_stats owns all calculations)."""

        snapshot = self.stats.get_snapshot()

        metric_labels = [
            "State", "Active Nodes", "PDO Frames/s", "SDO Frames/s",
            "HB Frames/s", "EMCY Frames/s", "Total Frames/s", "Peak Frames/s",
            "Bus Util %", "Bus Idle %", "SDO OK/Abort",
            "SDO resp time", "Last Error Frame", "Top Talkers", "Frame Dist."
        ]
        # Max label length + padding
        metric_col_width = max(len(label) for label in metric_labels) + 2
        graph_col_width = analyzer_defs.STATS_GRAPH_WIDTH

        # Build table: Metric & Graph fixed width, Value expands
        t = Table(title="Bus Stats", expand=True, box=box.SQUARE, style="yellow")
        t.add_column("Metric", no_wrap=True, width=metric_col_width)
        t.add_column("Value", justify="right", ratio=1)  # fill remaining width
        t.add_column("Graph", justify="left", width=graph_col_width)

        # Basic fields
        total_frames = getattr(snapshot.frame_count, "total", 0)
        nodes = getattr(snapshot, "nodes", {}) or {}
        # Bus state (authoritative, from bus_stats)
        bus_state = getattr(snapshot.rates, "bus_state", "Idle")
        t.add_row("State", bus_state, "")
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
            top = snapshot.top_talkers.most_common(analyzer_defs.MAX_STATS_SHOW)
            top_str = ", ".join(f"0x{c:03X}:{cnt}" for c, cnt in top) if top else "-"
            t.add_row("Top Talkers", top_str, "")
        except Exception:
            t.add_row("Top Talkers", "-", "")

        # Frame distribution — show top-N kinds sorted by count (descending)
        try:
            counts = snapshot.frame_count.counts  # mapping: defs.frame_type -> int
            # build list of (name, count) and sort by count desc
            items = sorted(((k.name, v) for k, v in counts.items()), key=lambda kv: kv[1], reverse=True)
            # choose how many to show inline
            shown = items[:analyzer_defs.MAX_STATS_SHOW]
            dist_pairs = ", ".join(f"{name}:{cnt}" for name, cnt in shown)
            if not dist_pairs:
                dist_pairs = "-"
        except Exception:
            dist_pairs = "-"
        t.add_row("Frame Dist.", dist_pairs, "")

        return t

    def render_tables(self):
        """! Render tables for displaying CLI data."""

        # Protocol Data
        t_proto = Table(title="Protocol Data", expand=True, box=box.SQUARE, style="cyan")
        t_proto.add_column("Time", no_wrap=True)
        t_proto.add_column("COB-ID", width=8)
        t_proto.add_column("Type", width=12)
        t_proto.add_column("Raw Data", no_wrap=True)
        t_proto.add_column("Decoded")
        t_proto.add_column("Count", width=6, justify="right")

        protos = list(self.fixed_proto.values())[-analyzer_defs.PROTOCOL_TABLE_HEIGHT:] if self.fixed else list(self.proto_frames)[-analyzer_defs.PROTOCOL_TABLE_HEIGHT:]
        while len(protos) < analyzer_defs.PROTOCOL_TABLE_HEIGHT:
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

        frames = list(self.fixed_pdo.values())[-analyzer_defs.DATA_TABLE_HEIGHT:] if self.fixed else list(self.pdo_frames)[-analyzer_defs.DATA_TABLE_HEIGHT:]
        while len(frames) < analyzer_defs.DATA_TABLE_HEIGHT:
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

        sdos = list(self.fixed_sdo.values())[-analyzer_defs.DATA_TABLE_HEIGHT:] if self.fixed else list(self.sdo_frames)[-analyzer_defs.DATA_TABLE_HEIGHT:]
        while len(sdos) < analyzer_defs.DATA_TABLE_HEIGHT:
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
        """! Run CLI based CANopen display."""

        self.log.info("display_cli started")
        # Use Live to update the complete dashboard
        with Live(console=self.console, refresh_per_second=5, screen=True) as live:
            try:
                # loop until stop requested
                while not self._stop_event.is_set():
                    # consume all available processed frames (non-blocking)
                    try:
                        while True:
                            pframe = self.processed_frame.get_nowait()
                            # pframe fields: time, cob (int), type (defs.frame_type), index, sub, name, raw, decoded
                            t = pframe.get("time", analyzer_defs.now_str())
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
                            type_name = ftype.name if isinstance(ftype, analyzer_defs.frame_type) else str(ftype)
                            if ftype == analyzer_defs.frame_type.PDO:
                                key = (cob, idx, sub)
                                row = {"time": t, "cob": cob_s, "name": name, "index": idx_s, "sub": sub_s, "raw": raw, "decoded": decoded, "count": 1}
                                if self.fixed:
                                    prev = self.fixed_pdo.get(key)
                                    if prev:
                                        row["count"] = prev.get("count", 1) + 1
                                    self.fixed_pdo[key] = row
                                else:
                                    self.pdo_frames.append(row)
                            elif ftype in (analyzer_defs.frame_type.SDO_REQ, analyzer_defs.frame_type.SDO_RES):
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
                self.log.info("display_cli exiting")

    def stop(self):
        """! Stop CLI display."""

        self._stop_event.set()
        self.log.debug("display_cli stop requested")

