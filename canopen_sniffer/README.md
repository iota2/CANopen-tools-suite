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

# CANopen Sniffer

A lightweight, Python-based CANopen bus sniffer and frame analyzer supporting **real-time monitoring**, **SDO/PDO decoding**, and **CSV export**.
Designed for engineers working on **CANopen nodes** or **embedded systems**.

---

## Overview
The CANopen Sniffer Suite is a collection of Python tools to monitor, parse,
and visualize CANopen traffic. It supports multiple frontends:
- Headless CLI (Rich-based)
- Textual TUI (interactive terminal UI)
- Optional GUI frontend (extension point)

It includes:
- EDS parsing to resolve Object Dictionary names
- RPDO/TPDO and SDO decoding
- Bus statistics collection and CSV export
- Graceful shutdown and thread-safe queues for integration into other tools

---

## Features

- Live capture of CANopen frames using `python-can` (SocketCAN backend)
- Decoding of **PDO**, **SDO Request/Response**, **NMT**, **Heartbeat**, **SYNC**, and **TIME** frames
- Integrated **Bus Statistics** and **Top Talker** tracking
- **EDS File Parsing** for dynamic Object Dictionary name mapping
- Dual-threaded architecture:
  - `can_sniffer` — raw frame capture
  - `process_frame` — frame classification, decoding & export
- Textual TUI: interactive terminal-based UI showing Protocol Data, Bus
  Stats and live graphs.
- CSV export: capture decoded frames and statistics for offline analysis.
- Improved EDS parsing: resilient name mapping and PDO resolution.
- Doxygen-ready docstrings: all modules and public functions/classes now
  contain expanded Doxygen-style comments for better generated docs.
- Graceful thread shutdown and signal handling
- Optional logging to file and console

---

## Architecture Overview

```
┌─────────────────────┐      ┌──────────────────┐      ┌──────────────────┐
| CAN Interface (HW)  | ---> | can_sniffer      | ---> | frame_processor  |
|  (SocketCAN/PCAN)   |      | (reader thread)  |      | (worker threads) |
└─────────────────────┘      └──────────────────┘      └──────────────────┘
                                         |                    |
                                         v                    v
                           ┌──────────────────┐     ┌─────────────────────┐
                           | bus_stats        |     | Output sinks        |
                           | (aggregator)     |     | - display_tui       |
                           └──────────────────┘     | - display_cli       |
                                                    | - display_gui       |
                                                    | - CSV export        |
                                                    └─────────────────────┘
```

---

### Components
- **can_sniffer**: reads raw CAN frames, performs initial classification,
  and pushes frames to the processing queue.
- **frame_processor**: decodes CANopen payloads (SDO/PDO), enriches frames
  with EDS metadata, updates bus_stats, and forwards frames to sinks.
- **bus_stats**: thread-safe aggregator of counters, rates, and timing
  histograms. Exposes `get_snapshot()` for rendering.
- **display_tui / display_cli / display_gui**: backends that render snapshots
  or frame streams for human consumption.

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
python canopen_sniffer.py --interface vcan0 --eds ./device.eds --export --log
```

---

### Options

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
| `canopen_sniffer_raw.csv` | Raw frames (COB-ID, data, time, error) |
| `canopen_sniffer_processed.csv` | Decoded frames with OD info |
| `canopen_sniffer.log` | Debug and status logs (if enabled) |

---

## Key Classes

### bus_stats
Collects CANopen bus statistics such as frame counts, top talkers, and SDO timing.

### eds_parser
Parses EDS files to build name and PDO mappings. Used by the processor thread for decoding object dictionary references.

### can_sniffer
Threaded CAN bus sniffer that reads from the SocketCAN interface, exports raw frames, and pushes them into a queue.

### process_frame
Processor thread that consumes queued frames, updates statistics, decodes SDO/PDO data, and writes processed CSVs.

### Sequence Diagram — frame flow

Available under `dox/diagrams` as @mermaid{sequence}

### State Diagram — sniffer life-cycle

Available under `dox/diagrams` as @mermaid{state}

### Class Diagram — core classes & relationships

Available under `dox/diagrams` as @mermaid{class}

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
