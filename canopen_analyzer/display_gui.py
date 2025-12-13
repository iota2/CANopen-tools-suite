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
@file display_gui.py
@brief Placeholder GUI display backend for the CANopen Analyzer.
@details
This module provides a minimal GUI display stub intended for future
extension using frameworks such as Qt or Tkinter.

It demonstrates how processed frames can be consumed from a shared queue
and integrated into a graphical event loop.

### Responsibilities
- Consume processed frames from the processing queue
- Serve as a structural example for GUI integration

### Design Notes
- This backend is optional and not required for CLI/TUI operation.
- No actual GUI widgets are implemented in the current version.

### Threading Model
Runs as a daemon thread and processes frames independently of other backends.

### Error Handling
All errors are logged and do not affect the core analyzer runtime.
"""

import logging

import threading
import queue

import analyzer_defs as analyzer_defs

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
                msg = (f"GUI frame: type={(pframe.get('type').name if isinstance(pframe.get('type'), analyzer_defs.frame_type) else str(pframe.get('type')))} "
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

