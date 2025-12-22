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
@file analyzer_defs.py
@brief Shared constants, enums, and utility helpers for the CANopen Analyzer.
@details
This module defines global constants, enumerations, and small utility
functions that are shared across the CANopen Analyzer codebase.

It centralizes application metadata, CLI defaults, logging configuration,
and helper functions to ensure consistent behavior across modules such as
the sniffer, processor, statistics engine, and display backends.

### Responsibilities
- Define application-wide constants (names, defaults, limits)
- Provide common enumerations (e.g. CANopen frame types)
- Configure and enable logging in a centralized manner
- Provide small, reusable helper utilities for formatting and parsing

### Design Notes
- This module contains no threading or I/O logic.
- Constants are intentionally grouped to support
  `from analyzer_defs import *` in CLI-oriented modules.
- Logging is disabled by default and enabled explicitly via CLI arguments.

### Threading Model
This module is **thread-safe** by design and contains no mutable shared state
once initialized.

### Error Handling
All helper functions are defensive and avoid raising unexpected exceptions.
"""

import logging

from enum import Enum
from datetime import datetime

# --------------------------------------------------------------------------
# ----- Definitions -----
# --------------------------------------------------------------------------

## Application organization name.
APP_ORG = "iota2"

## Application name.
APP_NAME = "CANopen-Analyzer"

# --------------------------------------------------------------------------
# ----- Defaults -----
# --------------------------------------------------------------------------

## Default CAN interface to be loaded.
DEFAULT_INTERFACE = "vcan0"

## Default CAN bus bit rate (in bits per second).
DEFAULT_CAN_BIT_RATE = 1000000

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
DATA_TABLE_HEIGHT = 30

## Height of the protocol table in the CLI interface (number of rows).
PROTOCOL_TABLE_HEIGHT = 15

## Width of the graphs in the CLI interface (in characters).
STATS_GRAPH_WIDTH = 20

## Minimum number of values to be shown in Bus stats window.
MIN_STATS_SHOW = 3

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
log = logging.getLogger(f"{APP_NAME}")
log.addHandler(logging.NullHandler())

def enable_logging():
    """! Enable file-only logging, enabled through argument."""
    filename = f"{APP_NAME}.log"

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
    log = logging.getLogger(f"{APP_NAME}")
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

