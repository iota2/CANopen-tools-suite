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
@file process_frames.py
@brief CANopen frame processing, decoding, and statistics update thread.
@details
This module implements the @ref process_frames thread, which consumes raw
CAN frames produced by the sniffer, classifies them according to the
CANopen specification, decodes payloads, updates statistics, and emits
processed frames for display.

### Responsibilities
- Classify frames into CANopen message types
- Decode SDO, PDO, EMCY, TIME, and heartbeat frames
- Update bus statistics and SDO timing metrics
- Resolve Object Dictionary names using the EDS parser
- Optionally export processed frames to CSV
- Push decoded frames to display backends

### Design Notes
- All decoding is best-effort and tolerant of malformed frames.
- The processor does not interact directly with CAN hardware.
- EDS parsing is read-only and shared safely across threads.

### Threading Model
Runs as a dedicated daemon thread and communicates exclusively via queues.

### Error Handling
Malformed frames, decode errors, and EDS lookup failures are logged and do
not interrupt processing.
"""

import os
import csv
import json
import struct
import logging

from datetime import datetime, timedelta, UTC

import threading
import queue

import analyzer_defs as analyzer_defs
from eds_parser import eds_parser
from bus_stats import bus_stats

class process_frames(threading.Thread):

    def _sdo_has_index(self, cs: int) -> bool:
        """!
        @brief Return True if SDO command specifier carries index/subindex.
        """
        return (cs & 0xE0) in (0x20, 0x40, 0x80)

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

    def __init__(self, stats: bus_stats, raw_frame: queue.Queue, processed_frame: queue.Queue, eds_map: eds_parser, export: str | None = None, sniffer: bool = False):
        """! Initialize the processor thread.
        @details
        The constructor stores references to required helpers, initializes a
        stop event and logging, sets up CSV export if requested, and ensures
        statistics collection start time is set.
        @param stats Instance of @ref bus_stats used to record statistics.
        @param raw_frame `queue.Queue` providing raw frames (dict) from the sniffer.
        @param processed_frame `queue.Queue` instance to push processed frames for display.
        @param eds_map Instance of @ref eds_parser from eds_parser.py used to
               resolve Object Dictionary names and PDO mappings.
        @param export `csv`, `json`: enable export of processed frames to a file.
        """
        super().__init__(daemon=True)

        ## Queue from which raw frame dictionaries are consumed.
        self.raw_frame = raw_frame

        ## Queue from which raw frame dictionaries are consumed.
        self.processed_frame = processed_frame

        ## Internal event used to signal the run loop to stop.
        self._stop_event = threading.Event()

        ## Logger instance scoped to this processor.
        self.log = logging.getLogger(f"{analyzer_defs.APP_NAME}.{self.__class__.__name__}")

        ## EDS map/parser used to resolve (index, subindex) -> name strings.
        self.eds_map = eds_map

        ## Reference to the bus_stats instance used for recording metrics.
        self.stats = stats
        self.stats.set_start_time()

        ## State for segmented SDO support: (node, index, sub) -> bytearray
        self._sdo_segments = {}

        ## Active processed-frame exports keyed by format ("csv" | "json").
        ## Each value is a per-format record dict holding that format's file /
        ## writer and bookkeeping. Formats are independent and may be active
        ## simultaneously. ("pcap" is accepted for symmetry with the sniffer
        ## but produces no processed file.)
        self._exports = {}

        ## Serializes export open/close against the processing run loop so a
        ## runtime enable/disable never races an in-flight write.
        self._export_lock = threading.Lock()

        # Open the requested export format (if any) at startup. The same
        # machinery is reused at runtime via @ref enable_export.
        if export:
            self.enable_export(export)

    # --- Runtime export management ---
    def _open_export(self, fmt: str):
        """! Open a processed-frame export for @p fmt and store its record.
        @details
        Opens the file/writer for @p fmt and writes any required header,
        registering a per-format record in @ref _exports. `pcap` is accepted
        for symmetry with the sniffer but writes no processed file (only the
        raw sniffer stream is PCAP-capable); the record is still registered so
        both workers stay in sync. Formats are independent, so this never
        disturbs other active exports. Callers must hold @ref _export_lock.
        @param fmt Export format: "csv", "json", or "pcap".
        """

        try:
            if fmt == "csv":
                filename = f"{analyzer_defs.APP_NAME}_processed.csv"
                f = open(filename, "w", newline="")
                writer = csv.writer(f)
                writer.writerow(
                    ["S.No.", "Time", "Type", "Direction", "COB-ID", "Index", "Sub", "Name", "Raw", "Decoded"]
                )
                try:
                    f.flush()
                    os.fsync(f.fileno())
                except Exception:
                    pass
                self._exports["csv"] = {"file": f, "writer": writer, "filename": filename, "serial": 1}
                self.log.info(f"CSV export enabled → {filename}")

            elif fmt == "json":
                filename = f"{analyzer_defs.APP_NAME}_processed.json"
                f = open(filename, "w")
                f.write("[\n")
                self._exports["json"] = {"file": f, "filename": filename, "json_first": True}
                self.log.info(f"JSON export enabled → {filename}")

            elif fmt == "pcap":
                # Processed frames are not PCAP-capable; register a record with
                # no file so both workers stay in sync.
                self._exports["pcap"] = {}

            else:
                self.log.warning("Unknown export format requested: %s", fmt)

        except Exception as e:
            self.log.exception("Failed to open %s export file: %s", fmt, e)
            self._exports.pop(fmt, None)

    def _close_export(self, fmt: str):
        """! Flush and close a single processed-frame export format.
        @details
        Finalizes the format's file (JSON array terminator, flush, fsync,
        close) then removes it from @ref _exports. Callers must hold
        @ref _export_lock.
        @param fmt Export format to close.
        """

        rec = self._exports.pop(fmt, None)
        if rec is None:
            return

        try:
            f = rec.get("file")
            if f:
                if fmt == "json":
                    try:
                        f.write("\n]\n")
                    except Exception:
                        pass
                try:
                    f.flush()
                    os.fsync(f.fileno())
                except Exception:
                    pass
                try:
                    f.close()
                except Exception:
                    pass
                self.log.info("%s processed export closed", fmt.upper())
        except Exception:
            self.log.exception("Failed during %s processed export cleanup", fmt)

    def _close_all_exports(self):
        """! Close every active processed-frame export. Callers must hold @ref _export_lock."""

        for fmt in list(self._exports.keys()):
            self._close_export(fmt)

    def enable_export(self, fmt: str):
        """! Enable processed-frame export in the given format at runtime.
        @details
        Thread-safe and independent: enabling a format does not affect any
        other active formats, so CSV and JSON can run at once. Enabling an
        already-active format is a no-op.
        @param fmt Export format: "csv", "json", or "pcap".
        @return The set of active export formats after the call.
        """

        with self._export_lock:
            if fmt not in self._exports:
                self._open_export(fmt)
            return set(self._exports.keys())

    def disable_export(self, fmt: str = None):
        """! Disable processed-frame export at runtime (thread-safe).
        @param fmt Format to disable, or None to disable all active formats.
        @return The set of active export formats after the call.
        """

        with self._export_lock:
            if fmt is None:
                self._close_all_exports()
            else:
                self._close_export(fmt)
            return set(self._exports.keys())

    def active_exports(self):
        """! Return the set of currently active export formats (thread-safe)."""

        with self._export_lock:
            return set(self._exports.keys())

    def _json_safe_processed_frame(self, frame: dict) -> dict:
        """! Create a processed frame for saving to JSON."""

        return {
            "time": frame["time"],
            "cob": frame["cob"],
            "type": frame["type"].name if hasattr(frame["type"], "name") else str(frame["type"]),
            "dir": frame["dir"],
            "index": frame["index"],
            "sub": frame["sub"],
            "name": frame["name"],
            "raw": frame["raw"],
            "decoded": frame["decoded"],
        }

    def export_processed_frame(self, frame: dict):
        """! Save a processed frame row to the processed export file.
        @details
        Writes processed frame to export file. Periodically flushes
        and `fsyncs` the file according to `defs.FSYNC_EVERY`. Serialized
        against runtime enable/disable via @ref _export_lock.
        @param frame Processed frame.
        """
        if not self._exports:
            return

        with self._export_lock:
            self._export_processed_frame_locked(frame)

    def _export_processed_frame_locked(self, frame: dict):
        """! Write a processed frame to every active export format.
        @details
        Each active format (CSV, JSON) is written independently. Caller must
        hold @ref _export_lock.
        """

        if "csv" in self._exports:
            self._write_csv_processed(self._exports["csv"], frame)
        if "json" in self._exports:
            self._write_json_processed(self._exports["json"], frame)

    def _write_csv_processed(self, rec: dict, frame: dict):
        """! Append one processed-frame row to the CSV export record."""

        try:
            rec["writer"].writerow([
                rec["serial"],
                frame["time"],
                frame["type"].name,
                frame["dir"],
                f"0x{frame['cob']:03X}",
                f"0x{frame['index']:04X}",
                f"0x{frame['sub']:02X}",
                frame["name"],
                frame["raw"],
                frame["decoded"],
            ])
            rec["serial"] += 1
            try:
                rec["file"].flush()
                if (rec["serial"] % analyzer_defs.FSYNC_EVERY) == 0:
                    os.fsync(rec["file"].fileno())
            except Exception:
                pass
        except Exception as e:
            self.log.error("CSV export failed: %s", e)

    def _write_json_processed(self, rec: dict, frame: dict):
        """! Append one processed-frame object to the JSON export record."""

        try:
            obj = self._json_safe_processed_frame(frame)

            if not rec["json_first"]:
                rec["file"].write(",\n")
            rec["json_first"] = False

            json.dump(obj, rec["file"], indent=2, ensure_ascii=False)

            try:
                rec["file"].flush()
            except Exception:
                pass
        except Exception as e:
            self.log.error("JSON export failed: %s", e)

    def save_processed_frame(self, frame: dict):
        """! Save a fully processed CANopen frame in memory and export it to CSV.
        @details
        Converts the raw and decoded payloads into hexadecimal string representations if necessary,
        then delegates the storage of the processed frame to @ref save_frame and its CSV export
        to @ref export_processed_frame.
        This function ensures consistent formatting for both in-memory data and CSV output.
        @param frame    Frame to be saved.
        """

        # Render decoded possibly already a string — only hex raw bytes
        frame["raw"] = analyzer_defs.bytes_to_hex(frame["raw"])
        frame["decoded"] = frame["decoded"] if isinstance(frame["decoded"], str) else analyzer_defs.bytes_to_hex(frame["decoded"])

        # Save frame for downstream use
        # self.save_frame(frame)

        # Decide log level once
        is_od_frame = frame["type"] in (
            analyzer_defs.frame_type.PDO,
            analyzer_defs.frame_type.SDO_REQ,
            analyzer_defs.frame_type.SDO_RES,
        )

        log_fn = self.log.debug
        if is_od_frame and frame["index"] == 0x0000:
            log_fn = self.log.error

        log_fn("Processed Frame: "
               f"[{frame['time']}] "
               f"[{frame['type']}] "
               f"[0x{frame['cob']:03X}] "
               f"[{frame['dir']}] "
               f"[0x{frame['index']:04X}] "
               f"[0x{frame['sub']:02X}] "
               f"[{frame['name']}] "
               f"[{frame['raw']}] "
               f"[{frame['decoded']}]")

        # Drop unresolved OD frames only
        if not (is_od_frame and frame["index"] == 0x0000):
            self.processed_frame.put(frame)

        # Export to CSV
        self.export_processed_frame(frame)

    def decode_by_datatype(self, raw: bytes, entry: dict | None):
        """!
        Decode raw frame to respective CANopen DataType.
        """

        if not entry:
            return raw.hex()

        dt = entry["data_type"]

        # ---------- BOOLEAN ----------
        if dt == "BOOLEAN":
            return bool(int.from_bytes(raw[:1], "little"))

        # ---------- UNSIGNED ----------
        if dt == "UNSIGNED8":
            val = int.from_bytes(raw[:1], "little")
            return f"{val} [0x{val:02X}]"
        if dt == "UNSIGNED16":
            val = int.from_bytes(raw[:2], "little")
            return f"{val} [0x{val:04X}]"
        if dt == "UNSIGNED32":
            val = int.from_bytes(raw[:4], "little")
            return f"{val} [0x{val:08X}]"

        # ---------- SIGNED ----------
        if dt == "INTEGER8":
            return struct.unpack("<b", raw[:1])[0]
        if dt == "INTEGER16":
            return struct.unpack("<h", raw[:2])[0]
        if dt == "INTEGER32":
            return struct.unpack("<i", raw[:4])[0]

        # ---------- REAL ----------
        if dt == "REAL32":
            return round(struct.unpack("<f", raw[:4])[0], 2)
        if dt == "REAL64":
            return round(struct.unpack("<d", raw[:8])[0], 2)

        # ---------- VISIBLE STRING ----------
        if dt == "VISIBLE_STRING":
            # Stop at first NUL (CiA-301)
            s = raw.split(b"\x00", 1)[0]

            # Decode ASCII safely
            try:
                return s.decode("ascii", errors="replace")
            except Exception:
                return s.decode("latin-1", errors="replace")

        # ---------- UNICODE STRING ----------
        if dt == "UNICODE_STRING":
            # UTF-16LE per CiA-301
            try:
                return raw.decode("utf-16-le").rstrip("\x00")
            except Exception:
                return raw.hex(" ")

        # ---------- OCTET STRING ----------
        if dt == "OCTET_STRING":
            # Binary but human-readable
            return raw.hex(" ")

        # ---------- DOMAIN ----------
        if dt == "DOMAIN":
            # Raw bytes, caller decides how to display/export
            return raw

        # ---------- FALLBACK ----------
        return raw.hex(" ")

    def _resolve_od_entry(self, index: int, sub: int):
        """!Resolve Object Dictionary metadata for (index, sub)."""

        try:
            entry = self.eds_map.entry_map.get((index, sub))
        except Exception:
            entry = None

        if not entry:
            return None, f"0x{index:04X}:{sub}", None, None

        return (
            entry,
            entry.get("name"),
            entry.get("data_type"),
            entry.get("access_type"),
        )

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

                # Check if it's a transmission frames
                is_tx = True if frame.get("type") == "tx" else False

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
                ftype = analyzer_defs.frame_type.UNKNOWN
                try:
                    if cob == 0x000:
                        ftype = analyzer_defs.frame_type.NMT
                        self.stats.increment_frame(analyzer_defs.frame_type.NMT)
                    elif cob == 0x080:
                        ftype = analyzer_defs.frame_type.SYNC
                        self.stats.increment_frame(analyzer_defs.frame_type.SYNC)
                    elif 0x080 <= cob <= 0x0FF:
                        ftype = analyzer_defs.frame_type.EMCY
                        self.stats.increment_frame(analyzer_defs.frame_type.EMCY)
                    elif 0x100 <= cob <= 0x17F:
                        ftype = analyzer_defs.frame_type.TIME
                        self.stats.increment_frame(analyzer_defs.frame_type.TIME)
                    elif 0x180 <= cob <= 0x4FF:
                        ftype = analyzer_defs.frame_type.PDO
                        self.stats.increment_frame(analyzer_defs.frame_type.PDO)
                    elif 0x580 <= cob <= 0x5FF:
                        ftype = analyzer_defs.frame_type.SDO_RES
                        self.stats.increment_frame(analyzer_defs.frame_type.SDO_RES)
                    elif 0x600 <= cob <= 0x67F:
                        ftype = analyzer_defs.frame_type.SDO_REQ
                        self.stats.increment_frame(analyzer_defs.frame_type.SDO_REQ)
                    elif 0x700 <= cob <= 0x7FF:
                        ftype = analyzer_defs.frame_type.HB
                        self.stats.increment_frame(analyzer_defs.frame_type.HB)
                    else:
                        ftype = analyzer_defs.frame_type.UNKNOWN
                        self.stats.increment_frame(analyzer_defs.frame_type.UNKNOWN)
                except Exception:
                    self.log.warning("Error while classifying frame cob=%s", cob)

                # detect error frames (python-can: is_error_frame)
                if error:
                    try:
                        self.stats._stats.error.last_time = analyzer_defs.now_str()
                        self.stats._stats.error.last_frame = raw
                    except Exception:
                        pass
                    self.log.warning("Error frame detected: %s", raw)

                # ---------------- SDO REQUEST (CLIENT → SERVER) ----------------
                if ftype == analyzer_defs.frame_type.SDO_REQ and raw and len(raw) >= 4:
                    try:
                        cs = raw[0]
                        index = raw[2] << 8 | raw[1]
                        sub = raw[3]

                        self.stats.update_sdo_request_time(index, sub)

                        entry, name, data_type, access_type = self._resolve_od_entry(index, sub)

                        decoded = ""
                        payload_len = 0
                        publish = True

                        # ---- UPLOAD REQUEST (READ) ----
                        if cs == 0x40:
                            decoded = "READ"

                        # ---- EXPEDITED DOWNLOAD (WRITE) ----
                        elif cs in (0x2F, 0x2B, 0x23):
                            unused = (cs >> 2) & 0x03
                            payload_len = 4 - unused
                            payload = raw[4:4 + payload_len]

                            try:
                                decoded = self.decode_by_datatype(payload, entry)
                            except Exception:
                                decoded = int.from_bytes(payload, "little", signed=False)

                        # ---- SEGMENTED DOWNLOAD INIT (CLIENT → SERVER) ----
                        elif (cs & 0xE0) == 0x20:
                            # Store transfer context explicitly
                            self._sdo_segments[(node_id, index, sub)] = {
                                "data": bytearray(),
                                "index": index,
                                "sub": sub,
                                "name": name,
                                "data_type": data_type,
                                "access_type": access_type,
                                "entry": entry,
                            }
                            publish = False

                        # ---- SEGMENTED DOWNLOAD SEGMENT ----
                        elif (cs & 0xE0) == 0x00:
                            publish = False

                            # Find active segmented transfer for this node
                            key = next(
                                (k for k in self._sdo_segments if k[0] == node_id),
                                None
                            )
                            if not key:
                                self.log.debug("Ignoring orphan SDO segment from node %d", node_id)
                                continue  # orphan segment → ignore safely

                            ctx = self._sdo_segments[key]
                            ctx["data"] += raw[1:8]

                            last = cs & 0x01
                            if last:
                                ctx = self._sdo_segments.pop(key)

                                full = bytes(ctx["data"])
                                decoded = self.decode_by_datatype(full, ctx["entry"])

                                index = ctx["index"]
                                sub = ctx["sub"]
                                name = ctx["name"]
                                data_type = ctx["data_type"]
                                access_type = ctx["access_type"]

                                publish = True

                        # ---- ABORT ----
                        elif cs == 0x80:
                            decoded = "ABORT"

                        if payload_len > 0:
                            try:
                                self.stats.increment_payload(
                                    analyzer_defs.frame_type.SDO_REQ, payload_len
                                )
                            except KeyError:
                                self.log.error(f"SDO REQ Payload increment: {KeyError}")
                                pass

                        if publish:
                            self.save_processed_frame({
                                "time": analyzer_defs.now_str(),
                                "cob": cob,
                                "type": ftype,
                                "dir": "TX" if is_tx else "RX",
                                "index": index,
                                "sub": sub,
                                "name": name,
                                "data_type": data_type,
                                "access_type": access_type,
                                "raw": raw,
                                "decoded": decoded,
                            })

                    except Exception as e:
                        self.log.warning(f"SDO_REQ processing failed: {e}")

                # ---------------- SDO RESPONSE (SERVER → CLIENT) ----------------
                elif ftype == analyzer_defs.frame_type.SDO_RES and raw and len(raw) >= 4:
                    try:
                        cs = raw[0]
                        index = raw[2] << 8 | raw[1]
                        sub = raw[3]

                        entry, name, data_type, access_type = self._resolve_od_entry(index, sub)

                        decoded = ""
                        payload_len = 0
                        publish = True

                        # ---- ABORT ----
                        if cs == 0x80 and len(raw) >= 8:
                            self.stats.increment_sdo_abort()
                            abort_code = int.from_bytes(raw[4:8], "little")
                            decoded = f"ABORT 0x{abort_code:08X}"

                        # ---- SEGMENTED UPLOAD INIT ----
                        elif (cs & 0xE0) == 0x40:
                            self._sdo_segments[(node_id, index, sub)] = bytearray()
                            decoded = "<SDO segmented upload start>"
                            publish = False

                        # ---- SEGMENTED UPLOAD SEGMENT ----
                        elif (cs & 0xE0) == 0x00:
                            key = (node_id, index, sub)
                            publish = False

                            if key in self._sdo_segments:
                                self._sdo_segments[key] += raw[1:8]
                                last = cs & 0x01
                                payload_len = len(raw[1:8])

                                if last:
                                    full = bytes(self._sdo_segments.pop(key))
                                    decoded = self.decode_by_datatype(full, entry)
                                    self.stats.increment_sdo_success()
                                    publish = True

                        # ---- EXPEDITED UPLOAD ----
                        elif cs in (0x43, 0x4B, 0x4F):
                            self.stats.increment_sdo_success()
                            n_unused = (cs >> 2) & 0x03
                            data_len = 4 - n_unused
                            payload = raw[4:4 + data_len]
                            decoded = self.decode_by_datatype(payload, entry)
                            payload_len = data_len

                        # ---- DOWNLOAD ACK ----
                        elif cs == 0x60:
                            self.stats.increment_sdo_success()
                            decoded = "OK"

                        if payload_len:
                            self.stats.increment_payload(analyzer_defs.frame_type.SDO_RES, payload_len)

                        if publish:
                            self.stats.update_sdo_response_time(index, sub)

                            self.save_processed_frame({
                                "time": analyzer_defs.now_str(),
                                "cob": cob,
                                "type": ftype,
                                "dir": "TX" if is_tx else "RX",
                                "index": index,
                                "sub": sub,
                                "name": name,
                                "data_type": data_type,
                                "access_type": access_type,
                                "raw": raw,
                                "decoded": decoded,
                            })

                    except Exception as e:
                        self.log.warning(f"SDO_RES processing failed: {e}")

                # PDO frame
                elif ftype == analyzer_defs.frame_type.PDO:
                    payload_len = len(raw)
                    self.stats.increment_payload(analyzer_defs.frame_type.PDO, payload_len)

                    # -------------------------------------------------
                    # Decide PDO role from EDS, NOT from TX/RX
                    # -------------------------------------------------
                    if cob in self.eds_map.tpdo_map:
                        pdo_map = self.eds_map.tpdo_map
                    elif cob in self.eds_map.rpdo_map:
                        pdo_map = self.eds_map.rpdo_map
                    else:
                        pdo_map = None

                    if pdo_map:
                        entries = pdo_map[cob]
                        offset = 0

                        for (index, sub, size) in entries:
                            size_bytes = max(1, size // 8)
                            chunk = raw[offset:offset + size_bytes]
                            offset += size_bytes

                            try:
                                entry, name, data_type, access_type = self._resolve_od_entry(index, sub)
                                decoded = self.decode_by_datatype(chunk, entry)
                            except Exception as e:
                                self.log.warning("PDO decoding failed: {e}")
                                decoded = int.from_bytes(chunk, "little", signed=False) if chunk else 0

                            name = (
                                self.eds_map.name_map.get((index, sub))
                                or self.eds_map.name_map.get((index, 0))
                                or f"0x{index:04X}:{sub}"
                            )

                            frame = {
                                "time": analyzer_defs.now_str(),
                                "cob": cob,
                                "type": ftype,
                                "dir": "TX" if is_tx else "RX",
                                "index": index,
                                "sub": sub,
                                "name": name,
                                "data_type": data_type,
                                "access_type": access_type,
                                "raw": raw,
                                "decoded": decoded,
                            }
                            self.save_processed_frame(frame)

                    else:
                        frame = {
                            "time": analyzer_defs.now_str(),
                            "cob": cob,
                            "type": ftype,
                            "dir": "TX" if is_tx else "RX",
                            "index": 0xFFFF,
                            "sub": 0xFF,
                            "name": "??",
                            "data_type": "",
                            "access_type": "",
                            "raw": raw,
                            "decoded": "No reference in EDS"
                        }
                        self.save_processed_frame(frame)

                # TIME frame
                elif ftype == analyzer_defs.frame_type.TIME:
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
                    frame = {
                        "time": analyzer_defs.now_str(),
                        "cob": cob,
                        "type": ftype,
                        "dir": "TX" if is_tx else "RX",
                        "index": 0,
                        "sub": 0,
                        "name": "TIME",
                        "raw": raw,
                        "data_type": "",
                        "access_type": "",
                        "decoded": decoded
                    }
                    self.save_processed_frame(frame)


                # Emergency (EMCY) frame — generic decoding (no vendor-specific interpretation)
                elif ftype == analyzer_defs.frame_type.EMCY:
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

                    frame = {
                        "time": analyzer_defs.now_str(),
                        "cob": cob,
                        "type": ftype,
                        "dir": "TX" if is_tx else "RX",
                        "index": 0,
                        "sub": 0,
                        "name": "EMCY",
                        "data_type": "",
                        "access_type": "",
                        "raw": raw,
                        "decoded": decoded
                        }
                    self.save_processed_frame(frame)


                # Heartbeat (HB) frame
                elif ftype == analyzer_defs.frame_type.HB:
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

                    frame = {
                        "time": analyzer_defs.now_str(),
                        "cob": cob,
                        "type": ftype,
                        "dir": "TX" if is_tx else "RX",
                        "index": 0,
                        "sub": 0,
                        "name": "HB",
                        "data_type": "",
                        "access_type": "",
                        "raw": raw,
                        "decoded": decoded
                    }
                    self.save_processed_frame(frame)


                # Other frames type
                else:
                    frame = {
                        "time": analyzer_defs.now_str(),
                        "cob": cob,
                        "type": ftype,
                        "dir": "TX" if is_tx else "RX",
                        "index": 0,
                        "sub": 0,
                        "name": "",
                        "data_type": "",
                        "access_type": "",
                        "raw": raw,
                        "decoded": ""
                    }
                    self.save_processed_frame(frame)

                # optionally mark task done if using task tracking
                try:
                    self.raw_frame.task_done()
                except Exception:
                    pass

        finally:
            with self._export_lock:
                self._close_all_exports()
            self.log.info("Exiting frame processing thread.")

    def stop(self):
        """! Request the processor thread to stop.
        @details
        Signals the internal stop event so the processing loop exits at the
        next opportunity. This method does not block waiting for thread exit;
        call `join()` on the thread object if synchronous shutdown is required.
        """
        self._stop_event.set()
        self.log.debug("Stop requested for frame processing thread")