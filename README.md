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

[![MIT licensed](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2Fiota2%2FCANopen-tools-suite.svg?type=shield&issueType=license)](https://app.fossa.com/projects/git%2Bgithub.com%2Fiota2%2FCANopen-tools-suite?ref=badge_shield&issueType=license)
[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2Fiota2%2FCANopen-tools-suite.svg?type=shield&issueType=security)](https://app.fossa.com/projects/git%2Bgithub.com%2Fiota2%2FCANopen-tools-suite?ref=badge_shield&issueType=security)

# CANopen Tools Suite

Version: <!-- VERSION:START -->v0.7.0<!-- VERSION:END -->

A collection of **CANopen utilities** for development and debugging:

- **Sniffer (GUI/CLI):** capture and decode PDO/SDO traffic using EDS files
- **Frame Simulator:** generate CANopen traffic aligned with EDS mappings
- **Node Monitor:** live OD variable monitor with Rich TUI and command panel

---

## Details info wiki

https://github.com/iota2/CANopen-tools-suite/wiki

## Tools Overview

### CANopen Sniffer (GUI / CLI)
- Decode PDOs and SDOs with EDS/OD metadata
- GUI: searchable table, filtering, CSV export, histogram, frame-rate graphs
- CLI: Rich tables, bus stats, logging, export
- Modes: **Fixed** (replace row) or **Sequential** (append row)

Run GUI:
```bash
python canopen_bus_sniffer_gui.py --interface vcan0 --eds sample_device.eds
```

Run CLI:
```bash
python canopen_bus_sniffer_cli.py --interface vcan0 --eds sample_device.eds --log --export
```

---

### CANopen Frame Simulator
- Parses TPDO mappings dynamically from EDS
- Sends PDOs (auto-generated values) + unmapped OD entries as SDOs
- Supports heartbeat, timestamp, emergency frames
- Logging option

Run:
```bash
python canopen_frame_simulator.py --interface vcan0 --count 20 --eds sample_device.eds --with-timestamp --with-emcy
```

---

### CANopen Node Monitor (CLI with Rich TUI)
- Uses LocalNode + RemoteNode from EDS
- Displays raw CAN frames, decoded PDOs, SDO requests/responses
- Split OD variable tables + live status panel
- Interactive command input panel
- CSV export of OD changes + logging

Run:
```bash
python canopen_node_monitor_cli.py --interface vcan0 --local-eds local.eds --remote-eds remote.eds --export --log
```

---

## Dependencies

```bash
pip install python-can canopen rich PyQt5 tqdm pytest
```

---

## SocketCAN Setup

### Virtual CAN
```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set vcan0 up
```

### Physical CAN
```bash
sudo ip link set can0 type can bitrate 1000000
sudo ip link set can0 up
```

---

## Typical Workflow

1. Start a virtual CAN (`vcan0`) or physical CAN (`can0`)
2. Run **Frame Simulator** to generate traffic (optional)
3. Use **Sniffer** (GUI/CLI) or **Node Monitor** to observe traffic
4. Export logs / CSVs for analysis

---

## Logging & Export

- **Sniffer:** CSV export (data, histogram)
- **Simulator:** logs to `simulate_can_frames.log` (optional)
- **Node Monitor:** logs to `canopen_node_monitor_cli.log`, OD variable changes to CSV (if enabled)

---

⚡ This suite is designed for **testing, debugging, and visualizing CANopen networks** with minimal setup.
