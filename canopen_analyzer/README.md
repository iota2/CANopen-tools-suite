```
 ██╗ ██████╗ ████████╗ █████╗ ██████╗
 ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
 ██║██║   ██║   ██║   ███████║ █████╔╝
 ██║██║   ██║   ██║   ██╔══██║██╔═══╝
 ██║╚██████╔╝   ██║   ██║  ██║███████╗
 ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝
 Copyright (c) 2025 iota2 (iota2 Engineering Tools)
 Licensed under the MIT License. See LICENSE file in the project root for details.
```

---

# CANopen Analyzer

A modular, Python-based CANopen bus **sniffer and analyzer** supporting
**real-time monitoring**, **SDO/PDO decoding**, **statistics**, and **CSV export**.
Designed for engineers working on **CANopen devices**, **embedded systems**,
and **industrial networks**.

---

## Overview

The CANopen Analyzer is organized as a
**cleanly layered, multi-threaded architecture**,
separating concerns such as CAN I/O, frame decoding, statistics,
and presentation.

Supported frontends:
- Headless CLI (Rich-based)
- Interactive TUI (Textual)
- Optional GUI backend (extension point)

---

## Key Features

- Live CAN frame capture using `python-can` (SocketCAN / PCAN)
- Full decoding of:
  - PDO (RPDO / TPDO)
  - SDO request / response
  - NMT, SYNC, TIME, EMCY, Heartbeat
- EDS-driven Object Dictionary and PDO name resolution
- Thread-safe bus statistics:
  - Frame distribution
  - Top talkers
  - Node presence
  - SDO latency and error tracking
- CSV export for both raw and decoded frames
- Rich CLI tables and Textual-based interactive TUI
- Graceful shutdown and signal handling
- Doxygen-ready documentation with Mermaid diagrams
- Optional logging to file and console

---

## Architecture Overview

```
┌─────────────────────┐
│ CAN Interface (HW)  │
│ (SocketCAN / PCAN)  │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│ canopen_sniffer.py  │   Raw CAN frames
│  (reader thread)    │──────────────┐
└─────────┬───────────┘              │
          ▼                          ▼
┌─────────────────────┐      ┌─────────────────────┐
│ process_frames.py   │─────▶│ bus_stats.py        │
│ (decoder thread)    │      │ (aggregator)        │
└─────────┬───────────┘      └─────────┬───────────┘
          │                            │
          ▼                            ▼
┌─────────────────────┐      ┌─────────────────────┐
│ Display Backends    │      │ CSV Export          │
│ - display_cli.py    │      │ raw / processed     │
│ - display_tui.py    │      └─────────────────────┘
│ - display_gui.py    │
└─────────────────────┘

Entry point:
  canopen_analyzer.py
```

---

## Software Design

### Sequence Diagram — frame flow

Available under `dox/diagrams` as @mermaid{sequence}

### State Diagram — sniffer life-cycle

Available under `dox/diagrams` as @mermaid{state}

### Class Diagram — core classes & relationships

Available under `dox/diagrams` as @mermaid{class}

---

## File Structure & Responsibilities

| File | Responsibility |
|------|----------------|
| `canopen_analyzer.py` | Main application entry point, argument parsing, lifecycle control |
| `analyzer_defs.py` | Global constants, enums, logging, helpers |
| `canopen_sniffer.py` | Raw CAN frame acquisition thread |
| `process_frames.py` | CANopen decoding, statistics update, processed CSV export |
| `bus_stats.py` | Thread-safe bus statistics and rate computation |
| `eds_parser.py` | EDS parsing and Object Dictionary / PDO name resolution |
| `display_cli.py` | Rich-based CLI display backend |
| `display_tui.py` | Textual interactive TUI frontend |
| `display_gui.py` | GUI placeholder backend |
| `requirements.txt` | Python dependencies |

---

## Technical specifications

- Python: 3.10+ recommended
- Dependencies: see `requirements.txt` or install:

```bash
pip install textual rich canopen python-can
```
- [python-can](https://pypi.org/project/python-can/)
- [canopen](https://pypi.org/project/canopen/)
- [rich](https://pypi.org/project/rich/)
- [textual](https://pypi.org/project/textual/)
- SocketCAN: Linux (for `socketcan` interface) or PCAN adapter supported
  via python-can drivers.
- Expected CPU/memory: lightweight for typical loads (single-digit MBs,
  single-digit percent CPU). High-volume buses may require batching and
  increased worker threads.

---

## Usage Examples

```bash
python canopen_analyzer.py \
    --interface vcan0 \
    --eds ./device.eds \
    --mode tui \
    --export \
    --log
```

---

### Command Options

| Argument | Description | Default |
|-----------|--------------|----------|
| `--interface` | CAN interface (e.g. `can0`, `vcan0`) | `vcan0` |
| `--bitrate` | CAN bus bitrate (bps) | `1000000` |
| `--mode` | Run mode: `cli`, `tui` or `gui` | `cli` |
| `--eds` | Path to EDS file for decoding | *optional* |
| `--export` | Enable CSV export | *off* |
| `--log` | Enable logging | *off* |
| `--fixed` | Keep CLI table fixed height | *off* |

---

## Example Output

```
[12:10:43.456] [PDO] [0x201] [0x6041:00] [StatusWord] [23 10]
[12:10:44.102] [SDO_REQ] [0x601] [0x6060:00] [ModeOfOperation] [Set:6]
[12:10:44.103] [SDO_RES] [0x581] [0x6060:00] [ModeOfOperation] [OK]
```

---

## Output Files

| File | Description |
|------|--------------|
| `*_raw.csv` | Raw frames (COB-ID, data, time, error) |
| `*_processed.csv` | Decoded frames with OD info |
| `*.log` | Debug and status logs (if enabled) |

---

## Doxygen Integration

Doxygen documentation will be available under `./dox` directory.
To regenerate the doxygen documentation, run following command in this scripts root directory:

```bash
doxygen dox/config
```

| Doxygen config | File path |
|------|--------------|
| Configurations | `./dox/config` |
| Pages Layout formatting | `./dox/layout.xml` |
| HTML header style | `./dox/doxygen_header.html` |
| HTML footer style | `./dox/doxygen_header.html` |
| Mermaid loader | `./dox/mermaid.min.js/` |
| Mermaid diagrams | `./dox/diagrams/` |
| Generated documentation | `./dox/documentation/` |

---

## Pre-Commit Hooks

This repository uses **pre-commit** to enforce formatting, documentation
generation, and project-quality standards before changes are committed.

### Installation

```sh
pip install pre-commit
pre-commit install
```

### Running Hooks Manually

```sh
pre-commit run --all-files
```

Run a specific hook:

```sh
pre-commit run check-license-headers
```

### Hooks Included in This Project

| Hook Name               | Description |
|------------------------|-------------|
| **check-license-headers** | Verifies license headers in `.py`, `.sh`, `.yml`, `.yaml`, `.md` files. |
| **fix-license-headers**   | Automatically inserts or fixes license headers. *(Manual run)* |
| **check-changelog**       | Validates `CHANGELOG.md` Unreleased section. |
| **fix-changelog**         | Automatically populates CHANGELOG from git history. *(Manual run)* |
| **generate-doxygen**      | Generates and stages Doxygen documentation. |

### Notes

- Some hooks (e.g., *fix-…*) are designed for **manual execution** and do not run on every commit.
- If a hook fails, resolve the issue and commit again.

---
