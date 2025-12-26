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

    def __init__(self, stats: bus_stats, raw_frame: queue.Queue, processed_frame: queue.Queue, eds_map: eds_parser, export: bool = False):
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
        self.log = logging.getLogger(f"{analyzer_defs.APP_NAME}.{self.__class__.__name__}")

        ## EDS map/parser used to resolve (index, subindex) -> name strings.
        self.eds_map = eds_map

        ## Reference to the bus_stats instance used for recording metrics.
        self.stats = stats
        self.stats.set_start_time()

        ## Flag indicating whether processed CSV export is enabled.
        self.export = export

        ## Output filename for processed CSV export.
        self.export_filename = f"{analyzer_defs.APP_NAME}_processed.csv"

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

    def save_frame(self, cob: int, ftype: analyzer_defs.frame_type, index: int, sub: int, name: str, raw: str, decoded: str):
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

        now = analyzer_defs.now_str()

        frame = {
            "time": now,
            "cob": cob,
            "type": ftype,
            "index": index,
            "sub": sub,
            "name": name,
            "raw": raw,
            "decoded": decoded,
        }

        # Decide log level once
        is_od_frame = ftype in (
            analyzer_defs.frame_type.PDO,
            analyzer_defs.frame_type.SDO_REQ,
            analyzer_defs.frame_type.SDO_RES,
        )

        log_fn = self.log.debug
        if is_od_frame and index == 0x0000:
            log_fn = self.log.error

        log_fn(
            "Processed frame: [%s] [%s] [0x%03X] [0x%04X] [0x%02X] [%s] [%s] [%s]",
            now, ftype.name, cob, index, sub, name, raw, decoded)

        # Drop unresolved OD frames only
        if not (is_od_frame and index == 0x0000):
            self.processed_frame.put(frame)

    def save_frame_to_csv(self, cob: int, ftype: analyzer_defs.frame_type, index: int, sub: int, name: str, raw: str, decoded: str):
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
                analyzer_defs.now_str(),
                ftype.name if isinstance(ftype, analyzer_defs.frame_type) else str(ftype),
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
                if (self.export_serial_number % analyzer_defs.FSYNC_EVERY) == 0:
                    os.fsync(self.export_file.fileno())
            except Exception:
                pass
        except Exception as e:
            self.log.error("CSV export failed: %s", e)

    def save_processed_frame(self, cob: int, ftype: analyzer_defs.frame_type, index: int, sub: int, name: str, raw: str, decoded: str):
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
        raw_hex = analyzer_defs.bytes_to_hex(raw)
        decoded_hex = decoded if isinstance(decoded, str) else analyzer_defs.bytes_to_hex(decoded)

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

                # Need not to process transmission frames
                if frame.get("type") == "tx":
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

                # SDO request (client->server)
                if ftype == analyzer_defs.frame_type.SDO_REQ and raw and len(raw) >= 4:
                    try:
                        cs = raw[0]
                        index = raw[2] << 8 | raw[1]
                        sub = raw[3]

                        self.stats.update_sdo_request_time(index, sub)

                        name = self.eds_map.name_map.get(
                            (index, sub), f"0x{index:04X}:{sub}"
                        )

                        decoded = ""
                        payload_len = 0

                        # ---- UPLOAD REQUEST (READ) ----
                        if cs == 0x40:
                            decoded = "READ"

                        # ---- DOWNLOAD REQUEST (WRITE) ----
                        elif cs in (0x2F, 0x2B, 0x23):
                            unused = (cs >> 2) & 0x03
                            payload_len = 4 - unused
                            payload = raw[4:4 + payload_len]
                            val = int.from_bytes(payload, "little")
                            decoded = str(val)

                        # ---- ABORT (rare in REQ) ----
                        elif cs == 0x80:
                            decoded = "ABORT"

                        try:
                            self.stats.increment_payload(
                                analyzer_defs.frame_type.SDO_REQ, payload_len
                            )
                        except Exception:
                            pass

                        self.save_processed_frame(
                            cob, ftype, index, sub, name, raw, decoded
                        )

                    except Exception:
                        self.log.warning("Malformed SDO request frame while recording req time")

                # SDO response (server->client)
                elif ftype == analyzer_defs.frame_type.SDO_RES:
                    if raw and len(raw) >= 4:
                        index = raw[2] << 8 | raw[1]
                        sub = raw[3]
                    else:
                        index, sub = 0, 0

                    cs = raw[0] if raw else 0x00

                    # ---- ABORT ----
                    if cs == 0x80 and raw and len(raw) >= 8:
                        self.stats.increment_sdo_abort()
                        abort_code = int.from_bytes(raw[4:8], "little")
                        decoded = f"ABORT 0x{abort_code:08X}"
                        payload_len = 0

                    # ---- EXPEDITED UPLOAD RESPONSE ----
                    elif cs in (0x43, 0x4B, 0x4F) and raw and len(raw) == 8:
                        self.stats.increment_sdo_success()

                        # Number of unused bytes encoded in CS
                        n_unused = (cs >> 2) & 0x03
                        data_len = 4 - n_unused

                        payload = raw[4:4 + data_len]
                        val = int.from_bytes(payload, "little")
                        decoded = str(val)
                        payload_len = data_len

                    # ---- DOWNLOAD ACK (no data) ----
                    elif cs == 0x60:
                        self.stats.increment_sdo_success()
                        decoded = "OK"
                        payload_len = 0

                    else:
                        decoded = ""
                        payload_len = 0

                    try:
                        self.stats.increment_payload(
                            analyzer_defs.frame_type.SDO_RES, payload_len
                        )
                    except Exception:
                        pass

                    self.stats.update_sdo_response_time(index, sub)

                    name = self.eds_map.name_map.get(
                        (index, sub), f"0x{index:04X}:{sub}"
                    )

                    self.save_processed_frame(
                        cob, ftype, index, sub, name, raw, decoded
                    )

                # PDO frame
                elif ftype == analyzer_defs.frame_type.PDO:
                    payload_len = len(raw)
                    self.stats.increment_payload(analyzer_defs.frame_type.PDO, payload_len)
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
                    self.save_processed_frame(cob, ftype, index=0, sub=0, name="TIME", raw=raw, decoded=decoded)

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

                    self.save_processed_frame(cob, ftype, index=0, sub=0, name="EMCY", raw=raw, decoded=decoded)

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