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
@file canopen_analyzer.py
@brief CANopen Analyzer application entry point and lifecycle manager.
@details
This module serves as the main orchestration layer for the CANopen Analyzer
application. It is responsible for initializing configuration, wiring
together worker threads, selecting display backends, and managing graceful
startup and shutdown.

All protocol handling, CAN I/O, frame decoding, and statistics computation
are delegated to specialized modules.

### Responsibilities
- Parse command-line arguments
- Initialize logging and shared configuration
- Construct and start sniffer and processor threads
- Select and launch CLI, TUI, or GUI display backends
- Handle OS signals and perform graceful shutdown

### Architecture
This module depends on:
- @ref canopen_sniffer for raw CAN frame acquisition
- @ref process_frames for CANopen decoding and classification
- @ref eds_parser for Object Dictionary and PDO name resolution
- @ref bus_stats for statistics aggregation
- Display backends for visualization

### Design Notes
- This module contains no protocol-specific logic.
- It exists purely as a coordinator and lifecycle controller.

### Threading Model
The main thread supervises background worker threads and display threads,
remaining idle until termination is requested.

### Error Handling
Startup failures are logged and may fall back to alternate display modes
where possible. Shutdown is always attempted cleanly.
"""

import time
import logging
import argparse
import signal

from dataclasses import dataclass, field
import queue

import analyzer_defs as analyzer_defs
from eds_parser import eds_parser
from canopen_sniffer import canopen_sniffer
from process_frames import process_frames
from bus_stats import bus_stats
from display_cli import display_cli
from display_tui import display_tui
from display_gui import display_gui

def main():
    """! Main entry point for the CANopen bus analyzer application.
    @details
    This function initializes the CANopen sniffer and frame processor threads,
    handles command-line arguments, and ensures a graceful shutdown on exit
    or when the user presses Ctrl+C. It supports both CLI and GUI modes, and
    optionally enables CSV export and detailed logging.

    The main steps include:
      - Parsing command-line arguments.
      - Initializing the EDS parser and CANopen statistics.
      - Creating and launching sniffer and frame processor threads.
      - Handling SIGINT/SIGTSTP for controlled shutdown.
      - Joining and cleaning up threads and CAN resources before exit.
    """

    ## Command-line argument parser setup.
    p = argparse.ArgumentParser()
    p.add_argument("--interface", default=analyzer_defs.DEFAULT_INTERFACE, help="CAN interface (default: {analyzer_defs.DEFAULT_INTERFACE})")
    p.add_argument("--mode", default="cli", choices=["cli", "tui", "gui"], help="enable cli or gui mode (default: cli)")
    p.add_argument("--bitrate", type=int, default=analyzer_defs.DEFAULT_CAN_BIT_RATE, help="CAN bitrate (default: {analyzer_defs.DEFAULT_CAN_BIT_RATE})")
    p.add_argument("--eds", help="EDS file path (optional)")
    p.add_argument("--fixed", action="store_true", help="update rows instead of scrolling")
    p.add_argument("--export", action="store_true", help="export received frames to CSV")
    p.add_argument("--log", action="store_true", help="enable logging")
    args = p.parse_args()

    ## Enable logging if requested.
    if args.log:
        analyzer_defs.enable_logging()

    ## Parse and load EDS mapping for object dictionary and PDOs.
    if args.eds:
        eds_map = eds_parser(args.eds)
        analyzer_defs.log.debug(f"Decoded PDO map: {eds_map.pdo_map}")
        analyzer_defs.log.debug(f"Decoded NAME map: {eds_map.name_map}")

    ## Check if user passed the desired bitrate else use default.
    if args.bitrate:
        bitrate = args.bitrate
    else:
        bitrate = analyzer_defs.DEFAULT_CAN_BIT_RATE

    analyzer_defs.log.info(f"Configured CAN bitrate : {bitrate}")

    ## Initialize bus statistics and reset counters.
    stats = bus_stats(bitrate=bitrate)
    stats.reset()

    ## Shared queue for communication between sniffer and processor threads.
    raw_frame = queue.Queue()

    # Shared queue for processed frames
    processed_frame = queue.Queue()

    # Shared queue for requested frames
    requested_frame = queue.Queue()

    ## Create CANopen sniffer thread for raw CAN frame capture.
    sniffer = canopen_sniffer(interface=args.interface,
                                raw_frame=raw_frame,
                                requested_frame=requested_frame,
                                export=args.export)

    ## Create frame processor thread for classification and stats update.
    processor = process_frames(stats=stats,
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
            analyzer_defs.log.info("Loading TUI interface")
            display_tui.run_textual(stats, processed_frame=processed_frame, requested_frame=requested_frame, fixed=args.fixed)
        except Exception as e:
            analyzer_defs.log.exception("Failed to start Textual TUI: %s", e)
            # fallback to legacy CLI thread if textual unavailable
            display = display_cli(stats=stats, processed_frame=processed_frame, fixed=args.fixed)
    elif args.mode == "gui":
        display_gui(stats, processed_frame=processed_frame, requested_frame=requested_frame,fixed=args.fixed)

    if display:
        display.start()

    ## Signal handler for graceful termination (Ctrl+C).
    def _stop_all(signum, frame):
        """! Signal handler to request graceful shutdown of all worker threads."""
        analyzer_defs.log.warning("Signal %s received — stopping threads...", signum)
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
        analyzer_defs.log.info("KeyboardInterrupt received — shutting down")
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
        analyzer_defs.log.info(f"Terminating {analyzer_defs.APP_NAME}...")

        # Shutdown logging now that threads have been joined\n"
        try:
            logging.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()