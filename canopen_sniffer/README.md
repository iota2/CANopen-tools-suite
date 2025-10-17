> `iota2` - Making Imaginations, Real
>
> <i2.iotasquare@gmail.com>


```
 ██╗ ██████╗ ████████╗ █████╗ ██████╗
 ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
 ██║██║   ██║   ██║   ███████║ █████╔╝
 ██║██║   ██║   ██║   ██╔══██║██╔═══╝
 ██║╚██████╔╝   ██║   ██║  ██║███████╗
 ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝
```

---

# CANopen Sniffer

A lightweight, Python-based CANopen bus sniffer and frame analyzer supporting **real-time monitoring**, **SDO/PDO decoding**, and **CSV export**.
Designed for engineers working on **CANopen nodes** or **embedded systems**.

---

## Features

- Live capture of CANopen frames using `python-can` (SocketCAN backend)
- Decoding of **PDO**, **SDO Request/Response**, **NMT**, **Heartbeat**, **SYNC**, and **TIME** frames
- Integrated **Bus Statistics** and **Top Talker** tracking
- **EDS File Parsing** for dynamic Object Dictionary name mapping
- Dual-threaded architecture:
  - `can_sniffer` — raw frame capture
  - `process_frame` — frame classification, decoding & export
- CSV export for both raw and processed CAN data
- Graceful thread shutdown and signal handling
- Optional logging to file and console

---

## Architecture Overview

```
┌────────────────────────────────────────────┐
│                  main()                    │
│  Parses args → starts threads → waits exit │
└────────────────────────────────────────────┘
          │
          ▼
┌────────────────────────────────────────────┐
│             can_sniffer(Thread)            │
│  - Captures CAN frames from interface      │
│  - Pushes to shared queue (raw_frame)      │
│  - Optional CSV export                     │
└────────────────────────────────────────────┘
          │
          ▼
┌────────────────────────────────────────────┐
│           process_frame(Thread)            │
│  - Consumes frames from queue              │
│  - Classifies & decodes via EDS map        │
│  - Updates bus_stats                       │
│  - Optional processed CSV export           │
└────────────────────────────────────────────┘
```

---

## Command-Line Usage

```bash
python canopen_sniffer.py [options]
```

### Options

| Argument | Description | Default |
|-----------|--------------|----------|
| `--interface` | CAN interface (e.g. `can0`, `vcan0`) | `vcan0` |
| `--bitrate` | CAN bus bitrate (bps) | `1000000` |
| `--mode` | Run mode: `cli` or `gui` | `cli` |
| `--eds` | Path to EDS file for decoding | *optional* |
| `--export` | Enable CSV export | *off* |
| `--log` | Enable logging | *off* |
| `--fixed` | Keep CLI table fixed height | *off* |

Example:
```bash
python canopen_sniffer.py --interface vcan0 --eds ./device.eds --export --log
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

## Example Output

```
[12:10:43.456] [PDO] [0x201] [0x6041:00] [StatusWord] [23 10]
[12:10:44.102] [SDO_REQ] [0x601] [0x6060:00] [ModeOfOperation] [Set:6]
[12:10:44.103] [SDO_RES] [0x581] [0x6060:00] [ModeOfOperation] [OK]
```

---

## Dependencies

- Python ≥ 3.9
- [python-can](https://pypi.org/project/python-can/)
- [canopen](https://pypi.org/project/canopen/)
- [rich](https://pypi.org/project/rich/) *(for CLI interface)*

Install dependencies with:

```bash
pip install python-can canopen rich
```

---

## Doxygen Integration

Doxygen documentation will be available under `./dox` directory.
To regenerate the doxygen documentation, run following command in this scripts root directory:

```bash
doxygen dox/config
```

---
