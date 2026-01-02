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

[![MIT licensed](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2Fiota2%2FCANopen-tools-suite.svg?type=shield&issueType=license)](https://app.fossa.com/projects/git%2Bgithub.com%2Fiota2%2FCANopen-tools-suite?ref=badge_shield&issueType=license)
[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2Fiota2%2FCANopen-tools-suite.svg?type=shield&issueType=security)](https://app.fossa.com/projects/git%2Bgithub.com%2Fiota2%2FCANopen-tools-suite?ref=badge_shield&issueType=security)

# CANopen Tools Suite

**🟢 Version:** <code><!-- VERSION:START -->v0.25.0<!-- VERSION:END --></code>

A collection of **CANopen utilities** for development and debugging:

- **Sniffer (GUI/CLI):** capture and decode PDO/SDO traffic using EDS files
- **Frame Simulator:** generate CANopen traffic aligned with EDS mappings
- **Node Monitor:** live OD variable monitor with Rich TUI and command panel

Check [wiki pages](https://github.com/iota2/CANopen-tools-suite/wiki) for more details regarding setup and tools.

---

## Dependencies

Install required Python dependencies:

```bash
pip install python-can canopen rich PyQt5 tqdm pytest pre-commit
```

### Pre-Commit Setup

This project uses **[pre-commit](https://pre-commit.com/)** hooks to automatically verify that
license headers are present in all files before committing.

To enable it, install the hooks once in your local clone:

```bash
pre-commit install
```

After running pre-commit install, the hooks defined in `.pre-commit-config.yaml` run automatically on every git commit (they operate on the staged files).
If any hook fails or reports missing headers, it will block the commit.

### Keep hooks up-to-date

To update the local hook versions defined in `.pre-commit-config.yaml`:

```bash
pre-commit autoupdate
```

### Run all hooks locally (manual / troubleshooting)

You can run all configured hooks against all tracked files (useful for CI parity or repo-wide fixes):

```bash
# Run all configured hooks against all files in the repo
pre-commit run --all-files

# Run a specific hook across all files (e.g. the auto-fix)
pre-commit run fix-license-headers --all-files -v
```

If a hook auto-fixes files (for example, the license header fixer), it will modify and re-stage them; you will then need to git add and commit the changes.

#### Check license headers

```bash
# Check all files in the repository
pre-commit run check-license-headers --all-files

# Check only specific files
pre-commit run check-license-headers --files path/to/file.py path/to/README.md
```

#### Automatically add missing headers (for maintainers):

```bash
pre-commit run fix-license-headers --all-files
```

This will run the internal `tools/add_license_headers.sh` script to fix headers automatically.

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

## 3rd Party License

This project uses PySide6 under the terms of the LGPL-3.0 license.
PySide6 is dynamically linked and remains unmodified.

