#!/usr/bin/env python3
"""
iota2 - Making Imaginations, Real
<i2.iotasquare@gmail.com>

 ██╗ ██████╗ ████████╗ █████╗ ██████╗
 ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
 ██║██║   ██║   ██║   ███████║ █████╔╝
 ██║██║   ██║   ██║   ██╔══██║██╔═══╝
 ██║╚██████╔╝   ██║   ██║  ██║███████╗
 ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝

CANopen Sniffer — CLI (Rich-based)
==================================

Features:
 - Parses PDO mappings and ParameterNames from EDS.
 - Displays PDO and SDO traffic with human-readable names.
 - Uses rich tables for live updates.
 - Optional `--log` for logging to file.
 - Optional `--fixed` mode: instead of scrolling history, updates existing rows and tracks repeat count.
 - Optional `--export` to create CSV file containing received CAN frames.
 - Bus Stats table next to Protocol Data with many metrics (rolling peak, sparklines, etc).

Usage examples:
---------------
# Run with virtual CAN and EDS file
python canopen_bus_sniffer_cli.py --interface vcan0 --eds sample_device.eds

# Enable logging to canopen_bus_sniffer_cli.log
python canopen_bus_sniffer_cli.py --chainterfacennel vcan0 --eds sample_device.eds --log

# Fixed mode (overwrite instead of scrolling)
python canopen_bus_sniffer_cli.py --interface vcan0 --eds sample_device.eds --fixed

# Set Bit rate (Used for bus loading calculations)
python canopen_bus_sniffer_cli.py --interface vcan0 --eds sample_device.eds --bitrate 1000000

# Export received CAN frames to CSV gfile
python canopen_bus_sniffer_cli.py --interface vcan0 --eds sample_device.eds --export
"""

import argparse
import configparser
import csv
import logging
import struct
import re
import time
from collections import deque, defaultdict, Counter
from datetime import datetime

import can
import canopen
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.text import Text
from rich import box

# ----- constants -----
DATA_TABLE_HEIGHT = 30
PROTOCOL_TABLE_HEIGHT = 15
GRAPH_WIDTH = 20

# ----- logging -----
log = logging.getLogger("canopen_bus_sniffer")
log.addHandler(logging.NullHandler())


def enable_logging():
    logging.basicConfig(
        filename="canopen_bus_sniffer_cli.log",
        filemode="w",
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    global log
    log = logging.getLogger("canopen_bus_sniffer")
    log.info("Logging enabled → canopen_bus_sniffer_cli.log")


# ----- helpers -----
def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def bytes_to_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def _clean_int_with_comment(val: str) -> int:
    return int(val.split(";", 1)[0].strip(), 0)


def sparkline(history, style="white"):
    """Render a mini sparkline graph from numeric history."""
    if not history:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    mn, mx = min(history), max(history)
    span = mx - mn or 1
    chars = [blocks[int((v - mn) / span * (len(blocks) - 1))] for v in history]
    return Text("".join(chars[-GRAPH_WIDTH:]), style=style)


# ----- sniffer -----
class CANopenSniffer:
    def __init__(self, interface: str, eds_path: str | None = None, bitrate: int = 1000000,
                 max_rows: int = 500, fixed: bool = False, export: bool = False):
        self.console = Console()
        self.fixed = fixed
        self.bitrate = bitrate
        self.export = export

        # frame buffers
        self.proto_frames = deque(maxlen=100)
        self.frames = deque(maxlen=max_rows)
        self.sdo_frames = deque(maxlen=max_rows)

        # fixed stores
        self.fixed_proto = {}
        self.fixed_pdo = {}
        self.fixed_sdo = {}

        # stats
        self.start_time = time.time()
        self.total_frames = 0
        self.pdo_frames_count = 0
        self.sdo_frames_count = 0
        self.other_frames_count = 0
        self.pdo_payload_bytes = 0
        self.sdo_payload_bytes = 0

        # history for frame rates
        self.last_counts = dict(pdo=0, sdo=0, total=0)
        self.last_rate_calc = time.time()
        self.pdo_rate_hist = deque(maxlen=GRAPH_WIDTH)
        self.sdo_rate_hist = deque(maxlen=GRAPH_WIDTH)
        self.tot_rate_hist = deque(maxlen=GRAPH_WIDTH)
        self.peak_rate_hist = deque(maxlen=GRAPH_WIDTH)  # rolling peak

        # extra stats
        self.nodes_seen = set()
        self.sdo_success = 0
        self.sdo_abort = 0
        self.frame_dist = defaultdict(int)
        self.last_error_time = None
        self.last_error_frame = None

        # latency tracking for SDO request/response
        self.sdo_req_time = {}  # (index,sub) -> timestamp
        self.sdo_response_times = deque(maxlen=GRAPH_WIDTH * 5)

        # top talkers
        self.top_talkers = Counter()

        # export CSV
        self.export_file = None
        self.export_writer = None
        self.export_serial = 1
        if self.export:
            try:
                self.export_file = open("canopen_bus_sniffer_cli.csv", "w", newline="")
                self.export_writer = csv.writer(self.export_file)
                self.export_writer.writerow(
                    ["S.No.", "Time", "Type", "COB-ID", "Name", "Index", "SubIndex", "Raw Data", "Decoded Data"]
                )
                log.info("CSV export enabled → canopen_bus_sniffer_cli.csv")
            except Exception as e:
                log.exception("Failed to open CSV export file: %s", e)
                self.export = False

        # open CAN socket
        try:
            self.bus = can.interface.Bus(channel=interface, interface="socketcan")
            log.info(f"CAN socket opened on {interface}")
        except Exception as e:
            log.exception("Failed to open CAN interface %s: %s", interface, e)
            raise

        # optional CANopen network
        self.network = canopen.Network()
        try:
            self.network.connect(channel=interface, interface="socketcan")
            log.info(f"Connected canopen.Network on {interface}")
        except Exception:
            log.debug("canopen.Network.connect() failed (not critical)")

        # EDS parsing
        self.eds_path = eds_path
        self.pdo_map = {}
        self.name_map = {}
        self.cob_name_overrides = {}
        if eds_path:
            try:
                self.name_map = self.build_name_map(eds_path)
                self.pdo_map = self.parse_pdo_map(eds_path)
                self.log_pdo_mapping_consistency()
                log.info(f"Loaded EDS: {eds_path} (pdo_map={len(self.pdo_map)}, names={len(self.name_map)})")
            except Exception as e:
                log.warning(f"Failed to parse EDS '{eds_path}': {e}")

    # --- EDS parsing ---
    def parse_pdo_map(self, eds_path):
        cfg = configparser.ConfigParser(strict=False)
        cfg.optionxform = str
        cfg.read(eds_path)
        pdo_map = {}
        cob_name_overrides = {}
        for sec in cfg.sections():
            if sec.upper().startswith("1A") and "SUB" not in sec.upper():
                try:
                    entries = []
                    subidx = 1
                    while f"{sec}sub{subidx}" in cfg:
                        raw = _clean_int_with_comment(cfg[f"{sec}sub{subidx}"]["DefaultValue"])
                        index = (raw >> 16) & 0xFFFF
                        sub = (raw >> 8) & 0xFF
                        size = raw & 0xFF
                        entries.append((index, sub, size))
                        subidx += 1
                    comm_sec = sec.replace("1A", "18", 1)
                    comm_sub1 = f"{comm_sec}sub1"
                    if comm_sub1 in cfg:
                        cob_id = _clean_int_with_comment(cfg[comm_sub1]["DefaultValue"])
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

    def build_name_map(self, eds_path):
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

    def log_pdo_mapping_consistency(self):
        for cob_id, entries in self.pdo_map.items():
            for (idx, sub, _) in entries:
                if (idx, sub) not in self.name_map and (idx, 0) not in self.name_map:
                    log.warning(f"COB 0x{cob_id:03X} maps to 0x{idx:04X}:{sub}, no ParameterName")

    # --- CSV export helper ---
    def export_frame(self, ftype, cob, name, index, sub, raw, decoded):
        if not self.export_writer:
            return
        try:
            self.export_writer.writerow([
                self.export_serial,
                now_str(),
                ftype,
                f"0x{cob:03X}" if cob is not None else "",
                name,
                index,
                sub,
                raw,
                decoded
            ])
            self.export_serial += 1
        except Exception as e:
            log.error("CSV export failed: %s", e)

    # --- message handling ---
    def handle_msg(self, msg: can.Message):
        """
        Central message handler. Tracks a lot of stats and now also exports to CSV:
         - total/pdo/sdo counts
         - frame distribution
         - top talkers
         - sdo request timestamps (for latency)
         - last error frame
         - CSV export of Protocol, PDO, SDO frames
        """
        self.total_frames += 1
        cob = msg.arbitration_id
        raw = bytes_to_hex(msg.data)

        # top talkers
        self.top_talkers[cob] += 1

        # nodes seen (extract node id from typical COB-ID position)
        node_id = cob & 0x7F
        if 1 <= node_id <= 127:
            self.nodes_seen.add(node_id)

        # frame distribution
        if cob == 0x000:
            self.frame_dist["NMT"] += 1
        elif cob == 0x080:
            self.frame_dist["SYNC"] += 1
        elif 0x080 <= cob <= 0x0FF:
            self.frame_dist["EMCY"] += 1
        elif cob == 0x100:
            self.frame_dist["TIME"] += 1
        elif 0x180 <= cob <= 0x4FF:
            self.frame_dist["PDO"] += 1
        elif 0x580 <= cob <= 0x5FF:
            self.frame_dist["SDO"] += 1
        elif 0x700 <= cob <= 0x7FF:
            self.frame_dist["Heartbeat"] += 1
        else:
            self.frame_dist["Other"] += 1

        # detect error frames (python-can: is_error_frame)
        if hasattr(msg, "is_error_frame") and msg.is_error_frame:
            self.last_error_time = now_str()
            self.last_error_frame = raw
            log.warning("Error frame detected: %s", raw)
        # Some backends may signal errors differently; we'll still record if arbitration_id == 0 and dlc==0 (?) - skip heuristic

        # Record SDO request for latency calculations (client -> server)
        if 0x600 <= cob <= 0x67F and len(msg.data) >= 4:
            try:
                index = msg.data[2] << 8 | msg.data[1]
                sub = msg.data[3]
                self.sdo_req_time[(index, sub)] = time.time()
                log.debug(f"SDO request idx=0x{index:04X} sub={sub} recorded for latency measurement")
            except Exception:
                log.debug("Malformed SDO request frame while recording req time")
            # NOTE: do not return here — SDO requests are not SDO responses; continue handling

        # --- SDO response (server -> client) ---
        if 0x580 <= cob <= 0x5FF:
            self.sdo_frames_count += 1
            # Ensure data length enough
            if len(msg.data) >= 4:
                index = msg.data[2] << 8 | msg.data[1]
                sub = msg.data[3]
            else:
                index, sub = 0, 0

            # detect abort
            if msg.data and msg.data[0] == 0x80:
                self.sdo_abort += 1
                decoded = "ABORT"
                payload_len = 0
            else:
                # assume expedited/data in bytes 4+
                payload = msg.data[4:]
                payload_len = len(payload)
                if payload_len:
                    val = int.from_bytes(payload, "little")
                    decoded = str(val)
                else:
                    decoded = ""

            self.sdo_payload_bytes += payload_len

            # compute response latency if request recorded
            resp_time = None
            key = (index, sub)
            req_ts = self.sdo_req_time.pop(key, None)
            if req_ts:
                resp_time = time.time() - req_ts
                self.sdo_response_times.append(resp_time)
                log.debug(f"SDO response latency for 0x{index:04X}:{sub} = {resp_time:.4f}s")

            name = self.name_map.get((index, sub), f"0x{index:04X}:{sub}")
            frame = {"time": now_str(), "cob": f"0x{cob:03X}", "index": f"0x{index:04X}",
                     "sub": f"0x{sub:02X}", "name": name, "raw": raw,
                     "decoded": decoded, "count": 1}
            if self.fixed:
                if key in self.fixed_sdo:
                    frame["count"] = self.fixed_sdo[key]["count"] + 1
                self.fixed_sdo[key] = frame
                log.debug("Fixed SDO updated: %s count=%d", name, frame["count"])
            else:
                self.sdo_frames.append(frame)
                log.debug("SDO appended: %s", name)

            # --- export to CSV ---
            self.export_frame("SDO", cob, name, f"0x{index:04X}", f"0x{sub:02X}", raw, decoded)
            log.debug(f"CSV export SDO: {name} decoded={decoded}")

            return

        # --- PDO frames ---
        if 0x180 <= cob <= 0x4FF:
            self.pdo_frames_count += 1
            self.pdo_payload_bytes += len(msg.data)
            if cob in self.pdo_map:
                entries = self.pdo_map[cob]
                offset = 0
                for (index, sub, size) in entries:
                    size_bytes = max(1, size // 8)
                    chunk = msg.data[offset:offset + size_bytes]
                    offset += size_bytes
                    try:
                        if size_bytes == 4:
                            val = struct.unpack("<f", chunk)[0]
                        else:
                            val = int.from_bytes(chunk, "little") if chunk else 0
                    except Exception:
                        val = int.from_bytes(chunk, "little") if chunk else 0

                    name = self.name_map.get((index, sub), f"0x{index:04X}:{sub}")
                    key = (cob, index, sub)
                    frame = {"time": now_str(), "cob": f"0x{cob:03X}", "name": name,
                             "index": f"0x{index:04X}", "sub": f"0x{sub:02X}",
                             "raw": raw, "decoded": str(val), "count": 1}
                    if self.fixed:
                        if key in self.fixed_pdo:
                            frame["count"] = self.fixed_pdo[key]["count"] + 1
                        self.fixed_pdo[key] = frame
                    else:
                        self.frames.append(frame)

                    # --- export to CSV ---
                    self.export_frame("PDO", cob, name, f"0x{index:04X}", f"0x{sub:02X}", raw, str(val))
                    log.debug(f"CSV export PDO: {name} decoded={val}")
                log.debug("PDO handled cob=0x%03X entries=%d", cob, len(entries))
            else:
                # store raw PDO
                frame = {"time": now_str(), "cob": f"0x{cob:03X}", "name": "",
                         "index": "", "sub": "", "raw": raw, "decoded": raw, "count": 1}
                key = (cob, None, None)
                if self.fixed:
                    if key in self.fixed_pdo:
                        frame["count"] = self.fixed_pdo[key]["count"] + 1
                    self.fixed_pdo[key] = frame
                else:
                    self.frames.append(frame)
                log.debug("Raw PDO appended cob=0x%03X", cob)
            return

        # --- Protocol / other frames ---
        self.other_frames_count += 1
        ptype = "Other"
        if cob == 0x000:
            ptype = "NMT"
        elif cob == 0x080:
            ptype = "SYNC"
        elif 0x080 <= cob <= 0x0FF:
            ptype = "EMCY"
        elif cob == 0x100:
            ptype = "TIME"
        elif 0x700 <= cob <= 0x7FF:
            ptype = "Heartbeat"

        frame = {"time": now_str(), "cob": f"0x{cob:03X}", "type": ptype, "raw": raw, "decoded": "", "count": 1}
        key = (cob, ptype)
        if self.fixed:
            if key in self.fixed_proto:
                frame["count"] = self.fixed_proto[key]["count"] + 1
            self.fixed_proto[key] = frame
        else:
            self.proto_frames.append(frame)
        log.debug("Protocol frame type=%s cob=0x%03X", ptype, cob)

        # --- export Protocol to CSV ---
        self.export_frame(ptype, cob, "", "", "", raw, "")
        log.debug(f"CSV export Protocol frame type={ptype} cob=0x{cob:03X}")

    # --- bus stats ---
    def build_bus_stats(self):
        now = time.time()
        elapsed = now - self.last_rate_calc
        if elapsed >= 1.0:
            pdo_rate = (self.pdo_frames_count - self.last_counts["pdo"]) / elapsed
            sdo_rate = (self.sdo_frames_count - self.last_counts["sdo"]) / elapsed
            tot_rate = (self.total_frames - self.last_counts["total"]) / elapsed

            self.pdo_rate_hist.append(pdo_rate)
            self.sdo_rate_hist.append(sdo_rate)
            self.tot_rate_hist.append(tot_rate)

            # rolling peak (max of recent total history)
            peak = max(self.tot_rate_hist) if self.tot_rate_hist else 0
            self.peak_rate_hist.append(peak)

            self.last_counts = {"pdo": self.pdo_frames_count,
                                "sdo": self.sdo_frames_count,
                                "total": self.total_frames}
            self.last_rate_calc = now

        t = Table(title="Bus Stats", expand=True, box=box.SQUARE, style="yellow")
        # Keep number of columns same as Protocol table (Protocol has 6 columns below), Bus Stats: Metric, Value, Graph -> we'll mimic widths
        t.add_column("Metric", no_wrap=True, width=10)
        t.add_column("Value", justify="right", width=30)
        t.add_column("Graph", justify="left", width=GRAPH_WIDTH)

        # State
        t.add_row("State", "Active" if self.total_frames else "Idle", "")

        # Active Nodes
        t.add_row("Active Nodes", str(len(self.nodes_seen)), f"[dim]{sorted(self.nodes_seen)}[/]" if self.nodes_seen else "")

        # PDO/SDO/Total frames per second + sparklines
        if self.pdo_rate_hist:
            t.add_row("PDO Frames/s", f"{self.pdo_rate_hist[-1]:.1f}", sparkline(self.pdo_rate_hist, "green"))
        else:
            t.add_row("PDO Frames/s", "0.0", "")

        if self.sdo_rate_hist:
            t.add_row("SDO Frames/s", f"{self.sdo_rate_hist[-1]:.1f}", sparkline(self.sdo_rate_hist, "magenta"))
        else:
            t.add_row("SDO Frames/s", "0.0", "")

        if self.tot_rate_hist:
            t.add_row("Total Frames/s", f"{self.tot_rate_hist[-1]:.1f}", sparkline(self.tot_rate_hist, "cyan"))
        else:
            t.add_row("Total Frames/s", "0.0", "")

        # Peak Frames/s (rolling)
        if self.peak_rate_hist:
            t.add_row("Peak Frames/s", f"{self.peak_rate_hist[-1]:.1f}", sparkline(self.peak_rate_hist, "yellow"))
        else:
            t.add_row("Peak Frames/s", "0.0", "")

        # Bus Util % (approx)
        # average bits per frame: approximate using observed payloads; if none observed, assume 128 bits/frame
        total_payload_frames = (self.pdo_frames_count + self.sdo_frames_count) or 1
        avg_payload_bytes = (self.pdo_payload_bytes + self.sdo_payload_bytes) / total_payload_frames if total_payload_frames else 0
        avg_frame_bits = max(64, int(avg_payload_bytes * 8 + 64))  # overhead approx
        util = (self.tot_rate_hist[-1] * avg_frame_bits) / self.bitrate * 100 if self.tot_rate_hist else 0.0
        idle = max(0.0, 100.0 - util)
        t.add_row("Bus Util %", f"{util:.2f}%", sparkline(self.tot_rate_hist, "cyan"))
        t.add_row("Bus Idle %", f"{idle:.2f}%", "")

        # Frame Dist (moved to Value column)
        dist_pairs = ", ".join(f"{k}:{v}" for k, v in self.frame_dist.items())
        t.add_row("Frame Dist.", dist_pairs or "-", "")

        # SDO OK/Abort
        t.add_row("SDO OK/Abort", f"{self.sdo_success}/{self.sdo_abort}", "")

        # Avg PDO / SDO payload
        avg_pdo_payload = (self.pdo_payload_bytes / self.pdo_frames_count) if self.pdo_frames_count else 0
        avg_sdo_payload = (self.sdo_payload_bytes / self.sdo_frames_count) if self.sdo_frames_count else 0
        t.add_row("Avg PDO Payload", f"{avg_pdo_payload:.1f} B", "")
        t.add_row("Avg SDO Payload", f"{avg_sdo_payload:.1f} B", "")

        # SDO response time (avg)
        avg_sdo_rt = (sum(self.sdo_response_times) / len(self.sdo_response_times)) if self.sdo_response_times else 0.0
        t.add_row("SDO resp time", f"{avg_sdo_rt * 1000:.1f} ms", "")

        # Top Talkers (show top 3)
        top = self.top_talkers.most_common(3)
        top_str = ", ".join(f"0x{c:03X}:{cnt}" for c, cnt in top) if top else "-"
        t.add_row("Top Talkers", top_str, "")

        # Last Error
        last_err = f"{self.last_error_time} {self.last_error_frame}" if self.last_error_time else "-"
        t.add_row("Last Error", last_err, "")

        return t

    # --- rendering ---
    def render_tables(self):
        # Protocol Data
        t_proto = Table(title="Protocol Data", expand=True, box=box.SQUARE, style="cyan")
        t_proto.add_column("Time", no_wrap=True)
        t_proto.add_column("COB-ID", width=8)
        t_proto.add_column("Type", width=12)
        t_proto.add_column("Raw Data", no_wrap=True)
        t_proto.add_column("Decoded")
        t_proto.add_column("Count", width=6, justify="right")

        protos = list(self.fixed_proto.values())[-PROTOCOL_TABLE_HEIGHT:] if self.fixed else list(self.proto_frames)[-PROTOCOL_TABLE_HEIGHT:]
        while len(protos) < PROTOCOL_TABLE_HEIGHT:
            protos.append({"time": "", "cob": "", "type": "", "raw": "", "decoded": "", "count": ""})
        for p in protos:
            t_proto.add_row(p["time"], p["cob"], p["type"], p["raw"], p["decoded"], str(p.get("count", "")))

        # Bus Stats
        t_bus = self.build_bus_stats()

        # PDO table (now matches SDO columns)
        t_pdo = Table(title="PDO Data", expand=True, box=box.SQUARE, style="green")
        t_pdo.add_column("Time", no_wrap=True)
        t_pdo.add_column("COB-ID", width=8)
        t_pdo.add_column("Name")
        t_pdo.add_column("Index")
        t_pdo.add_column("Sub")
        t_pdo.add_column("Raw Data", no_wrap=True)
        t_pdo.add_column("Decoded")
        t_pdo.add_column("Count", width=6, justify="right")

        frames = list(self.fixed_pdo.values())[-DATA_TABLE_HEIGHT:] if self.fixed else list(self.frames)[-DATA_TABLE_HEIGHT:]
        while len(frames) < DATA_TABLE_HEIGHT:
            frames.append({"time": "", "cob": "", "name": "", "index": "", "sub": "", "raw": "", "decoded": "", "count": ""})
        for f in frames:
            decoded = Text(f["decoded"], style="bold green") if f.get("decoded") else ""
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

        sdos = list(self.fixed_sdo.values())[-DATA_TABLE_HEIGHT:] if self.fixed else list(self.sdo_frames)[-DATA_TABLE_HEIGHT:]
        while len(sdos) < DATA_TABLE_HEIGHT:
            sdos.append({"time": "", "cob": "", "name": "", "index": "", "sub": "", "raw": "", "decoded": "", "count": ""})
        for s in sdos:
            decoded = Text(s["decoded"], style="bold magenta") if s.get("decoded") else ""
            t_sdo.add_row(s["time"], s["cob"], s.get("name", ""), s.get("index", ""), s.get("sub", ""), s.get("raw", ""), decoded, str(s.get("count", "")))

        # Layout: keep titles separate by passing None column between adjacent panels
        layout = Table.grid(expand=True)
        layout.add_row(t_proto, None, t_bus)
        layout.add_row(t_pdo, None, t_sdo)
        return layout

    def start(self):
        with Live(console=self.console, refresh_per_second=6, screen=True) as live:
            try:
                while True:
                    msg = self.bus.recv(timeout=0.1)
                    if msg:
                        try:
                            self.handle_msg(msg)
                        except Exception as e:
                            log.exception("Error handling message: %s", e)
                    live.update(self.render_tables())
            finally:
                if self.export_file:
                    self.export_file.close()
                try:
                    self.bus.shutdown()
                except Exception:
                    pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--interface", default="can0", help="CAN interface (default: can0)")
    p.add_argument("--eds", help="EDS file path (optional)")
    p.add_argument("--bitrate", type=int, default=1000000, help="CAN bitrate (default 1000000)")
    p.add_argument("--log", action="store_true", help="enable logging")
    p.add_argument("--fixed", action="store_true", help="update rows instead of scrolling")
    p.add_argument("--export", action="store_true", help="export received frames to CSV")
    args = p.parse_args()

    if args.log:
        enable_logging()

    sniffer = CANopenSniffer(interface=args.interface, eds_path=args.eds,
                             bitrate=args.bitrate, fixed=args.fixed, export=args.export)
    sniffer.start()


if __name__ == "__main__":
    main()
