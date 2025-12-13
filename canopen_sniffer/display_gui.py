#!/usr/bin/env python3
# ██╗ ██████╗ ████████╗ █████╗ ██████╗
# ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
# ██║██║   ██║   ██║   ███████║ █████╔╝
# ██║██║   ██║   ██║   ██╔══██║██╔═══╝
# ██║╚██████╔╝   ██║   ██║  ██║███████╗
# ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝
# Copyright (c) 2025 iota2 (iota2 Engineering Tools)
# Licensed under the MIT License. See LICENSE file in the project root for details.

"""
@file display_gui.py
@brief GUI display placeholder (PyQt/Tkinter) for the sniffer.
@details
This module provides a GUI frontend stub that can be extended to create a
graphical interface. The provided classes show how to:
 - Hook into the `processed_frame` queue for live updates.
 - Build tables and time-series graphs from snapshots produced by
   bus_stats.get_snapshot().

The GUI backend is not guaranteed to be bundled with the CLI/TUI; it is an
optional addition and may require additional Python packages to be installed.
"""

import logging

import threading
import queue

import sniffer_defs as sniffer_defs

class display_gui(threading.Thread):
    """! Placeholder GUI display thread — for now consume queue and log the frames.
       Replace this run() with actual Qt event integration later if needed.
    """

    def __init__(self, processed_frame: queue.Queue):
        super().__init__(daemon=True)
        self.processed_frame = processed_frame
        self._stop_event = threading.Event()
        self.log = logging.getLogger(self.__class__.__name__)

    def run(self):
        self.log.info("display_gui started (placeholder)")
        get_timeout = 0.1
        try:
            while not self._stop_event.is_set():
                try:
                    pframe = self.processed_frame.get(timeout=get_timeout)
                except queue.Empty:
                    continue

                # Inside display_gui.run(), where you currently do:
                self.log.info("GUI frame: type=%s cob=0x%03X name=%s raw=%s decoded=%s")

                # Replace with:
                msg = (f"GUI frame: type={(pframe.get('type').name if isinstance(pframe.get('type'), sniffer_defs.frame_type) else str(pframe.get('type')))} "
                    f"cob=0x{pframe.get('cob'):03X} name={pframe.get('name')} raw={pframe.get('raw')} decoded={pframe.get('decoded')}")
                # print(msg)              # immediate console feedback
                self.log.info(msg)     # still log to logger (file/handlers)

                try:
                    self.processed_frame.task_done()
                except Exception:
                    pass

        finally:
            self.log.info("display_gui exiting")

    def stop(self):
        self._stop_event.set()
        self.log.debug("display_gui stop requested")

