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

# Changelog

**🟢 Version:** <code><!-- VERSION:START -->v0.20.0<!-- VERSION:END --></code>

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [v0.20.0] - 2026-01-01

- Added tooltips on name's columns of SDO and PDO data in GUI.

## [v0.19.0] - 2026-01-01
- Added send / receive support for SDO and PDOs on TUI.

## [v0.18.0] - 2025-12-30

- Updated sniffer script for sends / receive SDOs and RPDOs.
- Added option to sniffer script to pass node-id through CLI.
- Added requested frames queue to send data over CAN.
- Added Send SDO / PDO option to GUI.
- Fixed bug that was causing only Counts to be updated in data tables in fixed mode.
- Updated sample EDS files.
- Added support for copying data from GUI data tables.
- Added frame directions to PDO and SDO tables.

## [v0.17.0] - 2025-12-22

- Bus Active/ Idle detection.
- Reset bus stats on nodes becoming inactive.
- Added active Node Ids to CLI, TUI and GUI.

## [v0.16.0] - 2025-12-22

- Updated canopen_bus_sniffer to support dark mode.
- Added GUI support to CANopen-Analyzer

## [v0.15.0] - 2025-12-13

- Added support to TUI interface.
- Separated files for Display interfaces, bus stats calculation and common defs.
- Restructuring of CANopen analyzer classes and updated documentation.

## [v0.14.0] - 2025-11-15

- Updated BUS-STATS class to calculate bus transfer rates.
- Added CLI display capabilities.
- Added dummy display processes for GUI and handled processed frame queue in them.
- Updated logger module, to log in file if `--log` is passed, else print on console.
- Added Doxygen documentation generation to pre-commit
- Fixed TIME frame in frame_simulator.
- Added ERROR frame to frame_simulator.
- In Sniffer Added decoding for TIME, HB and EMCY messages.

## [v0.12.0] - 2025-10-25

- Added feature and bug GIT issue templates.
- Added CHANGELOG check job to through a warning if no logs present under `Unreleased`.
- Added CHANGELOG check to precommit, and updated README.

## [v0.11.0] - 2025-10-19

- Fixed changelog for v0.10.0.

## [v0.10.0] - 2025-10-19

- Removed CHANGELOG check for ci pipeline to pass.

## [v0.9.0] - 2025-10-19

- Added `tools/add_license_header.sh` script to automatically add header to `.py`, `.md`, `.sh` and `.yml` files.
- Added `pre-commit` and ci `workflow` support to check license headers.
- Added CHANGELOG check for ci pipeline to pass.

## [v0.8.0] - 2025-10-19

- Updated release notes processing to skip version strings from changelog.
- Added `test_changelog_extract.sh` to test release notes before pushing and running as a workflow.

## [v0.7.0] - 2025-10-18

- Updated `manual-create-release.yml` to pick release notes from CHANGELOG file.
- Fixed missing versions from CHANGELOG.

## [v0.6.0] - 2025-10-18

- Fixed `manual-create-release.yml` syntax errors.

## [v0.5.0] - 2025-10-18

- Updated `manual-create-release.yml` workflow to enable release between two tags.

## [v0.4.0] - 2025-10-18

- Added `--no-release-notes` option to not release all tags on merge.
- Added `manual-create-release.yml` workflow for creating releases manually.

## [v0.3.0] - 2025-10-18

- Fixed CI/CD Pipeline with release notes.

## [v0.2.0] - 2025-10-18

- Add Unreleased change logs to release notes.

## [v0.1.0] - 2025-10-18

- Added version bump and CHANGELOG.

## [v0.0.1] - 2025-09-10

- CANopen Sniffer (GUI / CLI)
    - Decode PDOs and SDOs with EDS/OD metadata
    - GUI: searchable table, filtering, CSV export, histogram, frame-rate graphs
    - CLI: Rich tables, bus stats, logging, export
    - Modes: Fixed (replace row) or Sequential (append row)
- CANopen Frame Simulator
    - Parses TPDO mappings dynamically from EDS
    - Sends PDOs (auto-generated values) + unmapped OD entries as SDOs
    - Supports heartbeat, timestamp, emergency frames
    - Logging option
- CANopen Node Monitor (CLI with Rich TUI)
    - Uses LocalNode + RemoteNode from EDS
    - Displays raw CAN frames, decoded PDOs, SDO requests/responses
    - Split OD variable tables + live status panel
    - Interactive command input panel
    - CSV export of OD changes + logging

[v0.20.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.19.0...v0.20.0
[v0.19.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.18.0...v0.19.0
[v0.18.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.17.0...v0.18.0
[v0.17.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.16.0...v0.17.0
[v0.16.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.15.0...v0.16.0
[v0.15.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.14.0...v0.15.0
[v0.14.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.12.0...v0.14.0
[v0.12.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.11.0...v0.12.0
[v0.11.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.10.0...v0.11.0
[v0.10.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.9.0...v0.10.0
[v0.9.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.8.0...v0.9.0
[v0.8.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.7.0...v0.8.0
[v0.7.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.6.0...v0.7.0
[v0.6.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.5.0...v0.6.0
[v0.5.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.4.0...v0.5.0
[v0.4.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.3.0...v0.4.0
[v0.3.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.2.0...v0.3.0
[v0.2.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.1.0...v0.2.0
[v0.1.0]: https://github.com/iota2/CANopen-tools-suite/compare/v0.0.1...v0.1.0
[v0.0.1]: https://github.com/iota2/CANopen-tools-suite/tree/v0.0.1
