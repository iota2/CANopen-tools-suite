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
@brief Threaded CANopen raw frame sniffer and exporter.
@details
This module implements the @ref canopen_sniffer thread, which connects to a
SocketCAN interface, receives raw CAN frames, and forwards them for further
processing.

The sniffer optionally exports raw frames to export file and attempts to
attach to a CANopen network object for future extensions.

### Responsibilities
- Open and manage the CAN socket interface
- Receive raw CAN frames in a non-blocking loop
- Push received frames to a shared processing queue
- Optionally export raw frames to file
- Perform graceful shutdown of CAN resources

### Design Notes
- This module performs no CANopen decoding or classification.
- Frames are passed downstream as lightweight dictionaries.
- Network connection failures are non-fatal.

### Threading Model
Runs as a dedicated daemon thread. Communication with downstream consumers
is performed via thread-safe queues.

### Error Handling
CAN I/O errors are handled defensively and do not crash the application
during shutdown or transient failures.
"""

import os
import csv
import json
import time
import struct
import logging

import threading
import queue

import can
from can import exceptions as can_exceptions
import canopen

from scapy.utils import PcapWriter
from scapy.data import DLT_CAN_SOCKETCAN

import analyzer_defs as analyzer_defs

class canopen_sniffer(threading.Thread):
    """! CANopen bus sniffer thread.
    @details
    The sniffer opens a `socketcan` interface, receives `can.Message` frames,
    enqueues them on `raw_frame` for downstream processing, and optionally writes
    raw frames to an export file for offline analysis. The thread supports a graceful
    shutdown via `stop()`. Logging is performed on a per-instance logger.
    """

    def __init__(self, interface: str, raw_frame: queue.Queue = None, requested_frame=None, export: str | None = None):
        """! Initialize CAN sniffer thread and open resources.
        @details
        The constructor opens the socketcan Bus and attempts to connect a
        CANopen Network (non-fatal). If export is enabled, the export file
        and writer are created and a header row is persisted.
        @param interface CAN interface name as string (e.g., "can0" or "vcan0").
        @param raw_frame `queue.Queue` instance to push received frames for processing.
        @param export `csv`, `json`, `pcap`: enable export of raw frames to a file.
        """

        super().__init__(daemon=True)

        ## Queue used to push raw frames for downstream processing.
        self.raw_frame = raw_frame or queue.Queue()

        ## Queue used to receive frames for sending over CAN bus.
        self.requested_frame = requested_frame or queue.Queue()

        ## Thread stop event used to signal the run loop to exit.
        self._stop_event = threading.Event()

        ## Logger instance for this sniffer.
        self.log = logging.getLogger(f"{analyzer_defs.APP_NAME}.{self.__class__.__name__}")

        ## CAN interface name used by the sniffer.
        self.interface = interface

        ## Flag indicating whether export is enabled.
        self.export = export  # None | csv | json | pcap

        ## File name used when export is enabled.
        self.export_filename = None

        ## File object for export (or None if not exporting).
        self.export_file = None

        ## Writer instance used to write exported data (or None).
        self.export_writer = None

        ## Export serial number (incremented for each exported row).
        self.export_serial_number = 1

        if self.export == "csv":
            try:
                self.export_filename = f"{analyzer_defs.APP_NAME}_raw.csv"
                self.export_file = open(self.export_filename, "w", newline="")
                self.export_writer = csv.writer(self.export_file)
                self.export_writer.writerow(
                    ["S.No.", "Time", "Type", "COB-ID", "Error", "Raw"]
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

        elif self.export == "json":
            try:
                self.export_filename = f"{analyzer_defs.APP_NAME}_raw.json"
                self.export_file = open(self.export_filename, "w")

                # JSON array start
                self.export_file.write("[\n")
                ## Identifier for first element of JSON file.
                self._json_first = True

                self.log.info(f"JSON export enabled → {self.export_filename}")
            except Exception as e:
                self.log.exception("Failed to open JSON export file: %s", e)
                self.export = False

        elif self.export == "pcap":
            try:
                self.export_filename = f"{analyzer_defs.APP_NAME}_raw.pcap"
                ## PCAP writer object for exporting file.
                self.pcap_writer = PcapWriter(
                    self.export_filename,
                    append=False,
                    sync=True,
                    linktype=DLT_CAN_SOCKETCAN
                )
                self.log.info("PCAP export enabled (Scapy, SocketCAN) → %s", self.export_filename)
            except Exception as e:
                self.log.exception("Failed to open PCAP export file: %s", e)
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

    def _json_safe_raw_frame(self, frame: dict) -> dict:
        return {
            "time": analyzer_defs.now_str(),
            "type": frame["type"],
            "cob": frame["cob"],
            "error": frame["error"],
            "raw": analyzer_defs.bytes_to_hex(frame["raw"]),
        }

    def _ensure_bus(self):
        """! Ensure CAN bus is available before transmitting."""

        if not getattr(self, "bus", None):
            raise RuntimeError("CAN bus not initialized")

    def _handle_requested_frame(self):
        """! Dispatch queued control commands from UI layers."""

        try:
            while True:
                req = self.requested_frame.get_nowait()
                self._dispatch_request(req)
                self.requested_frame.task_done()
        except queue.Empty:
            pass

    def _dispatch_request(self, req: dict):
        """! Send request frame on CAN bus."""

        rtype = req.get("type")

        if rtype == "sdo_download":
            self.send_sdo_download(
                node_id=req["node"],
                index=req["index"],
                subindex=req["sub"],
                value=req["value"],
                size=req["size"],
            )

        elif rtype == "sdo_upload":
            self.send_sdo_upload_request(
                node_id=req["node"],
                index=req["index"],
                subindex=req["sub"],
            )

        elif rtype == "pdo":
            self.send_raw_pdo(
                cob_id=req["cob"],
                data=req["data"],
            )

        else:
            self.log.warning("Unknown request type: %s", rtype)

    # --- File export helper ---
    def export_raw_frame(self, frame: dict, msg: can.Message | None = None):
        """! Save a received CAN frame (raw view) to an export file.
        @details
        Writes a single row with a serial number, timestamp, COB-ID,
        error flag and raw payload. Periodically flushes and fsyncs the file
        according to `defs.FSYNC_EVERY`.
        @param frame Frame to be exported.
        @param msg CANopen message to be exported.
        @return None.
        """

        if not self.export:
            return

        if self.export == "csv":
            try:
                self.export_writer.writerow([
                    self.export_serial_number,
                    analyzer_defs.now_str(),
                    frame["type"],
                    f"0x{frame['cob']:03X}",
                    frame["error"],
                    analyzer_defs.bytes_to_hex(frame["raw"]),
                ])
                self.export_serial_number += 1
                # flush and fsync periodically
                try:
                    self.export_file.flush()
                    if (self.export_serial_number % analyzer_defs.FSYNC_EVERY) == 0:
                        os.fsync(self.export_file.fileno())
                except Exception:
                    pass
            except Exception as e:
                self.log.error("CSV export failed: %s", e)

        elif self.export == "json":
            try:
                obj = self._json_safe_raw_frame(frame)

                if not self._json_first:
                    self.export_file.write(",\n")
                self._json_first = False

                json.dump(obj, self.export_file, indent=2, ensure_ascii=False)

                try:
                    self.export_file.flush()
                except Exception:
                    pass

            except Exception as e:
                self.log.error("JSON export failed: %s", e)

        elif self.export == "pcap" and msg is not None and self.pcap_writer:
            try:
                # --- CAN ID (29-bit, then flags) ---
                can_id = msg.arbitration_id & 0x1FFFFFFF

                if msg.is_extended_id:
                    can_id |= 0x80000000  # CAN_EFF_FLAG
                if msg.is_remote_frame:
                    can_id |= 0x40000000  # CAN_RTR_FLAG

                # IMPORTANT:
                # CANopen EMCY is NOT a SocketCAN error frame
                if msg.is_error_frame and msg.arbitration_id == 0:
                    can_id |= 0x20000000  # CAN_ERR_FLAG

                # --- DLC must be actual data length ---
                data = bytes(msg.data)
                can_dlc = len(data)
                data = data.ljust(8, b"\x00")

                # --- MUST be network (big-endian) ---
                frame = struct.pack(
                    "!IB3x8s",
                    can_id,
                    can_dlc,
                    data
                )

                self.pcap_writer.write(frame)

            except Exception as e:
                self.log.error("PCAP export failed: %s", e)

    # --- Message handling ---
    def handle_received_message(self, msg: can.Message):
        """! Handle a received CAN message.
        @details
        Extracts arbitration id, raw payload and error flag, builds a small
        frame dictionary containing a timestamp and pushes it to `raw_frame`.
        Also logs the raw frame and triggers export if enabled.
        @param msg The `can.Message` instance received from the bus.
        """

        # Total received data
        cob = msg.arbitration_id
        raw = msg.data
        error = msg.is_error_frame

        frame = {"time": time.time(), "type": "rx", "cob": cob, "error": error, "raw": raw}
        # Push frame to queue and export if enabled.
        self.raw_frame.put(frame)
        self.export_raw_frame(frame, msg)

        self.log.debug(f"Rx Raw frame: [{analyzer_defs.now_str()}] [0x{cob:03X}] [{error}] [{analyzer_defs.bytes_to_hex(raw)}]")

    # --- SDO Download (Expedited Write) ---
    def send_sdo_download(self, node_id: int, index: int, subindex: int, value: int, size: int):
        """! SDO Download.
        @details
        Send expedited SDO download (write).
        @param node_id Node ID (1-127)
        @param index Object Dictionary index
        @param subindex Subindex
        @param value Integer value to write
        @param size Data size in bytes (1,2,4)
        """

        self._ensure_bus()

        if size not in (1, 2, 4):
            raise ValueError("SDO expedited size must be 1, 2 or 4 bytes")

        # Command specifier
        cs_map = {1: 0x2F, 2: 0x2B, 4: 0x23}
        cs = cs_map[size]

        payload = bytearray(8)
        payload[0] = cs
        payload[1] = index & 0xFF
        payload[2] = (index >> 8) & 0xFF
        payload[3] = subindex & 0xFF
        payload[4:4+size] = value.to_bytes(size, "little", signed=False)

        cob_id = 0x600 + node_id

        msg = can.Message(
            arbitration_id=cob_id,
            data=bytes(payload),
            is_extended_id=False
        )

        self.bus.send(msg)
        frame = {"time": analyzer_defs.now_str(), "type": "tx", "cob": cob_id, "error": "", "raw": msg.data}
        # Push frame to queue and export if enabled.
        self.raw_frame.put(frame)
        self.export_raw_frame(frame, msg)

        self.log.debug("SDO-Download Tx Raw frame: [%s] [0x%03X] [%s] [%s]", analyzer_defs.now_str(), cob_id, "", analyzer_defs.bytes_to_hex(bytes(payload)))

    # --- SDO Upload Request (Read) ---
    def send_sdo_upload_request(self, node_id: int, index: int, subindex: int):
        """! SDO Receive.
        @details
        Send SDO upload request (read).
        @param node_id Node ID (1-127)
        @param index Object Dictionary index
        @param subindex Subindex
        """

        self._ensure_bus()

        payload = bytearray(8)
        payload[0] = 0x40  # Initiate upload
        payload[1] = index & 0xFF
        payload[2] = (index >> 8) & 0xFF
        payload[3] = subindex & 0xFF

        cob_id = 0x600 + node_id

        msg = can.Message(
            arbitration_id=cob_id,
            data=bytes(payload),
            is_extended_id=False
        )
        self.bus.send(msg)

        frame = {"time": analyzer_defs.now_str(), "type": "tx", "cob": cob_id, "error": "", "raw": msg.data}
        # Push frame to queue and export if enabled.
        self.raw_frame.put(frame)
        self.export_raw_frame(frame, msg)

        self.log.debug("SDO-Upload Tx Raw frame: [%s] [0x%03X] [%s] [%s]", analyzer_defs.now_str(), cob_id, "", analyzer_defs.bytes_to_hex(bytes(payload)))

    # --- Raw PDO Send ---
    def send_raw_pdo(self, cob_id: int, data: bytes):
        """! Send PDO.
        @details
        Send raw PDO frame.
        @param cob_id PDO COB-ID
        @param data Up to 8 bytes
        """

        self._ensure_bus()

        if len(data) > 8:
            raise ValueError("PDO data length must be <= 8 bytes")

        msg = can.Message(
            arbitration_id=cob_id,
            data=data,
            is_extended_id=False
        )
        self.bus.send(msg)

        frame = {"time": analyzer_defs.now_str(), "type": "tx", "cob": cob_id, "error": "", "raw": msg.data}
        # Push frame to queue and export if enabled.
        self.raw_frame.put(frame)
        self.export_raw_frame(frame, msg)

        self.log.debug("PDO Tx Raw frame: [%s] [0x%03X] [%s] [%s]", analyzer_defs.now_str(), cob_id, "", analyzer_defs.bytes_to_hex(bytes(data)))

    def run(self):
        """! Main loop of the sniffer thread.

        @details
        Continuously receives frames from the CAN bus using a short timeout,
        handles interrupt-like exceptions gracefully, and delegates message
        processing to `handle_received_message`. On exit, export file and bus resources
        are closed/shutdown cleanly.
        """
        self.log.info("Sniffer thread started (interface=%s)", self.interface)
        recv_timeout = 0.1

        try:
            while not self._stop_event.is_set():

                # Handle outgoing requests (NEW)
                try:
                    self._handle_requested_frame()
                except can_exceptions.CanOperationError as e:
                    # Happens when the underlying socket is closed during shutdown.
                    # If we are stopping, treat silently; otherwise warn and break.
                    if self._stop_event.is_set():
                        self.log.warning("CanOperationError during shutdown: %s", e)
                        break
                    self.log.error("CAN operation error (send): %s", e)

                # Handle incoming CAN frames
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
                        self.log.warning("CanOperationError during shutdown: %s", e)
                        break
                    self.log.error("CAN operation error (recv): %s", e)
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
                        self.handle_received_message(msg)
                    except Exception:
                        self.log.exception("Exception while handling message")

        finally:
            # Always attempt to flush/close export (if any) and shutdown resources safely.
            try:
                export_file = getattr(self, "export_file", None)

                if export_file:
                    # Format-specific finalization
                    if self.export == "json":
                        try:
                            export_file.write("\n]\n")
                        except Exception:
                            pass

                    # Best-effort flush + fsync for file-based exports
                    try:
                        export_file.flush()
                        os.fsync(export_file.fileno())
                    except Exception:
                        pass

                    try:
                        export_file.close()
                    except Exception:
                        pass

                # PCAP writer has its own close semantics
                pcap_writer = getattr(self, "pcap_writer", None)
                if pcap_writer:
                    try:
                        pcap_writer.close()
                        self.log.info("PCAP writer closed")
                    except Exception as e:
                        self.log.warning("Failed to close PCAP writer: %s", e)

                if export_file or pcap_writer:
                    self.log.info("Raw export resources closed")

            except Exception:
                self.log.exception("Failed during raw export cleanup")

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
